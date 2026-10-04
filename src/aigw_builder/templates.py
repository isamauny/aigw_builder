"""Configuration templates written by `aigw-builder init`.

By default `init` writes the reference config, which documents every option with its built-in default. Its single
source is config/aigw.reference.yaml in the repository. Wheels bundle it as aigw_builder/reference.yaml (see
force-include in pyproject.toml); a source or editable checkout reads it from config/ directly.

Teams can keep their own starting point instead and pass it with `init --template` or AIGW_BUILDER_TEMPLATE,
which leaves the reference untouched.
"""

from importlib import resources
from pathlib import Path

REPO_REFERENCE = Path(__file__).resolve().parents[2] / "config" / "aigw.reference.yaml"


def _load() -> str:
    bundled = resources.files("aigw_builder").joinpath("reference.yaml")
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    return REPO_REFERENCE.read_text(encoding="utf-8")


SAMPLE_CONFIG = _load()
TEMPLATE_ENV = "AIGW_BUILDER_TEMPLATE"
