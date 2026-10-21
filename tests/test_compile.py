"""Tests for compile system."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb.compile.compiler import DEFAULT_MODELS, FALLBACK_CHAIN, Compiler
from agent2kb.compile.schema import WIKI_COMPILE_SYSTEM_PROMPT, build_compile_prompt
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client


@pytest.fixture
def profile():
    return ProfileConfig(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
        compile_provider=LLMProvider.OLLAMA,
        ollama_host="http://localhost:11434",
    )


@pytest.fixture
def mock_s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


class TestSchemaPrompt:
    def test_system_prompt_exists(self):
        assert "knowledge compiler" in WIKI_COMPILE_SYSTEM_PROMPT.lower()
        assert "wikilinks" in WIKI_COMPILE_SYSTEM_PROMPT.lower()

    def test_build_compile_prompt(self):
        messages = build_compile_prompt(
            content="Some content about AI",
            source="http://example.com",
            source_type="url",
            tags=["ai"],
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "Some content about AI" in messages[1]["content"]


class TestCompiler:
    def test_compile_single_with_mock_llm(self, profile, mock_s3):
        # Upload a raw file
        fm = Frontmatter(
            title="Test Article",
            source="http://example.com",
            source_type="url",
            tags=["test"],
        )
        raw_content = fm.to_yaml_block() + "\nThis is raw content about AI and machine learning."
        mock_s3.upload("test/raw/test-article.md", raw_content)

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)

        # Mock the LLM call
        mock_response = (
            "# Test Article\n\nThis is about [[AI]] and "
            "[[Machine Learning]].\n\n## Summary\n\nGreat stuff."
        )
        with patch.object(compiler, "_call_llm_with_fallback", return_value=mock_response):
            results = compiler.compile_all(new_only=False)

        assert len(results) == 1
        assert results[0].success
        assert results[0].wiki_key == "test/wiki/test-article.md"
        assert "AI" in results[0].wikilinks

        # Verify wiki file was uploaded
        wiki_content = mock_s3.download("test/wiki/test-article.md")
        assert "compiled: true" in wiki_content
        assert "Machine Learning" in wiki_content

    def test_incremental_compile_skips_when_hash_matches(self, profile, mock_s3):
        """If wiki has matching raw_content_hash, skip compilation.

        The LLM may still be called for the weekly summary in _update_index,
        but _compile_single must not be called (no compile work). We verify
        by checking results is empty, not by asserting zero LLM calls.
        """
        from agent2kb.utils import content_hash

        raw_content = Frontmatter(title="Existing", source="test").to_yaml_block() + "\nContent"
        raw_hash = content_hash(raw_content)

        # Upload raw
        mock_s3.upload("test/raw/existing.md", raw_content)
        # Upload wiki with matching hash
        wiki_fm = Frontmatter(
            title="Existing",
            source="test",
            compiled=True,
            raw_content_hash=raw_hash,
        )
        mock_s3.upload("test/wiki/existing.md", wiki_fm.to_yaml_block() + "\nCompiled")

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        with patch.object(compiler, "_compile_single") as mock_compile:
            with patch.object(compiler, "_call_llm_with_fallback", return_value="Summary."):
                results = compiler.compile_all(new_only=True)

        mock_compile.assert_not_called()
        assert len(results) == 0

    def test_fallback_chain_order(self):
        assert LLMProvider.GROQ in FALLBACK_CHAIN
        assert LLMProvider.OLLAMA in FALLBACK_CHAIN


class TestCompoundingActorWeighting:
    """``_load_output_insights`` prefers user captures over agent captures.

    Without this guard, a busy ingest day fills the 15-slot window with
    auto-enrichment fan-outs and pushes the user's actual questions out
    of the compounding loop. The fix (2026-05-14) reserves the window
    for user actors first, then tops up with agent actors.
    """

    def test_user_outputs_fill_first_slots(self, profile, mock_s3):
        # 20 user captures + 20 agent captures; budget is 15 → window
        # should be all-user.
        for i in range(20):
            stamp = f"20260513-{i:06d}"
            fm = Frontmatter(title=f"user q{i}", source="kb query")
            mock_s3.upload(
                f"test/outputs/{stamp}-q.md",
                fm.to_yaml_block() + f"\nuser body {i}\n",
            )
        for i in range(20):
            stamp = f"20260514-{i:06d}"
            fm = Frontmatter(title=f"agent q{i}", source="kb query")
            mock_s3.upload(
                f"test/outputs/agent/{stamp}-q.md",
                fm.to_yaml_block() + f"\nagent body {i}\n",
            )

        compiler = Compiler(profile=profile, s3=mock_s3)
        insights = compiler._load_output_insights()
        assert len(insights) == 15
        for question, _body in insights:
            assert question.startswith("user q"), (
                "User captures should fill the window before any agent capture"
            )

    def test_agent_outputs_used_when_user_window_not_full(
        self, profile, mock_s3
    ):
        # Only 5 user captures; the remaining 10 slots fill from agent.
        for i in range(5):
            stamp = f"20260513-{i:06d}"
            fm = Frontmatter(title=f"user q{i}", source="kb query")
            mock_s3.upload(
                f"test/outputs/{stamp}-q.md",
                fm.to_yaml_block() + f"\nuser body {i}\n",
            )
        for i in range(20):
            stamp = f"20260514-{i:06d}"
            fm = Frontmatter(title=f"agent q{i}", source="kb query")
            mock_s3.upload(
                f"test/outputs/agent/{stamp}-q.md",
                fm.to_yaml_block() + f"\nagent body {i}\n",
            )

        compiler = Compiler(profile=profile, s3=mock_s3)
        insights = compiler._load_output_insights()
        assert len(insights) == 15
        n_user = sum(1 for q, _ in insights if q.startswith("user"))
        n_agent = sum(1 for q, _ in insights if q.startswith("agent"))
        assert n_user == 5
        assert n_agent == 10

    def test_default_models_defined(self):
        for provider in LLMProvider:
            assert provider in DEFAULT_MODELS


class TestConceptRefreshPolicy:
    """Unit tests for Compiler._concept_needs_refresh policy helper."""

    def test_no_refresh_when_sources_unchanged(self):
        meta = {"source_pages": ["article-a", "article-b"]}
        current = ["article-a", "article-b"]
        assert Compiler._concept_needs_refresh(meta, current) is False

    def test_refresh_when_new_source_appears(self):
        meta = {"source_pages": ["article-a", "article-b"]}
        current = ["article-a", "article-b", "article-c"]
        assert Compiler._concept_needs_refresh(meta, current) is True

    def test_refresh_when_source_removed(self):
        meta = {"source_pages": ["article-a", "article-b", "article-c"]}
        current = ["article-a", "article-b"]
        assert Compiler._concept_needs_refresh(meta, current) is True

    def test_refresh_when_source_order_same_but_set_same(self):
        """Order must not affect the decision — we compare as sets."""
        meta = {"source_pages": ["b", "a"]}
        current = ["a", "b"]
        assert Compiler._concept_needs_refresh(meta, current) is False

    def test_no_refresh_when_legacy_page_missing_source_pages(self):
        """Legacy pages WITHOUT source_pages must NOT trigger LLM refresh.

        Rationale: the metadata-only backfill path
        (Compiler._migrate_concept_metadata) handles schema migration
        before _concept_needs_refresh is ever called on a legacy page.
        Returning True here would burn thousands of tokens on bodies that
        are already correctly synthesized.
        """
        meta = {"title": "Legacy"}  # no source_pages field
        current = ["article-a"]
        assert Compiler._concept_needs_refresh(meta, current) is False

    def test_no_refresh_when_source_pages_malformed(self):
        """Malformed source_pages (e.g. dict instead of list) must not rebuild."""
        meta = {"source_pages": {"not": "a list"}}
        current = ["article-a"]
        assert Compiler._concept_needs_refresh(meta, current) is False


class TestConceptMetadataMigration:
    """Integration tests for zero-LLM legacy concept schema migration."""

    def test_migrate_preserves_body_verbatim(self, profile, mock_s3):
        """Legacy concept page must keep its synthesized body after migration."""
        legacy_content = (
            "---\n"
            'title: "LLM"\n'
            'date: "2026-04-09"\n'
            "tags: [concept]\n"
            "type: concept\n"
            "auto_generated: true\n"
            "references: 5\n"
            "---\n\n"
            "**LLM**\n\n"
            "**Definition**\n"
            "LLM is a large language model. This body must be preserved.\n\n"
            "## Related Articles\n"
            "- [[article-a]]\n"
        )
        concept_key = "test/wiki/concepts/llm.md"
        mock_s3.upload(concept_key, legacy_content)

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        existing_meta = compiler._read_concept_meta(concept_key)
        assert "source_pages" not in existing_meta

        compiler._migrate_concept_metadata(
            concept_key=concept_key,
            existing_meta=existing_meta,
            current_sources=["article-a", "article-b"],
            ref_count=2,
            today="2026-04-11",
        )

        migrated = mock_s3.download(concept_key)
        # Body preserved exactly
        assert "**Definition**" in migrated
        assert "LLM is a large language model. This body must be preserved." in migrated
        assert "- [[article-a]]" in migrated
        # Schema migrated
        assert "source_pages:" in migrated
        assert "- article-a" in migrated
        assert "- article-b" in migrated
        assert 'last_refreshed: "2026-04-11"' in migrated
        # Original creation date preserved
        assert 'date: "2026-04-09"' in migrated

    def test_migrate_is_idempotent(self, profile, mock_s3):
        """Running migration twice produces the same result as running it once."""
        legacy_content = (
            "---\n"
            'title: "RAG"\n'
            'date: "2026-04-08"\n'
            "tags: [concept]\n"
            "---\n\n"
            "RAG body.\n"
        )
        concept_key = "test/wiki/concepts/rag.md"
        mock_s3.upload(concept_key, legacy_content)

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)

        # First migration
        meta1 = compiler._read_concept_meta(concept_key)
        compiler._migrate_concept_metadata(
            concept_key, meta1, ["article-a"], 1, "2026-04-11"
        )
        first_pass = mock_s3.download(concept_key)

        # Second migration with same sources — should be no-op-ish (identical output)
        meta2 = compiler._read_concept_meta(concept_key)
        # Now source_pages is present, so _concept_needs_refresh would return False;
        # the compile loop would skip migration entirely. But if called directly
        # it should still produce the same bytes.
        compiler._migrate_concept_metadata(
            concept_key, meta2, ["article-a"], 1, "2026-04-11"
        )
        second_pass = mock_s3.download(concept_key)

        assert first_pass == second_pass
        assert "RAG body." in second_pass


class TestCleanWikilinkTarget:
    """Regression for 0.15.1 concept-synth slug doubling bug.

    Background: ``extract_wikilinks`` returns the full inside of
    ``[[target|display#anchor]]`` (just stripping the brackets). Concept
    synthesis used to feed that raw string straight into slugify, which
    stripped the ``|`` character without inserting a separator —
    gluing the alias onto the target. ``[[redis-cluster|Redis Cluster]]``
    became the concept ``"redis-cluster|Redis Cluster"`` → slugified to
    ``"redis-clusterredis-cluster"`` and a broken concept page got
    auto-created at ``wiki/concepts/redis-clusterredis-cluster.md``.
    """

    def test_plain_target(self):
        from agent2kb.compile.compiler import _clean_wikilink_target
        assert _clean_wikilink_target("redis-cluster") == "redis-cluster"

    def test_strip_alias(self):
        from agent2kb.compile.compiler import _clean_wikilink_target
        assert _clean_wikilink_target("redis-cluster|Redis Cluster") == "redis-cluster"

    def test_strip_anchor(self):
        from agent2kb.compile.compiler import _clean_wikilink_target
        assert (
            _clean_wikilink_target("redis-cluster#setup-section")
            == "redis-cluster"
        )

    def test_strip_anchor_and_alias(self):
        from agent2kb.compile.compiler import _clean_wikilink_target
        assert (
            _clean_wikilink_target("redis-cluster#setup|Redis Setup")
            == "redis-cluster"
        )

    def test_whitespace_trimmed(self):
        from agent2kb.compile.compiler import _clean_wikilink_target
        assert _clean_wikilink_target("  redis-cluster  |  display  ") == "redis-cluster"

    def test_long_filename_target_with_alias(self):
        """The case that produced the doubled longer slugs in a real project:
        a wiki article filename used as the target, then an alias.
        """
        from agent2kb.compile.compiler import _clean_wikilink_target
        raw = (
            "20260418-Redis-Cluster-PostgreSQL-how-i-built-self-learning-...-4d92f0fe"
            "|Redis Cluster + PostgreSQL - how i built self-learning ... guide"
        )
        assert (
            _clean_wikilink_target(raw)
            == "20260418-Redis-Cluster-PostgreSQL-how-i-built-self-learning-...-4d92f0fe"
        )


class TestPendingSelection:
    """`_select_pending` replaces a per-file HEAD + two GETs with one
    listing plus a parallel fetch of only the files that need comparing.
    Behaviour must stay identical to the old per-file check."""

    def _raw(self, title="T"):
        return Frontmatter(title=title, source="test").to_yaml_block() + "\nContent"

    def test_missing_wiki_page_is_pending(self, profile, mock_s3):
        mock_s3.upload("test/raw/new.md", self._raw())
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)

        assert compiler._select_pending(["test/raw/new.md"]) == ["test/raw/new.md"]

    def test_matching_hash_is_skipped(self, profile, mock_s3):
        from agent2kb.utils import content_hash

        raw = self._raw()
        mock_s3.upload("test/raw/a.md", raw)
        wiki_fm = Frontmatter(
            title="T", source="test", compiled=True, raw_content_hash=content_hash(raw)
        )
        mock_s3.upload("test/wiki/a.md", wiki_fm.to_yaml_block() + "\nCompiled")

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        assert compiler._select_pending(["test/raw/a.md"]) == []

    def test_changed_raw_is_pending(self, profile, mock_s3):
        mock_s3.upload("test/raw/a.md", self._raw("changed"))
        wiki_fm = Frontmatter(
            title="T", source="test", compiled=True, raw_content_hash="staleHash"
        )
        mock_s3.upload("test/wiki/a.md", wiki_fm.to_yaml_block() + "\nCompiled")

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        assert compiler._select_pending(["test/raw/a.md"]) == ["test/raw/a.md"]

    def test_unparseable_wiki_is_recompiled(self, profile, mock_s3):
        mock_s3.upload("test/raw/a.md", self._raw())
        mock_s3.upload("test/wiki/a.md", "not frontmatter at all")

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        assert compiler._select_pending(["test/raw/a.md"]) == ["test/raw/a.md"]

    def test_mixed_batch_partitions_correctly(self, profile, mock_s3):
        from agent2kb.utils import content_hash

        fresh = self._raw("fresh")
        mock_s3.upload("test/raw/fresh.md", fresh)
        mock_s3.upload(
            "test/wiki/fresh.md",
            Frontmatter(
                title="fresh",
                source="test",
                compiled=True,
                raw_content_hash=content_hash(fresh),
            ).to_yaml_block()
            + "\nCompiled",
        )
        mock_s3.upload("test/raw/brand-new.md", self._raw("brand-new"))

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        pending = compiler._select_pending(["test/raw/fresh.md", "test/raw/brand-new.md"])

        assert pending == ["test/raw/brand-new.md"]


class TestConceptCreationCap:
    """Concept creation is capped per run.

    Ingesting a couple of feeds can push dozens of wikilinks past the
    reference threshold at once. Each new concept page is a separate
    sequential LLM call, so an uncapped run had no upper bound on wall
    clock. Deferred concepts stay above threshold and are created next run.
    """

    def test_creations_are_capped(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        cap = compiler.MAX_CONCEPT_CREATIONS_PER_RUN

        # Enough source pages that many distinct concepts cross threshold 2.
        n_concepts = cap + 8
        for page in range(2):
            links = " ".join(f"[[Concept {i}]]" for i in range(n_concepts))
            fm = Frontmatter(title=f"Page {page}", source="test", compiled=True)
            mock_s3.upload(f"test/wiki/page{page}.md", fm.to_yaml_block() + f"\n{links}\n")

        with patch.object(compiler, "_call_llm_with_fallback", return_value="Body."):
            compiler._synthesize_concepts()

        made = mock_s3.list_keys(sub_prefix="wiki/concepts/", suffix=".md")
        assert len(made) == cap

    def test_second_run_picks_up_deferred_concepts(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        cap = compiler.MAX_CONCEPT_CREATIONS_PER_RUN
        n_concepts = cap + 3

        for page in range(2):
            links = " ".join(f"[[Concept {i}]]" for i in range(n_concepts))
            fm = Frontmatter(title=f"Page {page}", source="test", compiled=True)
            mock_s3.upload(f"test/wiki/page{page}.md", fm.to_yaml_block() + f"\n{links}\n")

        with patch.object(compiler, "_call_llm_with_fallback", return_value="Body."):
            compiler._synthesize_concepts()
            first = len(mock_s3.list_keys(sub_prefix="wiki/concepts/", suffix=".md"))
            compiler._synthesize_concepts()
            second = len(mock_s3.list_keys(sub_prefix="wiki/concepts/", suffix=".md"))

        assert first == cap
        assert second == n_concepts  # backlog cleared on the next run


class TestProviderCircuitBreaker:
    """A provider that is out of credits, over quota, or unauthorized will
    fail every subsequent call. Without a breaker, compile still attempts
    one full call per remaining file and concept — turning a run that can
    accomplish nothing into an hour of wall clock.
    """

    def test_detects_non_retryable_errors(self):
        from agent2kb.compile.compiler import is_provider_exhausted

        assert is_provider_exhausted("ERROR: Your workspace is out of credits.")
        assert is_provider_exhausted("You hit your usage limit. Upgrade to Pro")
        assert is_provider_exhausted("401 Unauthorized")
        assert is_provider_exhausted("The 'gpt-5.4' model is not supported when using Codex")

    def test_transient_errors_are_not_treated_as_fatal(self):
        from agent2kb.compile.compiler import is_provider_exhausted

        assert not is_provider_exhausted("Read timed out")
        assert not is_provider_exhausted("429 Too Many Requests")  # retry/backoff handles this
        assert not is_provider_exhausted("")

    def test_batch_stops_after_fatal_error(self, profile, mock_s3):
        from agent2kb.models import CompileResult

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        attempted = []

        def fake_compile(raw_key, _insights):
            attempted.append(raw_key)
            return CompileResult(
                source_key=raw_key,
                wiki_key="",
                title="",
                content="",
                success=False,
                duration_sec=0.1,
                error="ERROR: Your workspace is out of credits.",
            )

        with patch.object(compiler, "_compile_single", side_effect=fake_compile):
            results = compiler._compile_batch(["a.md", "b.md", "c.md"], [])

        assert attempted == ["a.md"]  # stopped after the first fatal error
        assert len(results) == 1
        assert compiler._provider_down is True

    def test_batch_continues_past_ordinary_failure(self, profile, mock_s3):
        from agent2kb.models import CompileResult

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        attempted = []

        def fake_compile(raw_key, _insights):
            attempted.append(raw_key)
            return CompileResult(
                source_key=raw_key,
                wiki_key="",
                title="",
                content="",
                success=False,
                duration_sec=0.1,
                error="Read timed out",
            )

        with patch.object(compiler, "_compile_single", side_effect=fake_compile):
            compiler._compile_batch(["a.md", "b.md", "c.md"], [])

        assert attempted == ["a.md", "b.md", "c.md"]
        assert compiler._provider_down is False


class TestCliPurposeRouting:
    """Concept pages, summaries and lint go to the aux model; compile to the
    compile model. The CLI provider carries its own fallback CLI, so the
    generic API chain (Ollama etc.) must not be appended after it."""

    def test_concept_synthesis_uses_aux_purpose(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        for page in range(2):
            fm = Frontmatter(title=f"P{page}", source="test", compiled=True)
            mock_s3.upload(f"test/wiki/p{page}.md", fm.to_yaml_block() + "\n[[Shared Idea]]\n")

        purposes = []

        def fake(messages, purpose="compile"):
            purposes.append(purpose)
            return "Body."

        with patch.object(compiler, "_call_llm_with_fallback", side_effect=fake):
            compiler._synthesize_concepts()

        assert purposes and set(purposes) == {"aux"}

    def test_cli_provider_does_not_fall_through_to_api_chain(self, profile, mock_s3):
        from agent2kb.compile.compiler import ProviderExhaustedError

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.CLI)
        tried = []

        def fake(provider, messages, model, purpose="compile"):
            tried.append(provider)
            raise ProviderExhaustedError("claude down | fallback codex down")

        with patch.object(compiler, "_call_provider", side_effect=fake):
            with pytest.raises(ProviderExhaustedError):
                compiler._call_llm_with_fallback([{"role": "user", "content": "x"}])

        assert tried == [LLMProvider.CLI]

    def test_cli_purpose_reaches_router(self, profile, mock_s3):
        from agent2kb.cli_runner import CliResult, CliTarget

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.CLI)
        seen = []

        class FakeRouter:
            def __init__(self, purpose):
                self.purpose = purpose

            def run(self, system, user):
                seen.append((self.purpose, system, user))
                return CliResult("ok", CliTarget("claude", "m", "high"), 5, 7)

        with patch("agent2kb.compile.compiler.router_for",
                   side_effect=lambda prof, purpose: FakeRouter(purpose)):
            out = compiler._call_llm_with_fallback(
                [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}],
                purpose="aux",
            )

        assert out == "ok"
        assert seen == [("aux", "SYS", "USR")]
        assert compiler._last_token_usage == (5, 7)

    def test_batch_stops_on_exhausted_type_without_keywords(self, profile, mock_s3):
        """A combined fallback message may lack the marker keywords; the
        exception type alone must still stop the run."""
        from agent2kb.compile.compiler import ProviderExhaustedError

        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        for name in ("a", "b"):
            raw = Frontmatter(title=name, source="t").to_yaml_block() + "\nx"
            mock_s3.upload(f"test/raw/{name}.md", raw)

        calls = []

        def fake(messages, purpose="compile"):
            calls.append(1)
            raise ProviderExhaustedError("claude down | fallback codex down")

        with patch.object(compiler, "_call_llm_with_fallback", side_effect=fake):
            results = compiler._compile_batch(["test/raw/a.md", "test/raw/b.md"], [])

        assert len(calls) == 1
        assert len(results) == 1
        assert compiler._provider_down is True


# ── Whole-bucket (empty prefix) admin KB: destinations and quarantine ──


@pytest.fixture
def admin_profile():
    return ProfileConfig(
        name="admin",
        role="admin",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="",
        compile_provider=LLMProvider.OLLAMA,
        ollama_host="http://localhost:11434",
    )


@pytest.fixture
def admin_s3(admin_profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(admin_profile)
        client._client = conn
        yield client


def _raw(body="Raw body about alpha.", failures=None):
    meta = "compile_failures: %d\n" % failures if failures is not None else ""
    return f"---\ntitle: Sample\nsource: https://example.com/a\n{meta}---\n\n{body}\n"


class TestKeyDerivation:
    @pytest.mark.parametrize(
        "prefix,raw,wiki,failed",
        [
            ("", "raw/a.md", "wiki/a.md", "raw/.failed/a.md"),
            ("", "raw/sub/a.md", "wiki/sub/a.md", "raw/.failed/sub/a.md"),
            ("kbs/notes/", "kbs/notes/raw/a.md", "kbs/notes/wiki/a.md",
             "kbs/notes/raw/.failed/a.md"),
        ],
    )
    def test_relative_to_prefix(self, prefix, raw, wiki, failed):
        from agent2kb.s3_client import quarantine_key_for_raw, wiki_key_for_raw

        assert wiki_key_for_raw(prefix, raw) == wiki
        assert quarantine_key_for_raw(prefix, raw) == failed

    @pytest.mark.parametrize(
        "prefix,key",
        [("", "wiki/a.md"), ("", "raw/.failed/a.md"), ("kbs/notes/", "kbs/other/raw/a.md"),
         ("", "raw/"), ("", "raw//a.md")],
    )
    def test_rejects_keys_outside_raw(self, prefix, key):
        from agent2kb.s3_client import quarantine_key_for_raw, wiki_key_for_raw

        with pytest.raises(ValueError):
            wiki_key_for_raw(prefix, key)
        with pytest.raises(ValueError):
            quarantine_key_for_raw(prefix, key)


class TestWholeBucketCompile:
    def test_compile_writes_wiki_and_keeps_source(self, admin_profile, admin_s3):
        admin_s3.upload("raw/sample.md", _raw())
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        with patch.object(compiler, "_call_llm_with_fallback", return_value="# Sample\n\nok"):
            result = compiler._compile_single("raw/sample.md")
        assert result.success
        assert result.wiki_key == "wiki/sample.md"
        assert admin_s3.download("raw/sample.md") == _raw()
        assert admin_s3.exists("wiki/sample.md")

    def test_up_to_date_source_is_not_pending(self, admin_profile, admin_s3):
        admin_s3.upload("raw/sample.md", _raw())
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        with patch.object(compiler, "_call_llm_with_fallback", return_value="# Sample\n\nok"):
            compiler._compile_single("raw/sample.md")
        assert compiler._select_pending(["raw/sample.md"]) == []

    def test_compile_refuses_key_outside_raw(self, admin_profile, admin_s3):
        admin_s3.upload("wiki/page.md", _raw())
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        with patch.object(compiler, "_call_llm_with_fallback") as llm:
            result = compiler._compile_single("wiki/page.md")
        assert not result.success
        llm.assert_not_called()
        assert admin_s3.download("wiki/page.md") == _raw()

    def test_quarantine_moves_source_to_failed(self, admin_profile, admin_s3):
        from agent2kb.models import CompileResult

        admin_s3.upload("raw/sample.md", _raw(failures=2))
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        failed = CompileResult(source_key="raw/sample.md", wiki_key="", title="",
                               content="", success=False, error="bad output")
        out = compiler._handle_compile_failures([failed])
        assert not admin_s3.exists("raw/sample.md")
        assert admin_s3.download("raw/.failed/sample.md") == _raw(failures=2)
        # Still reported, flagged as quarantined.
        assert len(out) == 1 and out[0].quarantined and not out[0].success
        # And the quarantine folder is not picked up again.
        assert admin_s3.list_raw_files() == []

    def test_quarantine_keeps_source_when_copy_fails(self, admin_profile, admin_s3):
        from agent2kb.models import CompileResult

        admin_s3.upload("raw/sample.md", _raw(failures=2))
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        failed = CompileResult(source_key="raw/sample.md", wiki_key="", title="",
                               content="", success=False, error="bad output")
        real_upload = admin_s3.upload

        def flaky_upload(key, body, **kw):
            if key.startswith("raw/.failed/"):
                raise RuntimeError("storage unavailable")
            return real_upload(key, body, **kw)

        with patch.object(admin_s3, "upload", side_effect=flaky_upload):
            out = compiler._handle_compile_failures([failed])
        assert admin_s3.download("raw/sample.md") == _raw(failures=2)
        assert len(out) == 1 and not out[0].quarantined

    def test_quarantine_keeps_source_when_copy_differs(self, admin_profile, admin_s3):
        from agent2kb.models import CompileResult

        admin_s3.upload("raw/sample.md", _raw(failures=2))
        compiler = Compiler(profile=admin_profile, s3=admin_s3, provider=LLMProvider.OLLAMA)
        failed = CompileResult(source_key="raw/sample.md", wiki_key="", title="",
                               content="", success=False, error="bad output")
        real_download = admin_s3.download

        def corrupt(key):
            return "" if key.startswith("raw/.failed/") else real_download(key)

        with patch.object(admin_s3, "download", side_effect=corrupt):
            compiler._handle_compile_failures([failed])
        assert admin_s3.download("raw/sample.md") == _raw(failures=2)

    def test_prefixed_kb_list_raw_skips_quarantine(self, profile, mock_s3):
        mock_s3.upload("test/raw/a.md", _raw())
        mock_s3.upload("test/raw/.failed/b.md", _raw())
        assert mock_s3.list_raw_files() == ["test/raw/a.md"]


# ── No-op compile still finishes pending maintenance (F09) ──


class TestNoOpMaintenance:
    """A compile with nothing to compile must still run deferred or failed
    concept synthesis and a failed or stale embedding refresh, and must stay
    a cheap no-op when nothing is pending."""

    def _up_to_date(self, s3):
        from agent2kb.utils import content_hash

        raw = Frontmatter(title="T", source="test").to_yaml_block() + "\nContent"
        s3.upload("test/raw/a.md", raw)
        wiki_fm = Frontmatter(title="T", source="test", compiled=True,
                              raw_content_hash=content_hash(raw))
        s3.upload("test/wiki/a.md", wiki_fm.to_yaml_block() + "\nCompiled")

    def _state(self, s3, profile):
        from agent2kb.ops import read_maintenance_state

        return read_maintenance_state(s3, profile)

    def _run(self, profile, s3, synth="done", embed_error=None, embed_failures=0):
        compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)

        def fake_embed(force=False):
            if embed_error:
                raise RuntimeError(embed_error)
            compiler._embed_fingerprint = compiler._wiki_fingerprint(
                s3.list_objects(sub_prefix="wiki/", suffix=".md")
            )
            compiler._embed_failures = embed_failures
            return 0, 0

        with patch.object(compiler, "_synthesize_concepts", return_value=synth) as syn, \
                patch.object(compiler, "update_embeddings", side_effect=fake_embed) as emb:
            assert compiler.compile_all(new_only=True) == []
        return syn, emb

    def test_clean_state_is_a_no_op(self, profile, mock_s3):
        self._up_to_date(mock_s3)
        self._run(profile, mock_s3)  # first tick records the embedding fingerprint
        syn, emb = self._run(profile, mock_s3)
        syn.assert_not_called()
        emb.assert_not_called()

    def test_missing_state_keeps_old_concept_behaviour(self, profile, mock_s3):
        self._up_to_date(mock_s3)
        syn, emb = self._run(profile, mock_s3)
        syn.assert_not_called()  # nothing recorded as pending
        emb.assert_called_once()  # no fingerprint yet: one incremental refresh

    def test_deferred_concepts_run_on_next_no_op_tick(self, profile, mock_s3):
        from agent2kb.ops import write_maintenance_state

        self._up_to_date(mock_s3)
        self._run(profile, mock_s3)
        write_maintenance_state(mock_s3, profile, {
            **self._state(mock_s3, profile),
            "concepts": {"pending": True, "attempts": 0, "retry_after": 0},
        })
        syn, _ = self._run(profile, mock_s3, synth="deferred")
        syn.assert_called_once()
        assert self._state(mock_s3, profile)["concepts"]["pending"] is True
        syn, _ = self._run(profile, mock_s3, synth="done")
        syn.assert_called_once()
        assert self._state(mock_s3, profile)["concepts"]["pending"] is False
        syn, _ = self._run(profile, mock_s3)
        syn.assert_not_called()

    def test_failed_embedding_refresh_is_retried_with_backoff(self, profile, mock_s3):
        self._up_to_date(mock_s3)
        _, emb = self._run(profile, mock_s3, embed_error="provider down")
        emb.assert_called_once()
        entry = self._state(mock_s3, profile)["embeddings"]
        assert entry["pending"] is True and entry["attempts"] == 1

        # Inside the backoff window the no-op tick stays cheap.
        _, emb = self._run(profile, mock_s3)
        emb.assert_not_called()

        # Once it has passed, the refresh runs again and clears the flag.
        later = entry["retry_after"] + 1
        with patch("agent2kb.compile.compiler.time.time", return_value=later):
            _, emb = self._run(profile, mock_s3)
        emb.assert_called_once()
        assert self._state(mock_s3, profile)["embeddings"]["pending"] is False

    def test_backoff_doubles_and_is_capped(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        compiler._maint_state = {}
        with patch("agent2kb.compile.compiler.time.time", return_value=1000.0):
            delays = []
            for _ in range(10):
                compiler._mark_maintenance("embeddings", "failed")
                delays.append(compiler._maint_state["embeddings"]["retry_after"] - 1000.0)
        base = compiler.MAINTENANCE_RETRY_BASE_SEC
        assert delays[:3] == [base, 2 * base, 4 * base]
        assert max(delays) == compiler.MAINTENANCE_RETRY_MAX_SEC

    def test_per_page_embedding_failures_keep_it_pending(self, profile, mock_s3):
        self._up_to_date(mock_s3)
        self._run(profile, mock_s3, embed_failures=2)
        assert self._state(mock_s3, profile)["embeddings"]["pending"] is True

    def test_hand_edited_wiki_page_triggers_refresh(self, profile, mock_s3):
        self._up_to_date(mock_s3)
        self._run(profile, mock_s3)
        mock_s3.upload("test/wiki/topics/guide.md", "---\ntitle: Guide\n---\n\nEdited by hand.")
        _, emb = self._run(profile, mock_s3)
        emb.assert_called_once()
        _, emb = self._run(profile, mock_s3)
        emb.assert_not_called()

    def test_concepts_skipped_for_down_provider_are_pending(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        compiler._maint_state = {}
        compiler._provider_down = True
        with patch.object(compiler, "_synthesize_concepts") as syn:
            compiler._run_concepts()
        syn.assert_not_called()
        assert compiler._maint_state["concepts"]["pending"] is True

    def test_synthesis_reports_deferred_and_failed(self, profile, mock_s3):
        compiler = Compiler(profile=profile, s3=mock_s3, provider=LLMProvider.OLLAMA)
        n = compiler.MAX_CONCEPT_CREATIONS_PER_RUN + 2
        for page in range(2):
            links = " ".join(f"[[Concept {i}]]" for i in range(n))
            fm = Frontmatter(title=f"Page {page}", source="test", compiled=True)
            mock_s3.upload(f"test/wiki/page{page}.md", fm.to_yaml_block() + f"\n{links}\n")
        with patch.object(compiler, "_call_llm_with_fallback", return_value="Body."):
            assert compiler._synthesize_concepts() == "deferred"
            assert compiler._synthesize_concepts() == "done"
        mock_s3.upload("test/wiki/page2.md", "---\ntitle: P\n---\n\n[[Concept 0]] [[Fresh]]")
        mock_s3.upload("test/wiki/page3.md", "---\ntitle: Q\n---\n\n[[Fresh]]")
        with patch.object(compiler, "_call_llm_with_fallback", side_effect=RuntimeError("x")):
            assert compiler._synthesize_concepts() == "failed"
