"""Docker backend bootstrap (`a2kb bootstrap ...`): identities, provisioning, crontab, MCP."""

import json
import os
import re
import stat
import sys

import boto3
import botocore.exceptions
import pytest
import yaml
from moto import mock_aws
from rich.console import Console

from agent2kb import bootstrap
from agent2kb.bootstrap import BootstrapError

BUCKET = "kb-data"
CONSOLE = Console(quiet=True)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def init(tmp_path, names=("research",), mcp=None, bucket=BUCKET, **kw):
    sdir = tmp_path / "secrets"
    return bootstrap.ensure_identities(
        sdir, list(names), list(names if mcp is None else mcp), bucket=bucket, **kw
    )


def dirs_env(base, **extra) -> dict:
    """The container's directory variables, one folder each (as the split volumes are)."""
    env = {
        "KB_SECRETS_DIR": str(base / "secrets"),
        "KB_S3CONF_DIR": str(base / "s3conf"),
        "KB_CONNECT_DIR": str(base / "connect"),
        "KB_MCPKEYS_DIR": str(base / "mcpkeys"),
        "KB_PROXY_DIR": str(base / "proxy"),
    }
    env.update(extra)
    return env


def s3_identities(tmp_path) -> dict:
    split = tmp_path / "s3conf" / "s3.json"
    data = json.loads((split if split.exists() else tmp_path / "secrets" / "s3.json").read_text())
    return {i["name"]: i for i in data["identities"]}


# ─── names ───────────────────────────────────────────────────────────


class TestNames:
    def test_commas_spaces_and_repeats(self):
        assert bootstrap.parse_names("research, work  work\nnotes") == ["research", "work", "notes"]

    @pytest.mark.parametrize("bad", ["", "  ", "Research", "a", "x_y", "super", "../x"])
    def test_invalid(self, bad):
        with pytest.raises(BootstrapError):
            bootstrap.parse_names(bad)

    def test_default_is_research(self):
        assert bootstrap.kb_names({}) == ["research"]

    def test_mcp_names(self):
        names = ["a1", "b2"]
        assert bootstrap.mcp_names({}, names) == names
        assert bootstrap.mcp_names({"KB_MCP_KBS": "b2"}, names) == ["b2"]
        assert bootstrap.mcp_names({"KB_MCP_KBS": "none"}, names) == []

    def test_bucket_name_validated(self):
        assert bootstrap.bucket_name({}) == "kb-data"
        with pytest.raises(BootstrapError, match="3-63"):
            bootstrap.bucket_name({"KB_BUCKET_NAME": "kb"})


# ─── identities ──────────────────────────────────────────────────────


class TestIdentities:
    def test_files_are_private(self, tmp_path):
        init(tmp_path)
        for f in ("keys.json", "s3.json"):
            assert mode(tmp_path / "secrets" / f) == 0o600

    def test_scoping(self, tmp_path):
        init(tmp_path, names=("research", "work"))
        ids = s3_identities(tmp_path)
        assert set(ids) == {
            "admin",
            "research-writer",
            "research-obsidian",
            "work-writer",
            "work-obsidian",
        }
        assert "Admin" in ids["admin"]["actions"]
        for kb in ("research", "work"):
            for role in ("writer", "obsidian"):
                actions = ids[f"{kb}-{role}"]["actions"]
                assert "Admin" not in actions
                # every action names this KB's prefix and nothing broader
                assert all(a.endswith(f":{BUCKET}/kbs/{kb}/*") for a in actions)
                assert {a.split(":")[0] for a in actions} >= {"Read", "Write", "List"}
            assert "Tagging" in {a.split(":")[0] for a in ids[f"{kb}-writer"]["actions"]}
            assert "Tagging" not in {a.split(":")[0] for a in ids[f"{kb}-obsidian"]["actions"]}

    def test_keys_are_random_and_distinct(self, tmp_path):
        init(tmp_path, names=("research", "work"))
        creds = [
            (i["credentials"][0]["accessKey"], i["credentials"][0]["secretKey"])
            for i in s3_identities(tmp_path).values()
        ]
        assert len({a for a, _ in creds}) == len(creds)
        assert len({s for _, s in creds}) == len(creds)
        assert all(len(s) >= 40 and re.fullmatch(r"[A-Za-z0-9]+", s) for _, s in creds)
        assert all(re.fullmatch(r"KB[A-Z0-9]{18}", a) for a, _ in creds)

    def test_idempotent(self, tmp_path):
        first = init(tmp_path)
        before = {f: (tmp_path / "secrets" / f).read_bytes() for f in ("keys.json", "s3.json")}
        second = init(tmp_path)
        assert second.added == [] and not second.s3_json_changed
        assert first.added and first.s3_json_changed
        assert before == {
            f: (tmp_path / "secrets" / f).read_bytes() for f in ("keys.json", "s3.json")
        }

    def test_new_names_are_added_existing_untouched(self, tmp_path):
        init(tmp_path, names=("research",))
        before = json.loads((tmp_path / "secrets" / "keys.json").read_text())
        result = init(tmp_path, names=("research", "work"))
        after = json.loads((tmp_path / "secrets" / "keys.json").read_text())
        assert after["admin"] == before["admin"]
        assert after["kbs"]["research"] == before["kbs"]["research"]
        assert after["mcp"]["research"] == before["mcp"]["research"]
        assert "work" in after["kbs"] and result.s3_json_changed
        assert "work-writer" in s3_identities(tmp_path)

    def test_removed_name_keeps_its_keys(self, tmp_path):
        init(tmp_path, names=("research", "work"))
        init(tmp_path, names=("research",))
        assert "work-writer" in s3_identities(tmp_path)

    def test_never_overwrites_existing_secret(self, tmp_path):
        init(tmp_path)
        path = tmp_path / "secrets" / "keys.json"
        data = json.loads(path.read_text())
        data["kbs"]["research"]["writer"] = {"access_key": "KEEPME", "secret_key": "S" * 40}
        path.write_text(json.dumps(data))
        init(tmp_path)
        assert json.loads(path.read_text())["kbs"]["research"]["writer"]["access_key"] == "KEEPME"
        assert s3_identities(tmp_path)["research-writer"]["credentials"][0]["accessKey"] == "KEEPME"

    def test_damaged_keys_file_is_not_reset(self, tmp_path):
        sdir = tmp_path / "secrets"
        sdir.mkdir()
        (sdir / "keys.json").write_text("{not json")
        with pytest.raises(BootstrapError, match="Not touching"):
            init(tmp_path)
        assert (sdir / "keys.json").read_text() == "{not json"

    def test_changing_the_bucket_is_refused(self, tmp_path):
        init(tmp_path)
        with pytest.raises(BootstrapError, match="bucket"):
            init(tmp_path, bucket="other-bucket")

    def test_mcp_ports_and_tokens(self, tmp_path):
        result = init(tmp_path, names=("a1", "b2", "c3"), mcp=("b2", "a1"))
        mcp = result.keys["mcp"]
        assert mcp["b2"]["port"] == 8765 and mcp["a1"]["port"] == 8766
        assert "c3" not in mcp
        assert len(mcp["a1"]["token"]) >= 32 and mcp["a1"]["token"] != mcp["b2"]["token"]
        # ports are stable when more KBs are added later
        again = init(tmp_path, names=("a1", "b2", "c3"), mcp=("c3", "a1", "b2"))
        assert again.keys["mcp"]["b2"]["port"] == 8765
        assert again.keys["mcp"]["a1"]["port"] == 8766
        assert again.keys["mcp"]["c3"]["port"] == 8767

    def test_mcp_port_base_and_bind_from_the_environment(self, tmp_path):
        env = dirs_env(
            tmp_path, KB_NAMES="research", KB_MCP_PORT_BASE="19765", KB_MCP_BIND="127.0.0.1"
        )
        result = bootstrap.run_init(env, lambda _m: None)
        port = result.keys["mcp"]["research"]["port"]
        assert port == 19765
        argv = bootstrap.mcp_proc(env, "research", port, "t" * 40).argv
        assert "--http" in argv and "127.0.0.1:19765" in argv

    @pytest.mark.parametrize("bad", ["80", "x", "70000"])
    def test_bad_mcp_port_base(self, bad):
        with pytest.raises(BootstrapError, match="KB_MCP_PORT_BASE"):
            bootstrap.mcp_port_base({"KB_MCP_PORT_BASE": bad})

    def test_too_many_mcp_kbs(self, tmp_path):
        names = [f"kb{i:02d}" for i in range(bootstrap.MCP_PORT_COUNT + 1)]
        with pytest.raises(BootstrapError, match="At most"):
            init(tmp_path, names=names)

    def test_external_mode_has_no_s3_identities(self, tmp_path):
        result = init(tmp_path, external=True)
        assert "admin" not in result.keys
        assert not (tmp_path / "secrets" / "s3.json").exists()
        assert result.keys["mcp"]["research"]["token"]

    def test_run_init_writes_caddyfile_only_with_domain(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research work")
        bootstrap.run_init(env, say=lambda m: None)
        assert not (tmp_path / "proxy" / "Caddyfile").exists()
        bootstrap.run_init({**env, "DOMAIN": "kb.example.com", "ACME_EMAIL": "a@b.org"}, say=print)
        text = (tmp_path / "proxy" / "Caddyfile").read_text()
        assert "s3.kb.example.com" in text and "reverse_proxy seaweedfs:8333" in text
        assert "handle_path /research/*" in text and "reverse_proxy kb-mcp:8765" in text
        assert "handle_path /work/*" in text and "reverse_proxy kb-mcp:8766" in text
        assert "email a@b.org" in text
        bootstrap.run_init(env, say=lambda m: None)  # domain removed again
        assert not (tmp_path / "proxy" / "Caddyfile").exists()


class TestCaddyfile:
    def test_rejects_injection(self):
        for bad in ("evil.com {\n respond 200\n}", "a b.com", "localhost", "x.com;y"):
            with pytest.raises(BootstrapError):
                bootstrap.render_caddyfile(bad, "", {"a1": 8765})
        with pytest.raises(BootstrapError):
            bootstrap.render_caddyfile("kb.example.com", "a@b.com\nrespond", {})

    def test_rejects_a_trailing_newline(self):
        """`$` matches before a final newline; the patterns must not."""
        with pytest.raises(BootstrapError):
            bootstrap.render_caddyfile("kb.example.com\n", "", {"a1": 8765})
        with pytest.raises(BootstrapError):
            bootstrap.render_caddyfile("kb.example.com", "a@example.com\n", {})
        assert bootstrap.domain_of({"DOMAIN": " kb.example.com\n"}) == "kb.example.com"

    def test_external_s3_has_no_s3_site(self):
        text = bootstrap.render_caddyfile("kb.example.com", "", {"a1": 8765}, bundled_s3=False)
        assert "s3.kb.example.com" not in text and "mcp.kb.example.com" in text


# ─── public addresses ────────────────────────────────────────────────


class TestPublicUrls:
    def test_defaults_are_loopback(self):
        assert bootstrap.public_s3_endpoint({}) == "http://127.0.0.1:8333"
        assert bootstrap.public_mcp_url({}, "research", 8765) == "http://127.0.0.1:8765/mcp"

    def test_host_and_port(self):
        env = {"KB_PUBLIC_HOST": "kb.lan", "KB_S3_PORT": "9000"}
        assert bootstrap.public_s3_endpoint(env) == "http://kb.lan:9000"
        assert bootstrap.public_mcp_url(env, "r", 8766) == "http://kb.lan:8766/mcp"

    def test_domain(self):
        env = {"DOMAIN": "kb.example.com"}
        assert bootstrap.public_s3_endpoint(env) == "https://s3.kb.example.com"
        assert bootstrap.public_mcp_url(env, "r", 8766) == "https://mcp.kb.example.com/r/mcp"

    def test_explicit_wins(self):
        env = {"DOMAIN": "x.example.com", "KB_PUBLIC_S3_URL": "https://s3.other.net/"}
        assert bootstrap.public_s3_endpoint(env) == "https://s3.other.net"

    def test_loopback_detection(self):
        for url in ("http://127.0.0.1:8333", "http://localhost:1", "http://[::1]:8333"):
            assert bootstrap.is_loopback_url(url)
        for url in ("https://s3.example.com", "http://192.168.1.5:8333", "http://seaweedfs:8333"):
            assert not bootstrap.is_loopback_url(url)

    def test_bad_domain(self):
        with pytest.raises(BootstrapError):
            bootstrap.public_s3_endpoint({"DOMAIN": "not a domain"})


# ─── provision (moto) ────────────────────────────────────────────────


@pytest.fixture
def s3():
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


def prov_env(tmp_path, **extra):
    env = dirs_env(tmp_path, **{"KB_NAMES": "research", "KB_LLM": "claude", **extra})
    bootstrap.run_init(env, say=lambda m: None)
    return env


def run_provision(env, tmp_path, **kw):
    return bootstrap.provision(
        env, config_path=tmp_path / "home" / ".kb" / "config.yaml", console=CONSOLE,
        endpoint=None, wait=1, sleep=lambda s: None, **kw,
    )  # fmt: skip


def read_config(tmp_path) -> dict:
    return yaml.safe_load((tmp_path / "home" / ".kb" / "config.yaml").read_text())


class TestProvision:
    def test_creates_bucket_config_and_kbs(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_NAMES="research work")
        result = run_provision(env, tmp_path)
        assert result.bucket_created and result.created == ["research", "work"]
        assert BUCKET in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
        for kb in ("research", "work"):
            assert f"kbs/{kb}/wiki/index.md" in keys and f"kbs/{kb}/raw/.gitkeep" in keys
        path = tmp_path / "home" / ".kb" / "config.yaml"
        assert mode(path) == 0o600
        cfg = read_config(tmp_path)
        assert cfg["version"] == 2
        assert cfg["storage"]["bucket"] == BUCKET
        assert cfg["storage"]["addressing_style"] == "path"
        assert cfg["storage"]["checksum_mode"] == "when_required"
        assert "access_key" not in cfg["storage"]  # every KB has its own key
        assert cfg["llm"] == {"provider": "cli", "command": "claude"}
        assert cfg["defaults"] == {
            "output_language": "en",
            "allow_jina_reader": False,
            "allow_cloud_vision": False,
        }
        keys_file = json.loads((tmp_path / "secrets" / "keys.json").read_text())
        for kb in ("research", "work"):
            assert cfg["kbs"][kb]["storage"] == keys_file["kbs"][kb]["writer"]

    def test_each_kb_gets_its_own_scoped_profile(self, tmp_path, s3):
        from agent2kb import config_v2

        run_provision(prov_env(tmp_path, KB_NAMES="research work"), tmp_path)
        cfg = config_v2.load_v2(tmp_path / "home" / ".kb" / "config.yaml")
        a = config_v2.build_profile(cfg, "research")
        b = config_v2.build_profile(cfg, "work")
        assert a.prefix == "kbs/research/" and b.prefix == "kbs/work/"
        assert a.access_key != b.access_key and a.bucket == b.bucket == BUCKET

    def test_language_from_env(self, tmp_path, s3):
        run_provision(prov_env(tmp_path, KB_LANGUAGE="zh-HK"), tmp_path)
        assert read_config(tmp_path)["defaults"]["output_language"] == "zh-HK"

    def test_api_llm_and_ollama(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_LLM="groq", KB_LLM_API_KEY="gsk-test")
        run_provision(env, tmp_path)
        llm = read_config(tmp_path)["llm"]
        assert llm["provider"] == "groq" and llm["groq_api_key"] == "gsk-test"

    def test_ollama_gets_a_docker_host_and_model(self, tmp_path, s3):
        run_provision(prov_env(tmp_path, KB_LLM="ollama"), tmp_path)
        llm = read_config(tmp_path)["llm"]
        assert llm["provider"] == "ollama"
        assert llm["ollama_host"] == "http://host.docker.internal:11434"
        assert llm["model"] and "versatile" not in llm["model"]

    def test_unknown_llm(self, tmp_path, s3):
        with pytest.raises(BootstrapError, match="KB_LLM"):
            run_provision(prov_env(tmp_path, KB_LLM="nope"), tmp_path)

    def test_idempotent(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        path = tmp_path / "home" / ".kb" / "config.yaml"
        before = path.read_bytes()
        again = run_provision(env, tmp_path)
        assert again.created == [] and again.existing == ["research"] and not again.bucket_created
        assert path.read_bytes() == before

    def test_new_names_are_added_existing_untouched(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        s3.put_object(Bucket=BUCKET, Key="kbs/research/wiki/mine.md", Body=b"mine")
        first = read_config(tmp_path)["kbs"]["research"]
        env = prov_env(tmp_path, KB_NAMES="research work")
        again = run_provision(env, tmp_path)
        assert again.created == ["work"] and again.existing == ["research"]
        assert read_config(tmp_path)["kbs"]["research"] == first
        body = s3.get_object(Bucket=BUCKET, Key="kbs/research/wiki/mine.md")["Body"].read()
        assert body == b"mine"

    def test_existing_settings_win_over_env(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        env = prov_env(tmp_path, KB_NAMES="research work", KB_LLM="groq", KB_LANGUAGE="ja")
        run_provision(env, tmp_path)
        cfg = read_config(tmp_path)
        assert cfg["llm"]["provider"] == "cli" and cfg["defaults"]["output_language"] == "en"
        assert "work" in cfg["kbs"]

    def test_env_mismatch_is_reported_without_secrets(self, tmp_path, s3):
        from rich.console import Console

        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        before = (tmp_path / "home" / ".kb" / "config.yaml").read_text()
        env = prov_env(
            tmp_path, KB_LLM="gemini", KB_LLM_API_KEY="sk-secret-value", KB_LANGUAGE="ja"
        )
        console = Console(record=True, width=200)
        result = bootstrap.provision(
            env, config_path=tmp_path / "home" / ".kb" / "config.yaml", console=console,
            endpoint=None, wait=1, sleep=lambda s: None,
        )  # fmt: skip
        out = " ".join(console.export_text().split())
        assert not result.reconfigured
        assert any("KB_LLM " in m for m in result.mismatches)
        assert any("KB_LANGUAGE" in m for m in result.mismatches)
        assert any("no gemini API key is stored" in m for m in result.mismatches)
        assert "KB_RECONFIGURE=1" in out
        assert "sk-secret-value" not in out
        assert (tmp_path / "home" / ".kb" / "config.yaml").read_text() == before

    def test_matching_env_reports_nothing(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_LANGUAGE="en")
        run_provision(env, tmp_path)
        assert run_provision(env, tmp_path).mismatches == []

    def test_unset_env_values_are_not_compared_with_hand_edits(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        path = tmp_path / "home" / ".kb" / "config.yaml"
        cfg = read_config(tmp_path)
        cfg["llm"] = {"provider": "groq", "groq_api_key": "k"}
        cfg["defaults"]["output_language"] = "zh-HK"
        path.write_text(yaml.safe_dump(cfg))
        env = prov_env(tmp_path, KB_LLM="", KB_LANGUAGE="", KB_RECONFIGURE="1")
        result = run_provision(env, tmp_path)
        assert result.mismatches == [] and not result.reconfigured
        assert read_config(tmp_path)["llm"]["provider"] == "groq"

    def test_reconfigure_switches_llm_and_language_only(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        s3.put_object(Bucket=BUCKET, Key="kbs/research/raw/keep.md", Body=b"keep")
        first = read_config(tmp_path)
        env = prov_env(
            tmp_path, KB_LLM="gemini", KB_LLM_API_KEY="key-1", KB_LANGUAGE="ja",
            KB_RECONFIGURE="1",
        )  # fmt: skip
        result = run_provision(env, tmp_path)
        cfg = read_config(tmp_path)
        assert result.reconfigured
        assert cfg["llm"]["provider"] == "gemini" and cfg["llm"]["gemini_api_key"] == "key-1"
        assert "command" not in cfg["llm"]
        assert cfg["defaults"]["output_language"] == "ja"
        assert cfg["defaults"]["allow_jina_reader"] == first["defaults"]["allow_jina_reader"]
        assert cfg["storage"] == first["storage"] and cfg["kbs"] == first["kbs"]
        assert (tmp_path / "home" / ".kb" / "config.yaml.bak").exists()
        assert s3.get_object(Bucket=BUCKET, Key="kbs/research/raw/keep.md")["Body"].read() == (
            b"keep"
        )
        # a second start with the same .env finds nothing left to apply
        assert run_provision(env, tmp_path).mismatches == []

    def test_reconfigure_same_provider_keeps_stored_key_and_model(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_LLM="groq", KB_LLM_API_KEY="old-key", KB_LLM_MODEL="m-1")
        run_provision(env, tmp_path)
        env = prov_env(tmp_path, KB_LLM="groq", KB_LANGUAGE="ja", KB_RECONFIGURE="1")
        assert run_provision(env, tmp_path).reconfigured
        cfg = read_config(tmp_path)
        assert cfg["llm"]["groq_api_key"] == "old-key" and cfg["llm"]["model"] == "m-1"
        assert cfg["defaults"]["output_language"] == "ja"

    def test_reconfigure_supplies_a_missing_key(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_LLM="groq")
        run_provision(env, tmp_path)
        assert "groq_api_key" not in read_config(tmp_path)["llm"]
        env = prov_env(tmp_path, KB_LLM="groq", KB_LLM_API_KEY="new-key")
        assert run_provision(env, tmp_path).mismatches  # warned, not applied
        assert "groq_api_key" not in read_config(tmp_path)["llm"]
        assert run_provision({**env, "KB_RECONFIGURE": "1"}, tmp_path).reconfigured
        assert read_config(tmp_path)["llm"]["groq_api_key"] == "new-key"

    def test_adopts_a_prefix_that_has_data(self, tmp_path, s3):
        env = prov_env(tmp_path)
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key="kbs/research/raw/old.md", Body=b"old")
        run_provision(env, tmp_path)
        assert s3.get_object(Bucket=BUCKET, Key="kbs/research/raw/old.md")["Body"].read() == b"old"
        assert "research" in read_config(tmp_path)["kbs"]

    def test_requires_init_first(self, tmp_path, s3):
        env = {"KB_SECRETS_DIR": str(tmp_path / "none"), "KB_NAMES": "research"}
        with pytest.raises(BootstrapError, match="a2kb bootstrap init"):
            run_provision(env, tmp_path)

    def test_name_unknown_to_init(self, tmp_path, s3):
        env = prov_env(tmp_path)
        with pytest.raises(BootstrapError, match="work"):
            run_provision({**env, "KB_NAMES": "research work"}, tmp_path)

    def test_old_format_config_is_refused(self, tmp_path, s3):
        env = prov_env(tmp_path)
        cfg = tmp_path / "home" / ".kb" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("profiles:\n  x: {bucket: b, prefix: p/}\n")
        with pytest.raises(BootstrapError, match="old-format"):
            run_provision(env, tmp_path)
        assert cfg.read_text().startswith("profiles:")

    def test_restart_hint_when_seaweedfs_has_not_loaded_the_key(self, tmp_path, s3):
        from agent2kb.s3_client import S3Client

        env = prov_env(tmp_path)
        keys = json.loads((tmp_path / "secrets" / "keys.json").read_text())
        writers = {e["writer"]["access_key"] for e in keys["kbs"].values()}

        def stale(profile):
            """SeaweedFS before a restart: knows the admin key, not the new KB keys."""
            client = S3Client(profile)
            if profile.access_key in writers:
                err = botocore.exceptions.ClientError(
                    {"Error": {"Code": "InvalidAccessKeyId", "Message": "unknown"}},
                    "ListObjectsV2",
                )

                def refuse(*a, **kw):
                    raise err

                client.list_one = refuse
            return client

        with pytest.raises(BootstrapError, match="docker compose restart seaweedfs"):
            run_provision(env, tmp_path, client_factory=stale)
        # the KB is in the config already: running again after the restart just verifies
        assert "research" in read_config(tmp_path)["kbs"]
        assert run_provision(env, tmp_path).existing == ["research"]

    def test_external_mode_uses_the_given_storage(self, tmp_path, s3):
        env = prov_env(
            tmp_path,
            KB_S3_MODE="external",
            KB_EXT_S3_BUCKET="my-bucket",
            KB_EXT_S3_ACCESS_KEY="AKEXT",
            KB_EXT_S3_SECRET_KEY="SKEXT",
            KB_EXT_S3_REGION="us-east-1",
            KB_EXT_S3_PRESET="aws",
            KB_EXT_S3_CREATE_BUCKET="1",
        )
        run_provision(env, tmp_path)
        cfg = read_config(tmp_path)
        assert cfg["storage"]["bucket"] == "my-bucket"
        assert cfg["storage"]["access_key"] == "AKEXT"
        assert "storage" not in cfg["kbs"]["research"]
        assert "my-bucket" in [b["Name"] for b in s3.list_buckets()["Buckets"]]

    def test_external_mode_needs_the_bucket_to_exist(self, tmp_path, s3):
        env = prov_env(
            tmp_path,
            KB_S3_MODE="external",
            KB_EXT_S3_BUCKET="my-bucket",
            KB_EXT_S3_ACCESS_KEY="AKEXT",
            KB_EXT_S3_SECRET_KEY="SKEXT",
            KB_EXT_S3_PRESET="aws",
        )
        with pytest.raises(BootstrapError, match="does not exist"):
            run_provision(env, tmp_path)

    def test_external_mode_missing_settings(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_S3_MODE="external")
        with pytest.raises(BootstrapError, match="KB_EXT_S3_BUCKET"):
            run_provision(env, tmp_path)


class TestWaitForS3:
    class Flaky:
        def __init__(self, failures, error):
            self.failures, self.error = failures, error
            self.profile = None
            self.calls = 0

        def bucket_exists(self):
            self.calls += 1
            if self.calls <= self.failures:
                raise self.error
            return True

    def _unreachable(self):
        return botocore.exceptions.EndpointConnectionError(endpoint_url="http://seaweedfs:8333")

    def test_retries_until_up(self):
        client = self.Flaky(3, self._unreachable())
        assert bootstrap.wait_for_s3(client, timeout=60, sleep=lambda s: None) is True
        assert client.calls == 4

    def test_gives_up(self):
        from agent2kb.models import ProfileConfig

        client = self.Flaky(10**6, self._unreachable())
        client.profile = ProfileConfig(name="p", bucket="bkt", prefix="kbs/")
        with pytest.raises(BootstrapError, match="did not answer"):
            bootstrap.wait_for_s3(client, timeout=0, sleep=lambda s: None)

    def test_bad_keys_stop_at_once_with_restart_hint(self):
        from agent2kb.models import ProfileConfig

        err = botocore.exceptions.ClientError(
            {"Error": {"Code": "InvalidAccessKeyId", "Message": "x"}}, "HeadBucket"
        )
        client = self.Flaky(10**6, err)
        client.profile = ProfileConfig(name="p", bucket="bkt", prefix="kbs/")
        with pytest.raises(BootstrapError, match="restart seaweedfs"):
            bootstrap.wait_for_s3(client, timeout=60, sleep=lambda s: None)
        assert client.calls == 1


# ─── crontab ─────────────────────────────────────────────────────────

CRON_LINE = re.compile(
    r"^[0-9,*/]+ [0-9,*/]+ \* \* [0-9*] /opt/kb/scripts/kb-cron-[a-z]+\.sh \S+ \S+$"
)


class TestCrontab:
    def test_every_job_for_every_kb(self):
        text = bootstrap.render_crontab(["research", "work"])
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        assert all(CRON_LINE.match(ln) for ln in lines), lines
        for kb in ("research", "work"):
            for job in ("compile", "lint", "backup", "stubs"):
                assert any(f"kb-cron-{job}.sh /data/{kb} {kb}" in ln for ln in lines), (kb, job)
        assert "digest" not in "\n".join(lines)

    def test_names_no_model(self):
        text = bootstrap.render_crontab(["research"], digest=True).lower()
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert not re.search(r"claude|gpt|llama|gemini|haiku|sonnet|opus|codex|--model", code)
        header = "\n".join(ln for ln in text.splitlines() if ln.startswith("#"))
        assert not re.search(r"gpt|llama|haiku|sonnet|opus", header)

    def test_compile_starts_are_staggered(self):
        text = bootstrap.render_crontab(["aa", "bb", "cc"])
        minutes = [ln.split(" ")[0] for ln in text.splitlines() if "kb-cron-compile.sh" in ln]
        assert len(set(minutes)) == 3
        assert all(len(m.split(",")) == 4 for m in minutes)

    def test_digest_opt_in(self):
        text = bootstrap.render_crontab(["research"], digest=True)
        assert "kb-cron-digest.sh /data/research research" in text

    def test_run_crontab_reads_the_config(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_NAMES="research work")
        run_provision(env, tmp_path)
        out = tmp_path / "out" / "crontab"
        kbs = bootstrap.run_crontab(env, tmp_path / "home" / ".kb" / "config.yaml", out)
        assert kbs == ["research", "work"]
        assert "/data/work work" in out.read_text()

    def test_admin_kbs_are_not_scheduled(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "storage": {"bucket": "bkt"},
                    "kbs": {"notes": {}, "boss": {"role": "admin"}},
                }
            )
        )
        assert bootstrap.config_kbs(cfg) == ["notes"]

    def test_no_config(self, tmp_path):
        with pytest.raises(BootstrapError, match="provision"):
            bootstrap.config_kbs(tmp_path / "missing.yaml")


# ─── MCP supervision ─────────────────────────────────────────────────


class TestMcp:
    ENV = {"KB_NAMES": "research", "DOMAIN": "kb.example.com", "KB_PUBLIC_HOST": "kb.lan"}

    def test_command_line(self):
        p = bootstrap.mcp_proc(self.ENV, "research", 8765, "tok" * 12)
        argv = " ".join(p.argv)
        assert "-p research mcp --http 0.0.0.0:8765" in argv
        assert "--allow-host kb-mcp:8765" in argv
        assert "--allow-host mcp.kb.example.com" in argv
        assert "--allow-host kb.lan:8765" in argv
        assert "tok" * 12 not in argv  # not in `ps`
        assert p.env["KB_MCP_TOKEN"] == "tok" * 12
        assert "--allow-write" not in argv and "--allow-llm" not in argv

    def test_opt_in_tools(self):
        env = {
            **self.ENV,
            "KB_MCP_ALLOW_WRITE": "1",
            "KB_MCP_INGEST_DOMAINS": "a.org, b.org",
            "KB_MCP_ALLOW_LLM": "1",
        }
        argv = bootstrap.mcp_proc(env, "research", 8765, "t" * 40).argv
        assert "--allow-write" in argv and "--allow-llm" in argv
        assert argv.count("--ingest-allow-domain") == 2

    def test_procs_from_keys_and_config(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_NAMES="research work", KB_MCP_KBS="work")
        run_provision(env, tmp_path)
        procs = bootstrap.mcp_procs(env, tmp_path / "home" / ".kb" / "config.yaml")
        assert [(p.kb, p.port) for p in procs] == [("work", 8765)]

    def test_kb_missing_from_config(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_NAMES="research")
        run_provision(env, tmp_path)
        with pytest.raises(BootstrapError, match="work"):
            bootstrap.mcp_procs(
                {**env, "KB_NAMES": "research work", "KB_MCP_KBS": "work"},
                tmp_path / "home" / ".kb" / "config.yaml",
            )

    def _proc(self, kb, code, sleep):
        argv = [sys.executable, "-c", f"import time,sys; time.sleep({sleep}); sys.exit({code})"]
        return bootstrap.McpProc(kb=kb, port=1, argv=argv, env=dict(os.environ))

    def test_one_exit_stops_the_others(self):
        procs = [self._proc("a", 3, 0.3), self._proc("b", 0, 60)]
        lines = []
        rc = bootstrap.supervise(procs, say=lines.append, poll=0.05, grace=5)
        assert rc == 3
        assert any("'a' exited" in ln for ln in lines)

    def test_clean_exit_is_still_a_failure(self):
        rc = bootstrap.supervise([self._proc("a", 0, 0.1)], say=lambda m: None, poll=0.05, grace=5)
        assert rc == 1

    def test_ports_check(self):
        import socket

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()  # nothing listens now
        assert bootstrap.check_mcp_ports([port]) == [port]


# ─── revoke / rotate ─────────────────────────────────────────────────


def keys_json(tmp_path) -> dict:
    return json.loads((tmp_path / "secrets" / "keys.json").read_text())


class TestRevokeRotate:
    def sdir(self, tmp_path):
        return tmp_path / "secrets"

    def test_revoke_obsidian_marks_and_removes_from_s3_json(self, tmp_path):
        init(tmp_path)
        old = keys_json(tmp_path)["kbs"]["research"]["obsidian"]
        change = bootstrap.revoke_credentials(
            self.sdir(tmp_path), "research", "obsidian", listed=["research"]
        )
        assert change.roles == ["obsidian"] and change.s3_json_changed
        assert "research-obsidian" not in s3_identities(tmp_path)
        assert "research-writer" in s3_identities(tmp_path)
        marker = keys_json(tmp_path)["kbs"]["research"]["obsidian"]
        assert marker["access_key"] == old["access_key"] and marker["revoked_at"]
        assert "secret_key" not in marker  # the secret is gone, the marker stays
        assert old["secret_key"] not in (tmp_path / "secrets" / "s3.json").read_text()
        assert mode(tmp_path / "secrets" / "keys.json") == 0o600

    def test_revoke_twice_is_a_noop(self, tmp_path):
        init(tmp_path)
        args = (self.sdir(tmp_path), "research", "obsidian")
        bootstrap.revoke_credentials(*args, listed=[])
        assert bootstrap.revoke_credentials(*args, listed=[]).noop

    def test_listed_kb_cannot_lose_its_writer_or_everything(self, tmp_path):
        init(tmp_path)
        for role in (None, "writer"):
            with pytest.raises(BootstrapError, match="still in KB_NAMES"):
                bootstrap.revoke_credentials(
                    self.sdir(tmp_path), "research", role, listed=["research"]
                )
        assert "research-writer" in s3_identities(tmp_path)

    def test_unlisted_kb_can_be_revoked_completely(self, tmp_path):
        init(tmp_path, names=("research", "work"))
        init(tmp_path, names=("research",))
        change = bootstrap.revoke_credentials(
            self.sdir(tmp_path), "work", None, listed=["research"]
        )
        assert change.roles == ["writer", "obsidian"]
        assert set(s3_identities(tmp_path)) == {"admin", "research-writer", "research-obsidian"}

    def test_unknown_kb_and_bad_role(self, tmp_path):
        init(tmp_path)
        with pytest.raises(BootstrapError, match="No credentials for KB 'nope'"):
            bootstrap.revoke_credentials(self.sdir(tmp_path), "nope", None, listed=[])
        with pytest.raises(BootstrapError, match="--role"):
            bootstrap.rotate_credentials(self.sdir(tmp_path), "research", "admin")

    def test_revoked_stays_revoked_while_listed_and_init_says_so(self, tmp_path):
        init(tmp_path)
        bootstrap.revoke_credentials(self.sdir(tmp_path), "research", "obsidian", listed=[])
        again = init(tmp_path)
        assert again.added == [] and again.revoked_in_use == ["research-obsidian"]
        assert "research-obsidian" not in s3_identities(tmp_path)

    def test_fully_revoked_kb_listed_again_gets_fresh_keys(self, tmp_path):
        init(tmp_path, names=("research", "work"))
        old = keys_json(tmp_path)["kbs"]["work"]
        init(tmp_path, names=("research",))
        bootstrap.revoke_credentials(self.sdir(tmp_path), "work", None, listed=["research"])
        result = init(tmp_path, names=("research", "work"))
        new = keys_json(tmp_path)["kbs"]["work"]
        assert {"work-writer", "work-obsidian"} <= set(result.added)
        for role in ("writer", "obsidian"):
            assert new[role]["access_key"] != old[role]["access_key"]
            assert "revoked_at" not in new[role]
        assert {"work-writer", "work-obsidian"} <= set(s3_identities(tmp_path))

    def test_rotate_replaces_only_what_was_asked(self, tmp_path):
        init(tmp_path)
        before = keys_json(tmp_path)
        change = bootstrap.rotate_credentials(self.sdir(tmp_path), "research", "obsidian")
        after = keys_json(tmp_path)
        assert change.roles == ["obsidian"] and change.s3_json_changed
        assert after["kbs"]["research"]["writer"] == before["kbs"]["research"]["writer"]
        assert after["admin"] == before["admin"] and after["mcp"] == before["mcp"]
        old, new = before["kbs"]["research"]["obsidian"], after["kbs"]["research"]["obsidian"]
        assert new["access_key"] != old["access_key"] and new["secret_key"] != old["secret_key"]
        cred = s3_identities(tmp_path)["research-obsidian"]["credentials"][0]
        assert cred["accessKey"] == new["access_key"]
        assert old["access_key"] not in (tmp_path / "secrets" / "s3.json").read_text()

    def test_rotate_default_is_both_roles_and_replaces_a_revoked_key(self, tmp_path):
        init(tmp_path)
        bootstrap.revoke_credentials(self.sdir(tmp_path), "research", "obsidian", listed=[])
        change = bootstrap.rotate_credentials(self.sdir(tmp_path), "research")
        assert change.roles == ["writer", "obsidian"]
        assert "research-obsidian" in s3_identities(tmp_path)
        assert "revoked_at" not in keys_json(tmp_path)["kbs"]["research"]["obsidian"]

    def test_rotate_mcp_token(self, tmp_path):
        init(tmp_path)
        old = keys_json(tmp_path)["mcp"]["research"]
        change = bootstrap.rotate_credentials(self.sdir(tmp_path), "research", "mcp")
        new = keys_json(tmp_path)["mcp"]["research"]
        assert change.roles == ["mcp"] and not change.s3_json_changed
        assert new["token"] != old["token"] and new["port"] == old["port"]

    def test_external_mode_refuses(self, tmp_path):
        init(tmp_path, external=True)
        with pytest.raises(BootstrapError, match="external"):
            bootstrap.rotate_credentials(self.sdir(tmp_path), "research", external=True)

    def test_init_warns_about_unlisted_kbs_with_the_command(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research work")
        bootstrap.run_init(env, say=lambda m: None)
        lines: list[str] = []
        result = bootstrap.run_init({**env, "KB_NAMES": "research"}, say=lines.append)
        assert result.unlisted == ["work"]
        text = "\n".join(lines)
        assert "WARNING" in text and "'work' is not in KB_NAMES" in text
        assert "a2kb bootstrap revoke work" in text and "restart seaweedfs" in text
        assert "work-writer" in s3_identities(tmp_path)  # not revoked automatically
        # once revoked, the warning is gone
        bootstrap.run_revoke({**env, "KB_NAMES": "research"}, "work", None, lambda m: None)
        quiet: list[str] = []
        assert bootstrap.run_init({**env, "KB_NAMES": "research"}, say=quiet.append).unlisted == []
        assert "WARNING" not in "\n".join(quiet)

    def test_messages_name_the_restart(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research")
        bootstrap.run_init(env, say=lambda m: None)
        lines: list[str] = []
        bootstrap.run_rotate(env, "research", "obsidian", lines.append)
        text = "\n".join(lines)
        assert "docker compose restart seaweedfs" in text and "--show-secret" in text
        assert "kb-mcp" not in text and "kb-provision" not in text  # an Obsidian key only
        lines.clear()
        bootstrap.run_rotate(env, "research", "writer", lines.append)
        text = "\n".join(lines)
        steps = ("restart seaweedfs", "run --rm kb-provision", "restart kb-mcp")
        order = [text.index(x) for x in steps]
        assert order == sorted(order), text  # in this order
        lines.clear()
        bootstrap.run_rotate(env, "research", "mcp", lines.append)
        text = "\n".join(lines)
        assert "restart kb-mcp" in text and "seaweedfs" not in text


class TestProvisionAfterRotate:
    def test_rotated_writer_key_reaches_the_config(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        old = read_config(tmp_path)["kbs"]["research"]["storage"]["access_key"]
        bootstrap.rotate_credentials(tmp_path / "secrets", "research", "writer")
        result = run_provision(env, tmp_path)
        new = keys_json(tmp_path)["kbs"]["research"]["writer"]
        stored = read_config(tmp_path)["kbs"]["research"]["storage"]
        assert result.rekeyed == ["research"] and result.created == []
        assert stored["access_key"] == new["access_key"] != old
        assert stored["secret_key"] == new["secret_key"]
        assert run_provision(env, tmp_path).rekeyed == []  # and then nothing changes

    def test_revoked_writer_stops_provision(self, tmp_path, s3):
        env = prov_env(tmp_path)
        run_provision(env, tmp_path)
        bootstrap.revoke_credentials(tmp_path / "secrets", "research", "writer", listed=[])
        with pytest.raises(BootstrapError, match="writer key of KB 'research' is revoked"):
            run_provision(env, tmp_path)


class TestSplitSecrets:
    def test_each_view_holds_only_what_its_service_needs(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research work")
        bootstrap.run_init(env, say=lambda m: None)
        keys = keys_json(tmp_path)
        admin = keys["admin"]["secret_key"]
        views = (("s3conf", "s3.json"), ("connect", "connect.json"), ("mcpkeys", "mcp.json"))
        for sub, name in views:
            path = tmp_path / sub / name
            assert mode(path) == 0o600
            text = path.read_text()
            assert (admin in text) == (sub == "s3conf")  # only SeaweedFS must know the admin key
        connect = json.loads((tmp_path / "connect" / "connect.json").read_text())
        for kb in ("research", "work"):
            assert set(connect["kbs"][kb]) == {"obsidian"}  # no writer key
            assert connect["kbs"][kb]["obsidian"] == keys["kbs"][kb]["obsidian"]
        mcp = json.loads((tmp_path / "mcpkeys" / "mcp.json").read_text())
        assert set(mcp) == {"version", "mcp"} and mcp["mcp"] == keys["mcp"]
        writers = [keys["kbs"][k]["writer"]["secret_key"] for k in ("research", "work")]
        for sub in ("connect", "mcpkeys"):
            blob = " ".join(p.read_text() for p in (tmp_path / sub).iterdir())
            assert not any(w in blob for w in writers) and admin not in blob

    def test_revoked_marker_reaches_connect_json_without_a_secret(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research")
        bootstrap.run_init(env, say=lambda m: None)
        bootstrap.run_revoke(env, "research", "obsidian", lambda m: None)
        connect = json.loads((tmp_path / "connect" / "connect.json").read_text())
        marker = connect["kbs"]["research"]["obsidian"]
        assert marker["revoked_at"] and "secret_key" not in marker

    def test_rotation_updates_every_view(self, tmp_path):
        env = dirs_env(tmp_path, KB_NAMES="research")
        bootstrap.run_init(env, say=lambda m: None)
        old = keys_json(tmp_path)
        bootstrap.run_rotate(env, "research", "mcp", lambda m: None)
        new = keys_json(tmp_path)
        assert new["mcp"]["research"]["token"] != old["mcp"]["research"]["token"]
        mcp = json.loads((tmp_path / "mcpkeys" / "mcp.json").read_text())
        assert mcp["mcp"]["research"]["token"] == new["mcp"]["research"]["token"]
        connect = json.loads((tmp_path / "connect" / "connect.json").read_text())
        assert connect["mcp"]["research"]["token"] == new["mcp"]["research"]["token"]

    def test_a_crash_between_the_files_is_healed_by_the_next_init(self, tmp_path):
        """keys.json is written first; the derived files are re-rendered on every init."""
        env = dirs_env(tmp_path, KB_NAMES="research")
        bootstrap.run_init(env, say=lambda m: None)
        stale = (tmp_path / "s3conf" / "s3.json").read_text()
        # as if rotate had written keys.json and died before the derived files
        keys = keys_json(tmp_path)
        keys["kbs"]["research"]["obsidian"] = bootstrap._credential()
        (tmp_path / "secrets" / "keys.json").write_text(json.dumps(keys))
        assert (tmp_path / "s3conf" / "s3.json").read_text() == stale
        result = bootstrap.run_init(env, say=lambda m: None)
        assert result.s3_json_changed and (tmp_path / "s3conf" / "s3.json").read_text() != stale
        assert keys["kbs"]["research"]["obsidian"]["access_key"] in (
            tmp_path / "s3conf" / "s3.json"
        ).read_text()

    def test_mcp_servers_read_only_mcp_json(self, tmp_path, s3):
        env = prov_env(tmp_path, KB_NAMES="research")
        run_provision(env, tmp_path)
        (tmp_path / "secrets" / "keys.json").unlink()  # kb-mcp has no keys.json at all
        procs = bootstrap.mcp_procs(env, tmp_path / "home" / ".kb" / "config.yaml")
        assert [p.kb for p in procs] == ["research"] and procs[0].env["KB_MCP_TOKEN"]

    def test_health_failures_lists_the_failed_checks(self):
        from agent2kb.doctor import Report

        report = Report()
        report.add("Storage", "bucket", "ok")
        report.add("LLM", "claude login", "fail", "not logged in", "run claude auth login")
        lines = bootstrap.health_failures(report, "research")
        assert "doctor --read-only" in lines[0]
        assert lines[1:] == ["  FAIL LLM / claude login: not logged in -> run claude auth login"]
