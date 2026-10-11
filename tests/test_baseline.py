from pathlib import Path

import pytest

from aigw_builder import builder
from aigw_builder.builder import BuildError, resolve_baseline
from aigw_builder.config import parse_config
from aigw_builder.image_inspect import ImageInventory, InstalledPolicy


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


def test_include_new_adds_hub_policies_not_in_build(tmp_path: Path, monkeypatch, resolver) -> None:
    cfg = parse_config({"gateway": {"version": "1.2.0.11"}, "policies": ["semantic-cache"],
                        "exclude_base_policies": ["cors"]}, tmp_path / "aigw.yaml")
    inv = ImageInventory("img", "/app/build-manifest.yaml", [
        InstalledPolicy("jwt-auth", "v1.0.2", "go", "github.com/wso2/gateway-controllers/policies/jwt-auth@v1"),
        InstalledPolicy("cors", "v1.0.1", "go", "github.com/wso2/gateway-controllers/policies/cors@v1"),
    ])
    monkeypatch.setattr(builder, "inspect_image", lambda *a, **kw: inv)
    resolver.hub.data["untagged"] = ["1.0"]  # on the hub, but no release tag to build from

    assert [p.name for p in builder.plan_build(cfg, resolver).policies] == ["jwt-auth", "semantic-cache"]
    plan = builder.plan_build(cfg, resolver, include_new=True)
    assert plan.ok
    added = {p.name: p for p in plan.policies if p.origin == "hub"}
    assert sorted(added) == ["granite-guardian-prompt-injection", "major-pol"]  # cors stays excluded
    assert added["major-pol"].version == "v2.0.0"  # latest on the hub
    assert {r.name: r.status for r in plan.diff}["major-pol"] == "added"
    assert any("untagged" in w for w in plan.warnings)
