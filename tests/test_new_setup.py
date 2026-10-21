"""`a2kb new` and `a2kb setup` against moto S3."""

import hashlib
import os
import stat
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws
from typer.testing import CliRunner

from agent2kb import config as kbconfig
from agent2kb import doctor as doctor_mod
from agent2kb import setup_wizard
from agent2kb.config import resolve_profile
from agent2kb.doctor import Report
from agent2kb.main import app

runner = CliRunner()

BUCKET = "kb-test-bucket"
STORAGE = {"bucket": BUCKET, "region": "us-east-1", "access_key": "AK", "secret_key": "SK"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("KB_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    h = tmp_path / "home"
    (h / ".kb").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(kbconfig, "USER_CONFIG_PATH", h / ".kb" / "config.yaml")
    monkeypatch.setattr(kbconfig, "USER_CONFIG_DIR", h / ".kb")
    monkeypatch.chdir(h)
    return h


@pytest.fixture
def s3():
    """moto S3 with the test bucket already created."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _keys(s3, prefix="") -> list[str]:
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def _config(home, **extra) -> Path:
    data = {
        "version": 2,
        "storage": STORAGE,
        "llm": {"provider": "groq", "groq_api_key": "gsk"},
        "defaults": {"output_language": "en"},
        "kbs": {},
        **extra,
    }
    path = home / ".kb" / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    os.chmod(path, 0o600)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flat(output: str) -> str:
    return " ".join(output.split())


# ─── a2kb new ──────────────────────────────────────────────────────────


class TestNew:
    def test_creates_skeleton_and_config_entry(self, home, s3):
        path = _config(home)
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code == 0, result.output
        keys = _keys(s3)
        for expected in (
            "raw/.gitkeep",
            "wiki/.gitkeep",
            "outputs/.gitkeep",
            "wiki/index.md",
            "wiki/log.md",
            "wiki/concepts/.gitkeep",
        ):
            assert f"kbs/research/{expected}" in keys
        assert all(k.startswith("kbs/research/") for k in keys)
        assert yaml.safe_load(path.read_text())["kbs"] == {"research": {}}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert resolve_profile("research").prefix == "kbs/research/"

    def test_existing_kbs_and_blocks_untouched(self, home, s3):
        path = _config(home, kbs={"old": {"prefix": "projects/old/", "output_language": "zh-HK"}})
        s3.put_object(Bucket=BUCKET, Key="projects/old/raw/a.md", Body=b"keep")
        runner.invoke(app, ["new", "fresh"])
        raw = yaml.safe_load(path.read_text())
        assert raw["kbs"]["old"] == {"prefix": "projects/old/", "output_language": "zh-HK"}
        assert raw["storage"] == STORAGE and raw["defaults"] == {"output_language": "en"}
        assert _keys(s3, "projects/old/") == ["projects/old/raw/a.md"]
        assert s3.get_object(Bucket=BUCKET, Key="projects/old/raw/a.md")["Body"].read() == b"keep"

    def test_custom_prefix(self, home, s3):
        _config(home)
        result = runner.invoke(app, ["new", "acme", "--prefix", "clients/acme"])
        assert result.exit_code == 0, result.output
        assert "clients/acme/wiki/index.md" in _keys(s3)
        assert resolve_profile("acme").prefix == "clients/acme/"

    @pytest.mark.parametrize(
        "name", ["A", "a", "-ab", "has space", "x" * 64, "under_score", "../x"]
    )
    def test_invalid_names(self, home, s3, name):
        path = _config(home)
        before = _digest(path)
        result = runner.invoke(app, ["new", name])
        assert result.exit_code != 0
        assert _digest(path) == before and _keys(s3) == []

    def test_super_is_reserved(self, home, s3):
        _config(home)
        result = runner.invoke(app, ["new", "super"])
        assert result.exit_code != 0 and "reserved" in _flat(result.output)
        assert _keys(s3) == []

    def test_refuses_existing_name(self, home, s3):
        path = _config(home, kbs={"research": {}})
        before = _digest(path)
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code != 0 and "already exists" in _flat(result.output)
        assert _digest(path) == before

    def test_refuses_prefix_with_objects_unless_adopt(self, home, s3):
        path = _config(home)
        s3.put_object(Bucket=BUCKET, Key="kbs/research/raw/mine.md", Body=b"precious")
        before = _digest(path)
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code != 0 and "--adopt" in _flat(result.output)
        assert _digest(path) == before
        assert _keys(s3) == ["kbs/research/raw/mine.md"]  # no skeleton was written either

        result = runner.invoke(app, ["new", "research", "--adopt"])
        assert result.exit_code == 0, result.output
        body = s3.get_object(Bucket=BUCKET, Key="kbs/research/raw/mine.md")["Body"].read()
        assert body == b"precious"
        assert "kbs/research/wiki/index.md" in _keys(s3)
        assert "research" in yaml.safe_load(path.read_text())["kbs"]

    def test_adopt_never_overwrites_existing_index(self, home, s3):
        _config(home)
        s3.put_object(Bucket=BUCKET, Key="kbs/research/wiki/index.md", Body=b"my index")
        result = runner.invoke(app, ["new", "research", "--adopt"])
        assert result.exit_code == 0, result.output
        body = s3.get_object(Bucket=BUCKET, Key="kbs/research/wiki/index.md")["Body"].read()
        assert body == b"my index"

    def test_sibling_prefix_is_not_a_conflict_but_nested_is(self, home, s3):
        _config(home, kbs={"a": {"prefix": "team/a/"}})
        ok = runner.invoke(app, ["new", "ab", "--prefix", "team/ab/"])
        assert ok.exit_code == 0, ok.output
        nested = runner.invoke(app, ["new", "inner", "--prefix", "team/a/inner/"])
        assert nested.exit_code != 0 and "overlaps" in _flat(nested.output)

    def test_here_writes_secretless_pointer(self, home, s3):
        _config(home)
        proj = home / "app"
        proj.mkdir()
        os.chdir(proj)
        result = runner.invoke(app, ["new", "research", "--here"])
        assert result.exit_code == 0, result.output
        pointer = proj / ".kb" / "config.yaml"
        assert yaml.safe_load(pointer.read_text()) == {"kb": "research"}
        assert "SK" not in pointer.read_text()
        # ... and the pointer works without -p, even from a subdirectory
        sub = proj / "sub"
        sub.mkdir()
        os.chdir(sub)
        assert kbconfig.autodetect_profile() == "research"

    def test_here_never_overwrites_an_old_flat_config(self, home, s3):
        path = _config(home)
        proj = home / "app"
        flat = proj / ".kb" / "config.yaml"
        flat.parent.mkdir(parents=True)
        flat.write_text("name: old\nsecret_key: precious\n")
        os.chdir(proj)
        before = _digest(path)
        result = runner.invoke(app, ["new", "research", "--here"])
        assert result.exit_code != 0 and "not overwriting" in _flat(result.output)
        assert flat.read_text() == "name: old\nsecret_key: precious\n"
        assert _digest(path) == before and _keys(s3) == []

    def test_refuses_old_format_config_untouched(self, home, s3):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(
            yaml.safe_dump(
                {"profiles": {"legacy": {"bucket": BUCKET, "prefix": "projects/legacy/"}}}
            )
        )
        before = _digest(legacy)
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code != 0 and "old-format" in _flat(result.output)
        assert _digest(legacy) == before and _keys(s3) == []

    def test_works_with_kb_config_env_beside_old_file(self, home, s3, monkeypatch):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(
            yaml.safe_dump(
                {"profiles": {"legacy": {"bucket": BUCKET, "prefix": "projects/legacy/"}}}
            )
        )
        before = _digest(legacy)
        v2 = home / "v2.yaml"
        v2.write_text(yaml.safe_dump({"version": 2, "storage": STORAGE, "kbs": {}}))
        monkeypatch.setenv("KB_CONFIG", str(v2))
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code == 0, result.output
        assert _digest(legacy) == before
        assert "research" in yaml.safe_load(v2.read_text())["kbs"]

    def test_missing_config_non_interactive_points_to_setup(self, home, s3):
        (home / ".kb").rmdir()
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code != 0 and "a2kb setup" in _flat(result.output)
        assert not (home / ".kb").exists()

    def test_missing_bucket_is_reported_without_writing(self, home, s3):
        path = _config(home)
        data = yaml.safe_load(path.read_text())
        data["storage"]["bucket"] = "no-such-bucket"
        path.write_text(yaml.safe_dump(data))
        before = _digest(path)
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code != 0 and "does not exist" in _flat(result.output)
        assert _digest(path) == before

    def test_kb_list_sees_it(self, home, s3):
        _config(home)
        runner.invoke(app, ["new", "research"])
        out = runner.invoke(app, ["list"]).output
        assert "research" in out and "kbs/research/" in out and "member" in out

    def test_new_kb_status_works_through_the_cli(self, home, s3):
        _config(home)
        runner.invoke(app, ["new", "research"])
        result = runner.invoke(app, ["-p", "research", "doctor", "--read-only"])
        assert "kbs/research/" in result.output


# ─── a2kb setup ────────────────────────────────────────────────────────

NON_INTERACTIVE = [
    "setup",
    "--non-interactive",
    "--preset",
    "aws",
    "--region",
    "us-east-1",
    "--bucket",
    BUCKET,
    "--access-key",
    "AK",
    "--secret-key",
    "SK",
    "--llm",
    "groq",
    "--api-key",
    "gsk_x",
]


class TestSetupChecksEffectiveProfile:
    def test_doctor_sees_kb_env_overrides(self, home, s3, monkeypatch):
        # KB_OLLAMA_HOST overrides the file for every command, so setup's own check must use
        # it too instead of the wizard's default local address.
        seen = {}

        def fake_doctor(profile, path, read_only=True):
            seen["host"] = profile.ollama_host
            return Report()

        monkeypatch.setenv("KB_OLLAMA_HOST", "http://ollama.example.com:11434")
        monkeypatch.setattr(doctor_mod, "run_doctor", fake_doctor)
        monkeypatch.setattr(doctor_mod, "render", lambda *a, **k: None)
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        assert seen["host"] == "http://ollama.example.com:11434"


class TestSetupBadEnvOverride:
    def test_bad_kb_override_is_reported_not_a_traceback(self, home, s3, monkeypatch):
        monkeypatch.setenv("KB_COMPILE_PROVIDER", "bogus")
        result = runner.invoke(app, NON_INTERACTIVE)
        assert "could not be applied" in _flat(result.output), result.output
        assert not isinstance(result.exception, ValueError)


class TestSetupNonInteractive:
    def test_writes_private_v2_file_with_privacy_first_defaults(self, home, s3):
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        path = home / ".kb" / "config.yaml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        raw = yaml.safe_load(path.read_text())
        assert raw["version"] == 2
        assert raw["storage"] == STORAGE  # AWS preset: no endpoint, no checksum_mode
        assert raw["llm"]["provider"] == "groq" and raw["llm"]["groq_api_key"] == "gsk_x"
        assert raw["defaults"] == {
            "output_language": "en",
            "allow_jina_reader": False,
            "allow_cloud_vision": False,
        }
        assert list(raw["kbs"]) == ["notes"]
        assert "kbs/notes/wiki/index.md" in _keys(s3)
        assert "no blocking problems" in _flat(result.output)  # a2kb doctor --read-only ran
        p = resolve_profile("notes")
        assert p.allow_jina_reader is False and p.allow_cloud_vision is False
        assert p.output_language == "en"

    def test_opt_in_flags(self, home, s3):
        result = runner.invoke(
            app,
            NON_INTERACTIVE
            + ["--allow-jina", "--allow-cloud-vision", "--language", "zh-HK", "--kb-name", "work"],
        )
        assert result.exit_code == 0, result.output
        p = resolve_profile("work")
        assert p.allow_jina_reader and p.allow_cloud_vision and p.output_language == "zh-HK"

    def test_env_supplies_credentials(self, home, s3, monkeypatch):
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVAK")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSK")
        monkeypatch.setenv("KB_BUCKET", BUCKET)
        monkeypatch.setenv("KB_GROQ_API_KEY", "gsk_env")
        args = ["setup", "--non-interactive", "--preset", "aws", "--llm", "groq"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        raw = yaml.safe_load((home / ".kb" / "config.yaml").read_text())
        assert raw["storage"]["access_key"] == "ENVAK" and raw["llm"]["groq_api_key"] == "gsk_env"

    def test_missing_bucket_needs_create_flag(self, home, s3):
        args = [a if a != BUCKET else "brand-new-bucket" for a in NON_INTERACTIVE]
        result = runner.invoke(app, args)
        assert result.exit_code != 0 and "--create-bucket" in _flat(result.output)
        assert not (home / ".kb" / "config.yaml").exists()
        result = runner.invoke(app, args + ["--create-bucket"])
        assert result.exit_code == 0, result.output
        names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "brand-new-bucket" in names

    @pytest.mark.parametrize(
        "bucket", ["ab", "UPPER", "has_underscore", "-lead", "a..b", "192.168.0.1", "x" * 64]
    )
    def test_bucket_name_validation(self, home, s3, bucket):
        args = [a if a != BUCKET else bucket for a in NON_INTERACTIVE]
        result = runner.invoke(app, args)
        assert result.exit_code != 0 and "Bucket name" in _flat(result.output)
        assert not (home / ".kb" / "config.yaml").exists()

    def test_refuses_old_format_config_and_suggests_kb_config(self, home, s3):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(
            yaml.safe_dump(
                {"profiles": {"legacy": {"bucket": BUCKET, "prefix": "projects/legacy/"}}}
            )
        )
        before = _digest(legacy)
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code != 0
        assert "KB_CONFIG=" in _flat(result.output) and "old-format" in _flat(result.output)
        assert _digest(legacy) == before and _keys(s3) == []

    def test_kb_config_env_target_leaves_old_file_alone(self, home, s3, monkeypatch):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(
            yaml.safe_dump(
                {"profiles": {"legacy": {"bucket": BUCKET, "prefix": "projects/legacy/"}}}
            )
        )
        before = _digest(legacy)
        target = home / "new" / "v2.yaml"
        monkeypatch.setenv("KB_CONFIG", str(target))
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        assert _digest(legacy) == before
        assert yaml.safe_load(target.read_text())["version"] == 2
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_rerun_keeps_existing_blocks_and_adds_a_kb(self, home, s3):
        runner.invoke(app, NON_INTERACTIVE)
        path = home / ".kb" / "config.yaml"
        raw = yaml.safe_load(path.read_text())
        # same values as stored (api key too) are fine
        result = runner.invoke(app, NON_INTERACTIVE + ["--kb-name", "second", "--language", "EN"])
        assert result.exit_code == 0, result.output
        new = yaml.safe_load(path.read_text())
        assert new["storage"] == raw["storage"] and new["llm"] == raw["llm"]
        assert new["defaults"] == raw["defaults"]  # not re-asked, not overwritten
        assert set(new["kbs"]) == {"notes", "second"}

    @pytest.mark.parametrize(
        "flags,named",
        [
            (["--bucket", "other-bucket"], "--bucket"),
            (["--language", "ja"], "--language"),
            (["--llm", "ollama"], "--llm"),
            (["--allow-jina"], "--allow-jina"),
            (["--endpoint", "http://elsewhere:9000"], "--endpoint"),
            (["--access-key", "DIFFERENT"], "--access-key"),
            (["--api-key", "gsk_other"], "--api-key"),
        ],
    )
    def test_flag_conflicting_with_stored_value_is_an_error(self, home, s3, flags, named):
        runner.invoke(app, NON_INTERACTIVE)
        path = home / ".kb" / "config.yaml"
        before = _digest(path)
        result = runner.invoke(app, _rerun_args(flags))
        out = _flat(result.output)
        assert result.exit_code == 2, out
        assert named in out and "KB_CONFIG=" in out
        assert "gsk_other" not in out and "DIFFERENT" not in out  # secrets are never echoed
        assert _digest(path) == before and "kbs/second/wiki/index.md" not in _keys(s3)

    @pytest.mark.parametrize(
        "preset,extra,endpoint,region",
        [
            ("r2", ["--account-id", "abc123"], "https://abc123.r2.cloudflarestorage.com", "auto"),
            (
                "b2",
                ["--region", "us-west-004"],
                "https://s3.us-west-004.backblazeb2.com",
                "us-west-004",
            ),
            (
                "wasabi",
                ["--region", "eu-central-1"],
                "https://s3.eu-central-1.wasabisys.com",
                "eu-central-1",
            ),
            ("minio", [], "http://localhost:9000", None),
            ("seaweedfs", ["--endpoint", "http://sw:8333"], "http://sw:8333", None),
            ("other", ["--endpoint", "https://s3.example.org"], "https://s3.example.org", None),
        ],
    )
    def test_non_aws_presets_pin_checksum_and_endpoint(self, preset, extra, endpoint, region):
        opts = setup_wizard.SetupOptions(
            preset=preset, bucket="my-bucket", access_key="a", secret_key="b", account_id=None
        )
        for flag, value in zip(extra[::2], extra[1::2]):
            setattr(opts, flag.lstrip("-").replace("-", "_"), value)
        block = setup_wizard.ask_storage(opts, interactive=False, console=None)
        assert block["endpoint"] == endpoint and block.get("region") == region
        assert block["checksum_mode"] == "when_required"
        if preset in ("minio", "seaweedfs"):
            assert block["addressing_style"] == "path"

    def test_r2_note_points_to_the_storage_providers_page(self):
        printed = []

        class _Console:
            def print(self, msg, **_kw):
                printed.append(msg)

        opts = setup_wizard.SetupOptions(
            preset="r2", bucket="my-bucket", access_key="a", secret_key="b", account_id="acct"
        )
        block = setup_wizard.ask_storage(opts, interactive=True, console=_Console())
        assert block["endpoint"] == "https://acct.r2.cloudflarestorage.com"
        assert any("docs/storage-providers.md" in m for m in printed)

    def test_aws_preset_has_no_endpoint_or_checksum_pin(self):
        opts = setup_wizard.SetupOptions(preset="aws", bucket="my-bucket", aws_chain=True)
        block = setup_wizard.ask_storage(opts, interactive=False, console=None)
        assert "endpoint" not in block and "checksum_mode" not in block
        assert "access_key" not in block and block["region"] == "us-east-1"

    def test_credentials_required_without_aws_chain(self):
        opts = setup_wizard.SetupOptions(preset="aws", bucket="my-bucket")
        with pytest.raises(setup_wizard.SetupAbort, match="--aws-chain"):
            setup_wizard.ask_storage(opts, interactive=False, console=None)

    def test_llm_required_non_interactive(self, home, s3):
        args = [a for a in NON_INTERACTIVE if a not in ("--llm", "groq", "--api-key", "gsk_x")]
        result = runner.invoke(app, args)
        assert result.exit_code != 0 and "--llm" in _flat(result.output)

    def test_cli_llm_choice_stores_no_key_or_model(self, monkeypatch):
        opts = setup_wizard.SetupOptions(llm="claude")
        block = setup_wizard.ask_llm(opts, interactive=False, console=_Quiet())
        assert block == {"provider": "cli", "command": "claude"}

    def test_ollama_without_model_stores_the_ollama_default(self):
        opts = setup_wizard.SetupOptions(llm="ollama")
        block = setup_wizard.ask_llm(opts, interactive=False, console=_Quiet())
        assert block == {"provider": "ollama", "model": "llama3.3:70b"}
        given = setup_wizard.SetupOptions(llm="ollama", model="qwen3:8b")
        assert setup_wizard.ask_llm(given, interactive=False, console=_Quiet())["model"] == (
            "qwen3:8b"
        )

    def test_ollama_setup_resolves_to_an_ollama_model(self, home, s3, monkeypatch):
        import urllib.error

        from agent2kb import doctor

        def down(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(doctor.urllib.request, "urlopen", down)
        args = [a for a in NON_INTERACTIVE if a not in ("--api-key", "gsk_x")]
        args[args.index("groq")] = "ollama"
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output  # written, but doctor cannot reach Ollama
        assert "ollama" in _flat(result.output)
        assert resolve_profile("notes").default_model == "llama3.3:70b"

    def test_profile_without_model_never_gets_the_groq_slug_for_local_providers(self):
        from agent2kb.compile.compiler import DEFAULT_MODELS
        from agent2kb.models import LOCAL_DEFAULT_MODELS, LLMProvider, ProfileConfig

        for provider, model in LOCAL_DEFAULT_MODELS.items():
            assert DEFAULT_MODELS[provider] == model  # one source of truth in practice
            p = ProfileConfig(name="x", bucket="b", prefix="x/", compile_provider=provider)
            assert p.default_model == model
        pinned = ProfileConfig(
            name="x", bucket="b", prefix="x/", compile_provider=LLMProvider.OLLAMA,
            default_model="qwen3:8b",
        )
        assert pinned.default_model == "qwen3:8b"

    def test_here_flag(self, home, s3):
        proj = home / "app"
        proj.mkdir()
        os.chdir(proj)
        result = runner.invoke(app, NON_INTERACTIVE + ["--here"])
        assert result.exit_code == 0, result.output
        assert yaml.safe_load((proj / ".kb" / "config.yaml").read_text()) == {"kb": "notes"}


def _rerun_args(overrides: list[str]) -> list[str]:
    """NON_INTERACTIVE with the given flags replacing the same flags, plus a new KB name."""
    args = list(NON_INTERACTIVE)
    i = 0
    while i < len(overrides):
        flag = overrides[i]
        value = (
            overrides[i + 1]
            if i + 1 < len(overrides) and not overrides[i + 1].startswith("--")
            else None
        )
        if flag in args:
            at = args.index(flag)
            if value is not None:
                args[at + 1] = value
        else:
            args += [flag] + ([value] if value is not None else [])
        i += 2 if value is not None else 1
    return args + ["--kb-name", "second"]


class TestSetupOrderAndWrites:
    def test_missing_llm_fails_before_creating_the_bucket(self, home, s3):
        args = [a for a in NON_INTERACTIVE if a not in ("--llm", "groq", "--api-key", "gsk_x")]
        args = [a if a != BUCKET else "brand-new-bucket" for a in args] + ["--create-bucket"]
        result = runner.invoke(app, args)
        assert result.exit_code == 2 and "--llm" in _flat(result.output)
        assert "brand-new-bucket" not in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert not (home / ".kb" / "config.yaml").exists()

    def test_taken_kb_name_fails_before_creating_the_bucket(self, home, s3):
        _config(home, kbs={"notes": {}})
        data = yaml.safe_load((home / ".kb" / "config.yaml").read_text())
        data["storage"]["bucket"] = "brand-new-bucket"
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump(data))
        result = runner.invoke(app, ["setup", "--non-interactive", "--create-bucket"])
        assert result.exit_code == 2 and "already exists" in _flat(result.output)
        assert "brand-new-bucket" not in [b["Name"] for b in s3.list_buckets()["Buckets"]]

    def test_fresh_setup_writes_the_file_once(self, home, s3):
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        assert not (home / ".kb" / "config.yaml.bak").exists()  # nothing was overwritten

    def test_backup_is_the_original_not_an_intermediate_state(self, home, s3):
        path = _config(home)  # an existing v2 file with storage/llm/defaults, no KBs
        original = path.read_text()
        result = runner.invoke(app, ["setup", "--non-interactive", "--kb-name", "first"])
        assert result.exit_code == 0, result.output
        assert (home / ".kb" / "config.yaml.bak").read_text() == original

    def test_interactive_not_logged_in_cli_is_explained_and_can_be_declined(
        self, home, s3, monkeypatch
    ):
        monkeypatch.setattr(
            setup_wizard,
            "detect_llms",
            lambda: {
                "claude": ("warn", "found at /x, not logged in"),
                "codex": ("warn", "not found on PATH"),
                "ollama": ("fail", "cannot reach"),
            },
        )
        answers = "\n".join(
            [
                "aws",
                "",
                BUCKET,
                "n",
                "AK",
                "SK",
                "claude",
                "n",  # not logged in: do not continue -> asked again
                "codex",
                "n",  # not installed: do not continue
                "groq",
                "gsk_ok",  # finally an API provider
                "",
                "n",
                "n",
                "notes",
                "",
            ]
        )
        result = runner.invoke(app, ["setup"], input=answers)
        out = _flat(result.output)
        assert result.exit_code == 0, out
        assert "claude auth login" in out and "codex login" in out
        raw = yaml.safe_load((home / ".kb" / "config.yaml").read_text())
        assert raw["llm"]["provider"] == "groq"

    def test_interactive_user_may_continue_with_a_not_logged_in_cli(self, home, s3, monkeypatch):
        monkeypatch.setattr(
            setup_wizard,
            "detect_llms",
            lambda: {
                "claude": ("warn", "not logged in"),
                "codex": ("fail", "x"),
                "ollama": ("fail", "x"),
            },
        )
        monkeypatch.setattr("agent2kb.doctor._check_cli_login", lambda *a, **k: None)
        answers = "\n".join(
            ["aws", "", BUCKET, "n", "AK", "SK", "claude", "y", "", "n", "n", "notes", ""]
        )
        result = runner.invoke(app, ["setup"], input=answers)
        assert "claude auth login" in _flat(result.output)
        raw = yaml.safe_load((home / ".kb" / "config.yaml").read_text())
        assert raw["llm"] == {"provider": "cli", "command": "claude"}


class _Quiet:
    def print(self, *a, **k):
        pass


class TestSetupInteractive:
    def test_wizard_end_to_end(self, home, s3, monkeypatch):
        monkeypatch.setattr(
            setup_wizard,
            "detect_llms",
            lambda: {
                "claude": ("warn", "not found on PATH"),
                "codex": ("warn", "not found on PATH"),
                "ollama": ("fail", "cannot reach"),
            },
        )
        answers = "\n".join(
            [
                "aws",  # storage preset
                "",  # region (default us-east-1)
                BUCKET,  # bucket
                "n",  # use the AWS credential chain? no
                "AK",
                "SK",  # keys
                "groq",  # llm
                "gsk_wizard",  # api key
                "",  # language: default en
                "n",
                "n",  # jina, vision stay off
                "wizardkb",  # first KB
                "",
            ]
        )
        result = runner.invoke(app, ["setup"], input=answers)
        assert result.exit_code == 0, result.output
        raw = yaml.safe_load((home / ".kb" / "config.yaml").read_text())
        assert raw["storage"] == STORAGE
        assert raw["defaults"] == {
            "output_language": "en",
            "allow_jina_reader": False,
            "allow_cloud_vision": False,
        }
        assert raw["llm"]["groq_api_key"] == "gsk_wizard"
        assert "wizardkb" in raw["kbs"]
        assert "gsk_wizard" not in result.output  # hidden input is never echoed or printed
        # The vision question names every route ingest may use, the configured CLI included.
        flat = _flat(result.output)
        assert "`codex` CLI the KB compiles with" in flat and "Gemini" in flat

    def test_wizard_offers_alternative_path_for_old_config(self, home, s3, monkeypatch):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(
            yaml.safe_dump(
                {"profiles": {"legacy": {"bucket": BUCKET, "prefix": "projects/legacy/"}}}
            )
        )
        before = _digest(legacy)
        monkeypatch.setattr(setup_wizard, "detect_llms", lambda: {})
        answers = "\n".join(
            [
                "y",
                "aws",
                "",
                BUCKET,
                "n",
                "AK",
                "SK",
                "groq",
                "gsk_alt",
                "en",
                "n",
                "n",
                "notes",
                "",
            ]
        )
        result = runner.invoke(app, ["setup"], input=answers)
        assert result.exit_code == 0, result.output
        assert _digest(legacy) == before
        alt = home / ".kb" / "config-v2.yaml"
        assert yaml.safe_load(alt.read_text())["version"] == 2
        assert "KB_CONFIG" in _flat(result.output)

    def test_declining_the_alternative_writes_nothing(self, home, s3):
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(yaml.safe_dump({"profiles": {}}))
        result = runner.invoke(app, ["setup"], input="n\n")
        assert result.exit_code != 0
        assert not (home / ".kb" / "config-v2.yaml").exists()

    def test_bucket_creation_is_confirmed(self, home, s3, monkeypatch):
        monkeypatch.setattr(setup_wizard, "detect_llms", lambda: {})
        base = ["aws", "", "brand-new-bucket", "n", "AK", "SK"]
        # refuse creation -> abort, nothing written
        result = runner.invoke(app, ["setup"], input="\n".join(base + ["n", ""]))
        assert result.exit_code != 0 and not (home / ".kb" / "config.yaml").exists()
        names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "brand-new-bucket" not in names


# ─── prefix-scoped credentials (HeadBucket denied) ─────────────────────


def _denied(op: str, code: str = "AccessDenied"):
    from botocore.exceptions import ClientError

    status = 404 if code == "NoSuchBucket" else 403
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


@pytest.fixture
def scoped(monkeypatch):
    """A key whose policy allows ListBucket only under one prefix and never HeadBucket."""
    from agent2kb.s3_client import S3Client

    state = {"listed": [], "allowed": "kbs/notes/", "list_code": "AccessDenied"}

    def head(self):
        raise _denied("HeadBucket")

    real_list = S3Client.list_one

    def list_one(self, sub_prefix=""):
        state["listed"].append(self.prefix + sub_prefix)
        if not (self.prefix + sub_prefix).startswith(state["allowed"]):
            raise _denied("ListObjectsV2", state["list_code"])
        return real_list(self, sub_prefix)

    monkeypatch.setattr(S3Client, "bucket_exists", head)
    monkeypatch.setattr(S3Client, "list_one", list_one)
    return state


class TestPrefixScopedCredentials:
    def test_setup_succeeds_when_only_the_kb_prefix_can_be_listed(self, home, s3, scoped):
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        assert "kbs/notes/wiki/index.md" in _keys(s3)
        assert "kbs/notes/" in scoped["listed"]
        assert "kbs/" not in scoped["listed"]  # never the generic probe prefix
        assert "cannot be inspected" in _flat(result.output)

    def test_setup_probes_a_custom_prefix(self, home, s3, scoped):
        scoped["allowed"] = "team/notes/"
        result = runner.invoke(app, NON_INTERACTIVE + ["--prefix", "team/notes/"])
        assert result.exit_code == 0, result.output
        assert "team/notes/wiki/index.md" in _keys(s3)

    def test_denied_list_is_still_an_error_and_writes_nothing(self, home, s3, scoped):
        scoped["allowed"] = "elsewhere/"
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code != 0 and "Access denied" in _flat(result.output)
        assert not (home / ".kb" / "config.yaml").exists()
        assert _keys(s3) == []

    def test_missing_bucket_behind_denied_head_is_reported_as_missing(self, home, s3, scoped):
        scoped["allowed"] = "elsewhere/"
        scoped["list_code"] = "NoSuchBucket"
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code != 0 and "does not exist" in _flat(result.output)
        assert "--create-bucket" in _flat(result.output)

    def test_new_works_with_scoped_key(self, home, s3, scoped):
        _config(home)
        scoped["allowed"] = "kbs/research/"
        result = runner.invoke(app, ["new", "research"])
        assert result.exit_code == 0, result.output
        assert "kbs/research/wiki/index.md" in _keys(s3)

    def test_bucket_state_distinguishes_404_from_present(self, home, s3):
        from agent2kb.s3_client import S3Client

        profile = setup_wizard.probe_profile(STORAGE, prefix="kbs/notes/")
        assert setup_wizard.bucket_state(S3Client(profile), profile) is True
        gone = setup_wizard.probe_profile({**STORAGE, "bucket": "no-such-bucket-here"})
        assert setup_wizard.bucket_state(S3Client(gone), gone) is False


# ─── attach an existing KB to a folder (`a2kb new <existing> --here`) ───


class TestAttachExisting:
    def test_tip_command_attaches_without_touching_config_or_s3(self, home, s3, monkeypatch):
        path = _config(home)
        created = runner.invoke(app, ["new", "notes"])
        assert created.exit_code == 0, created.output
        assert "a2kb new notes --here" in _flat(created.output)  # the tip we then follow
        before, keys = _digest(path), _keys(s3)
        proj = home / "app"
        proj.mkdir()
        os.chdir(proj)

        def no_s3(*a, **k):
            raise AssertionError("attaching must not call S3")

        monkeypatch.setattr(setup_wizard, "S3Client", no_s3)
        result = runner.invoke(app, ["new", "notes", "--here"])
        assert result.exit_code == 0, result.output
        assert "already exists" in _flat(result.output)
        assert yaml.safe_load((proj / ".kb" / "config.yaml").read_text()) == {"kb": "notes"}
        assert kbconfig.autodetect_profile() == "notes"
        assert _digest(path) == before and _keys(s3) == keys

    def test_attaching_twice_is_harmless(self, home, s3):
        _config(home, kbs={"notes": {}})
        proj = home / "app"
        proj.mkdir()
        os.chdir(proj)
        assert runner.invoke(app, ["new", "notes", "--here"]).exit_code == 0
        result = runner.invoke(app, ["new", "notes", "--here"])
        assert result.exit_code == 0, result.output

    def test_never_replaces_a_pointer_to_another_kb(self, home, s3):
        _config(home, kbs={"notes": {}, "alpha": {}})
        pointer = home / ".kb" / "config.yaml"
        proj = home / "app"
        (proj / ".kb").mkdir(parents=True)
        (proj / ".kb" / "config.yaml").write_text("kb: alpha\n")
        os.chdir(proj)
        result = runner.invoke(app, ["new", "notes", "--here"])
        assert result.exit_code != 0 and "not overwriting" in _flat(result.output)
        assert (proj / ".kb" / "config.yaml").read_text() == "kb: alpha\n"
        assert pointer.exists()

    def test_without_here_or_with_create_flags_existing_name_still_fails(self, home, s3):
        path = _config(home, kbs={"notes": {}})
        before = _digest(path)
        proj = home / "app"
        proj.mkdir()
        os.chdir(proj)
        for args in (
            ["new", "notes"],
            ["new", "notes", "--here", "--adopt"],
            ["new", "notes", "--here", "--prefix", "other/"],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code != 0 and "already exists" in _flat(result.output), args
        assert _digest(path) == before
        assert not (proj / ".kb").exists()
