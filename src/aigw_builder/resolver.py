"""Resolve policy references (config entries and base-image policies) into exact build.yaml entries."""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from .config import PolicyRef
from .hub import HubError, HubNotFound, PolicyHubClient
from .image_inspect import InstalledPolicy

POLICY_REPO = "github.com/wso2/gateway-controllers"
POLICY_REPO_GIT = f"https://{POLICY_REPO}"
RAW_BASE = "https://raw.githubusercontent.com/wso2/gateway-controllers"

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_TAG_RE = re.compile(r"refs/tags/policies/([^/]+)/v(\d+)\.(\d+)\.(\d+)$")

Semver = Tuple[int, int, int]


class ResolveError(Exception):
    pass


def parse_version(value: str) -> Optional[Tuple[int, ...]]:
    """'v1.2.3' -> (1, 2, 3), '1.2' -> (1, 2), 'v1' -> (1,). None if not a version."""
    m = _VERSION_RE.match(value.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.groups() if p is not None)


def fmt_version(v: Iterable[int]) -> str:
    return "v" + ".".join(str(p) for p in v)


def gomodule_ref(name: str, tag: str) -> str:
    return f"{POLICY_REPO}/policies/{name}@{tag}"


def pip_ref(name: str, tag: str) -> str:
    return f"git+{POLICY_REPO_GIT}.git@policies/{name}/{tag}#subdirectory=policies/{name}"


class TagIndex:
    """Index of `policies/<name>/vX.Y.Z` tags in wso2/gateway-controllers (one `git ls-remote` per run)."""

    def __init__(self, lines: Optional[Iterable[str]] = None) -> None:
        self._tags: Optional[Dict[str, List[Semver]]] = None
        if lines is not None:
            self._tags = self._parse(lines)

    @staticmethod
    def _parse(lines: Iterable[str]) -> Dict[str, List[Semver]]:
        tags: Dict[str, set] = {}
        for line in lines:
            ref = line.split()[-1] if line.strip() else ""
            m = _TAG_RE.search(ref.replace("^{}", ""))
            if m:
                tags.setdefault(m.group(1), set()).add((int(m.group(2)), int(m.group(3)), int(m.group(4))))
        return {k: sorted(v) for k, v in tags.items()}

    def _load(self) -> Dict[str, List[Semver]]:
        if self._tags is None:
            try:
                proc = subprocess.run(
                    ["git", "ls-remote", "--tags", POLICY_REPO_GIT, "refs/tags/policies/*"],
                    capture_output=True, text=True, timeout=60,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise ResolveError(f"Could not list policy tags from {POLICY_REPO_GIT}: {exc}") from exc
            if proc.returncode != 0:
                raise ResolveError(f"Could not list policy tags from {POLICY_REPO_GIT}: {proc.stderr.strip()}")
            self._tags = self._parse(proc.stdout.splitlines())
        return self._tags

    def names(self) -> List[str]:
        return sorted(self._load())

    def versions(self, name: str) -> List[Semver]:
        return self._load().get(name, [])

    def best(self, name: str, spec: Optional[Tuple[int, ...]] = None) -> Optional[str]:
        """Highest tag matching the given prefix (major, major.minor or exact)."""
        candidates = [v for v in self.versions(name) if spec is None or v[: len(spec)] == tuple(spec)]
        return fmt_version(candidates[-1]) if candidates else None


class KindDetector:
    """Go policies have a go.mod at the tag; anything else is treated as a Python (pip) policy."""

    def __init__(self, probe: Optional[Callable[[str, str], bool]] = None) -> None:
        self._probe = probe or self._http_probe
        self._cache: Dict[Tuple[str, str], str] = {}

    @staticmethod
    def _http_probe(name: str, tag: str) -> bool:
        url = f"{RAW_BASE}/policies/{name}/{tag}/policies/{name}/go.mod"
        try:
            resp = httpx.head(url, timeout=20, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ResolveError(f"Could not check policy type for {name} {tag}: {exc}") from exc
        return resp.status_code == 200

    def kind(self, name: str, tag: str) -> str:
        key = (name, tag)
        if key not in self._cache:
            self._cache[key] = "go" if self._probe(name, tag) else "python"
        return self._cache[key]


@dataclass
class ResolvedPolicy:
    name: str
    version: str  # exact vX.Y.Z ('' for local policies)
    kind: str  # go | python | local
    ref: str  # gomodule / pipPackage / absolute filePath
    origin: str = "config"  # config | base
    requested: str = ""  # what the user asked for (for display)
    notes: List[str] = field(default_factory=list)

    def build_entry(self) -> Dict[str, str]:
        key = {"go": "gomodule", "python": "pipPackage", "local": "filePath"}[self.kind]
        return {"name": self.name, key: self.ref}

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name, "version": self.version, "kind": self.kind, "ref": self.ref,
            "origin": self.origin, "requested": self.requested, "notes": self.notes,
        }


@dataclass
class DiffRow:
    name: str
    status: str  # kept | added | unchanged | upgraded | downgraded | changed | excluded
    base_version: str = ""
    new_version: str = ""


class Resolver:
    def __init__(self, hub: PolicyHubClient, tags: Optional[TagIndex] = None, kinds: Optional[KindDetector] = None) -> None:
        self.hub = hub
        self.tags = tags or TagIndex()
        self.kinds = kinds or KindDetector()
        self._latest: Optional[Dict[str, str]] = None

    # -- helpers -----------------------------------------------------------------

    def _suggest(self, name: str) -> str:
        try:
            names = {p.name for p in self.hub.list_policies()}
        except HubError:
            names = set()
        names.update(self.tags.names())
        close = difflib.get_close_matches(name, sorted(names), n=3, cutoff=0.6)
        return f" Did you mean: {', '.join(close)}?" if close else ""

    def _pinned(self, name: str, tag: str, kind_override: Optional[str] = None) -> Tuple[str, str]:
        kind = kind_override or self.kinds.kind(name, tag)
        return kind, gomodule_ref(name, tag) if kind == "go" else pip_ref(name, tag)

    # -- config entries ------------------------------------------------------------

    def resolve_ref(self, ref: PolicyRef) -> ResolvedPolicy:
        if ref.is_local:
            return ResolvedPolicy(name=ref.name, version="", kind="local", ref=str(ref.path), requested=str(ref.path))

        notes: List[str] = []
        hub_versions: List[str] = []
        try:
            hub_versions = [v.version for v in self.hub.versions(ref.name)]
            hub_latest = next((v.version for v in self.hub.versions(ref.name) if v.is_latest), None)
        except HubNotFound:
            hub_latest = None
            if not self.tags.versions(ref.name):
                raise ResolveError(f"{ref.name}: not found on the Policy Hub.{self._suggest(ref.name)}")
            notes.append("not listed on the Policy Hub; resolved from gateway-controllers tags")

        if ref.version:
            spec = parse_version(ref.version)
            if spec is None:
                raise ResolveError(f"{ref.name}: invalid version {ref.version!r} (use X.Y, vX.Y.Z or vX)")
            if hub_versions and len(spec) >= 2:
                major_minor = f"{spec[0]}.{spec[1]}"
                if major_minor not in hub_versions:
                    msg = f"version {major_minor} is not on the Policy Hub (available: {', '.join(hub_versions)})"
                    if len(spec) == 2:
                        raise ResolveError(f"{ref.name}: {msg}")
                    notes.append(msg)
            requested = ref.version
        elif hub_latest:
            spec = parse_version(hub_latest)
            requested = f"latest ({hub_latest})"
        else:
            spec = None
            requested = "latest"

        tag = self.tags.best(ref.name, spec)
        if not tag:
            known = ", ".join(fmt_version(v) for v in self.tags.versions(ref.name)[-6:]) or "none"
            raise ResolveError(
                f"{ref.name}: no release tag in {POLICY_REPO} matches {requested} (recent tags: {known})"
            )
        kind, pinned = self._pinned(ref.name, tag, ref.kind)
        return ResolvedPolicy(name=ref.name, version=tag, kind=kind, ref=pinned, requested=requested, notes=notes)

    def resolve_all(self, refs: Iterable[PolicyRef]) -> Tuple[List[ResolvedPolicy], List[str]]:
        resolved: List[ResolvedPolicy] = []
        errors: List[str] = []
        for ref in refs:
            try:
                resolved.append(self.resolve_ref(ref))
            except ResolveError as exc:
                errors.append(str(exc))
        return resolved, errors

    # -- base image policies -----------------------------------------------------------

    def _base_tag(self, policy: InstalledPolicy, spec: Optional[Tuple[int, ...]]) -> Tuple[str, List[str]]:
        """Exact release tag for a base-image policy, or the nearest one in the same major.minor.

        Image manifests record the version the floating `@vN` ref resolved to at build time; that tag can later
        disappear upstream, so an exact match is not guaranteed.
        """
        exact = fmt_version(spec) if spec and len(spec) == 3 else None
        available = self.tags.versions(policy.name)
        if exact and tuple(spec) in available:
            return exact, []
        if spec and len(spec) == 3:
            same_minor = [v for v in available if v[:2] == tuple(spec[:2])]
            newer = [v for v in same_minor if v > tuple(spec)]
            pick = newer[0] if newer else (same_minor[-1] if same_minor else None)
            if pick:
                tag = fmt_version(pick)
                return tag, [f"base image has {exact}, which is not tagged upstream; using nearest release {tag}"]
        tag = self.tags.best(policy.name, spec) if spec and len(spec) < 3 else None
        if tag:
            return tag, []
        raise ResolveError(
            f"{policy.name}: base image has {policy.version or 'an unknown version'}, which has no matching "
            f"tag in {POLICY_REPO}; pin it in `policies` or list it in `exclude_base_policies`"
        )

    def resolve_installed(self, policy: InstalledPolicy) -> ResolvedPolicy:
        spec = parse_version(policy.version) if policy.version else None
        base = dict(origin="base", requested=policy.version or "?")

        if policy.kind == "local":
            raise ResolveError(
                f"{policy.name}: was built into the base image from a local path ({policy.ref}); add it to "
                "`policies` with a `path:` or list it in `exclude_base_policies`"
            )
        ref = policy.ref or ""
        from_policy_repo = POLICY_REPO in ref or policy.kind == "unknown"
        if not from_policy_repo:
            # Third-party module/package: keep it as recorded, pinned to the recorded version when possible.
            if policy.kind == "go" and spec and len(spec) == 3:
                ref = f"{ref.rsplit('@', 1)[0]}@{fmt_version(spec)}"
            return ResolvedPolicy(name=policy.name, version=policy.version, kind=policy.kind, ref=ref, **base)

        tag, notes = self._base_tag(policy, spec)
        if policy.kind == "go":
            kind, pinned = "go", f"{ref.rsplit('@', 1)[0]}@{tag}"
        elif policy.kind == "python":
            kind, pinned = "python", pip_ref(policy.name, tag)
        else:  # definition-only (no manifest): detect the type from the release itself
            kind, pinned = self._pinned(policy.name, tag)
        return ResolvedPolicy(name=policy.name, version=tag, kind=kind, ref=pinned, notes=notes, **base)


    # -- upgrades (brew outdated / upgrade) ------------------------------------------------

    def _hub_latest(self) -> Dict[str, str]:
        if self._latest is None:
            try:
                self._latest = {p.name: p.version for p in self.hub.list_policies()}
            except HubError:
                self._latest = {}
        return self._latest

    def upgrade_targets(self, name: str, current: str) -> Dict[str, Optional[str]]:
        """Newest release tags for a policy at each upgrade level, never lower than `current`.

        patch  - newest vX.Y.* (same major.minor)
        minor  - newest release published on the Policy Hub within the same major
        latest - the Policy Hub's latest release (may cross a major version)
        Policies the hub does not list fall back to the newest gateway-controllers tags.
        """
        cur = parse_version(current) if current else None
        if cur is None or len(cur) != 3:
            return {"patch": None, "minor": None, "latest": None}
        hub_latest = self._hub_latest().get(name)
        if hub_latest:
            latest_spec = parse_version(hub_latest)
            if latest_spec and latest_spec[0] == cur[0]:
                minor_spec: Optional[Tuple[int, ...]] = latest_spec
            else:
                same_major = []
                try:
                    same_major = [parse_version(v.version) for v in self.hub.versions(name)]
                except HubError:
                    pass
                same_major = [v for v in same_major if v and v[0] == cur[0]]
                minor_spec = max(same_major) if same_major else cur[:2]
        else:
            latest_spec, minor_spec = None, (cur[0],)
        targets = {
            "patch": self.tags.best(name, cur[:2]),
            "minor": self.tags.best(name, minor_spec),
            "latest": self.tags.best(name, latest_spec) if latest_spec else self.tags.best(name),
        }
        # Never report anything older than what is installed.
        return {k: (v if v and parse_version(v) > cur else None) for k, v in targets.items()}

    def upgraded(self, policy: ResolvedPolicy, level: str) -> ResolvedPolicy:
        """Copy of a base-image policy moved to the newest release allowed by `level` (none|patch|minor|latest)."""
        if level == "none" or policy.kind == "local" or POLICY_REPO not in policy.ref:
            return policy
        target = self.upgrade_targets(policy.name, policy.version).get(level)
        if not target:
            return policy
        if policy.kind == "go":
            ref = f"{policy.ref.rsplit('@', 1)[0]}@{target}"
        else:
            ref = pip_ref(policy.name, target)
        notes = policy.notes + [f"upgraded {policy.version} -> {target} ({level})"]
        return ResolvedPolicy(name=policy.name, version=target, kind=policy.kind, ref=ref, origin=policy.origin,
                              requested=policy.requested, notes=notes)


def _cmp(a: str, b: str) -> int:
    va, vb = parse_version(a) if a else None, parse_version(b) if b else None
    if va is None or vb is None:
        return 0 if a == b else 2
    return (va > vb) - (va < vb)


def diff_status(base_version: str, new_version: str) -> str:
    c = _cmp(new_version, base_version)
    return {0: "unchanged", 1: "upgraded", -1: "downgraded"}.get(c, "changed")


def merge(
    installed: List[InstalledPolicy],
    base: Dict[str, ResolvedPolicy],
    configured: List[ResolvedPolicy],
    excluded: Iterable[str] = (),
) -> Tuple[List[ResolvedPolicy], List[DiffRow]]:
    """Base-image policies minus exclusions, with configured policies applied on top (config wins on name clash).

    `installed` is what the base image contains; `base` holds the resolved entries for those carried over.
    """
    excluded = set(excluded)
    cfg_by_name = {p.name: p for p in configured}
    final: List[ResolvedPolicy] = []
    rows: List[DiffRow] = []
    for inst in installed:
        if inst.name in cfg_by_name:
            c = cfg_by_name[inst.name]
            rows.append(DiffRow(inst.name, diff_status(inst.version, c.version), inst.version, c.version or c.kind))
        elif inst.name in excluded:
            rows.append(DiffRow(inst.name, "excluded", inst.version, ""))
        elif inst.name in base:
            b = base[inst.name]
            final.append(b)
            status = "kept" if _cmp(b.version, inst.version) == 0 else diff_status(inst.version, b.version)
            rows.append(DiffRow(inst.name, status, inst.version, b.version))
    installed_names = {i.name for i in installed}
    for c in configured:
        final.append(c)
        if c.name not in installed_names:
            rows.append(DiffRow(c.name, "added", "", c.version or c.kind))
    final.sort(key=lambda p: p.name)
    rows.sort(key=lambda r: r.name)
    return final, rows
