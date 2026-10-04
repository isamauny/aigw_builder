"""Loading and validation of the aigw.yaml build configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

IMAGE_SOURCES = ("public", "supported")
COMPONENTS = ("builder", "controller", "runtime")
POLICY_KINDS = ("go", "python")
UPGRADE_LEVELS = ("none", "patch", "minor", "latest")
LOCAL_REPOSITORY = "localhost"  # default: images stay in the local image store
# Repositories that mean "local image store only": built images are never pushed from them.
# Docker expands a bare "local" to docker.io/local.
LOCAL_REPOSITORIES = ("localhost", "local", "docker.io/local")


def is_local_repository(repository: str) -> bool:
    return repository.strip().rstrip("/").lower() in LOCAL_REPOSITORIES
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")  # Docker tag syntax

DEFAULT_REGISTRIES = {
    "public": "ghcr.io/wso2/api-platform",
    "supported": "registry.wso2.com/wso2-api-platform",  # needs `docker login registry.wso2.com`
}


class ConfigError(Exception):
    def __init__(self, source: Path, errors: List[str]) -> None:
        self.source = source
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors))


@dataclass
class PolicyRef:
    name: str
    version: Optional[str] = None  # None -> latest on the hub
    path: Optional[Path] = None  # local policy directory (absolute)
    kind: Optional[str] = None  # go | python, overrides auto-detection

    @property
    def is_local(self) -> bool:
        return self.path is not None


@dataclass
class OutputConfig:
    repository: str = LOCAL_REPOSITORY
    push: bool = False
    platform: Optional[str] = None
    no_cache: bool = False
    output_dir: Optional[Path] = None
    tag: Optional[str] = None  # tag of the built images; None -> gateway.version
    build_files_dir: Optional[Path] = None  # where build.yaml + build-manifest.yaml are kept
    buildx_builder: Optional[str] = None  # None/auto: pick a builder that loads images into the local store


@dataclass
class Config:
    source: Path
    gateway_version: str
    name: Optional[str]
    image_source: str
    registries: Dict[str, str]
    image_overrides: Dict[str, str]
    base_policies: str  # keep | none
    upgrade_base_policies: str  # none | patch | minor | latest
    exclude_base_policies: List[str]
    output: OutputConfig
    policies: List[PolicyRef] = field(default_factory=list)
    from_image: Optional[str] = None  # set when the config was derived from an earlier build, not read from a file

    @property
    def base_dir(self) -> Path:
        return self.source.parent

    @property
    def gateway_name(self) -> str:
        return self.name or self.base_dir.name

    @property
    def build_files_dir(self) -> Path:
        """Where the generated build.yaml and ap's build-manifest.yaml are kept for review."""
        return self.output.build_files_dir or self.base_dir / "aigw-build" / self.gateway_name

    def output_images(self) -> List[str]:
        """Images produced by `ap`: controller first, then runtime."""
        repo, name, tag = self.output.repository, self.gateway_name, self.output_tag
        return [f"{repo}/{name}-gateway-controller:{tag}", f"{repo}/{name}-gateway-runtime:{tag}"]

    @property
    def last_build_image(self) -> str:
        """Controller image of the previous build of this config (what `--from last` refers to)."""
        return self.output_images()[0]

    @property
    def output_tag(self) -> str:
        return self.output.tag or self.gateway_version

    @property
    def registry_prefix(self) -> str:
        return self.registries[self.image_source]

    def image(self, component: str) -> str:
        """Base image for a component. An override is either a full reference or just a tag."""
        override = self.image_overrides.get(component)
        if override and "/" in override:
            return override
        return f"{self.registry_prefix}/gateway-{component}:{override or self.gateway_version}"

    def images(self) -> Dict[str, str]:
        return {c: self.image(c) for c in COMPONENTS}


def _as_bool(value: Any, where: str, errors: List[str]) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    errors.append(f"{where}: expected true/false, got {value!r}")
    return False


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _resolve_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def parse_config(raw: Any, source: Path) -> Config:
    errors: List[str] = []
    base = source.parent
    if not isinstance(raw, dict):
        raise ConfigError(source, ["top level must be a mapping"])

    gateway = raw.get("gateway") or {}
    if not isinstance(gateway, dict):
        errors.append("gateway: must be a mapping")
        gateway = {}
    version = _opt_str(gateway.get("version"))
    if not version:
        errors.append("gateway.version: required (e.g. \"1.1.0\")")

    image_source = str(raw.get("image_source") or "public").strip()
    if image_source not in IMAGE_SOURCES:
        errors.append(f"image_source: must be one of {', '.join(IMAGE_SOURCES)}, got {image_source!r}")
        image_source = "public"

    registries = dict(DEFAULT_REGISTRIES)
    raw_regs = raw.get("registries") or {}
    if not isinstance(raw_regs, dict):
        errors.append("registries: must be a mapping")
        raw_regs = {}
    for key, value in raw_regs.items():
        if key not in IMAGE_SOURCES:
            errors.append(f"registries.{key}: unknown image source (expected {', '.join(IMAGE_SOURCES)})")
            continue
        prefix = value.get("prefix") if isinstance(value, dict) else value
        prefix = _opt_str(prefix)
        if not prefix:
            errors.append(f"registries.{key}.prefix: required")
            continue
        registries[key] = prefix.rstrip("/")

    overrides: Dict[str, str] = {}
    raw_images = raw.get("images") or {}
    if not isinstance(raw_images, dict):
        errors.append("images: must be a mapping")
        raw_images = {}
    for key, value in raw_images.items():
        if key not in COMPONENTS:
            errors.append(f"images.{key}: unknown component (expected {', '.join(COMPONENTS)})")
        elif _opt_str(value):
            ref = str(value).strip()
            # Without a "/" the value is a tag for <registry prefix>/gateway-<component>.
            if "/" not in ref and not TAG_RE.match(ref):
                errors.append(f"images.{key}: {ref!r} is neither a tag nor a full image reference")
            overrides[key] = ref

    base_policies = str(raw.get("base_policies") or "keep").strip()
    if base_policies not in ("keep", "none"):
        errors.append(f"base_policies: must be 'keep' or 'none', got {base_policies!r}")
    upgrade = str(raw.get("upgrade_base_policies") or "none").strip()
    if upgrade not in UPGRADE_LEVELS:
        errors.append(f"upgrade_base_policies: must be one of {', '.join(UPGRADE_LEVELS)}, got {upgrade!r}")
        upgrade = "none"
    exclude = raw.get("exclude_base_policies") or []
    if not isinstance(exclude, list):
        errors.append("exclude_base_policies: must be a list of policy names")
        exclude = []

    raw_out = raw.get("output") or {}
    if not isinstance(raw_out, dict):
        errors.append("output: must be a mapping")
        raw_out = {}
    out_dir = _opt_str(raw_out.get("output_dir"))
    files_dir = _opt_str(raw_out.get("build_files_dir"))
    output = OutputConfig(
        repository=(_opt_str(raw_out.get("repository")) or LOCAL_REPOSITORY).rstrip("/"),
        push=_as_bool(raw_out.get("push"), "output.push", errors),
        platform=_opt_str(raw_out.get("platform")),
        no_cache=_as_bool(raw_out.get("no_cache"), "output.no_cache", errors),
        output_dir=_resolve_path(base, out_dir) if out_dir else None,
        tag=_opt_str(raw_out.get("tag")),
        build_files_dir=_resolve_path(base, files_dir) if files_dir else None,
        buildx_builder=None if _opt_str(raw_out.get("buildx_builder")) in (None, "auto")
        else _opt_str(raw_out.get("buildx_builder")),
    )

    if output.tag and not TAG_RE.match(output.tag):
        errors.append(f"output.tag: {output.tag!r} is not a valid image tag (letters, digits, _ . -; max 128)")
    if output.push and is_local_repository(output.repository):
        errors.append(
            f"output.push: {output.repository!r} means local images only; set output.repository to a registry "
            "(e.g. myregistry.example.com/team) to push"
        )

    policies: List[PolicyRef] = []
    raw_policies = raw.get("policies") or []
    if not isinstance(raw_policies, list):
        errors.append("policies: must be a list")
        raw_policies = []
    seen: Dict[str, int] = {}
    for i, entry in enumerate(raw_policies):
        where = f"policies[{i}]"
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a policy name or a mapping")
            continue
        name = _opt_str(entry.get("name"))
        if not name:
            errors.append(f"{where}.name: required")
            continue
        where = f"policies[{i}] ({name})"
        unknown = set(entry) - {"name", "version", "path", "kind"}
        if unknown:
            errors.append(f"{where}: unknown keys {', '.join(sorted(unknown))}")
        if name in seen:
            errors.append(f"{where}: duplicate of policies[{seen[name]}]")
        seen[name] = i
        kind = _opt_str(entry.get("kind"))
        if kind and kind not in POLICY_KINDS:
            errors.append(f"{where}.kind: must be one of {', '.join(POLICY_KINDS)}")
        path_val = _opt_str(entry.get("path"))
        path = None
        if path_val:
            path = _resolve_path(base, path_val)
            if not path.is_dir():
                errors.append(f"{where}.path: directory not found: {path}")
            elif not (path / "policy-definition.yaml").is_file():
                errors.append(f"{where}.path: no policy-definition.yaml in {path}")
        policies.append(PolicyRef(name=name, version=_opt_str(entry.get("version")), path=path, kind=kind))

    if errors:
        raise ConfigError(source, errors)

    return Config(
        source=source,
        gateway_version=str(version),
        name=_opt_str(gateway.get("name")),
        image_source=image_source,
        registries=registries,
        image_overrides=overrides,
        base_policies=base_policies,
        upgrade_base_policies=upgrade,
        exclude_base_policies=[str(x) for x in exclude],
        output=output,
        policies=policies,
    )


# <repository>/<name>-gateway-controller:<tag>, as produced by `ap gateway image build`
BUILT_IMAGE_RE = re.compile(r"^(?P<repo>.+)/(?P<name>[^/:@]+)-gateway-controller:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)$")
BASE_IMAGE_RE = re.compile(r"^(?P<prefix>.+)/gateway-(?P<component>[a-z]+):(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)$")


def config_from_image(image: str, base_images: Dict[str, str], base_dir: Path) -> Config:
    """Config that rebuilds an earlier `ap` build as is: same name, repository and base images, no extra policies.

    `base_images` maps each component to the full base image reference the earlier build used. The carried-over
    policies are read from `image` itself (see builder.plan_build's from_image).
    """
    source = base_dir / "aigw.yaml"  # nominal; nothing is read from it
    m = BUILT_IMAGE_RE.match(image)
    if not m:
        raise ConfigError(source, [f"{image}: not a controller image built by ap "
                                   "(<repository>/<name>-gateway-controller:<tag>); pass -f <config>"])
    base = BASE_IMAGE_RE.match(base_images.get("controller", ""))
    if not base:
        raise ConfigError(source, [f"{image}: cannot tell its base controller image "
                                   f"({base_images.get('controller') or 'unknown'}); pass -f <config>"])
    prefix, version = base["prefix"], base["tag"]
    image_source = next((s for s, p in DEFAULT_REGISTRIES.items() if p == prefix), "public")
    registries = dict(DEFAULT_REGISTRIES, **{image_source: prefix})
    # Only components whose base differs from <prefix>/gateway-<component>:<version> need an override.
    overrides = {c: ref for c, ref in base_images.items()
                 if c in COMPONENTS and ref != f"{prefix}/gateway-{c}:{version}"}
    return Config(
        source=source,
        gateway_version=version,
        name=m["name"],
        image_source=image_source,
        registries=registries,
        image_overrides=overrides,
        base_policies="keep",
        upgrade_base_policies="none",
        exclude_base_policies=[],
        output=OutputConfig(repository=m["repo"], tag=m["tag"]),
        from_image=image,
    )


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigError(path, ["file not found"])
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"invalid YAML: {exc}"]) from exc
    return parse_config(raw, path)
