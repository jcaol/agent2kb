"""Semantic search index for agent2kb.

Hybrid retrieval: keyword IDF gives the candidate set (the keyword layer
in ``query.py``), embedding cosine similarity reranks within. The
embedding index lives on S3 alongside the wiki so it travels with the
KB and survives across machines.

Design choices:

- **Single JSON blob** at ``<prefix>_embeddings/index.json`` rather than
  one file per page. A 1000-page KB at 768-dim float32 weighs ~3MB —
  trivially fits in memory and one S3 fetch beats per-page round trips.
  Atomic write also sidesteps cross-page consistency questions.

- **Provider-agnostic** ``EmbeddingClient`` — Gemini if a key is set
  (free tier, multilingual), otherwise Ollama (local, no key, requires
  ``ollama pull bge-m3``). OpenAI is supported when ``openai_api_key``
  is set in the profile config. The provider is recorded in the index
  metadata so a model switch invalidates the whole index cleanly.

- **Incremental embeds** keyed by content hash. Compile re-runs only
  embed pages whose content changed since the last index save, so a
  full KB re-embed is only required after a model change.

- **Cosine similarity in pure Python** (no numpy dep). For ~1000 pages
  this is ~1s per query — acceptable for a personal KB. If your KB
  grows past 10k pages, switch to numpy / faiss.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, replace
from typing import Optional

from loguru import logger

from agent2kb.extras import MissingExtraError, import_optional
from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client

# Default model names. These are **fallbacks** — the actual model is
# resolved from ``profile.embedding_model`` / ``profile.rerank_model``
# first, so a new generation (voyage-5-lite, gemini-embedding-002, …)
# is a config flip rather than a agent2kb release. Vectors from different
# models are not comparable, so changing the resolved model triggers a
# full re-embed (the saved index records the model name and is
# discarded on mismatch).
#
# Gemini: ``gemini-embedding-001`` is the current stable. The earlier
# ``text-embedding-004`` was retired in favour of the unified gemini-*
# embedding family — keep this constant in sync if Google ships another
# generation, since vectors won't be comparable across model families.
# Note: the new google.genai SDK takes the bare model id (no `models/`
# prefix); the deprecated google.generativeai SDK required the prefix.
GEMINI_EMBED_MODEL = "gemini-embedding-001"
OLLAMA_EMBED_MODEL = "bge-m3"
OPENAI_EMBED_MODEL = "text-embedding-3-small"

# Voyage: ``voyage-4-lite`` is the entry-level model with the same
# $0.02/M-token paid price as OpenAI 3-small but with 200M free per
# account. A multilingual model, so KBs in languages other than English embed
# well too. Bigger options if quality becomes an issue:
#   voyage-4         ($0.06/M, 1024-dim)
#   voyage-4-large   ($0.12/M, 1024-dim, flagship)
# Always start with 4-lite — the upgrade is one config flip + reindex.
VOYAGE_EMBED_MODEL = "voyage-4-lite"

# Truncate Gemini's 3072-dim native vectors to keep the on-disk index
# small (a 1000-page KB at 768d × float JSON ≈ 12MB vs ~48MB at 3072d).
# Matryoshka training keeps quality high under truncation, but the
# vector loses unit-norm so we re-normalize after truncation.
GEMINI_OUTPUT_DIM = 768

# voyage-4-lite's native default is 1024-dim (the Voyage SDK supports
# Matryoshka output_dimension truncation to 256/512/1024 but we don't
# pass that param — we accept the native vector). 1024-dim × float32
# JSON ≈ 22KB per page; a 1500-page KB lands around 32MB on S3. Bump
# the truncation back on if storage / bandwidth becomes a concern.
VOYAGE_OUTPUT_DIM = 1024

_INDEX_VERSION = 1
_INDEX_KEY_SUFFIX = "_embeddings/index.json"

# Max chars sent per embedding call. text-embedding-004 accepts ~2k
# tokens; ~6 chars/token across mixed CJK + English text keeps us
# safely under that with headroom for prompt template overhead.
_MAX_EMBED_CHARS = 8000


@dataclass
class EmbeddingItem:
    """One stored vector + provenance for incremental updates."""

    key: str  # S3 key of the wiki page (full prefix)
    content_hash: str  # sha256[:16] of the body — change → re-embed
    vector: list[float]
    # ETag of the S3 object this vector was built from. When it matches the
    # current listing we skip the download entirely; the refresh then costs
    # one list call instead of one GET per page. Empty for entries written
    # before this field existed — those fall back to downloading and hashing.
    source_etag: str = ""


class EmbeddingClient:
    """Provider-agnostic embedding generator.

    Provider selection priority:
      1. ``embedding_provider`` field on the profile (explicit override)
      2. ``voyage_api_key`` set → Voyage  (recommended — no-train opt-out,
                                            200M free per account, strong CJK)
      3. ``gemini_api_key`` set → Gemini  (free tier trains by default,
                                            paid Vertex is no-train)
      4. ``openai_api_key`` set → OpenAI  (no-train default but no free)
      5. fall back to Ollama (local, requires ``ollama pull bge-m3``)

    Asymmetric models (Voyage, Gemini, OpenAI) take a ``task_type`` hint
    — "RETRIEVAL_DOCUMENT" when embedding a page, "RETRIEVAL_QUERY"
    when embedding a user question. This puts the two vector
    populations into compatible subspaces; skipping it costs ~5-10
    points of nDCG on benchmarks.
    """

    def __init__(self, profile: ProfileConfig):
        self.profile = profile
        explicit = getattr(profile, "embedding_provider", None)
        if explicit:
            self.provider = explicit
        elif getattr(profile, "voyage_api_key", None):
            self.provider = "voyage"
        elif profile.gemini_api_key:
            self.provider = "gemini"
        elif getattr(profile, "openai_api_key", None):
            self.provider = "openai"
        else:
            self.provider = "ollama"
        # Profile override wins over the per-provider default constant.
        # This is the seam new model generations (voyage-5-lite,
        # gemini-embedding-002, ...) plug into without a agent2kb release.
        configured = getattr(profile, "embedding_model", None)
        self.model = configured or self._default_model()

    def _default_model(self) -> str:
        return {
            "voyage": VOYAGE_EMBED_MODEL,
            "gemini": GEMINI_EMBED_MODEL,
            "openai": OPENAI_EMBED_MODEL,
            "ollama": OLLAMA_EMBED_MODEL,
        }[self.provider]

    def embed(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
        """Return an embedding vector for ``text``.

        The text is truncated to ``_MAX_EMBED_CHARS`` to stay under
        provider context limits. For very long pages this means the
        tail is invisible to semantic search — a known trade-off.
        """
        text = text[:_MAX_EMBED_CHARS]
        if self.provider == "voyage":
            return self._embed_voyage(text, task_type)
        if self.provider == "gemini":
            return self._embed_gemini(text, task_type)
        if self.provider == "openai":
            return self._embed_openai(text)
        return self._embed_ollama(text)

    def _embed_voyage(self, text: str, task_type: str) -> list[float]:
        """Voyage embedding via the ``voyageai`` SDK.

        Voyage's input_type values are ``"query"`` and ``"document"`` —
        different naming from Gemini's ``RETRIEVAL_QUERY`` /
        ``RETRIEVAL_DOCUMENT``. Map them here so callers see one API.

        IMPORTANT: Voyage's default data policy is opt-IN to training.
        Toggle the opt-out on your account dashboard BEFORE the first
        API call. The SDK doesn't expose any per-request override —
        the policy is account-wide.
        """
        voyageai = import_optional("voyageai", "voyage", "Voyage embeddings")

        if not hasattr(self, "_voyage_client"):
            self._voyage_client = voyageai.Client(
                api_key=self.profile.voyage_api_key
            )

        # task_type → input_type mapping. Voyage's bi-encoder is
        # asymmetric — using the right side gives a real precision lift.
        input_type = "query" if task_type == "RETRIEVAL_QUERY" else "document"

        result = self._voyage_client.embed(
            [text],
            model=self.model,
            input_type=input_type,
            truncation=True,  # let the server clip to its max context
        )
        # voyage-4-lite returns 1024-dim by default. Pass
        # ``output_dimension=512`` here if you want to halve index
        # storage (Matryoshka-trained, quality holds up).
        return result.embeddings[0]

    def _embed_gemini(self, text: str, task_type: str) -> list[float]:
        # New google.genai SDK (the deprecated google.generativeai package
        # stopped receiving updates 2026-Q1). Cached on self so we don't
        # rebuild the client + auth handshake on every call.
        genai = import_optional("google.genai", "gemini", "Gemini embeddings")
        types = import_optional("google.genai.types", "gemini", "Gemini embeddings")

        if not hasattr(self, "_gemini_client"):
            self._gemini_client = genai.Client(api_key=self.profile.gemini_api_key)

        # gemini-embedding-001 returns 3072-dim vectors by default.
        # That's 12MB+ for a 1000-page KB, which JSON-inflates to ~50MB
        # on S3 and slows search. Truncating to 768 dims (Matryoshka
        # representation, vectors are trained to remain informative
        # under truncation) saves 4x storage with negligible cosine
        # quality loss. Re-normalize after truncation — the unit-norm
        # property is lost when you slice a Matryoshka vector.
        result = self._gemini_client.models.embed_content(
            model=self.model,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=GEMINI_OUTPUT_DIM,
            ),
        )
        return _renormalize(list(result.embeddings[0].values))

    def _embed_openai(self, text: str) -> list[float]:
        # Lazy import so OpenAI dep stays optional.
        OpenAI = import_optional("openai", "openai", "OpenAI embeddings").OpenAI

        client = OpenAI(api_key=self.profile.openai_api_key)
        resp = client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding

    def _embed_ollama(self, text: str) -> list[float]:
        # urllib over requests to avoid adding a runtime dep — the call
        # is one-shot and we don't need session/retry features here.
        import json as _json
        import urllib.request

        host = self.profile.ollama_host or "http://localhost:11434"
        req = urllib.request.Request(
            f"{host}/api/embeddings",
            data=_json.dumps({"model": self.model, "prompt": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = _json.loads(resp.read())
        return payload["embedding"]


class EmbeddingIndex:
    """Persistent embedding store on S3.

    Lifecycle:
      1. Construct → ``load()`` to pull existing index from S3
      2. ``upsert(key, body)`` for each wiki page (skip via ``needs_embed``)
      3. ``save()`` to persist back to S3
      4. ``search(query)`` to retrieve top-k similar pages

    The index is dictionary-backed so callers can do batch upserts
    without intermediate fsync overhead.
    """

    def __init__(
        self,
        profile: ProfileConfig,
        s3: S3Client,
        client: Optional[EmbeddingClient] = None,
    ):
        self.profile = profile
        self.s3 = s3
        self.client = client or EmbeddingClient(profile)
        self.items: dict[str, EmbeddingItem] = {}
        self._loaded = False
        self._dirty = False
        # Lazy-built numpy matrix for fast batched cosine. Reset whenever
        # the index changes (upsert/remove/load); rebuilt on next search.
        self._matrix = None
        self._matrix_keys: list[str] = []
        #: ETag of the index object this instance loaded: None when there was
        #: none (or ``load`` was not called), "" when the server sent no ETag.
        self.loaded_etag: Optional[str] = None
        #: The items as loaded, to tell this writer's changes from another's
        #: when a conditional save has to be re-applied (``_rebase``).
        self._base: dict[str, EmbeddingItem] = {}

    @property
    def index_key(self) -> str:
        return f"{self.profile.prefix}{_INDEX_KEY_SUFFIX}"

    def load(self) -> bool:
        """Load index from S3.

        Returns True if a usable index was loaded, False if no index
        existed or the model changed (caller should trigger full re-embed).
        """
        try:
            content, self.loaded_etag = self.s3.download_with_etag(self.index_key)
            data = json.loads(content)
        except Exception:
            self._loaded = True
            logger.debug("No embedding index on S3 yet")
            return False

        if data.get("model") != self.client.model:
            logger.warning(
                f"Embedding model changed ({data.get('model')} → "
                f"{self.client.model}); discarding stale index."
            )
            self._loaded = True
            return False

        for raw in data.get("items", []):
            self.items[raw["key"]] = EmbeddingItem(
                key=raw["key"],
                content_hash=raw["hash"],
                vector=raw["vec"],
                source_etag=raw.get("etag", ""),
            )
        self._base = dict(self.items)
        self._loaded = True
        logger.info(
            f"Loaded embedding index: {len(self.items)} pages "
            f"(model: {self.client.model})"
        )
        return True

    #: Re-applications of this writer's changes when a save loses a race.
    SAVE_ATTEMPTS = 5

    def save(self) -> None:
        """Persist index to S3 if anything changed since last save.

        After ``load()`` the write is conditional on the ETag loaded (or on
        the index still not existing). If another process (a second compile,
        ``a2kb embed``) saved in between, its index is re-read and this
        writer's own upserts and removals are applied on top of it, so
        neither refresh is lost.
        """
        if not self._dirty:
            return
        from agent2kb.s3_client import PreconditionFailed

        for attempt in range(self.SAVE_ATTEMPTS):
            data = {
                "version": _INDEX_VERSION,
                "model": self.client.model,
                "provider": self.client.provider,
                "items": [
                    {
                        "key": it.key,
                        "hash": it.content_hash,
                        "vec": it.vector,
                        "etag": it.source_etag,
                    }
                    for it in self.items.values()
                ],
            }
            conditional = {}
            if self._loaded:
                conditional = {"if_match": self.loaded_etag or None,
                               "if_none_match": self.loaded_etag is None}
            try:
                # Compact JSON to keep the blob small (no whitespace).
                new_etag = self.s3.upload(
                    self.index_key, json.dumps(data, separators=(",", ":")), **conditional
                )
                break
            except PreconditionFailed:
                if attempt == self.SAVE_ATTEMPTS - 1:
                    raise
                logger.info("Embedding index changed by another writer; merging")
                self._rebase()
        self.loaded_etag = new_etag if isinstance(new_etag, str) else ""
        self._base = dict(self.items)
        self._dirty = False
        logger.info(
            f"Saved embedding index: {len(self.items)} pages → {self.index_key}"
        )

    def _rebase(self) -> None:
        """Re-load the stored index and re-apply this instance's changes to it."""
        latest = EmbeddingIndex(profile=self.profile, s3=self.s3, client=self.client)
        latest.load()
        changed = {k: v for k, v in self.items.items() if self._base.get(k) is not v}
        removed = set(self._base) - set(self.items)
        merged = dict(latest.items)
        for key in removed:
            merged.pop(key, None)
        merged.update(changed)
        self.items = merged
        self._base = dict(latest.items)
        self.loaded_etag = latest.loaded_etag
        self._matrix = None

    def needs_embed(self, key: str, body: str) -> bool:
        """Return True if ``key`` is missing from the index or its
        content hash differs from the stored vector's hash."""
        h = _hash(body)
        existing = self.items.get(key)
        return existing is None or existing.content_hash != h

    def is_current(self, key: str, etag: str) -> bool:
        """True when the stored vector was built from this exact object.

        Lets the caller skip the download for unchanged pages. Entries
        predating ``source_etag`` return False so they are verified by hash
        once and gain an ETag on the next write.
        """
        existing = self.items.get(key)
        return bool(etag) and existing is not None and existing.source_etag == etag

    def upsert(self, key: str, body: str, source_etag: str = "") -> None:
        """Embed ``body`` and store the vector under ``key``.

        Caller is expected to batch many upserts then call ``save()``
        once. Each upsert does one provider API call.
        """
        h = _hash(body)
        vec = self.client.embed(body, task_type="RETRIEVAL_DOCUMENT")
        self.items[key] = EmbeddingItem(
            key=key, content_hash=h, vector=vec, source_etag=source_etag
        )
        self._dirty = True

    def record_etag(self, key: str, etag: str) -> None:
        """Attach an ETag to an existing vector without re-embedding.

        Used when a page's content hash proves unchanged but the stored
        entry has no ETag yet, so the next refresh can skip its download.
        """
        existing = self.items.get(key)
        if existing is not None and etag and existing.source_etag != etag:
            self.items[key] = replace(existing, source_etag=etag)
            self._dirty = True

    def remove(self, key: str) -> bool:
        """Drop a key from the index. Returns True if it existed."""
        if key in self.items:
            del self.items[key]
            self._dirty = True
            return True
        return False

    def prune_missing(self, valid_keys: set[str]) -> int:
        """Drop index entries whose underlying wiki page no longer exists.

        Run after a compile pass so deleted pages don't haunt query results
        with stale embeddings. Returns the number of entries pruned.
        """
        stale = [k for k in self.items if k not in valid_keys]
        for k in stale:
            del self.items[k]
        if stale:
            self._dirty = True
            logger.info(f"Pruned {len(stale)} stale embedding(s)")
        return len(stale)

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """Cosine-similarity search.

        Returns ``[(s3_key, similarity)]`` sorted by similarity desc.
        Empty list when no index is loaded — caller should fall back to
        keyword-only retrieval.

        Uses numpy for the dot-product when available (~200x faster than
        the Python loop on a 1000-page × 768-dim KB: ~10ms vs ~2s).
        Falls back to pure Python cosine when numpy import fails so the
        engine still works on numpy-less environments (Lambda, edge).
        Vectors are unit-normalized at save time so cosine reduces to a
        single matrix-vector multiply.
        """
        if not self.items:
            return []
        qv = self.client.embed(query, task_type="RETRIEVAL_QUERY")
        try:
            import numpy as np

            # Cache the matrix on the index — building it is O(N * D)
            # and dominates query latency if rebuilt each call. Cache is
            # invalidated whenever ``upsert``/``remove`` flips _dirty.
            if self._matrix is None or self._dirty:
                self.build_matrix()
            qv_arr = np.array(qv, dtype=np.float32)
            # Vectors are unit-normalized → cosine = dot product, no
            # division required.
            sims = self._matrix @ qv_arr
            top_idx = np.argsort(-sims)[:top_k]
            return [(self._matrix_keys[i], float(sims[i])) for i in top_idx]
        except ImportError:
            scores = [(it.key, _cosine(qv, it.vector)) for it in self.items.values()]
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]


    def build_matrix(self) -> None:
        """Build the numpy matrix ``search`` uses (raises ImportError without numpy)."""
        import numpy as np

        keys = list(self.items.keys())
        matrix = np.array([self.items[k].vector for k in keys], dtype=np.float32)
        # Keys first, then the matrix: a reader that sees the new matrix also
        # sees its keys (a shared, already-built index is never rebuilt).
        self._matrix_keys = keys
        self._matrix = matrix


class SharedEmbeddingIndex:
    """One KB's loaded embedding index, kept across requests of a long-lived
    process (the MCP server) instead of reloaded and rebuilt per search.

    Each ``get()`` costs one HEAD: when the index object's ETag differs from
    the one loaded (a compile or ``a2kb embed`` saved a new index) it is
    reloaded, so results never lag a refresh. Bounded: it holds a single
    index, and the old one is dropped on reload. The returned index is
    shared between threads and must be treated as read-only.
    """

    def __init__(self, profile: ProfileConfig, s3: S3Client):
        self.profile = profile
        self.s3 = s3
        self._lock = threading.Lock()
        self._index: Optional[EmbeddingIndex] = None
        self._etag: Optional[str] = None
        self._loaded = False

    def get(self) -> Optional[EmbeddingIndex]:
        """The current index, or ``None`` when there is none (keyword-only search)."""
        key = f"{self.profile.prefix}{_INDEX_KEY_SUFFIX}"
        try:
            etag = self.s3.head_etag(key)
        except Exception as e:
            logger.debug(f"Embedding index HEAD failed, using the cached copy: {e}")
            with self._lock:
                if self._loaded:
                    return self._index
            etag = None
        with self._lock:
            if self._loaded and etag == self._etag:
                return self._index
            index = EmbeddingIndex(profile=self.profile, s3=self.s3)
            usable = index.load() and bool(index.items)
            if usable:
                try:
                    index.build_matrix()
                except ImportError:
                    pass  # pure-Python cosine path; nothing to prebuild
            self._index = index if usable else None
            self._etag = index.loaded_etag
            self._loaded = True
            return self._index


def _renormalize(vec: list[float]) -> list[float]:
    """Project a vector back onto the unit sphere.

    Required after truncating a Matryoshka-trained embedding (e.g.
    Gemini's gemini-embedding-001 with output_dimensionality < 3072).
    The truncated vector loses its unit norm, which messes up cosine
    sim if mixed with normalized vectors.
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _hash(body: str) -> str:
    """Short content hash for incremental embed gating."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# Default reranker. Voyage's ``rerank-2.5-lite`` is $0.02/M tokens with
# 200M free tokens per account — a separate quota from embeddings.
# ``rerank-2.5`` (full) is $0.05/M with marginally better precision on
# adversarial cases; flip if the lite model's results disappoint.
VOYAGE_RERANK_MODEL = "rerank-2.5-lite"

# Cap input documents per rerank call. Voyage's API accepts up to a few
# hundred; we cap at 30 because (a) latency scales linearly with the
# document count and (b) embedding top-30 is already a tight filter so
# the marginal precision from reranking #31+ is tiny.
_RERANK_MAX_DOCS = 30


class Reranker:
    """Voyage cross-encoder reranker.

    Sits between hybrid retrieval (embedding cosine + keyword IDF) and
    LLM synthesis. Cross-encoders read the (query, doc) pair together
    so they catch nuance that independent bi-encoder embeddings miss —
    typical precision@10 lift is 10-20%, sometimes 30%+ on ambiguous
    queries. The trade-off is ~50ms latency per pair and one extra API
    round trip.

    Safe to use without checking availability: ``Reranker.rerank()``
    falls back to the input order on any error, so callers can call it
    unconditionally and degrade gracefully.
    """

    def __init__(
        self,
        profile: ProfileConfig,
        model: Optional[str] = None,
    ):
        self.profile = profile
        # Resolution: explicit constructor arg → profile.rerank_model →
        # default constant. Constructor arg exists for tests / one-off
        # overrides; production callers should set the profile field so
        # the choice travels with the profile config.
        self.model = (
            model
            or getattr(profile, "rerank_model", None)
            or VOYAGE_RERANK_MODEL
        )
        self._client = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.profile, "voyage_api_key", None))

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Score ``documents`` against ``query``; return top-k as
        ``(original_index, relevance_score)`` pairs newest-first.

        Returns the trivial passthrough ordering ``(0, 1.0), (1, 1.0), ...``
        — capped at ``top_k`` — when the reranker is unavailable or the
        SDK call fails. This keeps caller code simple: no retry loops,
        no fallback branches.
        """
        if not self.available or not documents:
            return [(i, 1.0) for i in range(min(top_k, len(documents)))]

        # Cap the doc count both for cost and latency.
        capped = documents[:_RERANK_MAX_DOCS]

        try:
            voyageai = import_optional("voyageai", "voyage", "Voyage rerank")
        except MissingExtraError as err:
            logger.warning(f"{err} — skipping rerank")
            return [(i, 1.0) for i in range(min(top_k, len(documents)))]

        if self._client is None:
            self._client = voyageai.Client(api_key=self.profile.voyage_api_key)

        try:
            # NOTE: voyageai SDK 0.3.x does NOT accept ``return_documents``
            # (a parameter on Voyage's HTTP API but not exposed by the
            # Python client). We don't need the echoed documents anyway
            # — caller already holds them in ``documents`` and we look
            # them up by ``r.index``. Earlier 0.13.0 builds passed this
            # kwarg and the SDK rejected the call with a TypeError,
            # causing every rerank to silently fall through to the
            # passthrough ordering (dashboard showed 0 rerank tokens
            # because no call ever reached Voyage). Don't add it back.
            result = self._client.rerank(
                query=query,
                documents=capped,
                model=self.model,
                top_k=min(top_k, len(capped)),
            )
        except Exception as e:
            logger.warning(f"Voyage rerank failed, falling back to input order: {e}")
            return [(i, 1.0) for i in range(min(top_k, len(documents)))]

        # Voyage returns objects with .index and .relevance_score.
        return [(r.index, float(r.relevance_score)) for r in result.results]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0 on bad input."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
