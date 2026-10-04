"""aigw-builder command line interface."""

from __future__ import annotations

import difflib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
import yaml
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .builder import (LAST_BUILD, MANIFEST_FILE, BuildError, ap_command, base_images_of, check_platform,
                      plan_build, preflight, run_build, write_build_file)
from .outdated import rows_for_image, rows_for_policies
from .config import (TAG_RE, UPGRADE_LEVELS, Config, ConfigError, config_from_image, is_local_repository,
                     load_config, parse_config)
from .docker_utils import DockerError
from .hub import HubError, HubNotFound, PolicyHubClient
from .image_inspect import InspectError, inspect_image
from .resolver import ResolveError, Resolver, diff_status
from .templates import SAMPLE_CONFIG, TEMPLATE_ENV

app = typer.Typer(
    help="Build WSO2 API Platform (AI) Gateway images with Policy Hub policies, driven by a YAML config.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

STATE: Dict[str, Any] = {"hub_url": None, "verbose": False}

STATUS_STYLE = {
    "added": "green", "upgraded": "cyan", "downgraded": "yellow", "changed": "yellow",
    "excluded": "red", "kept": "dim", "unchanged": "dim",
}


def _hub() -> PolicyHubClient:
    return PolicyHubClient(STATE["hub_url"])


def _fail(message: str, code: int = 1) -> "typer.Exit":
    err.print(f"[bold red]error:[/] {message}")
    return typer.Exit(code)


def _load(config: Path) -> Config:
    try:
        return load_config(config)
    except ConfigError as exc:
        err.print(f"[bold red]Invalid config[/] {exc.source}:")
        for e in exc.errors:
            err.print(f"  - {e}")
        raise typer.Exit(2)


def _short(text: str, width: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


@app.callback()
def main(
    hub_url: Optional[str] = typer.Option(None, "--hub-url", envvar="WSO2AP_POLICYHUB_BASE_URL",
                                          help="Policy Hub base URL."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show more detail."),
) -> None:
    STATE["hub_url"] = hub_url
    STATE["verbose"] = verbose


@app.command()
def version() -> None:
    """Print the aigw-builder version."""
    console.print(__version__)


# --------------------------------------------------------------------------- hub browsing

@app.command()
def categories(as_json: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """List Policy Hub categories with the number of policies in each."""
    try:
        with _hub() as hub:
            cats = hub.categories()
            counts = Counter(c for p in hub.list_policies() for c in p.categories)
    except HubError as exc:
        raise _fail(str(exc))
    if as_json:
        console.print_json(json.dumps([{"category": c, "policies": counts.get(c, 0)} for c in cats]))
        return
    table = Table(title="Policy Hub categories")
    table.add_column("Category", style="bold")
    table.add_column("Policies", justify="right")
    for c in cats:
        table.add_row(c, str(counts.get(c, 0)))
    console.print(table)
    console.print("[dim]Filter with: aigw-builder policies -c <category>[/]")


@app.command()
def policies(
    category: Optional[List[str]] = typer.Option(None, "--category", "-c",
                                                 help="Filter by category (repeat for several; OR match)."),
    search: Optional[str] = typer.Option(None, "--search", "-s", help="Case-insensitive match on name/description."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
    wide: bool = typer.Option(False, "--wide", "-w", help="Show full descriptions."),
) -> None:
    """List policies on the Policy Hub (latest versions), optionally filtered by category."""
    try:
        with _hub() as hub:
            if category:
                known = {c.lower(): c for c in hub.categories()}
                unknown = [c for c in category if c.lower() not in known]
                if unknown:
                    raise _fail(f"unknown category: {', '.join(unknown)} (known: {', '.join(known.values())})")
                category = [known[c.lower()] for c in category]
            items = hub.list_policies(category)
    except HubError as exc:
        raise _fail(str(exc))
    if search:
        needle = search.lower()
        items = [p for p in items if needle in p.name.lower() or needle in p.description.lower()
                 or needle in p.display_name.lower()]
    items.sort(key=lambda p: p.name)
    if as_json:
        console.print_json(json.dumps([p.to_dict() for p in items]))
        return
    title = "Policies" + (f" in {', '.join(category)}" if category else "")
    table = Table(title=f"{title} ({len(items)})")
    # Rich shrinks wrappable columns to nothing before truncating a long no_wrap cell, so give every
    # column an explicit width that adds up to the terminal width. Each column costs 3 chars of
    # border/padding, plus 1 for the closing border.
    name_w = max([len(p.name) for p in items] + [4])
    cat_nat = max([len(", ".join(p.categories)) for p in items] + [10])
    left = console.width - (name_w + 3) - (6 + 3) - 1
    show_desc = left - 3 - min(cat_nat, 14) - 3 >= 15
    if show_desc:
        cat_w = max(10, min(cat_nat, 28, left - 3 - 30 - 3))
        desc_w = left - (cat_w + 3) - 3
    else:
        cat_w = max(10, min(cat_nat, left - 3))
    table.add_column("Name", style="bold", no_wrap=True, width=name_w)
    table.add_column("Latest", justify="right", no_wrap=True, width=6)
    table.add_column("Categories", width=cat_w)
    if show_desc:
        table.add_column("Description", width=desc_w, no_wrap=not wide, overflow="ellipsis")
    for p in items:
        row = [p.name, p.version, ", ".join(p.categories)]
        if show_desc:
            row.append(" ".join(p.description.split()))
        table.add_row(*row)
    console.print(table)
    if not show_desc:
        console.print("[dim]Terminal too narrow for descriptions; widen it or use: aigw-builder show <name>[/]")


@app.command()
def show(
    name: str = typer.Argument(..., help="Policy name."),
    version_: Optional[str] = typer.Option(None, "--version", help="Version to show parameters for (default: latest)."),
) -> None:
    """Show a policy's versions and the parameters from its definition."""
    try:
        with _hub() as hub:
            try:
                versions = hub.versions(name)
            except HubNotFound:
                names = sorted(p.name for p in hub.list_policies())
                close = difflib.get_close_matches(name, names, n=3)
                raise _fail(f"policy {name!r} not found" + (f"; did you mean {', '.join(close)}?" if close else ""))
            if not versions:
                raise _fail(f"policy {name!r} has no versions")
            latest = next((v for v in versions if v.is_latest), versions[0])
            target = version_ or latest.version
            definition = hub.definition(name, target)
    except HubError as exc:
        raise _fail(str(exc))

    console.print(f"[bold]{latest.display_name}[/] ([cyan]{name}[/]) - {latest.provider}")
    console.print(f"Categories: {', '.join(latest.categories)}")
    console.print(f"Versions:   {', '.join(v.version + (' (latest)' if v.is_latest else '') for v in versions)}")
    console.print()
    console.print(" ".join(latest.description.split()))
    params = (definition.get("parameters") or {}).get("properties") or {}
    required = set((definition.get("parameters") or {}).get("required") or [])
    if params:
        table = Table(title=f"Parameters ({target})")
        table.add_column("Parameter", style="bold")
        table.add_column("Type")
        table.add_column("Req")
        table.add_column("Default")
        table.add_column("Description")
        for key, spec in params.items():
            spec = spec or {}
            default = spec.get("default")
            table.add_row(key, str(spec.get("type", "")), "yes" if key in required else "",
                          "" if default is None else json.dumps(default), _short(str(spec.get("description", "")), 60))
        console.print(table)
    elif STATE["verbose"]:
        console.print(Syntax(yaml.safe_dump(definition, sort_keys=False), "yaml"))


# --------------------------------------------------------------------------- image inspection

@app.command()
def inspect(
    image: Optional[str] = typer.Argument(None, help="Gateway controller image to inspect."),
    config: Optional[Path] = typer.Option(None, "--file", "-f", help="Inspect the base image of this config and diff."),
    pull: bool = typer.Option(True, "--pull/--no-pull", help="Pull the image if it is not available locally."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List the policies already baked into a gateway (controller) image."""
    if bool(image) == bool(config):
        raise _fail("give either an IMAGE or --file <config>")
    if config:
        _inspect_against_config(_load(config), pull, as_json)
        return
    try:
        inv = inspect_image(image, pull=pull)
    except (InspectError, DockerError) as exc:
        raise _fail(str(exc))
    if as_json:
        console.print_json(json.dumps({"image": inv.image, "source": inv.source,
                                       "policies": [p.to_dict() for p in inv.policies]}))
        return
    table = Table(title=f"{inv.image} - {len(inv.policies)} policies (from {inv.source})")
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Type")
    if STATE["verbose"]:
        table.add_column("Source ref")
    for p in inv.policies:
        row = [p.name, p.version, "-" if p.kind == "unknown" else p.kind]
        if STATE["verbose"]:
            row.append(p.ref or "")
        table.add_row(*row)
    console.print(table)


def _inspect_against_config(cfg: Config, pull: bool, as_json: bool) -> None:
    image = cfg.image("controller")
    try:
        inv = inspect_image(image, pull=pull, platform=cfg.output.platform)
        with _hub() as hub:
            configured, errors = Resolver(hub).resolve_all(cfg.policies)
    except (InspectError, DockerError, HubError, ResolveError) as exc:
        raise _fail(str(exc))
    installed = inv.by_name()
    rows = []
    for name, p in sorted(installed.items()):
        rows.append({"name": name, "base": p.version, "config": "", "status": "kept"})
        if name in cfg.exclude_base_policies or cfg.base_policies == "none":
            rows[-1]["status"] = "excluded"
    by_row = {r["name"]: r for r in rows}
    for c in configured:
        if c.name in by_row:
            by_row[c.name].update(config=c.version or c.kind, status=diff_status(by_row[c.name]["base"], c.version))
        else:
            rows.append({"name": c.name, "base": "", "config": c.version or c.kind, "status": "added"})
    rows.sort(key=lambda r: r["name"])
    if as_json:
        console.print_json(json.dumps({"image": image, "source": inv.source, "diff": rows, "errors": errors}))
    else:
        table = Table(title=f"{image} vs {cfg.source.name}")
        table.add_column("Policy", style="bold")
        table.add_column("In image")
        table.add_column("Config")
        table.add_column("Result")
        for r in rows:
            style = STATUS_STYLE.get(r["status"], "")
            table.add_row(r["name"], r["base"], r["config"], f"[{style}]{r['status']}[/]")
        console.print(table)
        if cfg.base_policies == "none":
            console.print("[yellow]base_policies: none - only the configured policies will be built.[/]")
    if errors:
        for e in errors:
            err.print(f"[red]  - {e}[/]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------- config + build

@app.command()
def init(
    output: Path = typer.Option(Path("aigw.yaml"), "--output", "-o", help="Where to write the config."),
    template: Optional[Path] = typer.Option(
        None, "--template", "-t", envvar=TEMPLATE_ENV,
        help="Start from this config file instead of the reference (e.g. your team's template).",
    ),
    no_validate: bool = typer.Option(False, "--no-validate", help="Write the template even if it is not a valid config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a starting config: the fully commented reference, or your own template."""
    if output.exists() and not force:
        raise _fail(f"{output} already exists (use --force to overwrite)")
    if template is None:
        text, source = SAMPLE_CONFIG, "the reference config"
    else:
        template = template.expanduser()
        if not template.is_file():
            raise _fail(f"template not found: {template}")
        text, source = template.read_text(encoding="utf-8"), str(template)
        if not no_validate:
            # Validate as if it already sat at the output location, so relative paths resolve the same way.
            try:
                parse_config(yaml.safe_load(text), output.expanduser().resolve())
            except yaml.YAMLError as exc:
                raise _fail(f"template {template} is not valid YAML: {exc}", 2)
            except ConfigError as exc:
                err.print(f"[bold red]Template {template} is not a valid config:[/]")
                for e in exc.errors:
                    err.print(f"  - {e}")
                err.print("[dim]Fix the template, or pass --no-validate to write it anyway.[/]")
                raise typer.Exit(2)
    output.write_text(text, encoding="utf-8")
    console.print(f"Wrote [bold]{output}[/] from {source}. Edit it, then run: aigw-builder validate -f {output}")


def _print_plan(plan, show_base: bool = True) -> None:
    cfg = plan.cfg
    console.print(f"[bold]Gateway[/] {cfg.gateway_name} {cfg.gateway_version} -> tag {cfg.output_tag}  "
                  f"[bold]images[/] {cfg.image_source} ({cfg.registry_prefix})")
    for comp, img in cfg.images().items():
        console.print(f"  {comp:<10} {img}")
    if plan.base_inventory and show_base:
        console.print(f"Carried-over policies read from [bold]{plan.baseline_image}[/] "
                      f"({len(plan.base_inventory.policies)} policies, {plan.base_inventory.source})")
    table = Table(title=f"Policies to build ({len(plan.policies)})")
    table.add_column("Policy", style="bold")
    table.add_column("Requested")
    table.add_column("Resolved")
    table.add_column("Type")
    table.add_column("From")
    if plan.diff:
        table.add_column("Change")
    status = {r.name: r for r in plan.diff}
    for p in plan.policies:
        row = [p.name, p.requested, p.version or "-", p.kind, p.origin]
        if plan.diff:
            r = status.get(p.name)
            s = r.status if r else ""
            row.append(f"[{STATUS_STYLE.get(s, '')}]{s}[/]" if s else "")
        table.add_row(*row)
    console.print(table)
    removed = [r for r in plan.diff if r.status == "excluded"]
    if removed:
        console.print("[red]Excluded from base:[/] " + ", ".join(f"{r.name} {r.base_version}" for r in removed))
    for p in plan.policies:
        for n in p.notes:
            console.print(f"[yellow]note:[/] {p.name}: {n}")
    if STATE["verbose"]:
        for p in plan.policies:
            console.print(f"[dim]{p.name}: {p.ref}[/]")


def _make_plan(cfg: Config, include_base: Optional[bool], pull: bool = True, upgrade: Optional[str] = None,
               from_image: Optional[str] = None):
    try:
        with _hub() as hub:
            return plan_build(cfg, Resolver(hub), include_base=include_base, pull=pull, upgrade=upgrade,
                              from_image=from_image)
    except (InspectError, DockerError, HubError, ResolveError, BuildError) as exc:
        raise _fail(str(exc))


def _report_errors(plan) -> None:
    if plan.errors:
        err.print(f"[bold red]{len(plan.errors)} problem(s):[/]")
        for e in plan.errors:
            err.print(f"  - {e}")
        raise typer.Exit(1)


@app.command()
def validate(
    config: Path = typer.Option(Path("aigw.yaml"), "--file", "-f", help="Build config."),
    skip_base: bool = typer.Option(False, "--skip-base", help="Do not inspect the base image (no Docker needed)."),
) -> None:
    """Check the config and resolve every policy to an exact version."""
    cfg = _load(config)
    plan = _make_plan(cfg, include_base=False if skip_base else None)
    _print_plan(plan)
    _report_errors(plan)
    console.print("[green]Config is valid.[/]")


UPGRADE_HELP = "Move base-image policies to newer releases: none, patch, minor or latest."
FROM_HELP = ("Take the carried-over policies from this controller image instead of the base image, e.g. an "
             "earlier build. 'last' = the previous build of this config on this machine.")


def _check_level(level: Optional[str]) -> None:
    if level is not None and level not in UPGRADE_LEVELS:
        raise _fail(f"--upgrade/--to must be one of {', '.join(UPGRADE_LEVELS)}")


TAG_HELP = "Tag of the built images (overrides output.tag). Required with --from <image> and no --file."
DEFAULT_CONFIG = Path("aigw.yaml")


def _build_config(config: Optional[Path], from_image: Optional[str], tag: Optional[str]) -> Config:
    """The config to build: --file, or (with --from <image> and no --file) one derived from that image."""
    if tag is not None and not TAG_RE.match(tag):
        raise _fail(f"--tag: {tag!r} is not a valid image tag (letters, digits, _ . -; max 128)")
    if config is None and from_image and from_image != LAST_BUILD:
        if not tag:
            raise _fail(f"--tag is required to rebuild {from_image} without a config, so the new images do not "
                        "overwrite it")
        try:
            cfg = config_from_image(from_image, base_images_of(from_image), Path.cwd())
        except (BuildError, DockerError) as exc:
            raise _fail(str(exc))
        except ConfigError as exc:
            raise _fail("; ".join(exc.errors), 2)
        if tag == cfg.output_tag:
            raise _fail(f"--tag {tag} is the tag of {from_image}; give a new tag")
        console.print(f"[dim]No config given: rebuilding {from_image} with its own policies and base images.[/]")
    else:
        config = config or DEFAULT_CONFIG
        if not config.exists():
            raise _fail(f"config {config} not found. Pass --file <config>, or --from <image> --tag <new tag> to "
                        "rebuild an earlier build as is", 2)
        cfg = _load(config)
    if tag:
        cfg.output.tag = tag
    return cfg


def _run_build(config: Optional[Path], dry_run: bool, push: bool, upgrade: Optional[str], from_image: Optional[str],
               tag: Optional[str] = None) -> None:
    _check_level(upgrade)
    cfg = _build_config(config, from_image, tag)
    if push:
        if is_local_repository(cfg.output.repository):
            raise _fail(f"--push needs output.repository set to a registry; {cfg.output.repository!r} means "
                        "local images only")
        cfg.output.push = True
    try:
        check_platform(cfg)
    except (BuildError, DockerError) as exc:
        raise _fail(str(exc))
    if not dry_run:
        try:
            preflight(cfg, log=lambda m: console.print(f"[dim]{m}[/]"))
        except (BuildError, DockerError) as exc:
            raise _fail(str(exc))
    plan = _make_plan(cfg, include_base=None, upgrade=upgrade, from_image=from_image)
    _print_plan(plan)
    _report_errors(plan)

    if dry_run:
        build_file = write_build_file(plan)
        console.rule("build.yaml")
        console.print(Syntax(plan.build_yaml(), "yaml"))
        console.rule()
        console.print("$ " + " ".join(ap_command(cfg, build_file.parent)))
        console.print(f"Dry run: nothing built. build.yaml saved for review: [bold]{_rel(build_file)}[/]")
        return

    code = run_build(plan, log=lambda m: console.print(f"[dim]{m}[/]"))
    files = cfg.build_files_dir
    if code != 0:
        console.print(f"build.yaml kept for review: [bold]{_rel(files / 'build.yaml')}[/]")
        raise _fail(f"ap gateway image build failed (exit {code})", code)
    where = f"pushed to {cfg.output.repository}" if cfg.output.push else "local images, not pushed"
    console.print(f"[green]Build complete[/] ({where}):")
    for img in plan.output_images():
        console.print(f"  {img}")
    console.print("Build files for review:")
    for name in ("build.yaml", MANIFEST_FILE):
        f = files / name
        console.print(f"  {_rel(f)}" + ("" if f.exists() else "  [yellow](not written by ap)[/]"))
    console.print(f"[dim]Verify with: aigw-builder inspect {plan.output_images()[0]}[/]")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


CONFIG_HELP = "Build config (default: aigw.yaml; not needed with --from <image> --tag <new tag>)."


@app.command()
def build(
    config: Optional[Path] = typer.Option(None, "--file", "-f", help=CONFIG_HELP),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write and print build.yaml and the ap command without building."),
    push: bool = typer.Option(False, "--push", help="Push the images to output.repository (default: local only)."),
    upgrade: Optional[str] = typer.Option(None, "--upgrade", help=UPGRADE_HELP + " Overrides upgrade_base_policies."),
    from_image: Optional[str] = typer.Option(None, "--from", help=FROM_HELP),
    tag: Optional[str] = typer.Option(None, "--tag", help=TAG_HELP),
) -> None:
    """Resolve policies, generate build.yaml and run `ap gateway image build` (local images by default)."""
    _run_build(config, dry_run, push, upgrade, from_image, tag)


@app.command()
def upgrade(
    config: Optional[Path] = typer.Option(None, "--file", "-f", help=CONFIG_HELP),
    to: str = typer.Option("minor", "--to", help="Upgrade level: patch, minor (default, same major) or latest."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and build.yaml without building."),
    push: bool = typer.Option(False, "--push", help="Push the images to output.repository (default: local only)."),
    from_image: Optional[str] = typer.Option(None, "--from", help=FROM_HELP),
    tag: Optional[str] = typer.Option(None, "--tag", help=TAG_HELP),
) -> None:
    """Rebuild with the carried-over policies upgraded to newer releases (like `brew upgrade`).

    Use --from last to upgrade the policies of your previous build rather than those of the base image.
    Without --file, `--from <image> --tag <new tag>` upgrades that earlier build as is: same policies, same
    base images, same name and repository, new tag.
    """
    if to == "none":
        raise _fail("--to must be patch, minor or latest")
    _run_build(config, dry_run, push, to, from_image, tag)


@app.command()
def outdated(
    image: Optional[str] = typer.Argument(None, help="Gateway controller image to check."),
    config: Optional[Path] = typer.Option(None, "--file", "-f", help="Check the policies this config would build."),
    from_image: Optional[str] = typer.Option(None, "--from", help=FROM_HELP),
    show_all: bool = typer.Option(False, "--all", "-a", help="Also list policies that are up to date."),
    pull: bool = typer.Option(True, "--pull/--no-pull", help="Pull the image if it is not available locally."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Compare an image's policies with the newest releases on the Policy Hub (like `brew outdated`)."""
    if bool(image) == bool(config):
        raise _fail("give either an IMAGE or --file <config>")
    if from_image and not config:
        raise _fail("--from needs --file <config>; to check an image on its own, pass it as IMAGE")
    try:
        with _hub() as hub:
            resolver = Resolver(hub)
            if image:
                inv = inspect_image(image, pull=pull)
                title = inv.image
                rows = rows_for_image(resolver, inv)
            else:
                cfg = _load(config)
                plan = plan_build(cfg, resolver, pull=pull, upgrade="none", from_image=from_image)
                _report_errors(plan)
                title = f"{cfg.source.name} ({plan.baseline_image or 'config only'} + config)"
                rows = rows_for_policies(resolver, plan.policies)
    except (InspectError, DockerError, HubError, ResolveError, BuildError) as exc:
        raise _fail(str(exc))

    shown = rows if show_all else [r for r in rows if r.outdated or r.status == "unknown"]
    if as_json:
        console.print_json(json.dumps({"target": title, "policies": [r.to_dict() for r in shown]}))
        return
    n_out = sum(r.outdated for r in rows)
    if not shown:
        console.print(f"[green]All {len(rows)} policies in {title} are up to date.[/]")
        return
    table = Table(title=f"{title}: {n_out} of {len(rows)} policies outdated")
    table.add_column("Policy", style="bold")
    table.add_column("Current")
    table.add_column("Patch")
    table.add_column("Minor")
    table.add_column("Latest")
    table.add_column("Status")
    for r in shown:
        style = {"major": "red", "minor": "yellow", "patch": "cyan", "up to date": "dim", "unknown": "red"}[r.status]
        table.add_row(r.name, r.current or "?", r.patch or "", r.minor or "", r.latest or "",
                      f"[{style}]{r.status}[/]")
    console.print(table)
    for r in shown:
        if r.note:
            console.print(f"[dim]{r.name}: {r.note}[/]")
    if n_out:
        hint = f"-f {config}" if config else "-f <config>"
        if from_image:
            hint += f" --from {from_image}"
        console.print(f"[dim]Upgrade with: aigw-builder upgrade {hint} --to patch|minor|latest "
                      "(minor stays within the current major)[/]")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
