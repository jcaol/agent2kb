"""Status command: show profile info, recent activity, and stats."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agent2kb.cli_models import cli_family, preflight_cli_model
from agent2kb.cli_runner import fallback_target_for, target_for
from agent2kb.errors import translate
from agent2kb.language import describe_output_language
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client, wiki_key_for_raw
from agent2kb.utils import content_hash


def _cloud_fallback_note(provider: LLMProvider) -> str:
    """What `compile --allow-cloud-fallback` would change for this provider."""
    if provider in (LLMProvider.OLLAMA, LLMProvider.GEMMA4):
        return (
            "off: a failing local provider never falls back to a cloud LLM "
            "(enable with `compile --allow-cloud-fallback`)"
        )
    if provider is LLMProvider.CLI:
        return "not used: the CLI provider falls back only to its own fallback CLI"
    return "not applicable: the compile provider is already a cloud service"


def show_status(profile: ProfileConfig, s3: S3Client, console: Console) -> bool:
    """Display profile status with rich formatting.

    Returns False when the bucket could not be read, so the command can exit non-zero.
    """
    # Profile info table
    info_table = Table(title=f"Profile: {profile.name}", show_header=False, padding=(0, 2))
    info_table.add_column("Field", style="bold cyan")
    info_table.add_column("Value")

    info_table.add_row("S3 Endpoint", escape(profile.s3_endpoint or "(AWS S3)"))
    info_table.add_row("Bucket", escape(profile.bucket))
    info_table.add_row("Prefix", escape(profile.prefix or "(whole bucket — admin)"))
    info_table.add_row("Compile Provider", profile.compile_provider.value)
    info_table.add_row("Default Model", escape(profile.default_model or ""))
    info_table.add_row("Output Language", describe_output_language(profile.output_language))

    # Preflight the codex CLI model. Codex rejects retired slugs with HTTP 400,
    # so surface a bad pin here rather than letting a cron compile die on it.
    cli_problems: list[str] = []
    if profile.compile_provider is LLMProvider.CLI:
        compile_t = target_for(profile, "compile")
        aux_t = target_for(profile, "aux")
        fallback_t = fallback_target_for(profile)
        info_table.add_row("CLI Compile", escape(str(compile_t)))
        info_table.add_row("CLI Aux", escape(str(aux_t)))
        if fallback_t and fallback_t.command == compile_t.command:
            # CliRouter drops a fallback that is the same CLI as the primary.
            info_table.add_row(
                "CLI Fallback",
                f"ignored: same CLI as the primary ({escape(str(fallback_t))}); "
                "use a different CLI or leave it empty",
            )
            fallback_t = None
        else:
            info_table.add_row(
                "CLI Fallback", escape(str(fallback_t)) if fallback_t else "(disabled)"
            )
        for t in dict.fromkeys(x for x in (compile_t, aux_t, fallback_t) if x):
            cli_problems += preflight_cli_model(t.model, t.effort, t.command)
    info_table.add_row("Cloud Fallback", _cloud_fallback_note(profile.compile_provider))
    info_table.add_row("Ollama Host", escape(profile.ollama_host or ""))

    console.print(info_table)
    for problem in cli_problems:
        console.print(f"  [yellow]![/yellow] {escape(str(problem))}")
    if profile.compile_provider is LLMProvider.CLI:
        commands = [profile.cli_command, profile.cli_fallback_command]
        if any(cli_family(c) == "claude" for c in commands if c):
            console.print(
                "  [dim]Note: Claude Code org-managed instructions, if any, take "
                "precedence over output_language.[/dim]"
            )
    console.print()

    # File counts
    try:
        # Two listings answer file counts, existence and sizes for the whole
        # KB. Downloading every object to measure it costs one round trip per
        # file, which on a few thousand pages is minutes; the listing is under
        # a second and carries size and ETag already.
        quarantine = f"{profile.prefix}raw/.failed/"
        raw_objects = [
            o for o in s3.list_objects(sub_prefix="raw/", suffix=".md")
            if not o.key.startswith(quarantine)
        ]
        wiki_objects = s3.list_objects(sub_prefix="wiki/", suffix=".md")
        raw_files = [o.key for o in raw_objects]
        wiki_files = [o.key for o in wiki_objects]
        existing_wiki = {o.key for o in wiki_objects}

        stats_table = Table(title="Knowledge Base Stats", padding=(0, 2))
        stats_table.add_column("Metric", style="bold")
        stats_table.add_column("Count", justify="right")

        # Count files needing compile: wiki missing OR raw content changed.
        # Only pairs where the wiki page exists need their bodies compared,
        # and those are fetched in parallel.
        # Derived relative to the prefix: a "/raw/" -> "/wiki/" replace is a
        # no-op for a whole-bucket KB, whose keys start with "raw/".
        def _wiki_for(raw_key: str) -> str:
            try:
                return wiki_key_for_raw(profile.prefix, raw_key)
            except ValueError:
                return ""  # malformed key: never matches, so it counts as pending

        wiki_for = {r: _wiki_for(r) for r in raw_files}
        needs_compile = 0
        to_verify = []
        for r in raw_files:
            if wiki_for[r] in existing_wiki:
                to_verify.append(r)
            else:
                needs_compile += 1

        if to_verify:
            bodies = s3.download_many(to_verify + [wiki_for[r] for r in to_verify])
            for r in to_verify:
                raw_content = bodies.get(r)
                wiki_content = bodies.get(wiki_for[r])
                if raw_content is None or wiki_content is None:
                    needs_compile += 1
                    continue
                try:
                    wiki_fm, _ = Frontmatter.from_yaml_block(wiki_content)
                except Exception:
                    needs_compile += 1
                    continue
                if wiki_fm.raw_content_hash != content_hash(raw_content):
                    needs_compile += 1

        # Token efficiency: raw vs wiki volume, taken from the listing.
        raw_total_bytes = sum(o.size for o in raw_objects)
        skip = {"index.md", "log.md", ".gitkeep"}
        wiki_total_bytes = sum(
            o.size for o in wiki_objects if o.key.split("/")[-1] not in skip
        )

        stats_table.add_row("Raw files", str(len(raw_files)))
        stats_table.add_row("Wiki pages", str(len(wiki_files)))
        stats_table.add_row("Needs compile", str(needs_compile))

        # Show token efficiency
        if raw_total_bytes > 0 and wiki_total_bytes > 0:
            # ~4 bytes per token (rough estimate for mixed EN/ZH UTF-8)
            raw_tokens = raw_total_bytes // 4
            wiki_tokens = wiki_total_bytes // 4
            ratio = raw_total_bytes / wiki_total_bytes if wiki_total_bytes else 0
            stats_table.add_row("", "")
            stats_table.add_row("Raw size", f"{raw_total_bytes:,} bytes (~{raw_tokens:,} tok)")
            stats_table.add_row("Wiki size", f"{wiki_total_bytes:,} bytes (~{wiki_tokens:,} tok)")
            if ratio > 1:
                savings = 100 - (100 / ratio)
                stats_table.add_row("Query savings", f"{ratio:.1f}x ({savings:.0f}% fewer tokens)")
            else:
                stats_table.add_row("KB growth", f"{1/ratio:.1f}x (enriched beyond sources)")

        console.print(stats_table)
        console.print()

        # Recent files
        # Timestamps come from the listing too — no extra HEAD per file.
        if raw_objects:
            console.print("[bold]Recent raw files:[/bold]")
            for obj in raw_objects[-5:]:
                console.print(f"  {escape(obj.key)}  [dim]({obj.last_modified or 'unknown'})[/dim]")

        if wiki_objects:
            console.print("\n[bold]Recent wiki pages:[/bold]")
            for obj in wiki_objects[-5:]:
                console.print(f"  {escape(obj.key)}  [dim]({obj.last_modified or 'unknown'})[/dim]")

    except Exception as e:
        friendly = translate(e, profile)
        if friendly:
            console.print(f"[red]✗[/red] {escape(friendly.message)}")
            console.print(f"[dim]{escape(friendly.hint)}[/dim]")
        else:
            console.print(f"[red]✗[/red] Cannot read the bucket: {escape(str(e))}")
            console.print("[dim]Check your S3 endpoint and credentials (`a2kb doctor`).[/dim]")
        return False
    return True
