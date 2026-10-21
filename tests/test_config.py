"""Tests for configuration loading and multi-level resolution."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent2kb.config import (
    _load_env_overrides,
    _load_project_config,
    autodetect_profile,
    load_config,
    resolve_profile,
    save_config,
)
from agent2kb.models import KBConfig, LLMProvider, ProfileConfig


class TestConfigLoading:
    def test_load_empty_config(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump({"profiles": {}}, f)
            f.flush()
            config = load_config(Path(f.name))
            assert len(config.profiles) == 0

    def test_load_config_with_profile(self):
        data = {
            "profiles": {
                "personal": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "testkey",
                    "secret_key": "testsecret",
                    "bucket": "test-bucket",
                    "prefix": "personal/",
                    "compile_provider": "groq",
                    "groq_api_key": "gsk_test",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            config = load_config(Path(f.name))
            assert "personal" in config.profiles
            p = config.profiles["personal"]
            assert p.name == "personal"
            assert p.prefix == "personal/"
            assert p.compile_provider == LLMProvider.GROQ

    def test_load_missing_file(self):
        config = load_config(Path("/tmp/nonexistent_kb_config.yaml"))
        assert len(config.profiles) == 0

    def test_save_and_reload(self):
        profile = ProfileConfig(
            name="test",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="test/",
            compile_provider=LLMProvider.OLLAMA,
        )
        config = KBConfig(profiles={"test": profile})

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            path = Path(f.name)

        save_config(config, path)
        loaded = load_config(path)
        assert "test" in loaded.profiles
        assert loaded.profiles["test"].compile_provider == LLMProvider.OLLAMA
        path.unlink()


class TestProjectConfig:
    def test_load_flat_project_config(self):
        """Flat YAML (no profiles: key) is treated as single project config."""
        data = {
            "s3_endpoint": "http://localhost:8333",
            "access_key": "proj-key",
            "secret_key": "proj-secret",
            "bucket": "bucket",
            "prefix": "projects/myproj/",
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            result = _load_project_config(Path(f.name))
            assert "_project_flat" in result
            assert result["_project_flat"]["access_key"] == "proj-key"
            Path(f.name).unlink()

    def test_load_multiprofile_project_config(self):
        """Multi-profile format in project dir falls through normally."""
        data = {
            "profiles": {
                "myproj": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "key",
                    "secret_key": "secret",
                    "bucket": "bucket",
                    "prefix": "projects/myproj/",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            result = _load_project_config(Path(f.name))
            assert "_project_flat" not in result
            assert "myproj" in result["profiles"]
            Path(f.name).unlink()


class TestEnvOverrides:
    def test_env_vars_loaded(self):
        env = {"KB_S3_ENDPOINT": "http://env:8333", "KB_ACCESS_KEY": "envkey"}
        with patch.dict(os.environ, env):
            overrides = _load_env_overrides()
            assert overrides["s3_endpoint"] == "http://env:8333"
            assert overrides["access_key"] == "envkey"

    def test_no_env_vars(self):
        # Clear any KB_ env vars
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("KB_")}
        with patch.dict(os.environ, clean_env, clear=True):
            overrides = _load_env_overrides()
            assert len(overrides) == 0


class TestResolveProfile:
    def test_resolve_from_user_config(self):
        data = {
            "profiles": {
                "test": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "key",
                    "secret_key": "secret",
                    "bucket": "bucket",
                    "prefix": "test/",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            profile = resolve_profile("test", config_path=Path(f.name))
            assert profile.access_key == "key"
            assert profile.prefix == "test/"
            Path(f.name).unlink()

    def test_cli_overrides_user_config(self):
        data = {
            "profiles": {
                "test": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "original-key",
                    "secret_key": "secret",
                    "bucket": "bucket",
                    "prefix": "test/",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            profile = resolve_profile(
                "test",
                config_path=Path(f.name),
                cli_overrides={"access_key": "cli-key"},
            )
            # CLI override wins
            assert profile.access_key == "cli-key"
            # Other fields from config
            assert profile.bucket == "bucket"
            Path(f.name).unlink()

    def test_env_overrides_user_config(self):
        data = {
            "profiles": {
                "test": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "config-key",
                    "secret_key": "secret",
                    "bucket": "bucket",
                    "prefix": "test/",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            with patch.dict(os.environ, {"KB_ACCESS_KEY": "env-key"}):
                profile = resolve_profile("test", config_path=Path(f.name))
                # ENV override wins over config
                assert profile.access_key == "env-key"
            Path(f.name).unlink()

    def test_missing_profile_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_profile(
                "nonexistent",
                config_path=Path("/tmp/nonexistent_kb_12345.yaml"),
            )


class TestSuperProfileSecurity:
    """Super profile must only come from user-level config, never project/env/CLI."""

    def test_super_from_user_config_works(self):
        data = {
            "profiles": {
                "super": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "super-key",
                    "secret_key": "super-secret",
                    "bucket": "bucket",
                    "prefix": "",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            profile = resolve_profile("super", config_path=Path(f.name))
            assert profile.is_super
            assert profile.access_key == "super-key"
            Path(f.name).unlink()

    def test_super_blocked_without_user_config(self):
        """Cannot resolve super if user config doesn't exist."""
        with pytest.raises(PermissionError, match="user-level config"):
            resolve_profile(
                "super",
                config_path=Path("/tmp/nonexistent_kb_super_test.yaml"),
            )

    def test_super_blocked_if_not_in_user_config(self):
        """Cannot resolve super if user config exists but has no super profile."""
        data = {
            "profiles": {
                "personal": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "k",
                    "secret_key": "s",
                    "bucket": "b",
                    "prefix": "personal/",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            with pytest.raises(PermissionError, match="not found"):
                resolve_profile("super", config_path=Path(f.name))
            Path(f.name).unlink()


class TestAutodetectProfile:
    """`a2kb query "..."` (no -p) → profile must be resolvable.

    Pinned discovery order (0.11.0):
      1. $KB_PROFILE env var
      2. `name:` field in nearest .kb/config.yaml
    Super is never auto-resolved; the elevation must be typed by hand.
    """

    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KB_PROFILE", "alpha")
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() == "alpha"

    def test_falls_back_to_project_config_name_field(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KB_PROFILE", raising=False)
        cfg_dir = tmp_path / ".kb"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text(
            "name: beta\ns3_endpoint: x\nbucket: b\nprefix: p/\n"
        )
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() == "beta"

    def test_falls_back_to_single_profile_in_multiprofile_config(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("KB_PROFILE", raising=False)
        cfg_dir = tmp_path / ".kb"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text(
            "profiles:\n  only:\n    s3_endpoint: x\n    bucket: b\n    prefix: p/\n"
        )
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() == "only"

    def test_returns_none_when_nothing_to_find(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KB_PROFILE", raising=False)
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() is None

    def test_super_via_env_is_rejected(self, tmp_path, monkeypatch):
        """KB_PROFILE=super must NOT auto-elevate. Super requires --profile super."""
        monkeypatch.setenv("KB_PROFILE", "super")
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() is None

    def test_super_in_project_config_is_rejected(self, tmp_path, monkeypatch):
        """Project-config name: super must NOT auto-elevate either."""
        monkeypatch.delenv("KB_PROFILE", raising=False)
        cfg_dir = tmp_path / ".kb"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text(
            "name: super\ns3_endpoint: x\nbucket: b\nprefix: p/\n"
        )
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() is None

    def test_super_blocked_via_multiprofile_single_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KB_PROFILE", raising=False)
        cfg_dir = tmp_path / ".kb"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text(
            "profiles:\n  super:\n    s3_endpoint: x\n    bucket: b\n    prefix: ''\n"
        )
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() is None

    def test_malformed_project_config_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KB_PROFILE", raising=False)
        cfg_dir = tmp_path / ".kb"
        cfg_dir.mkdir()
        (cfg_dir / "config.yaml").write_text("{{{ not yaml")
        monkeypatch.chdir(tmp_path)
        assert autodetect_profile() is None
