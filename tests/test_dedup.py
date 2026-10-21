"""Duplicate detection for Web Clipper imports and URL ingest.

Regression for a loop in production: a clipping came back after import
(the Obsidian device that clipped it re-uploaded its local copy on a later
sync), got a new date prefix in its raw key, and was imported and compiled
again — 21 extra copies across 8 articles in one KB.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from agent2kb.compile.compiler import Compiler
from agent2kb.dedup import RawIndex, hash_suffix, normalize_url
from agent2kb.ingest.base import IngestPipeline
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client
from agent2kb.utils import content_hash

P = "test/"


@pytest.fixture
def profile():
    return ProfileConfig(
        name="test", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="test-bucket", prefix=P,
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


class TestNormalizeUrl:
    @pytest.mark.parametrize("a, b", [
        ("https://agentusecases.ai/?fbclid=IwdGRjcAU6F9",
         "https://agentusecases.ai"),
        ("https://example.com/post?utm_source=x&utm_medium=y&id=7",
         "https://example.com/post?id=7"),
        ("https://twitter.com/user/status/1?s=20&t=abc",
         "https://x.com/user/status/1"),
        ("http://www.example.com/a/", "https://example.com/a"),
        ("https://m.youtube.com/watch?v=xyz&feature=share&si=k",
         "https://youtube.com/watch?v=xyz"),
        ("https://example.com/a?b=2&a=1#frag", "https://example.com/a?a=1&b=2"),
    ])
    def test_same_page(self, a, b):
        assert normalize_url(a) == normalize_url(b)

    @pytest.mark.parametrize("a, b", [
        # YouTube's `t` is a timestamp, not tracking.
        ("https://youtube.com/watch?v=xyz&t=60", "https://youtube.com/watch?v=xyz"),
        ("https://youtube.com/watch?v=one", "https://youtube.com/watch?v=two"),
        ("https://github.com/o/r?tab=readme", "https://github.com/o/r?tab=issues"),
        ("https://example.com/a", "https://example.com/b"),
    ])
    def test_different_pages_stay_different(self, a, b):
        assert normalize_url(a) != normalize_url(b)

    def test_non_url_passes_through(self):
        assert normalize_url("not a url") == "not a url"


def test_hash_suffix():
    assert hash_suffix("p/raw/20261010-Some-Title-8e3d02d3.md") == "8e3d02d3"
    assert hash_suffix("p/raw/index.md") is None


def _raw(s3, name, *, link="", body="body"):
    fm = Frontmatter(title=name, source=link, original_link=link)
    s3.upload(f"{P}raw/{name}", fm.to_yaml_block() + "\n" + body)


class TestRawIndex:
    def test_finds_by_hash_and_url_including_failed(self, s3):
        _raw(s3, "20261010-a-11111111.md", link="https://x.com/u/status/1?s=20")
        _raw(s3, ".failed/20261001-b-22222222.md", link="https://example.com/b")
        idx = RawIndex(s3)
        assert idx.by_hash("11111111").endswith("20261010-a-11111111.md")
        assert idx.by_hash("22222222")  # quarantined sources still count
        assert idx.by_url("https://twitter.com/u/status/1").endswith("a-11111111.md")
        assert idx.by_url("https://example.com/b/?utm_source=z")
        assert idx.by_url("https://example.com/other") is None


class TestClippingImport:
    """The loop: a clipping imported on one day comes back on another."""

    def _clip(self, s3, name, *, link, body):
        fm = Frontmatter(title="Awesome Widgets", source=link)
        s3.upload(f"{P}Clippings/{name}", fm.to_yaml_block() + "\n" + body)

    def _raws(self, s3):
        return s3.list_keys(sub_prefix="raw/", suffix=".md")

    def test_returning_clipping_with_same_body_is_discarded(self, s3, profile):
        body = "the list body"
        h = content_hash(body)[:8]
        # Imported on an earlier day: same hash, different date prefix.
        _raw(s3, f"20261010-Awesome-Widgets-{h}.md",
             link="https://github.com/example-org/Awesome_Widgets", body=body)
        self._clip(s3, "Awesome Widgets.md",
                   link="https://github.com/example-org/Awesome_Widgets", body=body)

        Compiler(profile=profile, s3=s3)._import_clippings()

        assert len(self._raws(s3)) == 1
        assert s3.list_keys(sub_prefix="Clippings/") == []  # clipping cleared

    def test_same_url_with_edited_body_is_discarded(self, s3, profile):
        _raw(s3, "20261010-Awesome-Widgets-aaaaaaaa.md",
             link="https://github.com/example-org/Awesome_Widgets")
        self._clip(s3, "Awesome Widgets.md",
                   link="https://github.com/example-org/Awesome_Widgets/",
                   body="slightly different stars count")

        Compiler(profile=profile, s3=s3)._import_clippings()

        assert len(self._raws(s3)) == 1

    def test_new_clipping_is_imported_once(self, s3, profile):
        self._clip(s3, "New.md", link="https://example.com/new", body="fresh")
        self._clip(s3, "New copy.md", link="https://example.com/new?fbclid=1", body="fresh")

        Compiler(profile=profile, s3=s3)._import_clippings()

        assert len(self._raws(s3)) == 1
        assert s3.list_keys(sub_prefix="Clippings/") == []


class TestUrlIngestDedup:
    def test_tracking_variant_is_recognised(self, s3, profile):
        _raw(s3, "20261012-agentusecases-33333333.md", link="https://agentusecases.ai/")
        pipe = IngestPipeline(profile=profile, s3=s3, tags=[])
        found = pipe._find_existing_by_url("https://agentusecases.ai/?fbclid=IwdGRjcAU6")
        assert found and found.endswith("agentusecases-33333333.md")

    def test_unrelated_url_is_not_matched(self, s3, profile):
        _raw(s3, "20261012-agentusecases-33333333.md", link="https://agentusecases.ai/")
        pipe = IngestPipeline(profile=profile, s3=s3, tags=[])
        assert pipe._find_existing_by_url("https://agentusecases.ai/pricing") is None


class TestPersistedUrlLookup:
    """URL dedup keeps a lookup in _ops/url-index.json keyed by ETag (F10): a
    later ingest downloads only raw files that are new or changed."""

    def _gets(self, s3):
        seen: list[str] = []
        s3._client.meta.events.register(
            "before-call.s3.GetObject", lambda params, **kw: seen.append(params["url_path"])
        )
        return seen

    def test_second_lookup_downloads_only_new_files(self, s3):
        for i in range(20):
            _raw(s3, f"20261010-p{i}-{i:08x}.md", link=f"https://example.com/p{i}")
        assert RawIndex(s3).by_url("https://example.com/p3")
        assert s3.exists(f"{P}_ops/url-index.json")

        _raw(s3, "20261011-new-abcdef12.md", link="https://example.com/new")
        gets = self._gets(s3)
        assert RawIndex(s3).by_url("https://example.com/new?utm_source=x")
        assert [g for g in gets if "/raw/" in g] == [f"/{P}raw/20261011-new-abcdef12.md"]
        assert len(gets) == 2  # the lookup file and the one new body

    def test_edited_raw_file_is_reindexed(self, s3):
        _raw(s3, "20261010-a-11111111.md", link="https://example.com/old")
        assert RawIndex(s3).by_url("https://example.com/old")
        _raw(s3, "20261010-a-11111111.md", link="https://example.com/moved")
        idx = RawIndex(s3)
        assert idx.by_url("https://example.com/moved")
        assert idx.by_url("https://example.com/old") is None

    def test_deleted_raw_file_is_forgotten(self, s3):
        _raw(s3, "20261010-a-11111111.md", link="https://example.com/a")
        assert RawIndex(s3).by_url("https://example.com/a")
        s3.delete(f"{P}raw/20261010-a-11111111.md")
        assert RawIndex(s3).by_url("https://example.com/a") is None

    def test_corrupt_lookup_file_is_rebuilt(self, s3):
        _raw(s3, "20261010-a-11111111.md", link="https://example.com/a")
        s3.upload(f"{P}_ops/url-index.json", "{not json")
        assert RawIndex(s3).by_url("https://example.com/a")

    def test_forged_entry_with_stale_etag_is_ignored(self, s3):
        import json

        _raw(s3, "20261010-a-11111111.md", link="https://example.com/a")
        s3.upload(f"{P}_ops/url-index.json", json.dumps({"version": 1, "entries": {
            f"{P}raw/20261010-a-11111111.md": {"etag": "stale",
                                                 "urls": ["https://example.com/forged"]},
        }}))
        idx = RawIndex(s3)
        assert idx.by_url("https://example.com/forged") is None
        assert idx.by_url("https://example.com/a")
