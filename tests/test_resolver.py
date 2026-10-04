import pytest

from aigw_builder.config import PolicyRef
from aigw_builder.image_inspect import InstalledPolicy, parse_manifest
from aigw_builder.resolver import ResolveError, merge, parse_version


def test_parse_version() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2") == (1, 2)
    assert parse_version("v1") == (1,)
    assert parse_version("latest") is None


def test_latest_uses_hub_and_highest_patch(resolver) -> None:
    r = resolver.resolve_ref(PolicyRef("semantic-cache"))
    assert r.version == "v1.10.0"  # numeric, not lexical, ordering
    assert r.ref == "github.com/wso2/gateway-controllers/policies/semantic-cache@v1.10.0"


def test_major_minor_and_exact(resolver) -> None:
    assert resolver.resolve_ref(PolicyRef("semantic-cache", "1.2")).version == "v1.2.0"
    assert resolver.resolve_ref(PolicyRef("jwt-auth", "v1.3.0")).version == "v1.3.0"


def test_python_policy_gets_pip_ref(resolver) -> None:
    r = resolver.resolve_ref(PolicyRef("granite-guardian-prompt-injection"))
    assert r.kind == "python"
    assert r.build_entry()["pipPackage"].endswith(
        "@policies/granite-guardian-prompt-injection/v0.9.0#subdirectory=policies/granite-guardian-prompt-injection"
    )


def test_kind_override(resolver) -> None:
    assert resolver.resolve_ref(PolicyRef("cors", kind="python")).kind == "python"


def test_unknown_policy_suggests(resolver) -> None:
    with pytest.raises(ResolveError, match="Did you mean: jwt-auth"):
        resolver.resolve_ref(PolicyRef("jwt-aut"))


def test_version_not_on_hub(resolver) -> None:
    with pytest.raises(ResolveError, match="not on the Policy Hub"):
        resolver.resolve_ref(PolicyRef("semantic-cache", "1.5"))


def test_not_on_hub_but_tagged_falls_back_to_tags(resolver) -> None:
    r = resolver.resolve_ref(PolicyRef("set-headers"))
    assert r.version == "v1.0.1" and r.notes


def test_resolve_all_collects_errors(resolver) -> None:
    ok, errors = resolver.resolve_all([PolicyRef("cors"), PolicyRef("nope"), PolicyRef("jwt-auth", "9.9")])
    assert [p.name for p in ok] == ["cors"]
    assert len(errors) == 2


MANIFEST = """
version: v1
policies:
  - name: jwt-auth
    version: v1.0.2
    gomodule: github.com/wso2/gateway-controllers/policies/jwt-auth@v1
  - name: granite-guardian-prompt-injection
    version: v0.9.0
    pipPackage: git+https://github.com/wso2/gateway-controllers.git@policies/granite-guardian-prompt-injection/v0.9.0#subdirectory=policies/granite-guardian-prompt-injection
  - name: cors
    version: v1.0.1
    gomodule: github.com/wso2/gateway-controllers/policies/cors@v1
  - name: my-local
    version: v0.1.0
    filePath: policies/my-local
"""


def test_manifest_policies_are_repinned(resolver) -> None:
    installed = {p.name: p for p in parse_manifest(MANIFEST)}
    jwt = resolver.resolve_installed(installed["jwt-auth"])
    assert jwt.ref.endswith("jwt-auth@v1.0.2") and jwt.origin == "base"
    py = resolver.resolve_installed(installed["granite-guardian-prompt-injection"])
    assert py.kind == "python" and py.ref == installed["granite-guardian-prompt-injection"].ref
    with pytest.raises(ResolveError, match="local path"):
        resolver.resolve_installed(installed["my-local"])


def test_definition_only_policy_maps_to_tag(resolver) -> None:
    r = resolver.resolve_installed(InstalledPolicy("cors", "v1.0.1", "unknown"))
    assert r.ref.endswith("cors@v1.0.1")
    with pytest.raises(ResolveError, match="no matching tag"):
        resolver.resolve_installed(InstalledPolicy("cors", "v2.0.0", "unknown"))


def test_untagged_base_version_uses_nearest_release(resolver) -> None:
    # Manifest says v1.0.1 but only v1.0.2 exists upstream -> nearest newer patch in the same minor.
    r = resolver.resolve_installed(
        InstalledPolicy("jwt-auth", "v1.0.1", "go", "github.com/wso2/gateway-controllers/policies/jwt-auth@v1")
    )
    assert r.version == "v1.0.2" and r.ref.endswith("jwt-auth@v1.0.2") and r.notes
    # Nothing newer in the minor -> highest older patch.
    assert resolver.resolve_installed(InstalledPolicy("cors", "v1.0.9", "unknown")).version == "v1.0.1"
    # No release at all in that minor -> error.
    with pytest.raises(ResolveError):
        resolver.resolve_installed(InstalledPolicy("jwt-auth", "v1.2.5", "go", "github.com/wso2/gateway-controllers/policies/jwt-auth@v1"))


def test_third_party_module_kept_as_recorded(resolver) -> None:
    r = resolver.resolve_installed(InstalledPolicy("x", "v0.3.1", "go", "github.com/acme/policies/x@v0"))
    assert r.ref == "github.com/acme/policies/x@v0.3.1"


def test_merge(resolver) -> None:
    installed = [p for p in parse_manifest(MANIFEST) if p.name != "my-local"]
    base = {p.name: resolver.resolve_installed(p) for p in installed if p.name in ("granite-guardian-prompt-injection",)}
    configured = [resolver.resolve_ref(PolicyRef("jwt-auth")), resolver.resolve_ref(PolicyRef("semantic-cache", "1.2"))]
    final, rows = merge(installed, base, configured, excluded=["cors"])
    assert [p.name for p in final] == ["granite-guardian-prompt-injection", "jwt-auth", "semantic-cache"]
    status = {r.name: r.status for r in rows}
    assert status == {
        "cors": "excluded",
        "granite-guardian-prompt-injection": "kept",
        "jwt-auth": "upgraded",
        "semantic-cache": "added",
    }
