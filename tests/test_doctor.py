"""Tests for `a2kb doctor`."""

import json
import os
import stat

import boto3
import pytest
import yaml
from botocore import exceptions as bex
from moto import mock_aws
from typer.testing import CliRunner

from agent2kb import doctor
from agent2kb.doctor import FAIL, OK, SKIP, WARN, Report
from agent2kb.main import app
from agent2kb.models import LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client

runner = CliRunner()


def _profile(**kw) -> ProfileConfig:
    base = dict(
        name="p",
        bucket="doc-bucket",
        prefix="projects/p/",
        region="us-east-1",
        compile_provider=LLMProvider.GROQ,
        groq_api_key="gsk_x",
    )
    base.update(kw)
    return ProfileConfig(**base)


def _status(report: Report, name_part: str) -> str:
    matches = [c for c in report.checks if name_part in c.name]
    assert matches, f"no check containing {name_part!r}: {[c.name for c in report.checks]}"
    return matches[-1].status


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


class TestStorage:
    def test_healthy_bucket(self, aws_env):
        with mock_aws():
            profile = _profile()
            S3Client(profile).create_bucket()
            report = Report()
            doctor.check_storage(profile, report)
            assert not report.failed
            assert _status(report, "bucket exists") == OK
            assert _status(report, "write probe") == OK
            # The probe object is cleaned up.
            assert S3Client(profile).list_one("_doctor/") == []

    def test_read_only_skips_write(self, aws_env):
        with mock_aws():
            profile = _profile()
            S3Client(profile).create_bucket()
            report = Report()
            doctor.check_storage(profile, report, read_only=True)
            assert _status(report, "write probe") == SKIP
            assert not report.failed

    def test_missing_bucket_fails_without_fix_and_changes_nothing(self, aws_env):
        with mock_aws():
            profile = _profile()
            report = Report()
            doctor.check_storage(profile, report, confirm=lambda q: True)
            assert report.failed
            assert _status(report, "bucket exists") == FAIL
            assert "--fix" in [c for c in report.checks if c.status == FAIL][0].hint
            assert S3Client(profile).bucket_exists() is False

    def test_fix_creates_bucket_after_confirmation(self, aws_env):
        with mock_aws():
            profile = _profile()
            asked = []
            report = Report()
            doctor.check_storage(
                profile, report, fix=True, confirm=lambda q: asked.append(q) or True
            )
            assert asked and "doc-bucket" in asked[0]
            assert not report.failed
            assert S3Client(profile).bucket_exists() is True

    def test_fix_declined_creates_nothing(self, aws_env):
        with mock_aws():
            profile = _profile()
            report = Report()
            doctor.check_storage(profile, report, fix=True, confirm=lambda q: False)
            assert report.failed
            assert S3Client(profile).bucket_exists() is False

    def test_dead_endpoint(self):
        report = Report()
        profile = _profile(
            s3_endpoint="http://127.0.0.1:1", access_key="a", secret_key="b", region=None
        )
        doctor.check_storage(profile, report)
        assert _status(report, "endpoint reachable") == FAIL
        assert report.failed

    def test_no_credentials(self, monkeypatch, tmp_path):
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none2"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        # boto3's default session caches credentials resolved by earlier tests.
        boto3.setup_default_session()
        try:
            report = Report()
            doctor.check_storage(_profile(), report)
        finally:
            boto3.setup_default_session()
        assert _status(report, "credentials accepted") == FAIL

    def test_head_bucket_denied_falls_back_to_list(self, aws_env):
        with mock_aws():
            profile = _profile()
            s3 = S3Client(profile)
            s3.create_bucket()

            def deny():
                raise bex.ClientError({"Error": {"Code": "403", "Message": ""}}, "HeadBucket")

            s3.bucket_exists = deny
            report = Report()
            doctor.check_storage(profile, report, client=s3)
            assert _status(report, "credentials accepted") == WARN
            assert _status(report, "bucket exists") == WARN
            assert _status(report, "write probe") == OK
            assert not report.failed


class TestConfigCheck:
    def test_loose_permissions_warn(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("profiles: {}")
        cfg.chmod(0o644)
        monkeypatch.chdir(tmp_path)
        report = Report()
        doctor.check_config(_profile(), cfg, report)
        assert _status(report, "config permissions") == WARN
        cfg.chmod(0o600)
        report = Report()
        doctor.check_config(_profile(), cfg, report)
        assert _status(report, "config permissions") == OK

    def test_no_secrets_in_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile = _profile(access_key="AKIA_SECRET_ID", secret_key="TOPSECRETVALUE")
        report = Report()
        doctor.check_config(profile, tmp_path / "missing.yaml", report)
        doctor.check_privacy(profile, report)
        assert "TOPSECRETVALUE" not in report.to_json()
        assert "AKIA_SECRET_ID" not in report.to_json()

    def test_privacy_hint_names_the_field_of_the_config_format(self):
        cloud = _profile(compile_provider="groq")
        old, new = Report(), Report()
        doctor.check_privacy(cloud, old)
        doctor.check_privacy(cloud, new, v2=True)
        assert "compile_provider: ollama" in old.to_json()
        assert "llm.provider: ollama" in new.to_json()
        assert "compile_provider" not in new.to_json()

    def test_is_v2_kb(self, tmp_path):
        v2 = tmp_path / "v2.yaml"
        v2.write_text("version: 2\nkbs:\n  notes: {}\n")
        old = tmp_path / "old.yaml"
        old.write_text("profiles:\n  notes: {}\n")
        assert doctor._is_v2_kb(v2, "notes") and not doctor._is_v2_kb(v2, "other")
        assert not doctor._is_v2_kb(old, "notes")
        assert not doctor._is_v2_kb(tmp_path / "missing.yaml", "notes")

    def test_non_super_needs_prefix(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        report = Report()
        doctor.check_config(_profile(prefix=""), tmp_path / "x.yaml", report)
        assert _status(report, "prefix") == FAIL


class TestLLM:
    def test_missing_api_key_fails(self):
        report = Report()
        doctor.check_llm(_profile(groq_api_key=None), report)
        assert _status(report, "groq API key") == FAIL

    def test_cli_binary_missing(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        report = Report()
        doctor.check_llm(
            _profile(
                compile_provider=LLMProvider.CLI,
                cli_command="codex",
                cli_fallback_command="claude",
            ),
            report,
        )
        assert _status(report, "CLI 'codex'") == FAIL
        assert _status(report, "CLI 'claude'") == WARN  # fallback is advisory

    def test_cli_login_status(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
        calls = []

        def fake_run(argv):
            calls.append(argv)
            if "codex" in argv[0]:
                return subprocess.CompletedProcess(argv, 0, "Logged in", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": False}), "")

        monkeypatch.setattr(doctor, "_run_status", fake_run)
        report = Report()
        doctor.check_llm(
            _profile(
                compile_provider=LLMProvider.CLI,
                cli_command="codex",
                cli_fallback_command="claude",
            ),
            report,
        )
        assert _status(report, "CLI 'codex'") == OK
        assert _status(report, "CLI 'claude'") == WARN  # not logged in, but fallback
        # Only status subcommands — never a prompt (no tokens spent).
        assert [a[1:] for a in calls] == [["login", "status"], ["auth", "status"]]

    def test_disabled_fallback_is_not_checked(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        report = Report()
        doctor.check_llm(
            _profile(
                compile_provider=LLMProvider.CLI, cli_command="claude", cli_fallback_command=""
            ),
            report,
        )
        names = [c.name for c in report.checks]
        assert names.count("CLI 'claude'") == 1
        assert not any("codex" in n for n in names)

    def test_ollama_unreachable(self):
        report = Report()
        doctor.check_llm(
            _profile(compile_provider=LLMProvider.OLLAMA, ollama_host="http://127.0.0.1:1"), report
        )
        assert _status(report, "ollama") == FAIL


def _ollama_tags(monkeypatch, *names):
    import io

    body = json.dumps({"models": [{"name": n} for n in names]}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda url, timeout: _Resp(body))


class TestOllamaModels:
    def _check(self, **kw) -> Report:
        report = Report()
        doctor.check_llm(_profile(compile_provider=LLMProvider.OLLAMA, groq_api_key=None, **kw),
                         report)
        return report

    def test_configured_chat_model_missing_fails(self, monkeypatch):
        _ollama_tags(monkeypatch, "qwen3:8b", "bge-m3:latest")
        report = self._check(default_model="llama3.3:70b")
        assert _status(report, "ollama chat model") == FAIL
        check = next(c for c in report.checks if c.name == "ollama chat model")
        assert "ollama pull llama3.3:70b" in check.hint
        assert report.failed

    def test_installed_chat_model_ok_and_latest_tag_matches(self, monkeypatch):
        _ollama_tags(monkeypatch, "mymodel:latest", "bge-m3:latest")
        report = self._check(default_model="mymodel")
        assert _status(report, "ollama chat model") == OK
        assert _status(report, "ollama embedding model") == OK
        assert not report.failed

    def test_default_model_is_an_ollama_model_not_the_groq_slug(self, monkeypatch):
        _ollama_tags(monkeypatch, "llama3.3:70b", "bge-m3")
        report = self._check()
        assert _status(report, "ollama chat model") == OK

    def test_missing_embedding_model_is_only_a_warning(self, monkeypatch):
        _ollama_tags(monkeypatch, "llama3.3:70b")
        report = self._check()
        assert _status(report, "ollama chat model") == OK
        assert _status(report, "ollama embedding model") == WARN
        assert not report.failed

    def test_no_embedding_check_when_embeddings_use_a_cloud_key(self, monkeypatch):
        _ollama_tags(monkeypatch, "llama3.3:70b")
        report = self._check(gemini_api_key="g")
        assert not any("embedding" in c.name for c in report.checks)

    def test_setup_detection_does_not_check_models(self, monkeypatch):
        _ollama_tags(monkeypatch, "anything:1b")
        report = Report()
        doctor._check_ollama(ProfileConfig(name="setup", bucket="x", prefix="x/"), report)
        assert [c.name for c in report.checks] == ["ollama"]
        assert report.checks[0].status == OK


class TestPrivacy:
    def test_flags_reflected(self):
        report = Report()
        doctor.check_privacy(_profile(), report)
        assert _status(report, "jina") == WARN
        report = Report()
        doctor.check_privacy(_profile(privacy_mode=True), report)
        assert _status(report, "jina") == OK
        assert _status(report, "cloud vision") == OK


class TestCommand:
    def _config(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.dump(
                {
                    "profiles": {
                        "t": {
                            "s3_endpoint": "http://127.0.0.1:1",
                            "access_key": "k",
                            "secret_key": "s",
                            "bucket": "bkt",
                            "prefix": "t/",
                            "compile_provider": "groq",
                            "groq_api_key": "g",
                        }
                    }
                }
            )
        )
        path.chmod(0o600)
        return path

    def test_exit_1_and_json_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["-p", "t", "--config", str(self._config(tmp_path)), "doctor", "--json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ok"] is False
        reach = [c for c in data["checks"] if c["name"] == "endpoint reachable"]
        assert reach and reach[0]["status"] == FAIL

    def test_text_output_symbols(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["-p", "t", "--config", str(self._config(tmp_path)), "doctor", "--read-only"]
        )
        assert result.exit_code == 1
        assert "✗" in result.output and "✓" in result.output

    def test_doctor_does_not_prompt_without_tty(self, tmp_path, monkeypatch):
        # --fix without --yes on a non-terminal must not create anything.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["-p", "t", "--config", str(self._config(tmp_path)), "doctor", "--fix"]
        )
        assert result.exit_code == 1


def test_mode_helper_ignores_private_files(tmp_path):
    f = tmp_path / "c"
    f.write_text("x")
    f.chmod(0o600)
    assert doctor._mode_warning(f) is None
    f.chmod(stat.S_IMODE(0o640))
    if os.name == "posix":
        assert doctor._mode_warning(f) is not None


class TestExtras:
    def test_missing_provider_package_fails_with_extras_hint(self, monkeypatch):
        monkeypatch.setattr(doctor, "_has_module", lambda name: False)
        report = Report()
        doctor.check_extras(_profile(compile_provider=LLMProvider.GROQ), report)
        groq = [c for c in report.checks if c.name == "groq"][0]
        assert groq.status == FAIL
        assert 'pip install "agent2kb[groq]"' in groq.hint

    def test_installed_package_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor, "_has_module", lambda name: True)
        report = Report()
        doctor.check_extras(_profile(compile_provider=LLMProvider.GEMINI), report)
        assert _status(report, "google.genai") == OK
        assert not report.failed

    def test_cli_provider_needs_no_package(self, monkeypatch):
        monkeypatch.setattr(doctor, "_has_module", lambda name: True)
        report = Report()
        doctor.check_extras(_profile(compile_provider=LLMProvider.CLI), report)
        assert not report.failed
        assert not any(c.name == "groq" for c in report.checks)

    def test_missing_embedding_package_is_a_warning(self, monkeypatch):
        monkeypatch.setattr(doctor, "_has_module", lambda name: name != "voyageai")
        report = Report()
        doctor.check_extras(_profile(voyage_api_key="v"), report)
        voyage = [c for c in report.checks if c.name == "voyageai"][0]
        assert voyage.status == WARN and "agent2kb[voyage]" in voyage.hint

    def test_tool_hints_use_extras_form(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        monkeypatch.setattr(doctor, "_has_module", lambda name: False)
        report = Report()
        doctor.check_tools(report)
        hints = " ".join(c.hint for c in report.checks)
        assert 'pip install "agent2kb[media]"' in hints
        assert 'pip install "agent2kb[whisper]"' in hints
        assert "pip install yt-dlp" not in hints and "pip install faster-whisper" not in hints


class TestOutputLanguageWarning:
    def _llm_checks(self, output_language):
        from agent2kb.doctor import Report, check_llm
        from agent2kb.models import LLMProvider, ProfileConfig

        p = ProfileConfig(name="x", s3_endpoint="http://e", access_key="a", secret_key="s",
                          bucket="b", prefix="x/", compile_provider=LLMProvider.GROQ,
                          groq_api_key="gsk_test", output_language=output_language)
        report = Report()
        check_llm(p, report)
        return [c for c in report.checks if c.name == "output language"]

    def test_unset_language_warns_that_english_is_used(self):
        checks = self._llm_checks(None)
        assert checks and checks[0].status == "warn" and "output_language" in checks[0].hint

    def test_explicit_language_is_quiet(self):
        assert self._llm_checks("en") == [] and self._llm_checks("zh-HK") == []
