from pathlib import Path

import pytest
from typer.testing import CliRunner

from aigw_builder import cli
from aigw_builder.builder import base_prefix_from
from aigw_builder.config import (COMPONENTS, DEFAULT_REGISTRIES, image_repository, parse_config, retag_base_images,
                                 source_for_prefix)

PUBLIC = "ghcr.io/wso2/api-platform"
SUPPORTED = "registry.wso2.com/wso2-api-platform"


def _supported(tmp_path: Path, **output):
    return parse_config({
        "gateway": {"version": "2026.10.0-up", "name": "demo"}, "image_source": "supported",
        "images": {"builder": "2026.10.0", "controller": "2026.10.0",
                   "runtime": "mirror.example.com:5000/wso2/gateway-runtime:2026.10.0@sha256:abc"},
        "output": {"repository": "local", **output},
    }, tmp_path / "aigw.yaml")


@pytest.mark.parametrize("ref, repo", [
    ("ghcr.io/wso2/api-platform/gateway-runtime:1.2.1", "ghcr.io/wso2/api-platform/gateway-runtime"),
    ("host:5000/a/b:1.0@sha256:abc", "host:5000/a/b"),
    ("host:5000/a/b", "host:5000/a/b"),
    ("busybox:1", "busybox"),
])
def test_image_repository(ref: str, repo: str) -> None:
    assert image_repository(ref) == repo


def test_retag_moves_all_base_images_and_output_tag(tmp_path: Path) -> None:
    cfg = _supported(tmp_path)
    changes = retag_base_images(cfg, "2026.11.0")
    assert cfg.images() == {
        "builder": f"{SUPPORTED}/gateway-builder:2026.11.0",
        "controller": f"{SUPPORTED}/gateway-controller:2026.11.0",
        "runtime": "mirror.example.com:5000/wso2/gateway-runtime:2026.11.0",  # full reference keeps its repository
    }
    assert changes["controller"] == (f"{SUPPORTED}/gateway-controller:2026.10.0",
                                     f"{SUPPORTED}/gateway-controller:2026.11.0")
    assert cfg.output_tag == "2026.11.0"
    # --from last still means the build made before the upgrade
    assert cfg.last_build_image == "local/demo-gateway-controller:2026.10.0-up"


def test_retag_keeps_explicit_output_tag(tmp_path: Path) -> None:
    cfg = _supported(tmp_path, tag="acme.7")
    retag_base_images(cfg, "2026.11.0")
    assert cfg.output_tag == "acme.7"


def test_retag_to_current_tag_changes_nothing(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "2026.10.0-up"}, "images": {c: "2026.10.0" for c in
                        ("builder", "controller", "runtime")}}, tmp_path / "aigw.yaml")
    assert retag_base_images(cfg, "2026.10.0") == {}
    assert cfg.output_tag == "2026.10.0-up" and cfg.previous_build is None


def test_retag_rejects_invalid_tag(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a valid image tag"):
        retag_base_images(_supported(tmp_path), "bad tag")


def _public(tmp_path: Path, **extra):
    return parse_config({"gateway": {"version": "1.2.1", "name": "demo"}, **extra}, tmp_path / "aigw.yaml")


def test_source_switches_registry_and_drops_overrides(tmp_path: Path) -> None:
    cfg = _supported(tmp_path)
    changes = retag_base_images(cfg, "2026.09.24", source="public")
    assert cfg.image_source == "public"
    assert cfg.images() == {c: f"{PUBLIC}/gateway-{c}:2026.09.24" for c in COMPONENTS}  # mirror override dropped
    assert set(changes) == set(COMPONENTS)


def test_source_uses_configured_prefix(tmp_path: Path) -> None:
    cfg = _public(tmp_path, registries={"supported": {"prefix": "registry.wso2.com/acme"}})
    retag_base_images(cfg, "2026.10.0", source="supported")
    assert cfg.image("controller") == "registry.wso2.com/acme/gateway-controller:2026.10.0"


def test_source_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="image source must be one of"):
        retag_base_images(_public(tmp_path), "2026.10.0", source="private")


def test_prefix_from_earlier_build_selects_its_source(tmp_path: Path) -> None:
    cfg = _public(tmp_path)
    retag_base_images(cfg, "2026.10.0", prefix=SUPPORTED)
    assert cfg.image_source == "supported" and cfg.image("runtime") == f"{SUPPORTED}/gateway-runtime:2026.10.0"


def test_prefix_of_a_mirror_is_used_as_is(tmp_path: Path) -> None:
    cfg = _public(tmp_path)
    retag_base_images(cfg, "2026.10.0", prefix="mirror.example.com/wso2")
    assert cfg.image("builder") == "mirror.example.com/wso2/gateway-builder:2026.10.0"


def test_source_wins_over_prefix(tmp_path: Path) -> None:
    cfg = _public(tmp_path)
    retag_base_images(cfg, "2026.09.24", source="public", prefix=SUPPORTED)
    assert cfg.image_source == "public" and cfg.registry_prefix == PUBLIC


def test_source_for_prefix() -> None:
    assert source_for_prefix(DEFAULT_REGISTRIES, SUPPORTED) == ("supported", DEFAULT_REGISTRIES)
    regs = dict(DEFAULT_REGISTRIES, supported="registry.wso2.com/acme")
    assert source_for_prefix(regs, "registry.wso2.com/acme")[0] == "supported"
    assert source_for_prefix(regs, "mirror.example.com/x") == ("public", dict(regs, public="mirror.example.com/x"))


def test_base_prefix_from_label_or_stock_reference() -> None:
    label = {"build.base-image": f"{SUPPORTED}/gateway-controller:1.2.0.15"}
    assert base_prefix_from("localhost/demo-gateway-controller:1", label) == SUPPORTED
    assert base_prefix_from(f"{PUBLIC}/gateway-controller:1.2.1", {}) == PUBLIC
    assert base_prefix_from("localhost/demo-gateway-controller:1", {}) is None


def test_cli_source_needs_base(tmp_path: Path) -> None:
    (tmp_path / "aigw.yaml").write_text("gateway: {version: '1.2.1', name: demo}\n")
    result = CliRunner().invoke(cli.app, ["upgrade", "-f", str(tmp_path / "aigw.yaml"), "--source", "supported"])
    assert result.exit_code == 1 and "--source needs --base" in result.output


def test_cli_base_follows_from_image_registry(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "aigw.yaml").write_text("gateway: {version: '1.2.1', name: demo}\n")
    cfg = cli._build_config(tmp_path / "aigw.yaml", None, None)
    monkeypatch.setattr(cli, "base_prefix_of", lambda image, **kw: SUPPORTED)
    monkeypatch.setattr(cli.dk, "check_docker", lambda: None)
    monkeypatch.setattr(cli.dk, "image_available", lambda image: True)
    cli._upgrade_base(cfg, "2026.10.0", from_image="localhost/demo-gateway-controller:1.2.0.15")
    assert cfg.image("controller") == f"{SUPPORTED}/gateway-controller:2026.10.0"

    cfg = cli._build_config(tmp_path / "aigw.yaml", None, None)
    cli._upgrade_base(cfg, "2026.09.24", source="public", from_image="localhost/demo-gateway-controller:1.2.0.15")
    assert cfg.image("controller") == f"{PUBLIC}/gateway-controller:2026.09.24"


def test_cli_tag_does_not_move_last_build(tmp_path: Path) -> None:
    (tmp_path / "aigw.yaml").write_text("gateway: {version: '1.2.1', name: demo}\n")
    cfg = cli._build_config(tmp_path / "aigw.yaml", "last", "1.2.1-p1")
    assert cfg.output_tag == "1.2.1-p1"
    assert cfg.last_build_image == "localhost/demo-gateway-controller:1.2.1"


def test_cli_upgrade_base_reports_missing_images(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "aigw.yaml").write_text("gateway: {version: '1.2.1', name: demo}\n")
    monkeypatch.setattr(cli.dk, "check_docker", lambda: None)
    monkeypatch.setattr(cli.dk, "image_available", lambda image: "gateway-runtime" not in image)
    result = CliRunner().invoke(cli.app, ["upgrade", "-f", str(tmp_path / "aigw.yaml"), "--base", "2026.09.24",
                                          "--dry-run"])
    assert result.exit_code == 1
    assert "gateway-runtime:2026.09.24" in result.output and "docker login ghcr.io" in result.output
