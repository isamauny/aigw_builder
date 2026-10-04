from pathlib import Path

from typer.testing import CliRunner

from aigw_builder.cli import app
from aigw_builder.templates import SAMPLE_CONFIG, TEMPLATE_ENV

runner = CliRunner()
TEAM = """# ACME team template
gateway:
  version: "1.2.0.11"
  name: acme-gateway
image_source: supported
output:
  repository: registry.acme.example/gateways
policies:
  - llm-cost
"""


def _init(tmp_path: Path, *args: str, env=None):
    out = tmp_path / "aigw.yaml"
    result = runner.invoke(app, ["init", "-o", str(out), *args], env=env or {TEMPLATE_ENV: ""})
    return result, out


def test_default_writes_reference(tmp_path: Path) -> None:
    result, out = _init(tmp_path)
    assert result.exit_code == 0, result.output
    assert out.read_text() == SAMPLE_CONFIG


def test_template_option(tmp_path: Path) -> None:
    team = tmp_path / "team.yaml"
    team.write_text(TEAM)
    result, out = _init(tmp_path, "--template", str(team))
    assert result.exit_code == 0, result.output
    assert out.read_text() == TEAM


def test_template_from_env_and_option_wins(tmp_path: Path) -> None:
    team = tmp_path / "team.yaml"
    team.write_text(TEAM)
    result, out = _init(tmp_path, env={TEMPLATE_ENV: str(team)})
    assert result.exit_code == 0, result.output
    assert out.read_text() == TEAM
    other = tmp_path / "other.yaml"
    other.write_text(TEAM.replace("acme-gateway", "other-gateway"))
    result, out = _init(tmp_path, "--force", "--template", str(other), env={TEMPLATE_ENV: str(team)})
    assert result.exit_code == 0 and "other-gateway" in out.read_text()


def test_invalid_template_rejected_unless_no_validate(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("image_source: private\npolicies: [llm-cost]\n")
    result, out = _init(tmp_path, "--template", str(bad))
    assert result.exit_code == 2 and not out.exists()
    assert "gateway.version" in result.output and "image_source" in result.output
    result, out = _init(tmp_path, "--template", str(bad), "--no-validate")
    assert result.exit_code == 0 and out.read_text() == bad.read_text()


def test_missing_template_and_no_overwrite(tmp_path: Path) -> None:
    result, _ = _init(tmp_path, "--template", str(tmp_path / "nope.yaml"))
    assert result.exit_code != 0 and "template not found" in result.output
    result, out = _init(tmp_path)
    assert result.exit_code == 0
    result, _ = _init(tmp_path)
    assert result.exit_code != 0 and "already exists" in result.output


def test_examples_work_as_templates(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("aigw.public.yaml", "aigw.supported.yaml"):
        example = root / "examples" / name
        out = tmp_path / name
        result = runner.invoke(app, ["init", "--template", str(example), "-o", str(out)], env={TEMPLATE_ENV: ""})
        assert result.exit_code == 0, (name, result.output)
        assert out.read_text() == example.read_text()
