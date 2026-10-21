"""Tests for wiki linter."""

import boto3
import pytest
from moto import mock_aws

from agent2kb.lint import WikiLinter
from agent2kb.models import Frontmatter, ProfileConfig
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


class TestWikiLinter:
    def test_no_issues_for_good_page(self, profile, mock_s3):
        fm = Frontmatter(title="Good Page", source="test", date="2024-01-01")
        body = "\n# Good Page\n\n> Source: [test](test)\n\nSome content here."
        mock_s3.upload("test/wiki/good-page.md", fm.to_yaml_block() + body)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert len(issues) == 0

    def test_article_missing_visible_source_line(self, profile, mock_s3):
        """Article pages with frontmatter source must surface it in body."""
        fm = Frontmatter(
            title="Article",
            source="https://example.com/post",
            original_link="https://example.com/post",
            date="2024-01-01",
        )
        # Body has heading + content but NO visible > Source: line
        body = "\n# Article\n\nSome content without source line."
        mock_s3.upload("test/wiki/article.md", fm.to_yaml_block() + body)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert any("missing visible source line" in i for i in issues)

    def test_concept_pages_exempt_from_visible_source_rule(self, profile, mock_s3):
        """Concept pages under wiki/concepts/ are synthesized, not article sources."""
        fm = Frontmatter(title="LLM", source="", date="2024-01-01")
        body = "\n# LLM\n\nSynthesized concept content."
        mock_s3.upload("test/wiki/concepts/llm.md", fm.to_yaml_block() + body)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert not any("missing visible source line" in i for i in issues)

    def test_index_log_exempt_from_visible_source_rule(self, profile, mock_s3):
        """index.md and log.md are meta pages without source provenance."""
        fm = Frontmatter(title="Index", source="", date="2024-01-01")
        mock_s3.upload("test/wiki/index.md", fm.to_yaml_block() + "\n# Index\n")
        mock_s3.upload("test/wiki/log.md", fm.to_yaml_block() + "\n# Log\n")

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert not any("missing visible source line" in i for i in issues)

    def test_concept_page_no_source_field_is_clean(self, profile, mock_s3):
        """Concept pages are synthesized from articles — no inherent source."""
        fm = Frontmatter(title="LLM", source="", date="2024-01-01")
        # Concept body uses **bold** section labels, not # headings
        body = (
            "\n**LLM**\n\n"
            "**Definition**\n"
            "LLM is a large language model.\n\n"
            "**Key Points**\n"
            "- point 1\n"
        )
        mock_s3.upload("test/wiki/concepts/llm.md", fm.to_yaml_block() + body)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        # No missing-source issue (concepts exempt from Check 1's source rule)
        assert not any(
            "concepts/llm.md: missing source in frontmatter" in i for i in issues
        )
        # No no-headings issue (concepts exempt from Check 4)
        assert not any(
            "concepts/llm.md: no headings found" in i for i in issues
        )
        # No missing-visible-source-line issue (concepts exempt from Check 5)
        assert not any(
            "concepts/llm.md: missing visible source line" in i for i in issues
        )

    def test_index_log_no_source_field_is_clean(self, profile, mock_s3):
        """index.md and log.md are meta files — no source field required."""
        fm = Frontmatter(title="sample KB", source="", date="2024-01-01")
        mock_s3.upload("test/wiki/index.md", fm.to_yaml_block() + "\n# sample KB\n")
        mock_s3.upload("test/wiki/log.md", fm.to_yaml_block() + "\n# Compile log\n")

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert not any(
            "index.md: missing source in frontmatter" in i for i in issues
        )
        assert not any("log.md: missing source in frontmatter" in i for i in issues)

    def test_article_still_requires_source(self, profile, mock_s3):
        """Articles must still be flagged when they lack a frontmatter source."""
        fm = Frontmatter(title="Article", source="", date="2024-01-01")
        body = "\n# Article\n\nBody content.\n"
        mock_s3.upload("test/wiki/20260411-article-abcd1234.md", fm.to_yaml_block() + body)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert any("missing source in frontmatter" in i for i in issues)

    def test_missing_title(self, profile, mock_s3):
        fm = Frontmatter(source="test", date="2024-01-01")
        content = fm.to_yaml_block() + "\n# Some Content"
        mock_s3.upload("test/wiki/no-title.md", content)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert any("missing title" in i for i in issues)

    def test_unresolved_wikilink_is_not_error(self, profile, mock_s3):
        """Unresolved wikilinks are normal (Obsidian ghost nodes), not lint errors."""
        fm = Frontmatter(title="Page A", source="test", date="2024-01-01")
        content = fm.to_yaml_block() + "\n# Page A\n\nSee [[Nonexistent Page]]."
        mock_s3.upload("test/wiki/page-a.md", content)

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert not any("wikilink" in i for i in issues)

    def test_valid_wikilink(self, profile, mock_s3):
        fm_a = Frontmatter(title="Page A", source="test", date="2024-01-01")
        fm_b = Frontmatter(title="Page B", source="test", date="2024-01-01")
        body_a = "\n# Page A\n\nSee [[Page B]]."
        mock_s3.upload("test/wiki/page-a.md", fm_a.to_yaml_block() + body_a)
        mock_s3.upload("test/wiki/page-b.md", fm_b.to_yaml_block() + "\n# Page B\n\nContent.")

        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        broken_links = [i for i in issues if "broken wikilink" in i]
        assert len(broken_links) == 0

    def test_empty_wiki(self, profile, mock_s3):
        linter = WikiLinter(profile=profile, s3=mock_s3)
        issues = linter.lint()
        assert len(issues) == 0
