"""Config v2: format, pointer files, admin role, endpoint trust, list/show/migrate."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agent2kb import config as kbconfig
from agent2kb import config_v2
from agent2kb.config import (
    ConfigError,
    autodetect_profile,
    load_config,
    resolve_profile,
    resolve_profile_detailed,
)
from agent2kb.language import DEFAULT_LANGUAGE, effective_language
from agent2kb.main import app
from agent2kb.models import ProfileConfig

runner = CliRunner()

STORAGE = {
    "endpoint": "http://seaweed.example:8333",
    "region": "auto",
    "bucket": "my-kb",
    "access_key": "SHAREDKEY",
    "secret_key": "SHAREDSECRET",
    "checksum_mode": "when_required",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake HOME with every KB_* variable cleared; cwd is HOME."""
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


def _v2(home, kbs=None, **blocks) -> Path:
    data = {"version": 2, "storage": STORAGE, "kbs": kbs if kbs is not None else {"research": {}}}
    data.update(blocks)
    path = home / ".kb" / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _pointer(directory: Path, data) -> Path:
    path = directory / ".kb" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


# ─── format ──────────────────────────────────────────────────────────


class TestFormat:
    def test_maps_blocks_onto_profile(self, home):
        path = _v2(
            home,
            llm={"provider": "cli", "command": "codex", "effort": "low", "aux_model": "m2"},
            defaults={"output_language": "en", "allow_jina_reader": False, "concept_threshold": 5},
            kbs={"research": {}},
        )
        p = resolve_profile("research", path)
        assert (p.s3_endpoint, p.bucket, p.region) == (STORAGE["endpoint"], "my-kb", "auto")
        assert (p.access_key, p.secret_key) == ("SHAREDKEY", "SHAREDSECRET")
        assert p.checksum_mode == "when_required"
        assert p.compile_provider.value == "cli" and p.cli_command == "codex"
        assert p.cli_reasoning_effort == "low" and p.aux_model == "m2"
        assert p.output_language == "en" and p.allow_jina_reader is False
        assert p.concept_threshold == 5
        assert p.role == "member" and not p.is_super

    def test_auto_prefix_only_for_v2_kbs(self, home):
        path = _v2(home, kbs={"research": {}, "acme": {"prefix": "clients/acme"}})
        assert resolve_profile("research", path).prefix == "kbs/research/"
        assert resolve_profile("acme", path).prefix == "clients/acme/"

    def test_per_kb_layers_win_over_shared(self, home):
        path = _v2(
            home,
            llm={"provider": "groq", "groq_api_key": "g1"},
            defaults={"output_language": "en"},
            kbs={
                "acme": {
                    "storage": {"access_key": "AK2", "secret_key": "SK2"},
                    "llm": {"provider": "ollama"},
                    "output_language": "zh-HK",
                }
            },
        )
        p = resolve_profile("acme", path)
        assert (p.access_key, p.secret_key) == ("AK2", "SK2")
        assert p.compile_provider.value == "ollama" and p.groq_api_key == "g1"
        assert p.output_language == "zh-HK"
        assert p.bucket == "my-kb"  # shared storage still applies

    def test_aws_chain_allowed_in_user_level_file(self, home):
        path = _v2(home, storage={"bucket": "my-kb", "region": "eu-west-1"})
        p = resolve_profile("research", path)
        assert p.access_key is None and p.s3_endpoint is None

    def test_detected_by_version_or_kbs(self, home):
        assert config_v2.is_v2({"version": 2})
        assert config_v2.is_v2({"kbs": {}})
        assert not config_v2.is_v2({"profiles": {}})
        assert config_v2.is_legacy_only({"profiles": {"a": {}}})

    @pytest.mark.parametrize(
        "kbs,match",
        [
            ({"super": {}}, "reserved"),
            ({"x": {"bucket": "b"}}, "storage setting"),
            ({"x": {"nonsense": 1}}, "not a known setting"),
            ({"x": {"role": "root"}}, "role"),
            ({"x": {"prefix": "/abs/"}}, "Invalid prefix"),
            ({"x": {"prefix": "a/../b/"}}, "Invalid prefix"),
            ({"x": {"storage": {"endpoint2": "u"}}}, "unknown storage key"),
        ],
    )
    def test_invalid_entries(self, home, kbs, match):
        path = _v2(home, kbs=kbs)
        with pytest.raises(ConfigError, match=match):
            name = next(iter(kbs))
            resolve_profile(name, path)

    def test_defaults_cannot_set_identity_or_storage(self, home):
        for bad in ({"role": "admin"}, {"prefix": "x/"}, {"bucket": "b"}):
            path = _v2(home, defaults=bad)
            with pytest.raises(ConfigError):
                resolve_profile("research", path)

    def test_unknown_top_level_key_and_future_version(self, home):
        path = _v2(home, surprise=1)
        with pytest.raises(ConfigError, match="unknown top-level"):
            resolve_profile("research", path)
        path.write_text(yaml.safe_dump({"version": 3, "kbs": {}}))
        with pytest.raises(ConfigError, match="version"):
            resolve_profile("research", path)

    def test_missing_bucket_message(self, home):
        path = _v2(home, storage={"region": "auto"})
        with pytest.raises(ConfigError, match="no bucket"):
            resolve_profile("research", path)

    def test_load_config_lists_v2_kbs_and_skips_broken(self, home):
        path = _v2(home, kbs={"good": {}, "bad": {"prefix": "../x/"}})
        cfg = load_config(path)
        assert set(cfg.profiles) == {"good"}

    def test_name_collision_between_kbs_and_profiles(self, home):
        path = _v2(home, kbs={"dup": {}}, profiles={"dup": {"bucket": "b", "prefix": "p/"}})
        with pytest.raises(ConfigError, match="both"):
            load_config(path)

    def test_write_v2_is_private_and_keeps_backup(self, home):
        path = home / "sub" / "c.yaml"
        config_v2.write_v2(path, {"kbs": {"a": {}}})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        config_v2.write_v2(path, {"kbs": {"a": {}, "b": {}}})
        backup = path.with_name("c.yaml.bak")
        assert yaml.safe_load(backup.read_text())["kbs"] == {"a": {}}
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_backup_and_legacy_save_are_never_world_readable(self, home, monkeypatch):
        """0600 from creation: no window between writing a secret and a later chmod."""
        from agent2kb.models import KBConfig

        path = home / "c.yaml"
        config_v2.write_v2(path, {"kbs": {"a": {}}})
        legacy = home / "legacy.yaml"
        old = os.umask(0)
        # With chmod disabled, only the mode the file was created with counts.
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        try:
            config_v2.write_v2(path, {"kbs": {"a": {}, "b": {}}})
            kbconfig.save_config(KBConfig(profiles={"alpha": ProfileConfig(
                name="alpha", bucket="b", prefix="alpha/", access_key="k", secret_key="s",
            )}), legacy)
        finally:
            os.umask(old)
        assert stat.S_IMODE(path.with_name("c.yaml.bak").stat().st_mode) == 0o600
        assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
        assert kbconfig.load_config(legacy).profiles["alpha"].secret_key == "s"


class TestUserFileIsNotAProjectConfig:
    """The upward walk reaches ~/.kb/config.yaml; a v2 file there must not act as a project."""

    def test_not_found_as_project_config(self, home):
        _v2(home, kbs={"research": {}, "work": {}})
        assert kbconfig._find_project_config() is None
        assert autodetect_profile() is None

    def test_no_project_layer_or_bogus_list_row(self, home):
        _v2(home)
        resolved = resolve_profile_detailed("research")
        assert resolved.project_path is None
        assert not any(src.startswith("project") for src in resolved.sources.values())
        rows = {
            r["name"]: r for r in json.loads(runner.invoke(app, ["list", "--json"]).output)["kbs"]
        }
        assert set(rows) == {"research"}

    def test_old_user_file_is_still_found_like_before(self, home):
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({"profiles": {"a": {}}}))
        assert kbconfig._find_project_config() == home / ".kb" / "config.yaml"


class TestUserConfigLocation:
    def test_kb_config_env(self, home, monkeypatch):
        other = home / "elsewhere.yaml"
        other.write_text(yaml.safe_dump({"version": 2, "storage": STORAGE, "kbs": {"e": {}}}))
        monkeypatch.setenv("KB_CONFIG", str(other))
        assert kbconfig.default_user_config_path() == other
        assert resolve_profile("e").prefix == "kbs/e/"

    def test_explicit_config_beats_env(self, home, monkeypatch):
        a = home / "a.yaml"
        b = home / "b.yaml"
        a.write_text(yaml.safe_dump({"version": 2, "storage": STORAGE, "kbs": {"x": {}}}))
        b.write_text(yaml.safe_dump({"version": 2, "storage": STORAGE, "kbs": {"y": {}}}))
        monkeypatch.setenv("KB_CONFIG", str(a))
        assert resolve_profile("y", b).name == "y"

    def test_default_is_home(self, home):
        assert kbconfig.default_user_config_path() == home / ".kb" / "config.yaml"


# ─── old formats untouched ───────────────────────────────────────────


class TestLegacyUnchanged:
    def test_old_profiles_keep_prefix_and_no_role(self, home):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "legacy": {
                            "bucket": "b",
                            "prefix": "projects/legacy",
                            "access_key": "a",
                            "secret_key": "s",
                        }
                    }
                }
            )
        )
        p = resolve_profile("legacy")
        assert p.prefix == "projects/legacy/" and p.role is None and not p.is_super
        assert p.output_language is None and p.allow_jina_reader is True

    def test_super_name_still_admin(self, home):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"super": {"bucket": "b", "prefix": ""}}})
        )
        assert resolve_profile("super").is_super

    def test_old_file_is_not_v2(self, home):
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({"profiles": {}}))
        assert config_v2.load_v2(home / ".kb" / "config.yaml") is None

    def test_role_field_default_none_not_written_by_save(self, home):
        p = ProfileConfig(name="x", bucket="b", prefix="x/")
        assert p.role is None
        assert "role" not in p.model_dump(exclude_none=True)


# ─── admin role ──────────────────────────────────────────────────────


class TestAdminRole:
    def _admin(self, home):
        return _v2(home, kbs={"boss": {"role": "admin"}, "research": {}})

    def test_role_admin_is_super(self, home):
        p = resolve_profile("boss", self._admin(home))
        assert p.is_super and p.prefix == "" and p.role == "admin"

    def test_admin_may_have_a_prefix(self, home):
        path = _v2(home, kbs={"boss": {"role": "admin", "prefix": "clients/acme/"}})
        p = resolve_profile("boss", path)
        assert p.is_super and p.prefix == "clients/acme/"

    def test_admin_ignores_project_env_and_flags(self, home, monkeypatch):
        path = self._admin(home)
        proj = home / "repo"
        _pointer(
            proj,
            {
                "name": "boss",
                "bucket": "evil",
                "prefix": "evil/",
                "access_key": "x",
                "secret_key": "y",
            },
        )
        monkeypatch.chdir(proj)
        monkeypatch.setenv("KB_BUCKET", "env-bucket")
        p = resolve_profile("boss", path, {"bucket": "flag-bucket", "prefix": "flag/"})
        assert p.bucket == "my-kb" and p.prefix == "" and p.access_key == "SHAREDKEY"

    def test_admin_is_never_autodetected(self, home, monkeypatch):
        path = self._admin(home)
        monkeypatch.setenv("KB_PROFILE", "boss")
        assert autodetect_profile(path) is None
        monkeypatch.delenv("KB_PROFILE")
        proj = home / "repo"
        _pointer(proj, {"kb": "boss"})
        monkeypatch.chdir(proj)
        assert autodetect_profile(path) is None
        _pointer(proj, {"name": "boss"})
        assert autodetect_profile(path) is None

    def test_admin_explicit_flag_still_works(self, home):
        self._admin(home)
        assert resolve_profile("boss").is_super

    def test_project_config_cannot_grant_role(self, home, monkeypatch):
        _v2(home)
        proj = home / "repo"
        _pointer(proj, {"name": "research", "role": "admin"})
        monkeypatch.chdir(proj)
        with pytest.raises(PermissionError, match="role|pointer"):
            resolve_profile("research")

    def test_project_multi_profile_cannot_grant_role(self, home, monkeypatch):
        proj = home / "repo"
        _pointer(
            proj,
            {
                "profiles": {
                    "p": {
                        "bucket": "b",
                        "prefix": "p/",
                        "role": "admin",
                        "access_key": "a",
                        "secret_key": "s",
                    }
                }
            },
        )
        monkeypatch.chdir(proj)
        with pytest.raises(PermissionError, match="role"):
            resolve_profile("p")

    def test_cli_compile_all_skips_admin_kbs(self, home):
        cfg = load_config(self._admin(home))
        assert cfg.profiles["boss"].is_super and not cfg.profiles["research"].is_super


# ─── the only KB is the default ──────────────────────────────────────


class TestSoleKbAutodetect:
    """`a2kb setup` creates exactly one KB; commands then work without -p."""

    def test_single_member_kb_is_selected(self, home):
        _v2(home)
        assert autodetect_profile() == "research"

    def test_explicit_config_path_and_kb_config_env(self, home, monkeypatch):
        other = home / "other.yaml"
        other.write_text(yaml.safe_dump({"version": 2, "storage": STORAGE, "kbs": {"solo": {}}}))
        assert autodetect_profile(other) == "solo"
        monkeypatch.setenv("KB_CONFIG", str(other))
        assert autodetect_profile() == "solo"

    def test_two_kbs_are_ambiguous(self, home):
        _v2(home, kbs={"research": {}, "work": {}})
        assert autodetect_profile() is None

    def test_admin_kb_is_never_the_default(self, home):
        _v2(home, kbs={"boss": {"role": "admin"}})
        assert autodetect_profile() is None

    def test_admin_alongside_member_is_ambiguous(self, home):
        _v2(home, kbs={"research": {}, "boss": {"role": "admin"}})
        assert autodetect_profile() is None

    def test_old_profiles_in_the_same_file_make_it_ambiguous(self, home):
        _v2(home, profiles={"legacy": {"bucket": "b", "prefix": "projects/legacy/"}})
        assert autodetect_profile() is None

    def test_old_style_user_config_behaves_as_before(self, home):
        # The old one-profile shortcut (found by the upward walk) is untouched.
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"only": {"bucket": "b", "prefix": "projects/only/"}}})
        )
        assert autodetect_profile() == "only"
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"a": {"bucket": "b"}, "b": {"bucket": "b"}}})
        )
        assert autodetect_profile() is None

    def test_legacy_project_config_is_left_alone(self, home, monkeypatch):
        _v2(home)
        proj = home / "old"
        _pointer(proj, {"s3_endpoint": "http://x:1", "access_key": "a", "secret_key": "b",
                        "bucket": "bkt", "prefix": "projects/x/"})
        monkeypatch.chdir(proj)
        assert autodetect_profile() is None

    def test_env_and_pointer_still_win(self, home, monkeypatch):
        _v2(home, kbs={"research": {}})
        monkeypatch.setenv("KB_PROFILE", "elsewhere")
        assert autodetect_profile() == "elsewhere"
        monkeypatch.delenv("KB_PROFILE")
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        assert autodetect_profile() == "research"

    def test_admin_env_does_not_fall_back_to_the_member_kb(self, home, monkeypatch):
        _v2(home, kbs={"research": {}, "boss": {"role": "admin"}})
        monkeypatch.setenv("KB_PROFILE", "boss")
        assert autodetect_profile() is None
        _v2(home)
        monkeypatch.setenv("KB_PROFILE", "super")
        assert autodetect_profile() is None

    def test_cli_runs_without_dash_p(self, home):
        _v2(home)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert "research" in result.output and "only KB in your config" in result.output


# ─── project pointer ─────────────────────────────────────────────────


class TestPointer:
    def test_pointer_selects_kb_from_subdir(self, home, monkeypatch):
        _v2(home)
        proj = home / "work" / "app"
        _pointer(proj, {"kb": "research"})
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert autodetect_profile() == "research"
        p = resolve_profile("research")
        assert p.prefix == "kbs/research/" and p.access_key == "SHAREDKEY"

    def test_env_and_flag_win_over_pointer(self, home, monkeypatch):
        _v2(home, kbs={"research": {}, "other": {}})
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        monkeypatch.setenv("KB_PROFILE", "other")
        assert autodetect_profile() == "other"
        monkeypatch.delenv("KB_PROFILE")
        # -p is just resolve_profile("other"): the pointer neither blocks nor alters it
        assert resolve_profile("other").prefix == "kbs/other/"

    def test_kb_profile_env_naming_an_admin_kb_does_not_rename_the_pointer_kb(
        self, home, monkeypatch
    ):
        _v2(home, kbs={"research": {}, "boss": {"role": "admin"}})
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        monkeypatch.setenv("KB_PROFILE", "boss")
        name = autodetect_profile()
        assert name == "research"  # admin env name ignored, pointer wins
        p = resolve_profile(name)
        assert p.name == "research" and not p.is_super

    def test_pointer_with_credentials_is_an_error(self, home, monkeypatch):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research", "access_key": "A", "secret_key": "B"})
        monkeypatch.chdir(proj)
        with pytest.raises(ConfigError, match="user-level"):
            autodetect_profile()
        with pytest.raises(ConfigError, match="credentials"):
            resolve_profile("research")

    @pytest.mark.parametrize("extra", ["s3_endpoint", "groq_api_key", "aws_profile"])
    def test_pointer_with_other_secretish_keys(self, home, monkeypatch, extra):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research", extra: "x"})
        monkeypatch.chdir(proj)
        with pytest.raises(ConfigError, match="credentials"):
            resolve_profile("research")

    def test_pointer_with_other_settings_is_an_error(self, home, monkeypatch):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research", "prefix": "evil/"})
        monkeypatch.chdir(proj)
        with pytest.raises(ConfigError, match="only `kb: <name>`"):
            resolve_profile("research")

    def test_pointer_to_unknown_kb_explains_itself(self, home, monkeypatch):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "nope"})
        monkeypatch.chdir(proj)
        with pytest.raises(ConfigError, match="a2kb new nope"):
            resolve_profile("nope")

    def test_pointer_cannot_redirect_storage(self, home, monkeypatch):
        """A pointer contributes nothing but a name, so nothing can leak keys."""
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        assert resolve_profile("research").s3_endpoint == STORAGE["endpoint"]

    def test_pointer_can_select_an_old_user_profile(self, home, monkeypatch):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "legacy": {
                            "bucket": "b",
                            "prefix": "projects/legacy/",
                            "access_key": "a",
                            "secret_key": "s",
                        }
                    }
                }
            )
        )
        proj = home / "app"
        _pointer(proj, {"kb": "legacy"})
        monkeypatch.chdir(proj)
        assert resolve_profile(autodetect_profile()).prefix == "projects/legacy/"

    def test_old_flat_file_still_autodetects(self, home, monkeypatch):
        proj = home / "app"
        _pointer(
            proj,
            {
                "name": "projA",
                "bucket": "b",
                "prefix": "projects/projA/",
                "access_key": "a",
                "secret_key": "s",
                "s3_endpoint": "http://h:1",
            },
        )
        monkeypatch.chdir(proj)
        assert autodetect_profile() == "projA"
        assert resolve_profile("projA").prefix == "projects/projA/"

    def test_cli_error_for_pointer_with_credentials(self, home, monkeypatch):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research", "secret_key": "B"})
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["status"])
        assert result.exit_code != 0
        assert "user-level" in result.output.replace("\n", " ")

    def test_cli_autodetects_pointer(self, home, monkeypatch):
        _v2(home)
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert "research" in result.output and "kbs/research/" in result.output


# ─── project config may not redirect user-level keys ─────────────────


KEYS = {"access_key": "USERKEY", "secret_key": "USERSECRET"}


class TestStorageTargetTrust:
    """A project layer may retarget storage only with keys of its own."""

    def _user(self, home, legacy=False, **profile):
        if legacy:
            data = {"profiles": {"app": {"bucket": "b", "prefix": "p/", **KEYS, **profile}}}
        else:
            data = {"version": 2, "storage": {"bucket": "b", **KEYS}, "kbs": {"app": profile}}
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump(data))

    def _project(self, home, monkeypatch, data):
        proj = home / "repo"
        _pointer(proj, data)
        monkeypatch.chdir(proj)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("s3_endpoint", "http://attacker.example"),
            ("region", "evil-1"),
            ("bucket", "attacker-public-bucket"),
            ("prefix", "projects/someone-else/"),
            ("addressing_style", "path"),
        ],
    )
    def test_target_field_with_inherited_keys_rejected(self, home, monkeypatch, field, value):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", field: value})
        with pytest.raises(PermissionError, match="without its own access_key"):
            resolve_profile("app")

    def test_bucket_exfil_with_aws_style_endpoint_none(self, home, monkeypatch):
        """Review finding 1: AWS (no endpoint) + user keys + project `bucket:` -> exfiltration."""
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "app": {
                            "bucket": "mine",
                            "prefix": "projects/app/",
                            "region": "eu-west-1",
                            **KEYS,
                        }
                    }
                }
            )
        )
        self._project(home, monkeypatch, {"name": "app", "bucket": "attacker-public-bucket"})
        with pytest.raises(PermissionError, match="bucket"):
            resolve_profile("app")

    def test_prefix_override_to_another_kbs_prefix(self, home, monkeypatch):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "app": {"bucket": "b", "prefix": "projects/app/", **KEYS},
                        "other": {"bucket": "b", "prefix": "projects/other/", **KEYS},
                    }
                }
            )
        )
        self._project(home, monkeypatch, {"name": "app", "prefix": "projects/other/"})
        with pytest.raises(PermissionError, match="prefix"):
            resolve_profile("app")

    def test_chain_credentials_cannot_be_retargeted_either(self, home, monkeypatch):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"app": {"bucket": "b", "prefix": "p/"}}})
        )
        self._project(home, monkeypatch, {"name": "app", "bucket": "attacker-bucket"})
        with pytest.raises(PermissionError):
            resolve_profile("app")

    def test_project_with_own_keys_may_retarget(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(
            home,
            monkeypatch,
            {
                "name": "app",
                "bucket": "proj-bucket",
                "prefix": "projects/app2/",
                "s3_endpoint": "http://seaweed:8333",
                "access_key": "OWN",
                "secret_key": "OWNS",
            },
        )
        p = resolve_profile("app")
        assert (p.bucket, p.prefix, p.access_key) == ("proj-bucket", "projects/app2/", "OWN")

    def test_only_one_own_key_is_not_enough(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "bucket": "x", "access_key": "OWN"})
        with pytest.raises((PermissionError, ValueError)):
            resolve_profile("app")

    def test_env_keys_count_as_explicit(self, home, monkeypatch):
        """Docker / CI: keys come from env, the project file names the target."""
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "bucket": "proj-bucket"})
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        p = resolve_profile("app")
        assert p.bucket == "proj-bucket" and p.access_key == "ENVKEY"

    @pytest.mark.parametrize(
        "field,value", [("s3_endpoint", "http://attacker"), ("region", "evil-1")]
    )
    def test_env_keys_are_never_sent_to_a_project_chosen_host(
        self, home, monkeypatch, field, value
    ):
        """HIGH regression: CI's KB_ACCESS_KEY/KB_SECRET_KEY + a cloned repo's endpoint."""
        proj = home / "repo"
        _pointer(proj, {"name": "app", "bucket": "b", "prefix": "p/", field: value})
        monkeypatch.chdir(proj)
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        with pytest.raises(PermissionError, match="host it names"):
            resolve_profile("app")

    def test_cli_keys_are_never_sent_to_a_project_chosen_host(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "s3_endpoint": "http://attacker"})
        with pytest.raises(PermissionError, match="host it names"):
            resolve_profile("app", cli_overrides={"access_key": "FK", "secret_key": "FS"})

    def test_env_keys_with_env_endpoint_and_project_bucket_allowed(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "bucket": "proj-bucket"})
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        monkeypatch.setenv("KB_S3_ENDPOINT", "http://mine:1")
        p = resolve_profile("app")
        assert (p.bucket, p.s3_endpoint, p.access_key) == ("proj-bucket", "http://mine:1", "ENVKEY")

    def test_env_keys_with_env_endpoint_override_project_endpoint(self, home, monkeypatch):
        self._project(
            home,
            monkeypatch,
            {"name": "app", "bucket": "b", "prefix": "p/", "s3_endpoint": "http://attacker:1"},
        )
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        monkeypatch.setenv("KB_S3_ENDPOINT", "http://mine:1")
        assert resolve_profile("app").s3_endpoint == "http://mine:1"

    def test_cli_keys_count_as_explicit(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "prefix": "projects/x/"})
        p = resolve_profile("app", cli_overrides={"access_key": "FK", "secret_key": "FS"})
        assert p.prefix == "projects/x/" and p.access_key == "FK"

    def test_one_env_key_is_not_enough(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "bucket": "proj-bucket"})
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        with pytest.raises((PermissionError, ValueError)):
            resolve_profile("app")

    def test_env_keys_overriding_project_keys_with_project_host_rejected(self, home, monkeypatch):
        self._project(
            home,
            monkeypatch,
            {
                "name": "app",
                "bucket": "b",
                "prefix": "p/",
                "s3_endpoint": "http://x:1",
                "access_key": "OWN",
                "secret_key": "OWNS",
            },
        )
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        with pytest.raises(PermissionError, match="would be sent to the host"):
            resolve_profile("app")

    def test_env_endpoint_overrides_project_endpoint(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "s3_endpoint": "http://attacker:1"})
        monkeypatch.setenv("KB_S3_ENDPOINT", "http://mine:1")
        assert resolve_profile("app").s3_endpoint == "http://mine:1"

    def test_flat_project_with_own_keys_and_endpoint_works(self, home, monkeypatch):
        """Every live KB: a self-contained flat project config, no user-level config."""
        self._project(
            home,
            monkeypatch,
            {
                "name": "projA",
                "s3_endpoint": "http://seaweed:8333",
                "access_key": "k",
                "secret_key": "s",
                "bucket": "kb-bucket",
                "prefix": "projects/projA/",
            },
        )
        p = resolve_profile("projA")
        assert p.s3_endpoint == "http://seaweed:8333" and p.access_key == "k"

    def test_project_without_target_fields_may_use_user_keys(self, home, monkeypatch):
        self._user(home, legacy=True)
        self._project(home, monkeypatch, {"name": "app", "output_language": "en"})
        assert resolve_profile("app").access_key == "USERKEY"

    # v2 KBs: only a pointer may live in the project folder

    @pytest.mark.parametrize(
        "data",
        [
            {"name": "app", "output_language": "en"},
            {"name": "app", "s3_endpoint": "http://attacker"},
            {"profiles": {"app": {"bucket": "b", "prefix": "p/", **KEYS}}},
        ],
    )
    def test_v2_kb_rejects_flat_or_multi_profile_project_config(self, home, monkeypatch, data):
        self._user(home)
        self._project(home, monkeypatch, data)
        with pytest.raises(PermissionError, match="pointer"):
            resolve_profile("app")

    def test_v2_kb_with_pointer_is_fine(self, home, monkeypatch):
        self._user(home)
        self._project(home, monkeypatch, {"kb": "app"})
        assert resolve_profile("app").access_key == "USERKEY"

    def test_v2_kb_ignores_unrelated_multi_profile_project_config(self, home, monkeypatch):
        self._user(home)
        self._project(home, monkeypatch, {"profiles": {"zzz": {"bucket": "b", "prefix": "z/"}}})
        assert resolve_profile("app").prefix == "kbs/app/"


class TestEmptyPrefix:
    @pytest.mark.parametrize("prefix", ["", "/", "//"])
    def test_project_layer_cannot_blank_the_prefix(self, home, monkeypatch, prefix):
        proj = home / "repo"
        _pointer(
            proj,
            {"name": "p", "bucket": "b", "prefix": prefix, "access_key": "a", "secret_key": "s"},
        )
        monkeypatch.chdir(proj)
        with pytest.raises(ConfigError, match="empty prefix"):
            resolve_profile("p")

    @pytest.mark.parametrize("prefix", ["", "/"])
    def test_env_and_flag_cannot_blank_it_either(self, home, monkeypatch, prefix):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"p": {"bucket": "b", "prefix": "p/", **KEYS}}})
        )
        with pytest.raises(ConfigError, match="empty prefix"):
            resolve_profile("p", cli_overrides={"prefix": prefix})
        monkeypatch.setenv("KB_PREFIX", prefix)
        with pytest.raises(ConfigError, match="empty prefix"):
            resolve_profile("p")

    def test_user_level_legacy_profile_with_empty_prefix_rejected(self, home):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"p": {"bucket": "b", "prefix": "", **KEYS}}})
        )
        with pytest.raises(ConfigError, match="empty prefix"):
            resolve_profile("p")

    def test_v2_member_cannot_have_empty_prefix(self, home):
        path = _v2(home, kbs={"x": {"prefix": ""}})
        with pytest.raises(ConfigError):
            resolve_profile("x", path)

    def test_admins_may_span_the_bucket(self, home):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"super": {"bucket": "b", "prefix": ""}}})
        )
        assert resolve_profile("super").prefix == ""
        path = _v2(home, kbs={"boss": {"role": "admin"}})
        assert resolve_profile("boss", path).prefix == ""


# ─── list / show / migrate ───────────────────────────────────────────


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestSourceLabel:
    def test_all_none_cli_overrides_are_not_a_cli_layer(self, home):
        _v2(home)
        overrides = {"s3_endpoint": None, "access_key": None, "bucket": None}
        resolved = resolve_profile_detailed("research", cli_overrides=overrides)
        assert not any(src == "cli flag" for src in resolved.sources.values())
        assert not resolved.layers[0].startswith("cli")
        with_flag = resolve_profile_detailed("research", cli_overrides={"bucket": "x-bucket"})
        assert with_flag.layers[0].startswith("cli + ")


class TestListAndShow:
    def test_list_shows_v2_old_and_pointer(self, home, monkeypatch):
        path = _v2(
            home,
            kbs={"research": {}, "boss": {"role": "admin"}},
            profiles={
                "legacy": {"bucket": "b", "prefix": "projects/legacy/"},
                "super": {"bucket": "b", "prefix": ""},
            },
        )
        proj = home / "app"
        _pointer(proj, {"kb": "research"})
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0, result.output
        rows = {r["name"]: r for r in json.loads(result.output)["kbs"]}
        assert rows["research"]["prefix"] == "kbs/research/"
        assert rows["research"]["role"] == "member"
        assert "selected by" in rows["research"]["note"]
        assert rows["boss"]["role"] == "admin" and rows["boss"]["prefix"] == "(whole bucket)"
        assert rows["legacy"]["where"] == "old profile" and rows["super"]["role"] == "admin"
        assert "SHAREDKEY" not in result.output
        assert path.is_file()

    def test_list_without_any_config(self, home):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0 and "a2kb setup" in result.output

    def test_list_includes_old_project_config(self, home, monkeypatch):
        proj = home / "app"
        _pointer(
            proj,
            {
                "name": "projA",
                "prefix": "projects/projA/",
                "bucket": "b",
                "access_key": "SECRETKEY1",
                "secret_key": "SECRETSECRET1",
            },
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["list", "--json"])
        assert "projA" in result.output and "SECRETKEY1" not in result.output

    def test_show_masks_secrets_and_reports_sources(self, home, monkeypatch):
        _v2(
            home,
            llm={"provider": "groq", "groq_api_key": "gsk_SECRET"},
            defaults={"output_language": "en"},
        )
        monkeypatch.setenv("KB_BUCKET", "env-bucket")
        result = runner.invoke(app, ["-p", "research", "config", "show", "--json"])
        assert result.exit_code == 0, result.output
        assert "SHAREDSECRET" not in result.output and "gsk_SECRET" not in result.output
        settings = json.loads(result.output)["settings"]
        assert settings["secret_key"]["value"] == "***"
        assert settings["bucket"]["value"] == "env-bucket" and settings["bucket"]["source"] == "env"
        assert settings["prefix"]["source"].startswith("auto")
        assert settings["output_language"]["source"].startswith("defaults")
        assert settings["groq_api_key"]["source"].startswith("llm")

    def test_show_old_flat_project(self, home, monkeypatch):
        proj = home / "app"
        _pointer(
            proj,
            {
                "name": "projA",
                "prefix": "projects/projA/",
                "bucket": "b",
                "access_key": "FLATKEY",
                "secret_key": "FLATSECRET",
                "s3_endpoint": "http://h:1",
            },
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert "project" in result.output and "projA" in result.output
        assert "***" in result.output and "FLATSECRET" not in result.output
        assert "en (default)" in result.output  # unset language means English


def _old_flat(home, monkeypatch, **extra):
    proj = home / "legacy"
    data = {
        "name": "legacy",
        "s3_endpoint": "http://seaweed:8333",
        "access_key": "OLDKEY",
        "secret_key": "OLDSECRET",
        "bucket": "kb-bucket",
        "prefix": "projects/legacy/",
        "groq_api_key": "gsk_OLD",
        **extra,
    }
    path = _pointer(proj, data)
    monkeypatch.chdir(proj)
    return path


class TestMigrate:
    def test_dry_run_prints_masked_and_writes_nothing(self, home, monkeypatch):
        old = _old_flat(home, monkeypatch)
        before = _digest(old)
        result = runner.invoke(app, ["config", "migrate", "--profile", "legacy"])
        assert result.exit_code == 0, result.output
        assert "OLDSECRET" not in result.output and "gsk_OLD" not in result.output
        assert "projects/legacy/" in result.output and "dry-run" in result.output
        assert not (home / ".kb" / "config.yaml").exists()
        assert _digest(old) == before

    def test_write_creates_v2_and_keeps_old_file_and_prefix(self, home, monkeypatch):
        old = _old_flat(home, monkeypatch)
        before = _digest(old)
        result = runner.invoke(app, ["config", "migrate", "-p", "legacy", "--write", "--yes"])
        assert result.exit_code == 0, result.output
        target = home / ".kb" / "config.yaml"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert _digest(old) == before
        monkeypatch.chdir(home)
        migrated = resolve_profile("legacy", target)
        assert migrated.prefix == "projects/legacy/"
        assert (migrated.s3_endpoint, migrated.bucket) == ("http://seaweed:8333", "kb-bucket")
        assert (migrated.access_key, migrated.groq_api_key) == ("OLDKEY", "gsk_OLD")

    def test_old_folder_refuses_the_migrated_v2_kb_until_switched_to_a_pointer(
        self, home, monkeypatch
    ):
        old = _old_flat(home, monkeypatch)
        runner.invoke(app, ["config", "migrate", "-p", "legacy", "--write", "--yes"])
        with pytest.raises(PermissionError, match="pointer"):
            resolve_profile("legacy")  # cwd is still the old project folder
        old.write_text("kb: legacy\n")
        assert resolve_profile("legacy").prefix == "projects/legacy/"

    def test_migrated_kb_keeps_old_behaviour_despite_defaults(self, home, monkeypatch):
        """File-level defaults (new-KB privacy/language) must not change a migrated KB."""
        _old_flat(home, monkeypatch)
        target = _v2(
            home,
            kbs={},
            defaults={
                "output_language": "ja",  # differs from DEFAULT_LANGUAGE, so a leak shows
                "allow_jina_reader": False,
                "allow_cloud_vision": False,
            },
            llm={"provider": "cli", "command": "claude"},
        )
        monkeypatch.chdir(home / "legacy")
        old = resolve_profile("legacy")
        result = runner.invoke(app, ["config", "migrate", "-p", "legacy", "--write", "--yes"])
        assert result.exit_code == 0, result.output
        monkeypatch.chdir(home)  # inside the old folder a v2 KB needs a pointer (see below)
        new = resolve_profile("legacy", target)
        for f in (
            "allow_jina_reader",
            "allow_cloud_vision",
            "compile_provider",
            "cli_command",
            "default_model",
            "prefix",
            "access_key",
            "secret_key",
            "s3_endpoint",
            "bucket",
        ):
            assert getattr(new, f) == getattr(old, f), f
        # migrate pins what unset means (DEFAULT_LANGUAGE), so a later change to a v2
        # file default cannot change the migrated KB's prompts or OpenCC handling
        assert old.output_language is None and new.output_language == DEFAULT_LANGUAGE
        assert effective_language(new.output_language) == effective_language(None)

    def test_second_kb_with_other_keys_gets_own_storage(self, home, monkeypatch):
        target = _v2(home, kbs={})
        proj = home / "other"
        _pointer(
            proj,
            {
                "name": "other",
                "s3_endpoint": STORAGE["endpoint"],
                "bucket": "my-kb",
                "prefix": "projects/other/",
                "access_key": "OTHERKEY",
                "secret_key": "OTHERSECRET",
            },
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["config", "migrate", "-p", "other", "--write", "--yes"])
        assert result.exit_code == 0, result.output
        raw = yaml.safe_load(target.read_text())
        own = raw["kbs"]["other"]["storage"]
        assert (own["access_key"], own["secret_key"]) == ("OTHERKEY", "OTHERSECRET")
        # the old profile had no region/checksum_mode: pinned as null so the shared ones
        # in `storage:` do not change how this KB talks to the server
        assert own["region"] is None and own["checksum_mode"] is None
        monkeypatch.chdir(home)
        new = resolve_profile("other", target)
        assert (new.access_key, new.region, new.checksum_mode) == ("OTHERKEY", None, None)
        assert raw["storage"] == STORAGE  # the shared block was not rewritten

    def test_refuses_old_format_target(self, home, monkeypatch):
        _old_flat(home, monkeypatch)
        legacy = home / ".kb" / "config.yaml"
        legacy.write_text(yaml.safe_dump({"profiles": {"x": {"bucket": "b", "prefix": "x/"}}}))
        before = _digest(legacy)
        result = runner.invoke(app, ["config", "migrate", "-p", "legacy", "--write", "--yes"])
        assert result.exit_code != 0 and "old-format" in result.output.replace("\n", " ")
        assert _digest(legacy) == before

    def test_super_needs_as_and_is_confirmed(self, home, monkeypatch):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "profiles": {
                        "super": {"bucket": "b", "prefix": "", "access_key": "a", "secret_key": "s"}
                    }
                }
            )
        )
        result = runner.invoke(app, ["config", "migrate", "-p", "super"])
        assert result.exit_code != 0 and "--as" in result.output
        target = home / "v2.yaml"
        # not a terminal: --yes skips the first question but the admin one still refuses
        result = runner.invoke(
            app,
            [
                "config",
                "migrate",
                "-p",
                "super",
                "--as",
                "root",
                "--to",
                str(target),
                "--write",
                "--yes",
            ],
        )
        assert result.exit_code == 1 and not target.exists()
        result = runner.invoke(
            app, ["config", "migrate", "-p", "super", "--as", "root", "--to", str(target)]
        )
        assert result.exit_code == 0 and "ADMIN" not in result.output  # dry-run: no warning yet
        assert "role: admin" in result.output

    def test_name_clash_and_bad_name(self, home, monkeypatch):
        _old_flat(home, monkeypatch)
        _v2(home, kbs={"legacy": {}})
        monkeypatch.chdir(home / "legacy")
        result = runner.invoke(app, ["config", "migrate", "-p", "legacy"])
        assert result.exit_code != 0
        result = runner.invoke(app, ["config", "migrate", "-p", "legacy", "--as", "Bad_Name"])
        assert result.exit_code != 0

    def test_already_v2_is_refused(self, home):
        _v2(home)
        result = runner.invoke(app, ["config", "migrate", "-p", "research"])
        assert result.exit_code != 0 and "already a v2 KB" in result.output.replace("\n", " ")


class TestFriendlyErrors:
    """New commands share the profile callback's error path: Error/Hint, exit 2, JSON."""

    def test_broken_v2_file_in_list(self, home):
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({"version": 2, "bogus": 1}))
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 2 and "Error:" in result.output
        assert "Traceback" not in result.output

    def test_json_error_is_valid_json_on_stdout(self, home):
        (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({"version": 2, "bogus": 1}))
        result = CliRunner().invoke(app, ["list", "--json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False and "bogus" in payload["error"]

    def test_config_show_unknown_profile(self, home):
        _v2(home)
        result = runner.invoke(app, ["-p", "nope", "config", "show"])
        assert result.exit_code == 2 and "Error:" in result.output

    def test_config_show_trust_violation_is_friendly(self, home, monkeypatch):
        (home / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"p": {"bucket": "b", "prefix": "p/", **KEYS}}})
        )
        _pointer(home / "repo", {"name": "p", "s3_endpoint": "http://attacker"})
        monkeypatch.chdir(home / "repo")
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 2 and "without its own" in " ".join(result.output.split())

    def test_markup_in_names_is_not_interpreted(self, home):
        path = _v2(home, kbs={"x": {}})
        result = runner.invoke(app, ["-p", "[red]boom[/red]", "config", "show"])
        assert result.exit_code == 2 and "[red]boom[/red]" in result.output
        assert path.is_file()


class TestRefusalMessages:
    """Trust refusals: one Error line naming field + file, one specific Hint line."""

    def _run(self, home, monkeypatch, project, user_profiles=None):
        if user_profiles is not None:
            (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({"profiles": user_profiles}))
        _pointer(home / "repo", project)
        monkeypatch.chdir(home / "repo")
        return CliRunner().invoke(app, ["-p", "app", "config", "show"])

    def test_endpoint_refusal_is_one_error_and_one_hint(self, home, monkeypatch):
        result = self._run(
            home,
            monkeypatch,
            {"name": "app", "s3_endpoint": "http://attacker"},
            {"app": {"bucket": "b", "prefix": "p/", **KEYS}},
        )
        assert result.exit_code == 2
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        assert sum(ln.startswith("Error:") for ln in lines) == 1
        assert sum(ln.startswith("Hint:") for ln in lines) == 1
        compact = "".join(result.stderr.split())  # long paths wrap mid-word at 80 columns
        assert "s3_endpoint" in compact
        assert "ownaccess_keyandsecret_key" in compact and "removes3_endpoint" in compact
        assert "(run with -v for the full traceback)" in result.stderr

    def test_empty_prefix_refusal(self, home, monkeypatch):
        result = self._run(home, monkeypatch, {"name": "app", "bucket": "b", "prefix": "", **KEYS})
        assert result.exit_code == 2
        assert "empty prefix" in " ".join(result.stderr.split())
        assert "Hint:" in result.stderr

    def test_pointer_with_credentials_refusal(self, home, monkeypatch):
        _v2(home)
        result = self._run(home, monkeypatch, {"kb": "research", "secret_key": "x"})
        assert result.exit_code == 2
        flat = " ".join(result.stderr.split())
        assert "Error:" in flat and "Hint:" in flat and "credentials" in flat


class TestMigrationOutput:
    def test_explicit_null_is_commented_and_still_valid_yaml(self, home, monkeypatch):
        target = _v2(home, kbs={})
        proj = home / "other"
        _pointer(
            proj,
            {
                "name": "other",
                "s3_endpoint": STORAGE["endpoint"],
                "bucket": "my-kb",
                "prefix": "projects/other/",
                "access_key": "OTHERKEY",
                "secret_key": "OTHERSECRET",
            },
        )
        monkeypatch.chdir(proj)
        runner.invoke(app, ["config", "migrate", "-p", "other", "--write", "--yes"])
        text = target.read_text()
        assert "region: null  # unset for this KB" in text
        assert yaml.safe_load(text)["kbs"]["other"]["storage"]["region"] is None

    def test_values_equal_to_inherited_defaults_are_omitted(self, home, monkeypatch):
        target = _v2(
            home,
            kbs={},
            defaults={"output_language": "en", "allow_jina_reader": False},
            llm={"provider": "groq"},
        )
        proj = home / "other"
        _pointer(
            proj,
            {
                "name": "other",
                "s3_endpoint": STORAGE["endpoint"],
                "bucket": "my-kb",
                "prefix": "projects/other/",
                "access_key": "SHAREDKEY",
                "secret_key": "SHAREDSECRET",
                "region": "auto",
                "checksum_mode": "when_required",
                "output_language": "en",
                "allow_jina_reader": False,
                "compile_provider": "groq",
            },
        )
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["config", "migrate", "-p", "other", "--write", "--yes"])
        assert result.exit_code == 0, result.output
        entry = yaml.safe_load(target.read_text())["kbs"]["other"]
        assert entry == {"prefix": "projects/other/"}, entry
        monkeypatch.chdir(home)
        assert resolve_profile("other", target).output_language == "en"


class TestBannerShowsTarget:
    def test_banner_shows_bucket_host_and_project_config(self, home, monkeypatch):
        from agent2kb import main as kb_main

        proj = home / "repo"
        cfg = _pointer(
            proj,
            {
                "name": "p",
                "s3_endpoint": "http://seaweed.example:8333",
                "bucket": "the-bucket",
                "prefix": "projects/p/",
                **KEYS,
            },
        )
        monkeypatch.chdir(proj)
        profile = resolve_profile("p")
        lines: list[str] = []
        monkeypatch.setattr(kb_main.console, "print", lambda *a, **k: lines.append(str(a[0])))
        kb_main._print_privacy_banner(profile, "Compiling")
        banner = lines[0]
        assert "the-bucket" in banner and "seaweed.example:8333" in banner
        assert "projects/p/" in banner and str(cfg) in banner

    def test_banner_without_project_layer_has_no_config_note(self, home, monkeypatch):
        from agent2kb import main as kb_main

        path = _v2(home)
        profile = resolve_profile("research", path)
        lines: list[str] = []
        monkeypatch.setattr(kb_main.console, "print", lambda *a, **k: lines.append(str(a[0])))
        kb_main._print_privacy_banner(profile, "Compiling")
        assert "config:" not in lines[0] and "my-kb" in lines[0]
