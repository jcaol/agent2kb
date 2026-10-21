"""``a2kb setup`` (first-run wizard) and ``a2kb new`` (add a knowledge base).

Both write the v2 user-level config (see ``agent2kb.config_v2``). Neither touches an
existing KB, an old-format config file or any existing S3 object.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click
import typer
from rich.markup import escape

from agent2kb import config_v2
from agent2kb.config import default_user_config_path
from agent2kb.config_v2 import ConfigError
from agent2kb.errors import translate
from agent2kb.models import LOCAL_DEFAULT_MODELS, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client

# Prefix the wizard probes with before any KB exists (S3Client wants a non-empty one).
_PROBE_PREFIX = "kbs/"


def _choice(options: list[str]) -> click.Choice:
    return click.Choice(options)


# ─── storage presets ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Preset:
    label: str
    #: Endpoint URL; ``{account_id}`` / ``{region}`` are asked for. ``None`` → AWS.
    endpoint: Optional[str] = None
    #: Ask for the endpoint itself (self-hosted servers), with this default.
    endpoint_default: Optional[str] = None
    region: Optional[str] = None  # fixed region
    region_default: Optional[str] = None  # asked, with this default
    addressing: Optional[str] = None
    checksum: Optional[str] = "when_required"
    note: str = ""


PRESETS: dict[str, Preset] = {
    "aws": Preset("Amazon S3", region_default="us-east-1", checksum=None),
    "r2": Preset(
        "Cloudflare R2",
        endpoint="https://{account_id}.r2.cloudflarestorage.com",
        region="auto",
        note=(
            "R2 tokens can be limited to a bucket but not to a prefix: use one bucket per KB "
            "if KBs must not see each other's data. Settings and caveats: "
            "docs/storage-providers.md (Cloudflare R2)."
        ),
    ),
    "b2": Preset("Backblaze B2", endpoint="https://s3.{region}.backblazeb2.com"),
    "minio": Preset("MinIO", endpoint_default="http://localhost:9000", addressing="path"),
    "seaweedfs": Preset("SeaweedFS", endpoint_default="http://localhost:8333", addressing="path"),
    "wasabi": Preset(
        "Wasabi", endpoint="https://s3.{region}.wasabisys.com", region_default="us-east-1"
    ),
    "other": Preset("Other S3-compatible", endpoint_default=None, addressing=None),
}

_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]*[a-z0-9]")


def validate_bucket_name(name: str) -> Optional[str]:
    """Return a problem description, or ``None`` if ``name`` is a valid bucket name."""
    if not 3 <= len(name) <= 63:
        return "must be 3-63 characters long"
    if not _BUCKET_RE.fullmatch(name):
        return (
            "use only lowercase letters, digits, '-' and '.', "
            "starting and ending with a letter or digit"
        )
    if ".." in name or ".-" in name or "-." in name:
        return "must not contain '..', '.-' or '-.'"
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return None
    return "must not look like an IP address"


def storage_block(
    preset: Preset,
    *,
    bucket: str,
    endpoint: Optional[str] = None,
    region: Optional[str] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    addressing_style: Optional[str] = None,
) -> dict:
    """The v2 ``storage:`` block for a preset (non-AWS presets pin ``when_required``)."""
    block: dict = {}
    if endpoint:
        block["endpoint"] = endpoint
    region = region or preset.region
    if region:
        block["region"] = region
    block["bucket"] = bucket
    if access_key and secret_key:
        block["access_key"] = access_key
        block["secret_key"] = secret_key
    if preset.checksum:
        block["checksum_mode"] = preset.checksum
    addressing = addressing_style or preset.addressing
    if addressing:
        block["addressing_style"] = addressing
    return block


# ─── options ─────────────────────────────────────────────────────────


@dataclass
class SetupOptions:
    """Answers given as flags/env; anything left ``None`` is asked (or defaulted)."""

    preset: Optional[str] = None
    endpoint: Optional[str] = None
    region: Optional[str] = None
    account_id: Optional[str] = None
    bucket: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    aws_chain: bool = False
    addressing_style: Optional[str] = None
    create_bucket: bool = False
    llm: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    language: Optional[str] = None
    allow_jina: Optional[bool] = None
    allow_vision: Optional[bool] = None
    kb_name: Optional[str] = None
    prefix: Optional[str] = None
    adopt: bool = False
    here: bool = False


LLM_CHOICES = ("claude", "codex", "ollama", "gemini", "groq", "openrouter")
_API_PROVIDERS = {
    "gemini": LLMProvider.GEMINI,
    "groq": LLMProvider.GROQ,
    "openrouter": LLMProvider.OPENROUTER,
}

_CLOUD_NOTES = {
    "claude": "Content you compile is sent to Anthropic through your Claude Code login.",
    "codex": "Content you compile is sent to OpenAI through your Codex login.",
    "gemini": (
        "Content you compile is sent to Google. On the FREE tier Google may use prompts and "
        "responses to improve its products, including training; use a paid key for private data."
    ),
    "groq": "Content you compile is sent to Groq.",
    "openrouter": "Content you compile is sent to OpenRouter and the model provider behind it.",
}


class SetupAbort(Exception):
    """Stop the wizard with a message (turned into an error exit by the command)."""


def _say(console, msg: str) -> None:
    # soft_wrap: never insert a line break inside a path or a copy-paste command.
    console.print(msg, soft_wrap=True)


# ─── target file ─────────────────────────────────────────────────────


def target_path(config_file: Optional[Path], *, interactive: bool, console) -> Path:
    """Where the v2 file goes; refuses to touch an old-format file."""
    path = Path(config_file) if config_file else default_user_config_path()
    if not path.is_file():
        return path
    raw = config_v2.read_raw(path)
    if not config_v2.is_legacy_only(raw):
        return path
    alt = path.with_name("config-v2.yaml")
    msg = (
        f"{path} is an old-format config (profiles:). agent2kb will not rewrite it, so your "
        "existing knowledge bases keep working unchanged."
    )
    if not interactive:
        raise SetupAbort(
            f"{msg}\nWrite the new file elsewhere instead, e.g. "
            f"KB_CONFIG={alt} a2kb setup --non-interactive ..."
        )
    _say(console, f"[yellow]![/yellow] {escape(msg)}")
    if not typer.confirm(f"Create the new config at {alt} instead?", default=True):
        raise SetupAbort("Nothing written.")
    if alt.exists():
        raise SetupAbort(
            f"{alt} already exists; pick another path with KB_CONFIG=<path> a2kb setup."
        )
    _say(
        console,
        f"[dim]Use it with: export KB_CONFIG={escape(str(alt))}  (while set, a2kb reads that "
        f"file instead of {escape(str(path))})[/dim]",
    )
    return alt


# ─── questions ───────────────────────────────────────────────────────


def _ask(text: str, *, default: Optional[str] = None, interactive: bool = True) -> str:
    if not interactive:
        return default or ""
    return typer.prompt(text, default=default, prompt_suffix=": ")


def ask_storage(opts: SetupOptions, *, interactive: bool, console) -> dict:
    """Build the ``storage:`` block from flags and (interactively) prompts."""
    preset_key = opts.preset
    if preset_key is None:
        if not interactive:
            preset_key = "other" if opts.endpoint else "aws"
        else:
            _say(console, "\n[bold]Where should your knowledge bases be stored?[/bold]")
            _say(console, "Any S3-compatible bucket works. Pick the closest match:")
            for key, p in PRESETS.items():
                _say(console, f"  [cyan]{key:<10}[/cyan] {p.label}")
            preset_key = typer.prompt("Storage", type=_choice(list(PRESETS)), default="aws")
    if preset_key not in PRESETS:
        raise SetupAbort(
            f"Unknown storage preset '{preset_key}'. Use one of: {', '.join(PRESETS)}."
        )
    preset = PRESETS[preset_key]
    if preset.note and interactive:
        _say(console, f"[dim]{escape(preset.note)}[/dim]")

    endpoint = opts.endpoint
    region = opts.region
    if endpoint is None:
        if preset.endpoint:
            values = {}
            if "{account_id}" in preset.endpoint:
                values["account_id"] = opts.account_id or _need(
                    "--account-id", interactive, "Cloudflare account id"
                )
            if "{region}" in preset.endpoint:
                region = region or _need(
                    "--region", interactive, "Region (e.g. us-west-004)", preset.region_default
                )
                values["region"] = region
            endpoint = preset.endpoint.format(**values)
        elif preset.endpoint_default or preset_key == "other":
            endpoint = _need("--endpoint", interactive, "Endpoint URL", preset.endpoint_default)
    if region is None and not preset.region:
        if preset.region_default:
            region = _need("--region", interactive, "Region", preset.region_default)
        elif preset_key == "other" and interactive:
            region = typer.prompt("Region (empty = none)", default="", show_default=False) or None

    bucket = opts.bucket or _need("--bucket", interactive, "Bucket name")
    problem = validate_bucket_name(bucket)
    if problem:
        raise SetupAbort(f"Bucket name '{bucket}' {problem}.")

    access_key, secret_key = opts.access_key, opts.secret_key
    if bool(access_key) != bool(secret_key):
        raise SetupAbort("Give both access key and secret key, or neither.")
    if not access_key and not opts.aws_chain:
        if interactive:
            _say(
                console,
                "\n[bold]Credentials[/bold] — stored only in your user-level config "
                "(mode 0600), never in a project folder.",
            )
            if typer.confirm(
                "Use the AWS credential chain (env vars, ~/.aws, IAM role) instead of keys?",
                default=(preset_key == "aws"),
            ):
                access_key = secret_key = None
            else:
                access_key = typer.prompt("Access key id")
                secret_key = typer.prompt("Secret access key", hide_input=True)
        else:
            raise SetupAbort(
                "Credentials missing: pass --access-key/--secret-key (or KB_ACCESS_KEY / "
                "KB_SECRET_KEY), or --aws-chain to use the AWS credential chain."
            )
    return storage_block(
        preset,
        bucket=bucket,
        endpoint=endpoint,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        addressing_style=opts.addressing_style,
    )


def _need(flag: str, interactive: bool, question: str, default: Optional[str] = None) -> str:
    if not interactive:
        if default:
            return default
        raise SetupAbort(f"Missing {flag} (required with --non-interactive).")
    return typer.prompt(question, default=default, prompt_suffix=": ")


def probe_profile(
    storage: dict, name: str = "setup", prefix: str = _PROBE_PREFIX
) -> ProfileConfig:
    """A throwaway profile over ``storage`` for connectivity checks."""
    data = config_v2.normalize_storage(storage, "storage")
    return ProfileConfig(name=name, prefix=prefix, **data)


def bucket_state(s3: S3Client, profile: ProfileConfig) -> Optional[bool]:
    """Whether the bucket exists: ``True``, ``False`` (404) or ``None`` (cannot inspect it).

    HeadBucket takes no prefix, so an identity whose ``ListBucket`` is limited to the KB's
    prefix (the policy docs/storage-providers.md recommends) is denied it. A 403 there is
    therefore not evidence of bad credentials: like `a2kb doctor`, fall back to listing the
    KB's own prefix. Only if that is denied too is the error raised. Other errors raise.
    """
    try:
        return s3.bucket_exists()
    except Exception as e:  # noqa: BLE001 - re-raised unless it is a HeadBucket 403
        friendly = translate(e, profile)
        if friendly is None or friendly.code != "access_denied":
            raise
    try:
        s3.list_one()
    except Exception as e:  # noqa: BLE001
        friendly = translate(e, profile)
        if friendly is not None and friendly.code == "no_such_bucket":
            return False
        raise
    return None


def check_bucket(
    profile: ProfileConfig,
    *,
    create: bool,
    interactive: bool,
    console,
    client: Optional[S3Client] = None,
) -> None:
    """Connectivity check; offers to create a missing bucket (asks first)."""
    s3 = client or S3Client(profile)
    try:
        exists = bucket_state(s3, profile)
    except Exception as e:  # noqa: BLE001 - shown as a friendly message
        friendly = translate(e, profile)
        if friendly is None:
            raise
        raise SetupAbort(f"{friendly.message}\n{friendly.hint}") from None
    if exists:
        _say(console, f"  [green]✓[/green] Connected; bucket '{escape(profile.bucket)}' exists")
        return
    if exists is None:
        _say(
            console,
            f"  [green]✓[/green] Connected; '{escape(profile.prefix)}' in bucket "
            f"'{escape(profile.bucket)}' can be listed",
        )
        _say(
            console,
            "  [dim]The bucket itself cannot be inspected (HeadBucket denied); this is normal "
            "for keys scoped to a prefix.[/dim]",
        )
        return
    where = profile.s3_endpoint or "AWS S3"
    if interactive:
        ok = typer.confirm(
            f"Bucket '{profile.bucket}' does not exist at {where}. Create it?", default=False
        )
    else:
        ok = create
    if not ok:
        raise SetupAbort(
            f"Bucket '{profile.bucket}' does not exist at {where}. Create it in your provider's "
            "console, or re-run with --create-bucket."
        )
    try:
        s3.create_bucket()
    except Exception as e:  # noqa: BLE001
        friendly = translate(e, profile)
        if friendly is None:
            raise
        raise SetupAbort(f"{friendly.message}\n{friendly.hint}") from None
    _say(console, f"  [green]✓[/green] Created bucket '{escape(profile.bucket)}'")


# ─── LLM ─────────────────────────────────────────────────────────────


def detect_llms() -> dict[str, tuple[str, str]]:
    """Status per backend (``ok`` / ``warn`` / ``fail``) and detail, via `a2kb doctor` checks.

    The CLI checks run ``<cli> auth status`` / ``codex login status`` from an empty temp
    dir; they read no tokens and spend nothing. Ollama is probed at its default host.
    """
    from agent2kb import doctor

    out: dict[str, tuple[str, str]] = {}
    for cmd in ("claude", "codex"):
        report = doctor.Report()
        doctor._check_cli_login(cmd, report, required=False)
        c = report.checks[-1]
        out[cmd] = (c.status, c.detail)
    report = doctor.Report()
    doctor._check_ollama(ProfileConfig(name="setup", bucket="x", prefix="x/"), report)
    out["ollama"] = (report.checks[-1].status, report.checks[-1].detail)
    return out


def _default_model(provider: LLMProvider) -> Optional[str]:
    try:
        from agent2kb.compile.compiler import DEFAULT_MODELS
    except ImportError:  # optional provider SDKs missing: leave the model to the default
        return None
    return DEFAULT_MODELS.get(provider)


_LOGIN_COMMAND = {"claude": "claude auth login", "codex": "codex login"}


def _backend_ready(choice: str, found: dict, console) -> bool:
    """True if ``choice`` is usable now, or the user says to continue anyway."""
    status, detail = found.get(choice, ("ok", ""))
    if status == "ok" or choice not in ("claude", "codex", "ollama"):
        return True
    if choice == "ollama":
        problem = "Ollama is not reachable at http://localhost:11434 (start it with: ollama serve)."
    elif "not found" in detail:
        problem = (
            f"'{choice}' is not installed (not on PATH). Install it, then log in with: "
            f"{_LOGIN_COMMAND[choice]}"
        )
    else:
        problem = (
            f"'{choice}' is installed but not logged in. Log in with: {_LOGIN_COMMAND[choice]}"
        )
    _say(console, f"[yellow]![/yellow] {escape(problem)}")
    return typer.confirm("Choose it anyway (you can fix this later)?", default=False)


def ask_llm(opts: SetupOptions, *, interactive: bool, console) -> dict:
    """Build the ``llm:`` block. CLI backends reuse their own login: no key is stored."""
    choice = opts.llm
    if choice is None:
        if not interactive:
            raise SetupAbort(
                f"Missing --llm (one of: {', '.join(LLM_CHOICES)}) with --non-interactive."
            )
        found = detect_llms()
        _say(console, "\n[bold]Which LLM compiles your wiki pages?[/bold]")
        for key in LLM_CHOICES:
            status, detail = found.get(key, ("", ""))
            mark = {
                "ok": "[green]detected[/green]",
                "warn": "[yellow]found, needs login[/yellow]",
                "fail": "[dim]not detected[/dim]",
            }.get(status, "")
            if key in ("claude", "codex"):
                if status == "ok":
                    mark = "[green]logged in[/green]"
                elif "not found" in detail:
                    mark = "[dim]not installed[/dim]"
                else:
                    mark = "[yellow]installed, not logged in[/yellow]"
            _say(console, f"  [cyan]{key:<11}[/cyan] {mark}")
        default = next((k for k in LLM_CHOICES if found.get(k, ("",))[0] == "ok"), "claude")
        while True:
            choice = typer.prompt("LLM", type=_choice(list(LLM_CHOICES)), default=default)
            if _backend_ready(choice, found, console):
                break
            default = choice
    if choice not in LLM_CHOICES:
        raise SetupAbort(f"Unknown --llm '{choice}'. Use one of: {', '.join(LLM_CHOICES)}.")

    if choice in _CLOUD_NOTES:
        _say(console, f"[yellow]Privacy:[/yellow] {escape(_CLOUD_NOTES[choice])}")
    block: dict
    if choice in ("claude", "codex"):
        block = {"provider": "cli", "command": choice}
    elif choice == "ollama":
        default = LOCAL_DEFAULT_MODELS[LLMProvider.OLLAMA]
        if interactive and not opts.model:
            _say(
                console,
                f"[dim]{default} is a 70B model (tens of GB of disk and memory); a smaller "
                "model you have pulled (`ollama list`) also works.[/dim]",
            )
        model = opts.model or (_ask("Ollama model", default=default) if interactive else default)
        # Always stored: without it the profile would fall back to another provider's model.
        return {"provider": "ollama", "model": model}
    else:
        provider = _API_PROVIDERS[choice]
        block = {"provider": provider.value}
        key = opts.api_key
        if key is None and interactive:
            key = typer.prompt(
                f"{choice} API key (empty = set KB_{choice.upper()}_API_KEY later)",
                hide_input=True,
                default="",
                show_default=False,
                prompt_suffix=": ",
            )
        if key:
            block[f"{choice}_api_key"] = key
        model = opts.model or _default_model(provider)
        if model:
            block["model"] = model
        return block
    if opts.model:
        block["model"] = opts.model
    return block


def ask_defaults(opts: SetupOptions, *, interactive: bool, console) -> dict:
    """Build the ``defaults:`` block: language and privacy-first ingest settings."""
    language = opts.language
    if language is None:
        language = _ask(
            "Language of wiki pages and answers (en, de, es, fr, ja, ko, ... or any language)",
            default="en",
            interactive=interactive,
        )
    jina = opts.allow_jina
    vision = opts.allow_vision
    if interactive and (jina is None or vision is None):
        _say(console, "\n[bold]Privacy[/bold] — both are OFF unless you opt in:")
    if jina is None:
        jina = interactive and typer.confirm(
            "Let `a2kb ingest --url` use the Jina Reader service (r.jina.ai)? Every URL you "
            "ingest is sent to jina.ai to extract its text.",
            default=False,
        )
    if vision is None:
        vision = interactive and typer.confirm(
            "Let `a2kb ingest` send images to a cloud vision model for OCR? It tries the "
            "`claude` or `codex` CLI the KB compiles with (Anthropic / OpenAI), then Gemini, "
            "then the `claude` CLI. The image content leaves your machine.",
            default=False,
        )
    return {
        "output_language": language,
        "allow_jina_reader": bool(jina),
        "allow_cloud_vision": bool(vision),
    }


# ─── create a KB ─────────────────────────────────────────────────────


def plan_kb(
    raw: dict,
    path: Path,
    name: str,
    *,
    prefix: Optional[str],
    here: Optional[Path],
    extra: Optional[dict] = None,
) -> tuple[dict, ProfileConfig]:
    """Validate a new KB against ``raw`` without any side effect.

    ``extra`` is merged into the KB's entry (``a2kb bootstrap`` uses it for per-KB keys).

    Returns the config dict with the KB added and the KB's profile. Raises ``ConfigError``
    for a bad or taken name, a bad or overlapping prefix, or a pointer that cannot be written.
    """
    config_v2.validate_kb_name(name, strict=True)
    cfg = config_v2.parse_v2(raw, path)
    if name in cfg.kbs or name in cfg.legacy_profiles:
        raise ConfigError(f"KB '{name}' already exists in {path}; nothing changed.")

    entry: dict = {}
    if prefix is not None:
        entry["prefix"] = config_v2.validate_prefix(prefix)
    entry.update(extra or {})
    trial = config_v2.with_kb(raw, name, entry)
    trial_cfg = config_v2.parse_v2(trial, path)
    profile = config_v2.build_profile(trial_cfg, name)

    clash = config_v2.find_prefix_conflict(trial_cfg, name, profile.prefix, profile.bucket)
    if clash:
        raise ConfigError(
            f"Prefix '{profile.prefix}' overlaps KB '{clash}'. Pick a different name or --prefix."
        )
    if here is not None:
        _check_pointer(here, name, "Run `a2kb new` without --here and edit")
    return trial, profile


def _check_pointer(here: Path, name: str, advice: str) -> None:
    pointer = Path(here) / ".kb" / "config.yaml"
    if pointer.exists() and config_v2.read_raw(pointer) != {config_v2.POINTER_KEY: name}:
        raise ConfigError(
            f"{pointer} already exists; not overwriting it. {advice} it by hand to contain "
            f"only `kb: {name}`."
        )


def kb_exists(path: Path, name: str) -> bool:
    """True if the v2 file at ``path`` already defines KB ``name`` (no side effect)."""
    if not path.is_file():
        return False
    raw = config_v2.read_raw(path)
    if not raw or config_v2.is_legacy_only(raw):
        return False
    return name in config_v2.parse_v2(raw, path).kbs


def attach_kb(path: Path, name: str, here: Path, *, console) -> ProfileConfig:
    """Point ``here`` at the existing KB ``name``: write ``.kb/config.yaml`` and nothing else.

    No S3 call, no change to the user-level file. Refuses to replace a pointer (or an old
    flat config) that says something else.
    """
    raw = config_v2.read_raw(path)
    cfg = config_v2.parse_v2(raw, path)
    if name not in cfg.kbs:
        raise ConfigError(f"KB '{name}' is not defined in {path}.")
    profile = config_v2.build_profile(cfg, name)
    _check_pointer(here, name, "Edit")
    target = config_v2.write_pointer(here, name)
    _say(
        console,
        f"  [green]✓[/green] Wrote {escape(str(target))} (kb: {escape(name)}; no secrets)",
    )
    return profile


def create_kb(
    path: Path,
    name: str,
    *,
    prefix: Optional[str] = None,
    adopt: bool = False,
    here: Optional[Path] = None,
    console,
    confirm_create_bucket: Optional[Callable[[str], bool]] = None,
    client_factory: Callable[[ProfileConfig], S3Client] = S3Client,
    base_raw: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> ProfileConfig:
    """Add KB ``name`` to the v2 file at ``path`` and create its S3 skeleton.

    Never touches existing KBs, old-format files or objects: refuses when the name or an
    overlapping prefix exists, and when the prefix already holds objects (unless ``adopt``).
    ``base_raw`` (used by ``a2kb setup``) is the not-yet-written config to extend; the file is
    then written once, here, after every check has passed. ``extra`` adds settings to the KB's
    entry (``client_factory`` may then sign with other keys than the entry's own).
    """
    from agent2kb.skeleton import create_skeleton

    if base_raw is None:
        if not path.is_file():
            raise ConfigError(f"No config file at {path}. Run `a2kb setup` first.")
        raw = config_v2.read_raw(path)
        if config_v2.is_legacy_only(raw):
            raise ConfigError(
                f"{path} is an old-format config (profiles:); agent2kb will not modify it. Point "
                "KB_CONFIG at a v2 file (`a2kb setup` creates one) and run `a2kb new` again."
            )
    else:
        raw = base_raw
    trial, profile = plan_kb(raw, path, name, prefix=prefix, here=here, extra=extra)

    s3 = client_factory(profile)
    try:
        if bucket_state(s3, profile) is False:
            where = profile.s3_endpoint or "AWS S3"
            if confirm_create_bucket and confirm_create_bucket(
                f"Bucket '{profile.bucket}' does not exist at {where}. Create it?"
            ):
                s3.create_bucket()
                _say(console, f"  [green]✓[/green] Created bucket '{escape(profile.bucket)}'")
            else:
                raise ConfigError(
                    f"Bucket '{profile.bucket}' does not exist. Create it, or run `a2kb doctor "
                    "--fix` after adding the KB."
                )
        existing = s3.list_one()
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001
        friendly = translate(e, profile)
        if friendly is None:
            raise
        raise ConfigError(f"{friendly.message} {friendly.hint}") from None
    if existing and not adopt:
        raise ConfigError(
            f"'{profile.prefix}' already contains objects (e.g. {existing[0]}). Nothing was "
            "changed. Use --adopt to use that data as this KB (existing objects are never "
            "overwritten), or choose another name/--prefix."
        )
    if existing:
        _say(
            console, f"[yellow]![/yellow] Adopting existing data under '{escape(profile.prefix)}'."
        )

    _say(console, f"[bold]Creating KB '{escape(name)}'[/bold] at {escape(profile.prefix)}")
    create_skeleton(profile, s3, console)
    config_v2.write_v2(path, trial)
    if base_raw is not None:
        _say(console, f"  [green]✓[/green] Wrote {escape(str(path))} (mode 600)")
    if here is not None:
        config_v2.write_pointer(here, name)
        _say(
            console,
            f"  [green]✓[/green] Wrote {escape(str(Path(here) / '.kb' / 'config.yaml'))} "
            f"(kb: {name}; no secrets)",
        )
    return profile


# ─── a2kb setup ────────────────────────────────────────────────────────


def _differs(given, stored) -> bool:
    return given is not None and given != stored


def stored_conflicts(
    raw: dict, path: Path, opts: SetupOptions, labels: Optional[dict[str, str]] = None
) -> list[str]:
    """Flags that contradict settings already stored in an existing v2 file.

    Secrets are named, never printed. Returns one line per conflict. ``labels`` renames
    flags in the messages (``a2kb bootstrap`` reports its ``KB_*`` variables instead).
    """
    from agent2kb.language import effective_language

    out: list[str] = []

    def add(flag: str, given, stored, block: str, secret: bool = False) -> None:
        flag = (labels or {}).get(flag, flag)
        if secret:
            out.append(f"{flag} differs from the {block} stored in {path}")
        else:
            out.append(f"{flag} {given!r} conflicts with the {block} stored in {path} ({stored!r})")

    if raw.get("storage"):
        st = config_v2.normalize_storage(raw["storage"], "storage")
        for flag, given, key in (
            ("--bucket", opts.bucket, "bucket"),
            ("--endpoint", opts.endpoint, "s3_endpoint"),
            ("--region", opts.region, "region"),
            ("--addressing-style", opts.addressing_style, "addressing_style"),
        ):
            if _differs(given, st.get(key)):
                add(flag, given, st.get(key), f"storage {key}")
        for flag, given, key in (
            ("--access-key", opts.access_key, "access_key"),
            ("--secret-key", opts.secret_key, "secret_key"),
        ):
            if _differs(given, st.get(key)):
                add(flag, given, None, f"storage {key}", secret=True)
    if raw.get("llm"):
        lm = config_v2.normalize_llm(raw["llm"], "llm")
        if opts.llm is not None:
            if opts.llm in ("claude", "codex"):
                ok = lm.get("compile_provider") == "cli" and lm.get("cli_command") == opts.llm
            else:
                ok = lm.get("compile_provider") == opts.llm
            if not ok:
                stored = lm.get("compile_provider")
                if stored == "cli":
                    stored = f"cli ({lm.get('cli_command', 'claude')})"
                add("--llm", opts.llm, stored, "llm provider")
        if _differs(opts.model, lm.get("default_model")):
            add("--model", opts.model, lm.get("default_model"), "llm model")
        if opts.llm in _API_PROVIDERS and _differs(opts.api_key, lm.get(f"{opts.llm}_api_key")):
            add("--api-key", None, None, f"{opts.llm} API key", secret=True)
    if raw.get("defaults"):
        df = config_v2.normalize_fields(raw["defaults"], "defaults", kb=False)
        if opts.language is not None and effective_language(opts.language) != effective_language(
            df.get("output_language")
        ):
            add("--language", opts.language, df.get("output_language"), "output_language")
        for flag, given, key in (
            ("--allow-jina/--no-allow-jina", opts.allow_jina, "allow_jina_reader"),
            (
                "--allow-cloud-vision/--no-allow-cloud-vision",
                opts.allow_vision,
                "allow_cloud_vision",
            ),
        ):
            if given is not None and bool(given) != bool(df.get(key, True)):
                add(flag, given, df.get(key, True), key)
    return out


def run_setup(
    opts: SetupOptions,
    *,
    config_file: Optional[Path],
    interactive: bool,
    console,
    here_dir: Optional[Path] = None,
) -> tuple[Path, ProfileConfig]:
    """Write/extend the v2 file, create the first KB; returns ``(file, kb profile)``.

    Every input is gathered and validated first; only then come side effects (creating the
    bucket after confirmation, S3 skeleton), and the config file is written exactly once.
    """
    path = target_path(config_file, interactive=interactive, console=console)
    raw = config_v2.read_raw(path) if path.is_file() else {}
    if raw and not config_v2.is_v2(raw):  # empty-but-existing file
        raw = {}
    existing = config_v2.parse_v2(raw, path) if raw else None

    _say(console, "[bold]a2kb setup[/bold] — creates your config and first knowledge base (KB)")
    _say(console, f"[dim]Config file: {escape(str(path))}[/dim]")

    conflicts = stored_conflicts(raw, path, opts) if existing else []
    if conflicts:
        alt = path.with_name("config-v2.yaml") if path.name != "config-v2.yaml" else "<other path>"
        raise SetupAbort(
            "; ".join(conflicts) + ".\n"
            f"Edit {path} by hand to change those settings, or write a separate file: "
            f"KB_CONFIG={alt} a2kb setup ..."
        )

    # 1. Gather and validate everything (no side effects yet).
    storage = (existing.raw.get("storage") if existing else None) or None
    if storage:
        _say(console, "Keeping the existing storage settings in that file.")
    else:
        storage = ask_storage(opts, interactive=interactive, console=console)
    probe_profile(storage)  # validates the storage block before anything else is asked

    llm = (existing.raw.get("llm") if existing else None) or None
    if llm:
        _say(console, "Keeping the existing llm settings.")
    else:
        llm = ask_llm(opts, interactive=interactive, console=console)
    defaults = (existing.raw.get("defaults") if existing else None) or None
    if defaults:
        _say(console, "Keeping the existing defaults.")
    else:
        defaults = ask_defaults(opts, interactive=interactive, console=console)

    kb_name = opts.kb_name
    if kb_name is None:
        kb_name = _ask(
            "Name of your first knowledge base (a-z, 0-9, '-')",
            default="notes",
            interactive=interactive,
        )
    base = config_v2.new_raw(storage, llm, defaults) if not raw else dict(raw)
    if raw:
        base.setdefault("storage", storage)
        base.setdefault("llm", llm)
        base.setdefault("defaults", defaults)
    _, planned = plan_kb(
        base, path, kb_name, prefix=opts.prefix, here=here_dir if opts.here else None
    )
    # Probe with the KB's real prefix: a key scoped to that prefix may list nothing else.
    probe = probe_profile(storage, prefix=planned.prefix)

    # 2. Side effects: bucket (confirmed), then one write of the file plus the KB skeleton.
    check_bucket(probe, create=opts.create_bucket, interactive=interactive, console=console)

    def confirm_bucket(q: str) -> bool:
        return typer.confirm(q, default=False) if interactive else opts.create_bucket

    profile = create_kb(
        path,
        kb_name,
        prefix=opts.prefix,
        adopt=opts.adopt,
        here=here_dir if opts.here else None,
        console=console,
        confirm_create_bucket=confirm_bucket,
        base_raw=base,
    )
    return path, profile
