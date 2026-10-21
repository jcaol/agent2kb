"""Tests for the S3 error translator and the CLI hook that uses it."""

import tempfile
from pathlib import Path

import pytest
import yaml
from botocore import exceptions as bex
from typer.testing import CliRunner

from agent2kb import main as kb_main
from agent2kb.errors import translate
from agent2kb.models import ProfileConfig

runner = CliRunner()


def _client_error(code: str, op: str = "HeadObject", message: str = "") -> bex.ClientError:
    return bex.ClientError({"Error": {"Code": code, "Message": message}}, op)


@pytest.fixture
def profile():
    return ProfileConfig(
        name="p",
        s3_endpoint="http://127.0.0.1:8333",
        access_key="k",
        secret_key="s",
        bucket="my-bucket",
        prefix="projects/p/",
    )


class TestTranslate:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("InvalidAccessKeyId", "bad_credentials"),
            ("SignatureDoesNotMatch", "bad_credentials"),
            ("403", "access_denied"),
            ("AccessDenied", "access_denied"),
            ("NoSuchBucket", "no_such_bucket"),
            ("InvalidBucketName", "bad_bucket_name"),
            ("RequestTimeTooSkewed", "clock_skew"),
            ("AuthorizationHeaderMalformed", "wrong_region"),
            ("SlowDown", "throttled"),
            ("XAmzContentSHA256Mismatch", "checksum"),
            ("SomethingNew", "s3_error"),
        ],
    )
    def test_client_error_codes(self, profile, code, expected):
        friendly = translate(_client_error(code), profile)
        assert friendly is not None
        assert friendly.code == expected
        assert friendly.message and friendly.hint

    def test_head_bucket_404_is_missing_bucket(self, profile):
        friendly = translate(_client_error("404", "HeadBucket"), profile)
        assert friendly.code == "no_such_bucket"
        assert "my-bucket" in friendly.message
        assert "a2kb doctor --fix" in friendly.hint

    def test_access_denied_mentions_prefix_scope(self, profile):
        friendly = translate(_client_error("403"), profile)
        assert "projects/p/" in friendly.hint

    def test_missing_credentials(self):
        friendly = translate(bex.NoCredentialsError())
        assert friendly.code == "no_credentials"
        assert "AWS credential chain" in friendly.hint

    def test_endpoint_down_shows_server_not_object_url(self, profile):
        exc = bex.EndpointConnectionError(endpoint_url="http://127.0.0.1:1/b/projects/p/x")
        friendly = translate(exc, profile)
        assert friendly.code == "unreachable"
        assert "http://127.0.0.1:8333" in friendly.message
        assert "b/projects/p/x" not in friendly.message

    def test_bad_bucket_name_param_validation(self, profile):
        exc = bex.ParamValidationError(report="Invalid bucket name \"kb\": must match ...")
        friendly = translate(exc, profile)
        assert friendly.code == "bad_bucket_name"
        assert "3-63" in friendly.hint

    def test_unknown_exception_is_not_swallowed(self):
        assert translate(RuntimeError("boom")) is None
        assert translate(PermissionError("outside prefix")) is None


def _config(tmpdir: str) -> Path:
    path = Path(tmpdir) / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "profiles": {
                    "test": {
                        "s3_endpoint": "http://localhost:8333",
                        "access_key": "k",
                        "secret_key": "s",
                        "bucket": "test-bucket",
                        "prefix": "test/",
                    }
                }
            }
        )
    )
    return path


class _FailingS3:
    def __init__(self, profile, exc):
        self.profile = profile
        self._exc = exc

    def exists(self, key):
        raise self._exc


class TestCliHook:
    def _run(self, monkeypatch, exc, *extra):
        monkeypatch.setattr(kb_main, "S3Client", lambda profile: _FailingS3(profile, exc))
        with tempfile.TemporaryDirectory() as tmp:
            args = ["-p", "test", "--config", str(_config(tmp)), *extra, "init"]
            return runner.invoke(kb_main.app, args)

    def test_known_error_is_one_liner_with_hint(self, monkeypatch):
        result = self._run(monkeypatch, _client_error("403"))
        assert result.exit_code == 1
        assert "Error:" in result.output and "Hint:" in result.output
        assert "Traceback" not in result.output

    def test_verbose_keeps_traceback(self, monkeypatch):
        result = self._run(monkeypatch, _client_error("403"), "-v")
        assert result.exit_code == 1
        assert isinstance(result.exception, bex.ClientError)

    def test_unknown_error_propagates(self, monkeypatch):
        result = self._run(monkeypatch, RuntimeError("boom"))
        assert result.exit_code == 1
        assert isinstance(result.exception, RuntimeError)

    def test_invalid_profile_config_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                yaml.dump({"profiles": {"t": {"bucket": "bkt", "prefix": "t/", "access_key": "k"}}})
            )
            result = runner.invoke(kb_main.app, ["-p", "t", "--config", str(path), "status"])
        assert result.exit_code != 0
        assert "secret_key" in result.output
