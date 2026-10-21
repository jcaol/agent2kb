"""ETag-keyed cache that lets a no-op compile tick skip body downloads.

Without it, every compile tick downloaded each raw/wiki pair to compare the
content hash, then downloaded every wiki page again to rebuild ``index.md``:
about ``3N`` GETs for ``N`` unchanged articles, every 15 minutes.

An S3 listing carries each object's ETag for free. The cache remembers:

* ``pairs``: raw key -> the raw and wiki ETags last verified as up to date
  (the wiki page's ``raw_content_hash`` matched the raw body), and
* ``pages``: wiki key -> its ETag and the metadata ``index.md`` needs.

An entry is used only when both ETags in the current listing equal the
stored ones, so any change to either object (compile, hand edit, restore,
another device) forces the old download path for that object alone. An
object without an ETag is never cached.

Stored at ``<prefix>_ops/compile-cache.json``, inside the KB's prefix and
outside ``_meta/``, so member KBs can use it. It is a pure speed cache: a
missing, unreadable or foreign-version file means "cold", which is exactly
how a KB behaved before it existed, and deleting it is always safe. Two
processes writing it at once can lose each other's entries; the loser just
re-verifies those objects on its next tick.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from loguru import logger

from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client

CACHE_VERSION = 1
_CACHE_FILE = "_ops/compile-cache.json"


def compile_cache_key(profile: ProfileConfig) -> str:
    return f"{profile.prefix}{_CACHE_FILE}"


class CompileCache:
    """In-memory view of the compile cache; ``save()`` writes it back if changed."""

    def __init__(self, s3: S3Client, profile: ProfileConfig):
        self.s3 = s3
        self.key = compile_cache_key(profile)
        self.pairs: dict[str, dict[str, str]] = {}
        self.pages: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.s3.download(self.key))
        except Exception:
            return  # cold cache: missing or unreadable
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return
        pairs, pages = data.get("pairs"), data.get("pages")
        if isinstance(pairs, dict):
            self.pairs = {k: v for k, v in pairs.items() if isinstance(v, dict)}
        if isinstance(pages, dict):
            self.pages = {k: v for k, v in pages.items() if isinstance(v, dict)}

    # ── raw/wiki pairs ───────────────────────────────────────────────

    def pair_current(self, raw_key: str, raw_etag: str, wiki_etag: str) -> bool:
        """True when this exact raw and wiki object were verified up to date."""
        entry = self.pairs.get(raw_key)
        return (
            bool(raw_etag and wiki_etag)
            and entry is not None
            and entry.get("raw") == raw_etag
            and entry.get("wiki") == wiki_etag
        )

    def record_pair(self, raw_key: str, raw_etag: str, wiki_etag: str) -> None:
        if not (raw_etag and wiki_etag):
            return
        entry = {"raw": raw_etag, "wiki": wiki_etag}
        if self.pairs.get(raw_key) != entry:
            self.pairs[raw_key] = entry
            self._dirty = True

    def forget_pair(self, raw_key: str) -> None:
        if self.pairs.pop(raw_key, None) is not None:
            self._dirty = True

    # ── wiki page metadata (index rebuild) ──────────────────────────

    def page_meta(self, key: str, etag: str) -> Optional[Any]:
        entry = self.pages.get(key)
        if etag and entry is not None and entry.get("etag") == etag:
            return entry.get("meta")
        return None

    def record_page(self, key: str, etag: str, meta: Any) -> None:
        if not etag:
            return
        entry = {"etag": etag, "meta": meta}
        if self.pages.get(key) != entry:
            self.pages[key] = entry
            self._dirty = True

    # ── housekeeping ────────────────────────────────────────────────

    def retain(self, *, raw_keys: Optional[Iterable[str]] = None,
               page_keys: Optional[Iterable[str]] = None) -> None:
        """Drop entries for objects that are no longer listed."""
        if raw_keys is not None:
            keep = set(raw_keys)
            for k in [k for k in self.pairs if k not in keep]:
                del self.pairs[k]
                self._dirty = True
        if page_keys is not None:
            keep = set(page_keys)
            for k in [k for k in self.pages if k not in keep]:
                del self.pages[k]
                self._dirty = True

    def save(self) -> None:
        """Persist when changed. Best-effort: a failure only costs a slower next tick."""
        if not self._dirty:
            return
        data = {"version": CACHE_VERSION, "pairs": self.pairs, "pages": self.pages}
        try:
            self.s3.upload(self.key, json.dumps(data, separators=(",", ":"), sort_keys=True),
                           content_type="application/json")
            self._dirty = False
        except Exception as e:
            logger.warning(f"Failed to write the compile cache to {self.key}: {e}")
