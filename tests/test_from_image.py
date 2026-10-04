from pathlib import Path

import pytest

from aigw_builder.builder import base_images_from_labels
from aigw_builder.buildfile import render
from aigw_builder.config import ConfigError, config_from_image

PUBLIC = "ghcr.io/wso2/api-platform"
SUPPORTED = "registry.wso2.com/wso2-api-platform"


def test_base_images_from_labels() -> None:
    controller = {"build.base-image": f"{PUBLIC}/gateway-controller:1.2.1", "build.builder-version": "1.2.1"}
    runtime = {"build.base-image": f"{PUBLIC}/gateway-runtime:1.2.1"}
    assert base_images_from_labels(controller, runtime) == {
        "builder": f"{PUBLIC}/gateway-builder:1.2.1",
        "controller": f"{PUBLIC}/gateway-controller:1.2.1",
        "runtime": f"{PUBLIC}/gateway-runtime:1.2.1",
    }


def test_base_images_without_runtime_labels_follow_controller() -> None:
    controller = {"build.base-image": f"{SUPPORTED}/gateway-controller:1.1.0-2"}
    assert base_images_from_labels(controller, {})["runtime"] == f"{SUPPORTED}/gateway-runtime:1.1.0-2"
    assert base_images_from_labels(controller, {})["builder"] == f"{SUPPORTED}/gateway-builder:1.1.0-2"


def test_stock_image_has_no_base_labels() -> None:
    assert base_images_from_labels({"org.opencontainers.image.version": "1.2.1"}, {}) == {}


def test_config_from_public_build(tmp_path: Path) -> None:
    bases = {c: f"{PUBLIC}/gateway-{c}:1.2.1" for c in ("builder", "controller", "runtime")}
    cfg = config_from_image("localhost/upgraded-ai-gateway-gateway-controller:1.2.1", bases, tmp_path)
    assert (cfg.gateway_name, cfg.output.repository, cfg.output_tag) == ("upgraded-ai-gateway", "localhost", "1.2.1")
    assert cfg.image_source == "public" and cfg.image_overrides == {}
    assert cfg.images() == bases
    assert cfg.policies == [] and cfg.base_policies == "keep" and cfg.exclude_base_policies == []
    assert cfg.build_files_dir == tmp_path / "aigw-build" / "upgraded-ai-gateway"
    assert "# Config: derived from localhost/upgraded-ai-gateway-gateway-controller:1.2.1" in render(cfg, [])


def test_config_from_supported_build_keeps_per_component_tags(tmp_path: Path) -> None:
    bases = {"builder": f"{SUPPORTED}/gateway-builder:1.1.0", "controller": f"{SUPPORTED}/gateway-controller:1.1.0-2",
             "runtime": f"{SUPPORTED}/gateway-runtime:1.1.0-3"}
    cfg = config_from_image("my.registry/team/ai-gw-gateway-controller:1.1.0.4", bases, tmp_path)
    assert cfg.image_source == "supported" and cfg.gateway_version == "1.1.0-2"
    assert cfg.output.repository == "my.registry/team"
    assert cfg.images() == bases


def test_config_from_custom_registry(tmp_path: Path) -> None:
    bases = {c: f"mirror.example.com/wso2/gateway-{c}:1.2.1" for c in ("builder", "controller", "runtime")}
    cfg = config_from_image("localhost/gw-gateway-controller:1", bases, tmp_path)
    assert cfg.registry_prefix == "mirror.example.com/wso2" and cfg.images() == bases


def test_config_from_image_rejects_non_ap_names(tmp_path: Path) -> None:
    bases = {"controller": f"{PUBLIC}/gateway-controller:1.2.1"}
    with pytest.raises(ConfigError, match="not a controller image built by ap"):
        config_from_image(f"{PUBLIC}/gateway-controller:1.2.1", bases, tmp_path)
    with pytest.raises(ConfigError, match="not a controller image built by ap"):
        config_from_image("localhost/gw-gateway-runtime:1.2.1", bases, tmp_path)
