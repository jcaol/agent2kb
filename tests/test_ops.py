"""Tests for agent2kb.ops — structured run summaries (Item 7)."""

import json

import boto3
import pytest
from moto import mock_aws

from agent2kb.models import CompileResult, ProfileConfig
from agent2kb.ops import (
    classify_error,
    is_transient,
    summarize_query_failure,
    write_compile_run,
)
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


class TestClassifyError:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("Rate limit exceeded", "rate_limit"),
            ("HTTP 429 Too Many Requests", "rate_limit"),
            ("Daily quota reached", "rate_limit"),
            ("Invalid API key", "auth"),
            ("HTTP 401 unauthorized", "auth"),
            ("HTTP 403 forbidden", "auth"),
            ("Content too large for context window", "content_too_large"),
            ("token limit exceeded", "content_too_large"),
            ("Connection timeout", "network"),
            ("DNS resolution failed", "network"),
            ("Response blocked by safety filter", "provider_blocked"),
            ("Empty response from model", "empty_response"),
            ("Some random error", "other"),
            ("", "other"),
        ],
    )
    def test_taxonomy(self, msg, expected):
        assert classify_error(msg) == expected

    def test_transient(self):
        assert is_transient("Rate limit hit") is True
        assert is_transient("Connection timeout") is True
        assert is_transient("Invalid API key") is False
        assert is_transient("Content too large") is False


class TestWriteCompileRun:
    def test_writes_summary_with_failures_classified(self, profile, mock_s3):
        results = [
            CompileResult(
                source_key="test/raw/a.md",
                wiki_key="test/wiki/a.md",
                title="a",
                content="ok",
                success=True,
                duration_sec=2.5,
                tokens_in=100,
                tokens_out=200,
            ),
            CompileResult(
                source_key="test/raw/b.md",
                wiki_key="",
                title="",
                content="",
                success=False,
                error="Rate limit exceeded — retry in 30s",
                duration_sec=0.3,
            ),
            CompileResult(
                source_key="test/raw/c.md",
                wiki_key="",
                title="",
                content="",
                success=False,
                error="Content too large for context",
                duration_sec=0.5,
            ),
        ]

        key = write_compile_run(
            mock_s3,
            profile,
            results,
            provider="gemini",
            model="gemini-2.5-flash",
            new_only=True,
            allow_cloud_fallback=False,
        )

        assert key is not None
        assert key.startswith("test/_ops/compile-runs/")
        assert key.endswith(".json")

        summary = json.loads(mock_s3.download(key))
        assert summary["profile"] == "test"
        assert summary["provider"] == "gemini"
        assert summary["model"] == "gemini-2.5-flash"
        assert summary["new_only"] is True
        assert summary["totals"]["files"] == 3
        assert summary["totals"]["ok"] == 1
        assert summary["totals"]["failed"] == 2
        assert summary["totals"]["tokens_in"] == 100
        assert summary["totals"]["tokens_out"] == 200

        # Failures carry their classification so dashboards can group.
        kinds = {f["source_key"]: f["error_class"] for f in summary["failures"]}
        assert kinds["test/raw/b.md"] == "rate_limit"
        assert kinds["test/raw/c.md"] == "content_too_large"
        transient = {f["source_key"]: f["transient"] for f in summary["failures"]}
        assert transient["test/raw/b.md"] is True
        assert transient["test/raw/c.md"] is False

    def test_empty_results_still_writes(self, profile, mock_s3):
        """A no-op run (nothing to compile) still writes a heartbeat."""
        key = write_compile_run(
            mock_s3,
            profile,
            [],
            provider="ollama",
            model=None,
            new_only=True,
            allow_cloud_fallback=False,
        )
        assert key is not None
        summary = json.loads(mock_s3.download(key))
        assert summary["totals"]["files"] == 0


class TestSummarizeQueryFailure:
    def test_known_reasons_pass_through(self):
        assert summarize_query_failure("no_hits")["reason"] == "no_hits"
        assert (
            summarize_query_failure("llm_answer_failure", "timeout")["reason"]
            == "llm_answer_failure"
        )

    def test_unknown_reason_normalizes_to_other(self):
        assert summarize_query_failure("banana")["reason"] == "other"
