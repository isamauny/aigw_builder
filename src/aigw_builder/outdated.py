"""`brew outdated`-style comparison of a policy set against the newest published releases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .image_inspect import ImageInventory
from .resolver import POLICY_REPO, ResolvedPolicy, Resolver, parse_version


@dataclass
class OutdatedRow:
    name: str
    current: str
    patch: Optional[str] = None
    minor: Optional[str] = None
    latest: Optional[str] = None
    origin: str = "base"
    note: str = ""

    @property
    def status(self) -> str:
        newest = self.latest or self.minor or self.patch
        cur = parse_version(self.current) if self.current else None
        if cur is None:
            return "unknown" if self.note else "up to date"
        if not newest:
            return "up to date"
        new = parse_version(newest) or cur
        if new[0] > cur[0]:
            return "major"
        return "minor" if new[1] > cur[1] else "patch"

    @property
    def outdated(self) -> bool:
        return self.status in ("patch", "minor", "major")

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["status"] = self.status
        return d


def rows_for_policies(resolver: Resolver, policies: List[ResolvedPolicy]) -> List[OutdatedRow]:
    rows: List[OutdatedRow] = []
    for p in sorted(policies, key=lambda p: p.name):
        if p.kind == "local" or POLICY_REPO not in p.ref:
            rows.append(OutdatedRow(p.name, p.version, origin=p.origin, note="not a Policy Hub policy; skipped"))
            continue
        targets = resolver.upgrade_targets(p.name, p.version)
        rows.append(OutdatedRow(p.name, p.version, origin=p.origin, note="; ".join(p.notes), **targets))
    return rows


def rows_for_image(resolver: Resolver, inventory: ImageInventory) -> List[OutdatedRow]:
    """Compare the versions recorded in the image (as installed, even if that release is no longer tagged)."""
    rows: List[OutdatedRow] = []
    for p in inventory.policies:
        if p.kind == "local" or (p.ref and POLICY_REPO not in p.ref):
            rows.append(OutdatedRow(p.name, p.version, note="not a Policy Hub policy; skipped"))
            continue
        spec = parse_version(p.version) if p.version else None
        if not spec or len(spec) != 3:
            rows.append(OutdatedRow(p.name, p.version, note="no exact version recorded in the image"))
            continue
        rows.append(OutdatedRow(p.name, p.version, **resolver.upgrade_targets(p.name, p.version)))
    return sorted(rows, key=lambda r: r.name)
