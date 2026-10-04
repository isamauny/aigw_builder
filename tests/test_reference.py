from pathlib import Path

import yaml

from aigw_builder.config import parse_config
from aigw_builder.templates import SAMPLE_CONFIG

ROOT = Path(__file__).resolve().parents[1]


def test_init_writes_the_config_reference() -> None:
    assert (ROOT / "config" / "aigw.reference.yaml").read_text() == SAMPLE_CONFIG


def test_reference_and_examples_are_valid(tmp_path: Path) -> None:
    for f in ["config/aigw.reference.yaml", "examples/aigw.public.yaml", "examples/aigw.supported.yaml"]:
        raw = yaml.safe_load((ROOT / f).read_text())
        cfg = parse_config(raw, tmp_path / f)
        assert cfg.gateway_version


def test_reference_states_the_real_defaults(tmp_path: Path) -> None:
    ref = parse_config(yaml.safe_load(SAMPLE_CONFIG), tmp_path / "aigw.yaml")
    default = parse_config({"gateway": {"version": "1.1.0"}}, tmp_path / "aigw.yaml")
    for attr in ("image_source", "base_policies", "upgrade_base_policies", "exclude_base_policies", "image_overrides"):
        assert getattr(ref, attr) == getattr(default, attr), attr
    assert ref.registries == default.registries
    # `init` must not write placeholders into settings that are active (not commented out).
    active = [line.split("#", 1)[0] for line in SAMPLE_CONFIG.splitlines()]
    assert not [line for line in active if "<" in line], "placeholder in an active setting"
    for attr in ("repository", "push", "platform", "no_cache", "output_dir", "buildx_builder"):
        assert getattr(ref.output, attr) == getattr(default.output, attr), attr
