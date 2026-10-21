"""Tests for ingest pipeline and handlers."""

import tempfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agent2kb.ingest.base import IngestPipeline, build_frontmatter_content
from agent2kb.models import ContentType, ProfileConfig
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


class TestBuildFrontmatterContent:
    def test_basic(self):
        content = build_frontmatter_content(
            title="Test Title",
            body="Hello World",
            source="http://example.com",
            source_type=ContentType.URL,
            tags=["test"],
        )
        assert content.startswith("---\n")
        assert "title: Test Title" in content
        assert "Hello World" in content

    def test_with_link(self):
        content = build_frontmatter_content(
            title="Test",
            body="Body",
            source="http://example.com",
            source_type=ContentType.URL,
            original_link="http://example.com",
        )
        assert "original_link: http://example.com" in content


class TestMarkdownHandler:
    def test_ingest_markdown_file(self, profile, mock_s3):
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# My Notes\n\nSome content here.")
            f.flush()
            path = Path(f.name)

        pipeline = IngestPipeline(profile=profile, s3=mock_s3, tags=["notes"])
        result = pipeline.ingest_file(path)
        assert result.success
        assert result.raw_key.startswith("test/raw/")
        assert result.raw_key.endswith(".md")
        assert result.title == "My Notes"

        # Verify content was uploaded
        uploaded = mock_s3.download(result.raw_key)
        assert "My Notes" in uploaded
        assert "notes" in uploaded
        path.unlink()

    def test_ingest_text_file(self, profile, mock_s3):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("Plain text content")
            f.flush()
            path = Path(f.name)

        pipeline = IngestPipeline(profile=profile, s3=mock_s3)
        result = pipeline.ingest_file(path)
        assert result.success
        path.unlink()


class TestIngestFolder:
    def test_ingest_folder(self, profile, mock_s3):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "a.md").write_text("# File A\nContent A")
            (Path(tmpdir) / "b.txt").write_text("Content B")
            (Path(tmpdir) / ".hidden").write_text("hidden")  # should be skipped

            pipeline = IngestPipeline(profile=profile, s3=mock_s3)
            results = pipeline.ingest_folder(Path(tmpdir))
            successes = [r for r in results if r.success]
            assert len(successes) == 2  # .hidden skipped


class TestURLDetection:
    def test_auto_detect_video(self, profile, mock_s3):
        from agent2kb.utils import detect_url_type

        assert detect_url_type("https://youtube.com/watch?v=123") == "video"
        assert detect_url_type("https://example.com/page") == "url"


class TestReadabilityImagePreservation:
    """Regression test: the readability fallback path must preserve image
    markdown references so screenshots, diagrams, and Medium article
    images survive into raw/ and (via the compile schema guideline)
    into the wiki page.

    Before 0.6.4 the readability path called markdownify with
    strip=["img"], discarding every image.
    """

    def test_img_tags_become_markdown_image_references(self):
        from agent2kb.ingest.handlers.url import _try_readability

        # Standalone HTML so readability doesn't need to do much
        html = """
        <html><head><title>Article</title></head>
        <body>
          <article>
            <h1>A post about things</h1>
            <p>First paragraph with content.</p>
            <img src="https://cdn.example.com/hero.png" alt="hero diagram"/>
            <p>Second paragraph.</p>
            <img src="https://cdn.example.com/chart.jpg"/>
            <p>Closing thoughts.</p>
          </article>
        </body></html>
        """
        title, md = _try_readability(html, "https://example.com/post")
        # Both image URLs should be present as markdown image references
        assert "![hero diagram](https://cdn.example.com/hero.png)" in md or \
               "https://cdn.example.com/hero.png" in md
        assert "https://cdn.example.com/chart.jpg" in md
        # The actual text content should still be there
        assert "First paragraph" in md
        assert "Second paragraph" in md
