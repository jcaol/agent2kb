"""Operation-count budget for a no-op compile, and invalidation of the
ETag-keyed compile cache (F10).

Before the cache, 100 unchanged raw/wiki pairs cost 300 body downloads per
compile tick (raw + wiki for the hash check, wiki again for index.md).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb.compile.cache import compile_cache_key
from agent2kb.compile.compiler import Compiler
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client
from agent2kb.utils import content_hash

N_PAIRS = 100


@pytest.fixture
def profile():
    return ProfileConfig(
        name="notes",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="notes/",
        compile_provider=LLMProvider.OLLAMA,
    )


@pytest.fixture
def s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


class GetCounter:
    """Counts GetObject calls made through the real boto3 client."""

    def __init__(self, s3: S3Client):
        self.keys: list[str] = []
        s3._client.meta.events.register("before-call.s3.GetObject", self._on_get)

    def _on_get(self, params, **_kw):
        self.keys.append(params.get("url_path", ""))

    def reset(self) -> None:
        self.keys.clear()

    @property
    def count(self) -> int:
        return len(self.keys)


def _pair(s3: S3Client, name: str, title: str = "Alpha") -> None:
    raw = Frontmatter(title=title, source="https://example.com/" + name).to_yaml_block()
    raw += f"\nRaw body about {name}."
    s3.upload(f"notes/raw/{name}.md", raw)
    wiki_fm = Frontmatter(title=title, source="https://example.com/" + name, compiled=True,
                          date="2020-01-01", tags=["alpha"], raw_content_hash=content_hash(raw))
    s3.upload(f"notes/wiki/{name}.md", wiki_fm.to_yaml_block() + f"\n# {title} {name}\n\nBody")


def _compile(profile, s3) -> tuple[list, list]:
    """One no-op-capable compile tick with the LLM and embeddings stubbed."""
    compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
    llm_calls: list = []

    def fake_embed(force=False):
        compiler._embed_fingerprint = compiler._wiki_fingerprint(
            s3.list_objects(sub_prefix="wiki/", suffix=".md")
        )
        return 0, 0

    def fake_llm(messages, purpose="compile"):
        llm_calls.append(purpose)
        return "compiled"

    with patch.object(compiler, "_synthesize_concepts", return_value="done"), \
            patch.object(compiler, "update_embeddings", side_effect=fake_embed), \
            patch.object(compiler, "_call_llm_with_fallback", side_effect=fake_llm):
        results = compiler.compile_all(new_only=True)
    return results, llm_calls


def test_no_op_compile_get_count_is_bounded(profile, s3):
    for i in range(N_PAIRS):
        _pair(s3, f"page-{i:03d}")
    gets = GetCounter(s3)

    # The first tick has a cold cache and verifies every pair once.
    results, llm = _compile(profile, s3)
    assert results == [] and llm == []

    gets.reset()
    results, llm = _compile(profile, s3)
    assert results == [] and llm == []
    # Was 3 * N_PAIRS = 300. What is left is a handful of small state files
    # (maintenance state, compile cache, index.md), independent of N.
    assert gets.count <= 5, gets.keys
    assert not any("/raw/" in k or "/wiki/page-" in k for k in gets.keys), gets.keys


def test_unchanged_index_is_not_rewritten(profile, s3):
    _pair(s3, "one")
    _compile(profile, s3)
    etag = s3.head_etag("notes/wiki/index.md")
    _compile(profile, s3)
    assert s3.head_etag("notes/wiki/index.md") == etag


def test_changed_raw_is_still_detected(profile, s3):
    _pair(s3, "one")
    _pair(s3, "two")
    _compile(profile, s3)  # warm the cache

    s3.upload("notes/raw/two.md", "---\ntitle: Alpha\n---\nEdited body.")
    compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
    assert compiler._select_pending(s3.list_raw_files()) == ["notes/raw/two.md"]


def test_hand_edited_wiki_page_refreshes_index_entry(profile, s3):
    _pair(s3, "one", title="Alpha")
    _compile(profile, s3)
    assert "Alpha one" in s3.download("notes/wiki/index.md")

    content = s3.download("notes/wiki/one.md").replace("# Alpha one", "# Beta one")
    s3.upload("notes/wiki/one.md", content)
    _compile(profile, s3)
    index = s3.download("notes/wiki/index.md")
    assert "Beta one" in index and "Alpha one" not in index


def test_deleted_page_leaves_cache_and_index(profile, s3):
    _pair(s3, "one")
    _pair(s3, "two")
    _compile(profile, s3)
    s3.delete("notes/wiki/two.md")
    s3.delete("notes/raw/two.md")
    _compile(profile, s3)
    cache = json.loads(s3.download(compile_cache_key(profile)))
    assert "notes/raw/two.md" not in cache["pairs"]
    assert "notes/wiki/two.md" not in cache["pages"]
    assert "[[two|" not in s3.download("notes/wiki/index.md")


@pytest.mark.parametrize("payload", ["not json", json.dumps({"version": 99, "pairs": {}})])
def test_corrupt_or_foreign_cache_is_cold(profile, s3, payload):
    _pair(s3, "one")
    s3.upload(compile_cache_key(profile), payload)
    gets = GetCounter(s3)
    results, _ = _compile(profile, s3)
    assert results == []
    # Cold: the pair is verified by downloading both bodies, as before.
    assert any(k.endswith("/raw/one.md") for k in gets.keys)


def test_forged_cache_cannot_hide_a_changed_source(profile, s3):
    """The cache only vouches for exact ETags; a stale entry is re-verified."""
    _pair(s3, "one")
    s3.upload(compile_cache_key(profile), json.dumps({
        "version": 1,
        "pairs": {"notes/raw/one.md": {"raw": "stale", "wiki": "stale"}},
        "pages": {},
    }))
    s3.upload("notes/raw/one.md", "---\ntitle: Alpha\n---\nChanged body.")
    compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
    assert compiler._select_pending(["notes/raw/one.md"]) == ["notes/raw/one.md"]
