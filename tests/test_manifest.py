"""Tests for agent2kb.manifest — per-profile page manifest (Item 6)."""

import json

import boto3
import pytest
from moto import mock_aws

from agent2kb import manifest as mf
from agent2kb.models import Frontmatter, ProfileConfig, SourceKind
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


def _make_page(
    mock_s3,
    key: str,
    title: str,
    *,
    kind: SourceKind | None = None,
    body: str = "",
) -> str:
    fm = Frontmatter(title=title, source="x", source_kind=kind)
    content = fm.to_yaml_block() + (body or f"\n# {title}\n\nbody\n")
    mock_s3.upload(key, content)
    return content


class TestManifestRebuild:
    def test_rebuild_walks_wiki_and_outputs(self, profile, mock_s3):
        _make_page(mock_s3, "test/wiki/20260411-a.md", "A")
        _make_page(mock_s3, "test/wiki/20260412-b.md", "B")
        _make_page(
            mock_s3,
            "test/outputs/20260413-120000-q.md",
            "Some question",
            kind=SourceKind.QUERY_OUTPUT,
        )

        m = mf.rebuild(mock_s3, profile)
        assert set(m["entries"].keys()) == {
            "test/wiki/20260411-a.md",
            "test/wiki/20260412-b.md",
            "test/outputs/20260413-120000-q.md",
        }
        # outputs/ entry is correctly classified as query_output
        assert (
            m["entries"]["test/outputs/20260413-120000-q.md"]["kind"]
            == "query_output"
        )
        # Persisted to S3
        raw = mock_s3.download("test/_meta/manifest.json")
        on_disk = json.loads(raw)
        assert on_disk["version"] == mf.MANIFEST_VERSION
        assert len(on_disk["entries"]) == 3

    def test_rebuild_skips_outputs_when_disabled(self, profile, mock_s3):
        _make_page(mock_s3, "test/wiki/a.md", "A")
        _make_page(
            mock_s3, "test/outputs/q.md", "Q", kind=SourceKind.QUERY_OUTPUT
        )
        m = mf.rebuild(mock_s3, profile, include_outputs=False)
        assert "test/wiki/a.md" in m["entries"]
        assert "test/outputs/q.md" not in m["entries"]

    def test_entry_carries_metadata_for_fast_lookup(self, profile, mock_s3):
        body = "\n# Foo\n\nSee [[Bar]] and [[Baz]] and [[Qux]].\n"
        _make_page(mock_s3, "test/wiki/foo.md", "Foo", body=body)
        m = mf.rebuild(mock_s3, profile)
        entry = m["entries"]["test/wiki/foo.md"]
        assert entry["title"] == "Foo"
        assert entry["wikilink_count"] == 3
        assert entry["hash"].startswith("sha256:")
        assert entry["kind"] == "article"  # inferred from path


class TestManifestLoad:
    def test_load_returns_none_when_missing(self, profile, mock_s3):
        assert mf.load(mock_s3, profile) is None

    def test_load_rejects_wrong_version(self, profile, mock_s3):
        mock_s3.upload(
            mf.manifest_key_for(profile),
            json.dumps({"version": 999, "entries": {}}),
        )
        assert mf.load(mock_s3, profile) is None

    def test_load_returns_parsed_dict(self, profile, mock_s3):
        _make_page(mock_s3, "test/wiki/a.md", "A")
        mf.rebuild(mock_s3, profile)
        m = mf.load(mock_s3, profile)
        assert m is not None
        assert "test/wiki/a.md" in m["entries"]


class TestUpdateEntry:
    def test_update_refreshes_existing_entry(self, profile, mock_s3):
        _make_page(mock_s3, "test/wiki/a.md", "A")
        mf.rebuild(mock_s3, profile)

        # Now overwrite the page with new content and update the entry
        new_content = _make_page(mock_s3, "test/wiki/a.md", "A v2")
        mf.update_entry(mock_s3, profile, "test/wiki/a.md", new_content)

        m = mf.load(mock_s3, profile)
        assert m["entries"]["test/wiki/a.md"]["title"] == "A v2"

    def test_update_is_no_op_when_manifest_missing(self, profile, mock_s3):
        """No manifest → update is a silent no-op (caller is in a hot loop)."""
        content = _make_page(mock_s3, "test/wiki/a.md", "A")
        mf.update_entry(mock_s3, profile, "test/wiki/a.md", content)
        # No manifest was created.
        assert mf.load(mock_s3, profile) is None


class TestRemoveEntry:
    def test_remove_drops_key(self, profile, mock_s3):
        _make_page(mock_s3, "test/wiki/a.md", "A")
        _make_page(mock_s3, "test/wiki/b.md", "B")
        mf.rebuild(mock_s3, profile)

        mf.remove_entry(mock_s3, profile, "test/wiki/a.md")
        m = mf.load(mock_s3, profile)
        assert "test/wiki/a.md" not in m["entries"]
        assert "test/wiki/b.md" in m["entries"]
