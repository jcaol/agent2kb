"""`a2kb doctor`: diagnose config, storage, LLM backend and external tools.

Read-only by default. Each check yields ok / fail / warn / skip with a fix
hint. The only mutations are the probe object under ``<prefix>_doctor/`` (skip
with ``--read-only``) and bucket creation under ``--fix`` after confirmation.
Secrets are never printed — only whether they are set.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yaml

from agent2kb import config as kb_config
from agent2kb.cli_models import cli_family, preflight_cli_model
from agent2kb.errors import translate
from agent2kb.extras import DIST_NAME
from agent2kb.models import LLMProvider, ProfileConfig, bucket_name_problem
from agent2kb.s3_client import S3Client, build_boto_config

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
_SYMBOL = {OK: "✓", FAIL: "✗", WARN: "!", SKIP: "–"}

#: Seconds for a login-status subprocess or the Ollama probe.
_PROBE_TIMEOUT = 20


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    # Underlying error code/message and traceback; only filled under `a2kb -v doctor`.
    debug: str = ""


class NotInteractive(Exception):
    """Raised by a ``confirm`` callback when there is no terminal to ask on."""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    verbose: bool = False

    def add(
        self,
        section: str,
        name: str,
        status: str,
        detail: str = "",
        hint: str = "",
        debug: str = "",
    ) -> Check:
        check = Check(section, name, status, detail, hint, debug if self.verbose else "")
        self.checks.append(check)
        return check

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def to_dict(self) -> dict:
        checks = [asdict(c) for c in self.checks]
        if not self.verbose:
            for c in checks:
                del c["debug"]
        return {"ok": not self.failed, "checks": checks}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


# ─── config ──────────────────────────────────────────────────────────


def _mode_warning(path: Path) -> Optional[str]:
    """Return a note when ``path`` is readable by group/other (POSIX only)."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if os.name == "posix" and mode & 0o077:
        return f"{path} is mode {mode:03o} (group/world accessible)"
    return None


def _file_defines_profile(path: Path, name: str) -> bool:
    """Whether the config file at ``path`` contributes to profile ``name``.

    A ``profiles:`` file counts if it has that profile; a flat project file
    (no ``profiles:``) counts if it has any settings, as it applies to the profile
    the project selects.
    """
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict):
        return False
    if "profiles" in data:
        profiles = data["profiles"]
        return isinstance(profiles, dict) and name in profiles
    return bool(data)


def _is_v2_kb(path: Path, name: str) -> bool:
    """Whether ``name`` is a KB of the v2 user-level file at ``path`` (not an old profile)."""
    try:
        raw = kb_config.config_v2.read_raw(path)
    except (OSError, kb_config.ConfigError):
        return False
    return kb_config.config_v2.is_v2(raw) and name in (raw.get("kbs") or {})


def check_config(profile: ProfileConfig, config_file: Optional[Path], report: Report) -> None:
    sec = "Config"
    user_path = config_file or kb_config.default_user_config_path()
    project_path = kb_config._find_project_config()
    env_set = sorted(
        kb_config.ENV_PREFIX + k
        for k in kb_config.ENV_MAP
        if kb_config.ENV_PREFIX + k in os.environ
    )

    sources = []
    if _file_defines_profile(user_path, profile.name):
        sources.append(f"user {user_path}")
    if project_path and _file_defines_profile(project_path, profile.name):
        sources.append(f"project {project_path}")
    if env_set:
        sources.append("env " + ", ".join(env_set))
    if not sources:
        report.add(
            sec,
            "profile",
            WARN,
            f"profile '{profile.name}' came from CLI flags only",
            "Create ~/.kb/config.yaml or .kb/config.yaml (see docs/storage-providers.md).",
        )
    else:
        report.add(sec, "profile", OK, f"'{profile.name}' resolved from " + "; ".join(sources))

    if profile.is_super:
        report.add(sec, "prefix", OK, "admin KB (whole bucket)")
    elif not profile.prefix:
        report.add(
            sec,
            "prefix",
            FAIL,
            "empty prefix on a non-admin KB",
            "Set `prefix:` e.g. 'projects/<name>/' so this KB is isolated.",
        )
    elif (
        profile.prefix.startswith("/")
        or "//" in profile.prefix
        or ".." in profile.prefix.split("/")
    ):
        report.add(
            sec,
            "prefix",
            FAIL,
            f"suspicious prefix '{profile.prefix}'",
            "Use a relative prefix without leading '/', '//' or '..', e.g. 'projects/<name>/'.",
        )
    else:
        report.add(sec, "prefix", OK, f"'{profile.prefix}'")

    pointer = False
    if project_path and project_path.is_file():
        try:
            pointer = kb_config.config_v2.is_pointer(kb_config.config_v2.read_raw(project_path))
        except kb_config.ConfigError:
            pass
    # A pointer (`kb: <name>`) holds no secrets, so its mode does not matter.
    paths = [user_path] if pointer else [user_path, project_path]
    for path in (p for p in paths if p and p.is_file()):
        note = _mode_warning(path)
        if note:
            report.add(
                sec,
                "config permissions",
                WARN,
                note,
                f"Run: chmod 600 {path}  (the file holds secrets)",
            )
        else:
            report.add(sec, "config permissions", OK, f"{path} is private")

    if profile.access_key:
        creds = "static access_key/secret_key"
    elif profile.aws_profile:
        creds = f"AWS credential chain (aws_profile {profile.aws_profile})"
    else:
        creds = "AWS credential chain"
    cfg = build_boto_config(profile)
    checksum = getattr(cfg, "request_checksum_calculation", None) or "boto3 default"
    report.add(
        sec,
        "storage settings",
        OK,
        f"endpoint={profile.s3_endpoint or 'AWS S3'}, bucket={profile.bucket}, "
        f"region={profile.region or 'default'}, "
        f"addressing={profile.addressing_style or 'default'}, "
        f"checksum={checksum}, credentials={creds}",
    )


# ─── storage ─────────────────────────────────────────────────────────


def _debug_text(exc: BaseException) -> str:
    """Underlying error code/message plus traceback, for `a2kb -v doctor`."""
    lines = []
    err = getattr(exc, "response", {}).get("Error") if hasattr(exc, "response") else None
    if err:
        lines.append(f"S3 error code={err.get('Code')!r} message={err.get('Message')!r}")
    lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
    return "\n".join(lines)


def _fail_from(report: Report, name: str, exc: Exception, profile: ProfileConfig) -> Optional[str]:
    """Record ``exc`` as a failed check; return its friendly code (if recognised)."""
    friendly = translate(exc, profile)
    debug = _debug_text(exc)
    if friendly:
        report.add("Storage", name, FAIL, friendly.message, friendly.hint, debug)
        return friendly.code
    report.add(
        "Storage",
        name,
        FAIL,
        f"{type(exc).__name__}: {exc}",
        "Re-run with `a2kb -v doctor` for details.",
        debug,
    )
    return None


_CREDENTIAL_CODES = ("bad_credentials", "expired_token", "clock_skew")


def _reconcile_credentials(report: Report, code: Optional[str], exc: Exception, profile) -> None:
    """A later step proved the credentials bad: don't leave "credentials accepted" at WARN."""
    if code not in _CREDENTIAL_CODES:
        return
    friendly = translate(exc, profile)
    for check in report.checks:
        if check.name == "credentials accepted" and check.status == WARN:
            check.status = FAIL
            check.detail = friendly.message
            check.hint = friendly.hint
            check.debug = _debug_text(exc) if report.verbose else ""


def check_storage(
    profile: ProfileConfig,
    report: Report,
    *,
    fix: bool = False,
    read_only: bool = False,
    confirm: Optional[Callable[[str], bool]] = None,
    client: Optional[S3Client] = None,
) -> None:
    sec = "Storage"
    name_problem = bucket_name_problem(profile.bucket)
    if name_problem:
        report.add(
            sec,
            "bucket name",
            FAIL,
            name_problem,
            "Bucket names are 3-63 characters: lowercase letters, digits, hyphens and dots, "
            "starting and ending with a letter or digit. Fix `bucket:` in your config.",
        )
        report.add(sec, "endpoint / bucket / list / write", SKIP, "fix the bucket name first")
        return
    try:
        s3 = client or S3Client(profile)
    except Exception as e:  # noqa: BLE001 - reported as a check result
        _fail_from(report, "client setup", e, profile)
        return

    reachable = True
    bucket_known: Optional[bool] = None  # None = could not tell (e.g. HeadBucket denied)
    try:
        bucket_known = s3.bucket_exists()
        report.add(sec, "endpoint reachable", OK, profile.s3_endpoint or "AWS S3")
        report.add(sec, "credentials accepted", OK, "server did not reject them")
    except Exception as e:  # noqa: BLE001
        friendly = translate(e, profile)
        code = friendly.code if friendly else ""
        if code in ("unreachable", "timeout", "ssl", "bad_endpoint"):
            _fail_from(report, "endpoint reachable", e, profile)
            reachable = False
        elif code in ("no_credentials", "no_aws_profile"):
            report.add(sec, "endpoint reachable", SKIP, "not tested without credentials")
            _fail_from(report, "credentials accepted", e, profile)
            reachable = False
        elif code in ("bad_credentials", "expired_token", "clock_skew"):
            report.add(sec, "endpoint reachable", OK, "server answered")
            _fail_from(report, "credentials accepted", e, profile)
            reachable = False
        elif code == "access_denied":
            report.add(sec, "endpoint reachable", OK, "server answered")
            report.add(
                sec,
                "credentials accepted",
                WARN,
                "HeadBucket was denied (403): either the key is wrong, or it is scoped to a "
                "prefix and may not call HeadBucket; testing list access next",
                "If the list step below also fails, check access_key / secret_key.",
            )
        else:
            _fail_from(report, "bucket check", e, profile)
            reachable = False
    if not reachable:
        report.add(sec, "bucket / list / write", SKIP, "fix the problems above first")
        return

    if bucket_known is True:
        report.add(sec, "bucket exists", OK, profile.bucket)
    elif bucket_known is False:
        missing = f"bucket '{profile.bucket}' does not exist"
        hint = "Run `a2kb doctor --fix` to create it, or create it in your provider's console."
        where = profile.s3_endpoint or "AWS S3"
        if not fix:
            report.add(sec, "bucket exists", FAIL, missing, hint)
            report.add(sec, "list / write", SKIP, "bucket missing")
            return
        try:
            confirmed = confirm is not None and confirm(
                f"Create bucket '{profile.bucket}' at {where}?"
            )
        except NotInteractive:
            report.add(
                sec,
                "bucket exists",
                FAIL,
                f"{missing} (cannot ask for confirmation: no terminal)",
                "Re-run with `a2kb doctor --fix --yes` to create it without asking.",
            )
            return
        if not confirmed:
            report.add(sec, "bucket exists", FAIL, f"{missing} (creation not confirmed)", hint)
            return
        try:
            s3.create_bucket()
        except Exception as e:  # noqa: BLE001
            _fail_from(report, "create bucket", e, profile)
            return
        report.add(sec, "bucket exists", OK, f"created '{profile.bucket}' (--fix)")
    else:
        report.add(sec, "bucket exists", WARN, "could not confirm (HeadBucket denied)")

    try:
        s3.list_one()
        report.add(sec, f"list {profile.prefix or '<bucket root>'}", OK)
    except Exception as e:  # noqa: BLE001
        code = _fail_from(report, "list prefix", e, profile)
        _reconcile_credentials(report, code, e, profile)
        report.add(sec, "write probe", SKIP, "list failed")
        return

    if read_only:
        report.add(sec, "write probe", SKIP, "--read-only")
        return

    key = f"{profile.prefix}_doctor/probe-{uuid.uuid4().hex[:12]}.txt"
    payload = "a2kb doctor probe"
    uploaded = False
    try:
        s3.upload(key, payload, content_type="text/plain")
        uploaded = True
        if s3.download(key) != payload:
            report.add(
                sec,
                "write probe",
                FAIL,
                "read-back did not match what was written",
                "The server altered the object; check proxies / compatibility mode.",
            )
        else:
            s3.delete(key)
            uploaded = False
            report.add(sec, "write probe", OK, "put + get + delete under _doctor/")
    except Exception as e:  # noqa: BLE001
        code = _fail_from(report, "write probe", e, profile)
        _reconcile_credentials(report, code, e, profile)
    finally:
        if uploaded:
            try:
                s3.delete(key)
            except Exception:  # noqa: BLE001
                report.add(
                    sec,
                    "probe cleanup",
                    WARN,
                    f"could not delete {key}",
                    "Delete the leftover probe object manually.",
                )


# ─── LLM ─────────────────────────────────────────────────────────────


def _run_status(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a login-status command from an empty temp dir (it spends no tokens)."""
    with tempfile.TemporaryDirectory(prefix="kb-doctor-") as tmp:
        return subprocess.run(
            argv,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )


def _check_cli_login(command: str, report: Report, *, required: bool) -> None:
    sec = "LLM"
    name = f"CLI '{command}'"
    exe = shutil.which(command)
    if not exe:
        report.add(
            sec,
            name,
            FAIL if required else WARN,
            "not found on PATH",
            f"Install {command} and make sure it is on PATH"
            + ("" if required else " (or set cli_fallback_command: '' to disable the fallback)."),
        )
        return
    family = cli_family(command)
    if family == "codex":
        argv, login_hint = [exe, "login", "status"], "Run: codex login"
    elif family == "claude":
        argv, login_hint = [exe, "auth", "status"], "Run: claude auth login"
    else:
        report.add(sec, name, OK, f"found at {exe} (login not verified: unknown CLI)")
        return
    try:
        proc = _run_status(argv)
    except (OSError, subprocess.SubprocessError) as e:
        report.add(
            sec,
            name,
            WARN,
            f"found at {exe} but login status failed: {e}",
            login_hint,
            _debug_text(e),
        )
        return
    logged_in = proc.returncode == 0
    if family == "claude" and proc.returncode == 0:
        try:
            logged_in = bool(json.loads(proc.stdout).get("loggedIn", False))
        except ValueError:
            logged_in = True  # exit 0 but non-JSON: trust the status code
    if logged_in:
        report.add(sec, name, OK, f"found at {exe}, logged in")
    else:
        report.add(
            sec,
            name,
            FAIL if required else WARN,
            f"found at {exe}, not logged in",
            login_hint,
            f"{' '.join(argv)} exited {proc.returncode}\n"
            f"{proc.stdout.strip()}\n{proc.stderr.strip()}".strip(),
        )


def _check_ollama(profile: ProfileConfig, report: Report) -> None:
    sec = "LLM"
    url = profile.ollama_host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - user-configured host
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as e:
        report.add(
            sec,
            "ollama",
            FAIL,
            f"cannot reach {profile.ollama_host}: {e}",
            "Start Ollama (`ollama serve`) or fix `ollama_host:` in your config.",
            _debug_text(e),
        )
        return
    models = [m.get("name") or m.get("model") or "?" for m in data.get("models", [])]
    local = profile.compile_provider in (LLMProvider.OLLAMA, LLMProvider.GEMMA4)
    if not models and not local:
        report.add(
            sec,
            "ollama",
            WARN,
            f"{profile.ollama_host} is up but has no models",
            "Pull one, e.g. `ollama pull llama3.3:70b`.",
        )
        return
    report.add(sec, "ollama", OK, f"{profile.ollama_host}, {len(models)} model(s) installed")
    if not local:
        return
    # Ollama answering is not enough: compile fails at its first call if this model is absent.
    installed = {_ollama_tag(m) for m in models}
    chat = profile.default_model
    if _ollama_tag(chat) in installed:
        report.add(sec, "ollama chat model", OK, chat)
    else:
        report.add(
            sec,
            "ollama chat model",
            FAIL,
            f"'{chat}' is not installed at {profile.ollama_host}",
            f"Run `ollama pull {chat}`, or set `model:` in the llm block to one that "
            "`ollama list` shows.",
        )
    # Embeddings are optional (query falls back to keyword search): a separate warning.
    try:
        from agent2kb.embeddings import EmbeddingClient

        emb = EmbeddingClient(profile)
    except Exception:  # noqa: BLE001 - advisory only
        return
    if emb.provider != "ollama":
        return
    if _ollama_tag(emb.model) in installed:
        report.add(sec, "ollama embedding model", OK, f"{emb.model} (optional)")
    else:
        report.add(
            sec,
            "ollama embedding model",
            WARN,
            f"'{emb.model}' is not installed: queries use keyword search only",
            f"Optional: `ollama pull {emb.model}` enables semantic search.",
        )


def _ollama_tag(name: str) -> str:
    """``bge-m3`` and ``bge-m3:latest`` name the same Ollama model."""
    name = (name or "").strip()
    return name if ":" in name else f"{name}:latest"


_API_KEY_FIELDS = {
    LLMProvider.GROQ: ("groq_api_key", "KB_GROQ_API_KEY"),
    LLMProvider.OPENROUTER: ("openrouter_api_key", "KB_OPENROUTER_API_KEY"),
    LLMProvider.GEMINI: ("gemini_api_key", "KB_GEMINI_API_KEY"),
}


def check_llm(profile: ProfileConfig, report: Report) -> None:
    sec = "LLM"
    provider = profile.compile_provider
    report.add(sec, "compile provider", OK, provider.value)
    if not (profile.output_language or "").strip():
        # Older configs may predate the English default; make the effective language visible.
        report.add(
            sec, "output language", WARN,
            "not set, so pages and answers are written in English",
            "Set output_language for this KB (for example en, de or ja) to choose its language "
            "and silence this warning.",
        )
    if provider is LLMProvider.CLI:
        _check_cli_login(profile.cli_command, report, required=True)
        if profile.cli_fallback_command and profile.cli_fallback_command != profile.cli_command:
            _check_cli_login(profile.cli_fallback_command, report, required=False)
        try:
            from agent2kb.cli_runner import fallback_target_for, target_for

            targets = [
                target_for(profile, "compile"),
                target_for(profile, "aux"),
                fallback_target_for(profile),
            ]
            problems: list[str] = []
            for t in dict.fromkeys(x for x in targets if x):
                problems += preflight_cli_model(t.model, t.effort, t.command)
        except Exception as e:  # noqa: BLE001 - preflight is advisory
            problems = [f"model preflight skipped: {e}"]
        for problem in problems:
            report.add(sec, "model", WARN, problem)
    elif provider in _API_KEY_FIELDS:
        attr, env = _API_KEY_FIELDS[provider]
        if getattr(profile, attr):
            report.add(sec, f"{provider.value} API key", OK, "set (not validated against the API)")
        else:
            report.add(
                sec,
                f"{provider.value} API key",
                FAIL,
                f"`{attr}` is not set",
                f"Add `{attr}:` to your config or export {env}.",
            )
    else:  # ollama / gemma4 (local)
        _check_ollama(profile, report)


# ─── tools / privacy ─────────────────────────────────────────────────


def _extra_hint(extra: str) -> str:
    """The pip command that installs optional-dependency group ``extra``."""
    return f'pip install "{DIST_NAME}[{extra}]"'


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a dotted name whose parent package is missing
        return False


def check_tools(report: Report) -> None:
    sec = "Tools"
    tools = [
        ("ffmpeg", "audio/video ingest", "install ffmpeg (apt/brew install ffmpeg)"),
        ("pandoc", "other document formats (odt, epub, ...)",
         "install pandoc (apt/brew install pandoc)"),
        ("yt-dlp", "video URL ingest", _extra_hint("media")),
    ]
    for exe, purpose, fix in tools:
        path = shutil.which(exe)
        if path:
            report.add(sec, exe, OK, path)
        else:
            report.add(
                sec, exe, WARN, f"missing (optional) — needed for {purpose}", f"To enable: {fix}"
            )
    optional_modules = [
        ("faster_whisper", "faster-whisper", "audio/video transcription", "whisper"),
        ("docx", "python-docx", "reading .docx files", "media"),
        ("openpyxl", "openpyxl", "reading .xlsx files", "media"),
        ("pypdf", "pypdf", "reading PDF files", "media"),
    ]
    for module, label, purpose, extra in optional_modules:
        if _has_module(module):
            report.add(sec, label, OK, "installed")
        else:
            report.add(
                sec,
                label,
                WARN,
                f"missing (optional) — needed for {purpose}",
                f"To enable: {_extra_hint(extra)}",
            )


#: compile provider -> (python module, extra, label)
_PROVIDER_PACKAGES = {
    LLMProvider.GROQ: ("groq", "groq", "Groq"),
    LLMProvider.OPENROUTER: ("openai", "openai", "OpenRouter (uses the openai SDK)"),
    LLMProvider.OLLAMA: ("ollama", "ollama", "Ollama"),
    LLMProvider.GEMMA4: ("ollama", "ollama", "Ollama"),
    LLMProvider.GEMINI: ("google.genai", "gemini", "Gemini"),
}
#: embedding provider -> (python module, extra, label)
_EMBEDDING_PACKAGES = {
    "voyage": ("voyageai", "voyage", "Voyage embeddings"),
    "gemini": ("google.genai", "gemini", "Gemini embeddings"),
    "openai": ("openai", "openai", "OpenAI embeddings"),
    "ollama": ("ollama", "ollama", "Ollama embeddings"),
}


def check_extras(profile: ProfileConfig, report: Report) -> None:
    """Report missing optional Python packages for the providers this profile uses."""
    sec = "Python packages"
    needed = []
    if profile.compile_provider in _PROVIDER_PACKAGES:
        needed.append((*_PROVIDER_PACKAGES[profile.compile_provider], FAIL))
    try:
        from agent2kb.embeddings import EmbeddingClient

        emb = EmbeddingClient(profile).provider
    except Exception:  # noqa: BLE001 - advisory
        emb = None
    if emb in _EMBEDDING_PACKAGES:
        needed.append((*_EMBEDDING_PACKAGES[emb], WARN))

    seen = set()
    for module, extra, label, missing_status in needed:
        if module in seen:
            continue
        seen.add(module)
        if _has_module(module):
            report.add(sec, module, OK, f"installed ({label})")
        else:
            report.add(
                sec,
                module,
                missing_status,
                f"{label} needs the '{module}' package, which is not installed",
                f"Install it: {_extra_hint(extra)}",
            )
    if not needed:
        report.add(sec, "provider packages", OK, "the CLI provider needs no Python package")
    if not _has_module("numpy"):
        report.add(
            sec,
            "numpy",
            WARN,
            "missing (optional) — faster vector search",
            f"To enable: {_extra_hint('fast')}",
        )


def _host(url: Optional[str]) -> str:
    return (urlparse(url).hostname or url or "") if url else "AWS S3"


def check_privacy(profile: ProfileConfig, report: Report, *, v2: bool = False) -> None:
    sec = "Privacy"
    provider = profile.compile_provider
    # Name the setting the way the user's config file spells it.
    local_hint = (
        "Use llm.provider: ollama for sensitive content."
        if v2
        else "Use compile_provider: ollama for sensitive content."
    )
    if provider in (LLMProvider.OLLAMA, LLMProvider.GEMMA4):
        report.add(
            sec, "compile", OK, f"{provider.value}: content stays on {_host(profile.ollama_host)}"
        )
    elif provider is LLMProvider.CLI:
        report.add(
            sec,
            "compile",
            WARN,
            f"wiki content is sent to the {profile.cli_command} vendor via its CLI login",
            local_hint,
        )
    else:
        extra = " (free tier may train on prompts)" if provider is LLMProvider.GEMINI else ""
        report.add(
            sec,
            "compile",
            WARN,
            f"content is sent to {provider.value}{extra}",
            local_hint,
        )

    try:
        from agent2kb.embeddings import EmbeddingClient

        emb = EmbeddingClient(profile).provider
    except Exception:  # noqa: BLE001
        emb = "unknown"
    if emb == "ollama":
        report.add(sec, "embeddings", OK, "ollama (local)")
    else:
        report.add(sec, "embeddings", WARN, f"page text is sent to {emb} for semantic search")

    cloud_ok = not profile.privacy_mode
    for flag, name, dest in (
        (profile.allow_jina_reader, "jina reader", "URLs you ingest are fetched via r.jina.ai"),
        (profile.allow_cloud_vision, "cloud vision", "images are sent to Gemini/Claude for OCR"),
    ):
        if flag and cloud_ok:
            report.add(
                sec, name, WARN, dest, "Set privacy_mode: true (or allow_* : false) to disable."
            )
        else:
            report.add(
                sec, name, OK, "disabled" + (" by privacy_mode" if profile.privacy_mode else "")
            )
    if profile.google_safe_browsing_key and cloud_ok:
        report.add(
            sec, "safe browsing", WARN, "ingested URLs are checked with Google Safe Browsing"
        )
    host = _host(profile.s3_endpoint)
    report.add(sec, "storage", OK, f"KB content is stored at {host}")


# ─── assembly / rendering ───────────────────────────────────────


def run_doctor(
    profile: ProfileConfig,
    config_file: Optional[Path] = None,
    *,
    fix: bool = False,
    read_only: bool = False,
    confirm: Optional[Callable[[str], bool]] = None,
    verbose: bool = False,
) -> Report:
    report = Report(verbose=verbose)
    check_config(profile, config_file, report)
    check_storage(profile, report, fix=fix, read_only=read_only, confirm=confirm)
    check_llm(profile, report)
    check_extras(profile, report)
    check_tools(report)
    user_path = config_file or kb_config.default_user_config_path()
    check_privacy(profile, report, v2=_is_v2_kb(user_path, profile.name))
    return report


def render(report: Report, console) -> None:
    from rich.markup import escape

    style = {OK: "green", FAIL: "red", WARN: "yellow", SKIP: "dim"}
    section = None
    for c in report.checks:
        if c.section != section:
            section = c.section
            console.print(f"\n[bold]{section}[/bold]")
        sym = f"[{style[c.status]}]{_SYMBOL[c.status]}[/{style[c.status]}]"
        detail = f" [dim]— {escape(c.detail)}[/dim]" if c.detail else ""
        console.print(f"  {sym} {escape(c.name)}{detail}")
        if c.hint and c.status in (FAIL, WARN):
            console.print(f"      [dim]→ {escape(c.hint)}[/dim]")
        for line in c.debug.splitlines():
            console.print(f"      [dim]{escape(line)}[/dim]", soft_wrap=True)
    counts = {s: sum(1 for c in report.checks if c.status == s) for s in (OK, WARN, FAIL)}
    verdict = (
        "[red]problems found[/red]" if report.failed else "[green]no blocking problems[/green]"
    )
    console.print(
        f"\n{verdict} — {counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)"
    )
