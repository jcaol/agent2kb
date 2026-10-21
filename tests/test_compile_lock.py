"""One compile per KB at a time: the lease in <prefix>_ops/compile.lock."""

from __future__ import annotations

import datetime
import json
import os
from unittest.mock import patch

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws
from typer.testing import CliRunner

from agent2kb import main as kb_main
from agent2kb.compile import lock as L
from agent2kb.models import CompileResult, ProfileConfig
from agent2kb.s3_client import S3Client

BUCKET = "test-bucket"


def _profile(name="alpha", prefix=None):
    return ProfileConfig(
        name=name, s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket=BUCKET, prefix=f"kbs/{name}/" if prefix is None else prefix,
    )


@pytest.fixture
def conn():
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket=BUCKET)
        yield c


def _client(conn, profile) -> S3Client:
    c = S3Client(profile)
    c._client = conn
    return c


@pytest.fixture
def s3(conn):
    return _client(conn, _profile())


@pytest.fixture(autouse=True)
def _reset_warning(monkeypatch):
    monkeypatch.setattr(L, "_unlocked_warned", False)


def _lock_body(s3):
    return json.loads(s3.download(L.lock_key(s3.profile)))


def _write_lease(s3, *, expires_in: int, holder="other", host="host-b", pid=4242):
    now = datetime.datetime.now(datetime.timezone.utc)
    body = {"holder": holder, "host": host, "pid": pid, "started": now.isoformat(),
            "expires": (now + datetime.timedelta(seconds=expires_in)).isoformat()}
    s3.upload(L.lock_key(s3.profile), json.dumps(body), content_type="application/json")


class TestLease:
    def test_acquire_writes_holder_and_release_deletes(self, s3):
        lease = L.acquire(s3, s3.profile)
        try:
            body = _lock_body(s3)
            assert body["holder"] == lease.holder and body["pid"] == os.getpid()
            assert set(body) >= {"host", "started", "expires", "renewed"}
            assert L.lock_key(s3.profile) == "kbs/alpha/_ops/compile.lock"
        finally:
            lease.release()
        assert not s3.exists(L.lock_key(s3.profile))

    def test_second_acquire_is_refused_while_held(self, s3):
        with L.acquire(s3, s3.profile):
            with pytest.raises(L.CompileLocked) as held:
                L.acquire(s3, s3.profile)
            assert held.value.info["pid"] == os.getpid()
        # Released: the next compile gets it.
        L.acquire(s3, s3.profile).release()

    def test_live_lease_of_another_host_is_respected(self, s3):
        _write_lease(s3, expires_in=300)
        with pytest.raises(L.CompileLocked) as held:
            L.acquire(s3, s3.profile)
        assert held.value.info["host"] == "host-b"
        assert _lock_body(s3)["holder"] == "other"

    def test_stale_lease_is_taken_over(self, s3):
        _write_lease(s3, expires_in=-1)
        with L.acquire(s3, s3.profile) as lease:
            assert lease.took_over["host"] == "host-b"
            assert _lock_body(s3)["holder"] == lease.holder
        assert not s3.exists(L.lock_key(s3.profile))

    def test_unreadable_lease_counts_as_stale(self, s3):
        s3.upload(L.lock_key(s3.profile), "not json")
        with L.acquire(s3, s3.profile) as lease:
            assert lease.took_over == {}

    def test_only_one_of_two_takeovers_of_the_same_stale_lease_wins(self, s3):
        _write_lease(s3, expires_in=-1)
        stale = s3.download_with_etag(L.lock_key(s3.profile))
        first = L.acquire(s3, s3.profile)
        # The second compile read the stale lease before the first one replaced it.
        real = s3.download_with_etag
        calls = []

        def download(key):
            calls.append(key)
            return stale if len(calls) == 1 else real(key)

        with patch.object(s3, "download_with_etag", side_effect=download):
            with pytest.raises(L.CompileLocked) as held:
                L.acquire(s3, s3.profile)
        assert held.value.info["holder"] == first.holder
        first.release()

    def test_renewal_extends_the_lease(self, s3):
        lease = L.acquire(s3, s3.profile)
        try:
            before = _lock_body(s3)["expires"]
            with patch.object(L, "_now", return_value=datetime.datetime.now(
                    datetime.timezone.utc) + datetime.timedelta(seconds=60)):
                assert lease.renew() is True
            assert _lock_body(s3)["expires"] > before
        finally:
            lease.release()

    def test_lost_lease_is_not_renewed_or_deleted(self, s3):
        lease = L.acquire(s3, s3.profile)
        _write_lease(s3, expires_in=300, holder="taker")
        assert lease.renew() is False and lease.lost
        lease.release()
        assert _lock_body(s3)["holder"] == "taker"

    def test_renewal_thread_runs(self, s3, monkeypatch):
        monkeypatch.setattr(L, "RENEW_SEC", 0.01)
        renewed = []
        real = L.CompileLease.renew

        def renew(self):
            renewed.append(1)
            return real(self) if len(renewed) < 3 else False

        monkeypatch.setattr(L.CompileLease, "renew", renew)
        lease = L.acquire(s3, s3.profile)
        lease._thread.join(timeout=5)
        lease.release()
        assert len(renewed) >= 3

    @staticmethod
    def _commit_then_412(s3, *, times=1):
        """put_object that writes, then answers 412 as botocore's retry of a PUT whose
        response was lost would."""
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

    def test_create_committed_but_retried_412_is_our_lease(self, s3):
        with self._commit_then_412(s3):
            lease = L.acquire(s3, s3.profile)
        assert _lock_body(s3)["holder"] == lease.holder
        assert lease.renew() is True and not lease.lost
        lease.release()
        assert not s3.exists(L.lock_key(s3.profile))

    def test_takeover_committed_but_retried_412_is_our_lease(self, s3):
        _write_lease(s3, expires_in=-1)
        with self._commit_then_412(s3):
            lease = L.acquire(s3, s3.profile)
        assert lease.took_over["holder"] == "other"
        assert _lock_body(s3)["holder"] == lease.holder
        lease.release()
        assert not s3.exists(L.lock_key(s3.profile))

    def test_renewal_committed_but_retried_412_keeps_the_lease(self, s3):
        lease = L.acquire(s3, s3.profile)
        warnings = []
        with self._commit_then_412(s3), \
                patch.object(L.logger, "warning", side_effect=warnings.append):
            assert lease.renew() is True
        assert not lease.lost and not warnings
        assert lease.renew() is True
        lease.release()
        assert not s3.exists(L.lock_key(s3.profile))

    @pytest.mark.parametrize("code,message", [
        ("NotImplemented", "no"), ("InvalidArgument", "If-None-Match is not supported"),
    ])
    def test_provider_without_conditional_writes_runs_unlocked(self, s3, code, message, caplog):
        real = s3._client.put_object

        def put(**kwargs):
            if "IfMatch" in kwargs or "IfNoneMatch" in kwargs:
                raise ClientError({"Error": {"Code": code, "Message": message}}, "PutObject")
            return real(**kwargs)

        warnings = []
        with patch.object(s3._client, "put_object", side_effect=put), \
                patch.object(L.logger, "warning", side_effect=warnings.append):
            lease = L.acquire(s3, s3.profile)
            assert lease.key is None
            assert not s3.exists(L.lock_key(s3.profile))
            # Second KB / second run in this process: no second lock warning.
            L.acquire(s3, s3.profile).release()
            lease.release()
        assert sum("not locked" in w for w in warnings) == 1

    def test_unlocked_fallback_keeps_a_lease_written_by_another_compile(self, s3):
        """The plain-PUT fallback write is removed only while it is still ours."""
        real = s3._client.put_object

        def put(**kwargs):
            if "IfNoneMatch" in kwargs:
                raise ClientError({"Error": {"Code": "NotImplemented", "Message": "no"}},
                                  "PutObject")
            response = real(**kwargs)
            # Another compile's plain PUT lands right after ours.
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            real(Bucket=kwargs["Bucket"], Key=kwargs["Key"], Body=json.dumps(
                {"holder": "other", "expires": now}).encode())
            return response

        with patch.object(s3._client, "put_object", side_effect=put):
            lease = L.acquire(s3, s3.profile)
        assert lease.key is None
        assert _lock_body(s3)["holder"] == "other"

    def test_member_kb_lease_stays_inside_its_prefix(self, conn):
        beta = _client(conn, _profile("beta"))
        with L.acquire(beta, beta.profile):
            keys = [o["Key"] for o in conn.list_objects_v2(Bucket=BUCKET)["Contents"]]
        assert keys == ["kbs/beta/_ops/compile.lock"]


# ── the compile command ─────────────────────────────────────────────


def _profile_data(prefix):
    return {"s3_endpoint": "http://localhost:5000", "access_key": "testing",
            "secret_key": "testing", "bucket": BUCKET, "prefix": prefix,
            "compile_provider": "groq", "groq_api_key": "g"}


@pytest.fixture
def cli(conn, tmp_path, monkeypatch):
    """Runs `a2kb compile` against moto with a fake compiler; returns (run, state)."""
    for key in list(os.environ):
        if key.startswith("KB_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {
        "super": _profile_data(""),
        "alpha": _profile_data("kbs/alpha/"),
        "beta": _profile_data("kbs/beta/"),
    }}))
    path.chmod(0o600)

    from agent2kb.compile import compiler as compiler_mod

    state = {"compiled": [], "raise": None, "seen_lock": []}

    class FakeCompiler:
        model = "m"

        def __init__(self, profile, s3, **_kw):
            self.profile, self.s3 = profile, s3

        def compile_all(self, new_only=True):
            state["compiled"].append(self.profile.name)
            state["seen_lock"].append(self.s3.exists(L.lock_key(self.profile)))
            if state["raise"]:
                raise state["raise"]
            return [CompileResult(source_key=f"{self.profile.prefix}raw/a.md",
                                  wiki_key=f"{self.profile.prefix}wiki/a.md",
                                  title="a", content="x")]

    monkeypatch.setattr(compiler_mod, "Compiler", FakeCompiler)
    monkeypatch.setattr("agent2kb.ops.write_compile_run", lambda *a, **k: None)
    monkeypatch.setattr(kb_main, "S3Client", lambda prof: _client(conn, prof))
    monkeypatch.setattr(kb_main, "get_s3",
                        lambda: _client(conn, kb_main.get_current_profile()))

    def run(*args, profile="alpha"):
        return CliRunner().invoke(kb_main.app, ["-p", profile, "--config", str(path), *args])

    return run, state


class TestCompileCommand:
    def test_compile_holds_and_releases_the_lease(self, cli, conn):
        run, state = cli
        r = run("compile")
        assert r.exit_code == 0, r.output
        assert state["compiled"] == ["alpha"] and state["seen_lock"] == [True]
        assert "Contents" not in conn.list_objects_v2(Bucket=BUCKET, Prefix="kbs/alpha/_ops/")

    def test_live_lease_skips_with_exit_zero_and_a_stderr_message(self, cli, conn):
        run, state = cli
        _write_lease(_client(conn, _profile()), expires_in=300)
        r = run("compile")
        assert r.exit_code == 0, r.output
        assert state["compiled"] == []
        assert "Another compile of KB 'alpha' is running" in r.stderr
        assert "host-b" in r.stderr and "4242" in r.stderr
        assert "Another compile" not in r.stdout
        # The other compile's lease is left alone.
        assert _lock_body(_client(conn, _profile()))["holder"] == "other"

    def test_live_lease_under_strict_exits_nonzero(self, cli, conn):
        run, state = cli
        _write_lease(_client(conn, _profile()), expires_in=300)
        r = run("compile", "--strict")
        assert r.exit_code == kb_main.COMPILE_LOCKED_EXIT == 75
        assert state["compiled"] == []

    def test_stale_lease_is_taken_over_by_the_command(self, cli, conn):
        run, state = cli
        _write_lease(_client(conn, _profile()), expires_in=-60)
        r = run("compile", "--strict")
        assert r.exit_code == 0, r.output
        assert state["compiled"] == ["alpha"] and "stale compile lease" in r.stderr
        assert not _client(conn, _profile()).exists(L.lock_key(_profile()))

    def test_lease_released_when_compile_raises(self, cli, conn):
        run, state = cli
        state["raise"] = RuntimeError("boom")
        r = run("compile")
        assert r.exit_code != 0
        assert not _client(conn, _profile()).exists(L.lock_key(_profile()))

    def test_all_profiles_skips_only_the_locked_kb(self, cli, conn):
        run, state = cli
        _write_lease(_client(conn, _profile("alpha")), expires_in=300)
        r = run("compile", "--all-profiles", profile="super")
        assert r.exit_code == 0, r.output
        assert state["compiled"] == ["beta"]
        assert "Another compile of KB 'alpha'" in r.stderr
        assert not _client(conn, _profile("beta")).exists(L.lock_key(_profile("beta")))
        r = run("compile", "--all-profiles", "--strict", profile="super")
        assert r.exit_code == 75 and "alpha" in r.stderr
        assert state["compiled"] == ["beta", "beta"]
