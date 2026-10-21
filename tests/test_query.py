"""Tests for QueryEngine index prefilter."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.query import QueryEngine
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
    )


@pytest.fixture
def mock_s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


class TestIndexPrefilter:
    """Regression tests for _index_prefilter concept path handling."""

    def _write_index(self, mock_s3, body: str) -> None:
        mock_s3.upload("test/wiki/index.md", body)

    def test_concept_link_with_prefix_resolves_to_wiki_concepts(self, profile, mock_s3):
        """Regression: [[concepts/llm]] must not become wiki/concepts/concepts/llm.md.

        Reproduces the P1 bug from the 2026-04-10 review where the index writer
        emits [[concepts/slug]] but the query prefilter then prepended
        wiki/concepts/ again, producing a doubled path.
        """
        index_body = (
            "# Test KB\n\n"
            "## Articles\n\n"
            "- [[20260410-llm-overview]] — LLM Overview\n\n"
            "## Concepts\n\n"
            "- [[concepts/llm]] — LLM (5)\n"
            "- [[concepts/rag]] — RAG (3)\n"
        )
        self._write_index(mock_s3, index_body)

        engine = QueryEngine(profile=profile, s3=mock_s3)
        candidates = engine._index_prefilter(["llm"])

        assert "test/wiki/concepts/llm.md" in candidates
        assert "test/wiki/concepts/concepts/llm.md" not in candidates

    def test_bare_concept_link_still_resolves(self, profile, mock_s3):
        """Legacy/bare concept links like [[llm]] still resolve under wiki/concepts/."""
        index_body = (
            "# Test KB\n\n"
            "## Concepts\n\n"
            "- [[llm]] — LLM (5)\n"
        )
        self._write_index(mock_s3, index_body)

        engine = QueryEngine(profile=profile, s3=mock_s3)
        candidates = engine._index_prefilter(["llm"])

        assert "test/wiki/concepts/llm.md" in candidates

    def test_article_link_resolves_under_wiki_root(self, profile, mock_s3):
        """Article links like [[20260410-foo]] map to wiki/20260410-foo.md."""
        index_body = (
            "# Test KB\n\n"
            "## Articles\n\n"
            "- [[20260410-llm-overview]] — LLM Overview\n"
        )
        self._write_index(mock_s3, index_body)

        engine = QueryEngine(profile=profile, s3=mock_s3)
        candidates = engine._index_prefilter(["llm"])

        assert "test/wiki/20260410-llm-overview.md" in candidates


class TestDuplicateIndexEntries:
    """index.md may list one article in several sections ("Past 7 Days" and "Articles")."""

    INDEX = (
        "# Test KB\n\n"
        "## Past 7 Days\n\n"
        "- [[20260410-redis-cluster]] — Redis Cluster notes\n\n"
        "## Articles\n\n"
        "### Other\n\n"
        "- [[20260410-redis-cluster]] — Redis Cluster notes\n"
        "- [[20260409-redis-basics]] — Redis basics\n"
    )

    def _engine(self, profile, mock_s3):
        mock_s3.upload("test/wiki/index.md", self.INDEX)
        for slug, title in (("20260410-redis-cluster", "Redis Cluster"),
                            ("20260409-redis-basics", "Redis basics")):
            fm = Frontmatter(title=title, source="https://example.org/" + slug)
            mock_s3.upload(f"test/wiki/{slug}.md", fm.to_yaml_block() + "\nAbout redis.\n")
        return QueryEngine(profile=profile, s3=mock_s3, expand_mode="off")

    def test_prefilter_returns_each_page_once(self, profile, mock_s3):
        engine = self._engine(profile, mock_s3)
        candidates = engine._index_prefilter(["redis"])
        assert len(candidates) == len(set(candidates)) == 2

    def test_search_returns_each_page_once(self, profile, mock_s3):
        engine = self._engine(profile, mock_s3)
        keys = [h.key for h in engine.search("redis")]
        assert sorted(keys) == sorted(set(keys))
        assert "test/wiki/20260410-redis-cluster.md" in keys and len(keys) == 2


class TestQueryLanguagePipeline:
    """Regression tests for query language handling.

    Before 0.6.2 the _llm_answer system prompt was English-only, so the
    LLM drifted to English when the wiki context didn't force it into
    Chinese. Terminal output and saved outputs/ files therefore ended up
    in inconsistent languages. Fix: share the compile pipeline's language
    instruction, and post-process every answer through
    ensure_traditional_chinese() (which converts only for zh-HK / zh-TW / yue).
    """

    @staticmethod
    def _zh_hk(profile):
        return profile.model_copy(update={"output_language": "zh-HK"})

    def _seed_wiki(self, mock_s3):
        """Minimal wiki fixture so QueryEngine._search finds a hit."""
        # A single article that the keyword 'llm' will match
        fm = Frontmatter(
            title="LLM Overview",
            source="https://example.com",
            date="2026-04-11",
            tags=["llm"],
        )
        body = "\n# LLM Overview\n\nLLM stands for Large Language Model.\n"
        mock_s3.upload("test/wiki/20260411-llm-overview.md", fm.to_yaml_block() + body)

        index = (
            "# Test KB\n\n"
            "## Articles\n\n"
            "- [[20260411-llm-overview]] — LLM Overview\n"
        )
        mock_s3.upload("test/wiki/index.md", index)

    @pytest.mark.parametrize(
        "language, expected",
        [
            (None, "ALWAYS write in English"),
            ("yue", "Cantonese-style Traditional Chinese"),
            ("zh-HK", "Hong Kong usage"),
        ],
    )
    def test_llm_answer_system_prompt_includes_language_instruction(
        self, profile, mock_s3, language, expected
    ):
        """The LLM system prompt must carry the KB's language rules.

        Unset means English; yue selects the Cantonese/Traditional Chinese rules and
        zh-HK the neutral Hong Kong Traditional Chinese ones.

        Both _expand_cross_language and _llm_answer invoke the compiler's
        _call_llm_with_fallback, so the patch captures multiple calls.
        We pick the one whose system message identifies itself as the
        "knowledge base assistant" — that's the answer path we care about.
        """
        self._seed_wiki(mock_s3)

        # autospec=True makes the mock respect method binding so it receives
        # (self, messages) the way the real method does.
        from agent2kb.compile.compiler import Compiler

        with patch.object(
            Compiler,
            "_call_llm_with_fallback",
            autospec=True,
            return_value="Mocked answer.",
        ) as mock_llm:
            kb = profile.model_copy(update={"output_language": language})
            engine = QueryEngine(profile=kb, s3=mock_s3)
            engine.query("llm", top_k=1)

        assert mock_llm.called, "LLM should have been called"

        # Find the call whose first message is the answer system prompt.
        answer_call = None
        for call in mock_llm.call_args_list:
            # autospec binds self as the first positional → call.args = (self, messages)
            messages = call.args[1] if len(call.args) > 1 else call.args[0]
            sys_content = messages[0]["content"] if messages else ""
            if "knowledge base assistant" in sys_content:
                answer_call = messages
                break

        assert answer_call is not None, "Answer path must call the LLM"
        system_content = answer_call[0]["content"]
        assert expected in system_content
        assert "[[Docker]]" in system_content  # sanity: the example line is present

    def test_query_converts_simplified_chinese_to_traditional(
        self, profile, mock_s3
    ):
        """For zh-HK, simplified Chinese in LLM output must be converted to traditional."""
        self._seed_wiki(mock_s3)

        # Fake LLM that returns a simplified-Chinese answer
        simplified_answer = "这是一个关于 LLM 的简体中文回答，包含 [[LLM Overview]]。"

        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback",
            return_value=simplified_answer,
        ):
            engine = QueryEngine(profile=self._zh_hk(profile), s3=mock_s3)
            answer = engine.query("llm", top_k=1)

        # Traditional Chinese forms we expect after conversion
        assert "這" in answer  # was "这"
        assert "個" in answer  # was "个"
        assert "簡體" in answer or "簡體中文" in answer  # was "简体"
        # And the simplified originals should be gone
        assert "这" not in answer
        assert "个" not in answer
        # English jargon and wikilinks must survive untouched
        assert "LLM" in answer
        assert "[[LLM Overview]]" in answer

    def test_query_default_language_leaves_simplified_chinese(self, profile, mock_s3):
        """Unset output_language means English: no OpenCC conversion of the answer."""
        self._seed_wiki(mock_s3)
        simplified_answer = "这是一个关于 LLM 的简体中文回答，包含 [[LLM Overview]]。"

        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback",
            return_value=simplified_answer,
        ):
            answer = QueryEngine(profile=profile, s3=mock_s3).query("llm", top_k=1)

        assert "这" in answer and "個" not in answer

    def test_save_output_persists_same_string_as_terminal(
        self, profile, mock_s3
    ):
        """Terminal and saved outputs/ file must contain the SAME answer string.

        Catches any regression where post-processing happens after printing
        but before saving (or vice versa), causing the two to diverge.
        """
        self._seed_wiki(mock_s3)
        canonical_answer = "呢個答案有 [[LLM Overview]] 同埋 English jargon 保持不變。"

        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback",
            return_value=canonical_answer,
        ):
            engine = QueryEngine(profile=profile, s3=mock_s3)
            terminal_answer = engine.query("llm", top_k=1)
            output_key = engine.save_output("llm question", terminal_answer)

        saved_body = mock_s3.download(output_key)
        # The answer text should appear verbatim in the saved file body
        assert terminal_answer in saved_body
        # No regression: English jargon preserved
        assert "English jargon" in saved_body
        assert "[[LLM Overview]]" in saved_body


class TestExpansionMode:
    """Cache-first cross-language expansion.

    The default expansion mode is ``auto``: dictionary-first, LLM only
    when the dict misses and the query is long/CJK enough to plausibly
    benefit. Tests pin the contract so future changes can't silently
    reintroduce a per-query LLM round-trip.
    """

    def test_off_mode_skips_expansion_entirely(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="off")
        with patch.object(engine, "_llm_expand") as mock_llm:
            extra = engine._expand_cross_language("LLM", ["llm"])
        assert extra == []
        assert not mock_llm.called

    def test_dict_mode_never_calls_llm(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="dict")
        with patch.object(engine, "_llm_expand") as mock_llm:
            # 'llm' is in ZH_CROSS_LANG_DICT so we should get hits
            engine._expand_cross_language("LLM", ["llm"])
        assert not mock_llm.called
        # Result may be empty if dict has no entry, but the LLM must never fire.

    def test_auto_mode_skips_llm_when_dict_hits(self, profile, mock_s3):
        """If the dict already has the term, no LLM round-trip is paid."""
        from agent2kb.lang_data import ZH_CROSS_LANG_DICT

        # Pick a term that's actually in the dictionary
        sample_term = next(iter(ZH_CROSS_LANG_DICT))
        # zh-HK keeps the dictionary's Chinese equivalents; English KBs drop them.
        zh_hk = profile.model_copy(update={"output_language": "zh-HK"})
        engine = QueryEngine(profile=zh_hk, s3=mock_s3, expand_mode="auto")
        with patch.object(engine, "_llm_expand") as mock_llm:
            extra = engine._expand_cross_language(sample_term, [sample_term.lower()])
        assert not mock_llm.called
        assert extra  # at least one expansion from the dict

    def test_auto_mode_short_unknown_term_does_not_call_llm(
        self, profile, mock_s3
    ):
        """Short all-Latin queries below the gate threshold stay cheap."""
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="auto")
        with patch.object(engine, "_llm_expand") as mock_llm:
            extra = engine._expand_cross_language("zorblax", ["zorblax"])
        assert not mock_llm.called
        assert extra == []

    def test_auto_mode_long_query_falls_back_to_llm(self, profile, mock_s3):
        """Long queries with no dict hit should hit the LLM (the case the
        dict-only mode would miss)."""
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="auto")
        with patch.object(engine, "_llm_expand", return_value=["译文"]) as mock_llm:
            extra = engine._expand_cross_language(
                "what does the new framework do for streaming agents",
                ["what", "framework", "streaming", "agents", "new"],
            )
        assert mock_llm.called
        assert "译文" in extra

    def test_llm_mode_always_calls_llm(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="llm")
        with patch.object(engine, "_llm_expand", return_value=["xyz"]) as mock_llm:
            engine._expand_cross_language("llm", ["llm"])
        assert mock_llm.called

    def test_expansion_is_cached_per_engine(self, profile, mock_s3):
        """Repeat queries on the same engine instance reuse the prior result."""
        engine = QueryEngine(profile=profile, s3=mock_s3, expand_mode="auto")
        with patch.object(engine, "_llm_expand", return_value=["abc"]) as mock_llm:
            engine._expand_cross_language("xx yy zz qq", ["xx", "yy", "zz", "qq"])
            engine._expand_cross_language("xx yy zz qq", ["xx", "yy", "zz", "qq"])
            engine._expand_cross_language("qq zz yy xx", ["qq", "zz", "yy", "xx"])
        # First call may invoke LLM. Subsequent calls (same token set) hit cache.
        assert mock_llm.call_count == 1

    def test_invalid_mode_raises(self, profile, mock_s3):
        with pytest.raises(ValueError):
            QueryEngine(profile=profile, s3=mock_s3, expand_mode="banana")


class TestRerankIntegration:
    """``_maybe_rerank`` should reorder + truncate hits when voyage key
    + ``rerank_enabled`` (or its default-on) align."""

    def _make_profile(self, voyage_key="vk-test", rerank_enabled=None):
        return ProfileConfig(
            name="test",
            s3_endpoint="http://localhost:5000",
            access_key="testing",
            secret_key="testing",
            bucket="test-bucket",
            prefix="test/",
            voyage_api_key=voyage_key,
            rerank_enabled=rerank_enabled,
        )

    def test_rerank_reorders_and_truncates(self, mock_s3, monkeypatch):
        from agent2kb.query import SearchHit

        prof = self._make_profile()
        engine = QueryEngine(profile=prof, s3=mock_s3)

        # Fake reranker that puts hit #2 first, hit #0 second
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.available = True
        fake.rerank.return_value = [(2, 0.99), (0, 0.85)]
        engine._reranker = fake

        hits = [
            SearchHit(key=f"k{i}", title=f"t{i}", score=float(10 - i), snippet="", body=f"b{i}")
            for i in range(5)
        ]
        out = engine._maybe_rerank("q?", hits, top_k=2)
        assert len(out) == 2
        assert [h.key for h in out] == ["k2", "k0"]
        # Scores should be the rerank scores, not the prior keyword scores
        assert out[0].score == pytest.approx(0.99)
        assert out[1].score == pytest.approx(0.85)

    def test_rerank_skipped_when_disabled(self, mock_s3):
        from agent2kb.query import SearchHit

        prof = self._make_profile(rerank_enabled=False)
        engine = QueryEngine(profile=prof, s3=mock_s3)
        # Reranker should never be built — explicit False
        hits = [
            SearchHit(key=f"k{i}", title=f"t{i}", score=float(10 - i), snippet="", body="")
            for i in range(5)
        ]
        out = engine._maybe_rerank("q?", hits, top_k=2)
        assert [h.key for h in out] == ["k0", "k1"]  # Original order, just truncated

    def test_rerank_skipped_without_voyage_key(self, mock_s3):
        from agent2kb.query import SearchHit

        prof = self._make_profile(voyage_key=None)
        engine = QueryEngine(profile=prof, s3=mock_s3)
        hits = [
            SearchHit(key=f"k{i}", title=f"t{i}", score=float(10 - i), snippet="", body="")
            for i in range(3)
        ]
        out = engine._maybe_rerank("q?", hits, top_k=2)
        # No voyage key → no rerank → first 2 by score
        assert [h.key for h in out] == ["k0", "k1"]

    def test_rerank_single_hit_passthrough(self, mock_s3):
        from agent2kb.query import SearchHit

        prof = self._make_profile()
        engine = QueryEngine(profile=prof, s3=mock_s3)
        single = [SearchHit(key="only", title="only", score=1.0, snippet="", body="")]
        out = engine._maybe_rerank("q?", single, top_k=10)
        # Single hit shortcuts out — no rerank needed
        assert out == single
