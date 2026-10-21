"""Structured run summaries for cron and CLI observability.

Long-running deployments need machine-readable evidence of what compile/query runs
actually did — provider, model, fallback attempts, transient vs
permanent errors, run counts. Plain log scraping breaks the moment
log lines are reformatted; a JSON blob per run gives cron systems and
dashboards a stable contract.

Storage layout (per profile):
    <prefix>_ops/compile-runs/YYYYMMDD-HHMMSS.json
    <prefix>_ops/maintenance.json                 (compile's pending maintenance)
    <prefix>_ops/query-runs/YYYYMMDD-HHMMSS.json   (optional / future)

The ``_ops/`` namespace is per-profile, not the global ``_meta/`` —
project profiles can read and write their own ops logs without being
super.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Optional

from loguru import logger

from agent2kb.models import CompileResult, ProfileConfig
from agent2kb.s3_client import S3Client

_OPS_PREFIX = "_ops/"
_COMPILE_RUNS_SUB = "compile-runs/"
_MAINTENANCE_FILE = "maintenance.json"


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def classify_error(error: str) -> str:
    """Bucket a free-form compile error string into a stable taxonomy.

    Triage labels we surface to operators:
      - ``rate_limit``    : transient, retry-with-backoff territory
      - ``auth``          : token/credentials wrong; user action needed
      - ``content_too_large`` : context-window overflow; switch provider
      - ``network``       : DNS/timeout/connection; usually transient
      - ``provider_blocked`` : provider refused (safety, region, etc.)
      - ``empty_response``: model returned nothing usable
      - ``other``         : everything else; keep raw error for context

    Operators read this to decide whether a failed compile run is worth
    paging on. ``rate_limit`` + ``network`` are noisy and usually
    self-heal; ``auth`` + ``provider_blocked`` need a human.
    """
    e = (error or "").lower()
    if "rate limit" in e or "429" in e or "quota" in e:
        return "rate_limit"
    if "auth" in e or "401" in e or "403" in e or "api key" in e or "credential" in e:
        return "auth"
    if "context" in e and ("too" in e or "exceed" in e or "overflow" in e or "limit" in e):
        return "content_too_large"
    if "token" in e and ("limit" in e or "exceed" in e or "max" in e):
        return "content_too_large"
    if "timeout" in e or "connection" in e or "dns" in e or "network" in e:
        return "network"
    if "blocked" in e or "safety" in e or "filtered" in e:
        return "provider_blocked"
    if "empty" in e or "no content" in e:
        return "empty_response"
    return "other"


def is_transient(error: str) -> bool:
    """True iff this error class is worth automatically retrying."""
    return classify_error(error) in {"rate_limit", "network", "empty_response"}


def write_compile_run(
    s3: S3Client,
    profile: ProfileConfig,
    results: list[CompileResult],
    *,
    provider: str,
    model: Optional[str],
    new_only: bool,
    allow_cloud_fallback: bool,
    fallback_attempts: Optional[list[dict[str, str]]] = None,
    cloud_fallback_blocked: bool = False,
) -> Optional[str]:
    """Persist a single compile-run summary to ``_ops/compile-runs/*.json``.

    Returns the s3 key written, or ``None`` if the write failed (we
    swallow exceptions here — a logging failure must never bring down
    the compile run itself).
    """
    ok = sum(1 for r in results if r.success)
    failed = [r for r in results if not r.success]

    summary: dict[str, Any] = {
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "profile": profile.name,
        "provider": provider,
        "model": model,
        "new_only": new_only,
        "allow_cloud_fallback": allow_cloud_fallback,
        "cloud_fallback_blocked": cloud_fallback_blocked,
        "totals": {
            "files": len(results),
            "ok": ok,
            "failed": len(failed),
            # Subset of "failed": sources moved to raw/.failed/ in this run.
            "quarantined": sum(1 for r in failed if r.quarantined),
            "duration_sec": round(sum(r.duration_sec for r in results), 1),
            "tokens_in": sum(r.tokens_in for r in results),
            "tokens_out": sum(r.tokens_out for r in results),
        },
        "failures": [
            {
                "source_key": r.source_key,
                "error": r.error,
                "error_class": classify_error(r.error or ""),
                "transient": is_transient(r.error or ""),
                "quarantined": r.quarantined,
                "duration_sec": r.duration_sec,
            }
            for r in failed
        ],
        "fallback_attempts": fallback_attempts or [],
    }

    key = f"{profile.prefix}{_OPS_PREFIX}{_COMPILE_RUNS_SUB}{_timestamp()}.json"
    try:
        s3.upload(key, json.dumps(summary, indent=2), content_type="application/json")
        logger.info(f"Compile run summary: {key}")
        return key
    except Exception as e:
        # We deliberately don't raise — observability must never break
        # the operation it's observing. Log it and move on; the human-
        # readable terminal summary in the CLI is still there.
        logger.warning(f"Failed to write compile run summary to {key}: {e}")
        return None


def summarize_query_failure(reason: str, detail: str = "") -> dict[str, str]:
    """Categorize a query failure for the structured summary.

    Reasons we distinguish (matching the proposal's checklist):
      - ``no_hits``           : search returned zero candidates
      - ``embedding_unavailable`` : semantic index missing or stale
      - ``llm_answer_failure``: LLM call raised
      - ``source_format_fallback`` : answer succeeded but sources
                                     section had to be rendered from raw
    """
    valid = {
        "no_hits",
        "embedding_unavailable",
        "llm_answer_failure",
        "source_format_fallback",
    }
    if reason not in valid:
        reason = "other"
    return {"reason": reason, "detail": detail}


def maintenance_key(profile: ProfileConfig) -> str:
    """Where compile keeps the state of its post-compile maintenance tasks."""
    return f"{profile.prefix}{_OPS_PREFIX}{_MAINTENANCE_FILE}"


def read_maintenance_state(s3: S3Client, profile: ProfileConfig) -> dict[str, Any]:
    """Compile's pending-maintenance state; ``{}`` when missing or unreadable.

    An empty state means "nothing known to be pending", which is how every KB
    behaved before this file existed.
    """
    try:
        data = json.loads(s3.download(maintenance_key(profile)))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_maintenance_state(s3: S3Client, profile: ProfileConfig, state: dict[str, Any]) -> None:
    """Persist the maintenance state. Best-effort, like the run summaries."""
    key = maintenance_key(profile)
    try:
        s3.upload(key, json.dumps(state, indent=2, sort_keys=True),
                  content_type="application/json")
    except Exception as e:
        logger.warning(f"Failed to write compile maintenance state to {key}: {e}")
