"""Decide whether a source is already in raw/.

Raw filenames are ``<YYYYMMDD>-<slug>-<hash8>.md``. The date is the day of
ingest, so comparing whole keys only catches a duplicate on the day it was
first ingested. Web Clipper imports did exactly that: a clipping that came
back from an Obsidian device's sync on a later day got a new date prefix,
was treated as new, and was compiled again — 21 extra copies across 8
articles in one KB, each costing a compile, a duplicate wiki page, and
inflated concept reference counts.

Two independent signals are used instead, neither involving the date:

- the content hash suffix, for identical bodies, and
- the normalised source URL, for the same page re-shared with different
  tracking parameters (``?fbclid=``, ``utm_*``) or host spelling
  (``twitter.com`` vs ``x.com``).
"""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from agent2kb.models import Frontmatter

_HASH_SUFFIX = re.compile(r"-([0-9a-f]{8})\.md$")

#: Query parameters that only identify the click, never the page.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "igshid", "igsh", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok",
    "ref_src", "ref_url", "si", "spm", "share_id", "trk", "trkCampaign",
})

#: Per-host parameters that are tracking there but meaningful elsewhere
#: (YouTube's ``t`` is a timestamp; on x.com ``s``/``t`` only tag the share).
_HOST_TRACKING_PARAMS = {
    "x.com": frozenset({"s", "t"}),
    "youtube.com": frozenset({"feature", "pp"}),
    "youtu.be": frozenset({"feature"}),
}

_HOST_ALIASES = {
    "twitter.com": "x.com",
    "fxtwitter.com": "x.com",
    "vxtwitter.com": "x.com",
    "fixupx.com": "x.com",
}


def normalize_url(url: str) -> str:
    """Canonical form of ``url`` for duplicate detection only.

    Conservative by design: it drops what is known to be noise (tracking
    parameters, ``www.``/``m.`` hosts, scheme, fragment, trailing slash) and
    keeps every other parameter, so two genuinely different pages never
    collapse into one.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.netloc:
        return raw

    host = parts.hostname or ""
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = _HOST_ALIASES.get(host, host)
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    host_drop = _HOST_TRACKING_PARAMS.get(host, frozenset())
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k not in _TRACKING_PARAMS
        and k not in host_drop
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def hash_suffix(key: str) -> Optional[str]:
    """The ``<hash8>`` from a raw filename, or None."""
    match = _HASH_SUFFIX.search(key)
    return match.group(1) if match else None


#: Persisted source-URL lookup, relative to the KB prefix. See ``RawIndex``.
URL_INDEX_FILE = "_ops/url-index.json"
_URL_INDEX_VERSION = 1


def _source_urls(content: str) -> list[str]:
    """Normalised source URLs named in a raw file's frontmatter."""
    fm, _ = Frontmatter.from_yaml_block(content)
    return [normalize_url(link) for link in (fm.original_link, fm.source)
            if link and "://" in link]


class RawIndex:
    """What raw/ already holds, for duplicate checks.

    Includes ``raw/.failed/``: a source quarantined after repeated compile
    failures must not be re-imported into the same failure loop.

    Hashes come from the listing alone. Source URLs need each frontmatter;
    they are kept in ``<prefix>_ops/url-index.json`` keyed by raw key and
    ETag, so a URL lookup downloads only the raw files added or changed
    since the last one (in parallel) instead of all of raw/ on every ingest.
    The file is a cache: missing, unreadable or stale entries are rebuilt
    from the bodies, and it is rewritten only when it changed.
    """

    def __init__(self, s3):
        self.s3 = s3
        objects = s3.list_objects(sub_prefix="raw/", suffix=".md")
        self.keys: list[str] = [o.key for o in objects]
        self._etags: dict[str, str] = {o.key: o.etag for o in objects}
        self._by_hash: dict[str, str] = {}
        for key in self.keys:
            h = hash_suffix(key)
            if h:
                self._by_hash.setdefault(h, key)
        self._by_url: Optional[dict[str, str]] = None

    @property
    def _store_key(self) -> str:
        return f"{self.s3.prefix}{URL_INDEX_FILE}"

    def _read_store(self) -> dict:
        try:
            data = json.loads(self.s3.download(self._store_key))
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("version") != _URL_INDEX_VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _load_urls(self) -> dict[str, str]:
        if self._by_url is not None:
            return self._by_url
        stored = self._read_store()
        fresh: dict[str, dict] = {}
        stale: list[str] = []
        for key in self.keys:
            entry = stored.get(key)
            etag = self._etags.get(key, "")
            if (etag and isinstance(entry, dict) and entry.get("etag") == etag
                    and isinstance(entry.get("urls"), list)):
                fresh[key] = entry
            else:
                stale.append(key)
        bodies = self.s3.download_many(stale)
        for key in stale:
            content = bodies.get(key)
            if content is None:
                continue
            try:
                urls = _source_urls(content)
            except Exception as e:
                logger.debug(f"dedup: unreadable frontmatter in {key}: {e}")
                urls = []
            fresh[key] = {"etag": self._etags.get(key, ""), "urls": urls}

        self._by_url = {}
        for key in self.keys:  # listing order, so the first copy wins as before
            for url in (fresh.get(key) or {}).get("urls", []):
                self._by_url.setdefault(url, key)

        persist = {k: v for k, v in fresh.items() if v.get("etag")}
        if persist != stored:
            try:
                self.s3.upload(
                    self._store_key,
                    json.dumps({"version": _URL_INDEX_VERSION, "entries": persist},
                               separators=(",", ":"), sort_keys=True),
                    content_type="application/json",
                )
            except Exception as e:
                logger.debug(f"dedup: could not update {self._store_key}: {e}")
        return self._by_url

    def by_hash(self, short_hash: str) -> Optional[str]:
        return self._by_hash.get(short_hash)

    def by_url(self, url: str) -> Optional[str]:
        if not url or "://" not in url:
            return None
        return self._load_urls().get(normalize_url(url))

    def add(self, key: str, url: str = "") -> None:
        """Record a key written during this run."""
        self.keys.append(key)
        h = hash_suffix(key)
        if h:
            self._by_hash.setdefault(h, key)
        if url and self._by_url is not None:
            self._by_url.setdefault(normalize_url(url), key)
