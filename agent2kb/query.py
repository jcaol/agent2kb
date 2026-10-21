"""Query engine: IDF-ranked search + agentic retrieval + sourced answers.

v2 redesign (vs the original keyword + top-5 LLM rerank):

1. **IDF weighting** — rare specific terms (e.g. "redis") now outrank
   common generic terms (e.g. "agent"), so a query about "Redis Cluster"
   surfaces the 9 articles actually about Redis instead of 30 generic
   `concepts/agent*.md` pages that happen to match "agent".

2. **Agentic two-stage retrieval** — after the first hit batch, the
   engine follows `[[wikilinks]]` mentioned in those hits to pull
   related concept pages and source articles into the context window.
   This is the "consolidated" answer pattern: instead of synthesizing
   from 5 isolated snippets, the LLM sees the full local neighbourhood.

3. **raw/ fallback** — wiki pages are LLM-compiled summaries that
   often drop install commands, GitHub URLs, and code blocks. When a
   hit's wiki body is a thin stub, the engine pulls the matching raw/
   file so the original detail reaches the LLM.

4. **Exhaustive intent detection** — queries like "list ALL" or "every"
   (in any supported language) skip the LLM rerank entirely and return a deterministic
   filename + frontmatter listing. Avoids hallucinations like "only 1
   article mentions redis" when 100 actually do.

5. **Sourced answers** — every answer ends with a `## Sources` section
   listing each cited page's KB wikilink + `original_link` URL, so the
   user can always click through to the source instead of trusting the
   LLM's compressed summary.
"""

from __future__ import annotations

import datetime
import math
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from agent2kb.compile.compiler import Compiler
from agent2kb.lang_data import ZH_CROSS_LANG_DICT
from agent2kb.language import (
    exhaustive_stop_words,
    exhaustive_triggers,
    keep_expansion_term,
    language_instruction,
    query_expansion_prompt,
    slug_shape_hint,
)
from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import PreconditionFailed, S3Client
from agent2kb.utils import ensure_traditional_chinese, extract_wikilinks, slugify


@dataclass
class SearchHit:
    """A search result with score and full context.

    Carries the wiki body and (optionally) the raw source body so downstream
    stages — agentic expansion, raw/ fallback, sources rendering — don't
    need to re-fetch from S3. Frontmatter is kept around so we can surface
    `original_link` in the Sources section.
    """

    key: str
    title: str
    score: float
    snippet: str
    frontmatter: Frontmatter | None = None
    body: str = ""
    raw_body: str = ""  # Populated by raw/ fallback when wiki body is thin
    citation_id: int = 0  # 1-indexed citation marker used in answer + sources


# Threshold below which a wiki page is considered "thin" (a compiled stub
# that likely lost install commands, code blocks, etc.) and the engine
# should fall back to the original raw/ source. Tuned empirically against
# concept pages, which run ~600-1500 chars; article wiki pages are usually
# 2000+ chars when they retain useful detail.
_WIKI_THIN_THRESHOLD = 1500

# Number of related wiki pages to pull in during agentic expansion.
# Each adds one S3 fetch and ~500 tokens of context. 5 keeps the total
# additional context under ~2.5k tokens — safe for any provider.
_AGENTIC_EXPAND_LIMIT = 5


class QueryEngine:
    """Search wiki pages and answer questions using LLM reranking."""

    # Expansion modes (see ``_expand_cross_language``):
    #  - "auto"  : dict-first; LLM only if dict yields nothing AND query
    #              is long/ambiguous enough to justify the call. Default.
    #  - "dict"  : dict only. Never call the LLM. Fastest.
    #  - "llm"   : always call LLM (legacy behaviour). Highest coverage
    #              for rare cross-language terms, at one extra LLM call
    #              per query.
    #  - "off"   : skip cross-language expansion entirely.
    EXPAND_MODES = ("auto", "dict", "llm", "off")

    def __init__(
        self,
        profile: ProfileConfig,
        s3: S3Client,
        *,
        expand_mode: str = "auto",
        embeddings=None,
    ):
        self.profile = profile
        self.s3 = s3
        #: Optional ``SharedEmbeddingIndex`` (long-lived processes such as the
        #: MCP server): reuse its loaded index instead of loading one per engine.
        self._shared_embeddings = embeddings
        if expand_mode not in self.EXPAND_MODES:
            raise ValueError(
                f"expand_mode must be one of {self.EXPAND_MODES}, got {expand_mode!r}"
            )
        self.expand_mode = expand_mode
        # Lazy embedding index — loaded on first semantic search call so
        # exhaustive listings and pure keyword queries don't pay the
        # one-shot ~3MB index-fetch cost.
        self._embed_index = None
        self._embed_loaded = False
        # Hits from the most recent query() call. Exposed so callers
        # like the --pin CLI flag can persist the source list to the
        # generated topic page without re-running retrieval.
        self.last_hits: list[SearchHit] = []
        # Per-instance memoization of cross-language expansion. Same
        # normalized question → same expanded terms, no repeat LLM
        # round-trip. Survives only for the lifetime of the engine
        # (one CLI invocation), which is the right scope: project KBs
        # do many similar queries in one session, but cache invalidation
        # across runs would surprise users editing the dict.
        self._expand_cache: dict[str, list[str]] = {}
        # Lazy reranker — built on first ``_maybe_rerank`` call only when
        # rerank is actually wired (profile.rerank_enabled + voyage key).
        self._reranker = None

    def query(self, question: str, top_k: int = 8) -> str:
        """Search wiki and generate an answer.

        Pipeline:
          1. Intent detection — exhaustive queries skip the LLM entirely.
          2. IDF-ranked search — rare terms outweigh common terms.
          3. Agentic expansion — follow [[wikilinks]] in top hits.
          4. raw/ fallback — pull original source when wiki page is thin.
          5. LLM synthesis — answer with numbered citations.
          6. Sources section — append wikilinks + original_link URLs.

        ``top_k`` governs the *initial* hit count; agentic expansion can
        add up to ``_AGENTIC_EXPAND_LIMIT`` more pages. Default raised
        from 5→8 because the old default starved breadth queries (e.g.
        "Redis Cluster setup") of context — 5 hits often gave 4 generic
        concept pages and 1 stub.
        """
        # Reset hit history so a follow-up query.last_hits never reads
        # stale data from an earlier call on the same engine instance.
        self.last_hits = []

        # Stage 0: Intent detection — bypass LLM for exhaustive queries.
        # These are notoriously bad with LLM rerank: top-k always lies
        # about coverage ("only 1 article mentions redis" when 100 do).
        if self._is_exhaustive_intent(question):
            logger.info("Detected exhaustive intent → deterministic listing")
            return self._exhaustive_listing(question)

        # Stage 1: IDF-ranked retrieval.
        hits = self._search(question, top_k=top_k)
        if not hits:
            return "No relevant results found in the knowledge base."

        # Stage 2: Agentic expansion — follow wikilinks from top hits.
        # This is what makes answers "consolidated": instead of 8 isolated
        # snippets, the LLM sees the full local neighbourhood (related
        # concepts, source articles cited by the top hits, etc.).
        expanded = self._agentic_expand(hits, original_query=question)

        # Dedup by s3 key, preserving first occurrence (highest-ranked
        # entry wins). Without this, agentic expansion that follows a
        # wikilink back to an already-retrieved page produces a
        # duplicate SearchHit — same key, same body, but TWO citation
        # IDs in the LLM prompt and TWO entries in the rendered Sources
        # section (observed 2026-05-15 with ``[3]`` and ``[4]`` pointing
        # at the same Akshay article). Dedup happens here so both the
        # LLM context AND the user-visible Sources stay clean; the
        # ``hit_set`` signature used by save_output was already deduped
        # via ``set()``, which is why hit_set showed 11 entries while
        # Sources listed 13.
        all_hits: list[SearchHit] = []
        _seen_keys: set[str] = set()
        for h in hits + expanded:
            if h.key in _seen_keys:
                continue
            _seen_keys.add(h.key)
            all_hits.append(h)

        # Stage 3: raw/ fallback — for thin wiki pages, pull original.
        # Wiki pages are LLM-compiled summaries that often drop install
        # commands, code blocks, and GitHub URLs. raw/ has the original.
        for h in all_hits:
            if len(h.body) < _WIKI_THIN_THRESHOLD:
                h.raw_body = self._raw_fallback(h.key)

        # Assign 1-indexed citation IDs for use in LLM prompt and sources.
        for i, h in enumerate(all_hits, 1):
            h.citation_id = i

        # Stage 4: LLM synthesis with numbered citations.
        context = self._build_context(all_hits)
        # Slug allowlist injected into the prompt teaches the LLM the
        # bilingual ``english-<local name>`` slug convention by example, cutting
        # the rate of hallucinated ``[[Display Name]]`` links the
        # compile pass would later have to discard. We intentionally do
        # NOT run a second cleanup pass in ``save_output`` (see its
        # docstring) — terminal answer == saved output is a hard
        # invariant for the compounding loop.
        slug_pool = self._build_slug_pool(all_hits, question)
        try:
            answer = self._llm_answer(question, context, slug_pool)
            answer = ensure_traditional_chinese(answer, self.profile.output_language)
        except Exception as e:
            logger.warning(f"LLM answer failed: {e}, returning raw results")
            return self._format_raw_results(all_hits)

        # Stash hits before returning so --pin / other callers can
        # capture the exact source set this answer was built from.
        self.last_hits = list(all_hits)

        # Stage 5: Sources section — always appended, never trimmed.
        # User can always click through to the original even when the
        # LLM answer is incomplete or wrong.
        return answer + "\n\n" + self._format_sources(all_hits)

    def search(self, question: str, top_k: int = 10) -> list[SearchHit]:
        """Retrieval only: ranked hits, no answer generation.

        Build the engine with ``expand_mode="dict"`` or ``"off"`` to
        guarantee no chat-LLM call; hybrid retrieval may still use the
        profile's embedding / rerank providers when those are configured.
        """
        return self._search(question, top_k=top_k)

    def _search(self, question: str, top_k: int = 10) -> list[SearchHit]:
        """Index-first search with IDF weighting + cross-language expansion.

        Karpathy pattern: the LLM reads index first to find relevant pages,
        then drills into them. Cross-language: English queries also match
        content in the KB's language and vice versa.

        IDF weighting matters here. Without it, "Redis Cluster" retrieves
        30+ generic `concepts/agent*.md` pages (each matching "agent"
        once) instead of the 9 articles actually about Redis (each
        matching the rare term "redis" once). With IDF, "redis" gets
        a multiplier ~10x larger than "agent" because it appears in far
        fewer index lines, and Redis-specific articles dominate.
        """
        query_terms = _tokenize(question)
        if not query_terms:
            return []

        query_lower = [t.lower() for t in query_terms]

        # Cross-language expansion: add translations
        expanded = self._expand_cross_language(question, query_lower)
        if expanded:
            query_lower = list(dict.fromkeys(query_lower + expanded))
            logger.debug(f"Cross-lang expanded: +{len(expanded)} terms")

        # Compute IDF table once per query (cheap: 1 index.md fetch).
        # Returned by _index_prefilter via cached attribute to avoid
        # double-loading the index.
        candidate_keys = list(dict.fromkeys(self._index_prefilter(query_lower)))
        idf = self._idf_cache  # set by _index_prefilter as side effect

        # Hybrid: union keyword prefilter with semantic top-N. Embeddings
        # catch synonyms ("自我學習" ↔ "self-learning") and conceptual
        # relationships that keyword scoring misses. Semantic scores are
        # mapped to the IDF score range so they sort comparably.
        sem_scores = self._semantic_candidates(question, top_n=30)
        for key in sem_scores:
            if key not in candidate_keys:
                candidate_keys.append(key)

        # Score candidate pages with IDF-weighted term frequency, plus a
        # semantic boost when an embedding match exists.
        hits = []
        for key, content in _iter_bodies(self.s3, candidate_keys):
            try:
                fm, body = Frontmatter.from_yaml_block(content)
                kw_score = _score_document(query_lower, body, fm, idf)
                # Map cosine sim ∈ [-1, 1] → boost ∈ [0, 30]. Cap at 30
                # so semantic alone can't dominate when keywords clearly
                # disagree (e.g. ambiguous query, off-topic semantic
                # neighbour). 30 is roughly the IDF score of one strong
                # title-level keyword match — a sensible "tie boost".
                sem_boost = max(0.0, sem_scores.get(key, 0.0)) * 30.0
                score = kw_score + sem_boost
                if score > 0:
                    snippet = _extract_snippet(body, query_lower, max_len=500)
                    hits.append(
                        SearchHit(
                            key=key,
                            title=fm.title or key.split("/")[-1],
                            score=score,
                            snippet=snippet,
                            frontmatter=fm,
                            body=body,
                        )
                    )
            except Exception as e:
                logger.debug(f"Error reading {key}: {e}")
                continue

        hits.sort(key=lambda h: h.score, reverse=True)
        # Voyage cross-encoder rerank (opt-in via profile.rerank_enabled,
        # defaults to on if voyage_api_key is configured). Refines the
        # top-30 hybrid candidates down to top_k by reading the
        # (query, body) pair through a cross-encoder — typical lift is
        # +10-20% precision@10. Falls back to the current ordering on
        # any error so search degrades gracefully.
        return self._maybe_rerank(question, hits, top_k=top_k)

    def _maybe_rerank(
        self,
        question: str,
        hits: list[SearchHit],
        *,
        top_k: int,
    ) -> list[SearchHit]:
        if not hits or len(hits) <= 1:
            return hits[:top_k]
        # Resolve config: explicit field wins; default is "on when voyage
        # key is set" so a user who configured Voyage gets the precision
        # boost without flipping another switch.
        rerank_pref = getattr(self.profile, "rerank_enabled", None)
        if rerank_pref is None:
            rerank_pref = bool(getattr(self.profile, "voyage_api_key", None))
        if not rerank_pref:
            return hits[:top_k]

        if self._reranker is None:
            try:
                from agent2kb.embeddings import Reranker
                self._reranker = Reranker(profile=self.profile)
            except Exception as e:
                logger.debug(f"Reranker construction failed: {e}")
                return hits[:top_k]

        if not self._reranker.available:
            return hits[:top_k]

        # Build the document strings the cross-encoder will read. Title
        # + first ~2k chars of body is a tight summary that fits in the
        # rerank model's context comfortably AND keeps cost predictable
        # — rerank tokens scale with doc length.
        docs = [
            (h.title + "\n\n" + (h.body or h.snippet or ""))[:2000]
            for h in hits
        ]
        scored = self._reranker.rerank(question, docs, top_k=top_k)
        # ``scored`` is [(orig_index, score), ...] in best-first order.
        # Pull the matching SearchHits and overwrite their score with
        # the rerank score so downstream code (agentic expand, sources)
        # sees a sensible ordering.
        out: list[SearchHit] = []
        for idx, score in scored:
            if 0 <= idx < len(hits):
                h = hits[idx]
                h.score = score
                out.append(h)
        return out

    def _semantic_candidates(self, question: str, top_n: int = 30) -> dict[str, float]:
        """Return ``{s3_key: cosine_sim}`` for the top semantic matches.

        Empty dict (and a single warning log) when the embedding index
        isn't available — the rest of the pipeline transparently falls
        back to keyword-only scoring. This keeps query healthy on fresh
        KBs and during embedding-provider outages.
        """
        if self._embed_index is None and not self._embed_loaded:
            try:
                from agent2kb.embeddings import EmbeddingIndex

                if self._shared_embeddings is not None:
                    idx = self._shared_embeddings.get()
                    loaded = idx is not None
                else:
                    idx = EmbeddingIndex(profile=self.profile, s3=self.s3)
                    loaded = idx.load()
                if loaded and idx.items:
                    self._embed_index = idx
                else:
                    logger.info(
                        "No embedding index found — run `a2kb embed` to enable "
                        "semantic search. Continuing with keyword-only retrieval."
                    )
            except Exception as e:
                logger.warning(f"Embedding index unavailable: {e}")
            self._embed_loaded = True

        if self._embed_index is None:
            return {}
        try:
            results = self._embed_index.search(question, top_k=top_n)
            return dict(results)
        except Exception as e:
            logger.warning(f"Semantic search failed, keyword-only: {e}")
            return {}

    def _index_prefilter(self, query_lower: list[str]) -> list[str]:
        """IDF-aware prefilter. Returns up to 40 candidates.

        Side effect: stores the IDF table in ``self._idf_cache`` so the
        caller (``_search``) can reuse it for full document scoring.
        Index lines double as the "document corpus" for IDF — they're
        cheap to scan (one S3 fetch we already do) and a faithful proxy
        for which terms are common vs rare in the KB.

        Candidate cap raised from 20→40. The old cap was a major silent
        cause of recall loss: when 30+ pages tied at low match-scores
        (e.g. matched only "agent"), the cutoff was effectively random
        and Redis-specific articles often fell off. With IDF scoring
        in place, the bottom of the candidate list is genuinely lower
        quality, so a wider gate is safe.
        """
        index_key = f"{self.profile.prefix}wiki/index.md"
        try:
            index_content = self.s3.download(index_key)
        except Exception:
            logger.debug("index.md not found, falling back to full scan")
            self._idf_cache = {t: 1.0 for t in query_lower}
            return self.s3.list_wiki_files()

        # Parse index lines first — they're our "document corpus" for IDF.
        # Index lines come in two formats:
        #   Article: "- YYYY-MM-DD [[filename|title]] `tag1` `tag2`"
        #   Concept: "- [[concepts/slug]] — Title (count)"
        # The original prefilter only matched the second form via
        # ``startswith("- [[")``, which silently dropped EVERY article
        # entry from candidates. That was the real reason "Redis Cluster"
        # queries returned only generic concept pages — the article
        # wikis were never even considered. Match both forms now.
        def _is_index_entry(line: str) -> bool:
            return line.startswith("- ") and "[[" in line

        index_lines = [line for line in index_content.split("\n") if _is_index_entry(line)]
        self._idf_cache = self._compute_idf(query_lower, index_lines)

        candidates = []
        prefix = self.profile.prefix
        in_concepts = False
        for line in index_content.split("\n"):
            if line.startswith("## Concepts"):
                in_concepts = True
                continue
            if line.startswith("## ") and not line.startswith("## Concepts"):
                in_concepts = False
                continue
            if not _is_index_entry(line):
                continue
            # IDF-weighted match score: rare terms count more than common.
            line_lower = line.lower()
            match_score = sum(
                self._idf_cache.get(t, 1.0) for t in query_lower if t in line_lower
            )
            if match_score > 0:
                match = re.search(r"\[\[([^\]]+)\]\]", line)
                if match:
                    fn = match.group(1)
                    # Wikilink may include a |display alias — strip it.
                    fn = fn.split("|", 1)[0]
                    # Concept entries: link may already include "concepts/" prefix
                    # (index writer emits [[concepts/slug]]). Avoid double-prefixing.
                    if in_concepts:
                        if fn.startswith("concepts/"):
                            key = f"{prefix}wiki/{fn}.md"
                        else:
                            key = f"{prefix}wiki/concepts/{fn}.md"
                        # Concept boost: weighted by max IDF in query so it
                        # doesn't dominate when query has only common terms.
                        max_idf = max(self._idf_cache.values(), default=1.0)
                        match_score += max_idf * 0.3
                    else:
                        key = f"{prefix}wiki/{fn}.md"
                    candidates.append((key, match_score))

        if not candidates:
            logger.debug("No index matches, falling back to full scan")
            return self.s3.list_wiki_files()

        # index.md can list one page in several sections ("Past N Days" and
        # "Articles"): keep each page once, at its best score.
        best: dict[str, float] = {}
        for key, score in candidates:
            if score > best.get(key, float("-inf")):
                best[key] = score
        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        return [k for k, _ in ranked[:40]]

    def _compute_idf(self, terms: list[str], doc_lines: list[str]) -> dict[str, float]:
        """Compute smoothed inverse document frequency from index lines.

        Uses the standard ln((N+1)/(df+1)) + 1 formula (sklearn's
        TfidfVectorizer default). The +1 smoothing prevents division by
        zero for unseen terms and the trailing +1 keeps IDF positive
        even when a term appears in every doc (df == N).

        For a representative KB (~1000 index lines):
          - "redis" (df ~= 9)  → IDF ~= 5.7
          - "agent"  (df ~= 250) → IDF ~= 2.4
          - "ai"     (df ~= 600) → IDF ~= 1.5

        So a Redis-specific article (matches "redis" once) scores
        ~5.7, while a generic agent concept page (matches "agent" once)
        scores only ~2.4 — the topical article wins, as it should.
        """
        n_docs = max(len(doc_lines), 1)
        idf = {}
        for term in terms:
            df = sum(1 for line in doc_lines if term in line.lower())
            idf[term] = math.log((n_docs + 1) / (df + 1)) + 1.0
        return idf

    def _expand_cross_language(self, question: str, query_lower: list[str]) -> list[str]:
        """Expand query terms across languages (EN↔ZH).

        Dictionary-first (the default ``auto`` mode). The hardcoded
        ``ZH_CROSS_LANG_DICT`` covers the high-frequency terms (LLM, RAG,
        prompt-engineering, ...) that account for the vast majority of
        queries against this KB. LLM expansion is only called when:

          * dict expansion found nothing AND
          * the question is long enough (≥4 tokens or any CJK character)
            to plausibly contain a term we don't know.

        This avoids paying one LLM round-trip per query for hits that the
        dict already handles. Override per-engine with ``expand_mode``
        — see ``EXPAND_MODES`` for full semantics.

        Cached per normalized question (token set, order-independent) so
        repeated queries in the same CLI session don't re-spend on the
        LLM.
        """
        mode = self.expand_mode

        if mode == "off":
            return []

        cache_key = " ".join(sorted(set(query_lower)))
        cached = self._expand_cache.get(cache_key)
        if cached is not None:
            return cached

        def _from_dict() -> list[str]:
            hits = []
            for term in query_lower:
                mapped = ZH_CROSS_LANG_DICT.get(term)
                if mapped:
                    hits.extend(mapped)
            return [
                t.lower()
                for t in hits
                if t and len(t) > 1 and keep_expansion_term(t, self.profile.output_language)
            ]

        def _from_llm() -> list[str]:
            try:
                extra = self._llm_expand(question)
                return [t.lower() for t in extra if t and len(t) > 1]
            except Exception as e:
                logger.debug(f"LLM cross-lang expansion failed: {e}")
                return []

        if mode == "dict":
            result = _from_dict()
        elif mode == "llm":
            result = _from_llm() or _from_dict()
        else:  # auto
            result = _from_dict()
            if not result and _should_try_llm_expand(question, query_lower):
                result = _from_llm()

        self._expand_cache[cache_key] = result
        return result

    def _llm_expand(self, question: str) -> list[str]:
        """Use LLM to generate cross-language search terms."""
        messages = [
            {
                "role": "system",
                "content": (
                    query_expansion_prompt(self.profile.output_language)
                ),
            },
            {"role": "user", "content": question},
        ]
        compiler = Compiler(profile=self.profile, s3=self.s3)
        result = compiler._call_llm_with_fallback(messages)
        terms = [
            line.strip().lower()
            for line in result.strip().split("\n")
            if line.strip() and not line.startswith("#")
        ]
        return terms[:10]

    # ─── Stage 0: Intent detection ─────────────────────────────────

    def _is_exhaustive_intent(self, question: str) -> bool:
        """Detect "list all" / "every" style queries in any table language.

        These are bad fits for LLM rerank: the LLM only sees top-k hits
        and confidently claims that's everything, hallucinating coverage.
        Detection is intentionally permissive — false positives just
        skip LLM synthesis (still useful), false negatives revert to the
        old broken behaviour.
        """
        q = question.lower()
        return any(t in q for t in _EXHAUSTIVE_TRIGGERS)

    def _exhaustive_listing(self, question: str) -> str:
        """Deterministic grep-style listing — no LLM, no hallucination.

        Scans every wiki file for query subject terms, returns a
        structured Markdown report with title, path, date, source URL,
        and a single snippet line per match. The user gets the truth
        even if the LLM layer is broken or missing.

        Two-stage filtering:
          1. Strip *instructional* tokens ("list", "all", "mention",
             "wiki", "article", etc.) — these belong to the query intent
             ("list ALL articles that mention X") not the subject ("X").
          2. Require ALL remaining tokens to appear (AND, not OR).
             OR-matching turns into a near-full-corpus dump because any
             common token catches everything.
        """
        terms = [t.lower() for t in _tokenize(question)]
        if not terms:
            return "Could not extract search terms from question."

        # Strip tokens that are part of the *instruction*, not the subject.
        # Without this, "list all wiki articles that mention redis" would
        # be matched as 6 separate terms — and "wiki" alone matches every
        # page (most pages contain "wiki/" in cross-references).
        terms = [t for t in terms if t not in _EXHAUSTIVE_STOP_WORDS]
        if not terms:
            return (
                "Could not extract a subject from the question. "
                "Try: `a2kb query \"list all articles about <topic>\"`"
            )

        wiki_keys = self.s3.list_keys(sub_prefix="wiki/", suffix=".md")
        matches: list[tuple[str, Frontmatter, str]] = []  # (key, fm, snippet)
        for key, content in _iter_bodies(self.s3, wiki_keys):
            try:
                content_lower = content.lower()
                # AND-match: every subject term must be present. Avoids
                # the OR-explosion where one common term sweeps in 800+
                # pages of unrelated content.
                if not all(t in content_lower for t in terms):
                    continue
                fm, body = Frontmatter.from_yaml_block(content)
                snippet = _extract_snippet(body, terms, max_len=180)
                matches.append((key, fm, snippet))
            except Exception:
                continue

        if not matches:
            return f"No wiki pages found containing all of: {', '.join(terms)}"

        # Sort by date (newest first), fall back to filename.
        matches.sort(key=lambda m: (m[1].date or "", m[0]), reverse=True)

        lines = [
            f"# Exhaustive listing — {len(matches)} wiki page(s) match",
            f"_Query terms: `{', '.join(terms)}`_",
            "",
        ]
        for key, fm, snippet in matches:
            filename = key.split("/")[-1].replace(".md", "")
            title = fm.title or filename
            date = fm.date or "—"
            link = fm.original_link or ""
            lines.append(f"### [[{filename}|{title}]]")
            lines.append(f"- **Date:** {date}")
            lines.append(f"- **Path:** `{key}`")
            if link:
                lines.append(f"- **Source:** [{link}]({link})")
            if snippet:
                # Indent so it renders as a quote in markdown.
                lines.append(f"- **Match:** _{snippet.replace(chr(10), ' ')}_")
            lines.append("")
        return "\n".join(lines)

    # ─── Stage 2: Agentic expansion ────────────────────────────────

    def _agentic_expand(
        self, hits: list[SearchHit], original_query: str
    ) -> list[SearchHit]:
        """Follow [[wikilinks]] in top hits to gather related context.

        This is the "consolidated" answer trick: instead of synthesizing
        from N isolated pages, follow the local citation graph one hop
        out. Two ranking factors decide which links to expand:

          1. Frequency — a wikilink mentioned by multiple top hits is
             likely the topic's "hub" concept (e.g. [[Redis Labs]]
             cited by 4 Redis articles).
          2. Existence — wikilinks pointing at non-existent pages are
             skipped silently. Avoids hallucinated concept fabrication.

        Bounded at ``_AGENTIC_EXPAND_LIMIT`` to keep token cost predictable.
        """
        from collections import Counter

        # Tally wikilinks across all top hits (frequency = importance signal).
        link_counts: Counter[str] = Counter()
        seen_keys = {h.key for h in hits}
        for h in hits:
            text = (h.body or h.snippet or "")
            for link in extract_wikilinks(text):
                # Strip |display alias and concepts/ prefix variations.
                clean = link.split("|", 1)[0].strip()
                # Skip self-references and noisy links.
                if not clean or len(clean) > 80:
                    continue
                link_counts[clean] += 1

        # Resolve each link to a concrete S3 key. Try concept page first
        # (most wikilinks resolve there), then top-level wiki/.
        # Concept files are lowercase by convention (e.g. ``postgresql.md``,
        # ``persistent-memory-持久記憶.md``) — but ``slugify`` preserves the
        # input case. Lowercase the slug here so wikilinks like
        # ``[[PostgreSQL]]`` resolve to ``concepts/postgresql.md``.
        prefix = self.profile.prefix
        candidates: list[tuple[str, int]] = []
        for link, count in link_counts.most_common(_AGENTIC_EXPAND_LIMIT * 4):
            # Wikilinks may carry an explicit prefix (``topics/foo``,
            # ``concepts/bar``). Honour the prefix when present so the
            # right subdirectory is tried first.
            if "/" in link:
                stem = link.split("/", 1)[1]
                subdir = link.split("/", 1)[0]
                slug = slugify(stem).lower()
                key_variants = [
                    f"{prefix}wiki/{subdir}/{slug}.md",
                    f"{prefix}wiki/{subdir}/{stem}.md",
                ]
            else:
                slug = slugify(link).lower()
                key_variants = [
                    f"{prefix}wiki/concepts/{slug}.md",
                    f"{prefix}wiki/topics/{slug}.md",
                    f"{prefix}wiki/{link}.md",  # Article filenames keep original case
                    f"{prefix}wiki/{slug}.md",
                ]
            for key in key_variants:
                if key in seen_keys:
                    break
                if self.s3.exists(key):
                    candidates.append((key, count))
                    seen_keys.add(key)
                    break
            if len(candidates) >= _AGENTIC_EXPAND_LIMIT:
                break

        # Fetch candidate pages and wrap as SearchHits.
        expanded: list[SearchHit] = []
        counts = dict(candidates)
        for key, content in _iter_bodies(self.s3, [k for k, _ in candidates]):
            count = counts[key]
            try:
                fm, body = Frontmatter.from_yaml_block(content)
                expanded.append(
                    SearchHit(
                        key=key,
                        title=fm.title or key.split("/")[-1],
                        score=float(count),  # Citation count as pseudo-score.
                        snippet=body[:500],
                        frontmatter=fm,
                        body=body,
                    )
                )
            except Exception as e:
                logger.debug(f"Agentic expand failed for {key}: {e}")
                continue

        if expanded:
            logger.info(
                f"Agentic expansion: +{len(expanded)} pages from wikilink graph"
            )
        return expanded

    # ─── Stage 3: raw/ fallback ────────────────────────────────────

    def _raw_fallback(self, wiki_key: str) -> str:
        """Pull matching raw/ source when wiki page is too thin.

        Wiki pages are LLM compiled summaries and frequently lose the
        important concrete bits (install commands, code blocks, GitHub
        URLs, exact CLI invocations). raw/ keeps the original verbatim.

        Matching is done by filename stem: a wiki page at
        ``wiki/<stem>.md`` likely has a sibling raw at ``raw/<stem>.md``.
        Concept pages (``wiki/concepts/...``) have no raw counterpart by
        design — their content is synthesized. Topic pages
        (``wiki/topics/...``) are hand-crafted references with no raw
        upstream either. Skip both.
        """
        if "/concepts/" in wiki_key or "/topics/" in wiki_key:
            return ""
        prefix = self.profile.prefix
        if not wiki_key.startswith(f"{prefix}wiki/"):
            return ""
        stem = wiki_key[len(f"{prefix}wiki/"):]  # filename.md
        raw_key = f"{prefix}raw/{stem}"
        try:
            content = self.s3.download(raw_key)
            _fm, body = Frontmatter.from_yaml_block(content)
            # Cap at 4000 chars — raw can be huge (full Medium articles).
            return body[:4000]
        except Exception:
            return ""

    # ─── Stage 4: Context building + LLM synthesis ─────────────────

    def _build_context(self, hits: list[SearchHit]) -> str:
        """Render hits as numbered context blocks for the LLM.

        Format puts the citation marker `[N]` first, then title and path,
        then snippet (or full body when short), then raw_body if present.
        Numbered citations work better than wikilinks in LLM prompts:
        the model rarely fabricates `[3]` when it has only seen [1]–[5],
        whereas it freely invents `[[Plausible Concept Name]]`.
        """
        parts = []
        for h in hits:
            block = [f"### [{h.citation_id}] {h.title}", f"_path: {h.key}_"]
            if h.frontmatter and h.frontmatter.original_link:
                block.append(f"_source: {h.frontmatter.original_link}_")
            block.append("")
            # Use full body when short, snippet otherwise.
            content = h.body if len(h.body) < 2000 else h.snippet
            block.append(content)
            if h.raw_body:
                block.append("\n**Original raw source (excerpt):**\n")
                block.append(h.raw_body)
            parts.append("\n".join(block))
        return "\n\n---\n\n".join(parts)

    def _format_sources(self, hits: list[SearchHit]) -> str:
        """Render the Sources section appended to every answer.

        Always present, even when the LLM answer is empty. Surfaces the
        original_link (the actual GitHub URL, blog post, etc.) so the
        user has an escape hatch when the LLM compression dropped useful
        detail.
        """
        lines = ["## Sources"]
        for h in hits:
            filename = h.key.split("/")[-1].replace(".md", "")
            link_part = ""
            if h.frontmatter and h.frontmatter.original_link:
                link_part = f" — [original]({h.frontmatter.original_link})"
            lines.append(f"- `[{h.citation_id}]` [[{filename}|{h.title}]]{link_part}")
        return "\n".join(lines)

    def _build_slug_pool(
        self, hits: list[SearchHit], question: str, cap: int = 150
    ) -> list[str]:
        """Pick the wiki slugs most likely to appear in the answer.

        Three layers, deduped, priority order F3 > F1 > F2 so the cap
        favours the highest-precision sources first:

          F3 — every hit's own filename stem. The LLM is about to
               summarise these articles, so its wikilinks should point
               back to them by canonical slug.
          F1 — every ``[[wikilink]]`` already inside a hit body, resolved
               to a slug. These are concepts the cited articles chose
               to link — strong query-relevance signal.
          F2 — concept slugs whose stem shares a token (case-insensitive
               whole-word match on hyphen-split) with the question.
               Catches concepts the LLM may naturally mention even
               though no hit linked to them.

        ``cap`` of 150 ≈ 1k tokens, kept well under the LLM's effective
        prompt budget after the context blocks land. Per-call cost is
        one S3 LIST + a pass over hit bodies — same order as Track B's
        post-processor.
        """
        from agent2kb.lint import build_resolution_fast

        resolution, _ = build_resolution_fast(self.s3, include_outputs=False)

        ordered: list[str] = []  # preserves insertion order, dedup separately
        seen: set[str] = set()

        def _add(slug: str) -> None:
            s = slug.lower()
            if s and s not in seen:
                seen.add(s)
                ordered.append(slug)

        # F3: hit slugs (highest priority).
        for h in hits:
            stem = h.key.split("/")[-1].replace(".md", "")
            _add(stem)

        # F1: wikilinks present in hit bodies — resolve each to a slug.
        for h in hits:
            for raw in extract_wikilinks(h.body):
                target = raw.split("|", 1)[0].split("#", 1)[0].strip()
                if not target:
                    continue
                resolved = resolution.get(target.lower())
                if not resolved:
                    # Try slugify form too — bilingual concept links arrive
                    # in display form and need normalization.
                    resolved = resolution.get(slugify(target).lower())
                if resolved:
                    stem = resolved.split("/")[-1].replace(".md", "")
                    _add(stem)
            if len(ordered) >= cap:
                break

        # F2: token-overlap concepts — only fire if there's still room.
        if len(ordered) < cap:
            q_tokens = {
                t.lower() for t in re.split(r"[\s/,.\-_]+", question) if len(t) >= 3
            }
            if q_tokens:
                concept_keys = [
                    k for k in resolution.values() if "/concepts/" in k
                ]
                for k in concept_keys:
                    stem = k.split("/")[-1].replace(".md", "")
                    parts = {p for p in stem.lower().split("-") if p}
                    if parts & q_tokens:
                        _add(stem)
                        if len(ordered) >= cap:
                            break

        return ordered[:cap]

    def _llm_answer(
        self, question: str, context: str, slug_pool: list[str] | None = None
    ) -> str:
        """Synthesize an answer from numbered context blocks.

        Shares the profile's language instruction with the compile pipeline so
        query answers come back in the same language that compiled wiki
        pages use (English unless the KB sets ``output_language``).

        Citation discipline is enforced via system prompt: every claim
        must end with a `[N]` marker matching one of the provided context
        blocks. The LLM is also explicitly told to surface install
        commands, URLs, and code blocks verbatim — exactly the detail
        the old prompt was letting slip through.

        ``slug_pool`` is a query-relevant list of existing wiki slugs
        injected so the LLM (a) prefers existing slugs over hallucinated
        ones and (b) imitates the bilingual ``english-<local name>`` slug shape
        by example. We rely on this prompt-time hardening alone — the
        old post-save cleanup pass was removed because it diverged the
        saved output from what the user actually saw on the terminal.
        """
        lang = self.profile.output_language
        slug_block = ""
        if slug_pool:
            joined = ", ".join(f"`{s}`" for s in slug_pool)
            slug_block = (
                "\n\nKB slug pool (existing wiki page slugs relevant to this "
                "query — prefer these when writing wikilinks; if a concept "
                "you want to link is NOT here, follow the same slug shape "
                f"you see{slug_shape_hint(lang)}):\n"
                f"{joined}"
            )
        system_prompt = (
            "You are a knowledge base assistant. Answer the user's question "
            "STRICTLY from the provided context blocks (numbered [1], [2], ...).\n\n"
            "Rules:\n"
            "- Cite EVERY claim with a `[N]` marker that matches a context block.\n"
            "- Surface concrete details verbatim: install commands, URLs, code "
            "blocks, GitHub repo links, CLI invocations. Do NOT paraphrase them.\n"
            "- When multiple sources cover the same point, consolidate into a "
            "single coherent narrative — don't repeat the same fact 3 times.\n"
            "- If the context lacks the answer, say so EXPLICITLY and point to "
            "which `[N]` source(s) come closest, instead of fabricating.\n"
            "- Use `[[slug|Display Name]]` for KB concepts when a matching slug "
            "exists in the slug pool below — Obsidian needs the canonical slug "
            "to resolve, not the human-readable display name. Keep the [N] "
            "citation marker AFTER the wikilink.\n"
            "- Never write markdown-style file paths like `[projects/.../foo.md]` "
            "— Obsidian renders these as nothing useful.\n"
            "- Do NOT add a Sources section yourself — the engine appends one.\n"
            f"{slug_block}\n\n"
            f"{language_instruction(lang)}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Context blocks:\n\n{context}\n\n"
                    "Provide a structured, well-organized answer. Lead with the "
                    "direct answer, then supporting detail. Use headings only if "
                    "the answer naturally splits into sections."
                ),
            },
        ]

        compiler = Compiler(profile=self.profile, s3=self.s3)
        return compiler._call_llm_with_fallback(messages)

    def _format_raw_results(self, hits: list[SearchHit]) -> str:
        """Format raw search results as markdown."""
        lines = ["## Search Results\n"]
        for i, hit in enumerate(hits, 1):
            lines.append(f"### {i}. {hit.title}")
            lines.append(f"*Score: {hit.score:.2f} | Source: {hit.key}*\n")
            lines.append(hit.snippet)
            lines.append("")
        return "\n".join(lines)

    ACTORS = ("user", "agent")

    # Dedup window for agent-actor saves. If a recent output (within this
    # many seconds, same actor, same hit-set) already exists, we append
    # the follow-up question to it instead of creating a new file. Keeps
    # the chat-driven "what is X / now its features / now its install"
    # breadth-first agent pattern from filling outputs/agent/ with
    # near-duplicate captures.
    _AGENT_DEDUP_WINDOW_SEC = 3600

    def save_output(
        self,
        question: str,
        answer: str,
        *,
        actor: str = "user",
    ) -> str:
        """Save query answer to outputs/ for compounding loop.

        ``actor`` controls **where** the answer lands and whether the
        save coalesces with a recent one:

          * ``user`` (default, human-typed query): writes
            ``outputs/<timestamp>-<slug>-<id>.md``. Always a new file —
            user-typed questions are durable artefacts and should never
            silently merge with another.
          * ``agent`` (auto-enrichment / chat-reply path): writes
            ``outputs/agent/<timestamp>-<slug>-<id>.md``. If a recent
            (≤1h) save in the same folder hit the **same set of source
            pages**, we update that file in place (append the new
            question to ``source_meta.also_asked`` and add a follow-up
            section to the body) rather than creating a duplicate.

        The dedup window targets the typical agent failure mode of
        firing ``what is X`` / ``features of X`` / ``install steps for
        X`` queries minutes apart against the same source articles. We
        intentionally do NOT dedup user saves — humans have intent
        between similar questions, and silent merging would be
        confusing.

        Invariant (unchanged from 0.8.0): the saved body contains the
        terminal answer verbatim. Any earlier link cleanup has already
        run on the string we receive here; we deliberately do NOT run a
        second pass via ``clean_links_in_body``. See the prior history
        of this function for the full rationale.

        Returns the s3 key of the new (or updated) output file.
        """
        if actor not in self.ACTORS:
            raise ValueError(
                f"actor must be one of {self.ACTORS}, got {actor!r}"
            )

        # Agent saves try dedup against recent same-hit-set captures
        # before allocating a new file. Within one process (the MCP server
        # runs tool calls on threads) they are serialised, so an overlapping
        # save merges into the capture the other one just made; across
        # processes the merge relies on a conditional write.
        if actor == "agent":
            with _AGENT_SAVE_LOCK:
                merged_key = self._try_dedup_agent_save(question, answer)
                if merged_key is not None:
                    return merged_key
                return self._write_new_output(question, answer, actor)
        return self._write_new_output(question, answer, actor)

    def _write_new_output(self, question: str, answer: str, actor: str) -> str:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        slug = slugify(question, max_length=60)

        # Directory layout:
        #   actor=user  → outputs/<...>.md      (root; preserves layout
        #                                          for legacy Obsidian
        #                                          sync, where users
        #                                          expect their captures
        #                                          at the top of the
        #                                          outputs folder)
        #   actor=agent → outputs/agent/<...>.md (namespaced so bulk
        #                                          prune / weighting in
        #                                          the compounding loop
        #                                          is just a path check)
        subdir = "" if actor == "user" else "agent/"

        cited = extract_wikilinks(answer)

        from agent2kb.models import SourceKind

        fm = Frontmatter(
            title=question,
            source="kb query",
            date=today,
            tags=["output", "query"],
            source_kind=SourceKind.QUERY_OUTPUT,
            source_meta={
                "actor": actor,
                "hit_set": self._current_hit_set_signature(),
            },
        )
        if cited:
            fm.tags.append("has-citations")

        content = fm.to_yaml_block() + f"\n# Query: {question}\n\n{answer}\n"
        # <timestamp>-<slug>-<random>.md: the timestamp has one-second
        # resolution and the slug is truncated, so two captures of the same
        # (or a similar long) question within a second used to share a key
        # and overwrite each other. The random suffix keeps the old
        # <timestamp>-<slug> prefix; If-None-Match refuses to replace a key
        # that exists anyway.
        for attempt in range(_CREATE_ATTEMPTS):
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            output_key = (
                f"{self.profile.prefix}outputs/{subdir}{timestamp}-{slug}-"
                f"{secrets.token_hex(4)}.md"
            )
            try:
                self.s3.upload(output_key, content, if_none_match=True)
                break
            except PreconditionFailed:
                if _holds(self.s3, output_key, content):
                    break  # our write committed; the SDK's retry got 412
                if attempt == _CREATE_ATTEMPTS - 1:
                    raise
        logger.info(f"Saved query output ({actor}): {output_key}")
        # Best-effort manifest refresh (Item 6). No-op when manifest is
        # absent — operator can ``a2kb manifest --rebuild`` to bootstrap.
        try:
            from agent2kb import manifest as _mf
            _mf.update_entry(self.s3, self.profile, output_key, content)
        except Exception as _e:  # pragma: no cover
            logger.debug(f"manifest update skipped: {_e}")
        return output_key

    # ─── Dedup helpers (P3 from 2026-05-14 review) ──────────────────

    def _current_hit_set_signature(self) -> str:
        """Stable signature of the hits behind the just-finished query.

        Sorted, comma-joined s3 keys of ``self.last_hits``. Order-
        independent: two queries that surface the same source pages
        produce the same signature regardless of rerank order. Empty
        string when there are no hits (the exhaustive-listing path
        leaves ``last_hits`` untouched).
        """
        keys = sorted({h.key for h in self.last_hits if h.key})
        return ",".join(keys)

    def _try_dedup_agent_save(
        self, question: str, answer: str
    ) -> Optional[str]:
        """Attempt to merge this answer into a recent same-hit-set agent save.

        Returns the s3 key of the updated file when a merge happens, or
        ``None`` when the caller should proceed with a fresh save.

        Scope: only agent saves; only within ``_AGENT_DEDUP_WINDOW_SEC``;
        only when the hit-set signature is non-empty AND matches a prior
        capture verbatim. This is intentionally conservative — false
        merges are worse than occasional non-merges.
        """
        signature = self._current_hit_set_signature()
        if not signature:
            return None

        try:
            agent_objects = self.s3.list_objects(
                sub_prefix="outputs/agent/", suffix=".md"
            )
        except Exception as e:
            logger.debug(f"agent dedup: listing failed — {e}")
            return None
        if not agent_objects:
            return None

        now = datetime.datetime.now(tz=datetime.timezone.utc)
        cutoff = now - datetime.timedelta(seconds=self._AGENT_DEDUP_WINDOW_SEC)
        # The listing's LastModified is UTC in this format; no HEAD per key.
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        # Walk newest-first (keys start with their timestamp); we expect dedup
        # to land on the most recent capture and the rate of agent saves is
        # bounded, so this stays cheap in practice.
        for obj in sorted(agent_objects, key=lambda o: o.key, reverse=True):
            if obj.last_modified and obj.last_modified < cutoff_str:
                # We're walking newest-first; once we cross the window
                # everything older also misses.
                break
            outcome = self._merge_follow_up(obj.key, signature, question, answer, now)
            if outcome == "merged":
                return obj.key
            if outcome == "failed":
                return None
        return None

    def _merge_follow_up(
        self,
        key: str,
        signature: str,
        question: str,
        answer: str,
        now: datetime.datetime,
    ) -> str:
        """Append a follow-up to ``key`` if it has this hit-set signature.

        Returns ``"merged"``, ``"skip"`` (different hit set or unreadable) or
        ``"failed"`` (the caller then writes a new capture, so the answer is
        never lost). The read-modify-write is guarded by If-Match on the ETag
        read: a writer that lost the race re-reads the file, which now holds
        the other follow-up, and appends to that.
        """
        stamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        follow_up = f"## Follow-up ({stamp}): {question}\n\n{answer}\n"
        for attempt in range(_MERGE_ATTEMPTS):
            try:
                content, etag = self.s3.download_with_etag(key)
                fm, body = Frontmatter.from_yaml_block(content)
            except Exception:
                return "skip"
            if (fm.source_meta or {}).get("hit_set") != signature:
                return "skip"

            also_asked = list((fm.source_meta or {}).get("also_asked") or [])
            if attempt and follow_up.rstrip() in body:
                # Our previous attempt committed; only its response was lost and the
                # SDK's retry got 412. Appending again would duplicate the follow-up.
                logger.info(f"Agent dedup: merged into {key} (also_asked count: "
                            f"{len(also_asked)})")
                try:
                    from agent2kb import manifest as _mf
                    _mf.update_entry(self.s3, self.profile, key, content)
                except Exception as _e:  # pragma: no cover
                    logger.debug(f"manifest update skipped: {_e}")
                return "merged"
            also_asked.append(question)
            fm.source_meta = {**(fm.source_meta or {}), "also_asked": also_asked}
            merged_body = body.rstrip() + "\n\n---\n\n" + follow_up
            new_content = fm.to_yaml_block() + merged_body
            try:
                self.s3.upload(key, new_content, if_match=etag or None)
            except PreconditionFailed:
                logger.debug(f"agent dedup: {key} changed while merging; re-reading")
                continue
            except Exception as e:
                logger.debug(f"agent dedup: merge upload failed — {e}")
                return "failed"
            logger.info(
                f"Agent dedup: merged into {key} (also_asked count: {len(also_asked)})"
            )
            try:
                from agent2kb import manifest as _mf
                _mf.update_entry(self.s3, self.profile, key, new_content)
            except Exception as _e:  # pragma: no cover
                logger.debug(f"manifest update skipped: {_e}")
            return "merged"
        logger.warning(f"agent dedup: {key} kept changing; saving the answer separately")
        return "failed"


def _holds(s3: S3Client, key: str, content: str) -> bool:
    """True when ``key`` already holds exactly ``content``: after a 412 on an
    If-None-Match create, the write was our own (committed, response lost)."""
    try:
        return s3.download(key) == content
    except Exception:
        return False


#: Serialises agent saves in this process (dedup check + merge or create).
_AGENT_SAVE_LOCK = threading.Lock()
#: Re-reads of a capture whose conditional merge lost a race.
_MERGE_ATTEMPTS = 5
#: Random key suffixes tried before giving up on creating a new capture.
_CREATE_ATTEMPTS = 3


#: Bodies fetched per batch when scanning pages. The fetch inside a batch is
#: parallel (``S3Client.download_many`` caps the worker count); batching keeps
#: a whole-wiki scan from holding every page in memory at once.
_FETCH_BATCH = 64


def _iter_bodies(s3: S3Client, keys: list[str]):
    """Yield ``(key, content)`` in ``keys`` order, skipping unreadable keys.

    Replaces one-at-a-time downloads: remote-object latency no longer adds
    up per candidate before a search can answer.
    """
    for start in range(0, len(keys), _FETCH_BATCH):
        batch = keys[start:start + _FETCH_BATCH]
        bodies = s3.download_many(
            batch, on_error=lambda k, e: logger.debug(f"Error reading {k}: {e}")
        )
        for key in batch:
            if key in bodies:
                yield key, bodies[key]


def _should_try_llm_expand(question: str, query_lower: list[str]) -> bool:
    """Gate for ``auto`` expansion mode: is an LLM round-trip worth it?

    Short, all-Latin queries are usually well-covered by ``ZH_CROSS_LANG_DICT``
    — no need to spend an LLM call on a question we already know we can't
    expand further. Long or CJK-containing questions are the cases where
    the LLM's broader vocabulary actually pays off.
    """
    if any("一" <= ch <= "鿿" for ch in question):
        return True
    return len(query_lower) >= 4


def _tokenize(text: str) -> list[str]:
    """Tokenize text for search. Supports CJK and Latin languages."""
    stopwords = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "but",
        "not",
        "with",
        "from",
        "by",
        "as",
        "it",
        "this",
        "that",
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        # Common CJK particles
        "的",
        "了",
        "是",
        "在",
        "有",
        "和",
        "與",
        "及",
        "或",
        "が",
        "の",
        "を",
        "に",
        "で",
        "は",
        "と",
        "も",
    }
    # Match word chars (includes CJK via Unicode \w)
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    # For CJK: also extract individual characters as tokens for character-level match
    cjk_chars = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]", text)
    tokens = [w for w in words if w not in stopwords and len(w) > 1]
    # Add CJK bigrams for better matching
    for i in range(len(cjk_chars) - 1):
        bigram = cjk_chars[i] + cjk_chars[i + 1]
        tokens.append(bigram)
    return tokens


def _score_document(
    query_terms: list[str],
    body: str,
    fm: Frontmatter,
    idf: dict[str, float] | None = None,
) -> float:
    """Score a document with IDF-weighted term frequency.

    Each term's contribution is multiplied by its IDF, so rare specific
    terms (the kind a user actually wants to filter by) dominate over
    common generic terms.

    Field weights (title > tags > body) are kept from the original
    keyword scorer because they're a sensible prior for relevance:
    a hit's title matching the query is much stronger evidence than
    body co-occurrence. IDF multiplies these field weights uniformly.
    """
    body_lower = body.lower()
    title_lower = fm.title.lower()
    tags_lower = " ".join(fm.tags).lower()
    idf = idf or {}

    score = 0.0
    for term in query_terms:
        weight = idf.get(term, 1.0)
        # Title match (high field weight)
        if term in title_lower:
            score += 5.0 * weight
        # Tag match (medium field weight)
        if term in tags_lower:
            score += 3.0 * weight
        # Body frequency (capped to prevent term-stuffing)
        count = body_lower.count(term)
        if count > 0:
            score += min(count * 0.5, 5.0) * weight

    return score


def _extract_snippet(body: str, query_terms: list[str], max_len: int = 500) -> str:
    """Extract a relevant snippet around the first matching term."""
    body_lower = body.lower()
    best_pos = len(body)

    for term in query_terms:
        pos = body_lower.find(term)
        if 0 <= pos < best_pos:
            best_pos = pos

    if best_pos >= len(body):
        return body[:max_len]

    start = max(0, best_pos - 100)
    end = min(len(body), start + max_len)
    snippet = body[start:end]

    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet = snippet + "..."

    return snippet


# Tokens that belong to the *instruction* of an exhaustive query
# ("list all articles that mention X"), not the *subject* ("X"), in every table
# language. Stripped from the search terms before grep-matching so a query like
# "list all wiki articles that mention redis" filters by "redis"
# alone, not by "wiki" (which appears in every cross-reference).
_EXHAUSTIVE_STOP_WORDS = exhaustive_stop_words()

# "List all" trigger substrings of every table language: people ask in English, or
# in their own language, whatever the KB language is.
_EXHAUSTIVE_TRIGGERS = exhaustive_triggers()
