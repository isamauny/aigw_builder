"""Read the policies baked into a gateway controller image.

The controller image records its policy set on disk:
  - /app/build-manifest.yaml   (custom builds and recent stock images; exact versions + source refs)
  - /app/policies/             (custom builds; policy definitions)
  - /app/default-policies/     (stock images; policy definitions)
The runtime image compiles policies into the policy-engine binary, so it cannot be inspected this way.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import docker_utils as dk

MANIFEST_PATH = "/app/build-manifest.yaml"
POLICY_DIRS = ("/app/policies", "/app/default-policies")


class InspectError(Exception):
    pass


@dataclass
class InstalledPolicy:
    name: str
    version: str
    kind: str  # go | python | local | unknown
    ref: Optional[str] = None  # gomodule / pipPackage / filePath as recorded in the manifest

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version, "kind": self.kind, "ref": self.ref}


@dataclass
class ImageInventory:
    image: str
    source: str  # path inside the image the list was read from
    policies: List[InstalledPolicy] = field(default_factory=list)

    def by_name(self) -> Dict[str, InstalledPolicy]:
        return {p.name: p for p in self.policies}


def parse_manifest(text: str) -> List[InstalledPolicy]:
    doc = yaml.safe_load(text) or {}
    out: List[InstalledPolicy] = []
    for entry in doc.get("policies") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        if entry.get("gomodule"):
            kind, ref = "go", entry["gomodule"]
        elif entry.get("pipPackage"):
            kind, ref = "python", entry["pipPackage"]
        elif entry.get("filePath"):
            kind, ref = "local", entry["filePath"]
        else:
            kind, ref = "unknown", None
        version = str(entry.get("version") or "")
        if not version and ref and "@" in ref and kind == "go":
            version = ref.rsplit("@", 1)[1]
        out.append(InstalledPolicy(name=str(entry["name"]), version=version, kind=kind, ref=ref))
    return out


def parse_definition_dir(directory: Path) -> List[InstalledPolicy]:
    """Policy definitions laid out either as <dir>/*.yaml or <dir>/<policy>/policy-definition.yaml."""
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    files += sorted(directory.glob("*/policy-definition.yaml"))
    found: Dict[str, InstalledPolicy] = {}
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict) or not doc.get("name"):
            continue
        name = str(doc["name"])
        found[name] = InstalledPolicy(name=name, version=str(doc.get("version") or ""), kind="unknown")
    return sorted(found.values(), key=lambda p: p.name)


def _docker_cp(container: str, src: str, dest: Path) -> bool:
    return dk.run(["docker", "cp", f"{container}:{src}", str(dest)], check=False).returncode == 0


def _looks_like_runtime(image: str) -> bool:
    title = dk.image_labels(image).get("org.opencontainers.image.title", "")
    return "runtime" in title.lower() or "gateway-runtime" in image


def inspect_image(image: str, pull: bool = True, platform: Optional[str] = None) -> ImageInventory:
    dk.check_docker()
    dk.ensure_image(image, pull_if_missing=pull, platform=platform)

    if _looks_like_runtime(image):
        hint = image.replace("gateway-runtime", "gateway-controller")
        raise InspectError(
            f"{image} looks like a gateway runtime image; its policies are compiled into the policy-engine "
            f"binary and cannot be listed. Inspect the matching controller image instead (e.g. {hint})."
        )

    # `docker create` never starts the container, so this works for distroless images too.
    name = f"aigw-inspect-{uuid.uuid4().hex[:10]}"
    dk.run(["docker", "create", "--name", name, "--entrypoint", "/aigw-noop", image])
    try:
        with tempfile.TemporaryDirectory(prefix="aigw-inspect-") as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "build-manifest.yaml"
            if _docker_cp(name, MANIFEST_PATH, manifest) and manifest.is_file():
                policies = parse_manifest(manifest.read_text())
                if policies:
                    return ImageInventory(image=image, source=MANIFEST_PATH, policies=policies)
            for src in POLICY_DIRS:
                dest = tmp_path / Path(src).name
                if _docker_cp(name, src, dest) and dest.is_dir():
                    policies = parse_definition_dir(dest)
                    if policies:
                        return ImageInventory(image=image, source=src, policies=policies)
    finally:
        dk.run(["docker", "rm", "-f", name], check=False)

    raise InspectError(
        f"No policy information found in {image} (looked for {MANIFEST_PATH} and {', '.join(POLICY_DIRS)}). "
        "Is this a gateway controller image?"
    )
