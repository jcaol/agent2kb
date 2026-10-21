"""Codex CLI model policy.

Codex validates the model slug server-side and returns HTTP 400 for one the
account cannot use, so a stale ``default_model`` in an old config breaks
compile and image ingest outright instead of degrading. These tests lock the
migration and preflight behaviour that keeps that from happening silently.
"""

from __future__ import annotations

import json

from agent2kb.cli_models import (
    CLI_DEFAULT_EFFORT,
    CLI_DEFAULT_MODEL,
    CLI_EFFORTS,
    LEGACY_CLI_MODELS,
    is_usable_cli_model,
    load_codex_models,
    normalize_cli_effort,
    preflight_cli_model,
    resolve_cli_model,
)


class TestModelResolution:
    def test_retired_slug_migrates_to_default(self):
        assert "gpt-5.4" in LEGACY_CLI_MODELS
        assert resolve_cli_model("gpt-5.4") == CLI_DEFAULT_MODEL

    def test_foreign_provider_slug_migrates_to_default(self):
        assert resolve_cli_model("llama3.3:70b") == CLI_DEFAULT_MODEL

    def test_empty_pin_falls_back(self):
        assert resolve_cli_model(None) == CLI_DEFAULT_MODEL
        assert resolve_cli_model("  ") == CLI_DEFAULT_MODEL

    def test_unknown_newer_slug_passes_through(self):
        """Operators can pin a newer model without a agent2kb release."""
        assert resolve_cli_model("gpt-9.9-nova") == "gpt-9.9-nova"

    def test_default_is_not_itself_retired(self):
        assert CLI_DEFAULT_MODEL not in LEGACY_CLI_MODELS
        assert is_usable_cli_model(CLI_DEFAULT_MODEL)


class TestEffortNormalization:
    def test_supported_levels_pass_through(self):
        assert normalize_cli_effort("max") == "max"
        assert normalize_cli_effort("XHigh") == "xhigh"

    def test_ultra_is_clamped(self):
        """"ultra" exists only on Sol/Terra, not on the CLI default model."""
        assert "ultra" not in CLI_EFFORTS
        assert normalize_cli_effort("ultra") == CLI_DEFAULT_EFFORT

    def test_missing_and_bogus_values_clamp(self):
        assert normalize_cli_effort(None) == CLI_DEFAULT_EFFORT
        assert normalize_cli_effort("aggressive") == CLI_DEFAULT_EFFORT


def _write_cache(tmp_path, models):
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": models}))
    return path


class TestPreflight:
    def test_missing_cache_reports_nothing(self, tmp_path, monkeypatch):
        """No codex install must never turn into a false alarm."""
        monkeypatch.setattr(
            "agent2kb.cli_models.CODEX_MODELS_CACHE", tmp_path / "absent.json"
        )
        assert load_codex_models() is None
        assert preflight_cli_model("anything", "high") == []

    def test_retired_model_is_flagged(self, tmp_path, monkeypatch):
        cache = _write_cache(
            tmp_path,
            [
                {
                    "slug": CLI_DEFAULT_MODEL,
                    "display_name": "Default",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "xhigh"}],
                }
            ],
        )
        monkeypatch.setattr("agent2kb.cli_models.CODEX_MODELS_CACHE", cache)

        problems = preflight_cli_model("gpt-5.4", "xhigh")
        assert len(problems) == 1
        assert "not in codex's model list" in problems[0]
        # The operator needs to know what to switch to.
        assert CLI_DEFAULT_MODEL in problems[0]

    def test_unsupported_effort_is_flagged(self, tmp_path, monkeypatch):
        cache = _write_cache(
            tmp_path,
            [
                {
                    "slug": "gpt-5.6-luna",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "high"},
                        {"effort": "xhigh"},
                        {"effort": "max"},
                    ],
                }
            ],
        )
        monkeypatch.setattr("agent2kb.cli_models.CODEX_MODELS_CACHE", cache)

        problems = preflight_cli_model("gpt-5.6-luna", "ultra")
        assert len(problems) == 1
        assert "does not support reasoning effort 'ultra'" in problems[0]
        assert preflight_cli_model("gpt-5.6-luna", "max") == []

    def test_hidden_models_are_not_suggested(self, tmp_path, monkeypatch):
        cache = _write_cache(
            tmp_path,
            [
                {"slug": "gpt-5.6-luna", "visibility": "list",
                 "supported_reasoning_levels": [{"effort": "xhigh"}]},
                {"slug": "gpt-reserve", "visibility": "hide",
                 "supported_reasoning_levels": [{"effort": "xhigh"}]},
            ],
        )
        monkeypatch.setattr("agent2kb.cli_models.CODEX_MODELS_CACHE", cache)

        problems = preflight_cli_model("gpt-5.4", "xhigh")
        assert "gpt-5.6-luna" in problems[0]
        assert "gpt-reserve" not in problems[0]

    def test_corrupt_cache_is_tolerated(self, tmp_path, monkeypatch):
        bad = tmp_path / "models_cache.json"
        bad.write_text("{not json")
        monkeypatch.setattr("agent2kb.cli_models.CODEX_MODELS_CACHE", bad)
        assert load_codex_models() is None
        assert preflight_cli_model("gpt-5.4", "xhigh") == []
