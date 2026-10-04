"""Plan and run a gateway image build via `ap gateway image build`."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import docker_utils as dk
from .buildfile import render
from .config import COMPONENTS, Config
from .image_inspect import ImageInventory, inspect_image
from .resolver import DiffRow, ResolvedPolicy, ResolveError, Resolver, merge


class BuildError(Exception):
    pass


@dataclass
class BuildPlan:
    cfg: Config
    policies: List[ResolvedPolicy]
    diff: List[DiffRow] = field(default_factory=list)
    base_inventory: Optional[ImageInventory] = None
    errors: List[str] = field(default_factory=list)
    baseline_image: Optional[str] = None  # image the carried-over policies were read from

    @property
    def ok(self) -> bool:
        return not self.errors

    def build_yaml(self) -> str:
        return render(self.cfg, self.policies)

    def output_images(self) -> List[str]:
        return self.cfg.output_images()


LAST_BUILD = "last"


def resolve_baseline(cfg: Config, from_image: Optional[str]) -> Tuple[str, bool]:
    """Image whose policies are carried over, and whether it may be pulled.

    None   -> the configured base controller image (default)
    "last" -> the controller image of the previous build of this config; must exist locally
    other  -> that image (e.g. an earlier build, local or in a registry)
    """
    if not from_image:
        return cfg.image("controller"), True
    if from_image == LAST_BUILD:
        image = cfg.last_build_image
        if not dk.image_exists(image):
            raise BuildError(
                f"no previous build of this config found locally ({image}). Build it first, or pass "
                "--from <controller image> to start from another image."
            )
        return image, False
    return from_image, True


BASE_IMAGE_LABEL = "build.base-image"
BUILDER_VERSION_LABEL = "build.builder-version"


def base_images_from_labels(controller: Dict[str, str], runtime: Dict[str, str]) -> Dict[str, str]:
    """Base images of an `ap` build, from the labels ap puts on the built controller and runtime images.

    The builder image is not recorded as such; it shares the controller's prefix and is tagged with the builder
    version. Without runtime labels (runtime image not available locally) the runtime base follows the controller.
    """
    base = controller.get(BASE_IMAGE_LABEL, "")
    if "/gateway-controller:" not in base:
        return {}
    prefix, tag = base.rsplit("/gateway-controller:", 1)
    return {
        "builder": f"{prefix}/gateway-builder:{controller.get(BUILDER_VERSION_LABEL) or tag}",
        "controller": base,
        "runtime": runtime.get(BASE_IMAGE_LABEL) or f"{prefix}/gateway-runtime:{tag}",
    }


def base_images_of(image: str, pull: bool = True) -> Dict[str, str]:
    """Base images an earlier `ap` build of this controller image (and its sibling runtime image) was made from."""
    dk.check_docker()
    dk.ensure_image(image, pull_if_missing=pull)
    runtime = image.replace("-gateway-controller:", "-gateway-runtime:")
    runtime_labels = dk.image_labels(runtime) if runtime != image and dk.image_exists(runtime) else {}
    images = base_images_from_labels(dk.image_labels(image), runtime_labels)
    if not images:
        raise BuildError(f"{image} has no {BASE_IMAGE_LABEL} label, so it was not built by `ap` (a stock image?); "
                         "pass -f <config> to say which base images to build on")
    return images


def resolve_base(resolver: Resolver, inventory: ImageInventory, cfg: Config) -> Tuple[Dict[str, ResolvedPolicy], List[str]]:
    """Resolve the base-image policies that will be carried over (overridden/excluded ones are skipped)."""
    skip = {p.name for p in cfg.policies} | set(cfg.exclude_base_policies)
    resolved: Dict[str, ResolvedPolicy] = {}
    errors: List[str] = []
    for installed in inventory.policies:
        if installed.name in skip:
            continue
        try:
            resolved[installed.name] = resolver.resolve_installed(installed)
        except ResolveError as exc:
            errors.append(str(exc))
    return resolved, errors


def plan_build(
    cfg: Config,
    resolver: Resolver,
    include_base: Optional[bool] = None,
    pull: bool = True,
    upgrade: Optional[str] = None,
    from_image: Optional[str] = None,
) -> BuildPlan:
    """Resolve the final policy set.

    `upgrade` overrides cfg.upgrade_base_policies (none|patch|minor|latest). `from_image` takes the carried-over
    policies from another controller image (e.g. a previous build, or "last") instead of the base image; the
    build itself still uses the configured base images.
    """
    configured, errors = resolver.resolve_all(cfg.policies)
    keep_base = cfg.base_policies == "keep" if include_base is None else include_base
    if from_image:
        keep_base = True
    if not keep_base:
        return BuildPlan(cfg=cfg, policies=sorted(configured, key=lambda p: p.name), errors=errors)

    image, may_pull = resolve_baseline(cfg, from_image)
    inventory = inspect_image(image, pull=pull and may_pull, platform=cfg.output.platform)
    base, base_errors = resolve_base(resolver, inventory, cfg)
    level = upgrade or cfg.upgrade_base_policies
    if level != "none":
        base = {name: resolver.upgraded(p, level) for name, p in base.items()}
    final, diff = merge(inventory.policies, base, configured, cfg.exclude_base_policies)
    unknown_excludes = set(cfg.exclude_base_policies) - {p.name for p in inventory.policies}
    errors += base_errors
    if not from_image:
        # An earlier build may already lack an excluded policy, so only check against the stock base image.
        errors += [f"exclude_base_policies: {n} is not in the base image" for n in sorted(unknown_excludes)]
    return BuildPlan(cfg=cfg, policies=final, diff=diff, base_inventory=inventory, errors=errors,
                     baseline_image=image)


def ap_command(cfg: Config, workdir: Path) -> List[str]:
    out = cfg.output
    cmd = ["ap", "gateway", "image", "build", "--path", str(workdir), "--name", cfg.gateway_name,
           "--repository", out.repository]
    if out.push:
        cmd.append("--push")
    if out.no_cache:
        cmd.append("--no-cache")
    if out.platform:
        cmd += ["--platform", out.platform]
    if out.output_dir:
        cmd += ["--output-dir", str(out.output_dir)]
    return cmd


def check_platform(cfg: Config) -> None:
    """Reject `output.platform` settings that `ap gateway image build` cannot honour.

    With --platform, ap (v0.9) always builds with `buildx --push`, and its gateway-builder step compiles the
    policy-engine binary for the Docker host's architecture only. A non-native or multi-platform target would
    produce runtime images containing a binary for the wrong CPU.
    """
    platform = cfg.output.platform
    if not platform:
        return
    targets = [t.strip() for t in platform.split(",") if t.strip()]
    host = dk.host_arch()
    foreign = [t for t in targets if t.split("/")[1:2] != [host]]
    if len(targets) > 1 or foreign:
        raise BuildError(
            f"output.platform {platform!r} is not supported: `ap` compiles the policy engine for the Docker "
            f"host's architecture only (linux/{host}), so other platforms would get a binary for the wrong CPU. "
            f"Remove output.platform, or build on a linux/{targets[0].split('/')[-1]} host."
        )
    if not cfg.output.push:
        raise BuildError(
            "output.platform makes `ap` build with `buildx --push`, so it needs push: true (or --push) and a "
            "registry in output.repository. Remove output.platform to build a local image."
        )


def preflight(cfg: Config, log=print) -> None:
    if not shutil.which("ap"):
        raise BuildError("`ap` CLI not found on PATH (see https://github.com/wso2/api-platform/releases)")
    dk.check_docker()
    for component in COMPONENTS:
        image = cfg.image(component)
        if dk.ensure_image(image, pull_if_missing=True, platform=cfg.output.platform):
            log(f"pulled {image}")


MANIFEST_FILE = "build-manifest.yaml"


def write_build_file(plan: BuildPlan) -> Path:
    """Write build.yaml into the build-files directory and drop any manifest from an earlier build.

    `ap` builds from this directory and writes build-manifest.yaml (exact policies baked into the image) next
    to build.yaml, so both stay on disk for review. A stale manifest is removed first so the two never disagree.
    """
    directory = plan.cfg.build_files_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_FILE).unlink(missing_ok=True)
    path = directory / "build.yaml"
    path.write_text(plan.build_yaml())
    return path


def run_build(plan: BuildPlan, log=print) -> int:
    cfg = plan.cfg
    build_file = write_build_file(plan)
    if cfg.output.output_dir:
        cfg.output.output_dir.mkdir(parents=True, exist_ok=True)
    cmd = ap_command(cfg, build_file.parent)
    env = dict(os.environ)
    builder = cfg.output.buildx_builder or dk.loading_builder(log=log)
    if builder:
        env["BUILDX_BUILDER"] = builder
    log(("BUILDX_BUILDER=" + builder + " " if builder else "") + "$ " + " ".join(cmd))
    return subprocess.run(cmd, env=env).returncode
