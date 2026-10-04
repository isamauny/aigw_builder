"""Thin wrappers around the docker CLI. Registry auth is left to the user's existing `docker login`."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Dict, List, Optional

AUTH_ERROR_MARKERS = ("unauthorized", "denied", "authentication required", "no basic auth credentials")


class DockerError(Exception):
    pass


class DockerAuthError(DockerError):
    pass


def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise DockerError(f"{cmd[0]} not found on PATH") from exc
    if check and proc.returncode != 0:
        raise DockerError(f"`{' '.join(cmd)}` failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def registry_host(image: str) -> str:
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def check_docker() -> None:
    if not shutil.which("docker"):
        raise DockerError("docker not found on PATH")
    proc = run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
    if proc.returncode != 0:
        raise DockerError("Docker daemon is not reachable (is Docker / Rancher Desktop running?)")


_ARCH_ALIASES = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "amd64": "amd64"}


def host_arch() -> str:
    """Architecture of the Docker host (the VM on macOS), as a Go/OCI arch name (amd64, arm64, ...)."""
    proc = run(["docker", "info", "--format", "{{.Architecture}}"], check=False)
    raw = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else __import__("platform").machine()
    return _ARCH_ALIASES.get(raw.lower(), raw.lower())


def image_exists(image: str) -> bool:
    return run(["docker", "image", "inspect", image], check=False).returncode == 0


def pull(image: str, platform: Optional[str] = None) -> None:
    cmd = ["docker", "pull", "--quiet"]
    if platform:
        cmd += ["--platform", platform]
    proc = run(cmd + [image], check=False)
    if proc.returncode == 0:
        return
    msg = (proc.stderr or proc.stdout).strip()
    lower = msg.lower()
    if "not found" in lower or "manifest unknown" in lower:
        # Harbor-style registries answer "unauthorized: project X not found" for a wrong path even when logged in.
        raise DockerError(
            f"Image not found: {image}. Check the registry prefix / image path in your config "
            f"(and that you are logged in with `docker login {registry_host(image)}`).\n  docker: {msg}"
        )
    if any(m in lower for m in AUTH_ERROR_MARKERS) or "403" in lower:
        raise DockerAuthError(
            f"Not authorized to pull {image}. Run `docker login {registry_host(image)}` if you have not, and "
            f"check that this tag exists for your subscription (some registries answer 'unauthorized' for "
            f"unknown tags).\n  docker: {msg}"
        )
    raise DockerError(f"Failed to pull {image}: {msg}")


def ensure_image(image: str, pull_if_missing: bool = True, platform: Optional[str] = None) -> bool:
    """Make sure an image is available locally. Returns True if it had to be pulled."""
    if image_exists(image):
        return False
    if not pull_if_missing:
        raise DockerError(f"Image {image} is not available locally (use --pull to fetch it)")
    pull(image, platform)
    return True


def image_labels(image: str) -> Dict[str, str]:
    proc = run(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"], check=False)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout.strip() or "null") or {}
    except json.JSONDecodeError:
        return {}


# --- buildx --------------------------------------------------------------------------------------------
# `ap` runs `docker buildx build` without `--load`. With a docker-container builder (the only kind available
# on Podman, and common with Docker Desktop/Rancher setups) the built images then stay in the build cache and
# never reach the local image store. A builder created with `default-load=true` loads results automatically.

LOADING_BUILDER = "aigw-builder"


def buildx_builder_info(name: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Driver and driver options of a buildx builder (current one if name is None); None if it doesn't exist."""
    cmd = ["docker", "buildx", "inspect"] + ([name] if name else [])
    proc = run(cmd, check=False)
    if proc.returncode != 0:
        return None
    info = {"driver": "", "options": ""}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "driver" and not info["driver"]:
            info["driver"] = value.strip()
        elif key == "driver options":
            info["options"] += value.strip() + " "
        elif key == "name" and "name" not in info:
            info["name"] = value.strip()
    return info


def _loads_images(info: Dict[str, str]) -> bool:
    return info.get("driver") == "docker" or 'default-load="true"' in info.get("options", "") \
        or "default-load=true" in info.get("options", "")


def loading_builder(log=print) -> Optional[str]:
    """Name of a buildx builder that loads built images into the local store, or None if the current one does.

    Creates a dedicated `aigw-builder` builder (docker-container, default-load=true) when needed. The user's
    selected builder is left untouched; the name is passed to `ap` through BUILDX_BUILDER.
    """
    current = buildx_builder_info()
    if current is None or _loads_images(current):
        return None
    ours = buildx_builder_info(LOADING_BUILDER)
    if ours is not None and _loads_images(ours):
        return LOADING_BUILDER
    if ours is not None:
        run(["docker", "buildx", "rm", LOADING_BUILDER], check=False)
    log(f"current buildx builder ({current.get('name', '?')}, {current.get('driver')}) does not load images; "
        f"creating buildx builder '{LOADING_BUILDER}' with default-load=true")
    run(["docker", "buildx", "create", "--name", LOADING_BUILDER, "--driver", "docker-container",
         "--driver-opt", "default-load=true"])
    return LOADING_BUILDER
