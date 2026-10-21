"""Codex CLI model policy — single source of truth.

The ``cli`` compile provider shells out to ``codex exec -m <model>``. Codex
validates the slug server-side against what the account may use and returns
HTTP 400 for anything else, so a stale pin breaks compile and image ingest
outright rather than degrading. Model names therefore must never be spread
across call sites; every consumer resolves through this module.

Kept deliberately free of LLM-SDK imports so light consumers (the image
ingest handler) can use it without pulling in the whole compiler stack.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger

# Default codex model. Verified against the codex model list; see
# CODEX_MODELS_CACHE below for the live check.
CLI_DEFAULT_MODEL = "gpt-6-luna"

# Default Claude Code model when `cli_command` is claude. Haiku 5.5 at high
# effort produced well-formed wiki pages in a dry run at a fraction of the
# cost and time of a reasoning model at xhigh.
CLAUDE_DEFAULT_MODEL = "claude-haiku-5-5"

# Aliases Claude Code resolves to the latest model of each tier.
CLAUDE_ALIASES = frozenset({"haiku", "sonnet", "opus", "fable"})

# Reasoning effort when a profile sets none. Per-profile
# `cli_reasoning_effort` / `aux_effort` / `cli_fallback_effort` override it.
CLI_DEFAULT_EFFORT = "high"

# Reasoning levels accepted by both CLIs. "ultra" exists only on some codex
# models (Sol/Terra/Astra), so it is not in the common set — passing a level the chosen model
# lacks is rejected server-side.
CLI_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Retired slugs that must never reach codex. Pinned profiles are migrated to
# CLI_DEFAULT_MODEL so old config.yaml files keep working.
LEGACY_CLI_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.3",
        "gpt-5.4",
        "gpt-5.4-medium",
        "gpt-5-codex",
    }
)

# Model names belonging to other providers. A profile that switches provider
# without clearing default_model would otherwise hand codex an ollama slug.
FOREIGN_MODELS = frozenset(
    {
        "llama-3.3-70b-versatile",
        "llama3.3:70b",
        "gemma4:27b",
    }
)

# codex caches its server-side model list here. Absent when codex is not
# installed or has never run; every read is best-effort.
CODEX_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"


def normalize_cli_effort(effort: Optional[str]) -> str:
    """Clamp a profile's ``cli_reasoning_effort`` to a level codex accepts.

    Unknown values (typos, or "ultra" on a model that lacks it) would be
    rejected by codex mid-compile, so fall back to CLI_DEFAULT_EFFORT.
    """
    value = (effort or "").strip().lower()
    if value in CLI_EFFORTS:
        return value
    if value:
        logger.warning(f"Unknown cli_reasoning_effort '{effort}' — using '{CLI_DEFAULT_EFFORT}'.")
    return CLI_DEFAULT_EFFORT


def cli_family(command: Optional[str]) -> str:
    """Which CLI a command runs: "claude", "codex", or "other"."""
    name = Path((command or "").strip()).name.lower()
    if "claude" in name:
        return "claude"
    if "codex" in name:
        return "codex"
    return "other"


def default_model_for(command: Optional[str]) -> str:
    """The default model for a CLI command."""
    return CLAUDE_DEFAULT_MODEL if cli_family(command) == "claude" else CLI_DEFAULT_MODEL


def _is_claude_model(model: str) -> bool:
    return model.startswith("claude-") or model in CLAUDE_ALIASES


def is_usable_cli_model(model: Optional[str], command: Optional[str] = "codex") -> bool:
    """True when ``model`` is a slug we are willing to hand to ``command``.

    Each CLI rejects the other's model names, so a profile that switches
    `cli_command` without updating its model pins must not pass them through.
    """
    value = (model or "").strip()
    if not value:
        return False
    if cli_family(command) == "claude":
        return _is_claude_model(value)
    return (
        value not in LEGACY_CLI_MODELS
        and value not in FOREIGN_MODELS
        and not _is_claude_model(value)
    )


def resolve_cli_model(pinned: Optional[str], command: Optional[str] = "codex") -> str:
    """Resolve a pinned model to one ``command`` will accept.

    Retired, foreign-provider, and other-CLI slugs fall back to that CLI's
    default with a warning; anything else is passed through so operators can
    pin newer models without a agent2kb release.
    """
    value = (pinned or "").strip()
    if is_usable_cli_model(value, command):
        return value
    default = default_model_for(command)
    if value in LEGACY_CLI_MODELS:
        logger.warning(
            f"Profile pins retired CLI model '{value}' — codex would reject it. "
            f"Using '{default}' instead; update default_model in your config."
        )
    elif value:
        logger.warning(
            f"Model '{value}' does not belong to the '{cli_family(command)}' CLI. "
            f"Using '{default}' instead."
        )
    return default


def load_codex_models() -> Optional[dict[str, dict]]:
    """Read codex's cached model list, keyed by slug.

    Returns None when the cache is missing or unreadable — codex may not be
    installed, and a preflight must never be the thing that fails.
    """
    try:
        data = json.loads(CODEX_MODELS_CACHE.read_text())
    except (OSError, ValueError):
        return None

    models = data.get("models")
    if not isinstance(models, list):
        return None

    out: dict[str, dict] = {}
    for entry in models:
        slug = entry.get("slug")
        if not slug:
            continue
        out[slug] = {
            "display_name": entry.get("display_name") or slug,
            "efforts": [
                level.get("effort")
                for level in entry.get("supported_reasoning_levels", [])
                if level.get("effort")
            ],
            "visibility": entry.get("visibility"),
        }
    return out


def preflight_cli_model(model: str, effort: str, command: Optional[str] = "codex") -> list[str]:
    """Check a resolved model/effort pair against codex's cached model list.

    Returns human-readable problems, empty when the pair looks usable. A
    missing cache yields no findings rather than a false alarm: the point is
    to catch a retired pin before a cron compile dies on it, not to block.
    """
    if cli_family(command) != "codex":
        # Claude Code keeps no local model catalogue; a bad slug surfaces as
        # an "issue with the selected model" error, which the run treats as
        # non-retryable.
        return []
    catalog = load_codex_models()
    if catalog is None:
        return []

    if model not in catalog:
        known = ", ".join(
            slug for slug, meta in sorted(catalog.items()) if meta["visibility"] != "hide"
        )
        return [
            f"Model '{model}' is not in codex's model list — compile will fail "
            f"with HTTP 400. Available: {known}"
        ]

    efforts = catalog[model]["efforts"]
    if efforts and effort not in efforts:
        return [
            f"Model '{model}' does not support reasoning effort '{effort}'. "
            f"Supported: {', '.join(efforts)}"
        ]
    return []
