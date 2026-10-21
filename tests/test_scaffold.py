"""Tests for agent2kb.scaffold — project-rules scaffolding (2026-05-14)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent2kb import scaffold as _sc


class TestRender:
    def test_render_substitutes_profile(self):
        out = _sc.render("alpha")
        # All template references to the profile use --p alpha or `-p alpha`
        assert "a2kb -p alpha query" in out
        assert "{{profile}}" not in out

    def test_render_substitutes_version(self):
        out = _sc.render("alpha")
        assert _sc.TEMPLATE_VERSION in out
        assert "{{version}}" not in out

    def test_render_includes_markers(self):
        out = _sc.render("alpha")
        assert "<!-- kb-scaffold: project-rules start v" in out
        assert "<!-- kb-scaffold: project-rules end -->" in out


class TestFindMarkerBlock:
    def test_finds_block_with_version(self):
        content = (
            "preamble\n\n"
            "<!-- kb-scaffold: project-rules start v0.9.0 -->\n"
            "## KB Discipline\nbody\n"
            "<!-- kb-scaffold: project-rules end -->\n\n"
            "trailing"
        )
        result = _sc.find_marker_block(content)
        assert result is not None
        s, e, version = result
        assert version == "0.9.0"
        assert content[s:e].startswith("<!-- kb-scaffold: project-rules start")
        assert content[s:e].rstrip().endswith(
            "<!-- kb-scaffold: project-rules end -->"
        )

    def test_returns_none_when_absent(self):
        assert _sc.find_marker_block("just some markdown") is None

    def test_returns_none_when_start_only(self):
        content = "<!-- kb-scaffold: project-rules start v0.9.0 -->\nbody"
        assert _sc.find_marker_block(content) is None


class TestFindHeadingBlock:
    def test_finds_legacy_section_by_heading(self):
        content = (
            "## Current Focus\n\nfoo\n\n"
            "## Knowledge-Base Query Discipline (MANDATORY for chat replies)\n\n"
            "old hand-written body line 1\n"
            "old hand-written body line 2\n\n"
            "## Decisions Log\n\n"
            "- entry\n"
        )
        block = _sc.find_heading_block(content)
        assert block is not None
        s, e = block
        assert content[s:e].startswith("## Knowledge-Base Query Discipline")
        # Stops before the next ## heading
        assert content[s:e].strip().endswith("old hand-written body line 2")

    def test_returns_none_when_absent(self):
        assert _sc.find_heading_block("# Title\n\nbody") is None

    def test_runs_to_eof_when_no_following_heading(self):
        content = (
            "## Knowledge-Base Query Discipline\n\nbody\nstill body\n"
        )
        block = _sc.find_heading_block(content)
        assert block is not None
        s, e = block
        assert e == len(content)


class TestUpsertInsert:
    def _tmpfile(self, content: str = "") -> Path:
        f = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".md").name)
        f.write_text(content, encoding="utf-8")
        return f

    def test_insert_into_empty_file(self):
        target = self._tmpfile("")
        result = _sc.upsert(target, "alpha", mode="insert")
        assert "insert" in result["action"]
        written = target.read_text(encoding="utf-8")
        assert "a2kb -p alpha query" in written
        assert _sc.find_marker_block(written) is not None

    def test_insert_before_decisions_log(self):
        target = self._tmpfile(
            "# Project\n\n## Conventions\n\n- foo\n\n## Decisions Log\n\n- entry\n"
        )
        _sc.upsert(target, "beta", mode="insert")
        written = target.read_text(encoding="utf-8")
        # Block must appear BEFORE Decisions Log
        block_idx = written.index("a2kb -p beta query")
        decisions_idx = written.index("## Decisions Log")
        assert block_idx < decisions_idx

    def test_insert_refuses_when_marker_already_present(self):
        existing = _sc.render("alpha")
        target = self._tmpfile(existing)
        with pytest.raises(RuntimeError, match="marker block already exists"):
            _sc.upsert(target, "alpha", mode="insert")


class TestUpsertUpgrade:
    def _tmpfile(self, content: str) -> Path:
        f = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".md").name)
        f.write_text(content, encoding="utf-8")
        return f

    def test_upgrade_replaces_marker_block(self):
        # Old block with a stale version pretending to be an earlier release.
        stale = (
            "preamble\n\n"
            "<!-- kb-scaffold: project-rules start v0.8.0 -->\n"
            "## Old Title\n\nold body using {profile} = whatever\n"
            "<!-- kb-scaffold: project-rules end -->\n\n"
            "## Decisions Log\n\n- entry\n"
        )
        target = self._tmpfile(stale)

        result = _sc.upsert(target, "gamma", mode="upgrade")
        assert "v0.8.0" in result["action"]
        assert _sc.TEMPLATE_VERSION in result["action"]

        written = target.read_text(encoding="utf-8")
        # Preamble + Decisions Log preserved
        assert written.startswith("preamble")
        assert "## Decisions Log" in written
        # New body present, old body gone
        assert "a2kb -p gamma query" in written
        assert "old body" not in written
        # Marker is updated to the current version
        block = _sc.find_marker_block(written)
        assert block is not None
        assert block[2] == _sc.TEMPLATE_VERSION

    def test_upgrade_refuses_without_marker(self):
        target = self._tmpfile("# Project\n\n## Decisions Log\n\n- entry\n")
        with pytest.raises(RuntimeError, match="no marker block found"):
            _sc.upsert(target, "alpha", mode="upgrade")


class TestUpsertForce:
    def _tmpfile(self, content: str) -> Path:
        f = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".md").name)
        f.write_text(content, encoding="utf-8")
        return f

    def test_force_migrates_hand_written_section(self):
        legacy = (
            "# Project\n\n## Current Focus\n\nstuff\n\n"
            "## Knowledge-Base Query Discipline (MANDATORY for chat replies)\n\n"
            "Some old hand-written content that should disappear.\n"
            "More lines.\n\n"
            "## Decisions Log\n\n- entry\n"
        )
        target = self._tmpfile(legacy)
        result = _sc.upsert(target, "delta", mode="force")
        assert "force-replace" in result["action"]

        written = target.read_text(encoding="utf-8")
        # Old content gone
        assert "old hand-written content" not in written
        # Surrounding sections preserved
        assert "## Current Focus" in written
        assert "## Decisions Log" in written
        # New markered block present
        block = _sc.find_marker_block(written)
        assert block is not None
        assert "a2kb -p delta query" in written

    def test_force_falls_back_to_insert_when_no_prior_section(self):
        target = self._tmpfile(
            "# Project\n\n## Conventions\n\n- foo\n\n## Decisions Log\n\n- entry\n"
        )
        result = _sc.upsert(target, "epsilon", mode="force")
        assert "insert" in result["action"].lower()
        written = target.read_text(encoding="utf-8")
        assert "a2kb -p epsilon query" in written


class TestDryRun:
    def test_dry_run_does_not_write(self):
        target = Path(
            tempfile.NamedTemporaryFile(delete=False, suffix=".md").name
        )
        target.write_text("", encoding="utf-8")
        original = target.read_text(encoding="utf-8")

        result = _sc.upsert(target, "alpha", mode="insert", dry_run=True)
        assert "DRY-RUN" in result["action"]
        assert "a2kb -p alpha query" in result["new_content"]
        # File unchanged on disk
        assert target.read_text(encoding="utf-8") == original


class TestInvalidMode:
    def test_unknown_mode_raises(self):
        target = Path(
            tempfile.NamedTemporaryFile(delete=False, suffix=".md").name
        )
        with pytest.raises(ValueError, match="mode must be"):
            _sc.upsert(target, "alpha", mode="banana")


class TestIdempotency:
    """Stamp twice with --upgrade → same result. The whole point of the
    marker is that repeated runs converge."""

    def test_upgrade_is_idempotent(self):
        target = Path(
            tempfile.NamedTemporaryFile(delete=False, suffix=".md").name
        )
        target.write_text("# Project\n\n", encoding="utf-8")
        _sc.upsert(target, "alpha", mode="insert")
        first = target.read_text(encoding="utf-8")

        _sc.upsert(target, "alpha", mode="upgrade")
        second = target.read_text(encoding="utf-8")

        assert first == second
