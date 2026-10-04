from pathlib import Path

import pytest

from aigw_builder import builder
from aigw_builder.builder import BuildError, check_platform
from aigw_builder.config import parse_config


def _cfg(tmp_path: Path, **output):
    return parse_config({"gateway": {"version": "1.1.0"}, "output": output}, tmp_path / "aigw.yaml")


@pytest.fixture(autouse=True)
def arm_host(monkeypatch):
    monkeypatch.setattr(builder.dk, "host_arch", lambda: "arm64")


def test_no_platform_is_fine(tmp_path: Path) -> None:
    check_platform(_cfg(tmp_path))


def test_foreign_and_multi_platform_rejected(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="wrong CPU"):
        check_platform(_cfg(tmp_path, platform="linux/amd64", repository="r.example.com/x", push=True))
    with pytest.raises(BuildError, match="wrong CPU"):
        check_platform(_cfg(tmp_path, platform="linux/amd64,linux/arm64", repository="r.example.com/x", push=True))


def test_native_platform_needs_push(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="push"):
        check_platform(_cfg(tmp_path, platform="linux/arm64"))
    check_platform(_cfg(tmp_path, platform="linux/arm64", repository="r.example.com/x", push=True))
