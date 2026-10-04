from aigw_builder.config import PolicyRef
from aigw_builder.image_inspect import ImageInventory, InstalledPolicy
from aigw_builder.outdated import OutdatedRow, rows_for_image


def test_upgrade_targets_levels(resolver) -> None:
    t = resolver.upgrade_targets("major-pol", "v1.0.0")
    # minor follows the hub (1.1), not the newest tag (v1.2.0 is unpublished)
    assert t == {"patch": "v1.0.1", "minor": "v1.1.0", "latest": "v2.0.0"}
    assert resolver.upgrade_targets("jwt-auth", "v1.3.1") == {"patch": None, "minor": None, "latest": None}
    assert resolver.upgrade_targets("jwt-auth", "v1.0.2")["minor"] == "v1.3.1"


def test_upgraded_moves_base_policy_only_up_to_level(resolver) -> None:
    base = resolver.resolve_installed(
        InstalledPolicy("major-pol", "v1.0.0", "go", "github.com/wso2/gateway-controllers/policies/major-pol@v1"))
    assert resolver.upgraded(base, "none") is base
    assert resolver.upgraded(base, "patch").version == "v1.0.1"
    assert resolver.upgraded(base, "minor").ref.endswith("major-pol@v1.1.0")
    up = resolver.upgraded(base, "latest")
    assert up.version == "v2.0.0" and any("upgraded" in n for n in up.notes)
    local = resolver.resolve_ref(PolicyRef("cors"))
    local.kind, local.ref = "local", "/tmp/x"
    assert resolver.upgraded(local, "latest") is local


def test_status() -> None:
    assert OutdatedRow("a", "v1.0.0", patch="v1.0.1").status == "patch"
    assert OutdatedRow("a", "v1.0.0", patch="v1.0.3", minor="v1.0.3", latest="v1.0.3").status == "patch"
    assert OutdatedRow("a", "v1.0.0", minor="v1.1.0", latest="v1.1.0").status == "minor"
    assert OutdatedRow("a", "v1.0.0", latest="v2.0.0").status == "major"
    assert OutdatedRow("a", "v1.0.0").status == "up to date"
    assert OutdatedRow("a", "", note="x").status == "unknown"


def test_rows_for_image_uses_recorded_version(resolver) -> None:
    inv = ImageInventory("img", "/app/build-manifest.yaml", [
        InstalledPolicy("jwt-auth", "v1.0.1", "go", "github.com/wso2/gateway-controllers/policies/jwt-auth@v1"),
        InstalledPolicy("thirdparty", "v0.1.0", "go", "github.com/acme/x@v0"),
    ])
    rows = {r.name: r for r in rows_for_image(resolver, inv)}
    assert rows["jwt-auth"].current == "v1.0.1"  # untagged upstream, still reported as installed
    assert rows["jwt-auth"].patch == "v1.0.2" and rows["jwt-auth"].status == "minor"
    assert rows["thirdparty"].note
