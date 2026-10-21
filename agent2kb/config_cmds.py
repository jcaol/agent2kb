"""``a2kb list``, ``a2kb config show`` and ``a2kb config migrate``.

Read-only except ``migrate --write``, which only appends to a v2 file and never changes
the old file or an S3 prefix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import typer
import yaml
from rich.markup import escape
from rich.table import Table

from agent2kb import config as kb_config
from agent2kb import config_v2
from agent2kb.config import Resolved, default_user_config_path
from agent2kb.config_v2 import ConfigError
from agent2kb.language import describe_output_language

# ─── a2kb list ─────────────────────────────────────────────────────────


def _legacy_role(name: str, data: dict) -> str:
    return "admin" if name == "super" or data.get("role") == "admin" else "member"


def gather_kbs(config_file: Optional[Path]) -> tuple[Path, str, list[dict]]:
    """Every KB visible from here: the user-level file plus this directory's project config.

    Returns ``(user_path, format, rows)``; a row is ``{name, prefix, role, where, note}``.
    """
    path = Path(config_file) if config_file else default_user_config_path()
    rows: list[dict] = []
    fmt = "missing"
    if path.is_file():
        raw = config_v2.read_raw(path)
        if config_v2.is_v2(raw):
            fmt = "v2"
            cfg = config_v2.parse_v2(raw, path)
            for name in cfg.kbs:
                try:
                    data, _ = config_v2.profile_data(cfg, name)
                    rows.append(
                        {
                            "name": name,
                            "prefix": data["prefix"] or "(whole bucket)",
                            "role": data["role"],
                            "where": "config file",
                            "note": "",
                        }
                    )
                except ConfigError as e:
                    rows.append(
                        {
                            "name": name,
                            "prefix": "?",
                            "role": "?",
                            "where": "config file",
                            "note": f"invalid: {e}",
                        }
                    )
            legacy = cfg.legacy_profiles
        else:
            fmt = "old (profiles:)"
            legacy = raw.get("profiles") or {}
        for name, data in legacy.items():
            data = data if isinstance(data, dict) else {}
            rows.append(
                {
                    "name": name,
                    "prefix": data.get("prefix") or "(whole bucket)",
                    "role": _legacy_role(name, data),
                    "where": "old profile",
                    "note": "",
                }
            )

    project = kb_config._find_project_config()
    if project:
        try:
            data = yaml.safe_load(project.read_text()) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        if isinstance(data, dict) and config_v2.is_pointer(data):
            target = data.get(config_v2.POINTER_KEY)
            hit = next((r for r in rows if r["name"] == target), None)
            if hit:
                hit["note"] = "selected by this folder"
            else:
                rows.append(
                    {
                        "name": str(target),
                        "prefix": "?",
                        "role": "?",
                        "where": f"pointer {project}",
                        "note": "not defined in the config file",
                    }
                )
        elif isinstance(data, dict) and data:
            name = data.get("name") or "?"
            rows.append(
                {
                    "name": str(name),
                    "prefix": data.get("prefix") or "?",
                    "role": "member",
                    "where": "old project config",
                    "note": f"this directory: {project}",
                }
            )
    return path, fmt, rows


def show_kbs(config_file: Optional[Path], console, *, as_json: bool = False) -> None:
    path, fmt, rows = gather_kbs(config_file)
    if as_json:
        typer.echo(
            json.dumps(
                {"config_file": str(path), "format": fmt, "kbs": rows}, indent=2, ensure_ascii=False
            )
        )
        return
    console.print(f"[dim]Config file: {escape(str(path))} ({fmt})[/dim]")
    if not rows:
        console.print(
            "No knowledge bases found. Run [bold]a2kb setup[/bold] to create your first one."
        )
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Name", "Prefix", "Role", "Defined in", "Note"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            escape(r["name"]),
            escape(str(r["prefix"])),
            r["role"],
            escape(r["where"]),
            escape(r["note"]),
        )
    console.print(table)


# ─── a2kb config show ──────────────────────────────────────────────────

_GROUPS: list[tuple[str, list[str]]] = [
    ("KB", ["name", "role", "prefix"]),
    (
        "Storage",
        [
            "bucket",
            "s3_endpoint",
            "region",
            "addressing_style",
            "checksum_mode",
            "access_key",
            "secret_key",
            "aws_profile",
        ],
    ),
    (
        "LLM",
        [
            "compile_provider",
            "default_model",
            "cli_command",
            "cli_reasoning_effort",
            "aux_model",
            "aux_effort",
            "cli_fallback_command",
            "cli_fallback_model",
            "cli_fallback_effort",
            "ollama_host",
            "groq_api_key",
            "openrouter_api_key",
            "gemini_api_key",
            "openai_api_key",
            "voyage_api_key",
            "embedding_provider",
            "embedding_model",
            "rerank_model",
            "rerank_enabled",
            "concept_threshold",
        ],
    ),
    (
        "Language and privacy",
        [
            "output_language",
            "privacy_mode",
            "allow_jina_reader",
            "allow_cloud_vision",
            "google_safe_browsing_key",
        ],
    ),
]


def effective_rows(resolved: Resolved) -> list[tuple[str, str, object, str]]:
    """``(group, field, masked value, source)`` for every set field of a resolved profile."""
    values = resolved.profile.model_dump(mode="json")
    seen: set[str] = set()
    rows: list[tuple[str, str, object, str]] = []
    groups = _GROUPS + [("Other", sorted(set(values) - {f for _, fs in _GROUPS for f in fs}))]
    for group, fields in groups:
        for f in fields:
            seen.add(f)
            v = values.get(f)
            if f == "output_language":
                v = describe_output_language(v)
            elif v in (None, ""):
                continue
            rows.append((group, f, config_v2.mask(f, v), resolved.sources.get(f, "default")))
    return rows


def show_config(resolved: Resolved, selected_by: str, console, *, as_json: bool = False) -> None:
    rows = effective_rows(resolved)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "profile": resolved.profile.name,
                    "selected_by": selected_by,
                    "user_config": str(resolved.user_path),
                    "project_config": str(resolved.project_path) if resolved.project_path else None,
                    "settings": {f: {"value": v, "source": src} for _, f, v, src in rows},
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    console.print(
        f"[bold]{escape(resolved.profile.name)}[/bold]  "
        f"[dim](selected by {escape(str(selected_by))})[/dim]"
    )
    missing = "" if resolved.user_path and resolved.user_path.is_file() else " (missing)"
    console.print(f"[dim]user config:    {escape(str(resolved.user_path))}{missing}[/dim]")
    console.print(f"[dim]project config: {escape(str(resolved.project_path or '(none)'))}[/dim]")
    console.print("[dim]Secrets are masked; a2kb never prints them.[/dim]")
    group = None
    table: Optional[Table] = None
    for g, f, v, src in rows:
        if g != group:
            if table is not None:
                console.print(table)
            group = g
            console.print(f"\n[bold]{g}[/bold]")
            table = Table(show_header=False, box=None, pad_edge=False)
            table.add_column(style="cyan", no_wrap=True)
            table.add_column()
            table.add_column(style="dim")
        assert table is not None
        # the files are listed above; keep the column short enough not to wrap
        table.add_row(f, escape(str(v)), escape(src.split(" (")[0]))
    if table is not None:
        console.print(table)


# ─── a2kb config migrate ───────────────────────────────────────────────


def run_migrate(
    profile_name: str,
    *,
    config_file: Optional[Path],
    as_name: Optional[str],
    to: Optional[Path],
    write: bool,
    yes: bool,
    console,
    confirm: Callable[[str], bool],
) -> None:
    """Print (or with ``write`` append) the v2 equivalent of an old profile."""
    user_path = Path(config_file) if config_file else default_user_config_path()
    if user_path.is_file():
        v2 = config_v2.load_v2(user_path)
        if v2 is not None and profile_name in v2.kbs:
            raise ConfigError(f"'{profile_name}' is already a v2 KB in {user_path}.")

    resolved = kb_config.resolve_profile_detailed(profile_name, config_file, None, use_env=False)
    old = resolved.profile
    new_name = as_name or profile_name
    if old.is_super and not as_name:
        raise ConfigError(
            "'super' is a reserved name. Choose the name for the admin KB with "
            "`--as <name>`; it will get `role: admin`."
        )
    config_v2.validate_kb_name(new_name, strict=True)

    target = Path(to) if to else user_path
    existing: Optional[dict] = None
    if target.is_file():
        existing = config_v2.read_raw(target)
        if config_v2.is_legacy_only(existing):
            raise ConfigError(
                f"{target} is an old-format config and will not be modified. Write the v2 "
                "KB to another file: `a2kb config migrate --profile ... --to <path> --write` "
                "(then use it with KB_CONFIG=<path>)."
            )
        cfg = config_v2.parse_v2(existing, target)
        if new_name in cfg.kbs or new_name in cfg.legacy_profiles:
            raise ConfigError(f"'{new_name}' already exists in {target}. Use --as <other name>.")
    entry, storage_block = config_v2.migration_entry(old, new_name, existing)

    shown: dict = {}
    if storage_block is not None:
        shown["version"] = config_v2.V2_VERSION
        shown["storage"] = storage_block
    shown["kbs"] = {new_name: entry}
    console.print(
        f"[bold]v2 equivalent of '{escape(profile_name)}'[/bold] "
        f"[dim](secrets masked; target: {escape(str(target))})[/dim]\n"
    )
    console.print(
        escape(yaml.safe_dump(config_v2.mask_dict(shown), sort_keys=False, allow_unicode=True))
    )
    source = resolved.project_path or resolved.user_path
    console.print(
        f"[dim]S3 prefix stays '{escape(old.prefix)}' — no objects are moved or renamed.\n"
        f"The old config ({escape(str(source))}) is not changed.[/dim]"
    )
    if not write:
        console.print("\n[cyan]●[/cyan] dry-run — add --write to append this to the v2 file.")
        return

    if old.is_super:
        console.print(
            "\n[bold red]Warning:[/bold red] this creates an ADMIN KB: it can read, write "
            "and delete everything in the bucket, including other KBs and _meta/."
        )
    if not yes and not confirm(f"Append '{new_name}' to {target}?"):
        console.print("Nothing written.")
        raise typer.Exit(1)
    if old.is_super and not confirm("Really create an admin KB with whole-bucket access?"):
        console.print("Nothing written.")
        raise typer.Exit(1)
    raw = existing if existing is not None else config_v2.new_raw()
    if storage_block is not None:
        raw["storage"] = storage_block
    out = config_v2.with_kb(raw, new_name, entry)
    config_v2.write_v2(target, out)
    console.print(f"[green]✓[/green] Added KB '{escape(new_name)}' to {escape(str(target))}")
    console.print(
        f"[dim]Try it: a2kb --config {escape(str(target))} -p {escape(new_name)} status. "
        "The old setup keeps working; "
        "switching a project folder to a `kb: <name>` pointer is a separate, manual step. "
        "Keep the old .kb/config.yaml if another tool reads keys (e.g. groq_api_key) from it."
        " Note: while that folder still has the old file, `a2kb` run there refuses to use the "
        "migrated KB (a v2 KB takes only a `kb: <name>` pointer in a project folder); use "
        "`--config` / `$KB_CONFIG` from elsewhere until you switch the folder."
        "[/dim]"
    )
