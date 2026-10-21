"""A person who runs a command before any KB exists is offered `a2kb setup`."""

from typer.testing import CliRunner

from agent2kb import main as main_mod
from agent2kb.main import app

runner = CliRunner()


def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "missing" / "config.yaml"))
    monkeypatch.chdir(tmp_path)


def test_non_interactive_run_keeps_the_config_error(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    result = runner.invoke(app, ["doctor"])  # CliRunner: not a terminal
    assert result.exit_code == 2
    assert "none could be autodetected" in result.output
    assert "Run `a2kb setup` now?" not in result.output


def test_terminal_user_is_offered_setup(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr(main_mod, "_interactive_terminal", lambda: True)
    ran = {}
    monkeypatch.setattr(main_mod, "_run_setup_command",
                        lambda config_file: ran.setdefault("cmd", "setup") and 0)
    result = runner.invoke(app, ["doctor"], input="y\n")
    assert "No knowledge base is set up yet" in result.output
    assert ran.get("cmd") == "setup"
    assert result.exit_code == 0 and "Run your command again" in result.output


def test_declining_keeps_the_config_error(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr(main_mod, "_interactive_terminal", lambda: True)
    result = runner.invoke(app, ["doctor"], input="n\n")
    assert result.exit_code == 2 and "none could be autodetected" in result.output


def test_failed_setup_passes_its_exit_code_on(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    monkeypatch.setattr(main_mod, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(main_mod, "_run_setup_command", lambda config_file: 1)
    result = runner.invoke(app, ["doctor"], input="y\n")
    assert result.exit_code == 1 and "Run your command again" not in result.output


def test_setup_child_never_imports_from_the_current_directory(monkeypatch):
    # `python -m` would put the current directory first on sys.path, so a stray agent2kb/
    # folder there would run instead of the installed package.
    import subprocess

    seen = {}
    monkeypatch.setattr(subprocess, "call", lambda args: seen.setdefault("args", args) and 0)
    main_mod._run_setup_command(None)
    args = seen["args"]
    assert "-m" not in args and args[1] == "-c"
    assert "sys.path[:] = [p for p in sys.path if p not in ('', '.')]" in args[2]
    assert args[-1] == "setup"
