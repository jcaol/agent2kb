"""`a2kb status` wording."""

from agent2kb.models import LLMProvider
from agent2kb.status import _cloud_fallback_note


def test_cloud_fallback_is_a_statement_per_provider():
    local = _cloud_fallback_note(LLMProvider.OLLAMA)
    assert local.startswith("off") and "--allow-cloud-fallback" in local
    assert "fallback CLI" in _cloud_fallback_note(LLMProvider.CLI)
    assert "already a cloud" in _cloud_fallback_note(LLMProvider.GROQ)
    assert not any(
        _cloud_fallback_note(p).startswith("Check") for p in LLMProvider
    )


def _status_text(**profile_kw) -> str:
    from io import StringIO

    import boto3
    from moto import mock_aws
    from rich.console import Console

    from agent2kb.models import ProfileConfig
    from agent2kb.s3_client import S3Client
    from agent2kb.status import show_status

    profile = ProfileConfig(
        name="test", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="test-bucket", prefix="test/", **profile_kw,
    )
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        buf = StringIO()
        show_status(profile, client, Console(file=buf, width=200))
    return " ".join(buf.getvalue().replace("│", " ").split())


def test_same_cli_fallback_is_reported_as_ignored():
    text = _status_text(
        compile_provider="cli", cli_command="claude", cli_fallback_command="claude"
    )
    assert "CLI Fallback ignored: same CLI as the primary" in text


def test_different_cli_fallback_is_listed_and_unset_is_disabled():
    text = _status_text(
        compile_provider="cli", cli_command="claude", cli_fallback_command="codex"
    )
    assert "CLI Fallback codex:" in text and "ignored" not in text
    text = _status_text(compile_provider="cli", cli_command="claude", cli_fallback_command="")
    assert "CLI Fallback (disabled)" in text


def test_whole_bucket_kb_counts_up_to_date_sources():
    """An empty-prefix admin KB maps raw/x.md to wiki/x.md, not onto itself."""
    from io import StringIO

    import boto3
    from moto import mock_aws
    from rich.console import Console

    from agent2kb.models import Frontmatter, ProfileConfig
    from agent2kb.s3_client import S3Client
    from agent2kb.status import show_status
    from agent2kb.utils import content_hash

    profile = ProfileConfig(
        name="admin", role="admin", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="test-bucket", prefix="",
    )
    raw = "---\ntitle: A\n---\n\nbody\n"
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        client.upload("raw/a.md", raw)
        client.upload("raw/.failed/b.md", raw)
        wiki = Frontmatter(title="A", raw_content_hash=content_hash(raw)).to_yaml_block()
        client.upload("wiki/a.md", wiki + "\ncompiled\n")
        buf = StringIO()
        show_status(profile, client, Console(file=buf, width=200))
    text = " ".join(buf.getvalue().replace("│", " ").split())
    assert "Raw files 1" in text
    assert "Needs compile 0" in text
