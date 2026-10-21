"""Per-profile page manifest for S3 metadata scaling (Item 6).

Many agent2kb operations currently scan every page in ``wiki/`` to gather
metadata — lint, query resolution, index rebuild, source/backfill
checks, concept synthesis. Each scan does N downloads. At 100 pages
this is invisible; at 5,000 pages it is 5–10 minutes of wall clock
per operation.

The manifest is an opportunistic cache: a single JSON document under
``<prefix>_meta/manifest.json`` that records the key, content hash,
title, date, tag set, source/original_link, page kind, wikilink summary,
and last-indexed timestamp for every wiki/output page. Operations that
only need metadata read the manifest in one S3 GET instead of N.

Consistency model:
  * The manifest is **eventually consistent** with S3 truth. Compile,
    promote, clip, and backfill flows opportunistically update the
    entry for the page they touched. ``a2kb manifest rebuild`` is the
    authoritative recovery — never trust the manifest blindly for
    correctness-critical operations.
  * If the manifest is missing, stale (older than the page it
    describes), or fails to parse, consumers fall back to the slow
    per-page download path. The manifest is a speed optimization, not
    a source-of-truth.

Schema (manifest.json):
    {
      "version": 1,
      "generated_at": "2026-05-13T10:00:00",
      "prefix": "personal/",
      "entries": {
        "personal/wiki/20260411-foo.md": {
          "hash": "sha256:...",
          "title": "Foo",
          "date": "2026-04-11",
          "tags": ["llm", "rag"],
          "source": "https://...",
          "original_link": "https://...",
          "kind": "article",
          "wikilink_count": 7,
          "indexed_at": "2026-05-13T10:00:00"
        },
        ...
      }
    }
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Callable, Iterable, Optional

from loguru import logger

from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import PreconditionFailed, S3Client
from agent2kb.utils import extract_wikilinks

MANIFEST_VERSION = 1
MANIFEST_KEY_SUFFIX = "_meta/manifest.json"


def manifest_key_for(profile: ProfileConfig) -> str:
    """S3 key where this profile's manifest lives."""
    return f"{profile.prefix}{MANIFEST_KEY_SUFFIX}"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _entry_from_content(key: str, content: str) -> dict:
    """Build a single manifest entry by parsing a page's full content."""
    fm, body = Frontmatter.from_yaml_block(content)
    kind = fm.source_kind.value if fm.source_kind else Frontmatter.infer_kind_from_key(key).value
    h = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    return {
        "hash": f"sha256:{h}",
        "title": fm.title or "",
        "date": fm.date or "",
        "tags": list(fm.tags or []),
        "source": fm.source or "",
        "original_link": fm.original_link or "",
        "kind": kind,
        "wikilink_count": len(extract_wikilinks(body)),
        "indexed_at": _now(),
    }


def rebuild(
    s3: S3Client,
    profile: ProfileConfig,
    *,
    include_outputs: bool = True,
) -> dict:
    """Rebuild the manifest from scratch by walking wiki/ and outputs/.

    O(N) downloads — call sparingly. Prefer ``update_entry`` from inside
    flows that already have the content in hand.
    """
    entries: dict = {}

    for key in s3.list_wiki_files():
        try:
            content = s3.download(key)
            entries[key] = _entry_from_content(key, content)
        except Exception as e:
            logger.debug(f"manifest: skip {key} — {e}")

    if include_outputs:
        for key in s3.list_output_files():
            try:
                content = s3.download(key)
                entries[key] = _entry_from_content(key, content)
            except Exception as e:
                logger.debug(f"manifest: skip {key} — {e}")

    manifest = {
        "version": MANIFEST_VERSION,
        "generated_at": _now(),
        "prefix": profile.prefix,
        "entries": entries,
    }

    try:
        s3.upload(
            manifest_key_for(profile),
            json.dumps(manifest, indent=2, sort_keys=True),
            content_type="application/json",
        )
        logger.info(f"Manifest rebuilt: {len(entries)} entries")
    except Exception as e:
        logger.warning(f"Failed to upload manifest: {e}")
    return manifest


def _parse(raw: str, key: str) -> Optional[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Manifest at {key} is unparseable: {e}")
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        version = data.get("version") if isinstance(data, dict) else None
        logger.warning(
            f"Manifest version mismatch (expected {MANIFEST_VERSION}, "
            f"got {version!r}) — treat as stale"
        )
        return None
    return data


def load(s3: S3Client, profile: ProfileConfig) -> Optional[dict]:
    """Return the parsed manifest, or ``None`` if missing/unparseable.

    Callers must tolerate ``None`` — the manifest is a speed cache, not
    a hard dependency. Falling back to per-page downloads is always
    correct, just slower.
    """
    key = manifest_key_for(profile)
    try:
        raw = s3.download(key)
    except Exception:
        return None
    return _parse(raw, key)


#: Re-reads of the manifest when a conditional write lost a race.
_WRITE_ATTEMPTS = 5


def _modify(s3: S3Client, profile: ProfileConfig, mutate: Callable[[dict], bool]) -> bool:
    """Read-modify-write the manifest under If-Match, retrying on a conflict.

    ``mutate`` changes the parsed manifest in place and returns whether
    anything changed. Two writers (compile and an MCP save, say) used to
    overwrite each other's entries; now the loser re-reads and re-applies.
    Returns False when there is no usable manifest or the write failed.
    Never raises: the manifest is a cache.
    """
    key = manifest_key_for(profile)
    for _attempt in range(_WRITE_ATTEMPTS):
        try:
            raw, etag = s3.download_with_etag(key)
        except Exception:
            # No manifest yet — don't auto-rebuild here (caller may be in a
            # hot loop). User can run ``a2kb manifest rebuild`` once.
            return False
        manifest = _parse(raw, key)
        if manifest is None:
            return False
        try:
            if not mutate(manifest):
                return True
            manifest["generated_at"] = _now()
            s3.upload(
                key,
                json.dumps(manifest, indent=2, sort_keys=True),
                content_type="application/json",
                if_match=etag or None,
            )
            return True
        except PreconditionFailed:
            continue
        except Exception as e:
            logger.debug(f"manifest: update failed — {e}")
            return False
    logger.debug(f"manifest: {key} kept changing; update skipped")
    return False


def update_entry(
    s3: S3Client,
    profile: ProfileConfig,
    key: str,
    content: str,
) -> None:
    """Opportunistically refresh a single manifest entry.

    Designed for callers that just wrote ``key`` and still have its
    content in memory — compile, promote, save_output, clip. Cheap:
    one GET + one conditional PUT (repeated if another writer got in
    first). Silent failure: a manifest update problem must never break
    the caller's write path.
    """
    entry = _entry_from_content(key, content)

    def mutate(manifest: dict) -> bool:
        manifest.setdefault("entries", {})[key] = entry
        return True

    _modify(s3, profile, mutate)


def remove_entry(
    s3: S3Client,
    profile: ProfileConfig,
    key: str,
) -> None:
    """Drop ``key`` from the manifest (page was deleted/moved)."""
    remove_entries(s3, profile, [key])


def remove_entries(
    s3: S3Client,
    profile: ProfileConfig,
    keys: "Iterable[str]",
) -> int:
    """Drop several ``keys`` from the manifest in one GET + one PUT.

    The batched cousin of ``remove_entry`` for bulk-delete flows
    (``a2kb rm --older-than``). Calling ``remove_entry`` in a loop would do
    N GET/PUT round-trips against ``_meta/manifest.json``; this collapses
    that to a single read-modify-write. Returns the number of entries
    actually removed. Silent failure: a manifest hiccup must never break
    the caller's S3 delete path — the page is already gone, the cache is
    just a speed optimization (see module docstring).
    """
    keys = list(keys)
    removed: list[str] = []

    def mutate(manifest: dict) -> bool:
        entries = manifest.get("entries", {})
        removed[:] = [k for k in keys if entries.pop(k, None) is not None]
        return bool(removed)

    if not _modify(s3, profile, mutate):
        return 0
    return len(removed)
