"""Tests for the weekly-digest promote mode (0.16.0)."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agent2kb.models import Frontmatter, ProfileConfig, SourceKind
from agent2kb.promote import promote
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


@pytest.fixture
def digest_file(tmp_path) -> Path:
    f = tmp_path / "kb-digest-W20.md"
    f.write_text(
        "# KB Digest — Week 20, 2026\n\n"
        "## Headline\n\n"
        "Big week for [[redis-cluster|Redis Cluster]] discussion.\n\n"
        "## Top entities\n\n"
        "- [[redis-cluster]] — 12 mentions\n"
        "- [[claude-code]] — 6 mentions\n",
        encoding="utf-8",
    )
    return f


class TestWeeklyMode:
    def test_destination_under_outputs_weekly(self, profile, mock_s3, digest_file):
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="agent",
        )
        # outputs/weekly/<stamp>-<slug>.md under the profile prefix
        assert dest.startswith("test/outputs/weekly/")
        assert dest.endswith(".md")

    def test_source_kind_is_digest(self, profile, mock_s3, digest_file):
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="agent",
        )
        raw = mock_s3.download(dest)
        fm, _ = Frontmatter.from_yaml_block(raw)
        assert fm.source_kind == SourceKind.DIGEST

    def test_source_meta_records_period_and_actor(
        self, profile, mock_s3, digest_file
    ):
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="agent",
        )
        raw = mock_s3.download(dest)
        fm, _ = Frontmatter.from_yaml_block(raw)
        assert fm.source_meta.get("period") == "weekly"
        assert fm.source_meta.get("actor") == "agent"

    def test_tags_include_digest_and_weekly(self, profile, mock_s3, digest_file):
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="agent",
        )
        raw = mock_s3.download(dest)
        fm, _ = Frontmatter.from_yaml_block(raw)
        assert "digest" in fm.tags
        assert "weekly" in fm.tags

    def test_body_preserved_verbatim(self, profile, mock_s3, digest_file):
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="agent",
        )
        raw = mock_s3.download(dest)
        _, body = Frontmatter.from_yaml_block(raw)
        assert "Big week for" in body
        assert "12 mentions" in body
        assert "[[redis-cluster|Redis Cluster]]" in body

    def test_user_actor_recorded_when_human_promotes(
        self, profile, mock_s3, digest_file
    ):
        """A human running a2kb promote --as weekly should be tagged correctly."""
        dest = promote(
            profile=profile,
            s3=mock_s3,
            file_path=digest_file,
            mode="weekly",
            actor="user",
        )
        raw = mock_s3.download(dest)
        fm, _ = Frontmatter.from_yaml_block(raw)
        assert fm.source_meta.get("actor") == "user"


class TestInferKindFromKey:
    """outputs/weekly/ path should infer SourceKind.DIGEST, not QUERY_OUTPUT."""

    def test_weekly_subfolder_recognized(self):
        assert (
            Frontmatter.infer_kind_from_key(
                "test/outputs/weekly/20260516-100000-digest.md"
            )
            == SourceKind.DIGEST
        )

    def test_monthly_subfolder_also_recognized(self):
        """Forward-compat: outputs/monthly/ would also be digest territory."""
        assert (
            Frontmatter.infer_kind_from_key(
                "test/outputs/monthly/20260101-100000-month.md"
            )
            == SourceKind.DIGEST
        )

    def test_outputs_root_still_query_output(self):
        assert (
            Frontmatter.infer_kind_from_key(
                "test/outputs/20260516-100000-q.md"
            )
            == SourceKind.QUERY_OUTPUT
        )

    def test_outputs_agent_still_query_output(self):
        """outputs/agent/ is agent-actor saves, NOT digests."""
        assert (
            Frontmatter.infer_kind_from_key(
                "test/outputs/agent/20260516-100000-q.md"
            )
            == SourceKind.QUERY_OUTPUT
        )
