"""Guards for the shipped Docker / cron example files (no Docker needed)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _jobs(path: Path) -> set[str]:
    """Wrapper scripts a crontab runs, including commented-out (opt-in) jobs."""
    return set(re.findall(r"scripts/(kb-cron-[a-z]+\.sh)", path.read_text()))


def test_docker_crontab_matches_host_crontab_example():
    assert _jobs(ROOT / "docker/crontab.example") == _jobs(ROOT / "scripts/kb-crontab.example")


def test_cron_examples_name_no_models():
    for f in ("docker/crontab.example", "scripts/kb-crontab.example"):
        text = (ROOT / f).read_text().lower()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"claude|gpt|llama|gemini|haiku|sonnet|--model", code)


def test_docker_crontab_profiles_are_kb_names_the_backend_accepts():
    cron = (ROOT / "docker/crontab.example").read_text()
    names = set(re.findall(r"^[^#\n].*kb-cron-[a-z]+\.sh /data/(\S+) (\S+)$", cron, re.M))
    assert names and all(path == name for path, name in names)
    from agent2kb.bootstrap import parse_names

    assert parse_names(" ".join(name for _, name in names))


def _env_example_settings() -> dict:
    """Every KEY=value in .env.example, commented-out examples included."""
    out = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("#")[0].strip()
    return out


def test_env_example_defaults_start_a_valid_backend():
    from agent2kb import bootstrap

    live = {
        k: v
        for line in (ROOT / ".env.example").read_text().splitlines()
        if (m := re.match(r"^([A-Z][A-Z0-9_]+)=(.*)$", line))
        for k, v in [(m.group(1), m.group(2).split("#")[0].strip())]
    }
    assert bootstrap.kb_names(live)
    assert live["KB_LLM"] in ("claude", "codex", "ollama", "gemini", "groq", "openrouter")
    assert bootstrap.bucket_name(live)


def test_env_example_documents_every_variable_the_compose_file_reads():
    compose = (ROOT / "docker-compose.yml").read_text()
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", compose))
    documented = _env_example_settings()
    # PATH-like plumbing that is not a user setting
    missing = sorted(v for v in used if v not in documented)
    assert not missing, f"not in .env.example: {missing}"


def test_dockerignore_keeps_secrets_out_of_the_build_context():
    ignored = (ROOT / ".dockerignore").read_text().split()
    for entry in (".env", "scripts/kb-crontab", ".git"):
        assert entry in ignored


def test_dockerignore_keeps_private_runtime_files_out():
    ignored = (ROOT / ".dockerignore").read_text().split()
    for entry in (".env.*", ".backups", "kb-*.log", "*.bak"):
        assert entry in ignored


def _git_ignored(path: str) -> bool:
    import shutil
    import subprocess

    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, capture_output=True
    )
    if proc.returncode not in (0, 1):
        pytest.skip(f"git check-ignore failed: {proc.stderr!r}")
    return proc.returncode == 0


@pytest.mark.parametrize("path", [
    ".backups/wiki-2026-10-19-1200.tar.gz",
    ".backups/alpha-wiki-2026-10-19-120000-abcdef.tar.gz",
    "kb-compile.log",
    "kb-backup.log",
    "kb-digest.log",
    "supercronic.log",
    ".env",
    ".env.production",
    ".env.local",
    "config.yaml.bak",
])
def test_gitignore_covers_private_runtime_files(path):
    assert _git_ignored(path)


@pytest.mark.parametrize("path", [".env.example", "config/config.v2.yaml.example"])
def test_gitignore_keeps_templates_tracked(path):
    assert not _git_ignored(path)


def test_base_images_are_pinned_by_digest():
    dockerfile = (ROOT / "Dockerfile").read_text()
    froms = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert froms and all("@sha256:" in f for f in froms)


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_file_parses_with_every_service():
    services = _compose()["services"]
    assert set(services) == {"kb-init", "seaweedfs", "kb-provision", "kb", "kb-mcp", "caddy"}


def test_compose_startup_order():
    s = _compose()["services"]
    done = "service_completed_successfully"
    assert s["seaweedfs"]["depends_on"]["kb-init"]["condition"] == done
    assert s["kb-provision"]["depends_on"]["kb-init"]["condition"] == done
    assert s["kb"]["depends_on"]["kb-provision"]["condition"] == done
    assert s["kb-mcp"]["depends_on"]["kb-provision"]["condition"] == done
    assert s["kb-init"]["restart"] == s["kb-provision"]["restart"] == "no"


def test_seaweedfs_is_on_by_default_and_https_is_opt_in():
    s = _compose()["services"]
    assert "profiles" not in s["seaweedfs"] and "profiles" not in s["kb"]
    assert s["caddy"]["profiles"] == ["https"]
    external = yaml.safe_load((ROOT / "docker-compose.external-s3.yml").read_text())
    assert external["services"]["seaweedfs"]["profiles"]


def test_seaweedfs_loads_the_generated_identities_and_restarts_on_new_kbs():
    s = _compose()["services"]["seaweedfs"]
    assert "-s3.config=/s3conf/s3.json" in s["command"]
    # KB_NAMES in its environment is what makes `up -d` recreate it after editing .env
    assert "KB_NAMES" in s["environment"]
    assert "kb-s3conf:/s3conf:ro" in s["volumes"]


def test_only_the_bootstrap_services_can_read_the_admin_key():
    """The kb service runs LLM CLIs on web content and kb-mcp faces agents: neither may mount
    the volume that holds keys.json (the S3 admin key)."""
    s = _compose()["services"]
    mounting = {n for n, svc in s.items() if any("kb-secrets" in v for v in svc.get("volumes", []))}
    assert mounting == {"kb-init", "kb-provision"}
    assert not any(v.startswith("kb-secrets") and not v.endswith(":ro")
                   for v in s["kb-provision"]["volumes"])  # provision only reads it
    assert s["seaweedfs"]["volumes"] == ["seaweed-data:/data", "kb-s3conf:/s3conf:ro"]
    assert "kb-connect:/connect:ro" in s["kb"]["volumes"]
    assert not any("kb-s3conf" in v or "kb-mcpkeys" in v for v in s["kb"]["volumes"])
    assert sorted(s["kb-mcp"]["volumes"]) == ["kb-home:/home/kb:ro", "kb-mcpkeys:/mcpkeys:ro"]
    assert {"kb-s3conf", "kb-connect", "kb-mcpkeys"} <= set(_compose()["volumes"])


def test_published_ports_default_to_loopback():
    s = _compose()["services"]
    for name in ("seaweedfs", "kb-mcp"):
        assert all(p.startswith("${KB_BIND:-127.0.0.1}:") for p in s[name]["ports"])
    from agent2kb import bootstrap

    last = bootstrap.MCP_PORT_BASE + bootstrap.MCP_PORT_COUNT - 1
    assert f"{bootstrap.MCP_PORT_BASE}-{last}:{bootstrap.MCP_PORT_BASE}-{last}" in " ".join(
        s["kb-mcp"]["ports"]
    )


def test_no_secrets_in_the_compose_file_or_the_dockerfile():
    compose = (ROOT / "docker-compose.yml").read_text()
    # secrets travel in volumes, never as values in the file
    assert "kb-config" not in compose and "./" not in compose.replace("./docker", "")
    assert re.search(r"kb-secrets:/secrets", compose)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "kb-config" not in dockerfile and not re.search(r"(ACCESS|SECRET)_KEY", dockerfile)


def test_dockerfile_prepares_volume_directories_for_the_kb_user():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"mkdir -p /data /secrets /s3conf /connect /mcpkeys /proxy", dockerfile)
    assert "chown -R kb:kb /data /secrets /s3conf /connect /mcpkeys /proxy" in dockerfile
    for var in ("KB_SECRETS_DIR", "KB_S3CONF_DIR", "KB_CONNECT_DIR", "KB_MCPKEYS_DIR"):
        assert var in dockerfile


def test_entrypoint_generates_a_crontab_unless_one_is_mounted():
    entry = (ROOT / "docker/entrypoint.sh").read_text()
    assert "/etc/kb/crontab" in entry and "a2kb bootstrap crontab" in entry
    from agent2kb.bootstrap import render_crontab

    assert "kb-cron-compile.sh" in render_crontab(["research"])


def test_compose_services_use_commands_that_exist():
    from typer.testing import CliRunner

    from agent2kb.main import app

    for name in ("kb-init", "kb-provision", "kb-mcp"):
        command = _compose()["services"][name]["command"]
        assert command[:2] == ["a2kb", "bootstrap"]
        result = CliRunner().invoke(app, command[1:] + ["--help"])
        assert result.exit_code == 0, (name, result.output)
    for name in ("kb", "kb-mcp"):
        check = _compose()["services"][name]["healthcheck"]["test"][1]
        assert check.startswith("a2kb bootstrap ")
        assert CliRunner().invoke(app, check.split()[1:] + ["--help"]).exit_code == 0


def test_no_legacy_setup_scripts_ship():
    # v1-layout IAM/cron helpers were replaced by `a2kb bootstrap` and the crontab template.
    for name in ("kb-iam-setup.sh", "setup_seaweedfs_iam.sh", "setup_cron.sh"):
        assert not (ROOT / "scripts" / name).exists()


def test_docker_e2e_workflow_parses_and_runs_the_script():
    import os

    wf = yaml.safe_load((ROOT / ".github/workflows/docker-e2e.yml").read_text())
    triggers = wf.get(True) or wf["on"]  # PyYAML reads the key `on` as True
    assert {"push", "pull_request", "schedule", "workflow_dispatch"} <= set(triggers)
    steps = wf["jobs"]["e2e"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses and all(re.search(r"@[0-9a-f]{40}\b", u) for u in uses)  # pinned by commit
    script = ROOT / "scripts/docker-e2e.sh"
    assert any(s.get("run") == "scripts/docker-e2e.sh" for s in steps)
    assert os.access(script, os.X_OK)


def test_e2e_script_checks_what_the_docs_promise():
    text = (ROOT / "scripts/docker-e2e.sh").read_text()
    for needle in (
        "a2kb connect obsidian",
        "a2kb connect agent",
        "doctor --read-only",
        "ingest --url https://example.com",
        "docker compose",
        "KB_NAMES=research work",
        "caddy",
    ):
        assert needle in text or needle.replace("docker compose", "dc ") in text, needle


def test_cron_compile_wrapper_is_strict_and_logs_the_failure(tmp_path):
    """The compile wrapper passes --strict and still logs + propagates a nonzero exit."""
    import os
    import subprocess

    fake = tmp_path / "a2kb"
    fake.write_text('#!/usr/bin/env bash\necho "args: $*"\nexit 1\n')
    fake.chmod(0o755)
    env = {**os.environ, "A2KB_BIN": str(fake)}
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts/kb-cron-compile.sh"), str(tmp_path), "notes"],
        env=env, capture_output=True, text=True,
    )
    log = (tmp_path / "kb-compile.log").read_text()
    assert proc.returncode == 1
    assert "args: -p notes compile --strict" in log
    assert "compile done (exit: 1)" in log


def test_cla_workflow_is_pinned_and_never_runs_pull_request_code():
    # pull_request_target runs with a write token: checking out the PR would run untrusted code.
    wf = yaml.safe_load((ROOT / ".github/workflows/cla.yml").read_text())
    triggers = wf.get(True) or wf["on"]
    assert "pull_request_target" in triggers
    steps = wf["jobs"]["cla"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses and all(re.search(r"@[0-9a-f]{40}\b", u) for u in uses)
    assert not any("checkout" in u for u in uses) and not any("run" in s for s in steps)
    assert "CLA.md" in steps[0]["with"]["path-to-document"]
    assert (ROOT / "CLA.md").exists()


def test_supercronic_is_started_by_absolute_path():
    # As PID 1 supercronic forks itself via os.Args[0] without a PATH lookup, so a bare
    # "supercronic" crash-loops the kb container with "Failed to fork exec".
    entry = (ROOT / "docker/entrypoint.sh").read_text()
    assert "exec /usr/local/bin/supercronic " in entry
    assert "exec supercronic " not in entry
    for doc in ("docs/docker.md", "docker/crontab.example"):
        assert " kb supercronic -test" not in (ROOT / doc).read_text().replace("\n", " ")
