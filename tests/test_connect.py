"""`a2kb connect obsidian|agent`: output for the Docker backend and for other installs."""

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agent2kb import bootstrap, connect
from agent2kb import config as kbconfig
from agent2kb.main import app
from agent2kb.models import ProfileConfig

runner = CliRunner()

OBS_SECRET = "OBSIDIANSECRET" + "x" * 26
MCP_TOKEN = "mcp-token-" + "t" * 30


def profile(**kw) -> ProfileConfig:
    base = dict(
        name="research",
        bucket="kb-data",
        prefix="kbs/research/",
        s3_endpoint="http://seaweedfs:8333",
        region="us-east-1",
        access_key="WRITERKEY",
        secret_key="WRITERSECRET",
    )
    base.update(kw)
    return ProfileConfig(**base)


@pytest.fixture
def secrets(tmp_path):
    sdir = tmp_path / "secrets"
    sdir.mkdir()
    (sdir / "connect.json").write_text(
        json.dumps(
            {
                "version": 1,
                "bucket": "kb-data",
                "kbs": {
                    "research": {
                        "writer": {"access_key": "WRITERKEY", "secret_key": "WRITERSECRET"},
                        "obsidian": {"access_key": "OBSIDIANKEY", "secret_key": OBS_SECRET},
                    }
                },
                "mcp": {"research": {"port": 8765, "token": MCP_TOKEN}},
            }
        )
    )
    return sdir


def docker_env(secrets, **extra):
    return {"KB_CONNECT_DIR": str(secrets), **extra}


class TestObsidianDocker:
    def test_uses_the_dedicated_obsidian_key_not_the_writer_key(self, secrets):
        s = connect.obsidian_settings(profile(), docker_env(secrets))
        assert s.access_key == "OBSIDIANKEY" and s.secret_key == OBS_SECRET
        assert s.source == "docker"
        assert s.bucket == "kb-data" and s.prefix == "kbs/research/" and s.path_style

    def test_revoked_key_is_refused_and_rotate_shows_the_new_one(self, tmp_path):
        from agent2kb import bootstrap

        sdir = tmp_path / "keys"
        bootstrap.ensure_identities(sdir, ["research"], [], bucket="kb-data")
        env = docker_env(sdir)
        first = connect.obsidian_settings(profile(), env).access_key
        bootstrap.revoke_credentials(sdir, "research", "obsidian", listed=[])
        with pytest.raises(connect.ConnectError, match="revoked.*rotate research --role obsidian"):
            connect.obsidian_settings(profile(), env)
        bootstrap.rotate_credentials(sdir, "research", "obsidian")
        again = connect.obsidian_settings(profile(), env)
        assert again.access_key != first and again.secret_key

    def test_secret_only_with_the_flag(self, secrets):
        s = connect.obsidian_settings(profile(), docker_env(secrets))
        hidden = connect.render_obsidian(s)
        assert OBS_SECRET not in hidden and "--show-secret" in hidden
        assert "OBSIDIANKEY" in hidden and "WRITERSECRET" not in hidden
        assert OBS_SECRET in connect.render_obsidian(s, show_secret=True)

    def test_names_the_remotely_save_fields_and_the_prefix(self, secrets):
        text = connect.render_obsidian(connect.obsidian_settings(profile(), docker_env(secrets)))
        for label in (
            "Endpoint",
            "Region",
            "Access Key ID",
            "Secret Access Key",
            "Bucket Name",
            "S3 URL style",
            "Change The Remote Prefix",
        ):
            assert label in text
        assert "kbs/research/" in text and "Clippings/" in text

    def test_loopback_endpoint_warns(self, secrets):
        s = connect.obsidian_settings(profile(), docker_env(secrets))
        assert s.endpoint == "http://127.0.0.1:8333"
        assert any("phone" in w for w in s.warnings)
        assert "WARNING" in connect.render_obsidian(s)

    def test_domain_gives_https_and_no_warning(self, secrets):
        s = connect.obsidian_settings(profile(), docker_env(secrets, DOMAIN="kb.example.com"))
        assert s.endpoint == "https://s3.kb.example.com" and s.warnings == []

    def test_public_host(self, secrets):
        s = connect.obsidian_settings(
            profile(), docker_env(secrets, KB_PUBLIC_HOST="10.0.0.5", KB_S3_PORT="9000")
        )
        assert s.endpoint == "http://10.0.0.5:9000" and s.warnings == []


class TestObsidianOtherInstalls:
    def test_uses_the_profile(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        p = profile(s3_endpoint="https://s3.example.org", access_key="AK", secret_key="SK")
        s = connect.obsidian_settings(p, env, config_path="/x/config.yaml")
        assert (s.endpoint, s.access_key, s.secret_key) == ("https://s3.example.org", "AK", "SK")
        assert s.source == "profile" and "/x/config.yaml" in s.secret_source
        assert any("same key agent2kb itself uses" in w for w in s.warnings)
        assert "SK" not in connect.render_obsidian(s).replace("SKIP", "")

    def test_internal_service_name_warns(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        s = connect.obsidian_settings(profile(), env)
        assert any("internal name" in w for w in s.warnings)

    def test_aws_default_endpoint_and_virtual_hosted(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        s = connect.obsidian_settings(profile(s3_endpoint=None, region="eu-west-1"), env)
        assert s.endpoint == "https://s3.eu-west-1.amazonaws.com" and not s.path_style

    def test_credential_chain_prints_the_sheet_with_placeholders(self, tmp_path, monkeypatch):
        # U04: no key in the config (AWS credential chain) still yields the non-secret
        # settings; the credential lines are placeholders, never the machine's session.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "MACHINEKEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "MACHINESECRET")
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        p = profile(access_key=None, secret_key=None, s3_endpoint=None, region="eu-west-1")
        s = connect.obsidian_settings(p, env)
        assert not s.credentials_known
        assert s.access_key == connect.PLACEHOLDER_ACCESS_KEY
        assert s.secret_key == connect.PLACEHOLDER_SECRET_KEY
        assert any("no access key" in w and "credential chain" in w for w in s.warnings)
        for show in (False, True):
            out = connect.render_obsidian(s, show_secret=show)
            assert "https://s3.eu-west-1.amazonaws.com" in out
            assert "kb-data" in out and "kbs/research/" in out and "eu-west-1" in out
            assert connect.PLACEHOLDER_ACCESS_KEY in out
            assert connect.PLACEHOLDER_SECRET_KEY in out
            assert "MACHINE" not in out
            assert "add --show-secret" not in out

    def test_sheet_says_to_leave_encryption_off(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        s = connect.obsidian_settings(profile(access_key="AK", secret_key="SK"), env)
        out = connect.render_obsidian(s)
        assert "encryption password empty" in out and "docs/obsidian.md" in out


class TestAgent:
    def test_docker_prints_claude_mcp_add_with_url_and_token(self, secrets):
        a = connect.agent_settings(
            profile(), docker_env(secrets, DOMAIN="kb.example.com", KB_PUBLIC_HOST="")
        )
        text = connect.render_agent(a, show_secret=True)
        assert (
            "claude mcp add --transport http kb-research "
            "https://mcp.kb.example.com/research/mcp "
            f"--header 'Authorization: Bearer {MCP_TOKEN}'"
        ) in text
        assert "-- docker compose" in text and "a2kb -p research mcp" in text  # stdio alternative
        assert "WARNING" not in text

    def test_token_hidden_without_show_secret(self, secrets):
        text = connect.render_agent(connect.agent_settings(profile(), docker_env(secrets)))
        assert MCP_TOKEN not in text and "Bearer <token>" in text and "--show-secret" in text

    def test_loopback_url_warns(self, secrets):
        text = connect.render_agent(connect.agent_settings(profile(), docker_env(secrets)))
        assert "http://127.0.0.1:8765/mcp" in text and "WARNING" in text

    def test_plain_http_warns(self, secrets):
        env = docker_env(secrets, KB_PUBLIC_HOST="kb.lan")
        text = connect.render_agent(connect.agent_settings(profile(), env))
        assert "http://kb.lan:8765/mcp" in text and "plain HTTP" in text

    def test_kb_not_served(self, secrets):
        with pytest.raises(connect.ConnectError, match="KB_MCP_KBS"):
            connect.agent_settings(profile(name="work", prefix="kbs/work/"), docker_env(secrets))

    def test_without_docker(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        text = connect.render_agent(connect.agent_settings(profile(), env))
        assert "claude mcp add kb-research -- a2kb -p research mcp" in text
        assert "--transport http kb-research http://127.0.0.1:8765/mcp" in text
        assert "KB_MCP_TOKEN" in text and "Bearer <that token>" in text

    def test_without_docker_custom_url(self, tmp_path):
        env = {"KB_CONNECT_DIR": str(tmp_path / "none")}
        a = connect.agent_settings(profile(), env, url="http://127.0.0.1:9000/mcp")
        assert "a2kb -p research mcp --http 127.0.0.1:9000" in connect.render_agent(a)


# ─── the commands ────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch, secrets):
    for k in list(os.environ):
        if k.startswith("KB_") or k == "DOMAIN":
            monkeypatch.delenv(k)
    h = tmp_path / "home"
    (h / ".kb").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(kbconfig, "USER_CONFIG_PATH", h / ".kb" / "config.yaml")
    monkeypatch.setattr(kbconfig, "USER_CONFIG_DIR", h / ".kb")
    monkeypatch.setenv("KB_CONNECT_DIR", str(secrets))
    monkeypatch.chdir(h)
    (h / ".kb" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "storage": {"bucket": "kb-data", "endpoint": "http://seaweedfs:8333"},
                "kbs": {
                    "research": {
                        "storage": {"access_key": "WRITERKEY", "secret_key": "WRITERSECRET"}
                    },
                    "work": {"storage": {"access_key": "W2", "secret_key": "W2S"}},
                },
            }
        )
    )
    return h


class TestCommands:
    def test_obsidian(self, home):
        r = runner.invoke(app, ["connect", "obsidian", "research"])
        assert r.exit_code == 0, r.output
        assert "OBSIDIANKEY" in r.output and OBS_SECRET not in r.output
        assert "kbs/research/" in r.output

    def test_obsidian_show_secret(self, home):
        r = runner.invoke(app, ["connect", "obsidian", "research", "--show-secret"])
        assert r.exit_code == 0 and OBS_SECRET in r.output

    def test_obsidian_credential_chain_exits_zero(self, home, tmp_path, monkeypatch):
        cfg = home / ".kb" / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "storage": {"bucket": "kb-data", "region": "eu-west-1"},
                    "kbs": {"notes": {}},
                }
            )
        )
        monkeypatch.setenv("KB_CONNECT_DIR", str(tmp_path / "none"))
        r = runner.invoke(app, ["connect", "obsidian", "notes"])
        assert r.exit_code == 0, r.output
        assert "kbs/notes/" in r.output and connect.PLACEHOLDER_ACCESS_KEY in r.output

    def test_obsidian_through_dash_p(self, home):
        r = runner.invoke(app, ["-p", "research", "connect", "obsidian"])
        assert r.exit_code == 0 and "OBSIDIANKEY" in r.output

    def test_agent(self, home):
        r = runner.invoke(app, ["connect", "agent", "research"])
        assert r.exit_code == 0, r.output
        assert MCP_TOKEN not in r.output.replace("\n", "")
        r = runner.invoke(app, ["connect", "agent", "research", "--show-secret"])
        assert r.exit_code == 0, r.output
        assert "claude mcp add --transport http kb-research" in r.output
        assert f"Bearer {MCP_TOKEN}" in r.output.replace("\n", "")

    def test_two_kbs_need_a_name(self, home):
        r = runner.invoke(app, ["connect", "obsidian"])
        assert r.exit_code == 2 and "Which KB?" in r.output

    def test_unknown_kb(self, home):
        r = runner.invoke(app, ["connect", "obsidian", "nope"])
        assert r.exit_code == 2 and "nope" in r.output

    def test_bootstrap_is_hidden_but_works(self, home):
        assert "bootstrap" not in runner.invoke(app, ["--help"]).output
        assert "connect" in runner.invoke(app, ["--help"]).output
        r = runner.invoke(app, ["bootstrap", "--help"])
        assert r.exit_code == 0 and "provision" in r.output and "mcp-serve" in r.output


def test_connect_dir_default_is_the_container_path():
    assert str(bootstrap.connect_dir({})) == "/connect"
