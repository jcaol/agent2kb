"""Isolated CLI execution, failure classification, and fallback routing.

The CLIs are full coding agents. agent2kb feeds them untrusted web content, so
every call must run without the user's settings, MCP servers or tools, from
an empty directory — and failures must be sorted into "this item failed"
versus "the provider is down" so a run can stop or switch CLIs.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent2kb.cli_runner import (
    CliError,
    CliResult,
    CliRouter,
    CliTarget,
    CliTimeout,
    ProviderExhaustedError,
    fallback_target_for,
    run_claude,
    run_codex,
    target_for,
)
from agent2kb.models import LLMProvider, ProfileConfig

HAIKU = CliTarget("claude", "claude-haiku-5-5", "high")
SONNET = CliTarget("claude", "claude-sonnet-5-5", "medium")
LUNA = CliTarget("codex", "gpt-6-luna", "high")


def _proc(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _claude_json(**over):
    data = {
        "is_error": False,
        "result": "compiled page",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 800,
                  "cache_read_input_tokens": 0, "output_tokens": 40},
    }
    data.update(over)
    return json.dumps(data)


class TestRunClaude:
    def test_runs_isolated(self):
        calls = {}

        def fake_run(args, **kw):
            calls["args"], calls["kw"] = args, kw
            return _proc(_claude_json())

        with patch("subprocess.run", side_effect=fake_run):
            result = run_claude(HAIKU, "SYSTEM", "USER")

        args, kw = calls["args"], calls["kw"]
        assert result.text == "compiled page"
        assert result.input_tokens == 802 and result.output_tokens == 40
        # No tools, no settings, no MCP, no session, system prompt replaced.
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--setting-sources") + 1] == ""
        for flag in ("--strict-mcp-config", "--no-session-persistence",
                     "--disable-slash-commands", "--system-prompt-file"):
            assert flag in args
        assert args[args.index("--model") + 1] == "claude-haiku-5-5"
        assert args[args.index("--effort") + 1] == "high"
        # Untrusted content goes on stdin, never argv; cwd is a throwaway dir.
        assert kw["input"] == "USER"
        assert "USER" not in args
        assert "kb-claude-" in kw["cwd"]

    def test_read_dir_grants_read_on_that_directory_only(self, tmp_path):
        """An unscoped `--allowedTools Read` approves reading ANY file, which
        let text inside an image pull ~/.kb/config.yaml into the KB."""
        calls = {}

        def fake_run(args, **kw):
            calls["args"], calls["kw"] = args, kw
            return _proc(_claude_json())

        stage = tmp_path / "stage"
        stage.mkdir()
        with patch("subprocess.run", side_effect=fake_run):
            run_claude(HAIKU, "S", "U", read_dir=str(stage))

        args = calls["args"]
        real = os.path.realpath(stage)
        assert args[args.index("--tools") + 1] == "Read"
        allowed = args[args.index("--allowedTools") + 1]
        assert allowed == f"Read(/{real}/**)"
        assert "--add-dir" not in args
        assert calls["kw"]["cwd"] == real

    def test_tolerates_diagnostic_line_before_json(self):
        out = "[claude-code:notice] something\n" + _claude_json(result="ok")
        with patch("subprocess.run", return_value=_proc(out)):
            assert run_claude(HAIKU, "S", "U").text == "ok"

    def test_unknown_model_is_exhausted(self):
        out = _claude_json(is_error=True, api_error_status=404,
                           result="There's an issue with the selected model (x).")
        with patch("subprocess.run", return_value=_proc(out, returncode=1)):
            with pytest.raises(ProviderExhaustedError):
                run_claude(HAIKU, "S", "U")

    def test_low_credit_is_exhausted(self):
        out = _claude_json(is_error=True, result="Credit balance is too low")
        with patch("subprocess.run", return_value=_proc(out, returncode=1)):
            with pytest.raises(ProviderExhaustedError):
                run_claude(HAIKU, "S", "U")

    def test_transient_error_is_item_failure(self):
        out = _claude_json(is_error=True, api_error_status=529, result="Overloaded")
        with patch("subprocess.run", return_value=_proc(out, returncode=1)):
            with pytest.raises(CliError) as exc:
                run_claude(HAIKU, "S", "U")
        assert not isinstance(exc.value, ProviderExhaustedError)

    def test_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            with pytest.raises(CliTimeout):
                run_claude(HAIKU, "S", "U", timeout=5)

    def test_empty_result_is_failure(self):
        with patch("subprocess.run", return_value=_proc(_claude_json(result="  "))):
            with pytest.raises(CliError):
                run_claude(HAIKU, "S", "U")


FEATURES_LIST = """\
shell_tool                               stable             true
unified_exec                             stable             true
browser_use                              stable             true
computer_use                             stable             true
memories                                 stable             false
item_ids                                 removed            true
api_key_model_discovery                  stable             true
enable_request_compression               stable             true
code_mode                                under development  true
"""


class TestCodexFeatureLockdown:
    def setup_method(self):
        from agent2kb.cli_runner import _codex_disabled_features
        _codex_disabled_features.cache_clear()

    def test_disables_every_non_keep_feature_regardless_of_state(self):
        from agent2kb.cli_runner import _codex_disabled_features

        with patch("subprocess.run", return_value=_proc(FEATURES_LIST)):
            disabled = _codex_disabled_features("codex")

        assert set(disabled) == {
            "shell_tool", "unified_exec", "browser_use", "computer_use",
            "code_mode", "memories",
        }
        assert "api_key_model_discovery" not in disabled
        assert "item_ids" not in disabled  # removed features are skipped

    def test_feature_off_in_user_config_is_still_disabled(self):
        """`features list` applies config.toml but exec ignores it, so a tool
        shown as false here would be back on at exec time."""
        from agent2kb.cli_runner import _codex_disabled_features

        listing = FEATURES_LIST.replace(
            "browser_use                              stable             true",
            "browser_use                              stable             false",
        )
        with patch("subprocess.run", return_value=_proc(listing)):
            assert "browser_use" in _codex_disabled_features("codex")

    def test_fails_closed_when_list_unreadable(self):
        from agent2kb.cli_runner import _codex_disabled_features

        with patch("subprocess.run", return_value=_proc("", "boom", returncode=1)):
            with pytest.raises(ProviderExhaustedError):
                _codex_disabled_features("codex")

    def test_fails_closed_on_changed_value_format(self):
        """A switch to on/off must not parse as "nothing to disable"."""
        from agent2kb.cli_runner import _codex_disabled_features

        listing = FEATURES_LIST.replace(" true", " on").replace(" false", " off")
        with patch("subprocess.run", return_value=_proc(listing)):
            with pytest.raises(ProviderExhaustedError):
                _codex_disabled_features("codex")

    def test_fails_closed_on_any_unparseable_line(self):
        from agent2kb.cli_runner import _codex_disabled_features

        listing = FEATURES_LIST + "Some new footer text\n"
        with patch("subprocess.run", return_value=_proc(listing)):
            with pytest.raises(ProviderExhaustedError):
                _codex_disabled_features("codex")

    def test_fails_closed_when_shell_tools_renamed(self):
        from agent2kb.cli_runner import _codex_disabled_features

        listing = FEATURES_LIST.replace("shell_tool ", "shell_v2   ")
        with patch("subprocess.run", return_value=_proc(listing)):
            with pytest.raises(ProviderExhaustedError):
                _codex_disabled_features("codex")

    def test_fails_closed_on_empty_output(self):
        from agent2kb.cli_runner import _codex_disabled_features

        with patch("subprocess.run", return_value=_proc("")):
            with pytest.raises(ProviderExhaustedError):
                _codex_disabled_features("codex")


class TestRunCodex:
    @pytest.fixture(autouse=True)
    def _features(self):
        with patch("agent2kb.cli_runner._codex_disabled_features",
                   return_value=("shell_tool", "browser_use")):
            yield

    def _fake(self, text="", stdout="", stderr="", returncode=0, sink=None):
        def fake_run(args, **kw):
            if sink is not None:
                sink.update(args=args, kw=kw)
            if text:
                with open(args[args.index("-o") + 1], "w") as f:
                    f.write(text)
            return _proc(stdout, stderr, returncode)
        return fake_run

    def test_read_only_sandbox_and_stdin_prompt(self):
        sink = {}
        with patch("subprocess.run", side_effect=self._fake("page", sink=sink)):
            result = run_codex(LUNA, "SYSTEM", "USER")

        args, kw = sink["args"], sink["kw"]
        assert result.text == "page"
        assert args[args.index("--sandbox") + 1] == "read-only"
        for flag in ("--skip-git-repo-check", "--ignore-user-config",
                     "--ignore-rules", "--ephemeral"):
            assert flag in args
        disabled = [args[i + 1] for i, a in enumerate(args) if a == "--disable"]
        assert disabled == ["shell_tool", "browser_use"]
        assert args[args.index("-m") + 1] == "gpt-6-luna"
        assert "SYSTEM" in kw["input"] and "USER" in kw["input"]
        assert "USER" not in " ".join(args)

    def test_out_of_credits_is_exhausted(self):
        fake = self._fake(stderr="ERROR: Your workspace is out of credits.", returncode=1)
        with patch("subprocess.run", side_effect=fake):
            with pytest.raises(ProviderExhaustedError):
                run_codex(LUNA, "S", "U")

    def test_other_failure_is_item_failure(self):
        fake = self._fake(stderr="ERROR: stream disconnected", returncode=1)
        with patch("subprocess.run", side_effect=fake):
            with pytest.raises(CliError) as exc:
                run_codex(LUNA, "S", "U")
        assert not isinstance(exc.value, ProviderExhaustedError)


class TestRouter:
    def _router(self, behaviours, fallback=LUNA):
        """behaviours: {command: callable(target) -> CliResult or raise}"""
        calls = []

        def fake_run_cli(target, system, user, *, timeout):
            calls.append(target.command)
            return behaviours[target.command](target)

        router = CliRouter(HAIKU, fallback)
        return router, calls, patch("agent2kb.cli_runner.run_cli", side_effect=fake_run_cli)

    @staticmethod
    def _ok(target):
        return CliResult(f"from {target.command}", target)

    @staticmethod
    def _down(target):
        raise ProviderExhaustedError(f"{target.command} down")

    @staticmethod
    def _slow(target):
        raise CliTimeout(f"{target.command} slow")

    def test_primary_success_never_touches_fallback(self):
        router, calls, p = self._router({"claude": self._ok, "codex": self._ok})
        with p:
            assert router.run("s", "u").text == "from claude"
        assert calls == ["claude"]

    def test_exhausted_primary_switches_for_rest_of_run(self):
        router, calls, p = self._router({"claude": self._down, "codex": self._ok})
        with p:
            assert router.run("s", "u").text == "from codex"
            assert router.run("s", "u").text == "from codex"
        # The dead primary is probed once, not once per item.
        assert calls == ["claude", "codex", "codex"]

    def test_timeout_retries_item_on_fallback_but_is_not_sticky(self):
        state = {"n": 0}

        def flaky(target):
            state["n"] += 1
            if state["n"] == 1:
                raise CliTimeout("slow once")
            return CliResult("from claude", target)

        router, calls, p = self._router({"claude": flaky, "codex": self._ok})
        with p:
            assert router.run("s", "u").text == "from codex"
            assert router.run("s", "u").text == "from claude"
        assert calls == ["claude", "codex", "claude"]

    def test_both_down_is_exhausted(self):
        router, _, p = self._router({"claude": self._down, "codex": self._down})
        with p, pytest.raises(ProviderExhaustedError) as exc:
            router.run("s", "u")
        assert "claude down" in str(exc.value) and "codex down" in str(exc.value)

    def test_no_fallback_reraises(self):
        router, _, p = self._router({"claude": self._down}, fallback=None)
        with p, pytest.raises(ProviderExhaustedError):
            router.run("s", "u")

    def test_same_command_fallback_is_dropped(self):
        assert CliRouter(HAIKU, SONNET).fallback is None


def _profile(**over):
    base = dict(
        name="t", s3_endpoint="http://x", access_key="a", secret_key="b",
        bucket="b", prefix="t/", compile_provider=LLMProvider.CLI,
        cli_command="claude", default_model="claude-haiku-5-5",
        cli_reasoning_effort="high",
    )
    base.update(over)
    return ProfileConfig(**base)


class TestProfileTargets:
    def test_compile_and_aux_split(self):
        p = _profile(aux_model="claude-sonnet-5-5", aux_effort="medium")
        assert target_for(p, "compile") == HAIKU
        assert target_for(p, "aux") == SONNET

    def test_aux_defaults_to_compile(self):
        p = _profile()
        assert target_for(p, "aux") == HAIKU

    def test_codex_pin_on_claude_command_is_replaced(self):
        """Switching cli_command without updating the pin must not hand
        claude a gpt model name."""
        p = _profile(default_model="gpt-5.6-luna")
        assert target_for(p, "compile").model == "claude-haiku-5-5"

    def test_fallback_resolves_in_its_own_family(self):
        p = _profile(cli_fallback_command="codex", cli_fallback_model="gpt-6-luna")
        assert fallback_target_for(p) == LUNA
        # A claude model pinned for a codex fallback falls back to codex's default.
        p = _profile(cli_fallback_command="codex", cli_fallback_model="claude-haiku-5-5")
        assert fallback_target_for(p).model == "gpt-6-luna"

    def test_fallback_disabled(self):
        assert fallback_target_for(_profile(cli_fallback_command="")) is None

    def test_unknown_purpose_rejected(self):
        with pytest.raises(ValueError):
            target_for(_profile(), "digest")


class TestSecretGuard:
    """Last line of defence: a credential in model output is never returned."""

    def test_output_with_credential_is_discarded(self):
        router = CliRouter(HAIKU, None, secrets=["AKIA-very-secret-value"])
        leak = CliResult("page text AKIA-very-secret-value more", HAIKU)
        with patch("agent2kb.cli_runner.run_cli", return_value=leak):
            with pytest.raises(CliError) as exc:
                router.run("s", "u")
        assert "AKIA" not in str(exc.value)  # the error must not echo it either

    def test_short_values_are_ignored(self):
        router = CliRouter(HAIKU, None, secrets=["abc", ""])
        with patch("agent2kb.cli_runner.run_cli", return_value=CliResult("abc def", HAIKU)):
            assert router.run("s", "u").text == "abc def"

    def test_profile_secrets_collects_credential_fields(self):
        from agent2kb.cli_runner import profile_secrets

        p = _profile(access_key="ACCESS-1234567890", secret_key="SECRET-1234567890",
                     groq_api_key="gsk-1234567890")
        found = profile_secrets(p)
        assert {"ACCESS-1234567890", "SECRET-1234567890", "gsk-1234567890"} <= set(found)
        assert "claude-haiku-5-5" not in found


class TestChildEnvironment:
    """The CLIs read untrusted content: they get no S3 or provider keys of ours."""

    ENV = {
        "PATH": "/usr/bin", "HOME": "/home/alpha", "LANG": "C.UTF-8",
        "KB_SECRET_KEY": "s3-secret", "KB_MCP_TOKEN": "tok", "KB_CONFIG": "/x.yaml",
        "AWS_SECRET_ACCESS_KEY": "aws-secret", "AWS_PROFILE": "alpha",
        "GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o", "GEMINI_API_KEY": "ge",
        "GOOGLE_API_KEY": "go", "VOYAGE_API_KEY": "v", "OPENAI_API_KEY": "oa",
        "ANTHROPIC_API_KEY": "an", "CLAUDE_CODE_OAUTH_TOKEN": "ct", "CODEX_HOME": "/c",
        "HTTPS_PROXY": "http://proxy.example.com:3128",
    }
    DROPPED_ALWAYS = {
        "KB_SECRET_KEY", "KB_MCP_TOKEN", "KB_CONFIG", "GROQ_API_KEY",
        "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "VOYAGE_API_KEY",
    }

    def _claude_env(self, extra=None):
        seen = {}

        def fake_run(args, **kw):
            seen.update(kw)
            return _proc(_claude_json())

        with patch.dict(os.environ, {**self.ENV, **(extra or {})}, clear=True), \
             patch("subprocess.run", side_effect=fake_run):
            run_claude(HAIKU, "SYSTEM", "USER")
        return seen["env"]

    def test_claude_gets_its_login_but_not_our_secrets(self):
        env = self._claude_env()
        assert not self.DROPPED_ALWAYS & set(env)
        assert "AWS_SECRET_ACCESS_KEY" not in env and "AWS_PROFILE" not in env
        assert "OPENAI_API_KEY" not in env
        for name in ("PATH", "HOME", "LANG", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "HTTPS_PROXY"):
            assert env[name] == self.ENV[name]

    def test_claude_on_bedrock_keeps_aws_credentials(self):
        env = self._claude_env({"CLAUDE_CODE_USE_BEDROCK": "1"})
        assert env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
        assert not self.DROPPED_ALWAYS & set(env)

    def test_codex_keeps_openai_key_and_codex_home(self):
        seen = {}

        def fake_run(args, **kw):
            seen.update(kw)
            with open(args[args.index("-o") + 1], "w") as f:
                f.write("page")
            return _proc()

        with patch.dict(os.environ, self.ENV, clear=True), \
             patch("agent2kb.cli_runner._codex_disabled_features", return_value=()), \
             patch("subprocess.run", side_effect=fake_run):
            run_codex(LUNA, "SYSTEM", "USER")
        env = seen["env"]
        assert not self.DROPPED_ALWAYS & set(env)
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert env["OPENAI_API_KEY"] == "oa" and env["CODEX_HOME"] == "/c"
