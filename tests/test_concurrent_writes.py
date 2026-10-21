"""Concurrent writers must not silently drop each other's work (F11).

The review's fixture: two agent saves with the same hit set both read the
existing capture before either writes. Both used to report success while
the later write erased the other follow-up.
"""

from __future__ import annotations

import json
import re
import threading
import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from agent2kb import manifest as _manifest
from agent2kb import outputs as _outputs
from agent2kb.compile.compiler import Compiler
from agent2kb.embeddings import EmbeddingClient, EmbeddingIndex
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.query import QueryEngine, SearchHit
from agent2kb.s3_client import PreconditionFailed, S3Client

P = "notes/"
HITS = [f"{P}wiki/alpha.md", f"{P}wiki/beta.md"]


@pytest.fixture
def profile():
    return ProfileConfig(
        name="notes", s3_endpoint="http://localhost:5000", access_key="testing",
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


def _engine(profile, s3) -> QueryEngine:
    engine = QueryEngine(profile=profile, s3=s3)
    engine.last_hits = [SearchHit(key=k, title=k, score=1.0, snippet="", body="") for k in HITS]
    return engine


def _sync_first_reads(s3: S3Client, parties: int = 2):
    """Make the first ``download_with_etag`` of each thread wait for the others,
    so every writer reads the same version before any of them writes."""
    barrier = threading.Barrier(parties, timeout=10)
    local = threading.local()
    real = s3.download_with_etag

    def synced(key):
        result = real(key)
        if not getattr(local, "done", False):
            local.done = True
            barrier.wait()
        return result

    return patch.object(s3, "download_with_etag", side_effect=synced)


def _run_threads(*targets):
    results: dict[int, object] = {}
    errors: list[BaseException] = []

    def wrap(i, fn):
        try:
            results[i] = fn()
        except BaseException as e:  # noqa: BLE001 - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    return [results[i] for i in range(len(targets))]


def _commit_then_412(s3: S3Client, times: int = 1):
    """put_object that stores the object, then answers 412 the way botocore's
    retry of a conditional PUT whose response was lost does."""
    real = s3._client.put_object
    left = [times]

    def put(**kwargs):
        response = real(**kwargs)
        if left[0] and ("IfMatch" in kwargs or "IfNoneMatch" in kwargs):
            left[0] -= 1
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "x"}},
                              "PutObject")
        return response

    return patch.object(s3._client, "put_object", side_effect=put)


# ── conditional writes in s3_client ─────────────────────────────────


class TestConditionalUpload:
    def test_upload_returns_etag(self, s3):
        etag = s3.upload(f"{P}a.md", "one")
        assert etag and s3.download_with_etag(f"{P}a.md") == ("one", etag)

    def test_if_match_rejects_a_changed_object(self, s3):
        stale = s3.upload(f"{P}a.md", "one")
        s3.upload(f"{P}a.md", "two")
        with pytest.raises(PreconditionFailed):
            s3.upload(f"{P}a.md", "three", if_match=stale)
        assert s3.download(f"{P}a.md") == "two"

    def test_if_match_rejects_a_deleted_object(self, s3):
        etag = s3.upload(f"{P}a.md", "one")
        s3.delete(f"{P}a.md")
        with pytest.raises(PreconditionFailed):
            s3.upload(f"{P}a.md", "two", if_match=etag)

    def test_if_none_match_refuses_to_replace(self, s3):
        s3.upload(f"{P}a.md", "one")
        with pytest.raises(PreconditionFailed):
            s3.upload(f"{P}a.md", "two", if_none_match=True)
        assert s3.download(f"{P}a.md") == "one"

    def test_conditional_writes_still_enforce_the_prefix(self, s3):
        with pytest.raises(PermissionError):
            s3.upload("other/a.md", "x", if_none_match=True)

    @pytest.mark.parametrize("code,message", [
        ("NotImplemented", "no"),
        ("InvalidArgument", "Unsupported header 'If-None-Match' received"),
        ("InvalidRequest", "Conditional writes are not supported"),
    ])
    def test_provider_without_conditional_writes_falls_back(self, s3, code, message):
        real = s3._client.put_object
        seen = []

        def put(**kwargs):
            seen.append(sorted(k for k in kwargs if k.startswith("If")))
            if "IfMatch" in kwargs or "IfNoneMatch" in kwargs:
                raise ClientError({"Error": {"Code": code, "Message": message}}, "PutObject")
            return real(**kwargs)

        with patch.object(s3._client, "put_object", side_effect=put):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
            s3.upload(f"{P}a.md", "two", if_match="whatever")
        assert s3.download(f"{P}a.md") == "two"
        assert s3.conditional_writes is False
        # Detected once; the second write goes straight to a plain PUT.
        assert seen == [["IfNoneMatch"], [], []]

    def test_http_501_falls_back(self, s3):
        real = s3._client.put_object

        def put(**kwargs):
            if "IfNoneMatch" in kwargs:
                raise ClientError({"Error": {"Code": "Unknown", "Message": ""},
                                   "ResponseMetadata": {"HTTPStatusCode": 501}}, "PutObject")
            return real(**kwargs)

        with patch.object(s3._client, "put_object", side_effect=put):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
        assert s3.conditional_writes is False

    def test_old_botocore_without_the_parameters_falls_back(self, s3):
        from botocore.exceptions import ParamValidationError

        real = s3._client.put_object

        def put(**kwargs):
            if "IfNoneMatch" in kwargs:
                raise ParamValidationError(
                    report='Unknown parameter in input: "IfNoneMatch", must be one of: ACL')
            return real(**kwargs)

        with patch.object(s3._client, "put_object", side_effect=put):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
        assert s3.conditional_writes is False

    @pytest.mark.parametrize("code,message", [
        ("InvalidArgument", "Invalid argument"),
        ("InvalidRequest", "Content-MD5 does not match"),
    ])
    def test_unrelated_invalid_request_keeps_conditional_writes(self, s3, code, message):
        def put(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": message}}, "PutObject")

        with patch.object(s3._client, "put_object", side_effect=put), \
                pytest.raises(ClientError):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
        assert s3.conditional_writes is True

    def test_other_param_validation_errors_are_not_swallowed(self, s3):
        from botocore.exceptions import ParamValidationError

        def put(**kwargs):
            raise ParamValidationError(report='Invalid type for parameter Body')

        with patch.object(s3._client, "put_object", side_effect=put), \
                pytest.raises(ParamValidationError):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
        assert s3.conditional_writes is True

    def test_other_errors_are_not_swallowed(self, s3):
        def put(**kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")

        with patch.object(s3._client, "put_object", side_effect=put), \
                pytest.raises(ClientError):
            s3.upload(f"{P}a.md", "one", if_none_match=True)
        assert s3.conditional_writes is True


# ── agent capture merges ────────────────────────────────────────────


class TestConcurrentAgentSaves:
    def _seed(self, profile, s3) -> str:
        return _engine(profile, s3).save_output("what is alpha", "first answer", actor="agent")

    def _body(self, s3, key):
        return Frontmatter.from_yaml_block(s3.download(key))

    def test_two_writers_reading_the_same_version_both_survive(self, profile, s3):
        """The review's two-thread fixture, run below the in-process lock as
        two processes would: both read before either writes."""
        key = self._seed(profile, s3)
        a, b = _engine(profile, s3), _engine(profile, s3)
        with _sync_first_reads(s3):
            got = _run_threads(
                lambda: a._try_dedup_agent_save("features of alpha", "answer A"),
                lambda: b._try_dedup_agent_save("install alpha", "answer B"),
            )
        assert got == [key, key]
        fm, body = self._body(s3, key)
        assert "answer A" in body and "answer B" in body and "first answer" in body
        assert sorted(fm.source_meta["also_asked"]) == ["features of alpha", "install alpha"]

    def test_in_process_lock_covers_servers_without_conditional_writes(self, profile, s3):
        key = self._seed(profile, s3)
        s3.conditional_writes = False  # plain PUTs: only the lock protects the merge
        real = s3.download_with_etag

        def slow(k):
            result = real(k)
            time.sleep(0.2)  # widen the read-modify-write window
            return result

        a, b = _engine(profile, s3), _engine(profile, s3)
        with patch.object(s3, "download_with_etag", side_effect=slow):
            got = _run_threads(
                lambda: a.save_output("features of alpha", "answer A", actor="agent"),
                lambda: b.save_output("install alpha", "answer B", actor="agent"),
            )
        assert got == [key, key]
        _, body = self._body(s3, key)
        assert "answer A" in body and "answer B" in body

    def test_merge_that_keeps_losing_saves_separately(self, profile, s3):
        key = self._seed(profile, s3)
        engine = _engine(profile, s3)
        with patch.object(s3, "upload", side_effect=[PreconditionFailed(key)] * 5 + [""]):
            new_key = engine.save_output("install alpha", "answer B", actor="agent")
        assert new_key != key  # written as a new capture instead of being dropped


# ── unique capture keys ─────────────────────────────────────────────


class TestCaptureKeys:
    def test_same_question_in_the_same_second_gets_two_files(self, profile, s3):
        engine = _engine(profile, s3)
        with patch("agent2kb.query.datetime") as dt:
            import datetime as real_dt

            dt.datetime.now.return_value = real_dt.datetime(2026, 10, 19, 12, 0, 0)
            dt.timedelta = real_dt.timedelta
            dt.timezone = real_dt.timezone
            first = engine.save_output("what is alpha", "one", actor="user")
            second = engine.save_output("what is alpha", "two", actor="user")
        assert first != second
        assert {s3.download(first).endswith("one\n"), s3.download(second).endswith("two\n")} == {
            True}

    def test_key_keeps_the_timestamp_slug_prefix(self, profile, s3):
        key = _engine(profile, s3).save_output("What is Alpha", "one", actor="user")
        name = key.rsplit("/", 1)[1]
        assert re.fullmatch(r"\d{8}-\d{6}-[^/]+-[0-9a-f]{8}\.md", name), name
        assert _outputs._parse_stamp(name) is not None

    def test_create_retries_on_a_taken_key(self, profile, s3):
        engine = _engine(profile, s3)
        real = s3.upload
        calls = []

        def upload(key, body, *a, **kw):
            calls.append(key)
            if len(calls) == 1:
                raise PreconditionFailed(key)
            return real(key, body, *a, **kw)

        with patch.object(s3, "upload", side_effect=upload):
            key = engine.save_output("what is alpha", "one", actor="user")
        assert len(calls) >= 2 and calls[0] != calls[1] and key == calls[1]


# ── other aggregates ────────────────────────────────────────────────


class TestManifestConcurrency:
    def test_concurrent_updates_keep_both_entries(self, profile, s3):
        s3.upload(_manifest.manifest_key_for(profile), json.dumps(
            {"version": _manifest.MANIFEST_VERSION, "entries": {}}))
        page = "---\ntitle: T\n---\nbody"
        with _sync_first_reads(s3):
            _run_threads(
                lambda: _manifest.update_entry(s3, profile, f"{P}wiki/a.md", page),
                lambda: _manifest.update_entry(s3, profile, f"{P}wiki/b.md", page),
            )
        entries = _manifest.load(s3, profile)["entries"]
        assert set(entries) == {f"{P}wiki/a.md", f"{P}wiki/b.md"}


class TestLogConcurrency:
    def test_concurrent_log_entries_are_kept(self, profile, s3):
        s3.upload(f"{P}wiki/log.md", "# Activity Log\n")
        a = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        b = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        with _sync_first_reads(s3):
            _run_threads(
                lambda: a._log_clipping_imports([("Alpha clip", "https://example.com/a")]),
                lambda: b._log_clipping_imports([("Beta clip", "https://example.com/b")]),
            )
        log = s3.download(f"{P}wiki/log.md")
        assert "Alpha clip" in log and "Beta clip" in log

    def test_first_log_entry_creates_the_file(self, profile, s3):
        Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)._log_clipping_imports(
            [("Alpha clip", "")])
        assert "Alpha clip" in s3.download(f"{P}wiki/log.md")


    def test_committed_write_retried_into_412_is_not_logged_twice(self, profile, s3):
        s3.upload(f"{P}wiki/log.md", "# Activity Log\n")
        compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        with _commit_then_412(s3):
            compiler._log_clipping_imports([("Alpha clip", "https://example.com/a")])
        assert s3.download(f"{P}wiki/log.md").count("Alpha clip") == 1

    def test_committed_first_entry_retried_into_412_is_not_logged_twice(self, profile, s3):
        compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        with _commit_then_412(s3):
            compiler._log_compile_removal("https://example.com/a", "boom", 3)
        assert s3.download(f"{P}wiki/log.md").count("https://example.com/a") == 1

    def test_server_without_etags_still_gets_the_entry(self, profile, s3, monkeypatch):
        from agent2kb.compile import compiler as _compiler

        monkeypatch.setattr(_compiler, "_log_etag_warned", False)
        s3.upload(f"{P}wiki/log.md", "# Activity Log\n")
        real = s3.download_with_etag
        warnings = []
        compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        with patch.object(s3, "download_with_etag", side_effect=lambda k: (real(k)[0], "")), \
                patch.object(_compiler.logger, "warning", side_effect=warnings.append):
            compiler._log_clipping_imports([("Alpha clip", "")])
            compiler._log_clipping_imports([("Beta clip", "")])
        log = s3.download(f"{P}wiki/log.md")
        assert "Alpha clip" in log and "Beta clip" in log
        assert sum("no ETag" in w for w in warnings) == 1


class TestCommittedRetries:
    """A conditional PUT that committed but whose response was lost: botocore
    retries it and the retry answers 412. The write must count once."""

    def test_agent_follow_up_is_merged_once(self, profile, s3):
        key = _engine(profile, s3).save_output("what is alpha", "first answer", actor="agent")
        with _commit_then_412(s3):
            got = _engine(profile, s3).save_output("install alpha", "answer B", actor="agent")
        assert got == key
        fm, body = Frontmatter.from_yaml_block(s3.download(key))
        assert body.count("answer B") == 1
        assert fm.source_meta["also_asked"] == ["install alpha"]

    def test_new_capture_is_written_once(self, profile, s3):
        with _commit_then_412(s3):
            key = _engine(profile, s3).save_output("what is alpha", "an answer")
        keys = [o.key for o in s3.list_objects(sub_prefix="outputs/", suffix=".md")]
        assert keys == [key]

    def test_promote_output_is_written_once(self, s3):
        from agent2kb.promote import _upload_new_output

        with _commit_then_412(s3):
            key = _upload_new_output(s3, f"{P}outputs/20260101-000000-alpha", "text")
        keys = [o.key for o in s3.list_objects(sub_prefix="outputs/", suffix=".md")]
        assert keys == [key]


class TestEmbeddingIndexConcurrency:
    def _index(self, profile, s3):
        idx = EmbeddingIndex(profile=profile, s3=s3)
        idx.load()
        return idx

    def test_interleaved_refreshes_merge(self, profile, s3):
        with patch.object(EmbeddingClient, "embed", return_value=[1.0, 0.0]):
            seed = self._index(profile, s3)
            seed.upsert(f"{P}wiki/old.md", "old page")
            seed.upsert(f"{P}wiki/keep.md", "kept page")
            seed.save()

            a, b = self._index(profile, s3), self._index(profile, s3)
            a.upsert(f"{P}wiki/new-a.md", "page a")
            a.save()
            b.upsert(f"{P}wiki/new-b.md", "page b")
            b.remove(f"{P}wiki/old.md")
            b.save()  # loses the race, re-reads a's index and re-applies its changes

        final = self._index(profile, s3)
        assert set(final.items) == {f"{P}wiki/keep.md", f"{P}wiki/new-a.md", f"{P}wiki/new-b.md"}

    def test_two_first_writers_merge(self, profile, s3):
        with patch.object(EmbeddingClient, "embed", return_value=[1.0, 0.0]):
            a, b = self._index(profile, s3), self._index(profile, s3)  # no index yet
            a.upsert(f"{P}wiki/a.md", "page a")
            b.upsert(f"{P}wiki/b.md", "page b")
            a.save()
            b.save()
        assert set(self._index(profile, s3).items) == {f"{P}wiki/a.md", f"{P}wiki/b.md"}
