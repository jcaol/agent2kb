"""Main entry point for agent2kb."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn, Optional

import click
import typer
import yaml
from loguru import logger
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from typer.core import TyperGroup

from agent2kb.config import (
    ConfigError,
    _find_project_config,
    autodetect_profile,
    load_config,
    resolve_profile,
    resolve_profile_detailed,
)
from agent2kb.errors import EXIT_CONFIG_ERROR, EXIT_FRIENDLY_ERROR, translate
from agent2kb.extras import MissingExtraError
from agent2kb.models import LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client

console = Console()
err_console = Console(stderr=True)


#: Order of the command groups and of the commands inside each in `a2kb --help`.
_HELP_ORDER = (
    "setup", "doctor", "new", "list", "config", "connect", "init",
    "ingest", "compile", "query", "status",
    "mcp",
    "lint", "stubs", "embed", "promote", "clip", "rm", "backfill-source", "manifest",
    "outputs", "scaffold",
    "backup", "restore",
    "bootstrap",
)


_LIST_OR_CODE_LINE = re.compile(r"\s|[-*$]|\d+[.)]\s|a2kb\s")


def reflow_help(text: Optional[str]) -> Optional[str]:
    """Join the hard-wrapped lines of each prose paragraph in a help text.

    Docstrings are wrapped at source width, and Typer keeps those line breaks, so on a wider
    terminal the help text breaks mid-sentence. Paragraphs that contain an indented, bulleted,
    numbered or command line (examples, mode tables) are left exactly as written.
    """
    if not text:
        return text
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        if len(lines) > 1 and not any(_LIST_OR_CODE_LINE.match(line) for line in lines):
            block = " ".join(line.strip() for line in lines)
        blocks.append(block)
    return "\n\n".join(blocks)


def _reflow_tree(group) -> None:
    for command in group.commands.values():
        command.help = reflow_help(command.help)
        if getattr(command, "commands", None):
            _reflow_tree(command)


def _is_help_request(args: list[str]) -> bool:
    """True when the invocation asks for ``--help``/``-h`` (nothing after a bare ``--`` counts)."""
    for arg in args:
        if arg == "--":
            return False
        if arg in ("--help", "-h"):
            return True
    return False


class _KBGroup(TyperGroup):
    """Top-level group that turns known S3 failures into one-line errors.

    Wrapping ``invoke`` covers every subcommand (and the profile-resolving
    callback) without touching each handler, and works for both the installed
    ``a2kb`` script and ``CliRunner``. Unrecognised exceptions, and everything
    under ``-v/--verbose``, propagate unchanged for the full traceback.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _reflow_tree(self)

    def list_commands(self, ctx: click.Context) -> list[str]:
        rank = {name: i for i, name in enumerate(_HELP_ORDER)}
        names = list(self.commands)
        return sorted(names, key=lambda n: (rank.get(n, len(rank)), names.index(n)))

    def invoke(self, ctx: click.Context):
        # The profile callback runs before the subcommand parses its own flags, so
        # note up front whether the caller wants JSON and whether -v was given.
        rest = [*getattr(ctx, "_protected_args", []), *ctx.args]
        _state["json_output"] = "--json" in rest
        _state["help_requested"] = _is_help_request(rest)
        _state["verbose"] = bool(ctx.params.get("verbose"))
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.Abort, click.exceptions.Exit):
            raise
        except MissingExtraError as e:
            if ctx.params.get("verbose"):
                raise
            err_console.print(f"[red]Error:[/red] {escape(str(e))}")
            raise typer.Exit(EXIT_FRIENDLY_ERROR) from None
        except Exception as e:
            friendly = None if ctx.params.get("verbose") else translate(e, _state.get("profile"))
            if friendly is None:
                raise
            err_console.print(f"[red]Error:[/red] {escape(friendly.message)}")
            err_console.print(f"[yellow]Hint:[/yellow]  {escape(friendly.hint)}")
            err_console.print("[dim](run with -v for the full traceback)[/dim]")
            raise typer.Exit(EXIT_FRIENDLY_ERROR) from None


def _fail_config(message: str, hint: str = "") -> NoReturn:
    """Report a config problem as one line (plus hint) and exit 2, without a Usage block.

    With ``--json`` the same information goes to stdout as a JSON object so a script
    parsing the output still gets valid JSON.
    """
    err_console.print(f"[red]Error:[/red] {escape(message)}")
    if hint:
        err_console.print(f"[yellow]Hint:[/yellow]  {escape(hint)}")
    err_console.print("[dim](run with -v for the full traceback)[/dim]")
    if _state.get("json_output"):
        typer.echo(json.dumps({"ok": False, "error": message, "hint": hint}, ensure_ascii=False))
    raise typer.Exit(EXIT_CONFIG_ERROR)


app = typer.Typer(
    name="a2kb",
    cls=_KBGroup,
    help="agent2kb — a headless knowledge compiler for agent teams.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["--help", "-h"]},
    # Default tracebacks dump every local variable, which includes S3 keys.
    pretty_exceptions_show_locals=False,
)

# Global state passed between callback and commands
_state: dict = {}


def _version_callback(value: bool) -> None:
    """Eager --version handler: print and exit before profile resolution.

    is_eager=True means this runs before the callback requires a profile,
    so `a2kb --version` works without any config — handy for verifying a
    deploy landed the right build.
    """
    if value:
        from agent2kb import __version__

        console.print(f"agent2kb {__version__}")
        raise typer.Exit()


def get_current_profile() -> ProfileConfig:
    """Get the current profile from global state."""
    if "profile" not in _state:
        raise typer.BadParameter("--profile is required for all commands.")
    return _state["profile"]


_VALID_ACTORS = ("user", "agent")


def _resolve_actor(explicit: Optional[str]) -> str:
    """Decide who 'asked' the current ``a2kb query`` invocation.

    Resolution order:
      1. ``--actor X`` flag on the command line.
      2. ``KB_ACTOR`` environment variable (``user`` or ``agent``).
      3. ``sys.stdin.isatty()`` — a human at a TTY → ``user``; a piped /
         redirected stdin → ``agent``.

    The TTY heuristic is the important one for chat-bot / cron callers
    that run ``a2kb query "..."`` without thinking about flags. Their
    stdin is wired to a pipe (or /dev/null) so isatty() returns False
    and we default to ``agent`` — which means ``--save`` is off and
    the auto-enrichment query does NOT pollute ``outputs/``.

    Generic mechanism: no caller-specific code paths. Any tool can
    set ``KB_ACTOR=agent`` to make its intent explicit; ``isatty()``
    catches the common case where it forgot.
    """
    if explicit:
        if explicit not in _VALID_ACTORS:
            raise ValueError(
                f"--actor must be one of {list(_VALID_ACTORS)}, got {explicit!r}"
            )
        return explicit
    env = os.environ.get("KB_ACTOR", "").strip().lower()
    if env in _VALID_ACTORS:
        return env
    if env:
        # Surface garbage env values so operators see why their override
        # didn't take effect, but don't fail — fall through to autodetect.
        logger.warning(
            f"KB_ACTOR={env!r} is not one of {list(_VALID_ACTORS)} — ignoring."
        )
    return "user" if sys.stdin.isatty() else "agent"


def get_s3() -> S3Client:
    """Get an S3 client for the current profile."""
    return S3Client(get_current_profile())


#: Commands that work without (or before) a profile: they create or inspect config.
_NO_PROFILE_COMMANDS = {"setup", "new", "list", "config", "connect", "bootstrap"}


@app.callback()
def main(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        "-p",
        help=(
            "Profile (KB) name. If omitted, agent2kb autodetects from $KB_PROFILE "
            "or the nearest .kb/config.yaml's 'kb:' (or old-style 'name:') field. Pass "
            "--profile <name> explicitly to use an admin KB (role: admin) or the legacy `super` profile."
        ),
    ),
    config_file: Optional[Path] = typer.Option(
        None, "--config", help="Config file path (default: $KB_CONFIG, else ~/.kb/config.yaml)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True,
        help="Show the agent2kb version and exit.",
    ),
    # CLI credential overrides (highest priority)
    s3_endpoint: Optional[str] = typer.Option(None, "--s3-endpoint", help="S3 endpoint URL"),
    access_key: Optional[str] = typer.Option(None, "--access-key", help="S3 access key"),
    secret_key: Optional[str] = typer.Option(None, "--secret-key", help="S3 secret key"),
    bucket: Optional[str] = typer.Option(None, "--bucket", help="S3 bucket name"),
    prefix: Optional[str] = typer.Option(None, "--prefix", help="S3 key prefix"),
):
    """a2kb — profile is autodetected when not passed explicitly.

    Profile resolution:
      1. ``--profile X`` flag (highest priority, also the only way to
         reach the ``super`` profile).
      2. ``$KB_PROFILE`` environment variable.
      3. ``kb:`` (pointer) or ``name:`` (old format) field in the nearest
         ``.kb/config.yaml`` (walks up from cwd).

    Per-field credential resolution (orthogonal to profile name):
      1. CLI flags (``--s3-endpoint``, ``--access-key``, etc.)
      2. ENV vars (``KB_S3_ENDPOINT``, ``KB_ACCESS_KEY``, etc.)
      3. Project config (``.kb/config.yaml`` in current or parent dir)
      4. User config (``~/.kb/config.yaml``)
    """
    logger.remove()
    if verbose:
        # stderr, so `a2kb -v <cmd> --json` keeps stdout machine-readable.
        logger.add(lambda msg: err_console.print(msg, style="dim", markup=False), level="DEBUG")

    # Build CLI overrides dict (only non-None values)
    cli_overrides = {
        "s3_endpoint": s3_endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "prefix": prefix,
    }
    _state["config_file"] = config_file
    _state["requested_profile"] = profile
    _state["cli_overrides"] = cli_overrides

    if ctx.invoked_subcommand in _NO_PROFILE_COMMANDS:
        _state.pop("profile", None)
        return

    # `a2kb <cmd> --help` only prints usage; it must work before any KB is configured.
    if ctx.resilient_parsing or _state.get("help_requested"):
        _state.pop("profile", None)
        return

    # Autodetect profile when --profile is omitted. We never let
    # autodetection produce 'super' (or an admin KB) — elevating always requires
    # an explicit --profile <name>.
    if profile is None:
        try:
            detected = autodetect_profile(config_path=config_file)
        except ConfigError as e:
            if verbose:
                raise
            head, _, rest = str(e).partition("\n")
            _fail_config(head, " ".join(rest.split()) or "Check your config files (`a2kb doctor`).")
        if not detected:
            _offer_first_setup(ctx, config_file)
            _fail_config(
                "No profile specified and none could be autodetected.",
                "Pass --profile <name>, set KB_PROFILE, or run from a directory whose "
                ".kb/config.yaml has a 'kb:' (or old-style 'name:') field. "
                "`a2kb list` shows your knowledge bases, `a2kb setup` creates one.",
            )
        profile = detected
        logger.debug(f"Profile autodetected: {profile}")

    try:
        _state["profile"] = resolve_profile(profile, config_file, cli_overrides)
        _state["config"] = load_config(config_file)
    except ValidationError as e:
        if verbose:
            raise
        problems = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'config'}: {err['msg']}"
            for err in e.errors()
        )
        _fail_config(
            f"Profile '{profile}' has an invalid configuration: {problems}",
            "Fix those fields in your config (see docs/storage-providers.md).",
        )
    except (PermissionError, ValueError, yaml.YAMLError) as e:
        if verbose:
            raise
        # resolve_profile's "not found" text is a headline plus a list of places looked at.
        head, _, rest = str(e).partition("\n")
        _fail_config(head, " ".join(rest.split()) or "Check your config files (`a2kb doctor`).")
    logger.info(f"Using profile: {profile} (prefix: {_state['profile'].prefix})")


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_setup_command(config_file: Optional[Path]) -> int:
    # A separate process: invoking the setup command from inside this one makes newer
    # Typer/Click versions re-parse the original arguments (`doctor` and so on). Not
    # `python -m`, which puts the current directory first on sys.path, so a stray
    # `agent2kb/` folder where the user happens to be would be imported instead.
    import subprocess

    bootstrap = (
        "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
        "from agent2kb.main import app; app(prog_name='a2kb')"
    )
    args = [sys.executable, "-c", bootstrap]
    if config_file:
        args += ["--config", str(config_file)]
    return subprocess.call(args + ["setup"])


def _offer_first_setup(ctx: typer.Context, config_file: Optional[Path]) -> None:
    """Offer to run `a2kb setup` when a person runs a command before any KB exists.

    Only when there is no user-level config at all and both stdin and stdout are a terminal:
    cron jobs, agents and MCP clients keep the usual exit 2 and message. Returns when the
    offer does not apply or is declined; exits after setup has run.
    """
    path = Path(config_file).expanduser() if config_file else kb_config_default()
    if path.exists() or not _interactive_terminal():
        return
    console.print("[yellow]![/yellow] No knowledge base is set up yet.")
    if not typer.confirm("Run `a2kb setup` now?", default=True):
        return
    code = _run_setup_command(config_file)
    if code != 0:
        raise typer.Exit(code)
    console.print(
        f"\nSetup finished. Run your command again: [bold]a2kb {escape(ctx.invoked_subcommand or '')}"
        " ...[/bold]"
    )
    raise typer.Exit(0)


#: LLM providers that keep compile content fully on-machine.
_LOCAL_COMPILE_PROVIDERS = {LLMProvider.OLLAMA}


def _print_privacy_banner(
    profile: ProfileConfig,
    context: str,
    effective_provider: Optional[LLMProvider] = None,
) -> None:
    """Print a one-line provider/privacy summary before long operations.

    Makes it obvious to the user that their content is going through a given
    LLM provider and which cloud extractors are active for this profile.

    When ``effective_provider`` is supplied (e.g. after a ``--llm`` override)
    it is shown instead of the profile default, so the banner never lies
    about where the request is actually going.
    """
    provider = effective_provider or profile.compile_provider
    provider_label = provider.value
    if provider_label == "cli":
        provider_label = f"cli({profile.cli_command} {profile.default_model})"
    elif profile.default_model and provider != profile.compile_provider:
        # Override in play — keep the model suffix explicit so users notice.
        provider_label = f"{provider_label}(override)"
    elif profile.default_model:
        provider_label = f"{provider_label}({profile.default_model})"

    if profile.privacy_mode:
        extras = "privacy_mode=on (no Jina Reader, no cloud vision or CLI image OCR)"
    else:
        parts = []
        parts.append("Jina Reader " + ("on" if profile.allow_jina_reader else "off"))
        # Image OCR through the claude/codex CLI is cloud vision too: same switch.
        parts.append(
            "cloud vision (incl. CLI image OCR) " + ("on" if profile.allow_cloud_vision else "off")
        )
        extras = ", ".join(parts)

    from urllib.parse import urlparse

    host = "AWS S3"
    if profile.s3_endpoint:
        host = urlparse(profile.s3_endpoint).netloc or profile.s3_endpoint
    project_cfg = getattr(profile, "_project_source", None)
    source = f" | config: {project_cfg}" if project_cfg else ""
    console.print(
        f"[dim]• {context} with [bold]{escape(provider_label)}[/bold] "
        f"| bucket: {escape(profile.bucket)} @ {escape(host)} "
        f"| prefix: {escape(profile.prefix or '(whole bucket)')} | {extras}{escape(source)}[/dim]"
    )

    # Loud warning: privacy_mode advertises ingest-only privacy, but users
    # reasonably expect it to cover compile too. Call this out explicitly
    # so nobody is surprised that their compile content left the machine.
    if profile.privacy_mode and provider not in _LOCAL_COMPILE_PROVIDERS:
        console.print(
            f"[yellow]⚠ privacy_mode is ON but compile provider "
            f"'{provider.value}' is not local — compile content will still "
            f"be sent to that provider. privacy_mode only covers ingest "
            f"(Jina Reader, cloud vision).[/yellow]"
        )


# ─── INGEST ────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Daily use")
def ingest(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="URL to ingest"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Local file to ingest"),
    folder: Optional[Path] = typer.Option(None, "--folder", "-d", help="Local folder to ingest"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into subfolders"),
    include_hidden: bool = typer.Option(
        False, "--include-hidden",
        help="With --folder: also ingest hidden files and folders (names starting with '.'). "
        "Credential and runtime folders (.git, .ssh, .aws, .kb, .obsidian, node_modules, ...), "
        "credential files and symlinks are skipped regardless.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="With --folder --recursive: skip the file-list confirmation shown at a terminal.",
    ),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated tags"),
    actor: Optional[str] = typer.Option(None, "--actor", help="Actor: user|agent (auto-resolved if omitted)"),
    skip_safety: bool = typer.Option(
        False, "--skip-safety",
        help="Warning: skips the pre-ingest URL safety check (phishing and malware lookups). "
        "Use only for URLs that you already trust. The SSRF guard still applies.",
    ),
):
    """Ingest content from URLs, files, or folders into raw/."""
    from agent2kb.ingest.base import IngestPipeline

    profile = get_current_profile()
    s3 = get_s3()
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    resolved_actor = _resolve_actor(actor)
    _print_privacy_banner(profile, "Ingesting")
    pipeline = IngestPipeline(profile=profile, s3=s3, tags=tag_list)

    def _log_ingest_failure(source: str, error: str) -> None:
        """Append ingest failure to log.md so the user can follow up."""
        import datetime as _dt
        log_key = f"{profile.prefix}wiki/log.md"
        try:
            existing = s3.download(log_key)
        except Exception:
            existing = ""
        entry = (
            f"\n### {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- ✗ Ingest failed: `{source}`\n"
            f"  - Error: {error}\n"
        )
        # Prepend after frontmatter header
        if "---" in existing:
            parts = existing.split("---", 2)
            if len(parts) >= 3:
                header = f"---{parts[1]}---"
                body = parts[2]
                existing = header + "\n" + entry + body
            else:
                existing += entry
        else:
            existing += entry
        try:
            s3.upload(log_key, existing)
        except Exception:
            pass

    if url:
        result = pipeline.ingest_url(url, actor=resolved_actor, skip_safety=skip_safety)
        if result.success:
            console.print(f"[green]✓[/green] Ingested URL → {escape(result.raw_key)}")
        else:
            console.print(f"[red]✗[/red] Failed: {escape(result.error or 'unknown')}")
            _log_ingest_failure(url, result.error or "unknown")
            raise typer.Exit(1)

    elif file:
        if not file.exists():
            console.print(f"[red]✗[/red] File not found: {escape(str(file))}")
            raise typer.Exit(1)
        result = pipeline.ingest_file(file)
        if result.success:
            console.print(f"[green]✓[/green] Ingested file → {escape(result.raw_key)}")
        else:
            console.print(f"[red]✗[/red] Failed: {escape(result.error or 'unknown')}")
            _log_ingest_failure(str(file), result.error or "unknown")
            raise typer.Exit(1)

    elif folder:
        if not folder.is_dir():
            console.print(f"[red]✗[/red] Not a directory: {escape(str(folder))}")
            raise typer.Exit(1)
        from agent2kb.ingest.base import select_folder_files

        selection = select_folder_files(folder, recursive=recursive, include_hidden=include_hidden)
        if selection.skipped:
            reasons = sorted({reason for _, reason in selection.skipped})
            console.print(
                f"[dim]Skipping {len(selection.skipped)} path(s): {escape(', '.join(reasons))}. "
                "Use --file for a single file you mean to ingest.[/dim]"
            )
        # A recursive ingest of a broad tree is where surprises happen: a person at a
        # terminal sees the list first. Agents, cron and pipes are never prompted.
        if (
            recursive and not yes and resolved_actor == "user" and _stdin_is_terminal()
            and selection.files
        ):
            shown = 20
            for fp in selection.files[:shown]:
                console.print(f"  {escape(fp.relative_to(folder).as_posix())}")
            if len(selection.files) > shown:
                console.print(f"  [dim]... and {len(selection.files) - shown} more[/dim]")
            try:
                answer = input(f"Ingest these {len(selection.files)} file(s)? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                console.print("[dim]Aborted — nothing ingested.[/dim]")
                return
        # Ingest exactly what was listed and confirmed, not a fresh selection.
        results = pipeline.ingest_folder(
            folder, recursive=recursive, include_hidden=include_hidden, selection=selection
        )
        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        msg = f"[green]✓[/green] Ingested {ok} files"
        if fail:
            msg += f", [red]{fail} failed[/red]"
        console.print(msg)
        for r in results:
            if not r.success:
                console.print(f"  [red]✗[/red] {escape(r.source)}: {escape(r.error or 'unknown')}")
        # Every file was attempted above; a non-zero exit tells cron/agents some failed.
        if fail:
            raise typer.Exit(1)

    else:
        console.print("[yellow]Provide --url, --file, or --folder[/yellow]")
        raise typer.Exit(1)


# ─── COMPILE ───────────────────────────────────────────────────────────


#: Exit status of ``compile --strict`` when a KB was skipped because another compile of
#: it holds the lease (EX_TEMPFAIL from sysexits.h: try again later). Without --strict a
#: skipped compile exits 0, like a compile with nothing to do.
COMPILE_LOCKED_EXIT = 75


@app.command(rich_help_panel="Daily use")
def compile(
    force: bool = typer.Option(False, "--force", "-f", help="Recompile all files (even unchanged)"),
    llm: Optional[str] = typer.Option(None, "--llm", help="LLM provider override"),
    all_profiles: bool = typer.Option(
        False, "--all-profiles", help="Compile every knowledge base (KB). Requires an admin KB (role: admin) or the legacy `super` profile."
    ),
    allow_cloud_fallback: bool = typer.Option(
        False,
        "--allow-cloud-fallback",
        help="Allow fallback to cloud LLM if local provider fails (privacy risk)",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        envvar="KB_COMPILE_STRICT",
        help="Exit with status 1 if any attempted file failed or was quarantined, "
        f"and {COMPILE_LOCKED_EXIT} if a KB was skipped because another compile of it "
        "is running (for cron and CI). Also set by KB_COMPILE_STRICT=1.",
    ),
):
    """Compile raw/ files into structured wiki/ pages using an LLM.

    By default only compiles new/modified files (incremental).
    Use --force to recompile everything. One compile of a KB runs at a time: a
    second one finds the first one's lease (<prefix>_ops/compile.lock) and skips.
    """
    new_only = not force
    profile = get_current_profile()

    if all_profiles:
        if not profile.is_super:
            console.print(
                "[red]✗[/red] --all-profiles requires an admin KB (role: admin) "
                "or the legacy `super` profile."
            )
            raise typer.Exit(1)
        config = _state["config"]
        failed_kbs: list[str] = []
        skipped_kbs: list[str] = []
        for name, prof in config.profiles.items():
            if prof.is_super:
                continue
            console.print(f"\n[bold]Compiling profile: {escape(name)}[/bold]")
            s3 = S3Client(prof)
            lease = _acquire_compile_lease(s3, prof)
            if lease is None:
                skipped_kbs.append(name)
                continue
            with lease:
                if _compile_one_of_many(
                    name, prof, s3, llm, new_only, allow_cloud_fallback
                ):
                    failed_kbs.append(name)
        if strict and failed_kbs:
            console.print(
                f"[red]✗[/red] --strict: compile failures in {len(failed_kbs)} KB(s): "
                f"{escape(', '.join(failed_kbs))}"
            )
            raise typer.Exit(1)
        if strict and skipped_kbs:
            err_console.print(
                f"[yellow]![/yellow] --strict: {len(skipped_kbs)} KB(s) skipped because "
                f"another compile is running: {escape(', '.join(skipped_kbs))}"
            )
            raise typer.Exit(COMPILE_LOCKED_EXIT)
        return

    s3 = get_s3()
    lease = _acquire_compile_lease(s3, profile)
    if lease is None:
        if strict:
            raise typer.Exit(COMPILE_LOCKED_EXIT)
        return
    with lease:
        _compile_current(profile, s3, llm, new_only, allow_cloud_fallback, strict)


def _acquire_compile_lease(s3, prof):
    """The KB's compile lease, or None (after a message on stderr) when another compile
    of the KB is running."""
    from agent2kb.compile import lock as _lock

    try:
        lease = _lock.acquire(s3, prof)
    except _lock.CompileLocked as held:
        info = held.info
        who = ", ".join(
            f"{k} {info[k]}" for k in ("host", "pid", "started", "expires") if info.get(k)
        )
        err_console.print(
            f"[yellow]![/yellow] Another compile of KB '{escape(prof.name)}' is running"
            f"{escape(f' ({who})') if who else ''}; skipping this one. Its lease is "
            f"{escape(_lock.lock_key(prof))}; a lease that is not renewed expires after "
            f"{_lock.TTL_SEC // 60} minutes."
        )
        return None
    if lease.took_over:
        stale = lease.took_over
        err_console.print(
            f"[dim]Took over a stale compile lease of KB '{escape(prof.name)}' "
            f"(host {escape(str(stale.get('host', '?')))}, pid "
            f"{escape(str(stale.get('pid', '?')))}, expired "
            f"{escape(str(stale.get('expires', '?')))}).[/dim]"
        )
    return lease


def _compile_one_of_many(name, prof, s3, llm, new_only, allow_cloud_fallback) -> bool:
    """One KB of ``compile --all-profiles``. True when some file failed."""
    from agent2kb.compile.compiler import Compiler
    from agent2kb.ops import write_compile_run

    provider = LLMProvider(llm) if llm else prof.compile_provider
    # Banner per sub-profile — --all-profiles can span mixed providers
    # so each profile deserves its own posture line.
    _print_privacy_banner(prof, f"Compiling {name}", effective_provider=provider)
    compiler = Compiler(
        profile=prof,
        s3=s3,
        provider=provider,
        allow_cloud_fallback=allow_cloud_fallback,
    )
    results = compiler.compile_all(new_only=new_only)
    ok = sum(1 for r in results if r.success)
    msg = f"  [green]✓[/green] Compiled {ok}/{len(results)} files"
    failed = ok < len(results)
    if failed:
        msg += f", [red]{_failure_summary(results)}[/red]"
    console.print(msg)
    write_compile_run(
        s3,
        prof,
        results,
        provider=provider.value,
        model=getattr(compiler, "model", None),
        new_only=new_only,
        allow_cloud_fallback=allow_cloud_fallback,
    )
    return failed


def _compile_current(profile, s3, llm, new_only, allow_cloud_fallback, strict) -> None:
    """``a2kb compile`` for the current KB, with its lease held."""
    from agent2kb.compile.compiler import Compiler
    from agent2kb.ops import classify_error, write_compile_run

    provider = LLMProvider(llm) if llm else profile.compile_provider
    _print_privacy_banner(profile, "Compiling", effective_provider=provider)
    compiler = Compiler(
        profile=profile,
        s3=s3,
        provider=provider,
        allow_cloud_fallback=allow_cloud_fallback,
    )

    with console.status("[bold green]Compiling..."):
        results = compiler.compile_all(new_only=new_only)

    ok = sum(1 for r in results if r.success)
    fail = len(results) - ok
    total_dur = sum(r.duration_sec for r in results)
    total_tok = sum(r.tokens_in + r.tokens_out for r in results)
    msg = f"[green]✓[/green] Compiled {ok} files"
    if fail:
        msg += f", [red]{_failure_summary(results)}[/red]"
    msg += f" ({total_dur:.0f}s, {total_tok:,} tokens)"
    console.print(msg)
    for r in results:
        if r.success:
            fn = r.source_key.split("/")[-1][:50]
            console.print(
                f"  [dim]{escape(fn)} → {r.duration_sec:.0f}s, {r.tokens_in}+{r.tokens_out}tok[/dim]"
            )
        else:
            cls = classify_error(r.error or "")
            console.print(
                f"  [red]✗[/red] {escape(r.source_key)}: {escape(f'[{cls}]')} {escape(str(r.error))}"
            )

    # Persist a structured run summary for cron/dashboard observability.
    # Best-effort: write_compile_run never raises.
    write_compile_run(
        s3,
        profile,
        results,
        provider=provider.value,
        model=getattr(compiler, "model", None),
        new_only=new_only,
        allow_cloud_fallback=allow_cloud_fallback,
    )

    if strict and fail:
        console.print(f"[red]✗[/red] --strict: {_failure_summary(results)}")
        raise typer.Exit(1)


def _failure_summary(results) -> str:
    """``"2 failed (1 quarantined)"`` for a run's results; quarantined files count as failed."""
    failed = [r for r in results if not r.success]
    quarantined = sum(1 for r in failed if getattr(r, "quarantined", False))
    text = f"{len(failed)} failed"
    if quarantined:
        text += f" ({quarantined} quarantined)"
    return text


# ─── EMBED ─────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Maintenance")
def embed(
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-embed every wiki page (default: incremental)"
    ),
):
    """Refresh the semantic search embedding index.

    By default only embeds pages whose content hash changed since the
    last index save (incremental). Use --force after switching embedding
    providers to verify a clean rebuild rather than relying on the
    automatic model-mismatch discard.

    Embeddings are stored at '<prefix>_embeddings/index.json' on S3
    and travel with the KB. Compile runs this step automatically at the end
    of every successful run, so it rarely needs to be run manually.
    """
    from agent2kb.compile.compiler import Compiler

    profile = get_current_profile()
    s3 = get_s3()
    compiler = Compiler(profile=profile, s3=s3)

    with console.status("[bold green]Embedding wiki pages..."):
        embedded, pruned = compiler.update_embeddings(force=force)

    console.print(
        f"[green]✓[/green] Embeddings updated: "
        f"{embedded} new/updated, {pruned} pruned (stale)"
    )
    if not embedded and not pruned:
        console.print("[dim]Index already up to date.[/dim]")


# ─── QUERY ─────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Daily use")
def query(
    question: str = typer.Argument(..., help="Question to search for"),
    top_k: int = typer.Option(
        8,
        "--top-k",
        "-k",
        help="Initial hits passed to LLM (agentic expansion may add more)",
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help=(
            "Who is asking: 'user' (human-typed) or 'agent' (automated "
            "post-ingest / chat-reply enrichment). If omitted: read "
            "$KB_ACTOR, then fall back to stdin-TTY detection (TTY → "
            "user, non-TTY → agent). The actor decides default --save "
            "behaviour and the storage path: user → outputs/<...>.md, "
            "agent → outputs/agent/<...>.md. Agent saves are also "
            "deduplicated against captures with the same hit set from "
            "the last hour, so that repeated chat queries do not fill "
            "the folder."
        ),
    ),
    save: Optional[bool] = typer.Option(
        None,
        "--save/--no-save",
        help=(
            "Override actor-based default: 'user' defaults save=on (so "
            "human captures land in Obsidian); 'agent' defaults "
            "save=off (chat reply only, no outputs/ pollution). Pass "
            "--save explicitly when an agent-side answer is worth "
            "persisting; pass --no-save when a user-side query is "
            "only exploratory."
        ),
    ),
    expand: str = typer.Option(
        "auto",
        "--expand",
        help=(
            "Cross-language expansion mode: auto (dictionary first; the LLM "
            "is used only when the dictionary misses on long or CJK "
            "queries; default), dict (dictionary only, never calls the "
            "LLM; fastest), llm (always calls the LLM; best coverage, "
            "one extra round trip), off (no expansion). Use --expand off "
            "or --expand dict to reduce latency for frequent queries."
        ),
    ),
):
    """Query the knowledge base with natural language.

    User-typed queries default to --save: the answer lands in outputs/
    so the compounding loop and Obsidian sync pick it up. Agent-driven
    queries default to --no-save: the chat reply still works, but
    outputs/ stays curated. Pass --actor agent --save for the rare case
    an agent answer is worth persisting; pass --no-save for a single
    user lookup that should not be stored.
    """
    # Resolve actor. Validated inside _resolve_actor — keeps the guard
    # robust when tests mock the whole engine module.
    try:
        actor = _resolve_actor(actor)
    except ValueError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=2)

    # Resolve save default from actor, with explicit --save/--no-save winning.
    if save is None:
        save = actor == "user"

    from agent2kb.query import QueryEngine

    profile = get_current_profile()
    s3 = get_s3()
    try:
        engine = QueryEngine(profile=profile, s3=s3, expand_mode=expand)
    except ValueError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=2)

    with console.status("[bold green]Searching..."):
        answer = engine.query(question, top_k=top_k)

    # The answer is LLM output built from untrusted pages: print it literally.
    console.print(answer, markup=False, emoji=False)

    if save:
        output_key = engine.save_output(question, answer, actor=actor)
        # Dim so it doesn't dominate the actual answer output.
        short_key = "/".join(output_key.rsplit("/", 2)[-2:])
        console.print(
            f"[dim]• Saved to outputs/{short_key} ({actor}) for compounding[/dim]"
        )


# ─── PROMOTE ───────────────────────────────────────────────────────────


@app.command(rich_help_panel="Maintenance")
def promote(
    file: Path = typer.Option(..., "--file", "-f", help="Local markdown file to promote"),
    as_: str = typer.Option(
        "outputs",
        "--as",
        help=(
            "Destination mode: 'outputs' (transient, default), 'weekly' "
            "(periodic digest → outputs/weekly/), or 'wiki-topic' "
            "(permanent pinned wiki page)"
        ),
    ),
    slug: Optional[str] = typer.Option(
        None, "--slug", help="Override the destination slug (default: derive from title/filename)"
    ),
    delete_source: bool = typer.Option(
        False, "--delete-source", help="Delete the local file after a successful upload"
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help=(
            "Who is promoting: 'user' (human-driven) or 'agent' "
            "(automated, for example a scheduled digest). If omitted, "
            "$KB_ACTOR is used, then TTY detection."
        ),
    ),
):
    r"""Promote an orphan markdown file (e.g. from an agent worktree) into the KB.

    Modes:
      --as outputs    (default) Transient material, stored as
                      outputs/<stamp>-<slug>-<id>.md.
      --as weekly     Periodic digest, stored as outputs/weekly/<stamp>-<slug>-<id>.md
                      with source_kind=digest and tags \[digest, weekly]. Intended
                      for a weekly insight digest (about 200 to 500 words:
                      headline, top entities and open tensions).
      --as wiki-topic Permanent pinned reference, stored as wiki/topics/<slug>.md
                      with pinned: true and a frontmatter list of sources
                      extracted from the inline \[\[wikilinks]].
    """
    from agent2kb.promote import promote as _promote

    if as_ not in {"outputs", "wiki-topic", "weekly"}:
        console.print(
            f"[red]✗[/red] --as must be 'outputs', 'weekly', or 'wiki-topic', got: {escape(as_)}"
        )
        raise typer.Exit(1)

    try:
        resolved_actor = _resolve_actor(actor)
    except ValueError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=2)

    profile = get_current_profile()
    s3 = get_s3()

    try:
        dest_key = _promote(
            profile=profile,
            s3=s3,
            file_path=file,
            mode=as_,  # type: ignore[arg-type]
            slug_override=slug,
            delete_source=delete_source,
            actor=resolved_actor,
        )
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(1)

    short = dest_key.split("/", 2)[-1]
    console.print(f"[green]✓[/green] Promoted → {escape(short)}  ({escape(resolved_actor)})")
    if delete_source:
        console.print(f"[dim]• Deleted local source: {escape(str(file))}[/dim]")
    if as_ == "wiki-topic":
        console.print(
            "[dim]• Pinned topic — compile will leave it untouched. "
            "It is now reachable via "
            + escape("[[topics/" + dest_key.rsplit("/", 1)[-1].replace(".md", "") + "]]")
            + ".[/dim]"
        )
    elif as_ == "weekly":
        console.print(
            "[dim]• Weekly digest — terminal artefact (does not feed compounding loop).[/dim]"
        )


# ─── LINT ──────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Maintenance")
def lint(
    auto_fix: bool = typer.Option(False, "--auto-fix", help="Automatically fix issues"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Run the auto-fix matcher and print what would change, but "
            "skip every S3 upload. Use this before --auto-fix to preview "
            "the rewrites and avoid accidental mass changes."
        ),
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="LLM-powered semantic health check (contradictions, gaps, suggestions)",
    ),
    no_links: bool = typer.Option(
        False,
        "--no-links",
        help="Skip the broken-wikilink check (unresolved links are then treated as ghost nodes)",
    ),
    llm: Optional[str] = typer.Option(None, "--llm", help="LLM provider for --deep"),
):
    """Lint wiki pages for frontmatter issues and (optionally) ghost wikilinks.

    Use --deep for an LLM-powered semantic health check that finds contradictions,
    knowledge gaps and unsourced claims, and suggests new articles.

    Unresolved wikilinks are treated as Obsidian ghost nodes and not
    reported by default. Run with --auto-fix to rewrite confident matches
    and surface the residual targets the rewriter could not resolve; pass
    --no-links to skip the resolver entirely.
    """
    from agent2kb.lint import WikiLinter

    profile = get_current_profile()
    s3 = get_s3()
    linter = WikiLinter(profile=profile, s3=s3)

    issues = linter.lint(auto_fix=auto_fix, check_links=not no_links, dry_run=dry_run)
    if not issues:
        console.print("[green]✓[/green] No format issues found")
    else:
        for issue in issues:
            console.print(f"  [yellow]⚠[/yellow] {escape(str(issue))}")
        if dry_run:
            preview_count = sum(1 for i in issues if "(dry-run)" in i)
            console.print(
                f"[cyan]●[/cyan] dry-run: {preview_count} change(s) would be applied "
                f"— re-run with [bold]--auto-fix[/bold] (without --dry-run) to commit"
            )
        elif auto_fix:
            console.print(f"[green]✓[/green] Fixed {len(issues)} issues")

    if deep:
        from agent2kb.models import LLMProvider as LP

        provider = LP(llm) if llm else profile.compile_provider
        console.print("\n[bold]Running semantic health check...[/bold]")
        with console.status("[bold green]Analysing the wiki content with the LLM..."):
            report = linter.deep_health_check(provider=provider)
        console.print(report, markup=False, emoji=False)


# ─── STUBS ─────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Maintenance")
def stubs(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Print which stubs would be created or refreshed without writing "
            "anything to S3. Use this before a real run on a new KB to "
            "check that the candidate list is reasonable (no acronyms and "
            "no spurious LLM-generated names)."
        ),
    ),
    min_refs: int = typer.Option(
        5,
        "--min-refs",
        help=(
            "Minimum number of distinct pages that must mention a ghost "
            "concept before a stub is created. The default of 5 is a "
            "high-signal filter. Lower it to 3 for broader coverage on a "
            "mature KB; raise it to 10 or more for a tightly curated wiki "
            "in which only the most frequently cited ghosts are wanted."
        ),
    ),
):
    r"""Auto-generate stub concept pages for ghost wikilinks.

    Scans wiki/ and outputs/ for \[\[Foo]] references whose target page
    does not exist, then writes a placeholder concepts/<slug>.md for each,
    listing the pages that mention it. This is a server-side fix: readers no
    longer reach Obsidian's "Create new page" prompt when they follow an
    unresolved link, and no client configuration is required.

    The command is conservative: it skips acronyms, URL-shaped targets,
    targets with too few references (see --min-refs), and anything that
    would overwrite a real (non-stub) page. Re-running is idempotent:
    existing stubs have their list of referring pages refreshed, and real
    pages are never modified.

    For best results, schedule it daily alongside 'a2kb compile'. Run it
    once with --dry-run first when setting up a new KB.
    """
    from agent2kb.stubs import StubGenerator

    profile = get_current_profile()
    s3 = get_s3()
    gen = StubGenerator(profile=profile, s3=s3)

    with console.status("[bold green]Scanning for ghost wikilinks..."):
        stats = gen.generate(dry_run=dry_run, min_referrers=min_refs)

    verb = "Would create" if dry_run else "Created"
    refresh_verb = "would refresh" if dry_run else "refreshed"
    console.print(
        f"[cyan]●[/cyan] candidates: {stats['candidates']}  "
        f"[green]{verb.lower()}: {stats['created']}[/green]  "
        f"{refresh_verb}: {stats['refreshed']}  "
        f"skipped: {stats['skipped']}  "
        f"low-refs (<{min_refs}): {stats['low_refs']}  "
        f"[red]errors: {stats['errors']}[/red]"
    )
    if dry_run and (stats["created"] or stats["refreshed"]):
        console.print(
            "[cyan]●[/cyan] dry-run only — re-run without [bold]--dry-run[/bold] to commit"
        )


# ─── CLIP ──────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Maintenance")
def clip(
    delete: bool = typer.Option(
        True, "--delete/--keep", help="Delete clippings from wiki/ after moving to raw/"
    ),
):
    """Move Web Clipper clippings from wiki/Clippings/ to raw/ for compilation.

    Obsidian Web Clipper saves to Clippings/ in your vault, which syncs to
    wiki/Clippings/ via Remotely Save. This command moves them to raw/ with
    proper frontmatter so they enter the normal compile pipeline.
    """
    import datetime

    from agent2kb.models import Frontmatter
    from agent2kb.utils import content_hash, slugify

    profile = get_current_profile()
    s3 = get_s3()

    # List all clippings
    clip_keys = s3.list_keys(sub_prefix="wiki/Clippings/", suffix=".md")
    if not clip_keys:
        console.print("[dim]No clippings found in wiki/Clippings/[/dim]")
        return

    moved = 0
    for key in clip_keys:
        try:
            content = s3.download(key)
            fm, body = Frontmatter.from_yaml_block(content)

            # Ensure required fields
            if not fm.source_type:
                fm.source_type = "url"
            if not fm.date:
                fm.date = datetime.datetime.now().strftime("%Y-%m-%d")
            if not fm.original_link and fm.source:
                fm.original_link = fm.source
            fm.compiled = False

            # Build raw key with content hash for dedup
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            slug = slugify(fm.title or key.split("/")[-1].replace(".md", ""))
            short_hash = content_hash(body)[:8]
            raw_filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = f"{profile.prefix}raw/{raw_filename}"

            # Skip if already exists in raw/
            if s3.exists(raw_key):
                console.print(f"  [dim]─ Already in raw: {escape(raw_filename)}[/dim]")
                if delete:
                    s3.delete(key)
                continue

            # Write to raw/ with normalized frontmatter
            full_content = fm.to_yaml_block() + "\n" + body
            s3.upload(raw_key, full_content)
            moved += 1
            console.print(f"  [green]✓[/green] {escape(fm.title or slug)} → raw/{escape(raw_filename)}")

            # Delete from wiki/Clippings/
            if delete:
                s3.delete(key)

        except Exception as e:
            console.print(f"  [red]✗[/red] {escape(key)}: {escape(str(e))}")

    console.print(f"\n[bold]Moved {moved} clippings to raw/[/bold]")
    if moved:
        console.print("[dim]Run 'a2kb compile' to compile them into wiki pages.[/dim]")


# ─── RM (remove raw + derived artifacts) ───────────────────────────────


def _print_removal_plan(selected, deletions, scope: str) -> None:
    """Pretty-print which articles + layers a plan will touch."""
    layers_by_key: dict[str, list[str]] = {}
    for d in deletions:
        layers_by_key.setdefault(d.key, []).append(d.layer)

    console.print(
        f"[bold]{len(selected)} item(s) matched[/bold] "
        f"[dim](scope: {scope})[/dim]"
    )
    for m in selected:
        head = m.label
        console.print(f"  • {escape(str(head))}  [dim]{escape(str(m.basename))}[/dim]")
        if m.original_link:
            console.print(f"      [dim]↳ {escape(str(m.original_link))}[/dim]")
        # Show the concrete keys/layers under this article.
        for key in (m.raw_key, m.wiki_key):
            if key and key in layers_by_key:
                tags = "+".join(layers_by_key[key])
                console.print(f"      [red]✗[/red] {escape(key)}  [dim]({escape(str(tags))})[/dim]")


def _stdin_is_terminal() -> bool:
    """Whether a person could be typing the confirmation (patched in tests)."""
    return sys.stdin.isatty()


def _rm(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Match the raw item ingested from this URL"),
    slug: Optional[str] = typer.Option(None, "--slug", help="Match by filename slug (loose)"),
    file: Optional[str] = typer.Option(None, "--file", help="Match by exact raw/wiki filename"),
    id: Optional[str] = typer.Option(None, "--id", help="Match by the short content-hash suffix"),
    raw: bool = typer.Option(False, "--raw", help="Delete only the raw/ source object"),
    wiki: bool = typer.Option(False, "--wiki", help="Delete only the compiled wiki/ page (+ its index/manifest entries)"),
    all_layers: bool = typer.Option(
        False, "--all", help="Delete raw + wiki + embedding + manifest (default when neither --raw nor --wiki given)"
    ),
    older_than: Optional[str] = typer.Option(
        None, "--older-than", help="Bulk mode: remove articles older than DURATION (e.g. 90d, 12w)"
    ),
    tag: Optional[str] = typer.Option(None, "--tag", help="Bulk mode: only remove articles carrying this tag"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, delete nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation (for scripts)"),
    reingest: bool = typer.Option(
        False, "--reingest", help="After removing a --url item, immediately re-ingest the same URL"
    ),
    actor: Optional[str] = typer.Option(None, "--actor", help="Actor: user|agent (auto-resolved if omitted)"),
):
    """Remove a raw ingest and its derived wiki, index and manifest entries.

    Dry-run-then-confirm by default: shows exactly what would be deleted
    and asks for y/N. Pass --dry-run to never delete, or --yes to skip the
    prompt for a single item. Bulk mode (--older-than) ignores --yes: it
    only deletes from an interactive terminal after you type the item
    count, and is a dry run anywhere else. Idempotent — selecting
    something already gone exits 0.

    Examples:
      a2kb rm --url https://x.com/foo --dry-run     # preview the chain
      a2kb rm --url https://x.com/foo --yes         # remove raw+wiki+index+manifest
      a2kb rm --slug 20260531-foo --wiki            # drop just the wiki page
      a2kb rm --older-than 90d --tag scratch        # bulk prune: needs a terminal; type the count
      a2kb rm --url https://x.com/foo --reingest -y # redo a stale ingest cleanly
    """
    from agent2kb import remove as _rm_mod

    # ── validate the selector combination ──────────────────────────
    selectors = [s for s in (url, slug, file, id) if s]
    bulk = older_than is not None
    if not selectors and not bulk:
        console.print(
            "[red]✗[/red] Provide a selector: --url / --slug / --file / --id, "
            "or --older-than for bulk prune."
        )
        raise typer.Exit(2)
    if len(selectors) > 1:
        console.print("[red]✗[/red] Use only one of --url / --slug / --file / --id.")
        raise typer.Exit(2)
    if bulk and selectors:
        console.print(
            "[red]✗[/red] --older-than is a bulk mode and cannot be combined with "
            "--url/--slug/--file/--id."
        )
        raise typer.Exit(2)
    if reingest and not url:
        console.print("[red]✗[/red] --reingest requires --url.")
        raise typer.Exit(2)
    if reingest and (raw or wiki):
        console.print(
            "[red]✗[/red] --reingest needs the full chain removed; drop --raw/--wiki "
            "(re-ingest rebuilds raw → wiki)."
        )
        raise typer.Exit(2)

    scope = _rm_mod.resolve_scope(raw=raw, wiki=wiki)

    profile = get_current_profile()
    s3 = get_s3()

    matches = _rm_mod.collect(s3, profile)

    # ── selection ──────────────────────────────────────────────────
    if bulk:
        from agent2kb.outputs import parse_duration

        try:
            delta = parse_duration(older_than)
        except ValueError as e:
            console.print(f"[red]✗[/red] {escape(str(e))}")
            raise typer.Exit(2)
        with console.status("[bold green]Scanning for matching articles..."):
            selected = _rm_mod.select_bulk(s3, profile, matches, older_than=delta, tag=tag)
    else:
        with console.status("[bold green]Resolving selector..."):
            selected = _rm_mod.select(
                s3, profile, matches, url=url, slug=slug, file=file, id=id
            )

    # Idempotent: nothing to do is a success.
    if not selected:
        console.print("[green]✓[/green] Nothing matched — nothing to remove.")
        return

    _rm_mod.enrich(s3, selected)

    # Single-item modes must disambiguate; never delete many silently.
    if not bulk and len(selected) > 1:
        console.print(
            f"[yellow]⚠ {len(selected)} items match — narrow your selector "
            f"(use --file for an exact filename):[/yellow]"
        )
        for m in selected:
            console.print(f"  • {escape(str(m.basename))}  [dim]{escape(str(m.title))}[/dim]")
        raise typer.Exit(2)

    deletions = _rm_mod.build_deletions(profile, selected, scope)
    _print_removal_plan(selected, deletions, scope)

    # Surface leftover references (best-effort, optional report).
    for w in _rm_mod.orphaned_raw_warnings(selected, scope):
        console.print(f"  [yellow]⚠[/yellow] {escape(str(w))}")
    if scope in (_rm_mod.SCOPE_ALL, _rm_mod.SCOPE_WIKI):
        for w in _rm_mod.find_orphaned_backlinks(s3, profile, selected):
            console.print(f"  [yellow]⚠ orphaned link:[/yellow] {escape(str(w))}")

    # ── safety gate ────────────────────────────────────────────────
    if dry_run:
        console.print(
            "[cyan]●[/cyan] dry-run — nothing deleted. Re-run without --dry-run "
            "(add --yes to skip the prompt)."
        )
        return

    if bulk:
        # Bulk prune is the one path that can empty a whole KB in a single
        # command, so it never accepts --yes and needs a person at a
        # terminal typing the item count. The actor label can't gate this:
        # it is self-declared, and an agent acting on injected instructions
        # from an ingested page can claim to be "user". Off a terminal
        # (scripts, cron, agent tool calls) bulk mode stays a dry run.
        if not _stdin_is_terminal():
            console.print(
                "[yellow]⚠ Bulk delete needs an interactive terminal — nothing deleted.[/yellow] "
                "[dim]The plan above is a dry run. Remove items one at a time with "
                "--url/--file (which accept --yes), or re-run this in a terminal.[/dim]"
            )
            raise typer.Exit(2)
        if yes:
            console.print("[dim]--yes is ignored for bulk deletes.[/dim]")
        try:
            answer = input(
                f"Type {len(selected)} to delete these {len(selected)} item(s): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != str(len(selected)):
            console.print("[dim]Aborted — nothing deleted.[/dim]")
            return
    elif not yes:
        try:
            answer = input("Delete the above? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            console.print("[dim]Aborted — nothing deleted.[/dim]")
            return

    # ── execute ────────────────────────────────────────────────────
    report = _rm_mod.apply_deletions(s3, profile, deletions)
    console.print(
        f"[green]✓[/green] Deleted {report.objects_deleted} object(s); "
        f"embeddings −{report.embeddings_removed}, manifest −{report.manifest_removed}."
    )

    # ── optional re-ingest ─────────────────────────────────────────
    if reingest and url:
        from agent2kb.ingest.base import IngestPipeline

        resolved_actor = _resolve_actor(actor)
        console.print(f"[bold]Re-ingesting[/bold] {escape(url)} ...")
        pipeline = IngestPipeline(profile=profile, s3=s3, tags=[])
        result = pipeline.ingest_url(url, actor=resolved_actor)
        if result.success:
            console.print(f"[green]✓[/green] Re-ingested → {escape(result.raw_key)}")
            console.print("[dim]Run 'a2kb compile' to rebuild the wiki page.[/dim]")
        else:
            console.print(f"[red]✗[/red] Re-ingest failed: {escape(str(result.error))}")
            raise typer.Exit(1)


# Register the same handler under both `rm` and its `delete` alias.
app.command(name="rm", rich_help_panel="Maintenance")(_rm)
app.command(name="delete", hidden=True)(_rm)


# ─── BACKFILL ──────────────────────────────────────────────────────────


@app.command("backfill-source", rich_help_panel="Maintenance")
def backfill_source(
    apply: bool = typer.Option(
        False, "--apply", help="Actually rewrite pages (default: dry-run preview)"
    ),
):
    """Backfill visible `> Source:` line on old article pages.

    Adds the visible source line used by newer compile runs to older wiki
    articles whose frontmatter has `source` or `original_link` but no
    visible `> Source:` line. Skips index.md, log.md, and wiki/concepts/.
    Dry-run by default — pass --apply to write changes.
    """
    from agent2kb.models import Frontmatter

    get_current_profile()  # Ensure profile is resolved for S3 client.
    s3 = get_s3()

    wiki_keys = s3.list_wiki_files()
    skip_filenames = {"index.md", "log.md", ".gitkeep"}
    candidates: list[tuple[str, str]] = []  # (key, source_url)

    for key in wiki_keys:
        filename = key.split("/")[-1]
        if filename in skip_filenames or "/concepts/" in key or "/topics/" in key:
            continue
        try:
            content = s3.download(key)
        except Exception as e:
            console.print(f"  [red]✗[/red] Cannot read {escape(key)}: {escape(str(e))}")
            continue

        fm, body = Frontmatter.from_yaml_block(content)
        if "> Source:" in body or "> source:" in body:
            continue

        source_url = fm.original_link or fm.source
        if not source_url:
            continue
        candidates.append((key, source_url))

    if not candidates:
        console.print("[green]✓[/green] All article pages already have a visible source line.")
        return

    console.print(
        f"[bold]{len(candidates)} article page(s) missing visible source line[/bold]"
    )
    for key, src in candidates[:20]:
        console.print(f"  {'[yellow]would-write[/yellow]' if not apply else '[green]writing[/green]'} {escape(key)}  ← {escape(str(src))}")
    if len(candidates) > 20:
        console.print(f"  [dim]...and {len(candidates) - 20} more[/dim]")

    if not apply:
        console.print("\n[dim]Dry-run. Re-run with --apply to rewrite pages.[/dim]")
        return

    written = 0
    for key, src in candidates:
        try:
            content = s3.download(key)
            fm, body = Frontmatter.from_yaml_block(content)
            source_line = f"> Source: [{src}]({src})\n\n"
            new_content = fm.to_yaml_block() + "\n" + source_line + body
            s3.upload(key, new_content)
            written += 1
        except Exception as e:
            console.print(f"  [red]✗[/red] {escape(key)}: {escape(str(e))}")

    console.print(f"\n[green]✓[/green] Backfilled {written} page(s).")


# ─── STATUS ────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Backup")
def backup(
    keep: int = typer.Option(
        30, "--keep", help="Number of this KB's archives to retain (0 keeps all)"
    ),
    include_raw: bool = typer.Option(
        True, "--include-raw/--no-raw",
        help="Also archive raw/ (default: on, because raw/ is the source of truth)"
    ),
    backup_dir: str = typer.Option(
        ".backups",
        "--dir",
        help="Local directory for archives (relative to CWD or absolute)",
    ),
):
    """Archive wiki/, outputs/ and raw/ to a local tar.gz file, independent of S3.

    The archive is written to <dir>/<kb>-wiki-YYYY-MM-DD-HHMMSS-<random>.tar.gz on the
    local filesystem (mode 0600; a new <dir> is created 0700), so it survives any S3
    failure (a synchronisation fault, a credential leak or the loss of the server). It
    records which KB it came from, and --keep only prunes this KB's archives.
    """
    from agent2kb import backup as _backup

    profile = get_current_profile()
    s3 = get_s3()
    out_dir = Path(backup_dir).expanduser().resolve()

    # raw/ is source of truth — include by default so a full restore is possible
    # from a single archive without needing to re-ingest any URL.
    targets = ["wiki/", "outputs/"]
    if include_raw:
        targets.append("raw/")

    archive_path, file_count, total_bytes = _backup.write_archive(
        s3, profile, out_dir, targets, include_raw
    )
    archive_size = archive_path.stat().st_size
    console.print(
        f"[green]✓[/green] Archived {file_count} files "
        f"({total_bytes:,} bytes → {archive_size:,} gz) → {escape(str(archive_path))}"
    )

    removed, foreign = _backup.prune(out_dir, profile.name, keep)
    for old in removed:
        console.print(f"  [dim]pruned {escape(old.name)}[/dim]")
    for other in foreign:
        console.print(
            f"  [yellow]![/yellow] kept {escape(other.name)}: its name matches this KB but its "
            "manifest names another KB (or it cannot be read), so retention leaves it alone."
        )
    legacy = _backup.legacy_archives(out_dir)
    if legacy:
        console.print(
            f"  [dim]{len(legacy)} archive(s) in the old wiki-<stamp>.tar.gz format are not "
            "pruned automatically, as they do not record their KB. Delete them by hand when "
            "they are no longer needed.[/dim]"
        )


@app.command(rich_help_panel="Backup")
def restore(
    date: str = typer.Option(..., "--date", help="Archive date (YYYY-MM-DD) to restore"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply", help="Dry-run shows plan; --apply writes to S3"
    ),
    target: str = typer.Option(
        "wiki/", "--target", help="Sub-prefix to restore into (default: wiki/)"
    ),
    backup_dir: str = typer.Option(
        ".backups",
        "--dir",
        help="Local directory holding archives (relative to CWD or absolute)",
    ),
    from_kb: Optional[str] = typer.Option(
        None,
        "--from-kb",
        help="Look for archives of this KB instead of the current one "
        "(restoring them also needs --allow-cross-kb)",
    ),
    allow_cross_kb: bool = typer.Option(
        False,
        "--allow-cross-kb",
        help="Restore an archive made from a different KB into the current one",
    ),
):
    """Restore from a local tar.gz archive back into S3.

    By default this is a dry run that lists the files without writing anything.
    Use --apply to upload them to S3. Existing S3 objects with the same key
    will be overwritten. An archive made from a different KB (name or prefix) is
    refused unless --allow-cross-kb is given; an old archive without a manifest is
    restored with a warning, as its KB cannot be checked.
    """
    import tarfile

    from agent2kb import backup as _backup

    profile = get_current_profile()
    s3 = get_s3()

    out_dir = Path(backup_dir).expanduser().resolve()
    if not _backup.valid_date(date):
        console.print(
            f"[red]✗[/red] --date takes YYYY-MM-DD or a longer stamp, not '{escape(date)}'"
        )
        raise typer.Exit(1)
    source_kb = from_kb or profile.name
    # Accept a date prefix (YYYY-MM-DD) or a full stamp; the newest match wins.
    found = _backup.find_archive(out_dir, source_kb, date)
    if found is None:
        console.print(
            f"[red]✗[/red] No archive of KB '{escape(source_kb)}' (or in the old "
            f"wiki-<stamp> format) matching '{escape(date)}' in {escape(str(out_dir))}"
        )
        available = _backup.kb_archives(out_dir, source_kb) + _backup.legacy_archives(out_dir)
        if available:
            console.print("Available archives:")
            for a in sorted(available, key=lambda a: a.stamp)[-10:]:
                console.print(f"  {escape(a.path.name)}")
        raise typer.Exit(1)
    archive_path = found.path
    console.print(f"[dim]Using {escape(archive_path.name)}[/dim]")

    restored = 0
    with tarfile.open(archive_path, "r:gz") as tar:
        manifest_data = _backup.read_manifest(tar)
        if manifest_data is None:
            console.print(
                "[yellow]![/yellow] This archive has no manifest (it was made before agent2kb "
                "recorded the source KB), so it cannot be checked against "
                f"'{escape(profile.name)}'. Make sure it belongs to this KB."
            )
        else:
            src = manifest_data.get("kb")
            src = src if isinstance(src, dict) else {}
            mismatch = _backup.identity_mismatch(manifest_data, profile)
            label = (
                f"'{escape(str(src.get('name')))}' (prefix '{escape(str(src.get('prefix')))}')"
            )
            if mismatch and not allow_cross_kb:
                console.print(
                    f"[red]✗[/red] {escape(archive_path.name)} was made from KB {label}, "
                    f"not '{escape(profile.name)}' (prefix '{escape(profile.prefix)}'). "
                    "Nothing was restored. Re-run with --allow-cross-kb to restore it into "
                    "this KB anyway."
                )
                raise typer.Exit(1)
            if mismatch:
                console.print(
                    f"[yellow]![/yellow] Cross-KB restore: archive from {label} into "
                    f"'{escape(profile.name)}'."
                )
            elif src.get("bucket") != profile.bucket:
                console.print(
                    f"[dim]The archive was made from bucket '{escape(str(src.get('bucket')))}'"
                    f"; restoring into '{escape(profile.bucket)}'.[/dim]"
                )
        skipped: list[tuple[str, str]] = []
        selected = []
        for member in tar.getmembers():
            if member.name == _backup.MANIFEST_NAME or member.isdir():
                continue
            problem = _backup.member_problem(member)
            if problem:
                skipped.append((member.name, problem))
                continue
            if not member.name.startswith(target):
                continue
            selected.append(member)
        total = sum(m.size for m in selected)
        if total > _backup.MAX_RESTORE_TOTAL_BYTES:
            console.print(
                f"[red]✗[/red] {escape(archive_path.name)} would restore {total:,} bytes, more "
                f"than the {_backup.MAX_RESTORE_TOTAL_BYTES // 1024**3} GiB limit; the archive "
                "is damaged or crafted. Nothing was restored."
            )
            raise typer.Exit(1)
        for member in selected:
            key = f"{profile.prefix}{member.name}"
            if dry_run:
                console.print(f"  [dim]would restore {escape(key)} ({member.size} bytes)[/dim]")
            else:
                f = tar.extractfile(member)
                if f is None:
                    continue
                # member_problem bounded member.size; never read past it either way.
                s3.upload(key, f.read(_backup.MAX_RESTORE_MEMBER_BYTES + 1))
                console.print(f"  [green]✓[/green] restored {escape(key)}")
            restored += 1

    if skipped:
        console.print(
            f"[yellow]![/yellow] Skipped {len(skipped)} archive member(s) that "
            "`a2kb backup` never writes (the archive may be damaged or crafted):"
        )
        for name, problem in skipped[:20]:
            console.print(f"  [yellow]skipped[/yellow] {escape(repr(name))}: {escape(problem)}")
        if len(skipped) > 20:
            console.print(f"  [dim]...and {len(skipped) - 20} more[/dim]")
    verb = "Would restore" if dry_run else "Restored"
    console.print(f"[green]✓[/green] {verb} {restored} files from {escape(str(archive_path))}")
    if dry_run:
        console.print("[yellow]dry-run mode[/yellow] — re-run with --apply to write")


@app.command(rich_help_panel="Maintenance")
def manifest(
    rebuild_flag: bool = typer.Option(
        False,
        "--rebuild",
        help="Rebuild the manifest from scratch by scanning wiki/ and outputs/.",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help="Print a summary of the current manifest (entry count, age).",
    ),
):
    """Rebuild or inspect the per-profile page manifest (a metadata cache).

    The manifest is a JSON cache at '<prefix>_meta/manifest.json' that lets
    lint, query and index avoid downloading every page just to read its
    metadata. Compile, promote and clip update it incrementally; use
    'a2kb manifest --rebuild' to recover when entries have drifted, or for a
    new profile.
    """
    from agent2kb import manifest as mf

    profile = get_current_profile()
    s3 = get_s3()

    if rebuild_flag:
        m = mf.rebuild(s3, profile)
        console.print(
            f"[green]✓[/green] Manifest rebuilt — {len(m['entries'])} entries"
        )
        return

    if show:
        m = mf.load(s3, profile)
        if m is None:
            console.print(
                "[yellow]No manifest yet.[/yellow] Run "
                "[bold]a2kb manifest --rebuild[/bold] to create one."
            )
            return
        n = len(m.get("entries", {}))
        kinds: dict[str, int] = {}
        for entry in m["entries"].values():
            kinds[entry.get("kind", "unknown")] = (
                kinds.get(entry.get("kind", "unknown"), 0) + 1
            )
        console.print(f"Manifest: {n} entries, generated_at={escape(str(m.get('generated_at')))}")
        for kind, count in sorted(kinds.items()):
            console.print(f"  [dim]{escape(str(kind))}[/dim]: {count}")
        return

    console.print(
        "Usage: a2kb manifest --rebuild | --show. "
        "Manifest powers fast metadata lookups (lint, query resolution, "
        "index). Run [bold]--rebuild[/bold] after large bulk imports."
    )


# ─── OUTPUTS (capture management) ──────────────────────────────────────

outputs_app = typer.Typer(
    name="outputs",
    help=(
        "List and prune the captures in outputs/. User captures are stored as "
        "outputs/<...>.md (durable); agent captures are stored under "
        "outputs/agent/ and may be pruned."
    ),
    no_args_is_help=True,
)
app.add_typer(outputs_app, name="outputs", rich_help_panel="Maintenance")


def _outputs_validate_actor(actor: Optional[str]) -> Optional[str]:
    """Shared CLI guard for --actor on the outputs subcommands."""
    if actor is None:
        return None
    if actor not in ("user", "agent"):
        console.print(
            f"[red]✗[/red] --actor must be 'user' or 'agent', got {escape(repr(actor))}"
        )
        raise typer.Exit(code=2)
    return actor


@outputs_app.command("ls")
def outputs_ls(
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help="Filter by actor: 'user' or 'agent'. Omit to list both.",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Only show captures newer than DURATION (e.g. 7d, 24h, 2w).",
    ),
    limit: int = typer.Option(
        50, "--limit", "-n", help="Max entries to display."
    ),
    titles: bool = typer.Option(
        False,
        "--titles",
        help=(
            "Download each file's frontmatter to show the question title "
            "and follow-up count. This needs one request per file, so it "
            "is slower."
        ),
    ),
):
    """List recent captures in outputs/ with their actor and age."""
    from agent2kb import outputs as _o

    _outputs_validate_actor(actor)
    s3 = get_s3()
    try:
        delta = _o.parse_duration(since) if since else None
    except ValueError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=2)

    entries = _o.list_entries(
        s3, actor=actor, since=delta, limit=limit, enrich_titles=titles
    )

    by_actor: dict[str, int] = {}
    for e in entries:
        by_actor[e.actor] = by_actor.get(e.actor, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_actor.items()))
    console.print(f"[bold]{len(entries)} entries[/bold] ({escape(summary or 'none')})")
    console.print(_o.format_table(entries, show_titles=titles), markup=False, emoji=False)


@outputs_app.command("prune")
def outputs_prune(
    older_than: str = typer.Option(
        ...,
        "--older-than",
        help="Delete captures older than DURATION (e.g. 30d, 12h, 2w).",
    ),
    actor: Optional[str] = typer.Option(
        None,
        "--actor",
        help=(
            "Only prune captures from this actor. Strongly recommended: "
            "'--actor agent' so user-typed captures are never touched."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete. Default is dry-run (preview only).",
    ),
):
    """Delete old captures in outputs/. This is a dry run unless --apply is given."""
    from agent2kb import outputs as _o

    _outputs_validate_actor(actor)
    s3 = get_s3()
    profile = get_current_profile()
    try:
        delta = _o.parse_duration(older_than)
    except ValueError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=2)

    # Safety rail: refuse the unscoped wide-net prune. Even seasoned
    # operators have fat-fingered ``--older-than 0d`` and deleted the
    # whole folder; requiring --actor for full wipes makes the
    # destructive shape explicit.
    if actor is None and delta < datetime.timedelta(days=7):
        console.print(
            "[red]✗[/red] refusing to prune outputs younger than 7 days "
            "without --actor. Add --actor agent (recommended) or pick a "
            "larger window."
        )
        raise typer.Exit(code=2)

    doomed = _o.prune(s3, profile, older_than=delta, actor=actor, apply=apply)

    verb = "Pruned" if apply else "Would prune"
    console.print(f"[bold]{verb} {len(doomed)} entr{'y' if len(doomed) == 1 else 'ies'}[/bold]")
    for e in doomed:
        stamp = e.created_at.strftime("%Y-%m-%d %H:%M")
        console.print(
            f"  [dim]{stamp}[/dim]  {e.actor.ljust(5)}  {escape(e.key.split('/')[-1])}"
        )
    if not apply and doomed:
        console.print(
            "[cyan]●[/cyan] dry-run — re-run with [bold]--apply[/bold] to delete."
        )


# ─── SCAFFOLD (canonical project-rules sections) ────────────────────────

scaffold_app = typer.Typer(
    name="scaffold",
    help=(
        "Generate the canonical project-rule sections (such as KB Query "
        "Discipline) in a target PROJECT.md or AGENTS.md file. The command "
        "is idempotent, because it relies on version markers in HTML "
        "comments: run it with --upgrade after upgrading agent2kb, or with "
        "--force to migrate hand-written sections to the marked form."
    ),
    no_args_is_help=True,
)
app.add_typer(scaffold_app, name="scaffold", rich_help_panel="Maintenance")


@scaffold_app.command("rules")
def scaffold_rules(
    target: Optional[Path] = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Target file (usually <project>/PROJECT.md). If omitted, the "
            "rendered block is printed to stdout so you can preview or "
            "paste manually."
        ),
    ),
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help=(
            "Replace an existing marker block with the current template. "
            "Use after upgrading agent2kb."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Migrate a hand-written section that predates the scaffold, "
            "matched by its heading (## Knowledge-Base Query Discipline). "
            "Inserts a new block if no such section exists. Intended for "
            "the first migration; use --upgrade for later runs."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the resulting content without writing it.",
    ),
):
    """Stamp the KB Query Discipline rule into a project's PROJECT.md.

    Workflow for a brand-new project:

        a2kb -p <profile> init                         # create the S3 layout
        $EDITOR <project-dir>/PROJECT.md               # add the project notes
        a2kb -p <profile> scaffold rules --target <project-dir>/PROJECT.md

    After upgrading agent2kb:

        a2kb -p <profile> scaffold rules --target <project-dir>/PROJECT.md --upgrade

    Note: a2kb modifies only the marked block inside the target file. If another
    tool regenerates or copies the instruction files (for example an agent runner
    or a mirror of the project), run its own synchronisation step afterwards so
    that those copies pick up the change.
    """
    from agent2kb import scaffold as _sc

    if upgrade and force:
        console.print("[red]✗[/red] --upgrade and --force are mutually exclusive.")
        raise typer.Exit(code=2)

    profile = get_current_profile()

    if target is None:
        # No target → render to stdout. Useful for preview / piping into
        # editors / cross-checking against an existing project file.
        console.print(_sc.render(profile.name), markup=False, emoji=False, highlight=False)
        return

    target_path = target.expanduser().resolve()
    mode = "force" if force else ("upgrade" if upgrade else "insert")

    try:
        result = _sc.upsert(target_path, profile.name, mode=mode, dry_run=dry_run)
    except RuntimeError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=1)
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] {escape(str(result['action']))} → {escape(str(result['target']))}")
    if dry_run:
        console.print(
            "[cyan]●[/cyan] dry-run — re-run without --dry-run to write."
        )


@app.command(rich_help_panel="Daily use")
def status():
    """Show profile status, recent activity, and rate limit info."""
    from agent2kb.status import show_status

    profile = get_current_profile()
    s3 = get_s3()
    if not show_status(profile=profile, s3=s3, console=console):
        raise typer.Exit(1)


# ─── MCP ──────────────────────────────────────────────────────────────


@app.command("mcp", rich_help_panel="Agents")
def mcp_cmd(
    ctx: typer.Context,
    http: Optional[str] = typer.Option(
        None,
        "--http",
        metavar="HOST:PORT",
        help=(
            "Serve streamable HTTP instead of stdio. Any host other than "
            "127.0.0.1/localhost requires --token."
        ),
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        envvar="KB_MCP_TOKEN",
        help=(
            "Bearer token required on every HTTP request (min 16 chars; required for "
            "non-loopback binds, generated if omitted on loopback). "
            "Prefer $KB_MCP_TOKEN: command-line arguments show up in `ps`."
        ),
    ),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        help=(
            "HTTP on loopback only: skip the bearer token. Without this, a random token "
            "is generated and printed to stderr. Risky: see docs/mcp.md."
        ),
    ),
    allow_origin: list[str] = typer.Option(
        [],
        "--allow-origin",
        help=(
            "HTTP: browser Origin to accept (repeatable, e.g. https://app.example.com). "
            "Requests with any other Origin header are refused."
        ),
    ),
    allow_host: list[str] = typer.Option(
        [],
        "--allow-host",
        help=(
            "HTTP: extra Host header value to accept (repeatable), for example the "
            "public name of a reverse proxy (kb.example.com)."
        ),
    ),
    ingest_allow_domain: list[str] = typer.Option(
        [],
        "--ingest-allow-domain",
        help=(
            "With --allow-write: only ingest URLs on this domain or its subdomains "
            "(repeatable; also checked on every redirect). Recommended when serving over HTTP."
        ),
    ),
    allow_llm: bool = typer.Option(
        False, "--allow-llm", help="Also expose `ask` (LLM answer; costs tokens)."
    ),
    allow_write: bool = typer.Option(
        False, "--allow-write", help="Also expose `ingest_url` (writes raw/; never compiles)."
    ),
):
    """Serve this KB to agents over MCP (read-only by default, stdio by default).

    One server serves one profile. Register it with an agent, for example:
    'claude mcp add kb -- a2kb -p <profile> mcp'. See docs/mcp.md.
    """
    from agent2kb import mcp_server

    # --token on the command line without --http is a mistake; a stray $KB_MCP_TOKEN in
    # the environment is not (it is simply ignored for stdio).
    source = ctx.get_parameter_source("token")
    if http is None and token and getattr(source, "name", "") == "COMMANDLINE":
        err_console.print("[red]✗[/red] --token needs --http.")
        raise typer.Exit(2)

    # stdout is the JSON-RPC channel in stdio mode: no log sink may write there.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    profile = get_current_profile()
    try:
        mcp_server.serve(
            profile,
            get_s3(),
            allow_llm=allow_llm,
            allow_write=allow_write,
            http=http,
            token=token,
            no_auth=no_auth,
            allow_origins=tuple(allow_origin),
            allow_hosts=tuple(allow_host),
            ingest_allow_domains=tuple(ingest_allow_domain),
        )
    except mcp_server.MCPUnavailableError as e:
        err_console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(1)
    except ValueError as e:
        err_console.print(f"[red]✗[/red] {escape(str(e))}")
        raise typer.Exit(2)


# ─── DOCTOR ───────────────────────────────────────────────────────────


@app.command(rich_help_panel="Getting started")
def doctor(
    fix: bool = typer.Option(
        False, "--fix", help="Offer to fix what can be fixed (creates a missing bucket, asks first)."
    ),
    read_only: bool = typer.Option(
        False, "--read-only", help="Skip the write/delete probe object under <prefix>_doctor/."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="With --fix: do not ask for confirmation."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Check config, S3 access, LLM backend and tools. Exit 1 if anything fails.

    Changes nothing unless --fix is given (plus a probe object removed again
    right away, unless --read-only).
    """
    from agent2kb.doctor import NotInteractive, render, run_doctor

    def confirm(question: str) -> bool:
        if yes:
            return True
        if not _stdin_is_terminal():
            raise NotInteractive
        return typer.confirm(question, err=True)

    report = run_doctor(
        get_current_profile(),
        _state.get("config_file"),
        fix=fix,
        read_only=read_only,
        confirm=confirm,
        verbose=bool(_state.get("verbose")),
    )
    if json_out:
        typer.echo(report.to_json())
    else:
        render(report, console)
    if report.failed:
        raise typer.Exit(1)


# ─── INIT ─────────────────────────────────────────────────────────────


@app.command(rich_help_panel="Getting started")
def init():
    """Create the storage layout of a KB: raw/, wiki/, outputs/, index.md, log.md and wiki/concepts/."""
    from agent2kb.skeleton import create_skeleton

    profile = get_current_profile()
    s3 = get_s3()

    created = create_skeleton(profile, s3, console)

    if created:
        console.print(f"\n[green]✓[/green] Project '{escape(profile.name)}' initialised")
    else:
        console.print(f"\n[dim]Project '{escape(profile.name)}' already initialised[/dim]")

    console.print(
        "\n[dim]Workflow: ingest → raw/ → compile → wiki/ → query → outputs/\n"
        "index.md is the catalogue, log.md is the audit trail and concepts/ holds the entity pages.\n"
        "outputs/ holds saved query answers, which are fed back into compile.[/dim]"
    )


# ─── SETUP / NEW / LIST / CONFIG ──────────────────────────────────────


def _fail(message: str, hint: str = "") -> NoReturn:
    """Config/setup problem: one line plus hint, exit 2 (same path as the profile callback)."""
    head, _, rest = message.partition("\n")
    _fail_config(head, hint or " ".join(rest.split()))


@app.command("setup", rich_help_panel="Getting started")
def setup_cmd(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Never prompt: use flags / KB_* env (Docker, CI)."
    ),
    preset: Optional[str] = typer.Option(
        None, "--preset", help="Storage: aws, r2, b2, minio, seaweedfs, wasabi or other."
    ),
    endpoint: Optional[str] = typer.Option(
        None, "--endpoint", envvar="KB_S3_ENDPOINT", help="S3 endpoint URL (not for AWS)."
    ),
    region: Optional[str] = typer.Option(None, "--region", envvar="KB_S3_REGION"),
    account_id: Optional[str] = typer.Option(None, "--account-id", help="Cloudflare account id."),
    bucket: Optional[str] = typer.Option(None, "--bucket", envvar="KB_BUCKET"),
    access_key: Optional[str] = typer.Option(
        None, "--access-key", envvar="KB_ACCESS_KEY",
        help="Prefer the env var: flags are visible in `ps`.",
    ),
    secret_key: Optional[str] = typer.Option(None, "--secret-key", envvar="KB_SECRET_KEY"),
    aws_chain: bool = typer.Option(
        False, "--aws-chain", help="No keys: use the AWS credential chain (env, ~/.aws, role)."
    ),
    addressing_style: Optional[str] = typer.Option(
        None, "--addressing-style",
        help="S3 addressing style: auto, path or virtual. The default comes from the storage preset (path for MinIO and SeaweedFS).",
    ),
    create_bucket: bool = typer.Option(
        False, "--create-bucket", help="Create the bucket if it does not exist."
    ),
    llm: Optional[str] = typer.Option(
        None, "--llm", help="claude, codex, ollama, gemini, groq or openrouter."
    ),
    model: Optional[str] = typer.Option(
        None, "--model",
        help="Model name for the selected --llm provider. Defaults to the provider's recommended model; for ollama the wizard asks when interactive.",
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="API key for gemini/groq/openrouter (or KB_<PROVIDER>_API_KEY)."
    ),
    language: Optional[str] = typer.Option(
        None, "--language", envvar="KB_OUTPUT_LANGUAGE", help="Output language (default: en)."
    ),
    allow_jina: Optional[bool] = typer.Option(
        None, "--allow-jina/--no-allow-jina", help="Allow Jina Reader (URLs go to jina.ai)."
    ),
    allow_vision: Optional[bool] = typer.Option(
        None, "--allow-cloud-vision/--no-allow-cloud-vision", help="Allow cloud image OCR."
    ),
    kb_name: Optional[str] = typer.Option(None, "--kb-name", help="Name of the first KB."),
    prefix: Optional[str] = typer.Option(None, "--prefix", help="Prefix of the first KB."),
    adopt: bool = typer.Option(False, "--adopt", help="Use a prefix that already has objects."),
    here: bool = typer.Option(
        False, "--here", help="Write .kb/config.yaml (kb: <name>) in the current directory."
    ),
):
    """First-run wizard: storage, LLM, language, privacy, then your first KB.

    Writes the v2 config (default ~/.kb/config.yaml, or $KB_CONFIG / --config) with mode
    0600 and ends with `a2kb doctor --read-only`. Refuses to overwrite an old-format
    config. Existing KBs and S3 objects are never touched.
    """
    from agent2kb.doctor import render, run_doctor
    from agent2kb.setup_wizard import SetupAbort, SetupOptions, run_setup

    if llm in ("gemini", "groq", "openrouter") and api_key is None:
        api_key = os.environ.get(f"KB_{llm.upper()}_API_KEY")
    opts = SetupOptions(
        preset=preset, endpoint=endpoint, region=region, account_id=account_id, bucket=bucket,
        access_key=access_key, secret_key=secret_key, aws_chain=aws_chain,
        addressing_style=addressing_style, create_bucket=create_bucket, llm=llm, model=model,
        api_key=api_key, language=language, allow_jina=allow_jina, allow_vision=allow_vision,
        kb_name=kb_name, prefix=prefix, adopt=adopt, here=here,
    )
    try:
        path, kb_profile = run_setup(
            opts, config_file=_state.get("config_file"), interactive=not non_interactive,
            console=console, here_dir=Path.cwd(),
        )
    except (SetupAbort, ConfigError) as e:
        _fail(str(e))
    # Check the KB as every later command will see it: KB_* variables (KB_OLLAMA_HOST, for
    # example) override the file, so checking the wizard's bare profile could test the wrong
    # endpoint.
    try:
        kb_profile = resolve_profile(kb_profile.name, path)
    except (ConfigError, ValueError, PermissionError) as e:  # ValidationError is a ValueError
        console.print(
            "[yellow]![/yellow] KB_* environment overrides could not be applied, so the check "
            f"below uses the saved config only: {escape(str(e))}"
        )
    console.print("\n[bold]Checking the new KB[/bold] (read-only) …")
    report = run_doctor(kb_profile, path, read_only=True)
    render(report, console)
    uses = "" if path == kb_config_default() else f" --config {path}"
    if report.failed:
        console.print(
            f"\n[yellow]![/yellow] KB '{escape(kb_profile.name)}' and the config were written, but the "
            "checks above found problems. Fix them and run `a2kb doctor` again."
        )
        raise typer.Exit(1)
    console.print(
        f"\n[green]✓[/green] Done. Try: [bold]a2kb{escape(uses)} -p {escape(kb_profile.name)} ingest "
        "--url <url>[/bold]",
        soft_wrap=True,
    )
    console.print(
        "[dim]First run with a sample file (ingest, compile, query, Obsidian, expected "
        "results): docs/first-run.md[/dim]",
        soft_wrap=True,
    )


def kb_config_default() -> Path:
    from agent2kb.config import default_user_config_path

    return default_user_config_path()


@app.command("new", rich_help_panel="Getting started")
def new_kb(
    name: str = typer.Argument(..., help="KB name: a-z, 0-9 and '-', 2-63 characters."),
    prefix: Optional[str] = typer.Option(
        None, "--prefix", help="S3 prefix (default: kbs/<name>/)."
    ),
    here: bool = typer.Option(
        False, "--here", help="Write .kb/config.yaml (kb: <name>) in the current directory."
    ),
    adopt: bool = typer.Option(
        False, "--adopt", help="Use a prefix that already holds objects (nothing is overwritten)."
    ),
):
    """Create a knowledge base: add it to your config and create its S3 skeleton.

    Needs the v2 config (run `a2kb setup` first). Never touches existing KBs; refuses if the
    prefix already contains objects unless --adopt. With --here and the name of a KB that
    already exists, only writes this folder's .kb/config.yaml pointer to it.
    """
    from agent2kb.setup_wizard import (
        SetupAbort,
        SetupOptions,
        attach_kb,
        create_kb,
        kb_exists,
        run_setup,
        target_path,
    )

    interactive = _stdin_is_terminal()
    try:
        path = target_path(_state.get("config_file"), interactive=interactive, console=console)
        if here and prefix is None and not adopt and kb_exists(path, name):
            attach_kb(path, name, Path.cwd(), console=console)
            console.print(
                f"\n[green]✓[/green] KB '{escape(name)}' already exists; this folder now uses it "
                "without -p.",
                soft_wrap=True,
            )
            return
        if not path.is_file() or not path.read_text().strip():
            if not interactive:
                raise SetupAbort(
                    f"No config file at {path}. Run `a2kb setup` (or `a2kb setup --non-interactive`) "
                    "first."
                )
            console.print(
                f"No config file at {escape(str(path))} yet; starting the setup wizard.", soft_wrap=True
            )
            run_setup(
                SetupOptions(kb_name=name, prefix=prefix, adopt=adopt, here=here),
                config_file=path, interactive=True, console=console, here_dir=Path.cwd(),
            )
            return
        profile = create_kb(
            path, name, prefix=prefix, adopt=adopt, here=Path.cwd() if here else None,
            console=console,
            confirm_create_bucket=(lambda q: typer.confirm(q, default=False)) if interactive
            else None,
        )
    except (SetupAbort, ConfigError) as e:
        _fail(str(e))
    uses = "" if path == kb_config_default() else f" --config {path}"
    console.print(
        f"\n[green]✓[/green] KB '{escape(profile.name)}' created "
        f"(prefix {escape(profile.prefix)}). Use it with: "
        f"[bold]a2kb{escape(uses)} -p {escape(profile.name)} ingest --url <url>[/bold]",
        soft_wrap=True,
    )
    if not here:
        console.print(
            f"[dim]Tip: to drop -p in a project folder, run `a2kb new {escape(name)} --here` "
            f"there; for an existing KB it only writes .kb/config.yaml (kb: {escape(name)}).[/dim]"
        )


@app.command("list", rich_help_panel="Getting started")
def list_kbs(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """List the knowledge bases you can use: v2 KBs, old profiles, this folder's config."""
    from agent2kb.config_cmds import show_kbs

    try:
        show_kbs(_state.get("config_file"), console, as_json=json_out)
    except ConfigError as e:
        _fail(str(e))


config_app = typer.Typer(help="Inspect and migrate configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config", rich_help_panel="Getting started")


def _selected_profile_name() -> tuple[str, str]:
    """The profile `a2kb config show` describes, and how it was selected."""
    requested = _state.get("requested_profile")
    if requested:
        return requested, "--profile"
    detected = autodetect_profile(config_path=_state.get("config_file"))
    if not detected:
        _fail(
            "No profile specified and none could be autodetected.",
            "Pass -p <name>, set KB_PROFILE, or run from a folder with a .kb/config.yaml "
            "(see `a2kb list`).",
        )
    if os.environ.get("KB_PROFILE") == detected:
        return detected, "$KB_PROFILE"
    if _find_project_config() is None:
        return detected, "the only KB in your config"
    return detected, "the project's .kb/config.yaml"


@config_app.command("show")
def config_show(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Show the effective config of the selected KB and where each value comes from.

    Secrets are masked. Layers, highest priority first: CLI flags, KB_* env, project
    .kb/config.yaml, user-level config.
    """
    from agent2kb.config_cmds import show_config

    try:
        name, how = _selected_profile_name()
        resolved = resolve_profile_detailed(
            name, _state.get("config_file"), _state.get("cli_overrides")
        )
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'config'}: {err['msg']}"
            for err in e.errors()
        )
        _fail(f"Profile '{name}' has an invalid configuration: {problems}")
    except (PermissionError, ValueError, yaml.YAMLError) as e:
        _fail(str(e))
    show_config(resolved, how, console, as_json=json_out)


@config_app.command("migrate")
def config_migrate(
    profile: Optional[str] = typer.Option(
        None, "--profile", "-p", help="Old-format profile to convert (default: the -p profile)."
    ),
    as_name: Optional[str] = typer.Option(
        None, "--as", help="Name of the KB in the v2 file (required for the old 'super')."
    ),
    to: Optional[Path] = typer.Option(
        None, "--to", help="v2 file to append to (default: the user-level config)."
    ),
    write: bool = typer.Option(
        False, "--write", help="Append to the v2 file. Without it: dry-run, prints only."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="With --write: skip the confirmation."),
):
    """Print the v2 equivalent of an old profile, or append it to a v2 file with --write.

    The old config file and the KB's S3 prefix are never changed or moved, and nothing is
    migrated automatically.
    """
    from agent2kb.config_cmds import run_migrate

    name = profile or _state.get("requested_profile")
    if not name:
        _fail("Missing --profile.", "Pass --profile <name> (the old profile to convert).")

    def confirm(question: str) -> bool:
        if not _stdin_is_terminal():
            return False
        return typer.confirm(question, default=False)

    try:
        run_migrate(
            name, config_file=_state.get("config_file"), as_name=as_name, to=to, write=write,
            yes=yes, console=console, confirm=confirm,
        )
    except (ConfigError, ValueError, PermissionError) as e:
        _fail(str(e))


# ─── CONNECT ──────────────────────────────────────────────────────────

connect_app = typer.Typer(
    help="Print the settings that connect Obsidian or an agent to a KB.", no_args_is_help=True
)
app.add_typer(connect_app, name="connect", rich_help_panel="Getting started")


def _connect_profile(kb: Optional[str]) -> ProfileConfig:
    """The KB for `a2kb connect`: the argument, else -p / $KB_PROFILE / the only KB."""
    name = kb or _state.get("requested_profile")
    if not name:
        try:
            name = autodetect_profile(config_path=_state.get("config_file"))
        except ConfigError as e:
            _fail(str(e))
    if not name:
        _fail(
            "Which KB? Give its name: `a2kb connect obsidian <kb>` (`a2kb list` shows them).",
        )
    try:
        return resolve_profile(name, _state.get("config_file"), _state.get("cli_overrides"))
    except (ConfigError, ValidationError, PermissionError, ValueError, yaml.YAMLError) as e:
        _fail(str(e))


@connect_app.command("obsidian")
def connect_obsidian(
    kb: Optional[str] = typer.Argument(None, help="KB name (default: -p / the only KB)."),
    show_secret: bool = typer.Option(
        False, "--show-secret", help="Print the secret access key (otherwise: where to read it)."
    ),
):
    """Print the Remotely Save (Obsidian) S3 settings for a KB, and the steps to use them."""
    from agent2kb import connect
    from agent2kb.config import default_user_config_path

    profile = _connect_profile(kb)
    try:
        settings = connect.obsidian_settings(
            profile, config_path=str(_state.get("config_file") or default_user_config_path())
        )
    except connect.ConnectError as e:
        _fail(str(e))
    typer.echo(connect.render_obsidian(settings, show_secret=show_secret))


@connect_app.command("agent")
def connect_agent(
    kb: Optional[str] = typer.Argument(None, help="KB name (default: -p / the only KB)."),
    url: Optional[str] = typer.Option(
        None, "--url", help="Outside Docker: the HTTP address the MCP server will have."
    ),
    show_secret: bool = typer.Option(
        False, "--show-secret", help="Print the MCP bearer token (otherwise a <token> placeholder)."
    ),
):
    """Print the `claude mcp add` command (and alternatives) that give an agent this KB."""
    from agent2kb import connect

    profile = _connect_profile(kb)
    try:
        typer.echo(
            connect.render_agent(
                connect.agent_settings(profile, url=url), show_secret=show_secret
            )
        )
    except connect.ConnectError as e:
        _fail(str(e))


# ─── BOOTSTRAP (Docker backend; see docs/docker.md) ───────────────────

bootstrap_app = typer.Typer(
    help="Docker backend bootstrap: run by docker-compose, idempotent.", no_args_is_help=True
)
app.add_typer(bootstrap_app, name="bootstrap", rich_help_panel="Admin / Docker", hidden=True)


def _bootstrap_run(fn, *args, **kwargs):
    from agent2kb.bootstrap import BootstrapError

    try:
        return fn(*args, **kwargs)
    except BootstrapError as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(EXIT_CONFIG_ERROR) from None


@bootstrap_app.command("init")
def bootstrap_init():
    """Before SeaweedFS starts: S3 identities, MCP tokens, Caddyfile (never overwrites keys)."""
    from agent2kb import bootstrap

    _bootstrap_run(bootstrap.run_init, os.environ, typer.echo)


@bootstrap_app.command("revoke")
def bootstrap_revoke(
    kb: str = typer.Argument(..., help="KB whose key(s) to disable."),
    role: Optional[str] = typer.Option(None, "--role", help="writer or obsidian (default: both)."),
):
    """Disable a KB's S3 key(s); restart SeaweedFS afterwards to apply."""
    from agent2kb import bootstrap

    _bootstrap_run(bootstrap.run_revoke, os.environ, kb, role, typer.echo)


@bootstrap_app.command("rotate")
def bootstrap_rotate(
    kb: str = typer.Argument(..., help="KB whose key(s) to replace."),
    role: Optional[str] = typer.Option(
        None, "--role", help="writer, obsidian or mcp (default: writer and obsidian)."
    ),
):
    """Issue a new random key (the old one stops working once SeaweedFS restarts)."""
    from agent2kb import bootstrap

    _bootstrap_run(bootstrap.run_rotate, os.environ, kb, role, typer.echo)


@bootstrap_app.command("provision")
def bootstrap_provision(
    wait: int = typer.Option(120, "--wait", help="Seconds to wait for S3 to answer."),
):
    """After S3 answers: bucket, config and every KB in KB_NAMES (existing ones untouched)."""
    from agent2kb import bootstrap
    from agent2kb.config import default_user_config_path

    path = _state.get("config_file") or default_user_config_path()
    endpoint = os.environ.get("KB_S3_INTERNAL_ENDPOINT") or bootstrap.INTERNAL_ENDPOINT
    result = _bootstrap_run(
        bootstrap.provision, os.environ, config_path=Path(path), console=console,
        endpoint=endpoint, wait=wait,
    )
    console.print(
        f"[green]✓[/green] KBs ready: {escape(', '.join(result.existing + result.created))}"
        + (f" (new: {escape(', '.join(result.created))})" if result.created else "")
    )
    if result.rekeyed:
        console.print(f"[green]✓[/green] New writer key stored for: {escape(', '.join(result.rekeyed))}")


@bootstrap_app.command("crontab")
def bootstrap_crontab(
    out: Path = typer.Option(..., "--out", help="File to write."),
):
    """Write the supercronic crontab for the KBs in the config."""
    from agent2kb import bootstrap
    from agent2kb.config import default_user_config_path

    path = _state.get("config_file") or default_user_config_path()
    kbs = _bootstrap_run(bootstrap.run_crontab, os.environ, Path(path), out)
    console.print(f"Scheduled: {escape(', '.join(kbs))} -> {escape(str(out))}")


@bootstrap_app.command("mcp-serve")
def bootstrap_mcp_serve():
    """One `a2kb mcp --http` per KB in KB_MCP_KBS, supervised (the kb-mcp service)."""
    from agent2kb import bootstrap
    from agent2kb.config import default_user_config_path

    path = _state.get("config_file") or default_user_config_path()
    procs = _bootstrap_run(bootstrap.mcp_procs, os.environ, Path(path))
    raise typer.Exit(bootstrap.supervise(procs, say=typer.echo))


@bootstrap_app.command("mcp-health")
def bootstrap_mcp_health():
    """Exit 1 unless every MCP server answers HTTP (the kb-mcp health check)."""
    from agent2kb import bootstrap
    from agent2kb.config import default_user_config_path

    path = _state.get("config_file") or default_user_config_path()
    procs = _bootstrap_run(bootstrap.mcp_procs, os.environ, Path(path))
    bad = bootstrap.check_mcp_ports([p.port for p in procs])
    if bad:
        err_console.print(f"No answer on port(s): {', '.join(map(str, bad))}")
        raise typer.Exit(1)


@bootstrap_app.command("health")
def bootstrap_health():
    """Read-only `a2kb doctor` for the first KB (the kb service's health check)."""
    from agent2kb import bootstrap
    from agent2kb.config import default_user_config_path
    from agent2kb.config import resolve_profile as _resolve
    from agent2kb.doctor import run_doctor

    path = Path(_state.get("config_file") or default_user_config_path())
    kbs = _bootstrap_run(bootstrap.config_kbs, path)
    if not kbs:
        err_console.print(f"Unhealthy: {escape(str(path))} has no KBs to check.")
        raise typer.Exit(1)
    try:
        report = run_doctor(_resolve(kbs[0], path, {}), path, read_only=True)
    except (ConfigError, ValidationError, PermissionError, ValueError, yaml.YAMLError) as e:
        err_console.print(f"Unhealthy: {escape(str(e))}")
        raise typer.Exit(1) from None
    if report.failed:
        # What `docker compose ps` / `docker inspect` show as the health check's output
        for line in bootstrap.health_failures(report, kbs[0]):
            err_console.print(escape(line), soft_wrap=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
