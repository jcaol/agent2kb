"""Output capture management (Item 2026-05-14: P2).

The compounding loop's value comes from ``outputs/`` being a *curated*
audit trail of user-typed questions. Once auto-enrichment agents start
saving every post-ingest "what is X" query, that trail bloats fast.
This module is the operator's broom: list captures with actor / age
filters, prune what's gone stale.

Layout reminder:
  <prefix>outputs/<...>.md          — actor=user (or legacy pre-actor)
  <prefix>outputs/agent/<...>.md    — actor=agent (auto-enrichment, dedup'd)
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from loguru import logger

from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import S3Client

# Filename stamps look like ``20260514-061345-<slug>.md``. We use the
# stamp as the primary creation-time signal (cheaper than head_object
# on every key) and fall back to ``LastModified`` for files that don't
# match the convention.
_STAMP_RE = re.compile(r"^(\d{8})-(\d{6})-")


@dataclass(frozen=True)
class OutputEntry:
    key: str
    actor: str  # "user" | "agent" | "unknown"
    created_at: datetime.datetime
    title: str
    also_asked_count: int


def _actor_for_key(key: str) -> str:
    """Path-based actor classification — no download required.

    Anything under ``outputs/agent/`` is an agent capture. Everything
    else under ``outputs/`` is treated as user / legacy. We don't peek
    at frontmatter here because the path layout is authoritative for
    filtering: a misclassified file is cheap to fix (move with
    ``s3mv``), but having to download N files just to list them defeats
    the purpose of the management surface.
    """
    if "/outputs/agent/" in key or key.startswith("outputs/agent/"):
        return "agent"
    return "user"


def _parse_stamp(filename: str) -> Optional[datetime.datetime]:
    m = _STAMP_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S"
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def list_entries(
    s3: S3Client,
    *,
    actor: Optional[str] = None,
    since: Optional[datetime.timedelta] = None,
    limit: Optional[int] = None,
    enrich_titles: bool = False,
) -> list[OutputEntry]:
    """List output captures with cheap metadata.

    ``enrich_titles=True`` downloads each file's frontmatter (O(N) GETs)
    to surface the title and ``also_asked`` follow-up count. ``False``
    is the default because the common case (prune) doesn't need it.
    """
    keys = s3.list_output_files()

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    cutoff = now - since if since else None

    entries: list[OutputEntry] = []
    for key in keys:
        a = _actor_for_key(key)
        if actor and a != actor:
            continue

        filename = key.split("/")[-1]
        created = _parse_stamp(filename)
        if created is None:
            # Filename doesn't match the stamp convention; fall back to
            # the s3 LastModified. One head_object per such file.
            try:
                meta = s3._client.head_object(Bucket=s3.bucket, Key=key)
                lm = meta.get("LastModified")
                created = (
                    lm if lm else datetime.datetime.now(tz=datetime.timezone.utc)
                )
            except Exception:
                created = datetime.datetime.now(tz=datetime.timezone.utc)

        if cutoff and created < cutoff:
            continue

        title = filename
        also_asked_count = 0
        if enrich_titles:
            try:
                fm, _ = Frontmatter.from_yaml_block(s3.download(key))
                title = fm.title or filename
                also_asked_count = len(
                    (fm.source_meta or {}).get("also_asked") or []
                )
            except Exception:
                pass

        entries.append(
            OutputEntry(
                key=key,
                actor=a,
                created_at=created,
                title=title,
                also_asked_count=also_asked_count,
            )
        )

    entries.sort(key=lambda e: e.created_at, reverse=True)
    if limit:
        entries = entries[:limit]
    return entries


def prune(
    s3: S3Client,
    profile: ProfileConfig,
    *,
    older_than: datetime.timedelta,
    actor: Optional[str] = None,
    apply: bool = False,
) -> list[OutputEntry]:
    """Delete captures older than ``older_than``. Default is dry-run.

    Returns the entries that were (or would have been) removed. The
    safety stance is symmetrical to ``a2kb restore``: previewing is the
    default, ``apply=True`` opts in to mutation. Refresh of the
    manifest is best-effort.
    """
    # 1y/365d ceiling for entries[] = all entries newer than 'older_than'? No.
    # We want the OLDER entries. Compute manually.
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    cutoff = now - older_than

    entries = list_entries(s3, actor=actor)
    doomed = [e for e in entries if e.created_at < cutoff]

    if not apply:
        return doomed

    try:
        from agent2kb import manifest as _mf
    except Exception:
        _mf = None

    for e in doomed:
        try:
            s3.delete(e.key)
            logger.info(f"Pruned output: {e.key}")
            if _mf is not None:
                try:
                    _mf.remove_entry(s3, profile, e.key)
                except Exception as me:  # pragma: no cover
                    logger.debug(f"manifest remove skipped: {me}")
        except Exception as ex:
            logger.warning(f"Prune failed for {e.key}: {ex}")
    return doomed


def parse_duration(text: str) -> datetime.timedelta:
    """Parse a human duration like ``30d``, ``2w``, ``12h``, ``45m``.

    Used by the CLI for both ``--since`` and ``--older-than``. We keep
    the grammar small on purpose — the ``a2kb`` surface should not grow
    its own date-parsing micro-language.
    """
    text = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", text)
    if not m:
        raise ValueError(
            f"duration must look like '30d' or '2w' (units: s/m/h/d/w), got {text!r}"
        )
    n = int(m.group(1))
    unit = m.group(2)
    return {
        "s": datetime.timedelta(seconds=n),
        "m": datetime.timedelta(minutes=n),
        "h": datetime.timedelta(hours=n),
        "d": datetime.timedelta(days=n),
        "w": datetime.timedelta(weeks=n),
    }[unit]


def format_table(entries: Iterable[OutputEntry], show_titles: bool) -> str:
    """Compact text table for ``a2kb outputs ls``.

    Returns a plain string (caller wraps in rich console.print). Format:
        2026-05-14 06:13  agent  +2  How we rotate the S3 keys: steps...
    The ``+N`` column is the also_asked follow-up count for merged
    agent captures (only meaningful when ``enrich_titles=True``).
    """
    rows: list[str] = []
    for e in entries:
        stamp = e.created_at.strftime("%Y-%m-%d %H:%M")
        actor = e.actor.ljust(5)
        followups = f"+{e.also_asked_count}" if show_titles and e.also_asked_count else "  "
        display = (e.title if show_titles else e.key.split("/")[-1])[:80]
        rows.append(f"  {stamp}  {actor}  {followups}  {display}")
    return "\n".join(rows) if rows else "  (none)"
