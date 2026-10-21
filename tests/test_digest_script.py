"""The weekly digest wrapper (scripts/kb-cron-digest.sh) run end to end, offline.

The script is executed by bash against temporary config layouts. ``KB_PYTHON`` and
``A2KB_BIN`` point at a small wrapper that runs the real agent2kb code inside moto's
``mock_aws`` (seeded with a few pages) and stubs only the LLM call, so profile
resolution, the activity snapshot, llm_run's routing and promote are all exercised
without network access or credentials.
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent2kb import llm_run
from agent2kb.cli_runner import CliError, ProviderExhaustedError
from agent2kb.models import LLMProvider, ProfileConfig

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "kb-cron-digest.sh"
ENDPOINT = "https://s3.us-east-1.amazonaws.com"  # intercepted by moto in the wrapper
CANNED = "# KB Digest — stub\n\n## Headline\nA quiet week around [[beta-entity]].\n"

WRAPPER = '''#!{python}
"""Stand-in for the KB interpreter and the a2kb executable, used by test_digest_script."""
import os
import runpy
import sys
import types
from pathlib import Path

from moto import mock_aws

CANNED = {canned!r}


def seed():
    from agent2kb.config import resolve_profile
    from agent2kb.s3_client import S3Client

    prof = resolve_profile(os.environ["KB_TEST_PROFILE"])
    s3 = S3Client(prof)
    s3.create_bucket()
    stamp = os.environ["KB_TEST_STAMP"]
    p = prof.prefix
    s3.upload(f"{{p}}raw/{{stamp}}-alpha-note.md",
              "---\\ntitle: Alpha seeded note\\n---\\n\\nSeeded raw body about beta.\\n")
    s3.upload(f"{{p}}wiki/{{stamp}}-alpha-note.md",
              "---\\ntitle: Alpha wiki page\\n---\\n\\nMentions [[beta-entity]] twice "
              "and [[beta-entity]] again.\\n")


def record(route, prompt):
    Path(os.environ["KB_TEST_CAPTURE"]).write_text(f"route={{route}}\\n{{prompt}}")


def stub_llm():
    from agent2kb.cli_runner import CliRouter
    from agent2kb.compile.compiler import Compiler

    def cli_run(self, system, prompt, *a, **k):
        record("cli", prompt)
        return types.SimpleNamespace(text=CANNED, target="stub", input_tokens=1,
                                     output_tokens=1, cost_usd=None)

    def provider_run(self, messages, purpose="compile", **kwargs):
        record(f"provider:{{self.provider.value}}:{{purpose}}", messages[-1]["content"])
        return CANNED

    CliRouter.run = cli_run
    Compiler._call_llm_with_fallback = provider_run


with mock_aws():
    seed()
    args = sys.argv[1:]
    if args and os.path.isfile(args[0]):
        # Run as the interpreter of an a2kb executable (env shebang, sh trampoline).
        args = args[1:]
    if args[:1] == ["-c"]:
        exec(args[1], {{"__name__": "__main__"}})
    elif args[:1] == ["-"]:
        sys.argv = ["-"]
        exec(compile(sys.stdin.read(), "<stdin>", "exec"), {{"__name__": "__main__"}})
    elif args[:1] == ["-m"]:
        if args[1] == "agent2kb.llm_run":
            stub_llm()
        sys.argv = [args[1], *args[2:]]
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
    else:
        if "promote" in args and os.environ.get("KB_TEST_MODES"):
            draft = Path(args[args.index("--file") + 1])
            Path(os.environ["KB_TEST_MODES"]).write_text(
                f"{{draft}}\\n{{draft.stat().st_mode & 0o777:o}}\\n"
                f"{{draft.parent.stat().st_mode & 0o777:o}}\\n"
            )
            if os.environ.get("KB_TEST_FAIL_PROMOTE"):
                sys.exit(1)
        from agent2kb.main import app
        sys.argv = ["a2kb", *args]
        app()
'''

STORAGE_V2 = {
    "endpoint": ENDPOINT,
    "region": "us-east-1",
    "bucket": "digest-test",
    "access_key": "testing",
    "secret_key": "testing",
}


@pytest.fixture
def wrapper(tmp_path) -> Path:
    path = tmp_path / "bin" / "fakepy"
    path.parent.mkdir()
    path.write_text(WRAPPER.format(python=sys.executable, canned=CANNED))
    path.chmod(0o755)
    return path


def _run(wrapper: Path, home: Path, project: Path, profile: str, *, extra_env=None,
         a2kb: Path | None = None, kb_python: Path | None = None, path_prefix=None,
         cwd: Path | None = None):
    """Run the digest script. ``a2kb`` replaces the a2kb executable; ``kb_python``
    (default: the wrapper) is KB_PYTHON, and is left unset when ``a2kb`` is given
    without it, so the script derives it. With ``cwd`` the project is passed relative
    to it."""
    capture = project.parent / "capture.txt"
    if kb_python is None and a2kb is None:
        kb_python = wrapper
    path = os.environ.get("PATH", "/usr/bin:/bin")
    env = {
        "PATH": f"{path_prefix}{os.pathsep}{path}" if path_prefix else path,
        "HOME": str(home),
        # HOME is a temp dir, so pass on this interpreter's path (user site-packages
        # included) with the checkout first.
        "PYTHONPATH": os.pathsep.join([str(REPO), *(p for p in sys.path if p)]),
        "A2KB_BIN": str(a2kb or wrapper),
        "KB_TEST_PROFILE": profile,
        "KB_TEST_STAMP": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d"),
        "KB_TEST_CAPTURE": str(capture),
        # Keep boto3 away from any real credential or config file.
        "AWS_CONFIG_FILE": str(home / "no-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(home / "no-aws-credentials"),
        **(extra_env or {}),
    }
    if kb_python is not None:
        env["KB_PYTHON"] = str(kb_python)
    project_arg = os.path.relpath(project, cwd) if cwd else str(project)
    proc = subprocess.run(
        ["bash", str(SCRIPT), project_arg, profile],
        env=env, capture_output=True, text=True, timeout=180, cwd=cwd,
    )
    log = (project / "kb-digest.log").read_text() if (project / "kb-digest.log").exists() else ""
    captured = capture.read_text() if capture.exists() else ""
    return proc, log, captured


def _assert_digest_ran(proc, log, captured, route: str):
    assert proc.returncode == 0, f"stderr={proc.stderr}\nlog={log}"
    assert "digest done" in log
    assert "Promoted" in log
    assert captured.startswith(f"route={route}\n")
    # The snapshot reached the prompt, built from the seeded bucket under the KB prefix.
    assert "Alpha seeded note" in captured
    assert "beta-entity: 2" in captured


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32", reason="needs bash and GNU date"
)


def test_v2_pointer_project(tmp_path, wrapper):
    home = tmp_path / "home"
    (home / ".kb").mkdir(parents=True)
    (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({
        "version": 2, "storage": STORAGE_V2,
        "llm": {"provider": "cli", "command": "claude"},
        "kbs": {"notes": {}},
    }))
    project = tmp_path / "work" / "notes-project"
    (project / ".kb").mkdir(parents=True)
    (project / ".kb" / "config.yaml").write_text("kb: notes\n")

    proc, log, captured = _run(wrapper, home, project, "notes")
    _assert_digest_ran(proc, log, captured, "cli")


def test_docker_layout_user_config_only(tmp_path, wrapper):
    """Docker: v2 config under HOME (or KB_CONFIG), cron runs in /data/<kb> with no .kb/."""
    home = tmp_path / "home"
    (home / ".kb").mkdir(parents=True)
    cfg = home / ".kb" / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "version": 2, "storage": STORAGE_V2,
        "llm": {"provider": "cli", "command": "codex"},
        "kbs": {"notes": {}, "alpha": {}},
    }))
    data_dir = tmp_path / "data" / "alpha"
    data_dir.mkdir(parents=True)

    proc, log, captured = _run(wrapper, home, data_dir, "alpha", extra_env={"KB_CONFIG": str(cfg)})
    _assert_digest_ran(proc, log, captured, "cli")


def test_legacy_flat_project_config_with_ollama(tmp_path, wrapper):
    """The old layout keeps working, and a non-CLI provider is honoured for the aux call."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "work" / "alpha"
    (project / ".kb").mkdir(parents=True)
    (project / ".kb" / "config.yaml").write_text(yaml.safe_dump({
        "s3_endpoint": ENDPOINT, "region": "us-east-1", "bucket": "digest-test",
        "access_key": "testing", "secret_key": "testing", "prefix": "projects/alpha/",
        "compile_provider": "ollama",
    }))

    proc, log, captured = _run(wrapper, home, project, "alpha")
    _assert_digest_ran(proc, log, captured, "provider:ollama:aux")


def _v2_project(tmp_path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".kb").mkdir(parents=True)
    (home / ".kb" / "config.yaml").write_text(yaml.safe_dump({
        "version": 2, "storage": STORAGE_V2,
        "llm": {"provider": "cli", "command": "claude"},
        "kbs": {"notes": {}},
    }))
    project = tmp_path / "work" / "notes-project"
    (project / ".kb").mkdir(parents=True)
    (project / ".kb" / "config.yaml").write_text("kb: notes\n")
    return home, project


def _a2kb(tmp_path, text: str) -> Path:
    path = tmp_path / "venv" / "bin" / "a2kb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def test_relative_project_dir_logs_into_the_project(tmp_path, wrapper):
    home, project = _v2_project(tmp_path)
    proc, log, captured = _run(wrapper, home, project, "notes", cwd=tmp_path)
    _assert_digest_ran(proc, log, captured, "cli")
    assert not (project / project.relative_to(tmp_path) / "kb-digest.log").exists()


class TestInterpreterFromA2kb:
    """KB_PYTHON unset: the script finds the interpreter a2kb runs under."""

    def test_env_shebang(self, tmp_path, wrapper):
        home, project = _v2_project(tmp_path)
        a2kb = _a2kb(tmp_path, f"#!/usr/bin/env {wrapper.name}\n")
        proc, log, captured = _run(wrapper, home, project, "notes", a2kb=a2kb,
                                   path_prefix=wrapper.parent)
        _assert_digest_ran(proc, log, captured, "cli")
        assert "WARNING" not in log

    def test_pip_sh_trampoline_for_a_long_path(self, tmp_path, wrapper):
        home, project = _v2_project(tmp_path)
        spaced = tmp_path / "a long venv path" / "bin" / "python"
        spaced.parent.mkdir(parents=True)
        spaced.symlink_to(wrapper)
        # What pip writes when the interpreter path is too long for a shebang.
        quote3 = "'" * 3
        a2kb = _a2kb(tmp_path, f'#!/bin/sh\n{quote3}exec\' "{spaced}" "$0" "$@"\n\' {quote3}\n')
        proc, log, captured = _run(wrapper, home, project, "notes", a2kb=a2kb)
        _assert_digest_ran(proc, log, captured, "cli")
        assert "WARNING" not in log

    def test_unusable_interpreter_falls_back_to_python3_with_a_warning(self, tmp_path, wrapper):
        home, project = _v2_project(tmp_path)
        fallback = tmp_path / "fallback-bin"
        fallback.mkdir()
        (fallback / "python3").symlink_to(wrapper)
        a2kb = _a2kb(tmp_path, "#!/nonexistent/bin/python\n")
        proc, log, captured = _run(wrapper, home, project, "notes", a2kb=a2kb,
                                   path_prefix=fallback)
        assert "WARNING: no interpreter that imports agent2kb" in log
        assert "using python3" in proc.stderr
        # The snapshot and the LLM call ran under the python3 fallback.
        assert captured.startswith("route=cli\n") and "Alpha seeded note" in captured

    def test_explicit_kb_python_is_used_as_given(self, tmp_path, wrapper):
        home, project = _v2_project(tmp_path)
        a2kb = _a2kb(tmp_path, f"#!/usr/bin/env {wrapper.name}\n")
        proc, log, captured = _run(wrapper, home, project, "notes", a2kb=a2kb,
                                   kb_python=wrapper, path_prefix=wrapper.parent)
        _assert_digest_ran(proc, log, captured, "cli")


class TestDraftFilePrivacy:
    """The draft holds KB content: private while it exists, gone when the run ends."""

    def _run_private(self, tmp_path, wrapper, **env):
        home, project = _v2_project(tmp_path)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        modes = tmp_path / "modes.txt"
        old = os.umask(0o022)
        try:
            proc, log, _ = _run(wrapper, home, project, "notes", extra_env={
                "TMPDIR": str(tmpdir), "KB_TEST_MODES": str(modes), **env,
            })
        finally:
            os.umask(old)
        draft, file_mode, dir_mode = modes.read_text().splitlines()
        return proc, log, tmpdir, Path(draft), file_mode, dir_mode

    def test_draft_is_private_and_removed_after_success(self, tmp_path, wrapper):
        proc, log, tmpdir, draft, file_mode, dir_mode = self._run_private(tmp_path, wrapper)
        assert proc.returncode == 0, log
        # The canned answer starts with an H1, so the preamble strip rewrote the draft.
        assert (file_mode, dir_mode) == ("600", "700")
        assert draft.parent.parent == tmpdir
        assert list(tmpdir.iterdir()) == []

    def test_failed_promote_keeps_a_private_draft_and_logs_it(self, tmp_path, wrapper):
        proc, log, tmpdir, draft, file_mode, dir_mode = self._run_private(
            tmp_path, wrapper, KB_TEST_FAIL_PROMOTE="1"
        )
        assert proc.returncode == 1
        assert f"draft kept at {draft}" in log
        assert draft.is_file() and (file_mode, dir_mode) == ("600", "700")
        assert oct(draft.stat().st_mode & 0o777) == "0o600"

    def test_failed_llm_leaves_nothing_behind(self, tmp_path, wrapper):
        home, project = _v2_project(tmp_path)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # An unknown KB fails the run before promote.
        proc, _, _ = _run(wrapper, home, project, "ghost",
                          extra_env={"TMPDIR": str(tmpdir), "KB_TEST_PROFILE": "notes"})
        assert proc.returncode != 0
        assert list(tmpdir.iterdir()) == []


def test_unknown_profile_fails_before_llm(tmp_path, wrapper):
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data" / "ghost"
    data_dir.mkdir(parents=True)
    env = {"KB_TEST_PROFILE": "ghost"}
    proc, log, captured = _run(wrapper, home, data_dir, "ghost", extra_env=env)
    assert proc.returncode != 0
    assert captured == ""


# ─── llm_run routing (in-process) ──────────────────────────────────────


def _profile(provider: LLMProvider) -> ProfileConfig:
    return ProfileConfig(
        name="alpha", s3_endpoint="http://s3.example.com", access_key="k", secret_key="s",
        bucket="b", prefix="projects/alpha/", compile_provider=provider,
    )


def _main(monkeypatch, provider, argv=("--profile", "alpha", "--purpose", "aux")):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("write a digest"))
    with patch.object(llm_run, "resolve_profile", return_value=_profile(provider)):
        return llm_run.main(list(argv))


class TestLlmRunProviders:
    def test_api_provider_does_not_build_cli_router(self, monkeypatch, capsys):
        with patch.object(llm_run, "router_for") as router, patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback", return_value="body"
        ) as call:
            assert _main(monkeypatch, LLMProvider.OLLAMA) == 0
        router.assert_not_called()
        assert call.call_args.kwargs["purpose"] == "aux"
        assert capsys.readouterr().out == "body\n"

    def test_api_provider_exhausted_exits_3(self, monkeypatch):
        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback",
            side_effect=ProviderExhaustedError("no credits"),
        ):
            assert _main(monkeypatch, LLMProvider.GROQ) == 3

    def test_api_provider_failure_exits_2(self, monkeypatch):
        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback",
            side_effect=RuntimeError("boom"),
        ):
            assert _main(monkeypatch, LLMProvider.GEMINI) == 2

    def test_api_provider_empty_output_exits_2(self, monkeypatch):
        with patch(
            "agent2kb.compile.compiler.Compiler._call_llm_with_fallback", return_value="  "
        ):
            assert _main(monkeypatch, LLMProvider.OLLAMA) == 2

    def test_cli_provider_still_uses_router(self, monkeypatch):
        with patch.object(llm_run, "router_for") as router:
            router.return_value.run.side_effect = CliError("down for this item")
            assert _main(monkeypatch, LLMProvider.CLI) == 2
        router.assert_called_once()

    def test_local_provider_never_falls_back_to_cloud(self, monkeypatch):
        from agent2kb.compile import compiler as compiler_mod

        seen = []

        def fake_call(self, provider, messages, model, purpose="compile"):
            seen.append(provider)
            raise RuntimeError("connection refused")

        with patch.object(compiler_mod.Compiler, "_call_provider", fake_call):
            assert _main(monkeypatch, LLMProvider.OLLAMA) == 2
        assert all(p not in compiler_mod.CLOUD_PROVIDERS for p in seen)

    @pytest.mark.parametrize("error,code", [
        (RuntimeError("connection refused"), 2),
        (ProviderExhaustedError("no credits"), 3),
    ])
    def test_cloud_provider_does_not_fall_back_to_other_providers(
        self, monkeypatch, error, code
    ):
        """A Groq profile with Gemini and OpenRouter keys still sends the digest
        to Groq only: llm_run has no chain, unlike compile."""
        from agent2kb.compile import compiler as compiler_mod

        seen = []

        def fake_call(self, provider, messages, model, purpose="compile"):
            seen.append(provider)
            raise error

        profile = _profile(LLMProvider.GROQ).model_copy(
            update={"gemini_api_key": "g", "openrouter_api_key": "o"})
        monkeypatch.setattr("sys.stdin", io.StringIO("write a digest"))
        with patch.object(llm_run, "resolve_profile", return_value=profile), \
                patch.object(compiler_mod.Compiler, "_call_provider", fake_call):
            assert llm_run.main(["--profile", "alpha", "--purpose", "aux"]) == code
        assert seen == [LLMProvider.GROQ]
