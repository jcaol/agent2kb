"""The first-run tutorial (docs/first-run.md) promises a raw file name for its sample."""

import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agent2kb.ingest.base import IngestPipeline
from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.s3_client import S3Client

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "first-note.md"
TUTORIAL = ROOT / "docs" / "first-run.md"
RAW_NAME = re.compile(r"^kbs/notes/raw/\d{8}-Alpha-Project-Facts-[0-9a-f]{8}\.md$")


@pytest.fixture
def s3():
    profile = ProfileConfig(
        name="notes",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="kb-notes",
        prefix="kbs/notes/",
    )
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="kb-notes")
        client = S3Client(profile)
        client._client = conn
        yield profile, client


def test_sample_ingests_under_the_documented_name(s3):
    profile, client = s3
    result = IngestPipeline(profile=profile, s3=client).ingest_file(SAMPLE)
    assert result.success, result.error
    assert RAW_NAME.match(result.raw_key), result.raw_key
    fm, body = Frontmatter.from_yaml_block(client.download(result.raw_key))
    assert fm.title == "Alpha Project Facts"
    assert "Lantern" in body


def test_tutorial_names_the_same_pattern():
    text = TUTORIAL.read_text()
    assert "examples/first-note.md" in text
    assert "YYYYMMDD-Alpha-Project-Facts-" in text
