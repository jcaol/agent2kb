"""Contract tests for the public agent2kb interface.

Existing KBs, agents, cron jobs and another tool that reads ``.kb/config.yaml``
depend on the surface pinned here: command and flag names, profile resolution,
config field names, the S3 layout and the default output language. Changing any
of it silently breaks callers that live outside this repo, so a failure in this
file means "ask before changing", not "update the test".

Everything is offline: configs live in tmp dirs, HOME and cwd are redirected,
S3 is moto.
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
import typer
import yaml
from moto import mock_aws
from typer.testing import CliRunner

from agent2kb import config as kbconfig
from agent2kb.compile.schema import LANGUAGE_INSTRUCTION, build_compile_prompt
from agent2kb.config import ENV_MAP, autodetect_profile, resolve_profile
from agent2kb.ingest.base import IngestPipeline
from agent2kb.main import _resolve_actor, app
from agent2kb.models import Frontmatter, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client

REPO = Path(__file__).resolve().parent.parent
runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})

FLAT = {
    "s3_endpoint": "http://localhost:8333",
    "access_key": "ak",
    "secret_key": "sk",
    "bucket": "kb-bucket",
    "prefix": "projects/projA/",
    "compile_provider": "ollama",
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake HOME + cwd with every KB_* variable cleared.

    ``USER_CONFIG_PATH`` is computed at import time, so patching HOME alone is
    not enough; patch the module constant and ``Path.home`` too.
    """
    import os

    for k in list(os.environ):
        if k.startswith("KB_"):
            monkeypatch.delenv(k)
    h = tmp_path / "home"
    (h / ".kb").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(kbconfig, "USER_CONFIG_PATH", h / ".kb" / "config.yaml")
    monkeypatch.setattr(kbconfig, "USER_CONFIG_DIR", h / ".kb")
    monkeypatch.chdir(h)
    return h


@pytest.fixture
def project(home, monkeypatch):
    """A project dir under HOME with a flat ``.kb/config.yaml``; cwd is inside it."""
    p = home / "work" / "projA"
    (p / ".kb").mkdir(parents=True)
    (p / ".kb" / "config.yaml").write_text(yaml.dump({"name": "projA", **FLAT}))
    monkeypatch.chdir(p)
    return p


def _write_user_config(home: Path, profiles: dict) -> Path:
    path = home / ".kb" / "config.yaml"
    path.write_text(yaml.dump({"profiles": profiles}))
    return path


@pytest.fixture
def stub_profile(monkeypatch):
    """Replace profile resolution in the CLI callback; records the requested name."""
    seen: list[str] = []

    def fake_resolve(name, config_file=None, cli_overrides=None):
        seen.append(name)
        fields = {k: v for k, v in FLAT.items() if k != "compile_provider"}
        return ProfileConfig(name=name, **fields)

    monkeypatch.setattr("agent2kb.main.resolve_profile", fake_resolve)
    monkeypatch.setattr("agent2kb.main.load_config", lambda *_a, **_k: kbconfig.KBConfig())
    return seen


# --------------------------------------------------------------------------- 1. CLI surface

# command path -> long flags that existing agents/cron jobs pass
COMMAND_FLAGS = {
    "ingest": {"--url", "--file", "--folder", "--tags", "--recursive", "--actor", "--skip-safety"},
    "compile": {"--force", "--llm", "--all-profiles", "--allow-cloud-fallback"},
    "query": {"--actor", "--save", "--no-save", "--top-k", "--expand"},
    "promote": {"--file", "--actor", "--as", "--slug", "--delete-source"},
    "lint": {"--auto-fix", "--dry-run", "--deep", "--no-links", "--llm"},
    "stubs": {"--dry-run", "--min-refs"},
    "backup": {"--dir", "--keep", "--include-raw", "--no-raw"},
    "restore": {"--dir", "--date", "--target", "--apply", "--dry-run"},
    "rm": {"--url", "--file", "--id", "--slug", "--all", "--tag", "--older-than", "--raw",
           "--wiki", "--reingest", "--dry-run", "--yes", "--actor"},
    "status": set(),
    "manifest": {"--rebuild", "--show"},
    "embed": {"--force"},
    "outputs ls": {"--actor", "--since", "--limit", "--titles"},
    "outputs prune": {"--actor", "--older-than", "--apply"},
    "scaffold rules": {"--target", "--force", "--upgrade", "--dry-run"},
}

SHORT_FLAGS = {
    "ingest": {"-u", "-f", "-d", "-t", "-r"},
    "query": {"-k"},
    "promote": {"-f"},
    "rm": {"-y", "-u"},
    "compile": {"-f"},
}


def _find(path: str):
    cmd = typer.main.get_command(app)
    for part in path.split():
        cmd = cmd.commands[part]
    return cmd


def _opts(cmd) -> set[str]:
    return {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)}


@pytest.mark.parametrize("path", sorted(COMMAND_FLAGS))
def test_command_exists_with_flags(path):
    opts = _opts(_find(path))
    missing = COMMAND_FLAGS[path] - opts
    assert not missing, f"`a2kb {path}` lost flags: {sorted(missing)}"


@pytest.mark.parametrize("path", sorted(SHORT_FLAGS))
def test_short_flags(path):
    assert SHORT_FLAGS[path] <= _opts(_find(path))


def test_query_takes_question_argument_and_actor_choice():
    cmd = _find("query")
    args = [p.name for p in cmd.params if p.param_type_name == "argument"]
    assert args == ["question"]
    save = next(p for p in cmd.params if "--save" in p.opts)
    assert "--no-save" in save.secondary_opts
    assert save.default is None  # None = "decide from actor"; agents rely on this


def test_compile_strict_flag_and_env():
    """Added after the table was locked: cron wrappers pass `--strict` (or set
    KB_COMPILE_STRICT) to get exit 1 on failed files. Off by default, so a plain
    `a2kb compile` keeps exiting 0 when files fail."""
    strict = next(p for p in _find("compile").params if "--strict" in p.opts)
    assert strict.default is False
    assert strict.envvar == "KB_COMPILE_STRICT"


def test_rm_has_delete_alias():
    assert _find("delete") is not None
    assert _opts(_find("delete")) == _opts(_find("rm"))


def test_global_options():
    opts = _opts(typer.main.get_command(app))
    assert {"--profile", "-p", "--version", "--config", "--verbose", "-v",
            "--s3-endpoint", "--access-key", "--secret-key", "--bucket", "--prefix"} <= opts


@pytest.mark.parametrize("path", sorted(COMMAND_FLAGS))
def test_help_exits_zero_and_lists_flags(path, stub_profile):
    result = runner.invoke(app, ["-p", "test", *path.split(), "--help"])
    assert result.exit_code == 0, result.output
    for flag in COMMAND_FLAGS[path]:
        assert flag in result.output, f"`a2kb {path} --help` no longer mentions {flag}"


def test_top_level_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("ingest", "compile", "query", "promote", "lint", "stubs", "rm", "backup",
                 "restore", "manifest", "status", "outputs", "scaffold"):
        assert name in result.output


def test_version_needs_no_config(home):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert re.match(r"agent2kb \d+\.\d+", result.output)


def test_profile_flag_is_forwarded(home, stub_profile):
    for flag in ("-p", "--profile"):
        result = runner.invoke(app, [flag, "alpha", "outputs", "ls", "--actor", "robot"])
        assert result.exit_code == 2  # --actor robot is rejected after the profile resolved
    assert stub_profile == ["alpha", "alpha"]


def test_no_profile_anywhere_is_an_error(home):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "No profile specified" in result.output
    assert "Error:" in result.output and "Invalid value" not in result.output


def test_cli_autodetects_from_env_and_cwd(project, stub_profile, monkeypatch):
    runner.invoke(app, ["outputs", "ls", "--actor", "robot"])
    assert stub_profile == ["projA"]  # from cwd .kb/config.yaml name:
    monkeypatch.setenv("KB_PROFILE", "fromenv")
    runner.invoke(app, ["outputs", "ls", "--actor", "robot"])
    assert stub_profile == ["projA", "fromenv"]  # env wins over cwd


def test_actor_resolution(monkeypatch):
    monkeypatch.delenv("KB_ACTOR", raising=False)
    assert _resolve_actor("agent") == "agent"
    assert _resolve_actor("user") == "user"
    with pytest.raises(ValueError):
        _resolve_actor("robot")
    monkeypatch.setenv("KB_ACTOR", "agent")
    assert _resolve_actor(None) == "agent"
    monkeypatch.setenv("KB_ACTOR", "User")
    assert _resolve_actor(None) == "user"
    monkeypatch.setenv("KB_ACTOR", "agent")
    assert _resolve_actor("user") == "user"  # explicit flag beats env
    monkeypatch.delenv("KB_ACTOR")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _resolve_actor(None) == "agent"  # piped stdin => agent
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert _resolve_actor(None) == "user"


def test_console_script_entry_point():
    text = (REPO / "pyproject.toml").read_text()
    assert re.search(r'^a2kb\s*=\s*"agent2kb\.main:app"', text, re.M)
    # No bare `kb` entry point: it would clobber other packages' `kb` command.
    assert not re.search(r"^kb\s*=", text, re.M)


# Cron wrappers are part of the interface: they honour $A2KB_BIN (and still read the
# older $KB_BIN) and route model choice through llm_run instead of naming a model.
@pytest.mark.parametrize("name", ["compile", "lint", "stubs", "backup", "digest"])
def test_cron_scripts_honour_a2kb_bin(name):
    script = (REPO / "scripts" / f"kb-cron-{name}.sh").read_text()
    assert "A2KB_BIN" in script
    assert "KB_BIN" in script.replace("A2KB_BIN", "")  # old name kept as a fallback


def test_digest_script_goes_through_llm_run():
    assert "agent2kb.llm_run" in (REPO / "scripts" / "kb-cron-digest.sh").read_text()


# --------------------------------------------------------------------------- 2. profile resolution


class TestAutodetect:
    def test_env_profile(self, home, monkeypatch):
        monkeypatch.setenv("KB_PROFILE", "alpha")
        assert autodetect_profile() == "alpha"

    def test_cwd_config_name(self, project):
        assert autodetect_profile() == "projA"

    def test_walks_up_from_subdirectory(self, project, monkeypatch):
        sub = project / "docs" / "deep"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert autodetect_profile() == "projA"

    def test_env_beats_cwd(self, project, monkeypatch):
        monkeypatch.setenv("KB_PROFILE", "other")
        assert autodetect_profile() == "other"

    def test_single_profile_multi_config(self, home, monkeypatch):
        p = home / "p"
        (p / ".kb").mkdir(parents=True)
        (p / ".kb" / "config.yaml").write_text(yaml.dump({"profiles": {"only": dict(FLAT)}}))
        monkeypatch.chdir(p)
        assert autodetect_profile() == "only"

    def test_nothing_found(self, home):
        assert autodetect_profile() is None

    def test_super_is_never_autodetected(self, project, monkeypatch):
        (project / ".kb" / "config.yaml").write_text(yaml.dump({"name": "super", **FLAT}))
        assert autodetect_profile() is None
        monkeypatch.setenv("KB_PROFILE", "super")
        assert autodetect_profile() is None


class TestFlatProjectConfig:
    def test_flat_config_resolves(self, project):
        p = resolve_profile("projA")
        assert p.name == "projA"
        assert p.prefix == "projects/projA/"
        assert p.bucket == "kb-bucket"
        assert p.compile_provider == LLMProvider.OLLAMA
        assert not p.is_super

    def test_flat_config_without_name_takes_requested_name(self, project):
        data = dict(FLAT)
        (project / ".kb" / "config.yaml").write_text(yaml.dump(data))
        assert resolve_profile("whatever").name == "whatever"

    def test_prefix_gets_trailing_slash(self, project):
        (project / ".kb" / "config.yaml").write_text(
            yaml.dump({"name": "projA", **{**FLAT, "prefix": "projects/projA"}})
        )
        assert resolve_profile("projA").prefix == "projects/projA/"

    def test_project_config_cannot_be_super(self, project):
        (project / ".kb" / "config.yaml").write_text(yaml.dump({"name": "super", **FLAT}))
        with pytest.raises(PermissionError):
            resolve_profile("projA")

    def test_unknown_profile_is_an_error(self, home):
        with pytest.raises(ValueError):
            resolve_profile("nope")


class TestUserConfig:
    def test_profiles_format(self, home):
        _write_user_config(home, {"alpha": {**FLAT, "prefix": "projects/alpha/"}})
        p = resolve_profile("alpha")
        assert (p.name, p.prefix) == ("alpha", "projects/alpha/")

    def test_explicit_config_path(self, home, tmp_path):
        path = tmp_path / "elsewhere.yaml"
        path.write_text(yaml.dump({"profiles": {"x": dict(FLAT)}}))
        assert resolve_profile("x", config_path=path).bucket == "kb-bucket"

    def test_project_overrides_user_per_field(self, home, monkeypatch):
        """Per-field override still works for a project config that carries its own keys.

        CONTRACT CHANGE (security, approved): this test used to override ``bucket`` from a
        project file with NO keys, i.e. while signing with the user-level keys. A cloned repo
        could then send a user's data to an attacker's bucket (or another KB's prefix) with
        the user's own keys, so storage-target fields (endpoint, region, bucket, prefix,
        addressing_style) are now only honoured from a project layer that supplies its own
        access_key + secret_key. Non-target fields still override freely. No live config on
        the production host has a user-level file (all KBs are self-contained flat project
        configs with their own keys), so production behaviour is unchanged.
        """
        _write_user_config(
            home, {"projA": {**FLAT, "bucket": "from-user", "default_model": "m-user"}}
        )
        proj = home / "projA"
        (proj / ".kb").mkdir(parents=True)
        override = {
            "name": "projA", "bucket": "from-proj", "access_key": "projk", "secret_key": "projs",
        }
        (proj / ".kb" / "config.yaml").write_text(yaml.dump(override))
        monkeypatch.chdir(proj)
        p = resolve_profile("projA")
        assert p.bucket == "from-proj"
        assert p.access_key == "projk"
        assert p.default_model == "m-user"

    def test_project_without_keys_cannot_retarget_user_keys(self, home, monkeypatch):
        """The other half of the rule above: no own keys -> no bucket/prefix/endpoint."""
        _write_user_config(home, {"projA": {**FLAT, "default_model": "m-user"}})
        proj = home / "projA"
        (proj / ".kb").mkdir(parents=True)
        for field, value in (("bucket", "x"), ("prefix", "projects/other/"), ("region", "r")):
            (proj / ".kb" / "config.yaml").write_text(yaml.dump({"name": "projA", field: value}))
            monkeypatch.chdir(proj)
            with pytest.raises(PermissionError):
                resolve_profile("projA")
        # a non-target override is fine
        (proj / ".kb" / "config.yaml").write_text(
            yaml.dump({"name": "projA", "default_model": "m-proj"})
        )
        assert resolve_profile("projA").default_model == "m-proj"

    def test_super_is_admin(self, home):
        _write_user_config(
            home, {"super": {**FLAT, "prefix": "", "compile_provider": "groq"}}
        )
        p = resolve_profile("super")
        assert p.name == "super"
        assert p.is_super
        assert p.prefix == ""

    def test_super_only_from_user_config(self, home):
        with pytest.raises(PermissionError):
            resolve_profile("super")  # nothing in user config
        _write_user_config(home, {"alpha": dict(FLAT)})
        with pytest.raises(PermissionError):
            resolve_profile("super")

    def test_env_cannot_make_super(self, home, monkeypatch):
        _write_user_config(home, {"alpha": dict(FLAT)})
        monkeypatch.setenv("KB_PROFILE", "super")
        with pytest.raises(PermissionError):
            resolve_profile("alpha")

    def test_all_profile_fields_in_user_config_roundtrip(self, home):
        """Every field name an existing config may carry stays a valid field."""
        fields = {
            "s3_endpoint", "access_key", "secret_key", "bucket", "prefix", "compile_provider",
            "groq_api_key", "openrouter_api_key", "gemini_api_key", "ollama_host",
            "default_model", "cli_command", "cli_reasoning_effort", "aux_model", "aux_effort",
            "cli_fallback_command", "cli_fallback_model", "cli_fallback_effort",
            "concept_threshold", "allow_jina_reader", "allow_cloud_vision", "privacy_mode",
            "google_safe_browsing_key", "openai_api_key", "voyage_api_key",
            "rerank_enabled", "embedding_model", "rerank_model", "embedding_provider",
        }
        assert fields <= set(ProfileConfig.model_fields)


class TestEnvOverrides:
    # Locked table: env var suffix -> field. New entries may be added; none may change.
    EXPECTED = {
        "S3_ENDPOINT": "s3_endpoint", "ACCESS_KEY": "access_key", "SECRET_KEY": "secret_key",
        "BUCKET": "bucket", "PREFIX": "prefix", "PROFILE": "name",
        "COMPILE_PROVIDER": "compile_provider", "GROQ_API_KEY": "groq_api_key",
        "OPENROUTER_API_KEY": "openrouter_api_key", "GEMINI_API_KEY": "gemini_api_key",
        "OLLAMA_HOST": "ollama_host", "DEFAULT_MODEL": "default_model",
        "CLI_COMMAND": "cli_command", "CLI_REASONING_EFFORT": "cli_reasoning_effort",
        "AUX_MODEL": "aux_model", "AUX_EFFORT": "aux_effort",
        "CLI_FALLBACK_COMMAND": "cli_fallback_command",
        "CLI_FALLBACK_MODEL": "cli_fallback_model", "CLI_FALLBACK_EFFORT": "cli_fallback_effort",
    }

    def test_env_map_is_stable(self):
        for suffix, field in self.EXPECTED.items():
            assert ENV_MAP.get(suffix) == field
        assert kbconfig.ENV_PREFIX == "KB_"

    @pytest.mark.parametrize("suffix", sorted(ENV_MAP))
    def test_every_env_key_overrides_its_field(self, home, monkeypatch, suffix):
        # Boolean fields are parsed strictly; every other value is passed through verbatim.
        if ENV_MAP[suffix] in kbconfig._BOOL_FIELDS:
            raw, want = "true", True
        elif suffix == "COMPILE_PROVIDER":
            raw = want = "ollama"
        else:
            raw = want = "v-1"
        monkeypatch.setenv("KB_" + suffix, raw)
        got = kbconfig._load_env_overrides()
        assert got == {ENV_MAP[suffix]: want}
        assert ENV_MAP[suffix] in ProfileConfig.model_fields

    def test_env_map_covers_the_privacy_switches(self):
        # Added after the table was locked (additive only; no existing entry changed).
        assert ENV_MAP["PRIVACY_MODE"] == "privacy_mode"
        assert ENV_MAP["ALLOW_JINA_READER"] == "allow_jina_reader"
        assert ENV_MAP["ALLOW_CLOUD_VISION"] == "allow_cloud_vision"

    @pytest.mark.parametrize("word", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_privacy_env_true_words(self, home, monkeypatch, word):
        monkeypatch.setenv("KB_PRIVACY_MODE", word)
        assert kbconfig._load_env_overrides() == {"privacy_mode": True}

    @pytest.mark.parametrize("word", ["0", "false", "No", "off"])
    def test_privacy_env_false_words(self, home, monkeypatch, word):
        monkeypatch.setenv("KB_ALLOW_JINA_READER", word)
        assert kbconfig._load_env_overrides() == {"allow_jina_reader": False}

    @pytest.mark.parametrize("word", ["", "ture", "2", "enabled", "y"])
    def test_privacy_env_rejects_other_values(self, home, monkeypatch, word):
        monkeypatch.setenv("KB_PRIVACY_MODE", word)
        with pytest.raises(kbconfig.ConfigError, match="KB_PRIVACY_MODE.*not a valid boolean"):
            kbconfig._load_env_overrides()

    def test_privacy_env_reaches_the_resolved_profile(self, project, monkeypatch):
        monkeypatch.setenv("KB_PRIVACY_MODE", "1")
        assert resolve_profile("projA").privacy_mode is True

    def test_env_beats_project_and_user_config(self, project, monkeypatch):
        monkeypatch.setenv("KB_BUCKET", "env-bucket")
        monkeypatch.setenv("KB_DEFAULT_MODEL", "env-model")
        monkeypatch.setenv("KB_AUX_EFFORT", "low")
        p = resolve_profile("projA")
        assert (p.bucket, p.default_model, p.aux_effort) == ("env-bucket", "env-model", "low")

    def test_cli_flags_beat_env(self, project, monkeypatch):
        monkeypatch.setenv("KB_BUCKET", "env-bucket")
        assert resolve_profile("projA", cli_overrides={"bucket": "flag"}).bucket == "flag"

    def test_env_only_profile(self, home, monkeypatch):
        """CI / container use: no config files at all, just KB_* variables."""
        monkeypatch.setenv("KB_S3_ENDPOINT", "http://s3:8333")
        monkeypatch.setenv("KB_ACCESS_KEY", "ak")
        monkeypatch.setenv("KB_SECRET_KEY", "sk")
        monkeypatch.setenv("KB_BUCKET", "b")
        monkeypatch.setenv("KB_PREFIX", "projects/ci")
        monkeypatch.setenv("KB_COMPILE_PROVIDER", "ollama")
        p = resolve_profile("ci")
        assert p.prefix == "projects/ci/"
        assert p.compile_provider == LLMProvider.OLLAMA


# --------------------------------------------------------------------------- 3. groq_api_key


def test_flat_config_with_groq_api_key_line(project):
    """Another tool regex-reads ``groq_api_key: "gsk_..."`` from this file."""
    text = (
        'name: projA\n'
        's3_endpoint: "http://localhost:8333"\n'
        'access_key: "ak"\n'
        'secret_key: "sk"\n'
        'bucket: "kb-bucket"\n'
        'prefix: "projects/projA/"\n'
        'compile_provider: "groq"\n'
        'groq_api_key: "gsk_TESTONLY0000"\n'
    )
    cfg = project / ".kb" / "config.yaml"
    cfg.write_text(text)
    assert re.search(r'^groq_api_key:\s*"(gsk_[^"]+)"', cfg.read_text(), re.M)
    p = resolve_profile("projA")
    assert p.groq_api_key == "gsk_TESTONLY0000"
    assert p.compile_provider == LLMProvider.GROQ
    assert autodetect_profile() == "projA"


def test_groq_api_key_in_user_profile(home):
    _write_user_config(home, {"alpha": {**FLAT, "groq_api_key": "gsk_TESTONLY1111"}})
    assert resolve_profile("alpha").groq_api_key == "gsk_TESTONLY1111"


def test_old_config_without_new_fields_loads(project):
    """Only the 6 original fields: every other field must default, not be required."""
    p = resolve_profile("projA")
    assert p.groq_api_key is None
    assert p.privacy_mode is False
    assert p.allow_jina_reader is True


# --------------------------------------------------------------------------- 4. llm_run


def test_llm_run_importable_with_main():
    import agent2kb.llm_run as llm_run

    assert callable(llm_run.main)


def test_llm_run_module_help():
    out = subprocess.run(
        [sys.executable, "-m", "agent2kb.llm_run", "--help"],
        capture_output=True, text=True, timeout=60, cwd=REPO,
    )
    assert out.returncode == 0, out.stderr
    for flag in ("--profile", "--purpose", "--system"):
        assert flag in out.stdout


def test_llm_run_exit_codes_documented():
    import agent2kb.llm_run as llm_run

    assert "3 every configured CLI is down" in llm_run.__doc__


def test_llm_run_empty_prompt_exits_2(monkeypatch):
    import io

    import agent2kb.llm_run as llm_run

    monkeypatch.setattr(sys, "stdin", io.StringIO("  \n"))
    assert llm_run.main(["--profile", "x"]) == 2


# --------------------------------------------------------------------------- 5. S3 layout


@pytest.fixture
def proj_profile():
    return ProfileConfig(
        name="projA", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="kb-bucket", prefix="projects/projA/",
        compile_provider=LLMProvider.OLLAMA,
    )


@pytest.fixture
def s3(proj_profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="kb-bucket")
        client = S3Client(proj_profile)
        client._client = conn
        yield client


def test_project_prefix_enforcement(s3):
    for sub in ("raw", "wiki", "outputs"):
        s3.upload(f"projects/projA/{sub}/x.md", "hi")
        assert s3.download(f"projects/projA/{sub}/x.md") == "hi"
        assert s3.list_keys(sub_prefix=f"{sub}/", suffix=".md") == [f"projects/projA/{sub}/x.md"]
    for bad in ("projects/projA2/raw/x.md", "projects/other/wiki/x.md", "personal/raw/x.md",
                "raw/x.md", "projects/projA/../projB/raw/x.md"):
        with pytest.raises(PermissionError):
            s3.upload(bad, "no")


def test_project_profile_cannot_touch_meta(s3):
    with pytest.raises(PermissionError):
        s3.upload("_meta/schema.md", "no")
    with pytest.raises(PermissionError):
        s3.download("_meta/schema.md")


def test_super_reaches_every_prefix_and_meta():
    sp = ProfileConfig(
        name="super", s3_endpoint="http://localhost:5000", access_key="t", secret_key="t",
        bucket="kb-bucket", prefix="", compile_provider=LLMProvider.GROQ,
    )
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="kb-bucket")
        c = S3Client(sp)
        c._client = conn
        for key in ("personal/wiki/a.md", "projects/projA/raw/b.md", "_meta/schema.md"):
            c.upload(key, "x")
            assert c.download(key) == "x"


def test_url_ingest_writes_raw_with_expected_name_and_frontmatter(
    s3, proj_profile, monkeypatch
):
    from agent2kb.ingest.handlers import url as url_handler

    body = "A paragraph of real article text. " * 10

    class Resp:
        headers = {"content-type": "text/html"}
        text = "<html></html>"
        content = b""

    monkeypatch.setattr(url_handler, "_validate_url", lambda u: None)
    monkeypatch.setattr(url_handler, "_safe_get", lambda u: Resp())
    monkeypatch.setattr(url_handler, "_extract_content", lambda *a, **k: ("My Test Article", body))
    monkeypatch.setattr(IngestPipeline, "_llm_entities", lambda *a, **k: [])

    pipe = IngestPipeline(profile=proj_profile, s3=s3, tags=["alpha", "beta"])
    result = pipe.ingest_url("https://example.com/post", skip_safety=True)

    assert result.success, result.error
    today = datetime.datetime.now().strftime("%Y%m%d")
    assert re.fullmatch(
        rf"projects/projA/raw/{today}-My-Test-Article-[0-9a-f]{{8}}\.md", result.raw_key
    ), result.raw_key
    assert s3.list_keys(sub_prefix="raw/", suffix=".md") == [result.raw_key]

    fm, text = Frontmatter.from_yaml_block(s3.download(result.raw_key))
    assert fm.title == "My Test Article"
    assert fm.source == "https://example.com/post"
    assert fm.original_link == "https://example.com/post"
    assert fm.tags == ["alpha", "beta"]
    assert fm.date == datetime.datetime.now().strftime("%Y-%m-%d")
    assert fm.source_type.value == "url"
    assert body.strip() in text

    raw_yaml = s3.download(result.raw_key).split("---")[1]
    keys = set(yaml.safe_load(raw_yaml))
    # source_kind is optional on legacy raws (None is dropped); the rest are always written.
    assert {"title", "source", "source_type", "date", "tags", "original_link"} <= keys


@pytest.fixture
def short_prefix_s3():
    """Prefix ``projects/proj/`` so ``projects/projA/`` is a string-prefix sibling."""
    prof = ProfileConfig(
        name="proj", s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket="kb-bucket", prefix="projects/proj/",
        compile_provider=LLMProvider.OLLAMA,
    )
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="kb-bucket")
        client = S3Client(prof)
        client._client = conn
        yield client


SIBLINGS = ["projects/projA/raw/x.md", "projects/proj-x/wiki/x.md", "projects/proj2/outputs/x.md",
            "projects", "projects/prox/raw/x.md"]


@pytest.mark.parametrize("key", SIBLINGS)
def test_enforce_prefix_is_segment_exact(short_prefix_s3, key):
    with pytest.raises(PermissionError):
        short_prefix_s3._enforce_prefix(key)


@pytest.mark.parametrize("sub", ["raw", "wiki", "outputs", "outputs/agent"])
def test_enforce_prefix_accepts_own_subprefixes(short_prefix_s3, sub):
    key = f"projects/proj/{sub}/x.md"
    assert short_prefix_s3._enforce_prefix(key) == key


@pytest.mark.parametrize("key", SIBLINGS[:3])
def test_every_s3_operation_rejects_sibling_prefix(short_prefix_s3, key):
    s3c = short_prefix_s3
    for op in (
        lambda: s3c.upload(key, "x"),
        lambda: s3c.download(key),
        lambda: s3c.download_bytes(key),
        lambda: s3c.exists(key),
        lambda: s3c.delete(key),
    ):
        with pytest.raises(PermissionError):
            op()


def test_list_cannot_escape_into_sibling_prefix(short_prefix_s3):
    short_prefix_s3.upload("projects/proj/raw/mine.md", "x")
    for sub in ("../projA/raw/", "raw/../../projA/raw/"):
        with pytest.raises(PermissionError):
            short_prefix_s3.list_keys(sub_prefix=sub)
    assert short_prefix_s3.list_keys(sub_prefix="raw/") == ["projects/proj/raw/mine.md"]


# Raw filename contract: every handler writes <date>-<slug>-<hash8>.md. audio/video/document
# used to omit the hash, so two same-day ingests with one title overwrote each other; keys
# already in S3 in the old shape are untouched (dedup recognises both).
_DATE_SLUG = r"\d{8}-[^/]+"
_HASHED = rf"{_DATE_SLUG}-[0-9a-f]{{8}}\.md"


@pytest.fixture
def handler_env(s3, proj_profile, monkeypatch, tmp_path):
    """Drive each handler with the heavy deps (whisper, yt-dlp, pandoc, vision) stubbed."""
    from agent2kb.ingest.handlers import audio, document, image, markdown, video

    monkeypatch.setattr(audio.AudioHandler, "_transcribe", lambda self, p: "hello transcript")
    monkeypatch.setattr(video.VideoHandler, "_transcribe", lambda self, p: "hello transcript")
    monkeypatch.setattr(video.VideoHandler, "_get_video_title", lambda self, u: "Some Video")
    monkeypatch.setattr(video, "validate_url", lambda u: None)
    monkeypatch.setattr(
        document.DocumentHandler, "_convert_pandoc", lambda self, p: ("Doc Title", "doc body")
    )

    def fake_run(cmd, **kw):
        # yt-dlp: create the file it would have written next to `-o`.
        Path(cmd[cmd.index("-o") + 1] + ".wav").write_bytes(b"RIFF")

    monkeypatch.setattr(video.subprocess, "run", fake_run)
    proj_profile.allow_cloud_vision = False  # no vision call; placeholder body instead

    def make(name: str, data: bytes) -> Path:
        f = tmp_path / name
        f.write_bytes(data)
        return f

    return {
        "profile": proj_profile, "s3": s3, "make": make,
        "audio": audio.AudioHandler, "video": video.VideoHandler, "image": image.ImageHandler,
        "document": document.DocumentHandler, "markdown": markdown.MarkdownHandler,
    }


HANDLER_CASES = [
    # (id, handler, file name, file bytes, source is file path?, expected name regex)
    ("markdown", "markdown", "note.md", b"# Heading\n\nbody text", True, _HASHED),
    ("image", "image", "pic-one.png", b"\x89PNG fake", True, _HASHED),
    ("audio", "audio", "talk.mp3", b"ID3", True, _HASHED),
    ("video-local", "video", "clip.mp4", b"\x00\x00", True, _HASHED),
    ("video-url", "video", None, None, False, _HASHED),
    ("document", "document", "paper.txt", b"plain", True, _HASHED),
]


@pytest.mark.parametrize(
    "kind, name, fname, data, is_file, pattern", HANDLER_CASES, ids=[c[0] for c in HANDLER_CASES]
)
def test_every_handler_writes_dated_raw_key(handler_env, kind, name, fname, data, is_file, pattern):
    env = handler_env
    handler = env[name](profile=env["profile"], s3=env["s3"], tags=["t1"])
    if is_file:
        path = env["make"](fname, data)
        result = handler.process(source=str(path), file_path=str(path))
    else:
        result = handler.process(source="https://example.com/v")
    assert result.success, result.error

    today = datetime.datetime.now().strftime("%Y%m%d")
    prefix = "projects/projA/raw/"
    assert result.raw_key.startswith(prefix), result.raw_key
    filename = result.raw_key[len(prefix):]
    assert re.fullmatch(pattern, filename), result.raw_key
    assert filename.startswith(today + "-"), f"{kind}: date prefix is not YYYYMMDD of today"
    assert env["s3"].list_keys(sub_prefix="raw/", suffix=".md") == [result.raw_key]
    fm, _ = Frontmatter.from_yaml_block(env["s3"].download(result.raw_key))
    assert fm.title
    assert fm.date == datetime.datetime.now().strftime("%Y-%m-%d")


@pytest.mark.parametrize("name, fname", [
    ("audio", "talk.mp3"), ("video", "clip.mp4"), ("document", "paper.txt"),
])
def test_same_day_same_title_different_content_does_not_overwrite(
    handler_env, monkeypatch, name, fname
):
    from agent2kb.ingest.handlers import audio, document, video

    env = handler_env
    bodies = iter(["first body", "second body"])
    monkeypatch.setattr(audio.AudioHandler, "_transcribe", lambda self, p: next(bodies))
    monkeypatch.setattr(video.VideoHandler, "_transcribe", lambda self, p: next(bodies))
    monkeypatch.setattr(
        document.DocumentHandler, "_convert_pandoc", lambda self, p: ("T", next(bodies))
    )
    path = env["make"](fname, b"data")
    handler = env[name](profile=env["profile"], s3=env["s3"], tags=[])
    first = handler.process(source=str(path), file_path=str(path))
    second = handler.process(source=str(path), file_path=str(path))
    assert first.success and second.success
    assert first.raw_key != second.raw_key
    assert len(env["s3"].list_keys(sub_prefix="raw/", suffix=".md")) == 2

    # dedup now sees these keys through their hash suffix
    from agent2kb.dedup import RawIndex, hash_suffix

    index = RawIndex(env["s3"])
    assert index.by_hash(hash_suffix(first.raw_key)) == first.raw_key


def test_frontmatter_roundtrips_source_kind_and_legacy_keys():
    fm = Frontmatter(title="t", source="s", tags=["a"], source_kind="query_output")
    block = fm.to_yaml_block()
    data = yaml.safe_load(block.split("---")[1])
    assert data["source_kind"] == "query_output"
    assert {"title", "source", "date", "tags", "original_link"} <= set(data)
    back, _ = Frontmatter.from_yaml_block(block + "\nbody")
    assert back.source_kind.value == "query_output"


def test_source_kind_inference_follows_layout():
    infer = Frontmatter.infer_kind_from_key
    assert infer("projects/p/raw/a.md").value == "raw"
    assert infer("projects/p/outputs/agent/a.md").value == "query_output"
    assert infer("projects/p/outputs/weekly/a.md").value == "digest"
    assert infer("projects/p/wiki/concepts/a.md").value == "concept"
    assert infer("projects/p/wiki/index.md").value == "meta"


# --------------------------------------------------------------------------- 6. default language


# Intended contract change (0.3.2): an unset output_language means English. The original
# Cantonese prompt is kept unchanged, now under output_language: yue (see below).


def test_default_compile_prompt_is_english():
    msgs = build_compile_prompt("some raw content", source="https://x", source_type="url")
    system = msgs[0]["content"]
    assert msgs[0]["role"] == "system"
    assert "ALWAYS write in English" in system
    assert "Cantonese" not in system
    assert not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", system)
    assert LANGUAGE_INSTRUCTION.rstrip() not in system
    assert "{LANGUAGE_INSTRUCTION}" not in system


# Intended contract change: zh-HK became neutral Hong Kong Traditional Chinese, and the
# Cantonese prompt it used to select moved, byte for byte, to output_language: yue.


def test_yue_compile_prompt_is_cantonese_traditional_chinese():
    msgs = build_compile_prompt(
        "some raw content", source="https://x", source_type="url", output_language="yue"
    )
    system = msgs[0]["content"]
    assert msgs[0]["role"] == "system"
    assert "Cantonese-style Traditional Chinese" in system
    assert "繁體中文" in system
    assert "廣東話語氣" in system
    assert "ALWAYS write in" in system
    # the shared instruction is spliced in whole, and no placeholder is left behind
    assert LANGUAGE_INSTRUCTION.rstrip() in system
    assert "{LANGUAGE_INSTRUCTION}" not in system


def test_zh_hk_compile_prompt_is_neutral_traditional_chinese():
    msgs = build_compile_prompt(
        "some raw content", source="https://x", source_type="url", output_language="zh-HK"
    )
    system = msgs[0]["content"]
    assert "Hong Kong usage" in system
    assert "繁體中文" in system
    assert "廣東話" not in system
    assert "Cantonese" not in system


def test_language_instruction_keeps_english_tech_terms():
    assert "Keep commonly used English IT/tech terms in English" in LANGUAGE_INSTRUCTION


def test_compile_output_format_markers_unchanged():
    """The parser splits on ---TAGS---; the prompt must keep asking for it."""
    system = build_compile_prompt("c")[0]["content"]
    assert "---TAGS---" in system
    assert "[[" in system  # wikilink instructions


def test_old_profile_gets_default_models_and_privacy_defaults():
    p = ProfileConfig(name="x", s3_endpoint="http://e", access_key="a", secret_key="s",
                      bucket="b", prefix="x")
    assert p.prefix == "x/"
    assert p.compile_provider == LLMProvider.GROQ
    # cli_command / cli_fallback_* defaults are deliberately not pinned: every
    # existing profile sets them explicitly, and the open-source defaults changed.
    assert p.aux_model is None and p.aux_effort is None  # None = reuse the compile pair
    assert p.concept_threshold == 2
    # Existing profiles don't set these, so their defaults must never move.
    assert p.privacy_mode is False
    assert p.allow_jina_reader is True
    assert p.allow_cloud_vision is True
    assert p.output_language is None  # None = legacy Cantonese Traditional Chinese
