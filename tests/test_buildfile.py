from pathlib import Path

import yaml

from aigw_builder.builder import ap_command
from aigw_builder.buildfile import render
from aigw_builder.config import PolicyRef, parse_config
from aigw_builder.image_inspect import parse_definition_dir


def test_render_build_yaml(tmp_path: Path, resolver) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0", "name": "gw"}}, tmp_path / "aigw.yaml")
    policies = [resolver.resolve_ref(PolicyRef("jwt-auth")), resolver.resolve_ref(PolicyRef("granite-guardian-prompt-injection"))]
    doc = yaml.safe_load(render(cfg, policies))
    assert doc["version"] == "v1"
    assert doc["gateway"]["version"] == "1.1.0"
    assert set(doc["gateway"]["images"]) == {"builder", "controller", "runtime"}
    assert doc["policies"][0]["name"] == "granite-guardian-prompt-injection"
    assert "pipPackage" in doc["policies"][0]
    assert doc["policies"][1] == {"name": "jwt-auth", "gomodule": "github.com/wso2/gateway-controllers/policies/jwt-auth@v1.3.1"}


def test_ap_command(tmp_path: Path) -> None:
    cfg = parse_config(
        {"gateway": {"version": "1.1.0", "name": "gw"},
         "output": {"repository": "r/x", "push": True, "platform": "linux/amd64", "output_dir": "dist"}},
        tmp_path / "aigw.yaml",
    )
    cmd = ap_command(cfg, Path("/w"))
    assert cmd[:6] == ["ap", "gateway", "image", "build", "--path", "/w"]
    assert ["--repository", "r/x"] == cmd[cmd.index("--repository"):cmd.index("--repository") + 2]
    assert "--push" in cmd and "--platform" in cmd
    assert cmd[-1] == str((tmp_path / "dist").resolve())


def test_parse_definition_dir_both_layouts(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: a\nversion: v1.0.0\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "policy-definition.yaml").write_text("name: b\nversion: v2.0.0\n")
    (tmp_path / "junk.yaml").write_text("- not a mapping\n")
    found = parse_definition_dir(tmp_path)
    assert [(p.name, p.version) for p in found] == [("a", "v1.0.0"), ("b", "v2.0.0")]


def test_ap_command_defaults_to_local_repository(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0", "name": "gw"}}, tmp_path / "aigw.yaml")
    cmd = ap_command(cfg, Path("/w"))
    assert cmd[cmd.index("--repository") + 1] == "localhost"
    assert "--push" not in cmd


def test_output_tag_goes_into_build_yaml(tmp_path: Path, resolver) -> None:
    cfg = parse_config(
        {"gateway": {"version": "1.1.0", "name": "gw"}, "images": {"runtime": "1.1.0-3"}, "output": {"tag": "2026.10"}},
        tmp_path / "aigw.yaml",
    )
    doc = yaml.safe_load(render(cfg, [resolver.resolve_ref(PolicyRef("cors"))]))
    assert doc["gateway"]["version"] == "2026.10"  # ap tags the output with this
    assert doc["gateway"]["images"]["runtime"].endswith("/gateway-runtime:1.1.0-3")
    assert doc["gateway"]["images"]["controller"].endswith("/gateway-controller:1.1.0")


def test_build_files_dir_default_and_override(tmp_path: Path) -> None:
    cfg = parse_config({"gateway": {"version": "1.1.0", "name": "gw"}}, tmp_path / "aigw.yaml")
    assert cfg.build_files_dir == tmp_path / "aigw-build" / "gw"
    cfg = parse_config({"gateway": {"version": "1.1.0"}, "output": {"build_files_dir": "review"}}, tmp_path / "aigw.yaml")
    assert cfg.build_files_dir == (tmp_path / "review").resolve()


def test_write_build_file_drops_stale_manifest(tmp_path: Path, resolver) -> None:
    from aigw_builder.builder import MANIFEST_FILE, BuildPlan, write_build_file

    cfg = parse_config({"gateway": {"version": "1.1.0", "name": "gw"}}, tmp_path / "aigw.yaml")
    stale = cfg.build_files_dir / MANIFEST_FILE
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    path = write_build_file(BuildPlan(cfg=cfg, policies=[resolver.resolve_ref(PolicyRef("cors"))]))
    assert path == cfg.build_files_dir / "build.yaml" and "cors@v1.0.1" in path.read_text()
    assert not stale.exists()
