"""Exit codes, config-error handling and doctor reporting at the CLI boundary."""

import json
import os

import pytest
import yaml
from botocore import exceptions as bex
from typer.testing import CliRunner

from agent2kb import doctor
from agent2kb import main as kb_main
from agent2kb.doctor import FAIL, OK, WARN, NotInteractive, Report
from agent2kb.errors import describe_error
from agent2kb.models import ContentType, IngestResult, ProfileConfig, bucket_name_problem

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KB_"):
            monkeypatch.delenv(key)


def _profile_data(**over):
    data = {
        "s3_endpoint": "http://127.0.0.1:1",
        "access_key": "k",
        "secret_key": "s",
        "bucket": "bkt",
        "prefix": "t/",
        "compile_provider": "groq",
        "groq_api_key": "g",
    }
    data.update(over)
    return data


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"t": _profile_data()}}))
    path.chmod(0o600)
    return path


def _run(cfg, *args, profile="t"):
    return runner.invoke(kb_main.app, ["-p", profile, "--config", str(cfg), *args])


class TestConfigErrors:
    def test_rejected_credential_source_is_one_line_exit_2(self, tmp_path, monkeypatch):
        user = tmp_path / "user.yaml"
        user.write_text(yaml.dump({"profiles": {}}))
        proj = tmp_path / "repo" / ".kb"
        proj.mkdir(parents=True)
        (proj / "config.yaml").write_text(
            yaml.dump({"name": "x", "bucket": "bkt", "prefix": "p/", "aws_profile": "prod"})
        )
        monkeypatch.chdir(tmp_path / "repo")
        result = runner.invoke(kb_main.app, ["-p", "x", "--config", str(user), "status"])
        assert result.exit_code == 2
        assert "Error:" in result.output and "aws_profile" in result.output
        assert "Traceback" not in result.output and "Usage:" not in result.output

    def test_unknown_profile_is_one_line_exit_2(self, cfg):
        result = _run(cfg, "status", profile="nosuch")
        assert result.exit_code == 2
        assert "not found" in result.output
        assert "Traceback" not in result.output and "Usage:" not in result.output

    def test_validation_error_has_no_usage_block(self, tmp_path):
        path = tmp_path / "c.yaml"
        # access_key without secret_key
        path.write_text(
            yaml.dump({"profiles": {"t": {"bucket": "bkt", "prefix": "t/", "access_key": "k"}}})
        )
        result = _run(path, "status")
        assert result.exit_code == 2
        assert "secret_key" in result.output and "Usage:" not in result.output

    def test_json_mode_still_emits_json_on_config_error(self, cfg):
        result = _run(cfg, "doctor", "--json", profile="nosuch")
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False and "not found" in payload["error"]

    def test_verbose_keeps_traceback_for_config_errors(self, cfg):
        result = runner.invoke(kb_main.app, ["-v", "-p", "nosuch", "--config", str(cfg), "status"])
        assert isinstance(result.exception, ValueError)


class TestVerboseJson:
    def test_stdout_is_valid_json_with_verbose(self, cfg):
        result = runner.invoke(
            kb_main.app, ["-v", "-p", "t", "--config", str(cfg), "doctor", "--json", "--read-only"]
        )
        data = json.loads(result.stdout)  # logs went to stderr
        failing = [c for c in data["checks"] if c["status"] == FAIL]
        assert failing and any("Traceback" in c["debug"] for c in failing)

    def test_no_debug_field_without_verbose(self, cfg):
        result = _run(cfg, "doctor", "--json", "--read-only")
        data = json.loads(result.stdout)
        assert all("debug" not in c for c in data["checks"])


class _DownS3:
    def __init__(self, profile):
        self.profile = profile

    def list_objects(self, *a, **kw):
        raise bex.EndpointConnectionError(endpoint_url="http://127.0.0.1:1/x")


class TestStatusExit:
    def test_unreachable_s3_exits_1_with_friendly_text(self, cfg, monkeypatch):
        monkeypatch.setattr(kb_main, "S3Client", _DownS3)
        result = _run(cfg, "status")
        assert result.exit_code == 1
        assert "Cannot reach the S3 endpoint" in result.output
        assert "botocore" not in result.output


class _StubPipeline:
    results: list = []

    def __init__(self, **kw):
        pass

    def ingest_url(self, url, **kw):
        return self.results[0]

    ingest_file = ingest_url

    def ingest_folder(self, folder, recursive=False, **kw):
        return self.results


def _res(ok, src="s"):
    return IngestResult(
        source=src,
        content_type=ContentType.TEXT,
        raw_key="k" if ok else "",
        title="t",
        success=ok,
        error=None if ok else "boom",
    )


class _LogS3:
    def __init__(self, profile):
        self.profile = profile

    def download(self, key):
        return ""

    def upload(self, *a, **kw):
        pass


class TestIngestExit:
    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr(kb_main, "S3Client", _LogS3)
        monkeypatch.setattr("agent2kb.ingest.base.IngestPipeline", _StubPipeline)

    def test_failed_url_exits_1(self, cfg):
        _StubPipeline.results = [_res(False)]
        assert _run(cfg, "ingest", "--url", "http://x").exit_code == 1

    def test_ok_url_exits_0(self, cfg):
        _StubPipeline.results = [_res(True)]
        assert _run(cfg, "ingest", "--url", "http://x").exit_code == 0

    def test_failed_file_exits_1(self, cfg, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("x")
        _StubPipeline.results = [_res(False)]
        assert _run(cfg, "ingest", "--file", str(f)).exit_code == 1

    def test_folder_with_any_failure_exits_1_after_reporting_all(self, cfg, tmp_path):
        _StubPipeline.results = [_res(True, "a"), _res(False, "b"), _res(True, "c")]
        result = _run(cfg, "ingest", "--folder", str(tmp_path))
        assert result.exit_code == 1
        assert "Ingested 2 files" in result.output and "1 failed" in result.output
        assert "b: boom" in result.output

    def test_folder_all_ok_exits_0(self, cfg, tmp_path):
        _StubPipeline.results = [_res(True), _res(True)]
        assert _run(cfg, "ingest", "--folder", str(tmp_path)).exit_code == 0


def test_describe_error_translates_s3_failures():
    exc = bex.EndpointConnectionError(endpoint_url="http://x:1/b/k")
    text = describe_error(exc, ProfileConfig(name="p", bucket="bkt", prefix="p/"))
    assert "Cannot reach the S3 endpoint" in text
    assert describe_error(RuntimeError("plain")) == "plain"


class TestBucketName:
    @pytest.mark.parametrize(
        "name", ["kb", "Bucket", "my_bucket", "-abc", "abc-", "a..b", "192.168.0.1", "x" * 64, ""]
    )
    def test_invalid(self, name):
        assert bucket_name_problem(name)

    @pytest.mark.parametrize("name", ["abc", "my-kb.bucket-1", "a" * 63, "kb-notes"])
    def test_valid(self, name):
        assert bucket_name_problem(name) is None

    def test_profile_only_warns(self):
        p = ProfileConfig(name="p", bucket="Legacy_Bucket", prefix="p/")
        assert p.bucket == "Legacy_Bucket"

    def test_create_bucket_refuses_invalid_name(self):
        from agent2kb.s3_client import S3Client

        c = S3Client(ProfileConfig(name="p", bucket="kb", prefix="p/", region="us-east-1"))
        with pytest.raises(ValueError, match="Invalid bucket name"):
            c.create_bucket()

    def test_doctor_fails_with_the_rule_and_skips_s3(self):
        report = Report()
        doctor.check_storage(ProfileConfig(name="p", bucket="kb", prefix="p/"), report)
        first = report.checks[0]
        assert first.name == "bucket name" and first.status == FAIL
        assert "3-63" in first.detail
        assert len(report.checks) == 2


class _FakeS3:
    """Stand-in S3Client: HeadBucket denied, list fails with the given code."""

    def __init__(self, list_code, head_code="403"):
        self._list_code, self._head_code = list_code, head_code

    def bucket_exists(self):
        raise bex.ClientError({"Error": {"Code": self._head_code, "Message": "x"}}, "HeadBucket")

    def list_one(self, sub_prefix=""):
        raise bex.ClientError({"Error": {"Code": self._list_code, "Message": "x"}}, "ListObjectsV2")


class TestDoctorReconcile:
    def _profile(self):
        return ProfileConfig(name="p", bucket="bkt", prefix="p/", s3_endpoint="http://x")

    def test_signature_mismatch_downgrades_head_403_warning_to_failure(self):
        report = Report()
        doctor.check_storage(self._profile(), report, client=_FakeS3("SignatureDoesNotMatch"))
        creds = [c for c in report.checks if c.name == "credentials accepted"]
        assert len(creds) == 1 and creds[0].status == FAIL
        assert "secret key does not match" in creds[0].detail

    def test_ambiguous_403_stays_warn_with_both_causes_in_text(self):
        report = Report()
        doctor.check_storage(self._profile(), report, client=_FakeS3("AccessDenied"))
        creds = [c for c in report.checks if c.name == "credentials accepted"][0]
        assert creds.status == WARN
        assert "key is wrong" in creds.detail and "scoped to a prefix" in creds.detail


class _MissingBucketS3:
    def bucket_exists(self):
        return False


class TestDoctorFix:
    def test_non_tty_fix_says_rerun_with_yes(self):
        def confirm(q):
            raise NotInteractive

        report = Report()
        profile = ProfileConfig(name="p", bucket="bkt", prefix="p/", s3_endpoint="http://x")
        doctor.check_storage(profile, report, fix=True, confirm=confirm, client=_MissingBucketS3())
        bad = [c for c in report.checks if c.status == FAIL][0]
        assert "--yes" in bad.hint

    def test_cli_fix_without_tty_and_yes_hints_yes(self, cfg, monkeypatch):
        monkeypatch.setattr(kb_main, "_stdin_is_terminal", lambda: False)
        monkeypatch.setattr(kb_main, "S3Client", lambda p: _MissingBucketS3())
        monkeypatch.setattr(doctor, "S3Client", lambda p: _MissingBucketS3())
        result = _run(cfg, "doctor", "--fix", "--json", "--read-only")
        data = json.loads(result.stdout)
        assert any("--yes" in c["hint"] for c in data["checks"])


class TestDoctorSources:
    def test_user_config_without_this_profile_is_not_listed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        user = tmp_path / "u.yaml"
        user.write_text(yaml.dump({"profiles": {"other": _profile_data()}}))
        proj = tmp_path / ".kb"
        proj.mkdir()
        (proj / "config.yaml").write_text(yaml.dump({"name": "mine", **_profile_data()}))
        profile = ProfileConfig(name="mine", **_profile_data())
        report = Report()
        doctor.check_config(profile, user, report)
        detail = report.checks[0].detail
        assert "project " in detail and "user " not in detail

    def test_user_config_listed_when_it_has_the_profile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        user = tmp_path / "u.yaml"
        user.write_text(yaml.dump({"profiles": {"mine": _profile_data()}}))
        report = Report()
        doctor.check_config(ProfileConfig(name="mine", **_profile_data()), user, report)
        assert "user" in report.checks[0].detail
        assert report.checks[0].status == OK


class TestCompileStrict:
    """`a2kb compile --strict` / KB_COMPILE_STRICT: nonzero exit on any failed file."""

    @pytest.fixture
    def fake_compile(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent2kb.compile import compiler as compiler_mod
        from agent2kb.models import CompileResult

        state = {"results": []}

        class FakeCompiler:
            model = "m"

            def __init__(self, **_kw):
                pass

            def compile_all(self, new_only=True):
                return list(state["results"])

        monkeypatch.setattr(compiler_mod, "Compiler", FakeCompiler)
        monkeypatch.setattr("agent2kb.ops.write_compile_run", lambda *a, **k: None)
        monkeypatch.setattr(kb_main, "get_s3", lambda: MagicMock())
        monkeypatch.setattr(kb_main, "S3Client", lambda *_a, **_k: MagicMock())
        # The compile lease needs a real (moto) bucket; tests/test_compile_lock.py covers it.
        from agent2kb.compile import lock as lock_mod

        monkeypatch.setattr(lock_mod, "acquire", lambda s3, prof: lock_mod.CompileLease(
            s3=s3, key=None, holder="test", started=""))

        def ok(name):
            return CompileResult(source_key=f"t/raw/{name}.md", wiki_key=f"t/wiki/{name}.md",
                                 title=name, content="x")

        def bad(name, quarantined=False):
            return CompileResult(source_key=f"t/raw/{name}.md", wiki_key="", title="",
                                 content="", success=False, error="boom",
                                 quarantined=quarantined)

        state["ok"], state["bad"] = ok, bad
        return state

    def test_default_exit_zero_even_when_all_fail(self, cfg, fake_compile):
        fake_compile["results"] = [fake_compile["bad"]("a")]
        result = _run(cfg, "compile")
        assert result.exit_code == 0, result.output
        assert "1 failed" in result.output

    def test_strict_exits_one_on_failure(self, cfg, fake_compile):
        fake_compile["results"] = [fake_compile["ok"]("a"), fake_compile["bad"]("b")]
        result = _run(cfg, "compile", "--strict")
        assert result.exit_code == 1, result.output

    def test_strict_counts_quarantined(self, cfg, fake_compile):
        fake_compile["results"] = [fake_compile["bad"]("a", quarantined=True)]
        result = _run(cfg, "compile", "--strict")
        assert result.exit_code == 1
        assert "1 quarantined" in result.output

    def test_strict_exit_zero_when_clean(self, cfg, fake_compile):
        fake_compile["results"] = [fake_compile["ok"]("a")]
        assert _run(cfg, "compile", "--strict").exit_code == 0
        fake_compile["results"] = []
        assert _run(cfg, "compile", "--strict").exit_code == 0

    @pytest.mark.parametrize("value,code", [("1", 1), ("true", 1), ("0", 0), ("false", 0)])
    def test_env_var(self, cfg, fake_compile, monkeypatch, value, code):
        fake_compile["results"] = [fake_compile["bad"]("a")]
        monkeypatch.setenv("KB_COMPILE_STRICT", value)
        assert _run(cfg, "compile").exit_code == code

    def test_all_profiles_strict_propagates(self, tmp_path, monkeypatch, fake_compile):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"profiles": {
            "super": _profile_data(prefix=""),
            "alpha": _profile_data(prefix="kbs/alpha/"),
        }}))
        path.chmod(0o600)
        fake_compile["results"] = [fake_compile["bad"]("a")]
        assert _run(path, "compile", "--all-profiles", profile="super").exit_code == 0
        result = _run(path, "compile", "--all-profiles", "--strict", profile="super")
        assert result.exit_code == 1, result.output
        assert "alpha" in result.output
