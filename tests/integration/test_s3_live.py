"""Manual integration tests against a real, throwaway S3-compatible server.

Skipped unless KB_IT_S3_ENDPOINT is set. Never point this at a server holding
real data: it creates and writes buckets named ``kbit-*``.

Example with a throwaway SeaweedFS (one identity, scratch dir, local ports)::

    echo '{"identities":[{"name":"it","credentials":[{"accessKey":"ITACCESSKEY",
      "secretKey":"ITSECRETKEY123456"}],"actions":["Admin","Read","Write","List"]}]}' > /tmp/s3.json
    weed server -dir=/tmp/weed-it -ip=127.0.0.1 -master.port=19333 -volume.port=18080 \\
      -filer -filer.port=18888 -s3 -s3.port=18333 -s3.config=/tmp/s3.json \\
      -master.volumeSizeLimitMB=64 &
    KB_IT_S3_ENDPOINT=http://127.0.0.1:18333 KB_IT_ACCESS_KEY=ITACCESSKEY \\
      KB_IT_SECRET_KEY=ITSECRETKEY123456 pytest tests/integration -v

SeaweedFS note: every bucket allocates volume slots, and the default server runs out after
a handful, after which writes hang. Start the throwaway server with ``-volume.max=100``.

Optional: KB_IT_REGION (e.g. ``auto`` for R2), KB_IT_BUCKET (an existing bucket;
default creates ``kbit-<random>`` through ``a2kb doctor --fix``).
"""

import json
import os
import uuid

import pytest
import yaml
from typer.testing import CliRunner

from agent2kb.config import resolve_profile
from agent2kb.main import app
from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client

ENDPOINT = os.environ.get("KB_IT_S3_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="set KB_IT_S3_ENDPOINT to run")

runner = CliRunner()


def _profile_dict(**over) -> dict:
    data = {
        "s3_endpoint": ENDPOINT,
        "access_key": os.environ.get("KB_IT_ACCESS_KEY", ""),
        "secret_key": os.environ.get("KB_IT_SECRET_KEY", ""),
        "bucket": "kbit-" + uuid.uuid4().hex[:10],
        "prefix": "projects/it/",
        "compile_provider": "ollama",
    }
    if os.environ.get("KB_IT_REGION"):
        data["region"] = os.environ["KB_IT_REGION"]
    data.update(over)
    return data


def _kb(tmp_path, *args, **over):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"profiles": {"it": _profile_dict(**over)}}))
    cfg.chmod(0o600)
    os.chdir(tmp_path)  # keep project-config discovery away from any real .kb/
    return runner.invoke(app, ["-p", "it", "--config", str(cfg), *args])


def _storage(result) -> dict[str, dict]:
    data = json.loads(result.output)
    return {c["name"]: c for c in data["checks"] if c["section"] == "Storage"}


def test_doctor_creates_bucket_then_passes(tmp_path):
    bucket = "kbit-" + uuid.uuid4().hex[:10]
    # Without --fix: reports the missing bucket (if the server does not auto-create
    # buckets on first PUT, as SeaweedFS does) and changes nothing.
    first = _kb(tmp_path, "doctor", "--json", "--read-only", bucket=bucket)
    if "bucket exists" in _storage(first) and _storage(first)["bucket exists"]["status"] == "fail":
        fixed = _kb(tmp_path, "doctor", "--fix", "--yes", "--json", bucket=bucket)
        assert _storage(fixed)["bucket exists"]["status"] == "ok"
    final = _kb(tmp_path, "doctor", "--json", bucket=bucket)
    storage = _storage(final)
    assert all(c["status"] in ("ok", "warn") for c in storage.values()), storage
    assert storage["write probe"]["status"] == "ok"


@pytest.mark.parametrize("mode", [None, "when_required", "when_supported"])
def test_checksum_modes_put_get_roundtrip(tmp_path, mode):
    """Unset keeps boto3's default; a server that rejects CRC32 needs when_required."""
    bucket = "kbit-" + uuid.uuid4().hex[:10]
    _kb(tmp_path, "doctor", "--fix", "--yes", "--json", "--read-only", bucket=bucket)
    profile = ProfileConfig(name="it", **_profile_dict(bucket=bucket, checksum_mode=mode))
    s3 = S3Client(profile)
    s3.upload("projects/it/raw/x.md", "héllo")
    assert s3.download("projects/it/raw/x.md") == "héllo"
    s3.delete("projects/it/raw/x.md")


def test_wrong_secret_is_friendly(tmp_path):
    result = _kb(tmp_path, "init", secret_key="definitely-not-the-secret")
    assert result.exit_code == 1
    assert "Hint:" in result.output and "Traceback" not in result.output


def test_missing_bucket_is_reported_by_doctor(tmp_path):
    result = _kb(tmp_path, "doctor", "--json", "--read-only")
    bucket_check = _storage(result)["bucket exists"]
    assert bucket_check["status"] == "fail"
    assert "does not exist" in bucket_check["detail"]


def test_short_bucket_name_is_friendly(tmp_path):
    result = _kb(tmp_path, "init", bucket="kb")
    assert result.exit_code == 1
    assert "Invalid bucket name" in result.output and "Traceback" not in result.output


def test_dead_endpoint_is_friendly(tmp_path):
    result = _kb(tmp_path, "init", s3_endpoint="http://127.0.0.1:1")
    assert result.exit_code == 1
    assert "Cannot reach the S3 endpoint" in result.output


def test_setup_new_and_adopt_roundtrip(tmp_path, monkeypatch):
    """`a2kb setup` (seaweedfs preset) then `a2kb new`: skeleton, pointer, doctor, no overwrite."""
    for k in list(os.environ):
        if k.startswith("KB_") and not k.startswith("KB_IT_"):
            monkeypatch.delenv(k)
    cfg = tmp_path / "v2" / "config.yaml"
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    bucket = "kbit-" + uuid.uuid4().hex[:10]
    res = runner.invoke(app, [
        "setup", "--non-interactive", "--preset", "seaweedfs", "--endpoint", ENDPOINT,
        "--bucket", bucket, "--access-key", os.environ.get("KB_IT_ACCESS_KEY", ""),
        "--secret-key", os.environ.get("KB_IT_SECRET_KEY", ""), "--create-bucket",
        "--llm", "ollama", "--kb-name", "first", "--here",
    ])
    assert res.exit_code in (0, 1), res.output  # 1 only if the Ollama check fails
    raw = yaml.safe_load(cfg.read_text())
    assert raw["storage"]["checksum_mode"] == "when_required"
    assert yaml.safe_load((tmp_path / ".kb" / "config.yaml").read_text()) == {"kb": "first"}

    profile = resolve_profile("first")
    s3 = S3Client(profile)
    assert s3.exists("kbs/first/wiki/index.md")

    # a second KB whose prefix already holds data is refused, then adopted without overwriting
    other = ProfileConfig(name="o", **{**_profile_dict(bucket=bucket), "prefix": "kbs/second/"})
    S3Client(other).upload("kbs/second/raw/mine.md", "precious")
    assert runner.invoke(app, ["new", "second"]).exit_code != 0
    assert runner.invoke(app, ["new", "second", "--adopt"]).exit_code == 0
    assert S3Client(other).download("kbs/second/raw/mine.md") == "precious"
