from pathlib import Path

import pytest

from aigw_builder.config import ConfigError, load_config, parse_config


def test_minimal_defaults(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0"}}, tmp_path / "aigw.yaml")
    assert cfg.image_source == "public"
    assert cfg.base_policies == "keep"
    assert cfg.gateway_name == tmp_path.name
    assert cfg.image("runtime") == "ghcr.io/wso2/api-platform/gateway-runtime:1.1.0"


def test_supported_registry_and_overrides(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "gateway": {"version": "1.1.0", "name": "gw"},
            "image_source": "supported",
            "registries": {"supported": {"prefix": "registry.wso2.com/acme/"}},
            "images": {"builder": "my/builder:x"},
        },
        tmp_path / "aigw.yaml",
    )
    assert cfg.image("controller") == "registry.wso2.com/acme/gateway-controller:1.1.0"
    assert cfg.image("builder") == "my/builder:x"


def test_local_policy_path_resolved_relative_to_config(tmp_path: Path) -> None:
    pol = tmp_path / "policies" / "mine"
    pol.mkdir(parents=True)
    (pol / "policy-definition.yaml").write_text("name: mine\nversion: v0.1.0\n")
    cfg = parse_config(
        {"gateway": {"version": "1.1.0"}, "policies": [{"name": "mine", "path": "policies/mine"}, "cors"]},
        tmp_path / "aigw.yaml",
    )
    assert cfg.policies[0].path == pol.resolve()
    assert cfg.policies[1].name == "cors" and not cfg.policies[1].is_local


def test_errors_are_collected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_config(
            {
                "image_source": "private",
                "images": {"sidecar": "x"},
                "policies": [{"name": "a"}, {"name": "a", "kind": "rust", "extra": 1}, {"version": "1.0"}],
            },
            tmp_path / "aigw.yaml",
        )
    text = "\n".join(exc.value.errors)
    for needle in ("gateway.version", "image_source", "images.sidecar", "duplicate", "kind", "unknown keys",
                   "policies[2].name"):
        assert needle in text


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_supported_has_default_prefix(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0"}, "image_source": "supported"}, tmp_path / "aigw.yaml")
    assert cfg.image("runtime") == "registry.wso2.com/wso2-api-platform/gateway-runtime:1.1.0"


def test_output_is_local_by_default(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0"}}, tmp_path / "aigw.yaml")
    assert cfg.output.repository == "localhost" and cfg.output.push is False
    assert cfg.upgrade_base_policies == "none"
    with pytest.raises(ConfigError, match="output.push"):
        parse_config({"gateway": {"version": "1.1.0"}, "output": {"push": True}}, tmp_path / "aigw.yaml")
    with pytest.raises(ConfigError, match="upgrade_base_policies"):
        parse_config({"gateway": {"version": "1.1.0"}, "upgrade_base_policies": "all"}, tmp_path / "aigw.yaml")


def test_image_override_tag_or_full_reference(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "gateway": {"version": "1.1.0"},
            "image_source": "supported",
            "registries": {"supported": {"prefix": "registry.wso2.com/wso2-api-platform"}},
            "images": {"controller": "1.1.0-2", "runtime": "my.reg/x/gateway-runtime:9"},
        },
        tmp_path / "aigw.yaml",
    )
    assert cfg.image("builder") == "registry.wso2.com/wso2-api-platform/gateway-builder:1.1.0"
    assert cfg.image("controller") == "registry.wso2.com/wso2-api-platform/gateway-controller:1.1.0-2"
    assert cfg.image("runtime") == "my.reg/x/gateway-runtime:9"
    with pytest.raises(ConfigError, match="images.builder"):
        parse_config({"gateway": {"version": "1.1.0"}, "images": {"builder": "bad tag!"}}, tmp_path / "aigw.yaml")


def test_output_tag(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0"}}, tmp_path / "aigw.yaml")
    assert cfg.output_tag == "1.1.0"
    cfg = parse_config({"gateway": {"version": "1.1.0"}, "output": {"tag": "1.1.0-acme.1"}}, tmp_path / "aigw.yaml")
    assert cfg.output_tag == "1.1.0-acme.1"
    with pytest.raises(ConfigError, match="output.tag"):
        parse_config({"gateway": {"version": "1.1.0"}, "output": {"tag": "-bad"}}, tmp_path / "aigw.yaml")



def test_local_and_localhost_are_both_local_only(tmp_path: Path) -> None:
    from aigw_builder.config import is_local_repository

    for repo in ("localhost", "local", "docker.io/local", "Local/"):
        assert is_local_repository(repo), repo
        with pytest.raises(ConfigError, match="local images only"):
            parse_config({"gateway": {"version": "1.1.0"}, "output": {"repository": repo, "push": True}},
                         tmp_path / "aigw.yaml")
    for repo in ("localhost:5000", "local.example.com/team", "myregistry.example.com/local"):
        assert not is_local_repository(repo), repo
