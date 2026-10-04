from pathlib import Path

import pytest

from aigw_builder import builder
from aigw_builder.builder import BuildError, resolve_baseline
from aigw_builder.config import parse_config


def _cfg(tmp_path: Path):
    return parse_config(
        {"gateway": {"version": "1.2.0.11", "name": "demo"}, "output": {"repository": "local", "tag": "1.2.0.12"}},
        tmp_path / "aigw.yaml",
    )


def test_default_baseline_is_base_controller(tmp_path: Path) -> None:
    assert resolve_baseline(_cfg(tmp_path), None) == ("ghcr.io/wso2/api-platform/gateway-controller:1.2.0.11", True)


def test_last_is_previous_build_and_never_pulled(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.last_build_image == "local/demo-gateway-controller:1.2.0.12"
    monkeypatch.setattr(builder.dk, "image_exists", lambda image: True)
    assert resolve_baseline(cfg, "last") == ("local/demo-gateway-controller:1.2.0.12", False)
    monkeypatch.setattr(builder.dk, "image_exists", lambda image: False)
    with pytest.raises(BuildError, match="no previous build"):
        resolve_baseline(cfg, "last")


def test_explicit_image(tmp_path: Path) -> None:
    ref = "docker.io/local/demo-ai-gateway-gateway-controller:1.2.0.12"
    assert resolve_baseline(_cfg(tmp_path), ref) == (ref, True)
