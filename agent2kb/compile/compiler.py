"""Multi-provider LLM compiler with fallback chain and rate limiting."""

from __future__ import annotations

import datetime
import re
import time
from typing import Callable, Optional

from loguru import logger
from rich.console import Console
from rich.markup import escape

from agent2kb.cli_models import (
    CLI_DEFAULT_MODEL,
    FOREIGN_MODELS,
    LEGACY_CLI_MODELS,
    resolve_cli_model,
)
from agent2kb.cli_runner import (  # noqa: F401 - re-exported for callers
    CliRouter,
    ProviderExhaustedError,
    is_provider_exhausted,
    router_for,
)
from agent2kb.compile.schema import build_compile_prompt
from agent2kb.extras import MissingExtraError, import_optional
from agent2kb.models import (
    CompileResult,
    Frontmatter,
    LLMProvider,
    ProfileConfig,
)
from agent2kb.s3_client import S3Client, quarantine_key_for_raw, wiki_key_for_raw
from agent2kb.utils import (
    content_hash,
    ensure_traditional_chinese,
    extract_wikilinks,
    sanitize_wiki_urls,
)

console = Console()

#: The "no ETag on wiki/log.md" warning is shown once per process.
_log_etag_warned = False

# Fallback chain order
FALLBACK_CHAIN = [
    LLMProvider.GEMINI,
    LLMProvider.GROQ,
    LLMProvider.OPENROUTER,
    LLMProvider.OLLAMA,
]

# Remote API providers (upload data to cloud) — privacy-sensitive
# Gemini free tier DOES use data for training; paid tier does not.
# Gemma4 is local Ollama, not cloud.
CLOUD_PROVIDERS = {LLMProvider.GROQ, LLMProvider.OPENROUTER, LLMProvider.GEMINI}

# Default models per provider
DEFAULT_MODELS = {
    LLMProvider.GROQ: "llama-3.3-70b-versatile",
    LLMProvider.OPENROUTER: "google/gemma-3-27b-it",
    LLMProvider.OLLAMA: "llama3.3:70b",
    LLMProvider.GEMMA4: "gemma4:27b",
    LLMProvider.GEMINI: "gemini-3.1-flash-lite-preview",
    LLMProvider.CLI: CLI_DEFAULT_MODEL,
}


def _split_log_header(content: str) -> tuple[str, str]:
    """Split log.md into header (frontmatter + H1 title) and entries body.

    Returns (header, body) where new entries should go between them.
    Stops at the first ## heading (which is a log entry, not header).
    """
    if not content.strip():
        return "# Activity Log\n", ""

    lines = content.split("\n")
    header_lines = []
    in_frontmatter = False
    body_start = 0

    for i, line in enumerate(lines):
        if line.strip() == "---" and i == 0:
            in_frontmatter = True
            header_lines.append(line)
            continue
        if in_frontmatter:
            header_lines.append(line)
            if line.strip() == "---":
                in_frontmatter = False
            continue
        # After frontmatter: only absorb H1, quotes, blank lines
        # Stop at ## (log entries) or bullet lines
        if line.startswith("## ") or line.startswith("- "):
            body_start = i
            break
        if line.startswith("# ") or line.startswith(">") or not line.strip():
            header_lines.append(line)
            continue
        body_start = i
        break
    else:
        body_start = len(lines)

    header = "\n".join(header_lines).rstrip()
    body = "\n".join(lines[body_start:]).strip()
    return header, body


def _sanitize_tags(tag_block: str) -> list[str]:
    """Extract clean tags from LLM output, filtering out garbage.

    Some models (especially Gemma) dump headings, bullet points, full
    sentences, and table rows into the tags section. This function
    rejects anything that doesn't look like a real tag.
    """
    tags = []
    for line in tag_block.split("\n"):
        t = line.strip().lower().strip("- *`")
        if not t:
            continue
        # Reject: too long (real tags are short)
        if len(t) > 40:
            continue
        # Reject: headings, table rows, bullets with content
        if t.startswith(("#", "|", ">", "```", "*")):
            continue
        # Reject: contains markdown syntax
        if "[[" in t or "]]" in t or "---" in t:
            continue
        # Reject: looks like a sentence (has spaces and is long)
        if " " in t and len(t) > 30:
            continue
        # Reject: contains special chars that aren't tag-like
        if any(c in t for c in (":", "|", "=", "$", "→", "\\", "{")):
            continue
        # Clean: collapse spaces to hyphens
        t = re.sub(r"\s+", "-", t).strip("-")
        if t and len(t) > 1:
            tags.append(t)
    return tags


def _clean_llm_output(content: str) -> str:
    """Strip chain-of-thought and planning artifacts from LLM output.

    Some models (Gemma 4 especially) prefix output with thinking/planning
    blocks like '*check:*', '*section 1:*', bullet-point outlines, etc.
    This strips those artifacts to leave clean wiki content.
    """
    lines = content.split("\n")
    clean_lines = []
    in_thinking = False

    for line in lines:
        stripped = line.strip()
        # Detect thinking/planning patterns
        if stripped.startswith("*   *") and ("*:" in stripped or "*check*" in stripped):
            in_thinking = True
            continue
        if stripped.startswith("*   Source:") or stripped.startswith("*   Target"):
            in_thinking = True
            continue
        if stripped.startswith("*   Key points:") or stripped.startswith("*   Goal:"):
            in_thinking = True
            continue
        if stripped.startswith("*   Requirements:") or stripped.startswith("*   Structure:"):
            in_thinking = True
            continue
        # End of thinking block: real content starts with heading or prose
        is_real_content = stripped.startswith("#") or (
            len(stripped) > 50 and not stripped.startswith("*   *")
        )
        if in_thinking and is_real_content:
            in_thinking = False
        if in_thinking and stripped.startswith("*   "):
            continue
        # Skip isolated planning lines
        if re.match(r"^\*\s+\*\w+:\*", stripped):
            continue
        clean_lines.append(line)

    result = "\n".join(clean_lines).strip()
    # If we stripped too aggressively (>80% removed), return original
    if len(result) < len(content) * 0.2:
        return content.strip()
    return result


def _clean_wikilink_target(raw: str) -> str:
    """Strip ``|alias`` and ``#anchor`` from a wikilink to get the bare target.

    ``extract_wikilinks`` returns the FULL inside of ``[[...]]`` —
    ``"target"`` for plain links, but ``"target|display"`` /
    ``"target#anchor"`` / ``"target#anchor|display"`` for the richer
    Obsidian forms. Concept synthesis needs the bare ``target`` portion;
    feeding the alias-bearing form through slugify glued the alias back
    onto the target (with no separator between ``target`` and ``alias``
    because slugify stripped ``|``), producing doubled slugs like
    ``redis-clusterredis-cluster``. Strip both syntax characters here.
    """
    target = raw.split("|", 1)[0]   # drop alias
    target = target.split("#", 1)[0]  # drop anchor
    return target.strip()


def _normalize_concept_name(name: str) -> str:
    """Compute the canonical key two concept names share if they're aliases.

    Heuristic: drop everything after the first ``/`` (this is the common
    "English / local name" form), strip whitespace, lowercase. So "Redis" and
    "Redis Cluster" still differ (different head terms), but "Persistent
    Memory" and "Persistent Memory / Mémoire persistante" collapse onto the same
    key. This is intentionally conservative — we only collapse pure
    bilingual variants, not noun-phrase variants, to keep recall safe.
    """
    head = name.split("/")[0].strip().lower()
    return re.sub(r"\s+", " ", head)


def _merge_concept_aliases(
    refs: dict[str, list[tuple[str, str]]],
) -> dict[str, list[tuple[str, str]]]:
    """Collapse alias variants into a single canonical concept key.

    Per cluster (= same normalized head), the canonical name is whichever
    variant collected the most refs (ties broken by length: the longer,
    more explicit form wins). All refs from the cluster are merged onto
    the canonical, with snippets de-duplicated by source filename.
    """
    clusters: dict[str, list[str]] = {}
    for name in refs:
        clusters.setdefault(_normalize_concept_name(name), []).append(name)

    merged: dict[str, list[tuple[str, str]]] = {}
    for variants in clusters.values():
        if len(variants) == 1:
            merged[variants[0]] = refs[variants[0]]
            continue
        # Pick canonical: most refs, then longest name.
        canonical = max(variants, key=lambda v: (len(refs[v]), len(v)))
        combined: dict[str, str] = {}  # page_fn → snippet (last wins)
        for v in variants:
            for page_fn, snippet in refs[v]:
                combined[page_fn] = snippet
        merged[canonical] = list(combined.items())
    return merged


def _yaml_meta(content: str) -> dict:
    """The raw YAML frontmatter mapping (keeps fields the Frontmatter model drops)."""
    import yaml as _yaml

    if not content.startswith("---"):
        return {}
    meta = _yaml.safe_load(content.split("---", 2)[1]) or {}
    return meta if isinstance(meta, dict) else {}


def _article_index_meta(key: str, content: str) -> list:
    """``[date, link_name, title, tags_display, primary_tag]`` for index.md.

    Plain strings only: the result is stored in the JSON compile cache.
    """
    link_name = key.split("/")[-1].replace(".md", "")
    page_title = page_tags_display = page_date = primary_tag = ""
    try:
        fm, body = Frontmatter.from_yaml_block(content)
        page_date = str(fm.date or "")
        # Prefer the H1 heading from the compiled body — this is the
        # localized title the LLM crafted for readability. Frontmatter
        # title is the English source headline (useful for provenance
        # but not for the Obsidian index display).
        for line in body.split("\n"):
            if line.strip().startswith("# "):
                page_title = line.strip()[2:].strip()
                break
        if not page_title and fm.title:
            page_title = fm.title
        all_tags = fm.tags or []
        if all_tags:
            page_tags_display = " `" + "` `".join(all_tags[:4]) + "`"
            primary_tag = all_tags[0]
    except Exception:
        pass
    return [page_date, link_name, page_title or link_name, page_tags_display, primary_tag]


def _concept_index_meta(key: str, content: str) -> list:
    """``[references, title, link_name]`` for a concept page."""
    stem = key.split("/")[-1].replace(".md", "")
    page_title, refs = stem, 0
    try:
        # 'references' is not in the Frontmatter model, so read the YAML directly.
        meta = _yaml_meta(content)
        page_title = str(meta.get("title") or page_title)
        refs = int(meta.get("references", 0) or 0)
    except Exception:
        pass
    return [refs, page_title, f"concepts/{stem}"]


def _topic_index_meta(key: str, content: str) -> list:
    """``[promoted_date, title, link_name]`` for a pinned topic page."""
    stem = key.split("/")[-1].replace(".md", "")
    page_title, promoted_date = stem, ""
    try:
        meta = _yaml_meta(content)
        page_title = str(meta.get("title") or page_title)
        promoted_date = str(meta.get("promoted_date") or meta.get("date") or "")
    except Exception:
        pass
    return [promoted_date, page_title, f"topics/{stem}"]


class Compiler:
    """Multi-provider LLM compiler for raw → wiki transformation."""

    def __init__(
        self,
        profile: ProfileConfig,
        s3: S3Client,
        provider: Optional[LLMProvider] = None,
        allow_cloud_fallback: bool = False,
    ):
        self.profile = profile
        self.s3 = s3
        self.provider = provider or profile.compile_provider
        self.allow_cloud_fallback = allow_cloud_fallback
        default = DEFAULT_MODELS.get(self.provider, "")
        model = profile.default_model or default
        # Prevent wrong provider's model names from leaking across providers
        if self.provider is LLMProvider.CLI:
            model = resolve_cli_model(model, profile.cli_command)
        elif self.provider is LLMProvider.GEMINI and model in (FOREIGN_MODELS | LEGACY_CLI_MODELS):
            model = default
        self.model = model

        #: Set once the LLM provider proves hard-down (no credits, no quota,
        #: bad key) so the rest of the run can stop instead of retrying.
        self._provider_down = False

        #: One CLI router per purpose, built lazily. Kept for the whole run
        #: so a primary CLI found dead on one item is skipped for the rest.
        self._cli_routers: dict[str, CliRouter] = {}

        #: Pending-maintenance state (loaded lazily from _ops/maintenance.json),
        #: the wiki listing taken by _select_pending, and the last embedding
        #: refresh's failure count and wiki fingerprint.
        self._maint_state: Optional[dict] = None
        self._wiki_listing: Optional[list] = None
        self._embed_failures = 0
        self._embed_fingerprint: Optional[str] = None
        #: ETag-keyed cache of verified raw/wiki pairs and index metadata
        #: (_ops/compile-cache.json), loaded lazily; see compile/cache.py.
        self._compile_cache = None

        # Show privacy warning for cloud providers
        if self.provider in CLOUD_PROVIDERS:
            logger.warning(
                f"Using cloud provider '{self.provider.value}' — data will be uploaded "
                f"to third-party servers. For sensitive content, use --llm ollama."
            )

    def compile_all(self, new_only: bool = True) -> list[CompileResult]:
        """Compile all raw files, or only new/modified ones.

        Pre-step: auto-imports Web Clipper clippings from Clippings/ to raw/.
        Fast-path: when nothing changed, returns in ~2s with zero LLM tokens.
        "Nothing changed" also covers maintenance: concept synthesis that was
        deferred or failed, and an embedding refresh that failed or is stale
        against the wiki listing, run on a no-op tick (see ``_maintenance_due``).
        """
        # Auto-import Web Clipper clippings before compiling
        self._import_clippings()

        raw_keys = self.s3.list_raw_files()

        if not new_only:
            # Force: compile everything
            output_insights = self._load_output_insights()
            results = self._compile_batch(raw_keys, output_insights)
        else:
            # Cheap check: find which raw files need compilation
            pending = self._select_pending(raw_keys)

            if not pending:
                due = self._maintenance_due()
                if not due:
                    logger.info("Nothing to compile (all raw files up-to-date)")
                    # Still rebuild index — catches ghost file cleanup, manual
                    # S3 edits, and prevents stale index between compile events.
                    self._update_index()
                    return []
                logger.info(
                    "Nothing to compile; running pending maintenance: "
                    + ", ".join(sorted(due))
                )
                if "concepts" in due:
                    self._run_concepts()
                self._update_index()
                if "embeddings" in due:
                    self._run_embeddings()
                self._save_maintenance()
                return []

            # Only load LLM resources when there's actual work
            output_insights = self._load_output_insights()
            results = self._compile_batch(pending, output_insights)

        # Post-compile: handle persistent failures, then log/concepts/index
        if results:
            # Remove raw files that have failed too many times (default 3).
            # Log the failure with source URL once, then stop retrying.
            results = self._handle_compile_failures(results)
            self._append_to_log(results)
            self._run_concepts()
        self._update_index()
        self._run_embeddings()
        self._save_maintenance()

        return results

    # ── Pending maintenance (concepts, embeddings) ──────────────────────
    #
    # Concept synthesis is capped per run and stops when the LLM is down; an
    # embedding refresh can fail on a provider outage, and hand-edited wiki
    # pages need re-embedding although no raw file changed. Their state is kept
    # in <prefix>_ops/maintenance.json so a no-op compile tick can finish the
    # work instead of waiting for the next raw change.

    #: First retry delay for maintenance that failed (as opposed to deferred
    #: by a per-run cap); doubles per consecutive failure up to the cap. Only
    #: no-op ticks wait for it; a run that compiles files always retries.
    MAINTENANCE_RETRY_BASE_SEC = 15 * 60
    MAINTENANCE_RETRY_MAX_SEC = 24 * 3600

    def _maintenance(self) -> dict:
        if self._maint_state is None:
            from agent2kb.ops import read_maintenance_state

            self._maint_state = read_maintenance_state(self.s3, self.profile)
        return self._maint_state

    def _save_maintenance(self) -> None:
        if self._maint_state is not None:
            from agent2kb.ops import write_maintenance_state

            write_maintenance_state(self.s3, self.profile, self._maint_state)

    def _mark_maintenance(self, task: str, outcome: str, **extra) -> None:
        """Record ``outcome`` (``done``, ``deferred`` or ``failed``) for ``task``."""
        state = self._maintenance()
        prev = state.get(task) if isinstance(state.get(task), dict) else {}
        entry: dict = {"pending": outcome != "done", "attempts": 0, "retry_after": 0}
        if outcome == "failed":
            attempts = int(prev.get("attempts") or 0) + 1
            delay = min(
                self.MAINTENANCE_RETRY_BASE_SEC * 2 ** (attempts - 1),
                self.MAINTENANCE_RETRY_MAX_SEC,
            )
            entry.update(attempts=attempts, retry_after=time.time() + delay)
        entry.update(extra)
        state[task] = entry

    def _wiki_fingerprint(self, objects) -> str:
        """Hash of the wiki listing (keys + ETags), minus the pages compile rewrites itself."""
        import hashlib

        own = {f"{self.profile.prefix}wiki/index.md", f"{self.profile.prefix}wiki/log.md"}
        lines = sorted(f"{o.key}\t{o.etag}" for o in objects if o.key not in own)
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def _maintenance_due(self) -> set[str]:
        """Maintenance tasks a no-op compile tick should run now."""
        state = self._maintenance()
        now = time.time()
        due: set[str] = set()

        def ready(entry: dict) -> bool:
            try:
                return now >= float(entry.get("retry_after") or 0)
            except (TypeError, ValueError):
                return True

        concepts = state.get("concepts")
        if isinstance(concepts, dict) and concepts.get("pending") and ready(concepts):
            due.add("concepts")

        embeddings = state.get("embeddings")
        embeddings = embeddings if isinstance(embeddings, dict) else {}
        if embeddings.get("pending"):
            if ready(embeddings):
                due.add("embeddings")
        elif self._wiki_listing is not None and embeddings.get(
            "wiki_fingerprint"
        ) != self._wiki_fingerprint(self._wiki_listing):
            # A wiki page changed without a raw change (hand edit, promote,
            # restore), or no refresh has been recorded yet.
            due.add("embeddings")
        return due

    def _run_concepts(self) -> None:
        # Concept synthesis is one LLM call per concept. Skip it entirely
        # when the provider is already known to be down, rather than
        # queueing hundreds of calls that will each fail.
        if self._provider_down:
            logger.warning("Skipping concept synthesis — LLM provider is unavailable")
            self._mark_maintenance("concepts", "failed")
            return
        try:
            outcome = self._synthesize_concepts()
        except MissingExtraError:
            raise
        except Exception as e:
            logger.warning(f"Concept synthesis failed: {e}")
            outcome = "failed"
        self._mark_maintenance("concepts", outcome or "done")

    def _run_embeddings(self) -> None:
        # Incremental, only re-embeds changed pages. A missing embedding
        # provider (e.g. ollama not installed) must not fail the compile run;
        # semantic search just won't work until fixed, and the refresh is
        # retried on later runs.
        self._embed_failures = 0
        self._embed_fingerprint = None
        try:
            self.update_embeddings()
        except Exception as e:
            logger.warning(f"Embedding refresh failed (semantic search disabled): {e}")
            self._mark_maintenance("embeddings", "failed")
            return
        if self._embed_failures:
            logger.warning(
                f"Embedding refresh: {self._embed_failures} page(s) failed; "
                "they are retried on a later compile"
            )
        self._mark_maintenance(
            "embeddings",
            "failed" if self._embed_failures else "done",
            wiki_fingerprint=self._embed_fingerprint,
        )

    def update_embeddings(self, force: bool = False) -> tuple[int, int]:
        """Refresh the embedding index for the active profile.

        Returns ``(embedded, pruned)``. Incremental by default — only
        pages whose body hash changed get re-embedded. ``force=True``
        re-embeds everything (use after switching providers if you want
        to verify regression rather than relying on the model-mismatch
        auto-discard).
        """
        from agent2kb.embeddings import EmbeddingIndex

        index = EmbeddingIndex(profile=self.profile, s3=self.s3)
        index.load()

        # One listing carries every page's ETag, so pages whose stored vector
        # already matches the live object never get downloaded. On a 2,300
        # page wiki that is the difference between ~171s of serial GETs and
        # under a second, and a typical refresh has 0-2 actually-changed
        # pages. `force` still visits everything.
        wiki_objects = self.s3.list_objects(sub_prefix="wiki/", suffix=".md")
        wiki_keys = [o.key for o in wiki_objects]
        etags = {o.key: o.etag for o in wiki_objects}
        self._embed_fingerprint = self._wiki_fingerprint(wiki_objects)
        self._embed_failures = 0

        if force:
            candidates = wiki_keys
        else:
            candidates = [o.key for o in wiki_objects if not index.is_current(o.key, o.etag)]
            skipped = len(wiki_objects) - len(candidates)
            if skipped:
                logger.debug(f"Embedding refresh: {skipped} pages unchanged by ETag")

        # The survivors still need their bodies, but only the survivors, and
        # they are fetched in parallel rather than one at a time.
        bodies = self.s3.download_many(candidates)

        embedded = 0
        for key in candidates:
            content = bodies.get(key)
            if content is None:
                continue
            try:
                fm, body = Frontmatter.from_yaml_block(content)
                # Skip near-empty pages — embedding noise, no signal.
                if len(body.strip()) < 100:
                    continue
                # Embed body + title to bias toward topical match.
                payload = f"{fm.title}\n\n{body}" if fm.title else body
                if force or index.needs_embed(key, payload):
                    index.upsert(key, payload, source_etag=etags.get(key, ""))
                    embedded += 1
                    if embedded % 20 == 0:
                        logger.info(f"  embedded {embedded} pages...")
                else:
                    # Content is unchanged but the entry predates ETag
                    # tracking; stamp it so the next run skips the download.
                    index.record_etag(key, etags.get(key, ""))
            except Exception as e:
                self._embed_failures += 1
                logger.debug(f"Skip embed for {key}: {e}")

        pruned = index.prune_missing(set(wiki_keys))
        index.save()
        if embedded or pruned:
            logger.info(f"Embeddings: +{embedded} new/updated, -{pruned} pruned")
        return embedded, pruned

    #: After this many consecutive compile failures, the raw file is moved to
    #: raw/.failed/ and the failure is logged once with the source URL.
    MAX_COMPILE_RETRIES = 3

    # Errors that should NOT count toward the failure budget: transient external
    # issues (quota, rate limit, network, provider outage). Counting them caused
    # mass deletion of raw files during a Codex quota window on 2026-04-18.
    TRANSIENT_ERROR_MARKERS = (
        "usage limit",
        "quota",
        "rate limit",
        "rate-limit",
        "ratelimit",
        "429",
        "503",
        "502",
        "timeout",
        "timed out",
        "connection",
        "failed to connect",
        "ollama",
        "try again",
        "please retry",
        "temporarily unavailable",
    )

    @classmethod
    def _is_transient_error(cls, err: str) -> bool:
        if not err:
            return False
        low = err.lower()
        return any(marker in low for marker in cls.TRANSIENT_ERROR_MARKERS)

    def _handle_compile_failures(self, results: list[CompileResult]) -> list[CompileResult]:
        """Track compile failures and auto-remove persistently failing raw files.

        Transient errors (quota, rate limit, network) do NOT increment the
        counter — they retry on the next cron tick without penalty.

        For permanent failures, increments a compile_failures counter in the
        raw file's frontmatter. After MAX_COMPILE_RETRIES consecutive failures,
        MOVES the raw file to raw/.failed/ (not delete) so it can be recovered,
        and logs once to log.md. The source is only deleted once the quarantine
        copy has been read back intact.

        Every result is returned, quarantined ones flagged ``quarantined=True``,
        so run totals and ``compile --strict`` still count them as failures.
        """
        import yaml as _yaml

        kept = []
        for r in results:
            if r.success:
                kept.append(r)
                continue

            # Skip transient errors entirely — next cron run will retry
            if self._is_transient_error(r.error or ""):
                logger.info(
                    f"Transient error for {r.source_key.split('/')[-1]}, "
                    f"not incrementing failure count: {r.error[:80] if r.error else ''}"
                )
                kept.append(r)
                continue

            # Read current failure count from raw frontmatter
            try:
                raw_content = self.s3.download(r.source_key)
            except Exception:
                kept.append(r)
                continue

            fm, body = Frontmatter.from_yaml_block(raw_content)
            # Read compile_failures from raw YAML directly (not in Frontmatter model)
            failures = 0
            try:
                if raw_content.startswith("---"):
                    meta = _yaml.safe_load(raw_content.split("---", 2)[1]) or {}
                    failures = int(meta.get("compile_failures", 0))
            except Exception:
                pass
            failures += 1

            if failures >= self.MAX_COMPILE_RETRIES:
                # Quarantine rather than delete — recoverable by copying
                # raw/.failed/<file>.md back to raw/<file>.md.
                source_url = fm.original_link or fm.source or r.source_key
                if not self._quarantine(r.source_key, raw_content):
                    kept.append(r)  # source left in place; retried next run
                    continue
                logger.warning(
                    f"Quarantined persistently failing raw file after "
                    f"{failures} attempts: {r.source_key} "
                    f"(source: {source_url}, error: {r.error})"
                )
                console.print(
                    f"  [yellow]⚠ Quarantined {escape(r.source_key.split('/')[-1])} "
                    f"after {failures} failures: {escape((r.error or '')[:100])}[/yellow]"
                )
                try:
                    self.s3.delete(self._wiki_key_for(r.source_key))
                except Exception:
                    pass
                self._log_compile_removal(source_url, r.error or "unknown", failures)
                kept.append(r.model_copy(update={"quarantined": True}))
            else:
                # Increment failure count in raw frontmatter for next retry
                try:
                    if raw_content.startswith("---"):
                        meta = _yaml.safe_load(raw_content.split("---", 2)[1]) or {}
                    else:
                        meta = {}
                    meta["compile_failures"] = failures
                    new_fm = "---\n" + _yaml.dump(
                        meta, default_flow_style=False, allow_unicode=True
                    ) + "---\n\n"
                    self.s3.upload(r.source_key, new_fm + body)
                    logger.info(
                        f"Compile failure {failures}/{self.MAX_COMPILE_RETRIES} "
                        f"for {r.source_key.split('/')[-1]}"
                    )
                except Exception as e:
                    logger.debug(f"Could not update failure count: {e}")
                kept.append(r)

        return kept

    def _quarantine(self, raw_key: str, raw_content: str) -> bool:
        """Move ``raw_key`` to ``raw/.failed/``. True only when the source was moved.

        The copy is written, then read back and compared, and only then is the
        source deleted. Any failure leaves the source where it is.
        """
        try:
            quarantine_key = quarantine_key_for_raw(self.profile.prefix, raw_key)
        except ValueError as e:
            logger.warning(f"Cannot quarantine {raw_key}: {e}. Source left in place.")
            return False
        if quarantine_key == raw_key:
            logger.warning(f"Cannot quarantine {raw_key} onto itself. Source left in place.")
            return False
        try:
            self.s3.upload(quarantine_key, raw_content)
            if self.s3.download(quarantine_key) != raw_content:
                raise RuntimeError("quarantine copy does not match the source")
        except Exception as e:
            logger.warning(
                f"Could not quarantine {raw_key} → {quarantine_key}: {e}. Source left in place."
            )
            return False
        try:
            self.s3.delete(raw_key)
        except Exception as e:
            # The copy exists, so nothing is lost; the source is retried next run.
            logger.warning(f"Quarantine copy written but {raw_key} could not be removed: {e}")
            return False
        return True

    #: Re-reads of wiki/log.md when a conditional write lost a race.
    LOG_WRITE_ATTEMPTS = 5

    def _update_log(self, transform: Callable[[str], str], entry: str = "") -> None:
        """Rewrite wiki/log.md as ``transform(current)`` without losing entries.

        The write is conditional on the ETag read (If-None-Match when the log
        does not exist yet), so an entry another process added in between is
        re-read and kept instead of overwritten. ``entry`` is the text the
        transform adds: after a conflict, a log that already holds it means our
        write committed and only its response was lost (the SDK retry then got
        412), so it is not added twice. A server that returns no ETag on GET
        cannot be written conditionally; the log is then written plainly.
        Raises when the final write fails; callers treat the log as best-effort.
        """
        from agent2kb.s3_client import PreconditionFailed

        global _log_etag_warned
        log_key = f"{self.profile.prefix}wiki/log.md"
        for attempt in range(self.LOG_WRITE_ATTEMPTS):
            read = True
            try:
                existing, etag = self.s3.download_with_etag(log_key)
            except Exception:
                existing, etag, read = "", "", False
            if attempt and entry and entry in existing:
                return
            conditional: dict = {"if_match": etag or None, "if_none_match": not etag}
            if read and not etag:
                if not _log_etag_warned:
                    _log_etag_warned = True
                    logger.warning(
                        "The S3 endpoint returned no ETag for wiki/log.md; writing the log "
                        "without a conditional check (concurrent entries may be lost)."
                    )
                conditional = {}
            try:
                self.s3.upload(log_key, transform(existing), **conditional)
                return
            except PreconditionFailed:
                if attempt == self.LOG_WRITE_ATTEMPTS - 1:
                    raise

    def _log_compile_removal(self, source_url: str, error: str, attempts: int) -> None:
        """Append a one-time removal entry to wiki/log.md."""
        entry = (
            f"\n### {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- ✗ Raw file removed after {attempts} compile failures\n"
            f"  - Source: {source_url}\n"
            f"  - Last error: {error[:200]}\n"
        )

        def add(existing: str) -> str:
            if "---" in existing:
                parts = existing.split("---", 2)
                if len(parts) >= 3:
                    return f"---{parts[1]}---" + "\n" + entry + parts[2]
            return existing + entry

        try:
            self._update_log(add, entry)
        except Exception:
            pass

    def _compile_batch(
        self, raw_keys: list[str], output_insights: list
    ) -> list["CompileResult"]:
        """Compile a list of raw files, stopping early if the LLM is down.

        Without this, a provider that is out of credits or unauthorized
        still gets one full call attempt per remaining file. Each failure
        costs a round trip and, with a reasoning model, can take minutes —
        so a run that can accomplish nothing would grind on for an hour.
        """
        results: list[CompileResult] = []
        for i, raw_key in enumerate(raw_keys):
            result = self._compile_single(raw_key, output_insights)
            results.append(result)
            if not result.success and (
                self._provider_down or is_provider_exhausted(result.error or "")
            ):
                self._provider_down = True
                remaining = len(raw_keys) - i - 1
                logger.error(
                    f"LLM provider is unavailable: {result.error}. "
                    f"Stopping this run with {remaining} file(s) not attempted."
                )
                break
        return results

    def _wiki_key_for(self, raw_key: str) -> str:
        """The wiki page compiled from ``raw_key``, derived relative to the prefix.

        Raises ``ValueError`` when the key is not a source of this KB or the
        destination would be the source itself. A plain ``"/raw/"`` replace is
        a no-op for a whole-bucket KB (keys start with ``raw/``) and used to
        upload the compiled page over the original source.
        """
        wiki_key = wiki_key_for_raw(self.profile.prefix, raw_key)
        if wiki_key == raw_key:
            raise ValueError(f"wiki destination equals the source: {raw_key!r}")
        return wiki_key

    def _select_pending(self, raw_keys: list[str]) -> list[str]:
        """Pick the raw files that need compiling, in as few round trips
        as possible.

        The per-file check costs a HEAD plus two GETs, which on a few
        hundred files dominates a run that often has nothing to do. Instead:
        one listing of wiki/ answers "does the page exist" for every file at
        once, and the bodies of the survivors are fetched in parallel.
        """
        self._wiki_listing = self.s3.list_objects(sub_prefix="wiki/", suffix=".md")
        wiki_etags = {o.key: o.etag for o in self._wiki_listing}
        cache = self._cache()

        pending: list[str] = []
        to_verify: list[str] = []
        wiki_for: dict[str, str] = {}
        for raw_key in raw_keys:
            try:
                wiki_for[raw_key] = self._wiki_key_for(raw_key)
            except ValueError as e:
                logger.warning(f"Skipping {raw_key}: {e}")
                continue
            if wiki_for[raw_key] not in wiki_etags:
                pending.append(raw_key)  # no wiki page yet — definitely compile
            else:
                to_verify.append(raw_key)

        # A pair whose raw and wiki ETags both match the last successful check
        # is still up to date; only the rest need their bodies downloaded. The
        # listings are taken before any body is read, so a recorded ETag can
        # only be older than the body it vouches for, never newer.
        raw_etags: dict[str, str] = {}
        if to_verify:
            raw_etags = {
                o.key: o.etag for o in self.s3.list_objects(sub_prefix="raw/", suffix=".md")
            }
            unchanged = {
                k for k in to_verify
                if cache.pair_current(k, raw_etags.get(k, ""), wiki_etags[wiki_for[k]])
            }
            if unchanged:
                logger.debug(f"Up-to-date check: {len(unchanged)} pairs unchanged by ETag")
                to_verify = [k for k in to_verify if k not in unchanged]

        if to_verify:
            wanted = to_verify + [wiki_for[k] for k in to_verify]
            bodies = self.s3.download_many(wanted)
            for raw_key in to_verify:
                raw_content = bodies.get(raw_key)
                if raw_content is None:
                    continue  # can't read raw — skip, same as before
                wiki_content = bodies.get(wiki_for[raw_key])
                if wiki_content is None:
                    pending.append(raw_key)
                    continue
                try:
                    wiki_fm, _ = Frontmatter.from_yaml_block(wiki_content)
                except Exception:
                    pending.append(raw_key)
                    continue
                if wiki_fm.raw_content_hash != content_hash(raw_content):
                    pending.append(raw_key)
                    cache.forget_pair(raw_key)
                else:
                    logger.debug(f"Skipping up-to-date: {raw_key}")
                    cache.record_pair(
                        raw_key, raw_etags.get(raw_key, ""), wiki_etags[wiki_for[raw_key]]
                    )

        cache.retain(raw_keys=raw_keys)
        cache.save()
        return pending

    def _cache(self):
        """The compile cache, loaded on first use (one GET per run)."""
        if self._compile_cache is None:
            from agent2kb.compile.cache import CompileCache

            self._compile_cache = CompileCache(self.s3, self.profile)
        return self._compile_cache

    def _needs_compile(self, raw_key: str, wiki_key: str) -> bool:
        """Check if raw file needs (re)compilation.

        Fast path: first checks if wiki file exists at all (cheap S3 HEAD).
        If wiki exists, downloads both to compare content hashes.
        Returns True if wiki doesn't exist or raw content has changed.
        """
        # Fast path: if wiki doesn't exist, definitely needs compile
        if not self.s3.exists(wiki_key):
            return True

        try:
            raw_content = self.s3.download(raw_key)
        except Exception:
            return False  # Can't read raw — skip

        raw_hash = content_hash(raw_content)

        try:
            wiki_content = self.s3.download(wiki_key)
            wiki_fm, _ = Frontmatter.from_yaml_block(wiki_content)
            if wiki_fm.raw_content_hash == raw_hash:
                return False  # Content unchanged — skip
            return True  # Content changed — recompile
        except Exception:
            return True  # Wiki doesn't exist — needs compile

    def _append_to_log(self, results: list[CompileResult]) -> None:
        """Prepend compile results to wiki/log.md (newest first)."""
        import datetime

        today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        ok = sum(1 for r in results if r.success)
        total_duration = sum(r.duration_sec for r in results)
        total_tok_in = sum(r.tokens_in for r in results)
        total_tok_out = sum(r.tokens_out for r in results)

        entry_lines = [f"## {today} — Compile ({self.provider.value} / {self.model})\n"]
        entry_lines.append(
            f"- **Summary**: {ok}/{len(results)} compiled | "
            f"{total_duration:.0f}s total | "
            f"{total_tok_in:,}+{total_tok_out:,} tokens"
        )
        for r in results:
            status = "✓" if r.success else "✗"
            stats = f"{r.duration_sec:.0f}s, {r.tokens_in}+{r.tokens_out}tok"
            if r.success:
                lines_detail = f"- {status} {r.source_key} → {r.wiki_key} ({stats})"
                entry_lines.append(lines_detail)
            else:
                entry_lines.append(f"- {status} {r.source_key} — {r.error}")

        new_entry = "\n".join(entry_lines) + "\n"

        def add(existing: str) -> str:
            # Split existing into header (frontmatter + title) and body,
            # then prepend the new entry after the header.
            header, body = _split_log_header(existing)
            return header + "\n" + new_entry + "\n" + body

        try:
            self._update_log(add, new_entry)
        except Exception as e:
            logger.debug(f"Could not update log.md: {e}")

    def _update_index(self) -> None:
        """Rebuild wiki/index.md as a catalog of all wiki pages.

        Karpathy pattern: index.md is content-oriented — each page listed
        with a link, a one-line summary, and tags. The LLM reads index first
        to find relevant pages when answering queries.
        """
        index_key = f"{self.profile.prefix}wiki/index.md"
        wiki_objects = self.s3.list_objects(sub_prefix="wiki/", suffix=".md")
        wiki_keys = [o.key for o in wiki_objects]
        etags = {o.key: o.etag for o in wiki_objects}
        cache = self._cache()

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        skip = {"index.md", "log.md", ".gitkeep"}

        # Split into articles, concept pages, and pinned topics.
        # Topics live under wiki/topics/ and are hand-crafted reference
        # pages (compile leaves them alone). They get their own index
        # section so users can scan curated reading material distinct
        # from auto-compiled articles.
        page_keys = []
        concept_keys = []
        topic_keys = []
        for k in wiki_keys:
            fn = k.split("/")[-1]
            if fn in skip:
                continue
            if "/concepts/" in k:
                concept_keys.append(k)
            elif "/topics/" in k:
                topic_keys.append(k)
            else:
                page_keys.append(k)

        lines = [
            "---",
            f'title: "{self.profile.name} Knowledge Base"',
            f'date: "{today}"',
            "tags: [index]",
            "---",
            "",
            f"# {self.profile.name} Knowledge Base",
            "",
            f"> {len(page_keys)} articles, {len(concept_keys)} concepts, {len(topic_keys)} topics",
            "",
        ]

        # --- Read metadata for all article pages (shared by weekly + grouped sections) ---
        # Every page's frontmatter is needed. Pages whose ETag matches the
        # compile cache reuse the metadata parsed last time; only new or
        # changed pages are downloaded, in parallel.
        page_entries = [  # (date, link_name, title, tags_display, primary_tag)
            tuple(m) for m in self._index_metas(page_keys, etags, _article_index_meta)
        ]

        # --- "Past N Days" recap section (recent_days; 0 = off) ---
        lines.extend(
            self._build_recent_digest(page_entries, index_key)
        )

        # Group by primary tag. Tags appearing for < 2 articles go to "Other".
        from collections import Counter
        tag_counts = Counter(e[4] for e in page_entries if e[4])
        min_group_size = 2

        # Build groups: {display_label: [(date, link, title, tags_display)]}
        groups: dict[str, list] = {}
        for date, link, title, tags_disp, ptag in page_entries:
            if ptag and tag_counts[ptag] >= min_group_size:
                label = ptag.replace("-", " ").title()
            else:
                label = "Other"
            groups.setdefault(label, []).append((date, link, title, tags_disp))

        # Sort groups: largest first, "Other" always last
        sorted_labels = sorted(
            groups.keys(),
            key=lambda g: (g == "Other", -len(groups[g]), g),
        )

        lines.append("## Articles\n")
        for label in sorted_labels:
            entries = groups[label]
            entries.sort(key=lambda e: e[0] or "", reverse=True)
            lines.append(f"### {label}\n")
            for date_str, link_name, page_title, page_tags in entries:
                date_prefix = date_str[:10] + " " if date_str else ""
                lines.append(f"- {date_prefix}[[{link_name}|{page_title}]]{page_tags}")
            lines.append("")

        # --- Concepts section (only synthesized concept pages) ---
        if concept_keys:
            lines.append("")
            lines.append("## Concepts\n")
            concept_entries = [
                tuple(m) for m in self._index_metas(concept_keys, etags, _concept_index_meta)
            ]

            concept_entries.sort(key=lambda e: (-e[0], e[1].lower()))
            for refs, page_title, link_name in concept_entries:
                ref_info = f" ({refs})" if refs else ""
                lines.append(f"- [[{link_name}]] — {page_title}{ref_info}")

        # --- Topics section (pinned hand-crafted reference pages) ---
        # Listed as `[[topics/<slug>]]` so the prefilter and index can
        # resolve them with the same logic that handles concept entries.
        if topic_keys:
            lines.append("")
            lines.append("## Topics\n")
            topic_entries = [
                tuple(m) for m in self._index_metas(topic_keys, etags, _topic_index_meta)
            ]

            # Newest first — pinned references age and the most recent
            # consolidations are usually the most actionable.
            topic_entries.sort(key=lambda e: (e[0] or "", e[1].lower()), reverse=True)
            for date_str, page_title, link_name in topic_entries:
                date_prefix = date_str[:10] + " " if date_str else ""
                lines.append(f"- {date_prefix}[[{link_name}|{page_title}]]")

        lines.append("")
        cache.retain(page_keys=wiki_keys)
        index_text = "\n".join(lines)
        index_hash = content_hash(index_text)
        # An identical index.md over the object we wrote last time is not
        # rewritten: a no-op tick then costs no PUT and no sync churn.
        if cache.page_meta(index_key, etags.get(index_key, "")) == index_hash:
            logger.debug("index.md unchanged")
            cache.save()
            return
        try:
            new_etag = self.s3.upload(index_key, index_text)
            if isinstance(new_etag, str):
                cache.record_page(index_key, new_etag, index_hash)
            logger.info(
                f"Updated index.md ({len(page_keys)} articles, "
                f"{len(concept_keys)} concepts, {len(topic_keys)} topics)"
            )
        except Exception as e:
            logger.warning(f"Failed to update index.md: {e}")
            console.print(f"  [yellow]⚠ index.md update failed: {escape(str(e))}[/yellow]")
        cache.save()

    def _index_metas(self, keys: list[str], etags: dict[str, str], parse) -> list[list]:
        """``parse(key, content)`` for each key, reusing cached results by ETag.

        Only keys whose ETag is new or changed are downloaded (in parallel).
        A page that cannot be read gets ``parse(key, "")``, the fallback entry.
        """
        cache = self._cache()
        metas: dict[str, list] = {}
        missing = []
        for key in keys:
            cached = cache.page_meta(key, etags.get(key, ""))
            if isinstance(cached, list):
                metas[key] = cached
            else:
                missing.append(key)
        bodies = self.s3.download_many(missing)
        for key in missing:
            content = bodies.get(key)
            metas[key] = parse(key, content or "")
            if content is not None:
                cache.record_page(key, etags.get(key, ""), metas[key])
        return [metas[k] for k in keys]

    def _build_recent_digest(
        self,
        page_entries: list[tuple],
        index_key: str,
    ) -> list[str]:
        """Build the 'Past N Days' recap section for index.md (N = ``recent_days``).

        Rolling N-day window (not ISO week), with an LLM-generated summary
        of the period's knowledge themes. Summary is cached and only
        regenerated when the article count or the window changes, to avoid
        wasting tokens. ``recent_days: 0`` leaves the section out.
        """
        days = self.profile.recent_days
        if days <= 0:
            return []
        now = datetime.datetime.now()
        cutoff = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        recent_articles = []
        for date_str, link_name, title, tags_disp, _ in page_entries:
            if date_str and date_str[:10] >= cutoff:
                recent_articles.append((date_str, link_name, title, tags_disp))

        if not recent_articles:
            return []

        recent_articles.sort(key=lambda e: e[0], reverse=True)

        # Cache check: reuse existing summary if article count unchanged
        cached_summary = ""
        try:
            existing_index = self.s3.download(index_key)
            # Older indexes carry no window in the marker: they covered 7 days.
            cm = re.search(
                r"<!-- digest-count:(\d+)(?::(\d+))? -->",
                existing_index,
            )
            if (
                cm
                and int(cm.group(1)) == len(recent_articles)
                and int(cm.group(2) or 7) == days
            ):
                m = re.search(
                    r"## Past \d+ Days[^\n]*\n\n> (.+?)(?:\n\n|\n##)",
                    existing_index,
                    re.DOTALL,
                )
                if m:
                    cached_summary = m.group(1).strip()
        except Exception:
            pass

        summary = cached_summary
        if not summary:
            summary = self._generate_weekly_summary(recent_articles, f"past {days} days")

        lines = [
            f"## Past {days} Days ({len(recent_articles)} articles)",
            "",
        ]
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        for date_str, link_name, title, _ in recent_articles:
            date_prefix = date_str[:10] if date_str else ""
            lines.append(f"- {date_prefix} [[{link_name}|{title}]]")

        # The window is recorded only when it differs from 7, so default indexes keep
        # the marker they always had.
        window = "" if days == 7 else f":{days}"
        lines.append(f"<!-- digest-count:{len(recent_articles)}{window} -->")
        lines.append("")
        return lines

    def _generate_weekly_summary(
        self,
        week_articles: list[tuple],
        week_label: str,
    ) -> str:
        """LLM-generated 2-3 sentence summary of the week's knowledge themes."""
        from agent2kb.language import language_instruction

        titles_and_tags = "\n".join(
            f"- {title}{tags}" for _, _, title, tags in week_articles
        )
        prompt = (
            "Given the following articles ingested into a personal knowledge base "
            f"during week {week_label}, write a 2-3 sentence summary of the key "
            "themes and insights. Focus on what connects these articles — shared "
            "topics, emerging patterns, contrasting viewpoints. Be specific about "
            "the actual content, not generic.\n\n"
            f"{language_instruction(self.profile.output_language)}\n"
            f"Articles:\n{titles_and_tags}"
        )
        try:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Write the weekly summary."},
            ]
            result = self._call_llm_with_fallback(messages, purpose="aux")
            # Clean: take just the first paragraph, strip quotes if the LLM wraps
            result = result.strip().split("\n\n")[0].strip()
            if result.startswith('"') and result.endswith('"'):
                result = result[1:-1]
            if result.startswith("> "):
                result = result[2:]
            logger.info(f"Generated weekly summary for {week_label}")
            return result
        except Exception as e:
            logger.debug(f"Weekly summary generation failed: {e}")
            return ""

    #: Safety cap on how many concept pages a single compile run may create.
    #: Creations used to be unbounded: ingesting a couple of feeds can push
    #: dozens of new wikilinks past the reference threshold at once, and each
    #: one is a separate sequential LLM call, so a run could stretch for an
    #: hour with no upper bound. Concepts deferred here stay above threshold
    #: and are picked up by the next run.
    MAX_CONCEPT_CREATIONS_PER_RUN = 15

    #: Safety cap on how many LLM-based concept refreshes a single compile
    #: run is allowed to perform. Protects against runaway token usage if a
    #: future bug ever causes mass refresh. Legacy metadata-only backfills
    #: (zero-token) are NOT counted against this cap.
    MAX_CONCEPT_REFRESHES_PER_RUN = 10

    def _synthesize_concepts(self) -> str:
        """Generate concept pages for wikilinks referenced by threshold+ articles.

        Karpathy pattern: concept/entity pages accumulate knowledge over time.
        Instead of creating empty stubs for every wikilink, we only create pages
        for concepts that appear across multiple sources — and we synthesize
        actual content from the referencing articles.

        Three paths for existing pages:
        - **No source_pages field (legacy schema)** → metadata-only backfill.
          Write source_pages/last_refreshed without touching the body. Zero LLM.
        - **source_pages unchanged** → skip entirely.
        - **source_pages changed** → LLM re-synthesis, capped per run.

        Returns ``"done"``, ``"deferred"`` (a per-run cap left work for the
        next run) or ``"failed"`` (an LLM call failed; retried with backoff).
        """
        from agent2kb.compile.schema import concept_synthesis_prompt

        threshold = self.profile.concept_threshold
        if threshold <= 0:
            return "done"
        concept_prompt = concept_synthesis_prompt(self.profile.output_language)

        # Scan all wiki pages for wikilinks
        wiki_keys = self.s3.list_wiki_files()
        skip = {"index.md", "log.md", ".gitkeep"}
        concept_refs: dict[str, list[tuple[str, str]]] = {}  # concept → [(page_fn, snippet)]

        # topics/ are pinned hand-crafted references (e.g. "Redis
        # consolidated guide"). Their wikilinks should NOT trigger
        # concept-page creation — a topic page already covers the
        # consolidated view, and double-counting would inflate refs.
        scan_keys = [
            key
            for key in wiki_keys
            if key.split("/")[-1] not in skip
            and "/concepts/" not in key
            and "/topics/" not in key
        ]
        scan_bodies = self.s3.download_many(scan_keys)

        for key in scan_keys:
            fn = key.split("/")[-1]
            try:
                content = scan_bodies[key]
                fm, body = Frontmatter.from_yaml_block(content)
                page_fn = fn.replace(".md", "")
                # Normalize: ``extract_wikilinks`` returns the FULL inside
                # of ``[[target|display#anchor]]`` including alias and
                # anchor. Using that verbatim as a concept-refs key
                # produced corrupted concept slugs after slugify — e.g.
                # ``[[redis-cluster|Redis Cluster]]`` collapsed to
                # ``redis-clusterredis-cluster`` (alias re-glued to target
                # because ``|`` was stripped without an inserted
                # separator). Strip alias + anchor here so the concept
                # name is the bare target.
                links = {_clean_wikilink_target(raw) for raw in extract_wikilinks(body)}
                links.discard("")
                for link in links:
                    snippet = self._extract_context(body, link, max_chars=300)
                    concept_refs.setdefault(link, []).append((page_fn, snippet))
            except Exception:
                continue

        # Query→concept feedback loop: also scan outputs/ for wikilinks.
        # When the user repeatedly asks about an entity (mentioned in N+
        # query answers), it earns a concept page even if the underlying
        # articles never crossed the threshold — usage is the strongest
        # relevance signal in a personal KB. Each output is treated as a
        # "page" for tally purposes; sources prefixed with ``query:`` so
        # the concept page's source_pages list shows where the demand
        # actually came from.
        try:
            output_keys = self.s3.list_keys(sub_prefix="outputs/", suffix=".md")
            output_bodies = self.s3.download_many(output_keys)
            for okey in output_keys:
                try:
                    ocontent = output_bodies[okey]
                    _ofm, obody = Frontmatter.from_yaml_block(ocontent)
                    # Use the FULL output stem — earlier ``[:60]`` truncation
                    # produced refs that didn't match the actual filename
                    # (output stems can be ~76 chars: timestamp + 60-char
                    # slug), leaving wiki pages with broken ``[[query:...]]``
                    # links. Lint resolver registers all three forms; emit
                    # the canonical full-stem one here.
                    page_fn = "query:" + okey.split("/")[-1].replace(".md", "")
                    # Same normalization as the wiki/ scan above —
                    # strip ``|alias`` and ``#anchor`` so concept_refs
                    # keys never contain wikilink syntax characters.
                    raw_links = {
                        _clean_wikilink_target(raw)
                        for raw in extract_wikilinks(obody)
                    }
                    raw_links.discard("")
                    for link in raw_links:
                        snippet = self._extract_context(obody, link, max_chars=300)
                        concept_refs.setdefault(link, []).append((page_fn, snippet))
                except Exception:
                    continue
        except Exception:
            pass  # No outputs/ yet — fresh KB.

        # Alias merge: collapse "Redis" + "Redis Cluster" into one concept,
        # collapse "Persistent Memory" + "Persistent Memory / 持久記憶"
        # into one. Without this, every variant gets its own concept page
        # and the knowledge graph fragments. Canonical name for each
        # cluster is whichever variant collected the most refs (ties
        # broken by length — the longer form is usually the explicit one
        # the LLM eventually settles on).
        concept_refs = _merge_concept_aliases(concept_refs)

        # Filter to concepts at or above threshold
        concepts_prefix = f"{self.profile.prefix}wiki/concepts/"
        # One listing answers "does this concept page exist" for every
        # candidate, instead of a HEAD request per concept.
        existing_concepts = set(self.s3.list_keys(sub_prefix="wiki/concepts/", suffix=".md"))
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        created = 0
        refreshed = 0
        migrated = 0  # legacy pages backfilled to new schema (zero LLM)
        deferred = 0  # refreshes skipped because per-run cap was hit
        failed = 0  # LLM calls that raised; the concept stays pending

        for concept, refs in concept_refs.items():
            if len(refs) < threshold:
                continue

            # Slugify concept name for filename
            slug = re.sub(r"[^\w\s-]", "", concept, flags=re.UNICODE)
            slug = re.sub(r"[-\s]+", "-", slug).strip("-").lower()
            if not slug:
                continue
            concept_key = f"{concepts_prefix}{slug}.md"

            # Current source-page set (deterministic, sorted for stable compare)
            current_sources = sorted({page_fn for page_fn, _ in refs})
            existing_meta: dict = {}
            if concept_key in existing_concepts:
                existing_meta = self._read_concept_meta(concept_key)

                # Legacy path: page predates source_pages schema. Backfill
                # the metadata without touching the body (zero LLM tokens).
                # Future compiles will then compare source sets properly.
                if existing_meta and "source_pages" not in existing_meta:
                    try:
                        self._migrate_concept_metadata(
                            concept_key, existing_meta, current_sources, len(refs), today
                        )
                        migrated += 1
                    except Exception as e:
                        logger.debug(f"Concept metadata backfill failed for {concept}: {e}")
                    continue

                if not self._concept_needs_refresh(existing_meta, current_sources):
                    # No new source pages → leave existing concept page as-is
                    continue

                # Needs LLM refresh — honor the per-run cap.
                if refreshed >= self.MAX_CONCEPT_REFRESHES_PER_RUN:
                    deferred += 1
                    continue
            elif created >= self.MAX_CONCEPT_CREATIONS_PER_RUN:
                # New concept, but this run has created its share already.
                deferred += 1
                continue

            # Build snippets for LLM
            snippet_text = ""
            for page_fn, snippet in refs:
                snippet_text += f"\n### From [[{page_fn}]]\n{snippet}\n"

            # Call LLM to synthesize (or re-synthesize) concept page
            try:
                prompt = concept_prompt.format(
                    concept=concept,
                    ref_count=len(refs),
                    snippets=snippet_text,
                )
                messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Generate concept page for: {concept}"},
                ]
                synthesis = self._call_llm_with_fallback(messages, purpose="aux")
                synthesis = ensure_traditional_chinese(synthesis, self.profile.output_language)

                sources_yaml = "\n".join(f"  - {s}" for s in current_sources)
                # Preserve original creation date when refreshing
                created_date = existing_meta.get("date") or today
                page_content = (
                    f"---\n"
                    f'title: "{concept}"\n'
                    f'date: "{created_date}"\n'
                    f'last_refreshed: "{today}"\n'
                    f"tags: [concept]\n"
                    f"type: concept\n"
                    f"source_kind: concept\n"
                    f"auto_generated: true\n"
                    f"references: {len(refs)}\n"
                    f"source_pages:\n{sources_yaml}\n"
                    f"---\n\n"
                    f"{synthesis}\n"
                )
                self.s3.upload(concept_key, page_content)
                if existing_meta:
                    refreshed += 1
                    logger.info(
                        f"Refreshed concept: {concept} "
                        f"({len(existing_meta.get('source_pages') or [])} → {len(refs)} refs)"
                    )
                else:
                    created += 1
                    logger.info(f"Synthesized concept: {concept} ({len(refs)} refs)")
            except ProviderExhaustedError as e:
                # Every remaining concept would fail the same way. Stop now;
                # the backlog is picked up once the provider works again.
                self._provider_down = True
                logger.error(f"Stopping concept synthesis — {e}")
                failed += 1
                break
            except Exception as e:
                failed += 1
                logger.debug(f"Could not synthesize concept '{concept}': {e}")

        parts = []
        if created:
            parts.append(f"{created} synthesized")
        if refreshed:
            parts.append(f"{refreshed} refreshed")
        if migrated:
            parts.append(f"{migrated} metadata-backfilled")
        if deferred:
            parts.append(
                f"{deferred} deferred (caps: {self.MAX_CONCEPT_CREATIONS_PER_RUN} new, "
                f"{self.MAX_CONCEPT_REFRESHES_PER_RUN} refresh)"
            )
        if parts:
            console.print(
                f"  [cyan]Concepts: {', '.join(parts)}"
                f" (threshold: {threshold}+ references)[/cyan]"
            )
            if deferred:
                console.print(
                    "  [yellow]⚠ Some concept refreshes were deferred to limit "
                    "per-run LLM cost. They will re-queue on the next compile.[/yellow]"
                )
        if failed:
            return "failed"
        return "deferred" if deferred else "done"

    def _migrate_concept_metadata(
        self,
        concept_key: str,
        existing_meta: dict,
        current_sources: list[str],
        ref_count: int,
        today: str,
    ) -> None:
        """Zero-LLM backfill: add source_pages to a legacy concept page.

        Preserves the existing synthesized body verbatim — only rewrites the
        YAML frontmatter so future `_concept_needs_refresh()` calls work. This
        is the migration path for pages created before the source_pages schema
        existed, avoiding a one-shot re-synthesis of the entire concept layer.
        """
        content = self.s3.download(concept_key)
        if not content.startswith("---"):
            # Missing frontmatter entirely — skip rather than fabricate one.
            logger.debug(f"Concept {concept_key} has no frontmatter to migrate")
            return
        parts = content.split("---", 2)
        if len(parts) < 3:
            return
        body = parts[2].lstrip("\n")

        sources_yaml = "\n".join(f"  - {s}" for s in current_sources)
        created_date = existing_meta.get("date") or today
        title = existing_meta.get("title") or concept_key.split("/")[-1].replace(".md", "")
        # Carry over any existing type/auto_generated flags rather than guessing.
        type_line = f"type: {existing_meta.get('type', 'concept')}\n"
        auto_line = (
            f"auto_generated: {str(existing_meta.get('auto_generated', True)).lower()}\n"
        )
        new_front = (
            f"---\n"
            f'title: "{title}"\n'
            f'date: "{created_date}"\n'
            f'last_refreshed: "{today}"\n'
            f"tags: [concept]\n"
            f"{type_line}"
            f"{auto_line}"
            f"references: {ref_count}\n"
            f"source_pages:\n{sources_yaml}\n"
            f"---\n\n"
        )
        self.s3.upload(concept_key, new_front + body)
        logger.info(
            f"Migrated concept metadata: {title} "
            f"({len(current_sources)} source pages, body preserved)"
        )

    def _read_concept_meta(self, concept_key: str) -> dict:
        """Return YAML frontmatter of a concept page, or {} on failure."""
        try:
            import yaml as _yaml
            content = self.s3.download(concept_key)
            if not content.startswith("---"):
                return {}
            _, yaml_block, _ = content.split("---", 2)
            meta = _yaml.safe_load(yaml_block) or {}
            return meta if isinstance(meta, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _concept_needs_refresh(existing_meta: dict, current_sources: list[str]) -> bool:
        """Decide whether an existing concept page should be re-synthesized.

        Policy: refresh iff the *set of source pages* changed. This avoids
        burning LLM tokens on stable concepts while still picking up genuinely
        new evidence (a brand new article, or an old one removed/renamed).

        Contract: callers MUST handle the "legacy page with no source_pages
        field" case themselves (by running `_migrate_concept_metadata`)
        BEFORE calling this function. When `source_pages` is absent here,
        we return False — "don't refresh" — because triggering mass
        re-synthesis on schema migration would burn thousands of tokens
        on bodies that are already correct.
        """
        if "source_pages" not in existing_meta:
            return False
        prior_sources = existing_meta.get("source_pages") or []
        if not isinstance(prior_sources, list):
            return False  # Malformed — let the operator clean it up manually.
        return sorted(prior_sources) != sorted(current_sources)

    @staticmethod
    def _extract_context(body: str, link: str, max_chars: int = 300) -> str:
        """Extract paragraph around a [[wikilink]] mention for concept synthesis."""
        marker = f"[[{link}]]"
        pos = body.find(marker)
        if pos < 0:
            return ""
        start = max(0, body.rfind("\n\n", 0, pos))
        end = body.find("\n\n", pos + len(marker))
        if end < 0:
            end = min(len(body), pos + max_chars)
        return body[start:end].strip()[:max_chars]

    def _import_clippings(self) -> list[tuple[str, str]]:
        """Auto-import Web Clipper clippings from Clippings/ to raw/.

        Checks both {prefix}Clippings/ and {prefix}wiki/Clippings/ since
        Remotely Save path depends on base directory config.
        Zero cost when no clippings exist (just S3 LIST calls).
        Returns list of (title, source_url) tuples for import logging.
        """
        clip_keys = []
        for sub in ["Clippings/", "wiki/Clippings/"]:
            try:
                keys = self.s3.list_keys(sub_prefix=sub, suffix=".md")
                clip_keys.extend(keys)
            except Exception:
                pass

        if not clip_keys:
            return []

        import datetime as _dt

        from agent2kb.dedup import RawIndex

        # A clipping can come back after import: the Obsidian device that
        # clipped it still holds the local file and re-uploads it on a later
        # sync. Duplicates are recognised by content hash and source URL —
        # never by the dated raw key, which only matched on the same day.
        raw_index = RawIndex(self.s3)
        moved = 0
        skipped = 0
        imported: list[tuple[str, str]] = []
        for key in clip_keys:
            try:
                content = self.s3.download(key)
                fm, body = Frontmatter.from_yaml_block(content)

                if not fm.source_type:
                    fm.source_type = "url"
                if not fm.date:
                    fm.date = _dt.datetime.now().strftime("%Y-%m-%d")
                if not fm.original_link and fm.source:
                    fm.original_link = fm.source
                fm.compiled = False

                # Build raw key with content hash for dedup
                from agent2kb.utils import slugify
                date_str = _dt.datetime.now().strftime("%Y%m%d")
                slug = slugify(fm.title or key.split("/")[-1].replace(".md", ""))
                short_hash = content_hash(body)[:8]
                raw_key = f"{self.profile.prefix}raw/{date_str}-{slug}-{short_hash}.md"

                link = fm.original_link or fm.source or ""
                existing = raw_index.by_hash(short_hash) or raw_index.by_url(link)
                if existing:
                    self.s3.delete(key)
                    skipped += 1
                    logger.info(
                        f"Clipping already in raw/ as {existing.split('/')[-1]} — "
                        f"discarded duplicate {key.split('/')[-1]}"
                    )
                    continue

                self.s3.upload(raw_key, fm.to_yaml_block() + "\n" + body)
                self.s3.delete(key)
                raw_index.add(raw_key, link)
                moved += 1
                imported.append((fm.title or slug, fm.original_link or fm.source or ""))
                logger.info(f"Clipping → raw: {fm.title or slug}")
            except Exception as e:
                logger.warning(f"Failed to import clipping {key}: {e}")

        if skipped:
            console.print(
                f"  [dim]Discarded {skipped} Web Clipper clipping(s) already in raw/[/dim]"
            )
        if moved:
            console.print(f"  [cyan]Imported {moved} Web Clipper clipping(s) to raw/[/cyan]")
            self._log_clipping_imports(imported)
        return imported

    def _log_clipping_imports(self, imported: list[tuple[str, str]]) -> None:
        """Prepend a Clipping-import entry to wiki/log.md."""
        import datetime

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry_lines = [f"## {ts} — Web Clipper import ({len(imported)})\n"]
        for title, url in imported:
            if url:
                entry_lines.append(f"- 📎 [{title}]({url})")
            else:
                entry_lines.append(f"- 📎 {title}")
        new_entry = "\n".join(entry_lines) + "\n"

        def add(existing: str) -> str:
            header, body = _split_log_header(existing)
            return header + "\n" + new_entry + "\n" + body

        try:
            self._update_log(add, new_entry)
        except Exception as e:
            logger.debug(f"Could not log clipping imports: {e}")

    def _load_output_insights(self) -> list[tuple[str, str]]:
        """Load query outputs for the compounding loop, actor-weighted.

        Returns list of ``(question, body_preview)`` tuples. Relevance
        filtering happens per-file in ``_compile_single`` to keep
        unrelated queries from leaking into a compile.

        Actor weighting (2026-05-14): user-typed captures
        (``outputs/<...>.md``) carry the operator's intent and almost
        always reflect a question worth feeding back. Agent captures
        (``outputs/agent/<...>.md``) are auto-enrichment fan-outs and
        can flood the 15-slot window after a few ingests. We fill
        user slots first, then top up with the most recent agent
        captures — so a busy ingest day can't push real questions out.
        """
        budget = 15
        try:
            output_keys = self.s3.list_output_files()
        except Exception:
            return []

        if not output_keys:
            return []

        # Newest-first split. Path layout is authoritative; we don't
        # read frontmatter at this stage (cheap listing, expensive
        # downloads happen below).
        user_keys: list[str] = []
        agent_keys: list[str] = []
        for k in sorted(output_keys, reverse=True):
            if "/outputs/agent/" in k or k.startswith("outputs/agent/"):
                agent_keys.append(k)
            else:
                user_keys.append(k)

        # Reserve at least the latest few user captures, then fill the
        # rest with whichever recency wins after user is satisfied.
        picked: list[str] = user_keys[:budget]
        remaining = budget - len(picked)
        if remaining > 0:
            picked.extend(agent_keys[:remaining])

        # Final ordering for the prompt: newest-first within the picked
        # set (the prompt assembler reads them in order).
        picked = sorted(picked, reverse=True)

        insights = []
        for key in picked:
            try:
                content = self.s3.download(key)
                fm, body = Frontmatter.from_yaml_block(content)
                question = fm.title or ""
                body_preview = body[:600] if len(body) > 600 else body
                insights.append((question, body_preview))
            except Exception:
                continue

        if insights:
            n_user = sum(
                1 for k in picked
                if "/outputs/agent/" not in k and not k.startswith("outputs/agent/")
            )
            logger.info(
                f"Compounding: loaded {len(insights)} insights "
                f"(user={n_user}, agent={len(insights) - n_user})"
            )
        return insights

    def _compile_single(
        self, raw_key: str, output_insights: list[tuple[str, str]] | None = None
    ) -> CompileResult:
        """Compile a single raw file into a wiki page."""
        t_start = time.time()
        self._last_token_usage = (0, 0)  # (in, out) — set by _call_gemini

        try:
            # Resolve the destination before any LLM call or write, so a key
            # that would map onto itself fails here instead of overwriting.
            wiki_key = self._wiki_key_for(raw_key)

            # Download raw content
            raw_content = self.s3.download(raw_key)

            # Parse frontmatter
            fm, body = Frontmatter.from_yaml_block(raw_content)
            if not body.strip():
                return CompileResult(
                    source_key=raw_key,
                    wiki_key="",
                    title="",
                    content="",
                    success=False,
                    error="Empty content body",
                )

            # Compounding: filter output insights by relevance to this file
            relevant_insights = ""
            if output_insights:
                body_lower = body[:2000].lower()
                title_lower = (fm.title or "").lower()
                relevant = []
                for question, answer in output_insights:
                    q_lower = question.lower()
                    # Include only if question terms overlap with raw content
                    q_words = {w for w in re.findall(r"\w+", q_lower) if len(w) > 2}
                    if any(w in body_lower or w in title_lower for w in q_words):
                        relevant.append(f"Q: {question}\n{answer}")
                if relevant:
                    relevant_insights = "\n\n---\n\n".join(relevant[:5])

            # Build LLM prompt (with optional compounding + entity context).
            # Passing fm.entities here enforces consistent canonical wikilink
            # names — see schema.ENTITIES_ADDENDUM for the rationale.
            messages = build_compile_prompt(
                content=body,
                source=fm.source,
                source_type=fm.source_type,
                tags=fm.tags,
                output_insights=relevant_insights,
                entities=fm.entities,
                output_language=self.profile.output_language,
            )

            # Call LLM with fallback
            compiled_content = self._call_llm_with_fallback(messages)

            # Post-process: strip chain-of-thought / thinking artifacts
            compiled_content = _clean_llm_output(compiled_content)

            # Convert simplified Chinese → traditional in LLM output (zh-HK/zh-TW only)
            compiled_content = ensure_traditional_chinese(
                compiled_content, self.profile.output_language
            )

            # URL safety: expand shorteners, flag suspicious domains
            compiled_content = sanitize_wiki_urls(compiled_content)

            # Parse ---TAGS--- separator if present
            llm_tags = []
            if "---TAGS---" in compiled_content:
                parts = compiled_content.split("---TAGS---", 1)
                compiled_content = parts[0].strip()
                tag_block = parts[1].strip()
                llm_tags = _sanitize_tags(tag_block)

            # Extract wikilinks from compiled content
            wikilinks = extract_wikilinks(compiled_content)

            # Update frontmatter
            fm.compiled = True
            fm.compile_provider = self.provider.value
            fm.compile_model = self.model
            fm.compile_date = datetime.datetime.now().strftime("%Y-%m-%d")
            fm.raw_content_hash = content_hash(raw_content)
            # Stamp trust classification for downstream consumers. A
            # newly compiled wiki page is an article (single-source LLM
            # compile) — concept/topic pages get stamped from their own
            # build paths.
            from agent2kb.models import SourceKind as _SK
            fm.source_kind = _SK.ARTICLE
            # Merge LLM-generated tags with existing tags (deduplicated)
            existing = set(fm.tags or [])
            fm.tags = sorted(existing | set(llm_tags))

            # Add source URL line at top of content (visible in Obsidian)
            source_line = ""
            if fm.original_link:
                source_line = f"> Source: [{fm.original_link}]({fm.original_link})\n\n"
            elif fm.source:
                source_line = f"> Source: [{fm.source}]({fm.source})\n\n"

            # Combine frontmatter + source link + compiled content
            full_wiki = fm.to_yaml_block() + "\n" + source_line + compiled_content

            # Upload to wiki/
            self.s3.upload(wiki_key, full_wiki)
            # Best-effort manifest refresh — never break compile on
            # observability failures. ``update_entry`` no-ops if no
            # manifest exists yet (operator runs ``a2kb manifest
            # --rebuild`` once after a fresh install).
            try:
                from agent2kb import manifest as _mf
                _mf.update_entry(self.s3, self.profile, wiki_key, full_wiki)
            except Exception as _e:  # pragma: no cover — paranoia
                logger.debug(f"manifest update skipped: {_e}")
            duration = time.time() - t_start
            tok_in, tok_out = self._last_token_usage
            logger.info(
                f"Compiled: {raw_key} → {wiki_key} ({duration:.1f}s, {tok_in}+{tok_out} tokens)"
            )

            title = fm.title or raw_key.split("/")[-1].replace(".md", "")
            return CompileResult(
                source_key=raw_key,
                wiki_key=wiki_key,
                title=title,
                content=compiled_content,
                wikilinks=wikilinks,
                duration_sec=round(duration, 1),
                tokens_in=tok_in,
                tokens_out=tok_out,
            )

        except MissingExtraError:
            # Not a per-file failure: the configured provider cannot run at all.
            raise
        except Exception as e:
            logger.error(f"Compile failed for {raw_key}: {e}")
            if isinstance(e, ProviderExhaustedError):
                self._provider_down = True
            duration = time.time() - t_start
            return CompileResult(
                source_key=raw_key,
                wiki_key="",
                title="",
                content="",
                success=False,
                duration_sec=round(duration, 1),
                error=str(e),
            )

    def _call_llm_with_fallback(
        self, messages: list[dict], purpose: str = "compile", *, primary_only: bool = False
    ) -> str:
        """Call LLM with fallback chain on failure.

        Cloud fallback is OFF by default to prevent silent data leakage.
        If the primary provider is local (ollama/gemma4) and fails, we only
        fall back to other local providers unless allow_cloud_fallback=True.
        ``primary_only`` tries the configured provider alone (no chain at all).

        On total failure, the raised RuntimeError surfaces the PRIMARY
        provider's error prominently (since that's almost always the
        actionable root cause) along with a compact chain summary. Prior
        versions only reported the *last* provider's error, which hid the
        real cause behind the last-ditch provider (usually Ollama).
        """
        providers_to_try = [self.provider]
        primary_is_local = self.provider not in CLOUD_PROVIDERS

        # The CLI provider carries its own fallback (a second CLI, see
        # cli_runner.CliRouter). Appending the generic API chain after it
        # only added a doomed Ollama attempt on machines without Ollama and
        # masked "provider exhausted" behind an unrelated connection error.
        fallback_chain = (
            [] if primary_only or self.provider is LLMProvider.CLI else FALLBACK_CHAIN
        )
        for p in fallback_chain:
            if p == self.provider or p in providers_to_try:
                continue
            # Block cloud fallback unless explicitly opted in
            if primary_is_local and p in CLOUD_PROVIDERS and not self.allow_cloud_fallback:
                logger.debug(
                    f"Skipping cloud fallback {p.value} (use --allow-cloud-fallback to enable)"
                )
                continue
            providers_to_try.append(p)

        errors: list[tuple[LLMProvider, str]] = []
        # Track "hard-down" by exception type, not just by message text: a
        # message that has been summarised or combined may no longer carry
        # the keyword that would identify it.
        exhausted: list[bool] = []
        for provider in providers_to_try:
            try:
                model = (
                    self.model if provider == self.provider else DEFAULT_MODELS.get(provider, "")
                )
                result = self._call_provider(provider, messages, model, purpose)
                if result:
                    return result
            except MissingExtraError as e:
                if provider == self.provider:
                    # The configured provider cannot run: say how to fix it and
                    # stop, rather than walking the chain for every file.
                    raise
                logger.debug(f"Skipping fallback {provider.value}: {e}")
                continue
            except Exception as e:
                # Keep only the first line and cap length so the error
                # stays readable when we aggregate the full chain below.
                err_short = str(e).split("\n")[0][:300]
                errors.append((provider, err_short))
                exhausted.append(
                    isinstance(e, ProviderExhaustedError) or is_provider_exhausted(str(e))
                )
                logger.warning(f"Provider {provider.value} failed: {err_short}")
                continue

        if not errors:
            raise RuntimeError("No LLM providers were attempted")

        # Build a readable error that shows the PRIMARY cause first,
        # followed by a compact chain summary so the user can see what
        # we tried and in what order.
        primary = errors[0]
        chain_summary = " → ".join(f"{p.value}({e[:80]})" for p, e in errors)
        message = f"{primary[0].value} failed: {primary[1]}\n  Fallback chain: {chain_summary}"

        # If every provider we tried is hard-down (no credits, no quota, bad
        # key), retrying for each remaining file just burns wall clock. Say
        # so with a distinct type the run loops can act on.
        if all(exhausted):
            raise ProviderExhaustedError(message)
        raise RuntimeError(message)

    def _call_provider(
        self, provider: LLMProvider, messages: list[dict], model: str, purpose: str = "compile"
    ) -> str:
        """Call a specific LLM provider."""
        if provider == LLMProvider.GROQ:
            return self._call_groq(messages, model)
        elif provider == LLMProvider.OPENROUTER:
            return self._call_openrouter(messages, model)
        elif provider in (LLMProvider.OLLAMA, LLMProvider.GEMMA4):
            return self._call_ollama(messages, model)
        elif provider == LLMProvider.GEMINI:
            return self._call_gemini(messages, model)
        elif provider == LLMProvider.CLI:
            return self._call_cli(messages, purpose)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def _call_groq(self, messages: list[dict], model: str) -> str:
        """Call Groq API with rate limit handling."""
        Groq = import_optional("groq", "groq", "The Groq provider").Groq

        api_key = self.profile.groq_api_key
        if not api_key:
            raise ValueError("groq_api_key not set in profile config")

        if not hasattr(self, "_groq_client"):
            self._groq_client = Groq(api_key=api_key)
        max_retries = 3

        for attempt in range(max_retries):
            try:
                response = self._groq_client.chat.completions.create(
                    model=model or "llama-3.3-70b-versatile",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=4096,
                )
                if not response.choices:
                    raise ValueError("Groq returned empty response")
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Groq returned empty content")
                return content
            except Exception as e:
                if "rate_limit" in str(e).lower() and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Groq rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

    def _call_openrouter(self, messages: list[dict], model: str) -> str:
        """Call OpenRouter API."""
        OpenAI = import_optional("openai", "openai", "The OpenRouter provider").OpenAI

        api_key = self.profile.openrouter_api_key
        if not api_key:
            raise ValueError("openrouter_api_key not set in profile config")

        if not hasattr(self, "_openrouter_client"):
            self._openrouter_client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=api_key,
            )
        response = self._openrouter_client.chat.completions.create(
            model=model or "google/gemma-3-27b-it",
            messages=messages,
            temperature=0.3,
            max_tokens=4096,
        )
        if not response.choices:
            raise ValueError("OpenRouter returned empty response")
        return response.choices[0].message.content

    def _call_ollama(self, messages: list[dict], model: str) -> str:
        """Call local Ollama instance."""
        ollama_sdk = import_optional("ollama", "ollama", "The Ollama provider")

        if not hasattr(self, "_ollama_client"):
            self._ollama_client = ollama_sdk.Client(host=self.profile.ollama_host)
        response = self._ollama_client.chat(
            model=model or "llama3.3:70b",
            messages=messages,
            options={"temperature": 0.3},
        )
        return response["message"]["content"]

    def _call_gemini(self, messages: list[dict], model: str) -> str:
        """Call Google Gemini / Gemma API via the google.genai SDK.

        Supports all models available on AI Studio including Gemma 4.

        Tier characteristics (verify Google's current docs — these change):
          - **AI Studio free tier**: 15 RPM chat / 100 RPM embed, 1000 RPD.
            Google's current policy DOES use free-tier prompts and
            responses to improve their products and models, including
            for training. If your KB has any sensitive content, do NOT
            stay on the free tier — use Vertex AI / paid Gemini API,
            or switch ``compile_provider`` to a local provider (Ollama).
          - **Paid Gemini API / Vertex AI**: data NOT used for training,
            higher rate limits, ~$0.075/1M input tokens (Flash). For a
            personal KB this typically runs <$1/month.

        Uses the new ``google.genai`` package; the legacy
        ``google.generativeai`` SDK was sunsetted in 2026-Q1 and won't
        receive further updates or new model support.
        """
        genai = import_optional("google.genai", "gemini", "The Gemini provider")
        types = import_optional("google.genai.types", "gemini", "The Gemini provider")

        api_key = self.profile.gemini_api_key
        if not api_key:
            raise ValueError("gemini_api_key not set in profile config")

        # Cache the client so repeated compile calls in one process
        # don't redo the auth handshake.
        if not hasattr(self, "_gemini_client"):
            self._gemini_client = genai.Client(api_key=api_key)

        # Combine system + user/assistant messages into the Gemini shape.
        # New SDK is more forgiving about contents being a single string
        # — multi-turn chat would use the chats API instead.
        system_instruction = None
        contents = []
        for msg in messages:
            if msg["role"] == "system":
                system_instruction = msg["content"]
            elif msg["role"] in ("user", "assistant"):
                contents.append(msg["content"])

        cfg = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.3,
            max_output_tokens=8192,
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self._gemini_client.models.generate_content(
                    model=model or "gemini-3.1-flash-lite-preview",
                    contents="\n\n".join(contents),
                    config=cfg,
                )
                if not response.text:
                    raise ValueError("Gemini returned empty response")
                # Capture token usage for monitoring.
                if response.usage_metadata:
                    um = response.usage_metadata
                    self._last_token_usage = (
                        um.prompt_token_count or 0,
                        um.candidates_token_count or 0,
                    )
                return response.text
            except Exception as e:
                err = str(e).lower()
                if ("rate" in err or "quota" in err or "429" in err) and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Gemini rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

    def _cli_router(self, purpose: str) -> CliRouter:
        if purpose not in self._cli_routers:
            self._cli_routers[purpose] = router_for(self.profile, purpose)
        return self._cli_routers[purpose]

    def _call_cli(self, messages: list[dict], purpose: str = "compile") -> str:
        """Call the profile's CLI (claude or codex) for ``purpose``.

        System and user messages stay separate: Claude Code takes the system
        prompt as its own (replacing its agent prompt), so the instructions
        keep system-level weight instead of being pasted into user text.
        Isolation, fallback and error classification live in cli_runner.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        result = self._cli_router(purpose).run(system, user)
        self._last_token_usage = (result.input_tokens, result.output_tokens)
        return result.text
