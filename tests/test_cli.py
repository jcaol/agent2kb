"""Tests for CLI commands (dry run / smoke tests)."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from agent2kb.main import app

runner = CliRunner()


def _make_config(tmpdir: str) -> Path:
    """Create a test config file."""
    config_path = Path(tmpdir) / "config.yaml"
    config = {
        "profiles": {
            "test": {
                "s3_endpoint": "http://localhost:8333",
                "access_key": "testkey",
                "secret_key": "testsecret",
                "bucket": "test-bucket",
                "prefix": "test/",
                "compile_provider": "ollama",
                "ollama_host": "http://localhost:11434",
            },
            "super": {
                "s3_endpoint": "http://localhost:8333",
                "access_key": "superkey",
                "secret_key": "supersecret",
                "bucket": "test-bucket",
                "prefix": "",
                "compile_provider": "groq",
                "groq_api_key": "gsk_test",
            },
        }
    }
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


class TestCLIHelp:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "a headless knowledge compiler for agent teams" in result.output

    def test_ingest_help(self):
        # Need to provide --profile for callback, but help should still work
        result = runner.invoke(app, ["--help"])
        assert "ingest" in result.output
        assert "compile" in result.output
        assert "query" in result.output
        assert "lint" in result.output
        assert "status" in result.output


class TestCLIIngest:
    def test_ingest_no_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _make_config(tmpdir)
            result = runner.invoke(
                app,
                [
                    "--profile",
                    "test",
                    "--config",
                    str(config_path),
                    "ingest",
                ],
            )
            assert "Provide --url, --file, or --folder" in result.output

    def test_ingest_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _make_config(tmpdir)
            result = runner.invoke(
                app,
                [
                    "--profile",
                    "test",
                    "--config",
                    str(config_path),
                    "ingest",
                    "--file",
                    "/tmp/nonexistent_file_12345.md",
                ],
            )
            assert "not found" in result.output.lower() or result.exit_code != 0


class TestCLIProfileValidation:
    def test_missing_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _make_config(tmpdir)
            result = runner.invoke(
                app,
                [
                    "--profile",
                    "nonexistent",
                    "--config",
                    str(config_path),
                    "status",
                ],
            )
            # Should fail because profile doesn't exist
            assert result.exit_code != 0 or "not found" in result.output.lower()


class TestCLIQuerySaveDefault:
    """Regression tests for the a2kb query compounding loop default.

    As of 0.6.1, a2kb query saves to outputs/ by default (Karpathy
    compounding loop), with --no-save as the explicit opt-out. Before
    0.6.1 the default was off and required -s/--save.
    """

    def test_query_default_calls_save_output(self):
        """a2kb query (user actor) without --no-save should call save_output().

        Under pytest stdin is not a TTY → the new actor autodetect (agent2kb
        0.12.0) would default to ``agent`` (save=off). To pin the
        *human-shell* contract — "type a2kb query and it lands in
        Obsidian" — the test explicitly passes ``--actor user``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _make_config(tmpdir)

            # Patch QueryEngine so we don't actually hit S3 or an LLM
            with patch("agent2kb.query.QueryEngine") as MockEngine:
                instance = MockEngine.return_value
                instance.query.return_value = "mock answer with [[citation]]"
                instance.save_output.return_value = "test/outputs/20260411-120000-q.md"

                result = runner.invoke(
                    app,
                    [
                        "--profile",
                        "test",
                        "--config",
                        str(config_path),
                        "query",
                        "what is the meaning of life",
                        "--actor",
                        "user",
                    ],
                )

                assert instance.query.called
                assert instance.save_output.called, (
                    "a2kb query --actor user default should call save_output()"
                )
                assert "Saved to outputs/" in result.output

    def test_query_no_save_skips_save_output(self):
        """a2kb query --no-save should NOT call QueryEngine.save_output()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _make_config(tmpdir)

            with patch("agent2kb.query.QueryEngine") as MockEngine:
                instance = MockEngine.return_value
                instance.query.return_value = "mock answer"

                result = runner.invoke(
                    app,
                    [
                        "--profile",
                        "test",
                        "--config",
                        str(config_path),
                        "query",
                        "throwaway exploratory question",
                        "--no-save",
                    ],
                )

                assert instance.query.called
                assert not instance.save_output.called, (
                    "--no-save must bypass save_output()"
                )
                assert "Saved to outputs/" not in result.output


class TestActorAutodetect:
    """``a2kb query`` (no --actor flag): how do we decide who's asking?

    Pinned resolution order (0.12.0):
      1. ``--actor X`` flag.
      2. ``KB_ACTOR`` env var.
      3. ``sys.stdin.isatty()`` — TTY → user, else agent.

    Matters because chat-bot / cron callers without an explicit flag
    must default to ``agent`` (no-save, ``outputs/agent/`` folder) to
    keep ``outputs/`` curated.
    """

    def test_explicit_flag_beats_everything(self, monkeypatch):
        monkeypatch.setenv("KB_ACTOR", "agent")
        from agent2kb.main import _resolve_actor

        assert _resolve_actor("user") == "user"
        assert _resolve_actor("agent") == "agent"

    def test_env_wins_when_no_flag(self, monkeypatch):
        from agent2kb.main import _resolve_actor

        monkeypatch.setenv("KB_ACTOR", "agent")
        assert _resolve_actor(None) == "agent"
        monkeypatch.setenv("KB_ACTOR", "user")
        assert _resolve_actor(None) == "user"

    def test_tty_falls_through_to_user(self, monkeypatch):
        from agent2kb.main import _resolve_actor

        monkeypatch.delenv("KB_ACTOR", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        assert _resolve_actor(None) == "user"

    def test_non_tty_falls_through_to_agent(self, monkeypatch):
        """pytest's own stdin is non-TTY — the chat-bot case."""
        from agent2kb.main import _resolve_actor

        monkeypatch.delenv("KB_ACTOR", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert _resolve_actor(None) == "agent"

    def test_garbage_env_falls_through(self, monkeypatch):
        from agent2kb.main import _resolve_actor

        monkeypatch.setenv("KB_ACTOR", "banana")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        assert _resolve_actor(None) == "user"  # TTY fallback

    def test_garbage_env_falls_through_to_agent_when_non_tty(self, monkeypatch):
        from agent2kb.main import _resolve_actor

        monkeypatch.setenv("KB_ACTOR", "garbage")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert _resolve_actor(None) == "agent"

    def test_invalid_explicit_flag_raises(self):
        from agent2kb.main import _resolve_actor

        with pytest.raises(ValueError, match="--actor"):
            _resolve_actor("banana")

    def test_env_case_insensitive(self, monkeypatch):
        from agent2kb.main import _resolve_actor

        monkeypatch.setenv("KB_ACTOR", "AGENT")
        assert _resolve_actor(None) == "agent"
        monkeypatch.setenv("KB_ACTOR", "  User  ")
        assert _resolve_actor(None) == "user"
