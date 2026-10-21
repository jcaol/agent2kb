"""Run an LLM CLI (Claude Code or codex) as an isolated text transformer.

agent2kb drives LLMs through their CLIs so it can reuse an existing login
instead of holding API keys. Both CLIs are full coding agents, though, and
by default they bring their whole environment along: the user's CLAUDE.md,
project settings, MCP servers, skills, and tools such as Bash. For a
compiler that feeds the model untrusted web content, all of that is wrong:

- it leaks into the output (a model told to answer "OK" adds commentary
  prompted by unrelated personal instructions),
- it costs money (the default Claude Code system prompt is ~15x the size of
  the one agent2kb needs), and
- it hands shell access to whatever instructions a scraped page contains.

Every call here therefore runs with tools off (or read-only for images),
no settings or MCP, a replaced system prompt, and an empty working
directory. Organisation-level managed instructions still apply — they are
administrator policy and are deliberately not bypassed.

This module is also the single place that knows how each CLI reports
failure, so callers only see three outcomes: text, ``CliError`` (this item
failed), or ``ProviderExhaustedError`` (every further call will fail too).
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Iterable, Optional

from loguru import logger

from agent2kb.cli_models import cli_family

#: Seconds before a single CLI call is abandoned. Long sources at high
#: effort measured ~110s on Haiku 5.5; codex at xhigh has exceeded 300s.
DEFAULT_TIMEOUT = 600

#: Secrets agent2kb reads from the environment that neither CLI needs. The CLIs process
#: untrusted web content, so their environment carries no S3 or other provider keys.
#: Each CLI keeps its own login variables (ANTHROPIC_*, CLAUDE_*, OPENAI_*, CODEX_HOME).
_CHILD_ENV_DROP = frozenset({
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "VOYAGE_API_KEY", "GOOGLE_SAFE_BROWSING_KEY",
})


def _child_env(cli: str) -> dict[str, str]:
    """The environment for a ``claude`` or ``codex`` call: ours minus agent2kb's secrets.

    Drops every ``KB_*`` variable (S3 keys, MCP tokens, API keys), the provider keys in
    ``_CHILD_ENV_DROP`` and ``AWS_*`` (agent2kb's S3 credentials), except for Claude Code
    on Bedrock (``CLAUDE_CODE_USE_BEDROCK``), which authenticates with them. Claude Code
    does not read ``OPENAI_API_KEY`` (agent2kb's embeddings key), so it is dropped there.
    """
    bedrock = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower()
    keep_aws = cli == "claude" and bedrock not in ("", "0", "false")
    env = {}
    for name, value in os.environ.items():
        if name.startswith("KB_") or name in _CHILD_ENV_DROP:
            continue
        if name.startswith("AWS_") and not keep_aws:
            continue
        if name == "OPENAI_API_KEY" and cli != "codex":
            continue
        env[name] = value
    return env


class CliError(RuntimeError):
    """One call failed; the provider may still work for the next item."""


class CliTimeout(CliError):
    """One call exceeded its time budget."""


class ProviderExhaustedError(RuntimeError):
    """The provider is hard-down for the rest of this run.

    Out of credits, quota exhausted, bad credentials, or an unusable model —
    conditions no amount of retrying fixes. Distinguished from an ordinary
    failure so a run can stop instead of making one doomed call per item.
    """


#: Substrings that mean "this provider will fail every subsequent call
#: too". Matched case-insensitively against the provider's error text.
_EXHAUSTED_MARKERS = (
    # codex / OpenAI
    "out of credits",
    "hit your usage limit",
    "upgrade to pro",
    "quota exceeded",
    "insufficient_quota",
    "not supported when using codex",
    # Claude Code
    "credit balance is too low",
    "usage limit reached",
    "please run /login",
    "token has expired",
    "issue with the selected model",
    # generic
    "401 unauthorized",
    "403 forbidden",
    "invalid api key",
)

#: HTTP statuses Claude Code reports that no retry will fix.
_EXHAUSTED_STATUSES = {401, 402, 403, 404}


def is_provider_exhausted(error: str) -> bool:
    """True when an error means every further call to this provider fails."""
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _EXHAUSTED_MARKERS)


@dataclass(frozen=True)
class CliTarget:
    """Which CLI to run, with which model and reasoning effort."""

    command: str
    model: str
    effort: str

    def __str__(self) -> str:
        return f"{self.command}:{self.model}@{self.effort}"


@dataclass(frozen=True)
class CliResult:
    text: str
    target: CliTarget
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None


# --- Claude Code ------------------------------------------------------------

_CLAUDE_ISOLATION = (
    "--setting-sources", "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--output-format", "json",
)


def _last_json_line(stdout: str) -> Optional[dict]:
    """Claude Code may print a diagnostic line before its JSON result."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def run_claude(
    target: CliTarget,
    system: str,
    user: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    read_dir: Optional[str] = None,
) -> CliResult:
    """Run ``claude -p`` isolated. ``read_dir`` grants the Read tool on one
    directory and nothing else — used to let the model see an image."""
    # The system prompt file lives outside ``read_dir`` so the Read tool,
    # when granted, cannot see it either.
    workdir = tempfile.mkdtemp(prefix="kb-claude-")
    system_file = os.path.join(workdir, "system.txt")
    try:
        with open(system_file, "w") as f:
            f.write(system or "Follow the user's instructions exactly.")

        args = [
            target.command, "-p",
            "--model", target.model,
            "--effort", target.effort,
            # A file, not argv: concept prompts embed every referencing
            # snippet and can exceed the kernel's per-argument limit.
            "--system-prompt-file", system_file,
            *_CLAUDE_ISOLATION,
        ]
        if read_dir:
            # Read is pre-approved for this one directory only. An unscoped
            # `--allowedTools Read` approves reading ANY path, so text inside
            # an image could have the model read ~/.kb/config.yaml into the
            # knowledge base. Claude Code's absolute-path rule syntax is
            # `//abs/path/**`; reads elsewhere are denied in -p mode.
            read_dir = os.path.realpath(read_dir)
            args += ["--tools", "Read", "--allowedTools", f"Read(/{read_dir}/**)"]
        else:
            args += ["--tools", ""]

        logger.info(f"Calling CLI: claude {target.model} effort={target.effort} "
                    f"(prompt: {len(user)} chars)")
        try:
            proc = subprocess.run(
                args, input=user, capture_output=True, text=True,
                timeout=timeout, cwd=read_dir or workdir, env=_child_env("claude"),
            )
        except subprocess.TimeoutExpired as e:
            raise CliTimeout(f"claude {target.model} timed out after {timeout}s") from e

        data = _last_json_line(proc.stdout)
        if data is None:
            detail = (proc.stderr or proc.stdout or "no output").strip()[:500]
            if is_provider_exhausted(detail):
                raise ProviderExhaustedError(f"claude: {detail}")
            raise CliError(f"claude exit {proc.returncode}: {detail}")

        text = (data.get("result") or "").strip()
        if data.get("is_error"):
            status = data.get("api_error_status")
            message = f"claude {target.model} failed ({status}): {text[:400]}"
            if status in _EXHAUSTED_STATUSES or is_provider_exhausted(text):
                raise ProviderExhaustedError(message)
            raise CliError(message)
        if not text:
            raise CliError(f"claude {target.model} returned empty output")

        usage = data.get("usage") or {}
        return CliResult(
            text=text,
            target=target,
            input_tokens=(usage.get("input_tokens", 0)
                          + usage.get("cache_creation_input_tokens", 0)
                          + usage.get("cache_read_input_tokens", 0)),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd"),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- codex -------------------------------------------------------------------

#: codex features that may stay on: transport and auth plumbing, nothing that
#: gives the model a tool. Every other enabled feature is switched off per
#: call. This is an allowlist on purpose — codex ships new agent features
#: (shell, browser, computer use, plugins, MCP apps, multi-agent...) enabled
#: by default, and a denylist would silently fall behind.
_CODEX_KEEP_FEATURES = frozenset({
    "api_key_model_discovery",
    "auth_elicitation",
    "compaction_image_budget",
    "content_item_kinds",
    "enable_request_compression",
    "unbounded_connection_retries",
})


#: Tool features that must appear in the list. If they are missing, the
#: list's format or naming has changed and we cannot trust our parse.
_CODEX_REQUIRED_DISABLES = frozenset({"shell_tool", "unified_exec"})


@functools.lru_cache(maxsize=8)
def _codex_disabled_features(command: str) -> tuple[str, ...]:
    """Every codex feature outside the keep-list, enabled or not.

    The current on/off column is deliberately ignored. ``features list``
    reports state *with* the user's config.toml applied, while exec runs
    with ``--ignore-user-config``; a tool that is on by default but switched
    off in config.toml would list as "false", be skipped here, and come back
    on at exec time. Disabling an already-off feature is harmless, so every
    non-removed feature outside the keep-list is disabled unconditionally.

    Fails closed on anything unexpected: a non-zero exit, a line that does
    not parse as ``name stage... true|false``, or a list that no longer
    names the shell tools. Any of those means the format changed and a
    partial parse could leave tools on.
    """
    try:
        proc = subprocess.run(
            [command, "features", "list"], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL, env=_child_env("codex"),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProviderExhaustedError(f"codex features list failed: {e}") from e

    def refuse(reason: str) -> ProviderExhaustedError:
        return ProviderExhaustedError(
            f"Refusing to run codex: {reason}. Its tools could not be shown to be off."
        )

    if proc.returncode != 0:
        raise refuse(f"`codex features list` exited {proc.returncode}")

    disable: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3 or parts[-1] not in ("true", "false"):
            raise refuse(f"unrecognised feature-list line {line.strip()[:80]!r}")
        name, stage = parts[0], " ".join(parts[1:-1])
        if stage != "removed" and name not in _CODEX_KEEP_FEATURES:
            disable.append(name)

    missing = _CODEX_REQUIRED_DISABLES - set(disable)
    if missing:
        raise refuse(f"feature list no longer names {sorted(missing)}")
    return tuple(disable)


_CODEX_ERROR_LINE = ("ERROR:", "Error:", "error:", "FAIL", "[ERROR]")


def _codex_error_text(output: str) -> str:
    lines = [
        ln.strip() for ln in output.splitlines()
        if ln.strip().startswith(_CODEX_ERROR_LINE) or is_provider_exhausted(ln)
    ]
    return " | ".join(dict.fromkeys(lines))[:800] or output.strip()[-500:] or "no output"


def run_codex(
    target: CliTarget,
    system: str,
    user: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    image: Optional[str] = None,
) -> CliResult:
    """Run ``codex exec`` with every tool disabled, from an empty directory.

    ``--sandbox read-only`` alone is not enough: it stops writes but still
    lets the shell read any file, so injected instructions in a scraped page
    could copy ~/.kb/config.yaml into a wiki page. Shell, browser, computer
    use, plugins and the rest are therefore disabled outright (see
    ``_CODEX_KEEP_FEATURES``), user config and exec rules are ignored, and
    the sandbox stays read-only as a second layer.

    The prompt always goes over stdin, which avoids the kernel's per-argument
    limit that CJK-heavy prompts used to hit.
    """
    workdir = tempfile.mkdtemp(prefix="kb-codex-")
    outfile = os.path.join(workdir, "out.txt")
    try:
        args = [
            target.command, "exec",
            "-m", target.model,
            "-c", f'model_reasoning_effort="{target.effort}"',
            # Untrusted web content goes into this prompt; never let it
            # drive a writable shell.
            "--sandbox", "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "-o", outfile,
        ]
        for feature in _codex_disabled_features(target.command):
            args += ["--disable", feature]
        if image:
            args += ["-i", image]
        args += ["--", "Follow the instructions provided on stdin."]

        prompt = f"{system}\n\n---\n\n{user}" if system else user
        logger.info(f"Calling CLI: codex {target.model} effort={target.effort} "
                    f"(prompt: {len(prompt)} chars)")
        try:
            proc = subprocess.run(
                args, input=prompt, capture_output=True, text=True,
                timeout=timeout, cwd=workdir, env=_child_env("codex"),
            )
        except subprocess.TimeoutExpired as e:
            raise CliTimeout(f"codex {target.model} timed out after {timeout}s") from e

        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        tokens = 0
        match = re.search(r"tokens used\s*\n\s*([\d,]+)", combined)
        if match:
            tokens = int(match.group(1).replace(",", ""))

        text = ""
        if os.path.exists(outfile):
            with open(outfile) as f:
                text = f.read().strip()
        if text:
            return CliResult(text=text, target=target, input_tokens=tokens)

        detail = _codex_error_text(combined)
        message = f"codex {target.model} failed (exit {proc.returncode}): {detail}"
        if is_provider_exhausted(detail):
            raise ProviderExhaustedError(message)
        raise CliError(message)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- dispatch + fallback -----------------------------------------------------

def run_cli(target: CliTarget, system: str, user: str, *,
            timeout: int = DEFAULT_TIMEOUT) -> CliResult:
    """Run one call on whichever CLI ``target`` names."""
    if not shutil.which(target.command):
        raise ProviderExhaustedError(f"CLI '{target.command}' not found in PATH")
    family = cli_family(target.command)
    if family == "claude":
        return run_claude(target, system, user, timeout=timeout)
    if family == "codex":
        return run_codex(target, system, user, timeout=timeout)
    raise ProviderExhaustedError(f"Unsupported CLI '{target.command}' (expected claude or codex)")


class CliRouter:
    """A primary CLI target with an optional fallback, remembered per run.

    - Primary hard-down → switch to the fallback, and stay switched for the
      rest of the run rather than re-probing a dead provider on every item.
    - Primary timeout → try the fallback once for this item only; slowness
      on one long source says nothing about the next one.
    - Both hard-down → ``ProviderExhaustedError`` so the caller can stop.
    """

    def __init__(self, primary: CliTarget, fallback: Optional[CliTarget] = None,
                 *, timeout: int = DEFAULT_TIMEOUT, secrets: Iterable[str] = ()):
        self.primary = primary
        self.fallback = fallback if fallback and fallback.command != primary.command else None
        self.timeout = timeout
        # Short values would false-positive on ordinary text.
        self._secrets = tuple(v for v in secrets if v and len(v) >= 12)
        self._primary_down: Optional[str] = None
        self.last: Optional[CliResult] = None

    def run(self, system: str, user: str) -> CliResult:
        result = self._run(system, user)
        self._refuse_leaked_secrets(result)
        return result

    def _refuse_leaked_secrets(self, result: CliResult) -> None:
        """Last line of defence against prompt injection.

        Tools are off, so a model should have no way to read a credential.
        If one shows up in the output anyway, something upstream failed:
        discard the text rather than let it reach S3 and every synced device.
        """
        for secret in self._secrets:
            if secret in result.text:
                logger.error(
                    f"{result.target} output contained a credential from this profile; "
                    "discarded. The source may contain a prompt-injection attempt."
                )
                raise CliError("LLM output contained a profile credential; discarded")

    def _run(self, system: str, user: str) -> CliResult:
        if self._primary_down is None:
            try:
                self.last = run_cli(self.primary, system, user, timeout=self.timeout)
                return self.last
            except ProviderExhaustedError as e:
                self._primary_down = str(e)
                if not self.fallback:
                    raise
                logger.warning(f"{self.primary} unavailable ({str(e)[:160]}); "
                               f"switching to {self.fallback} for the rest of this run")
            except CliTimeout as e:
                if not self.fallback:
                    raise
                logger.warning(f"{e}; retrying this item once on {self.fallback}")
                return self._run_fallback(system, user, primary_error=str(e))
        return self._run_fallback(system, user, primary_error=self._primary_down)

    def _run_fallback(self, system: str, user: str, *, primary_error: str) -> CliResult:
        try:
            self.last = run_cli(self.fallback, system, user, timeout=self.timeout)
            return self.last
        except ProviderExhaustedError as e:
            raise ProviderExhaustedError(f"{primary_error} | fallback {self.fallback}: {e}") from e
        except CliError as e:
            combined = f"{primary_error} | fallback {self.fallback}: {e}"
            # Primary is gone and the fallback failed this item: the item
            # failed, but the fallback may still serve the next one.
            raise CliError(combined) from e


# --- profile wiring ------------------------------------------------------------

#: What a call is for. "compile" turns a raw source into a wiki page;
#: "aux" covers concept pages, summaries, lint reports, and the digest.
PURPOSES = ("compile", "aux")


def target_for(profile, purpose: str = "compile") -> CliTarget:
    """The primary CLI target a profile uses for ``purpose``."""
    from agent2kb.cli_models import normalize_cli_effort, resolve_cli_model

    if purpose not in PURPOSES:
        raise ValueError(f"Unknown purpose '{purpose}' (expected one of {PURPOSES})")
    command = (profile.cli_command or "claude").strip()
    model = profile.default_model
    effort = profile.cli_reasoning_effort
    if purpose == "aux":
        model = profile.aux_model or model
        effort = profile.aux_effort or effort
    return CliTarget(command, resolve_cli_model(model, command), normalize_cli_effort(effort))


def fallback_target_for(profile) -> Optional[CliTarget]:
    """The fallback CLI target, or None when disabled."""
    from agent2kb.cli_models import normalize_cli_effort, resolve_cli_model

    command = (profile.cli_fallback_command or "").strip()
    if not command:
        return None
    return CliTarget(
        command,
        resolve_cli_model(profile.cli_fallback_model, command),
        normalize_cli_effort(profile.cli_fallback_effort),
    )


_SECRET_FIELD = re.compile(r"(key|secret|token|password)$")


def profile_secrets(profile) -> list[str]:
    """Credential values held by a profile (S3 keys, API keys, ...)."""
    return [
        value for name, value in profile.model_dump().items()
        if isinstance(value, str) and _SECRET_FIELD.search(name)
    ]


def router_for(profile, purpose: str = "compile", *, timeout: int = DEFAULT_TIMEOUT) -> CliRouter:
    """A router for one purpose, with the profile's fallback attached."""
    return CliRouter(
        target_for(profile, purpose), fallback_target_for(profile),
        timeout=timeout, secrets=profile_secrets(profile),
    )
