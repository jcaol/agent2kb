"""Tests for S3 client with prefix enforcement."""

import boto3
import pytest
from moto import mock_aws

from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client


@pytest.fixture
def s3_profile():
    return ProfileConfig(
        name="testproj",
        s3_endpoint="http://localhost:5000",  # moto endpoint
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="projects/testproj/",
    )


@pytest.fixture
def super_profile():
    return ProfileConfig(
        name="super",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="",
    )


@pytest.fixture
def mock_s3(s3_profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(s3_profile)
        # Override the client's internal boto3 with moto's
        client._client = conn
        yield client


@pytest.fixture
def mock_s3_super(super_profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(super_profile)
        client._client = conn
        yield client


class TestPrefixEnforcement:
    def test_allows_correct_prefix(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/test.md", "hello")
        content = mock_s3.download("projects/testproj/raw/test.md")
        assert content == "hello"

    def test_blocks_wrong_prefix(self, mock_s3):
        with pytest.raises(PermissionError, match="outside profile prefix"):
            mock_s3.upload("personal/raw/test.md", "hello")

    def test_blocks_meta_access(self, mock_s3):
        with pytest.raises(PermissionError):
            mock_s3.upload("_meta/test.md", "hello")

    def test_blocks_sibling_prefix(self, mock_s3):
        """projects/testproj/ must NOT match projects/testproj2/."""
        with pytest.raises(PermissionError, match="outside profile prefix"):
            mock_s3.upload("projects/testproj2/raw/test.md", "hello")

    def test_blocks_partial_prefix_match(self, mock_s3):
        """projects/testproj/ must NOT match projects/testproject/."""
        with pytest.raises(PermissionError, match="outside profile prefix"):
            mock_s3.upload("projects/testproject/raw/test.md", "hello")

    def test_blocks_path_traversal(self, mock_s3):
        with pytest.raises(PermissionError, match="path traversal"):
            mock_s3.upload("projects/testproj/../../personal/raw/x.md", "h")

    @pytest.mark.parametrize("key", [
        "projects//testproj/raw/x.md",
        "./projects/testproj/raw/x.md",
        "projects/./testproj/raw/x.md",
        "projects/testproj//raw/x.md",
        "projects/testproj/./raw/x.md",
        "projects/testproj/raw/.",
        "projects/testproj",
    ])
    def test_blocks_keys_that_only_normalise_into_the_prefix(self, mock_s3, key):
        """S3 does not normalise keys: the raw key must sit under the prefix."""
        with pytest.raises(PermissionError):
            mock_s3.upload(key, "hello")
        with pytest.raises(PermissionError):
            mock_s3.download(key)

    def test_listing_the_prefix_itself_is_allowed(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/a.md", "hello")
        assert mock_s3.list_keys() == ["projects/testproj/raw/a.md"]
        assert mock_s3.list_keys("raw/") == ["projects/testproj/raw/a.md"]

    def test_super_can_access_any_prefix(self, mock_s3_super):
        mock_s3_super.upload("personal/raw/test.md", "hello")
        mock_s3_super.upload("projects/a/raw/test.md", "hello")
        mock_s3_super.upload("_meta/schema.md", "schema")
        assert mock_s3_super.download("_meta/schema.md") == "schema"


class TestS3Operations:
    def test_upload_and_download(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/doc.md", "# Test\n\nHello world")
        content = mock_s3.download("projects/testproj/raw/doc.md")
        assert "Hello world" in content

    def test_exists(self, mock_s3):
        assert not mock_s3.exists("projects/testproj/raw/nope.md")
        mock_s3.upload("projects/testproj/raw/yes.md", "data")
        assert mock_s3.exists("projects/testproj/raw/yes.md")

    def test_list_keys(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/a.md", "a")
        mock_s3.upload("projects/testproj/raw/b.md", "b")
        mock_s3.upload("projects/testproj/wiki/c.md", "c")

        raw_keys = mock_s3.list_keys(sub_prefix="raw/", suffix=".md")
        assert len(raw_keys) == 2
        assert "projects/testproj/raw/a.md" in raw_keys

    def test_list_raw_files(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/a.md", "a")
        assert len(mock_s3.list_raw_files()) == 1

    def test_list_wiki_files(self, mock_s3):
        mock_s3.upload("projects/testproj/wiki/a.md", "a")
        assert len(mock_s3.list_wiki_files()) == 1

    def test_delete(self, mock_s3):
        mock_s3.upload("projects/testproj/raw/del.md", "data")
        assert mock_s3.exists("projects/testproj/raw/del.md")
        mock_s3.delete("projects/testproj/raw/del.md")
        assert not mock_s3.exists("projects/testproj/raw/del.md")


class TestListObjects:
    """`list_objects` exists so scan-heavy paths can skip downloads.

    A listing carries ETag, size and timestamp for free; fetching the same
    information one object at a time is what made compile and status slow.
    """

    def test_returns_etag_size_and_timestamp(self, mock_s3):
        mock_s3.upload(f"{mock_s3.prefix}wiki/a.md", "hello")
        objects = mock_s3.list_objects(sub_prefix="wiki/", suffix=".md")

        assert len(objects) == 1
        obj = objects[0]
        assert obj.key.endswith("wiki/a.md")
        assert obj.size == len("hello")
        assert obj.last_modified is not None
        # ETags must be unquoted so stored values compare cleanly.
        assert obj.etag and not obj.etag.startswith('"')

    def test_etag_changes_when_content_changes(self, mock_s3):
        mock_s3.upload(f"{mock_s3.prefix}wiki/a.md", "before")
        first = mock_s3.list_objects(sub_prefix="wiki/")[0].etag
        mock_s3.upload(f"{mock_s3.prefix}wiki/a.md", "after")
        second = mock_s3.list_objects(sub_prefix="wiki/")[0].etag
        assert first != second

    def test_suffix_filter_applies(self, mock_s3):
        mock_s3.upload(f"{mock_s3.prefix}wiki/a.md", "x")
        mock_s3.upload(f"{mock_s3.prefix}wiki/b.txt", "y")
        keys = [o.key for o in mock_s3.list_objects(sub_prefix="wiki/", suffix=".md")]
        assert len(keys) == 1
        assert keys[0].endswith("a.md")

    def test_prefix_enforcement_still_applies(self, mock_s3):
        with pytest.raises(PermissionError):
            mock_s3.list_objects(sub_prefix="../other/")


class TestDownloadMany:
    """Parallel fetch for the objects a scan actually needs."""

    def test_returns_all_contents(self, mock_s3):
        for i in range(25):
            mock_s3.upload(f"{mock_s3.prefix}wiki/p{i}.md", f"body {i}")
        keys = [o.key for o in mock_s3.list_objects(sub_prefix="wiki/")]

        bodies = mock_s3.download_many(keys)

        assert len(bodies) == 25
        assert bodies[f"{mock_s3.prefix}wiki/p7.md"] == "body 7"

    def test_empty_input_makes_no_calls(self, mock_s3):
        assert mock_s3.download_many([]) == {}

    def test_unreadable_key_is_skipped_not_raised(self, mock_s3):
        """One bad object must not abort a whole scan."""
        mock_s3.upload(f"{mock_s3.prefix}wiki/good.md", "ok")
        keys = [f"{mock_s3.prefix}wiki/good.md", f"{mock_s3.prefix}wiki/missing.md"]

        seen = []
        bodies = mock_s3.download_many(keys, on_error=lambda k, e: seen.append(k))

        assert list(bodies) == [f"{mock_s3.prefix}wiki/good.md"]
        assert seen == [f"{mock_s3.prefix}wiki/missing.md"]
