"""Provider-neutral S3 settings: config model, botocore config, bucket ops."""

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from agent2kb import s3_client
from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client, _is_aws_endpoint, build_boto_config


def _profile(**kw) -> ProfileConfig:
    base = dict(name="p", bucket="my-bucket", prefix="projects/p/")
    base.update(kw)
    return ProfileConfig(**base)


class TestModel:
    def test_legacy_profile_unchanged(self):
        p = _profile(s3_endpoint="http://x:8333", access_key="a", secret_key="b")
        assert p.region is None and p.addressing_style is None
        assert p.checksum_mode is None and p.aws_profile is None

    def test_no_endpoint_no_keys_is_valid(self):
        p = _profile()
        assert p.s3_endpoint is None and p.access_key is None

    def test_empty_strings_mean_unset(self):
        p = _profile(s3_endpoint="", access_key="", secret_key="")
        assert p.s3_endpoint is None and p.access_key is None and p.secret_key is None

    def test_one_key_without_other_is_error(self):
        with pytest.raises(ValidationError, match="secret_key"):
            _profile(access_key="a")
        with pytest.raises(ValidationError, match="access_key"):
            _profile(secret_key="b")

    def test_invalid_enums(self):
        with pytest.raises(ValidationError, match="addressing_style"):
            _profile(addressing_style="sideways")
        with pytest.raises(ValidationError, match="checksum_mode"):
            _profile(checksum_mode="never")


class TestBotoConfig:
    def test_is_aws_endpoint(self):
        assert _is_aws_endpoint(None)
        assert _is_aws_endpoint("https://s3.eu-west-1.amazonaws.com")
        assert not _is_aws_endpoint("http://127.0.0.1:8333")
        assert not _is_aws_endpoint("https://acct.r2.cloudflarestorage.com")
        assert not _is_aws_endpoint("https://evil-amazonaws.com.example.org")

    def test_seaweed_defaults_unchanged(self):
        cfg = build_boto_config(_profile(s3_endpoint="http://127.0.0.1:8333"))
        assert cfg.signature_version == "s3v4"
        assert cfg.retries == {"max_attempts": 3, "mode": "adaptive"}
        assert cfg.max_pool_connections == 32
        # Unset → boto3 default, for custom endpoints too (existing behaviour).
        assert cfg.request_checksum_calculation is None
        assert cfg.response_checksum_validation is None

    def test_unset_checksum_mode_is_boto3_default_everywhere(self):
        for endpoint in (None, "https://s3.us-west-2.amazonaws.com", "http://x:8333"):
            cfg = build_boto_config(_profile(s3_endpoint=endpoint))
            assert cfg.request_checksum_calculation is None
            assert cfg.response_checksum_validation is None

    def test_explicit_mode_wins(self):
        cfg = build_boto_config(
            _profile(s3_endpoint="http://x:8333", checksum_mode="when_supported")
        )
        assert cfg.request_checksum_calculation == "when_supported"
        cfg = build_boto_config(
            _profile(s3_endpoint="http://x:8333", checksum_mode="when_required")
        )
        assert cfg.request_checksum_calculation == "when_required"
        assert cfg.response_checksum_validation == "when_required"

    def test_old_botocore_is_guarded(self, monkeypatch):
        monkeypatch.setattr(s3_client, "_botocore_supports_checksum_config", lambda: False)
        cfg = build_boto_config(
            _profile(s3_endpoint="http://x:8333", checksum_mode="when_required")
        )
        assert cfg.signature_version == "s3v4"

    def test_addressing_style(self):
        assert build_boto_config(_profile(addressing_style="path")).s3 == {
            "addressing_style": "path"
        }
        assert build_boto_config(_profile()).s3 is None


class TestClientCredentials:
    def test_static_keys_and_region(self):
        c = S3Client(
            _profile(s3_endpoint="http://x:8333", access_key="AK", secret_key="SK", region="auto")
        )
        assert c._client._request_signer._credentials.access_key == "AK"
        assert c._client.meta.region_name == "auto"
        assert c._client.meta.endpoint_url == "http://x:8333"

    def test_default_chain_when_no_keys(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENVSECRET")
        boto3.setup_default_session()  # drop credentials cached by earlier tests
        c = S3Client(_profile(region="us-east-1"))
        assert c._client._request_signer._credentials.access_key == "ENVKEY"

    def test_aws_profile(self, monkeypatch, tmp_path):
        creds = tmp_path / "credentials"
        creds.write_text("[kbprof]\naws_access_key_id = PROFKEY\naws_secret_access_key = PS\n")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
        c = S3Client(_profile(aws_profile="kbprof", region="us-east-1"))
        assert c._client._request_signer._credentials.access_key == "PROFKEY"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


class TestBucketOps:
    def test_bucket_exists_and_create(self, aws_env):
        with mock_aws():
            c = S3Client(_profile(region="us-east-1"))
            assert c.bucket_exists() is False
            c.create_bucket()
            assert c.bucket_exists() is True

    def test_create_bucket_non_default_region_sets_location_constraint(self, aws_env):
        with mock_aws():
            c = S3Client(_profile(region="eu-west-1"))
            c.create_bucket()
            loc = boto3.client("s3", region_name="eu-west-1").get_bucket_location(
                Bucket="my-bucket"
            )
            assert loc["LocationConstraint"] == "eu-west-1"

    def test_other_errors_are_not_reported_as_missing(self, aws_env):
        from botocore.exceptions import ClientError

        c = S3Client(_profile(region="us-east-1"))

        def deny(**_):
            raise ClientError({"Error": {"Code": "403", "Message": "no"}}, "HeadBucket")

        c._client.head_bucket = deny
        with pytest.raises(ClientError):
            c.bucket_exists()

    def test_list_one_enforces_prefix(self, aws_env):
        with mock_aws():
            c = S3Client(_profile(region="us-east-1"))
            c.create_bucket()
            assert c.list_one() == []
            c.upload("projects/p/raw/a.md", "x")
            assert c.list_one("raw/") == ["projects/p/raw/a.md"]
