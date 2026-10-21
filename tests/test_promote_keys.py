"""Promote must never replace an existing outputs/ file (same second + slug)."""

from __future__ import annotations

import datetime as real_dt
import re
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb import outputs as _outputs
from agent2kb.models import ProfileConfig
from agent2kb.promote import promote
from agent2kb.s3_client import PreconditionFailed, S3Client

P = "notes/"


@pytest.fixture
def profile():
    return ProfileConfig(
        name="notes", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="test-bucket", prefix=P,
    )


@pytest.fixture
def s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


def _note(tmp_path, name: str, body: str):
    f = tmp_path / name
    f.write_text(f"# Alpha notes\n\n{body}\n", encoding="utf-8")
    return f


@pytest.mark.parametrize("mode,sub", [("outputs", "outputs/"), ("weekly", "outputs/weekly/")])
def test_same_slug_in_the_same_second_gets_two_files(profile, s3, tmp_path, mode, sub):
    first_file = _note(tmp_path, "a.md", "one")
    second_file = _note(tmp_path, "b.md", "two")
    with patch("agent2kb.promote.datetime") as dt:
        dt.datetime.now.return_value = real_dt.datetime(2026, 10, 19, 12, 0, 0)
        first = promote(profile, s3, first_file, mode=mode)
        second = promote(profile, s3, second_file, mode=mode)
    assert first != second
    assert first.startswith(P + sub) and second.startswith(P + sub)
    assert "one" in s3.download(first) and "two" in s3.download(second)


def test_key_keeps_the_timestamp_slug_prefix(profile, s3, tmp_path):
    key = promote(profile, s3, _note(tmp_path, "a.md", "one"), mode="outputs")
    name = key.rsplit("/", 1)[1]
    assert re.fullmatch(r"\d{8}-\d{6}-Alpha-notes-[0-9a-f]{8}\.md", name), name
    assert _outputs._parse_stamp(name) is not None


def test_create_uses_if_none_match_and_retries_on_a_taken_key(profile, s3, tmp_path):
    real = s3.upload
    calls = []

    def upload(key, body, *a, **kw):
        calls.append((key, kw.get("if_none_match")))
        if len(calls) == 1:
            raise PreconditionFailed(key)
        return real(key, body, *a, **kw)

    with patch.object(s3, "upload", side_effect=upload):
        key = promote(profile, s3, _note(tmp_path, "a.md", "one"), mode="outputs")
    assert len(calls) == 2 and calls[0][0] != calls[1][0] and key == calls[1][0]
    assert all(flag is True for _, flag in calls)


def test_wiki_topic_keeps_its_slug_key_and_updates_in_place(profile, s3, tmp_path):
    f = _note(tmp_path, "a.md", "one")
    first = promote(profile, s3, f, mode="wiki-topic", slug_override="alpha")
    f.write_text("# Alpha notes\n\ntwo\n", encoding="utf-8")
    second = promote(profile, s3, f, mode="wiki-topic", slug_override="alpha")
    assert first == second == f"{P}wiki/topics/alpha.md"
    assert "two" in s3.download(first)
