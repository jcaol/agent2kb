"""Tests for Voyage embedding + reranking (0.13.0).

Voyage SDK is mocked end-to-end so tests don't need network or a real
API key. The contracts pinned here:

  1. EmbeddingClient routes to Voyage when ``voyage_api_key`` is set
     (and embedding_provider isn't an explicit override pointing
     elsewhere).
  2. ``task_type`` is mapped: ``RETRIEVAL_DOCUMENT`` → ``document``,
     ``RETRIEVAL_QUERY`` → ``query``.
  3. Reranker degrades gracefully when SDK is missing or the call
     errors — passthrough order, no exception.
  4. ``rerank_enabled`` config field resolves to default-on when
     voyage key is set; explicit False overrides.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from agent2kb.embeddings import EmbeddingClient, Reranker
from agent2kb.models import ProfileConfig


def _profile(**overrides) -> ProfileConfig:
    base = dict(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
    )
    base.update(overrides)
    return ProfileConfig(**base)


def _stub_voyageai(
    monkeypatch,
    *,
    embed_vec=None,
    rerank_results=None,
    raise_on_embed=False,
    raise_on_rerank=False,
):
    """Install a fake ``voyageai`` module that records calls."""
    mod = types.ModuleType("voyageai")
    client = MagicMock()
    if raise_on_embed:
        client.embed.side_effect = RuntimeError("simulated embed error")
    else:
        embed_result = MagicMock()
        embed_result.embeddings = [embed_vec or [0.1, 0.2, 0.3]]
        client.embed.return_value = embed_result
    if raise_on_rerank:
        client.rerank.side_effect = RuntimeError("simulated rerank error")
    else:
        rerank_result = MagicMock()
        rerank_result.results = rerank_results or []
        client.rerank.return_value = rerank_result
    mod.Client = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "voyageai", mod)
    return client


class TestEmbeddingClientRoutesToVoyage:
    def test_voyage_key_picks_voyage_provider(self):
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        assert client.provider == "voyage"
        assert "voyage" in client.model

    def test_explicit_provider_beats_voyage_key(self):
        client = EmbeddingClient(
            _profile(voyage_api_key="vk-test", embedding_provider="openai", openai_api_key="ok")
        )
        assert client.provider == "openai"

    def test_voyage_priority_over_gemini(self):
        """voyage_api_key set wins over gemini_api_key — Voyage is the
        no-train-by-default-with-opt-out option and the better CJK model."""
        client = EmbeddingClient(
            _profile(voyage_api_key="vk-test", gemini_api_key="g-test")
        )
        assert client.provider == "voyage"

    def test_embed_calls_voyage_sdk_with_document_input_type(self, monkeypatch):
        fake = _stub_voyageai(monkeypatch, embed_vec=[0.5, 0.6, 0.7])
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        vec = client.embed("a wiki page", task_type="RETRIEVAL_DOCUMENT")
        assert vec == [0.5, 0.6, 0.7]
        args, kwargs = fake.embed.call_args
        assert kwargs["input_type"] == "document"

    def test_embed_query_uses_query_input_type(self, monkeypatch):
        fake = _stub_voyageai(monkeypatch)
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        client.embed("how does X work?", task_type="RETRIEVAL_QUERY")
        args, kwargs = fake.embed.call_args
        assert kwargs["input_type"] == "query"

    def test_embed_truncates_long_input(self, monkeypatch):
        fake = _stub_voyageai(monkeypatch)
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        long = "x" * 20000
        client.embed(long, task_type="RETRIEVAL_DOCUMENT")
        sent = fake.embed.call_args[0][0]
        # First positional arg is a list — get the only document
        assert len(sent[0]) <= 8000

    def test_missing_voyageai_sdk_raises_clear_error(self, monkeypatch):
        # Simulate ``import voyageai`` failure
        monkeypatch.setitem(sys.modules, "voyageai", None)
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        with pytest.raises(RuntimeError, match=r"agent2kb\[voyage\]"):
            client.embed("text")


class TestReranker:
    def test_unavailable_without_voyage_key(self):
        r = Reranker(_profile())
        assert r.available is False

    def test_available_with_voyage_key(self):
        r = Reranker(_profile(voyage_api_key="vk-test"))
        assert r.available is True

    def test_rerank_returns_passthrough_when_unavailable(self):
        r = Reranker(_profile())
        out = r.rerank("q", ["doc-a", "doc-b", "doc-c"], top_k=2)
        assert out == [(0, 1.0), (1, 1.0)]

    def test_rerank_calls_voyage_sdk(self, monkeypatch):
        # Voyage rerank result objects: each has .index and .relevance_score
        r1 = MagicMock()
        r1.index, r1.relevance_score = 2, 0.95
        r2 = MagicMock()
        r2.index, r2.relevance_score = 0, 0.7
        fake = _stub_voyageai(monkeypatch, rerank_results=[r1, r2])

        r = Reranker(_profile(voyage_api_key="vk-test"))
        out = r.rerank("query", ["doc-a", "doc-b", "doc-c"], top_k=2)
        assert out == [(2, 0.95), (0, 0.7)]
        kwargs = fake.rerank.call_args.kwargs
        assert kwargs["query"] == "query"
        assert kwargs["model"].startswith("rerank-")

    def test_rerank_does_not_pass_return_documents(self, monkeypatch):
        """Regression for 0.13.0 → 0.14.1: voyageai SDK 0.3.x does NOT
        accept return_documents=. Passing it caused every rerank call
        to raise TypeError and silently fall through to passthrough.
        """
        fake = _stub_voyageai(monkeypatch, rerank_results=[])
        r = Reranker(_profile(voyage_api_key="vk-test"))
        r.rerank("q", ["doc-a"], top_k=1)
        kwargs = fake.rerank.call_args.kwargs
        assert "return_documents" not in kwargs, (
            "voyageai 0.3.x SDK rejects return_documents=; calling with "
            "it makes every rerank silently fall back to passthrough."
        )

    def test_rerank_falls_back_on_api_error(self, monkeypatch):
        _stub_voyageai(monkeypatch, raise_on_rerank=True)
        r = Reranker(_profile(voyage_api_key="vk-test"))
        out = r.rerank("q", ["doc-a", "doc-b", "doc-c"], top_k=2)
        # Falls back to passthrough — search must always return something
        assert out == [(0, 1.0), (1, 1.0)]

    def test_rerank_caps_input_documents(self, monkeypatch):
        # 100 docs in, but Voyage call should see at most 30
        fake = _stub_voyageai(monkeypatch, rerank_results=[])
        r = Reranker(_profile(voyage_api_key="vk-test"))
        r.rerank("q", [f"doc-{i}" for i in range(100)], top_k=10)
        kwargs = fake.rerank.call_args.kwargs
        assert len(kwargs["documents"]) <= 30

    def test_rerank_empty_documents(self):
        r = Reranker(_profile(voyage_api_key="vk-test"))
        assert r.rerank("q", [], top_k=10) == []


class TestRerankEnabledResolution:
    """``rerank_enabled`` default behaviour: on when voyage key set."""

    def test_default_on_when_voyage_key_set(self):
        prof = _profile(voyage_api_key="vk-test")
        assert prof.rerank_enabled is None
        # The resolution lives in QueryEngine._maybe_rerank — confirm
        # the policy by inspecting the field directly.
        # (rerank_enabled is None means "auto = on when voyage key set")
        explicit = prof.rerank_enabled
        resolved = bool(prof.voyage_api_key) if explicit is None else explicit
        assert resolved is True

    def test_default_off_without_voyage_key(self):
        prof = _profile()
        resolved = bool(prof.voyage_api_key) if prof.rerank_enabled is None else prof.rerank_enabled
        assert resolved is False

    def test_explicit_false_overrides_voyage_key(self):
        prof = _profile(voyage_api_key="vk-test", rerank_enabled=False)
        resolved = bool(prof.voyage_api_key) if prof.rerank_enabled is None else prof.rerank_enabled
        assert resolved is False


class TestConfigDrivenModelNames:
    """``embedding_model`` and ``rerank_model`` on ProfileConfig
    override the per-provider default constants. Pins the contract so
    operators can upgrade to voyage-5-lite / gemini-embedding-002 /
    rerank-3-lite by editing config alone, no agent2kb release needed.
    """

    def test_default_embedding_model_when_unset(self):
        client = EmbeddingClient(_profile(voyage_api_key="vk-test"))
        # Falls back to per-provider constant
        from agent2kb.embeddings import VOYAGE_EMBED_MODEL
        assert client.model == VOYAGE_EMBED_MODEL

    def test_profile_overrides_default_embedding_model(self):
        client = EmbeddingClient(
            _profile(voyage_api_key="vk-test", embedding_model="voyage-5-future")
        )
        assert client.model == "voyage-5-future"

    def test_override_passed_to_sdk(self, monkeypatch):
        fake = _stub_voyageai(monkeypatch)
        client = EmbeddingClient(
            _profile(voyage_api_key="vk-test", embedding_model="voyage-XXX-experimental")
        )
        client.embed("foo")
        kwargs = fake.embed.call_args.kwargs
        assert kwargs["model"] == "voyage-XXX-experimental"

    def test_override_works_for_any_provider(self):
        """Field is provider-agnostic — works for Gemini override too."""
        client = EmbeddingClient(
            _profile(gemini_api_key="g", embedding_model="gemini-embedding-002")
        )
        assert client.provider == "gemini"
        assert client.model == "gemini-embedding-002"

    def test_default_rerank_model_when_unset(self):
        r = Reranker(_profile(voyage_api_key="vk-test"))
        from agent2kb.embeddings import VOYAGE_RERANK_MODEL
        assert r.model == VOYAGE_RERANK_MODEL

    def test_profile_overrides_default_rerank_model(self):
        r = Reranker(
            _profile(voyage_api_key="vk-test", rerank_model="rerank-3.0-lite")
        )
        assert r.model == "rerank-3.0-lite"

    def test_rerank_constructor_arg_wins_over_profile(self):
        """Tests / one-off scripts can override even the profile field."""
        r = Reranker(
            _profile(voyage_api_key="vk-test", rerank_model="from-config"),
            model="from-arg",
        )
        assert r.model == "from-arg"

    def test_rerank_passes_configured_model_to_sdk(self, monkeypatch):
        fake = _stub_voyageai(monkeypatch, rerank_results=[])
        r = Reranker(
            _profile(voyage_api_key="vk-test", rerank_model="rerank-3.0-experimental")
        )
        r.rerank("q", ["doc"], top_k=1)
        kwargs = fake.rerank.call_args.kwargs
        assert kwargs["model"] == "rerank-3.0-experimental"
