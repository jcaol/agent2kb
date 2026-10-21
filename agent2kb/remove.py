"""a2kb rm — safe removal of raw ingests and their derived artifacts.

The agent2kb store is a derivation chain rooted at ``raw/`` (the source of
truth). Every other layer is derived from it:

    raw/<file>.md
      └─ wiki/<file>.md          (LLM compile — same basename, mirrored)
           ├─ _embeddings/index.json entry   (keyed by the wiki S3 key)
           └─ _meta/manifest.json entry      (keyed by the wiki S3 key)

Because the wiki filename mirrors the raw filename exactly
(``raw/X.md`` → ``wiki/X.md``, see ``Compiler._wiki_key_for``), the whole
chain is recoverable from a single basename — no reverse index needed.

A correct ``rm`` therefore deletes a *chain*, not a single object, so the
KB stays internally consistent: a subsequent ``a2kb query`` won't surface a
deleted page (embedding entry gone) and ``a2kb status`` / lint won't trip
over a dangling manifest row.

This module is the pure-logic layer (no typer, no console) so it can be
unit-tested with an in-memory fake S3. The thin CLI wiring lives in
``agent2kb.main``.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from loguru import logger

from agent2kb import manifest as _manifest
from agent2kb.embeddings import _INDEX_KEY_SUFFIX
from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import S3Client
from agent2kb.utils import extract_wikilinks, slugify

# Structural pages that ``rm`` must never delete — they are not articles
# and removing them would corrupt the wiki. ``concepts/`` and ``topics/``
# pages are deletable (they're real content) but only via an exact --file
# match, never by a loose --slug substring (guarded in ``_name_matches``).
_PROTECTED_BASENAMES = frozenset({"index.md", "log.md", ".gitkeep"})

# Delete-scope: which layers of the derivation chain to remove.
SCOPE_ALL = "all"
SCOPE_RAW = "raw"
SCOPE_WIKI = "wiki"

# Layer labels used in the deletion plan / report.
LAYER_RAW = "raw"
LAYER_WIKI = "wiki"
LAYER_EMBEDDING = "embedding"
LAYER_MANIFEST = "manifest"


@dataclass
class RemovalMatch:
    """One article identified for removal, with every layer key it owns.

    ``basename`` is the shared identity: for a normal article it is the
    mirrored raw/wiki filename (``20260531-foo-7c3eb3bf.md``); for a
    wiki-only page (concept/topic, or an orphaned compile) it is the path
    relative to ``wiki/`` (``concepts/foo.md``).
    """

    basename: str
    raw_key: Optional[str] = None
    wiki_key: Optional[str] = None
    # True when the wiki object is already gone but a derived row
    # (embedding/manifest) still references it — see ``collect`` self-heal.
    wiki_orphan: bool = False
    # Populated lazily by ``enrich`` / url + bulk selection.
    title: str = ""
    original_link: str = ""
    date: str = ""
    tags: list[str] = field(default_factory=list)
    last_modified: Optional[datetime.datetime] = None

    @property
    def stem(self) -> str:
        return self.basename[:-3] if self.basename.endswith(".md") else self.basename

    @property
    def label(self) -> str:
        return self.title or self.basename


@dataclass
class LayerDeletion:
    """A single (S3 key, layer) pair scheduled for deletion."""

    key: str
    layer: str


@dataclass
class RemovalReport:
    """Outcome of applying a deletion plan."""

    deletions: list[LayerDeletion]
    objects_deleted: int = 0
    embeddings_removed: int = 0
    manifest_removed: int = 0


# ─────────────────────────── scope ──────────────────────────────────


def resolve_scope(*, raw: bool, wiki: bool) -> str:
    """Map the --raw/--wiki flags to a scope.

    Neither flag (or both) → ``all`` (delete the whole chain). ``--raw``
    alone → only the raw object. ``--wiki`` alone → the wiki page plus its
    embedding + manifest entries (those derive from the wiki key, so they
    go with it).
    """
    if raw and not wiki:
        return SCOPE_RAW
    if wiki and not raw:
        return SCOPE_WIKI
    return SCOPE_ALL


# ─────────────────────────── collection ─────────────────────────────


def _owned_key(profile: ProfileConfig, key: str, sub: str) -> bool:
    """True when ``key`` is a markdown object directly under this profile's
    ``<prefix><sub>`` with no path tricks.

    ``rm`` deletes keys it did not list itself: orphan rows read back from
    the manifest and the embedding index. Those files are data, so a key in
    them must prove it belongs to this profile's raw/ or wiki/ before it can
    reach the deletion plan — otherwise a stray or planted row such as
    ``<prefix>outputs/wiki/saved-answer.md`` gets deleted as a "wiki page".
    """
    head = f"{profile.prefix}{sub}"
    if not key.startswith(head) or not key.endswith(".md"):
        return False
    segments = key[len(head):].split("/")
    return all(seg not in ("", ".", "..") for seg in segments)


def _rel(profile: ProfileConfig, key: str, sub: str) -> Optional[str]:
    """Return ``key`` relative to ``<prefix><sub>`` (e.g. sub='raw/').

    None for anything outside it. There is deliberately no basename
    fallback: matching an out-of-place key by its leaf name is how a key
    from another area ends up selected by ``--file``.
    """
    if not _owned_key(profile, key, sub):
        return None
    return key[len(f"{profile.prefix}{sub}"):]


def collect(s3: S3Client, profile: ProfileConfig) -> dict[str, RemovalMatch]:
    """Build the basename → RemovalMatch index over raw/ and wiki/.

    Cheap: two LIST calls, zero downloads. Frontmatter is only fetched
    later for the items that actually match (``enrich`` / url selection).
    """
    matches: dict[str, RemovalMatch] = {}

    for rk in s3.list_raw_files():
        base = _rel(profile, rk, "raw/")
        if base is None or base in _PROTECTED_BASENAMES:
            continue
        matches.setdefault(base, RemovalMatch(basename=base)).raw_key = rk

    for wk in s3.list_wiki_files():
        rel = _rel(profile, wk, "wiki/")
        if rel is None or rel.split("/")[-1] in _PROTECTED_BASENAMES:
            continue
        matches.setdefault(rel, RemovalMatch(basename=rel)).wiki_key = wk

    # Self-heal: surface orphaned derived entries whose wiki object was
    # already deleted out-of-band (e.g. a manual S3 delete done before
    # ``a2kb rm`` existed). These leave a stale row in the embedding index
    # and/or manifest that the two LISTs above can't see — without this,
    # ``rm`` would report "nothing matched" yet ``query`` keeps surfacing
    # a key it can no longer download. We register the wiki key anyway so
    # the normal selection + deletion path purges the stale rows; the
    # object delete itself is a harmless no-op (apply skips absent keys).
    for wk in _orphan_wiki_keys(s3, profile):
        rel = _rel(profile, wk, "wiki/")
        if rel is None or rel.split("/")[-1] in _PROTECTED_BASENAMES:
            continue
        m = matches.setdefault(rel, RemovalMatch(basename=rel))
        if m.wiki_key is None:
            m.wiki_key = wk
            m.wiki_orphan = True

    return matches


def _orphan_wiki_keys(s3: S3Client, profile: ProfileConfig) -> set[str]:
    """Collect wiki keys that live only in the derived caches.

    Reads the manifest and the embedding index directly (model-agnostic
    — we never gate on the embedding model here, otherwise a stale entry
    written by a different provider would be invisible). Best-effort:
    any read failure yields an empty contribution.
    """
    keys: set[str] = set()
    try:
        man = _manifest.load(s3, profile)
        if man:
            keys.update(man.get("entries", {}))
    except Exception as e:
        logger.debug(f"rm: manifest scan for orphans failed — {e}")
    try:
        keys.update(_embedding_keys(s3, profile))
    except Exception as e:
        logger.debug(f"rm: embedding scan for orphans failed — {e}")
    # Rows are untrusted data: keep only keys this profile actually owns.
    return {k for k in keys if isinstance(k, str) and _owned_key(profile, k, "wiki/")}


def _frontmatter_for(s3: S3Client, m: RemovalMatch) -> Optional[Frontmatter]:
    """Download + parse the frontmatter for a match (prefers raw)."""
    key = m.raw_key or m.wiki_key
    if not key:
        return None
    try:
        fm, _ = Frontmatter.from_yaml_block(s3.download(key))
        return fm
    except Exception as e:
        logger.debug(f"rm: cannot read frontmatter for {key} — {e}")
        return None


def enrich(s3: S3Client, matches: Iterable[RemovalMatch]) -> None:
    """Fill in title/original_link/date/tags for display (best-effort)."""
    for m in matches:
        if m.title or m.original_link:
            continue
        fm = _frontmatter_for(s3, m)
        if fm is None:
            continue
        m.title = fm.title or ""
        m.original_link = fm.original_link or fm.source or ""
        m.date = fm.date or ""
        m.tags = list(fm.tags or [])


# ─────────────────────────── selection ──────────────────────────────


def _name_matches(m: RemovalMatch, *, slug: Optional[str],
                  file: Optional[str], id: Optional[str]) -> bool:
    """Pure filename matching (no downloads) for --slug/--file/--id."""
    base = m.basename
    stem = m.stem
    leaf = base.split("/")[-1]            # last path segment, with .md
    leaf_stem = leaf[:-3] if leaf.endswith(".md") else leaf

    if file is not None:
        # Exact, precise targeting — accept with or without .md, and match
        # either the full relative path or just the leaf filename.
        want = file
        want_stem = want[:-3] if want.endswith(".md") else want
        return want in (base, stem, leaf, leaf_stem) or want_stem in (stem, leaf_stem)

    if id is not None:
        # Match the trailing content-hash segment (…-7c3eb3bf.md). Fall back
        # to substring so a partial hash still resolves when unambiguous.
        segs = stem.split("-")
        return stem.endswith("-" + id) or segs[-1] == id or id in stem

    if slug is not None:
        # Refuse to match concept/topic pages by loose slug so a stray
        # slug can't sweep curated pages — those require an exact --file
        # (full relative path).
        if "/" in base:
            return slug in (base, stem)
        # Articles: exact stem/filename, else substring on the stem.
        if slug in (base, stem, leaf, leaf_stem):
            return True
        return slug in stem

    return False


def select(s3: S3Client, profile: ProfileConfig,
           matches: dict[str, RemovalMatch], *,
           url: Optional[str] = None, slug: Optional[str] = None,
           file: Optional[str] = None, id: Optional[str] = None
           ) -> list[RemovalMatch]:
    """Resolve a single-item selector to the matching articles.

    Returns 0, 1, or many matches. The CLI treats >1 as "ambiguous —
    narrow your selector"; this layer never silently picks one.
    """
    if url is not None:
        target = url.strip()
        hits: list[RemovalMatch] = []
        for m in matches.values():
            fm = _frontmatter_for(s3, m)
            if fm is None:
                continue
            if target in (fm.original_link, fm.source):
                m.title = fm.title or ""
                m.original_link = fm.original_link or fm.source or ""
                m.date = fm.date or ""
                m.tags = list(fm.tags or [])
                hits.append(m)
        return hits

    return [
        m for m in matches.values()
        if _name_matches(m, slug=slug, file=file, id=id)
    ]


def select_bulk(s3: S3Client, profile: ProfileConfig,
                matches: dict[str, RemovalMatch], *,
                older_than: datetime.timedelta,
                tag: Optional[str] = None) -> list[RemovalMatch]:
    """Resolve the bulk selector (--older-than [+ --tag]).

    Age is taken from the S3 object's last-modified time (authoritative,
    survives frontmatter drift), falling back to the frontmatter ``date``
    when S3 metadata is unavailable. ``--tag`` filters on frontmatter tags.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - older_than
    selected: list[RemovalMatch] = []

    for m in matches.values():
        fm = _frontmatter_for(s3, m)
        if fm is not None:
            m.title = fm.title or ""
            m.original_link = fm.original_link or fm.source or ""
            m.date = fm.date or ""
            m.tags = list(fm.tags or [])

        if tag is not None and tag not in m.tags:
            continue

        age_dt = _age_of(s3, m)
        if age_dt is None or age_dt > cutoff:
            continue
        m.last_modified = age_dt
        selected.append(m)

    return selected


def _age_of(s3: S3Client, m: RemovalMatch) -> Optional[datetime.datetime]:
    """Best-effort UTC timestamp for a match (S3 mtime → frontmatter date)."""
    key = m.raw_key or m.wiki_key
    if key:
        try:
            meta = s3.get_metadata(key)
            lm = meta.get("last_modified")
            if isinstance(lm, datetime.datetime):
                if lm.tzinfo is None:
                    lm = lm.replace(tzinfo=datetime.timezone.utc)
                return lm
        except Exception as e:
            logger.debug(f"rm: no S3 mtime for {key} — {e}")
    if m.date:
        try:
            d = datetime.datetime.strptime(m.date[:10], "%Y-%m-%d")
            return d.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    return None


# ─────────────────────────── planning ───────────────────────────────


def build_deletions(profile: ProfileConfig, selected: Iterable[RemovalMatch],
                    scope: str) -> list[LayerDeletion]:
    """Expand selected articles + scope into concrete (key, layer) deletions.

    Deleting a wiki page always drags its embedding + manifest entries
    with it (both keyed by the wiki S3 key) — otherwise ``query`` keeps
    returning a page that no longer exists.
    """
    dels: list[LayerDeletion] = []
    for m in selected:
        # Second gate, independent of how ``m`` was built: never plan a
        # delete outside this profile's raw/ and wiki/.
        if m.raw_key and not _owned_key(profile, m.raw_key, "raw/"):
            logger.warning(f"rm: refusing to delete {m.raw_key} — not under {profile.prefix}raw/")
            m.raw_key = None
        if m.wiki_key and not _owned_key(profile, m.wiki_key, "wiki/"):
            logger.warning(f"rm: refusing to delete {m.wiki_key} — not under {profile.prefix}wiki/")
            m.wiki_key = None
        if scope in (SCOPE_ALL, SCOPE_RAW) and m.raw_key:
            dels.append(LayerDeletion(m.raw_key, LAYER_RAW))
        if scope in (SCOPE_ALL, SCOPE_WIKI) and m.wiki_key:
            dels.append(LayerDeletion(m.wiki_key, LAYER_WIKI))
            dels.append(LayerDeletion(m.wiki_key, LAYER_EMBEDDING))
            dels.append(LayerDeletion(m.wiki_key, LAYER_MANIFEST))
    return dels


def orphaned_raw_warnings(selected: Iterable[RemovalMatch], scope: str) -> list[str]:
    """Warn when --raw leaves a compiled wiki page with no source behind it."""
    if scope != SCOPE_RAW:
        return []
    return [
        f"wiki page still present with no raw source: {m.wiki_key}"
        for m in selected
        if m.wiki_key
    ]


# ─────────────────────────── execution ──────────────────────────────


def _embedding_index_key(profile: ProfileConfig) -> str:
    return f"{profile.prefix}{_INDEX_KEY_SUFFIX}"


def _embedding_keys(s3: S3Client, profile: ProfileConfig) -> set[str]:
    """Return the set of wiki keys present in the embedding index.

    Reads the raw JSON directly so it is independent of the embedding
    model recorded in the index (a model mismatch must not hide a key
    we may need to purge).
    """
    import json

    try:
        data = json.loads(s3.download(_embedding_index_key(profile)))
    except Exception:
        return set()
    return {it.get("key") for it in data.get("items", []) if it.get("key")}


def _purge_embedding_entries(s3: S3Client, profile: ProfileConfig,
                             wiki_keys: Iterable[str]) -> int:
    """Drop ``wiki_keys`` from the embedding index JSON in place.

    One GET + one PUT, model-agnostic. Returns the number of vectors
    removed. No-op (and no PUT) when nothing matched, keeping repeated
    applies idempotent.
    """
    import json

    key = _embedding_index_key(profile)
    try:
        data = json.loads(s3.download(key))
    except Exception:
        return 0
    items = data.get("items", [])
    doomed = set(wiki_keys)
    kept = [it for it in items if it.get("key") not in doomed]
    removed = len(items) - len(kept)
    if removed:
        data["items"] = kept
        s3.upload(key, json.dumps(data, separators=(",", ":")))
    return removed


def apply_deletions(s3: S3Client, profile: ProfileConfig,
                    deletions: Iterable[LayerDeletion]) -> RemovalReport:
    """Execute a deletion plan against S3 + the embedding/manifest caches.

    Object deletes happen first; the embedding index and manifest are then
    updated in a single read-modify-write each (not per-key) so a bulk
    prune doesn't hammer ``index.json`` / ``manifest.json``.
    """
    deletions = list(deletions)
    report = RemovalReport(deletions=deletions)

    object_keys = [d.key for d in deletions if d.layer in (LAYER_RAW, LAYER_WIKI)]
    wiki_keys = [d.key for d in deletions if d.layer == LAYER_WIKI]

    for key in object_keys:
        try:
            # Only count objects that actually existed — keeps the summary
            # honest and makes a repeated apply a true no-op (S3's
            # delete_object succeeds even on a missing key).
            if not s3.exists(key):
                continue
            s3.delete(key)
            report.objects_deleted += 1
        except Exception as e:
            logger.warning(f"rm: failed to delete {key} — {e}")

    if wiki_keys:
        # Embedding index: model-agnostic low-level purge (one GET + one
        # PUT). We deliberately do NOT go through EmbeddingIndex.load(),
        # because that discards the whole index on an embedding-model
        # mismatch — which would silently skip cleanup of a stale row
        # written by a different provider (e.g. a gemini-built index
        # being purged from an ollama-only environment).
        try:
            report.embeddings_removed = _purge_embedding_entries(s3, profile, wiki_keys)
        except Exception as e:
            logger.warning(f"rm: embedding index cleanup failed — {e}")

        # Manifest: batched remove (one GET + one PUT).
        try:
            report.manifest_removed = _manifest.remove_entries(s3, profile, wiki_keys)
        except Exception as e:
            logger.debug(f"rm: manifest cleanup failed — {e}")

    return report


# ─────────────────── optional: orphaned backlink report ──────────────


_DATE_PREFIX_RE = re.compile(r"^\d{8}-")
_HASH_SUFFIX_RE = re.compile(r"-[0-9a-f]{8}$")


def _link_target(link: str) -> str:
    """The page part of a wikilink: drop ``|alias`` and ``#anchor``."""
    return link.split("|", 1)[0].split("#", 1)[0].strip()


def _link_keys(name: str) -> set[str]:
    """Lookup forms for one link text or page name (lowercase, and slugified)."""
    name = name.strip().lower()
    return {name, slugify(name).lower()} - {""}


def find_orphaned_backlinks(s3: S3Client, profile: ProfileConfig,
                            removed: Iterable[RemovalMatch]) -> list[str]:
    """Report pages whose ``[[wikilinks]]`` now point at a removed page.

    Best-effort and read-only. :func:`derive_link_targets` says which link texts used to
    reach each removed page. A link is only reported when it matches one of those *and*
    no surviving page answers to the same name (a concept page ``Redis`` keeps
    ``[[Redis]]`` valid even when an article titled "Redis" is removed).
    """
    removed = list(removed)
    if not removed:
        return []
    enrich(s3, removed)  # titles for the heuristic; no-op when already known

    targets: dict[str, str] = {}  # lookup key → basename it pointed at
    for m in removed:
        for text in derive_link_targets(m):
            for k in _link_keys(text):
                targets[k] = m.basename
    if not targets:
        return []

    removed_keys = {m.wiki_key for m in removed if m.wiki_key}
    survivors: list[tuple[str, str]] = []  # (key, body)
    alive: set[str] = set()  # every name a surviving page answers to
    for key in s3.list_wiki_files():
        if key in removed_keys:
            continue
        try:
            fm, body = Frontmatter.from_yaml_block(s3.download(key))
        except Exception:
            continue
        survivors.append((key, body))
        # Relative to the prefix: splitting on "/wiki/" misses a whole-bucket
        # KB's "wiki/x.md" keys.
        rel = _rel(profile, key, "wiki/") or key.split("/")[-1]
        names = [rel[:-3] if rel.endswith(".md") else rel, fm.title or ""]
        names.append(names[0].split("/")[-1])
        for n in names:
            alive |= _link_keys(n) if n else set()

    warnings: list[str] = []
    for key, body in survivors:
        for link in extract_wikilinks(body):
            forms = _link_keys(_link_target(link))
            hit = next((targets[f] for f in forms if f in targets), None)
            if hit and not forms & alive:
                warnings.append(f"{key.split('/')[-1]} still links to [[{link}]] (→ removed {hit})")
    return sorted(set(warnings))


def derive_link_targets(m: RemovalMatch) -> list[str]:
    """Return the ``[[wikilink]]`` texts that would point at page ``m``.

    Compile writes ``[[Title]]`` and concept pages are also reached by their short slug,
    so a page can be referenced by:

    - its title, verbatim (needs ``m.title``; filled in by :func:`enrich`),
    - its filename stem, e.g. ``20260531-Rahul-on-X-7c3eb3bf``,
    - the stem without the date prefix and hash suffix (``Rahul-on-X``) and its
      humanized form (``Rahul on X``),
    - the path form Obsidian accepts for concepts/topics: ``concepts/foo``.

    Matching is case-insensitive and slug-tolerant (see :func:`_link_keys`). Very short
    de-dated names are skipped: they would collide with unrelated pages.
    """
    stem = m.stem
    leaf = stem.split("/")[-1]
    out: list[str] = [stem, leaf]
    if m.title:
        out.append(m.title)
    bare = _HASH_SUFFIX_RE.sub("", _DATE_PREFIX_RE.sub("", leaf))
    if bare != leaf and len(bare) >= 4:
        out.extend([bare, bare.replace("-", " ")])
    seen: set[str] = set()
    return [t for t in out if t and not (t.lower() in seen or seen.add(t.lower()))]
