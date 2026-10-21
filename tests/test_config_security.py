"""Config trust boundaries: ambient AWS credentials and executed commands."""

import pytest
import yaml
from pydantic import ValidationError

from agent2kb import config as kb_config
from agent2kb.config import resolve_profile
from agent2kb.models import ProfileConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("KB_"):
            monkeypatch.delenv(key)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))
    return path


def _user_config(tmp_path, profiles):
    return _write(tmp_path / "home" / "config.yaml", {"profiles": profiles})


def _project(tmp_path, monkeypatch, data):
    proj = tmp_path / "repo"
    _write(proj / ".kb" / "config.yaml", data)
    monkeypatch.chdir(proj)
    return proj


KEYS = {"access_key": "USERKEY", "secret_key": "USERSECRET"}


class TestCredentialChainTrust:
    def test_project_without_keys_and_no_user_profile_is_error(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"other": {"bucket": "b", "prefix": "o/", **KEYS}})
        _project(
            tmp_path,
            monkeypatch,
            {
                "name": "evil",
                "s3_endpoint": "http://attacker.example",
                "bucket": "bkt",
                "prefix": "p/",
            },
        )
        with pytest.raises(PermissionError, match="credential chain"):
            resolve_profile("evil", user)

    def test_project_blank_keys_cannot_enable_chain(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"other": {"bucket": "b", "prefix": "o/", **KEYS}})
        _project(
            tmp_path,
            monkeypatch,
            {
                "name": "evil",
                "s3_endpoint": "http://attacker.example",
                "access_key": "",
                "secret_key": "",
                "bucket": "bkt",
                "prefix": "p/",
            },
        )
        with pytest.raises(PermissionError, match="credential chain"):
            resolve_profile("evil", user)

    def test_empty_project_keys_do_not_blank_user_keys(self, tmp_path, monkeypatch):
        user = _user_config(
            tmp_path,
            {"notes": {"s3_endpoint": "http://good:8333", "bucket": "b", "prefix": "n/", **KEYS}},
        )
        _project(
            tmp_path,
            monkeypatch,
            {"name": "notes", "access_key": "", "secret_key": "", "s3_endpoint": ""},
        )
        profile = resolve_profile("notes", user)
        assert profile.access_key == "USERKEY" and profile.secret_key == "USERSECRET"
        assert profile.s3_endpoint == "http://good:8333"

    def test_project_cannot_set_aws_profile(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"notes": {"bucket": "b", "prefix": "n/", **KEYS}})
        _project(tmp_path, monkeypatch, {"name": "notes", "aws_profile": "prod-admin"})
        with pytest.raises(PermissionError, match="aws_profile"):
            resolve_profile("notes", user)

    def test_project_cannot_set_aws_profile_even_with_keys(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {})
        _project(
            tmp_path,
            monkeypatch,
            {"name": "x", "bucket": "b", "prefix": "p/", "aws_profile": "prod", **KEYS},
        )
        with pytest.raises(PermissionError, match="aws_profile"):
            resolve_profile("x", user)

    def test_project_cannot_redirect_endpoint_of_chain_profile(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"notes": {"bucket": "b", "prefix": "n/"}})
        _project(tmp_path, monkeypatch, {"name": "notes", "s3_endpoint": "http://attacker"})
        with pytest.raises(PermissionError, match="endpoint"):
            resolve_profile("notes", user)

    def test_user_level_chain_profile_is_allowed(self, tmp_path, monkeypatch):
        user = _user_config(
            tmp_path, {"notes": {"bucket": "b", "prefix": "n/", "aws_profile": "kb"}}
        )
        monkeypatch.chdir(tmp_path)  # no project config
        profile = resolve_profile("notes", user)
        assert profile.access_key is None and profile.aws_profile == "kb"

    def test_user_chain_profile_with_project_prefix_override_is_rejected(
        self, tmp_path, monkeypatch
    ):
        """Was "allowed" before the storage-target rule: a project file must not retarget
        the prefix (or bucket/endpoint/region) of a profile that signs with ambient or
        user-level credentials."""
        user = _user_config(tmp_path, {"notes": {"bucket": "b", "prefix": "n/"}})
        _project(tmp_path, monkeypatch, {"name": "notes", "prefix": "projects/notes/"})
        with pytest.raises(PermissionError, match="without its own access_key"):
            resolve_profile("notes", user)

    def test_user_chain_profile_with_project_non_target_settings_is_allowed(
        self, tmp_path, monkeypatch
    ):
        user = _user_config(tmp_path, {"notes": {"bucket": "b", "prefix": "n/"}})
        _project(tmp_path, monkeypatch, {"name": "notes", "output_language": "en"})
        profile = resolve_profile("notes", user)
        assert profile.access_key is None and profile.prefix == "n/"

    def test_env_can_enable_chain_and_aws_profile(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KB_BUCKET", "envbucket")
        monkeypatch.setenv("KB_PREFIX", "p/")
        monkeypatch.setenv("KB_AWS_PROFILE", "kb")
        profile = resolve_profile("anything", user)
        assert profile.aws_profile == "kb" and profile.access_key is None

    def test_env_keys_satisfy_project_without_keys(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {})
        _project(tmp_path, monkeypatch, {"name": "p", "bucket": "b", "prefix": "p/"})
        monkeypatch.setenv("KB_ACCESS_KEY", "ENVKEY")
        monkeypatch.setenv("KB_SECRET_KEY", "ENVSECRET")
        assert resolve_profile("p", user).access_key == "ENVKEY"

    def test_project_with_its_own_keys_still_works(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {})
        _project(
            tmp_path,
            monkeypatch,
            {"name": "p", "s3_endpoint": "http://seaweedfs:8333", "bucket": "b",
             "prefix": "p/", **KEYS},
        )
        assert resolve_profile("p", user).s3_endpoint == "http://seaweedfs:8333"

    def test_helper_drops_blank_values(self):
        layer = kb_config._project_layer({"access_key": "", "bucket": "", "region": None, "x": 1})
        assert layer == {"bucket": "", "x": 1}


class TestCliCommandValidation:
    @pytest.mark.parametrize("command", ["claude", "codex", "claude-code", "my_cli.v2"])
    def test_bare_names_accepted(self, command):
        p = ProfileConfig(name="p", bucket="bkt", prefix="p/", cli_command=command)
        assert p.cli_command == command

    @pytest.mark.parametrize(
        "command",
        ["/tmp/evil", "./claude", "..\\claude", "claude; rm -rf ~", "a b", "$(id)", "bin/codex"],
    )
    @pytest.mark.parametrize("field", ["cli_command", "cli_fallback_command"])
    def test_paths_and_shell_fragments_rejected(self, field, command):
        with pytest.raises(ValidationError, match="bare command name"):
            ProfileConfig(name="p", bucket="bkt", prefix="p/", **{field: command})

    def test_empty_fallback_disables_it(self):
        p = ProfileConfig(name="p", bucket="bkt", prefix="p/", cli_fallback_command="")
        assert p.cli_fallback_command == ""

    def test_empty_primary_stays_empty(self):
        # An empty primary falls back to "claude" in cli_runner; the model keeps it empty.
        p = ProfileConfig(name="p", bucket="bkt", prefix="p/", cli_command="")
        assert p.cli_command == ""

    def test_env_override_is_validated(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"p": {"bucket": "b", "prefix": "p/", **KEYS}})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KB_CLI_COMMAND", "/tmp/evil")
        with pytest.raises(ValidationError, match="bare command name"):
            resolve_profile("p", user)

    def test_project_config_cannot_inject_command(self, tmp_path, monkeypatch):
        user = _user_config(tmp_path, {"p": {"bucket": "b", "prefix": "p/", **KEYS}})
        _project(tmp_path, monkeypatch, {"name": "p", "cli_command": "/tmp/evil"})
        with pytest.raises(ValidationError, match="bare command name"):
            resolve_profile("p", user)
