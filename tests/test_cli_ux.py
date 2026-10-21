# ruff: noqa: F811
"""CLI ergonomics: --help without a profile, command names in hints, help layout."""

import os
import re
from pathlib import Path

import pytest
import typer.main
import yaml
from typer.testing import CliRunner

import agent2kb
from agent2kb import config as kbconfig
from agent2kb.main import _HELP_ORDER, app
from tests.test_connect import home as connect_home  # noqa: F401  (fixture)
from tests.test_connect import secrets  # noqa: F401  (fixture)
from tests.test_new_setup import NON_INTERACTIVE, s3  # noqa: F401  (fixture)
from tests.test_new_setup import home as setup_home  # noqa: F401  (fixture)

runner = CliRunner()

#: A bare `kb` used as a command (the old entry point), as opposed to `a2kb`, a
#: `kb:` YAML key, `kb-mcp`, a `.kb/` path or prose such as "the kb config".
BARE_KB_COMMAND = re.compile(
    r"(?<![\w./-])kb(?= (?:-p|--profile|--config|--help|ingest|compile|query|lint|status|setup|new|"
    r"doctor|list|config|connect|mcp|stubs|embed|promote|clip|rm|backfill-source|manifest|"
    r"outputs|scaffold|backup|restore)\b)"
)


def _walk(command, path=()):
    yield path
    for name, sub in getattr(command, "commands", {}).items():
        yield from _walk(sub, (*path, name))


ALL_PATHS = [p for p in _walk(typer.main.get_command(app)) if p]


@pytest.fixture
def bare_env(tmp_path, monkeypatch):
    """No config, no profile, no KB_* variables, and an empty scratch HOME."""
    for k in list(os.environ):
        if k.startswith("KB_"):
            monkeypatch.delenv(k)
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(kbconfig, "USER_CONFIG_PATH", h / ".kb" / "config.yaml")
    monkeypatch.setattr(kbconfig, "USER_CONFIG_DIR", h / ".kb")
    monkeypatch.chdir(h)
    return h


class TestHelpNeedsNoProfile:
    def test_every_command_is_covered(self):
        assert len(ALL_PATHS) > 25

    @pytest.mark.parametrize("path", ALL_PATHS, ids=[" ".join(p) for p in ALL_PATHS])
    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_help_works_without_any_config(self, bare_env, path, flag):
        result = runner.invoke(app, [*path, flag])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
        assert "No profile" not in result.output

    def test_a_real_command_still_demands_a_profile(self, bare_env):
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 2 and "No profile specified" in result.output

    def test_help_after_double_dash_is_not_a_help_request(self):
        from agent2kb.main import _is_help_request

        assert _is_help_request(["ingest", "--help"]) and _is_help_request(["ingest", "-h"])
        assert not _is_help_request(["query", "--", "--help"])
        assert not _is_help_request(["query", "what is --helpful"])


class TestNoBareKbCommand:
    def test_pattern_catches_the_old_form(self):
        assert BARE_KB_COMMAND.search("Try: kb -p notes ingest --url <url>")
        assert BARE_KB_COMMAND.search("run `kb status`")
        assert not BARE_KB_COMMAND.search("Try: a2kb -p notes ingest --url <url>")
        assert not BARE_KB_COMMAND.search("the kb-mcp service, kb: notes, .kb/config.yaml")

    def test_no_source_string_names_the_old_command(self):
        root = Path(agent2kb.__file__).parent
        offenders = []
        for path in [*root.rglob("*.py"), *root.rglob("*.md")]:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                # `source="kb query"` and friends are stored frontmatter values that older
                # KBs already contain (compile keys off them), not instructions to a user.
                if line.lstrip().startswith("#") or 'source="kb ' in line or "fm.source" in line:
                    continue
                if BARE_KB_COMMAND.search(line):
                    offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        assert not offenders, "\n".join(offenders)

    def test_setup_and_new_output(self, setup_home, s3, tmp_path):
        result = runner.invoke(app, NON_INTERACTIVE)
        assert result.exit_code == 0, result.output
        assert "a2kb" in result.output and not BARE_KB_COMMAND.search(result.output)
        result = runner.invoke(app, ["new", "second"])
        assert result.exit_code == 0, result.output
        assert "a2kb -p second ingest" in " ".join(result.output.split())
        assert not BARE_KB_COMMAND.search(result.output)
        alt = tmp_path / "alt.yaml"
        result = runner.invoke(app, ["--config", str(alt), *NON_INTERACTIVE, "--kb-name", "other"])
        assert result.exit_code == 0, result.output
        assert not BARE_KB_COMMAND.search(result.output)
        assert "--config" in " ".join(result.output.split())  # non-default path is named

    def test_connect_output(self, connect_home):
        for cmd in ("obsidian", "agent"):
            result = runner.invoke(app, ["connect", cmd, "research", "--show-secret"])
            assert result.exit_code == 0, result.output
            assert not BARE_KB_COMMAND.search(result.output)

    def test_doctor_output(self, tmp_path, bare_env):
        path = tmp_path / "c.yaml"
        path.write_text(
            yaml.dump(
                {
                    "profiles": {
                        "t": {
                            "s3_endpoint": "http://127.0.0.1:1", "access_key": "k",
                            "secret_key": "s", "bucket": "bkt", "prefix": "t/",
                            "compile_provider": "groq", "groq_api_key": "g",
                        }
                    }
                }
            )
        )
        path.chmod(0o600)
        result = runner.invoke(app, ["-p", "t", "--config", str(path), "doctor", "--read-only"])
        assert result.exit_code == 1 and "✗" in result.output
        assert not BARE_KB_COMMAND.search(result.output)


class TestHelpLayout:
    def test_commands_are_grouped_in_order(self):
        out = runner.invoke(app, ["--help"]).output
        panels = ["Getting started", "Daily use", "Agents", "Maintenance", "Backup"]
        positions = [out.index(p) for p in panels]
        assert positions == sorted(positions)
        assert "delete" not in re.findall(r"^│ (\S+)", out, re.M)

    def test_delete_alias_is_hidden_but_works(self):
        assert runner.invoke(app, ["delete", "--help"]).exit_code == 0

    def test_every_visible_command_has_a_rank(self):
        top = typer.main.get_command(app).commands
        assert set(top) <= set(_HELP_ORDER) | {"delete"}


def _help_text(path) -> str:
    return runner.invoke(app, [*path, "--help"], terminal_width=200).output


class TestHelpWording:
    FORBIDDEN = re.compile(
        r"``|n't\b|\b(?:you|it|that|we|they)'(?:re|ll|ve|s)\b|OVERWRITTEN|\bWOULD\b|\bLOCAL\b"
        r"|\bJust\b|\bjunk\b|flooding|Karpathy|Claude's|a agent2kb|\bschema\b",
    )

    @pytest.mark.parametrize("path", ALL_PATHS, ids=[" ".join(p) for p in ALL_PATHS])
    def test_help_text_is_formal(self, path):
        # --show-completion and --install-completion are Typer's own wording.
        text = "\n".join(
            line for line in _help_text(path).splitlines() if "completion" not in line
        )
        found = self.FORBIDDEN.findall(text)
        assert not found, f"`a2kb {' '.join(path)} --help`: {found}"

    def test_wikilink_examples_survive_rich_markup(self):
        assert "[[wikilinks]]" in _help_text(("promote",))
        assert "[[Foo]]" in _help_text(("stubs",))
        assert "[digest, weekly]" in _help_text(("promote",))

    def test_init_help_describes_what_it_creates(self):
        text = " ".join(_help_text(("init",)).split())
        assert "index.md" in text and "log.md" in text and "concepts" in text

    def test_hard_wrapped_paragraphs_are_joined(self):
        from agent2kb.main import reflow_help

        example = "Example:\n  a2kb ingest\n  a2kb query\n"
        text = "Summary.\n\nOne sentence that\nwraps here.\n\n" + example
        assert reflow_help(text) == "Summary.\n\nOne sentence that wraps here.\n\n" + example
        assert "mode 0600 and ends" in " ".join(_help_text(("setup",)).split())
        # no line break between "with mode" and "0600" in the rendered help of a wide terminal
        assert "with mode 0600" in _help_text(("setup",)).replace("\n", " ")

    def test_env_variable_error_points_at_the_variable(self, bare_env, monkeypatch):
        monkeypatch.setenv("KB_PRIVACY_MODE", "maybe")
        monkeypatch.setenv("KB_PROFILE", "demo")
        (bare_env / ".kb").mkdir()
        (bare_env / ".kb" / "config.yaml").write_text(
            yaml.safe_dump({"profiles": {"demo": {
                "s3_endpoint": "http://127.0.0.1:1", "access_key": "k", "secret_key": "s",
                "bucket": "b", "prefix": "demo/", "compile_provider": "groq",
            }}})
        )
        (bare_env / ".kb" / "config.yaml").chmod(0o600)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 2
        flat = " ".join(result.output.split())
        assert "KB_PRIVACY_MODE" in flat and "not a valid boolean" in flat
        assert "unset it" in flat and "Check your config files" not in flat
