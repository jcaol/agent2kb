"""The MCP server keeps one loaded embedding index between tool calls and
reloads it only when the stored index changes (F10); retrieval fetches
candidate pages in parallel batches."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb import mcp_server
from agent2kb.embeddings import OLLAMA_EMBED_MODEL, EmbeddingClient, SharedEmbeddingIndex
from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import S3Client

EMB_KEY = "notes/_embeddings/index.json"


@pytest.fixture
def profile():
    return ProfileConfig(
        name="notes", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="test-bucket", prefix="notes/",
    )


@pytest.fixture
def s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


def _index(s3, keys, vec=(1.0, 0.0)):
    s3.upload(EMB_KEY, json.dumps({
        "version": 1, "model": OLLAMA_EMBED_MODEL, "provider": "ollama",
        "items": [{"key": k, "hash": "h", "vec": list(vec)} for k in keys],
    }))


def _count_gets(s3, key_part):
    seen: list[str] = []
    s3._client.meta.events.register(
        "before-call.s3.GetObject",
        lambda params, **kw: seen.append(params["url_path"]) if key_part in params[
            "url_path"] else None,
    )
    return seen


def test_index_is_loaded_once_while_unchanged(profile, s3):
    _index(s3, ["notes/wiki/a.md"])
    gets = _count_gets(s3, "_embeddings")
    shared = SharedEmbeddingIndex(profile, s3)
    first = shared.get()
    assert first is not None and set(first.items) == {"notes/wiki/a.md"}
    assert shared.get() is first
    assert shared.get() is first
    assert len(gets) == 1


def test_changed_index_is_reloaded(profile, s3):
    _index(s3, ["notes/wiki/a.md"])
    shared = SharedEmbeddingIndex(profile, s3)
    first = shared.get()
    _index(s3, ["notes/wiki/a.md", "notes/wiki/b.md"])
    second = shared.get()
    assert second is not first
    assert set(second.items) == {"notes/wiki/a.md", "notes/wiki/b.md"}


def test_missing_then_created_index(profile, s3):
    shared = SharedEmbeddingIndex(profile, s3)
    assert shared.get() is None
    assert shared.get() is None
    _index(s3, ["notes/wiki/a.md"])
    assert shared.get() is not None


def test_deleted_index_is_dropped(profile, s3):
    _index(s3, ["notes/wiki/a.md"])
    shared = SharedEmbeddingIndex(profile, s3)
    assert shared.get() is not None
    s3.delete(EMB_KEY)
    assert shared.get() is None


def test_concurrent_first_use_loads_once(profile, s3):
    _index(s3, ["notes/wiki/a.md"])
    gets = _count_gets(s3, "_embeddings")
    shared = SharedEmbeddingIndex(profile, s3)
    results = []
    threads = [threading.Thread(target=lambda: results.append(shared.get())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(r) for r in results}) == 1
    assert len(gets) == 1


def _page(s3, name, text):
    fm = Frontmatter(title=name, source="https://example.com/" + name)
    s3.upload(f"notes/wiki/{name}.md", fm.to_yaml_block() + "\n" + text)


def test_mcp_search_reuses_the_index_between_calls(profile, s3):
    _page(s3, "alpha", "alpha widgets explained in depth")
    _index(s3, ["notes/wiki/alpha.md"])
    ctx = mcp_server.KBContext(profile=profile, s3=s3)
    gets = _count_gets(s3, "_embeddings")
    with patch.object(EmbeddingClient, "embed", return_value=[1.0, 0.0]):
        for _ in range(3):
            assert "alpha" in mcp_server.search(ctx, "alpha widgets", 5)
        assert len(gets) == 1

        _page(s3, "beta", "beta gadgets")
        _index(s3, ["notes/wiki/alpha.md", "notes/wiki/beta.md"])
        mcp_server.search(ctx, "beta gadgets", 5)
        assert len(gets) == 2
        assert set(ctx.embeddings().get().items) == {"notes/wiki/alpha.md", "notes/wiki/beta.md"}


def test_candidate_pages_are_fetched_in_parallel(profile, s3):
    from agent2kb.query import QueryEngine

    for i in range(12):
        _page(s3, f"page{i}", f"alpha topic number {i}")
    engine = QueryEngine(profile=profile, s3=s3, expand_mode="off")
    calls: list[list[str]] = []
    real = s3.download_many

    def spy(keys, on_error=None):
        calls.append(list(keys))
        return real(keys, on_error=on_error)

    with patch.object(s3, "download_many", side_effect=spy):
        hits = engine.search("alpha topic", top_k=20)
    assert len(hits) == 12
    # No index.md here, so every page is a candidate: one parallel batch.
    assert [len(c) for c in calls] == [12]
