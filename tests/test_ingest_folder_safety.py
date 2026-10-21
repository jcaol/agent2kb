"""Folder ingest must stay inside the chosen folder and leave credentials behind (F02)."""

import os
from pathlib import Path

import pytest

from agent2kb.ingest.base import IngestPipeline, select_folder_files
from agent2kb.models import ProfileConfig

SECRET = "FAKE-SECRET-MARKER"


class _S3:
    def __init__(self):
        self.store: dict[str, str] = {}

    def download(self, key):
        return self.store[key]

    def upload(self, key, body, content_type="text/markdown"):
        self.store[key] = body


def _pipeline():
    profile = ProfileConfig(name="p", bucket="b", prefix="projects/p/", region="us-east-1")
    pipe = IngestPipeline(profile=profile, s3=_S3())
    pipe._extract_entities = lambda key: None  # no LLM in these tests
    return pipe


def _write(root: Path, rel: str, text: str = "note body") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "notes"
    _write(root, "a.md", "# A\n\nalpha")
    _write(root, "sub/b.txt", "beta")
    _write(root, ".aws/credentials", SECRET)
    _write(root, ".ssh/id_rsa", SECRET)
    _write(root, ".kb/config.yaml", SECRET)
    _write(root, ".git/config", SECRET)
    _write(root, ".obsidian/plugins/sync/data.json", SECRET)
    _write(root, ".drafts/hidden-note.md", "hidden draft")
    _write(root, ".hidden.md", "hidden file")
    _write(root, "sub/.env", SECRET)
    _write(root, "sub/server.pem", SECRET)
    _write(root, "node_modules/pkg/readme.md", "vendored")
    return root


def _rels(root, files):
    return sorted(p.relative_to(root).as_posix() for p in files)


def test_recursive_selection_skips_hidden_and_credentials(tree):
    sel = select_folder_files(tree, recursive=True)
    assert _rels(tree, sel.files) == ["a.md", "sub/b.txt"]
    reasons = dict(sel.skipped)
    assert reasons[".aws/"] == "credential or runtime directory"
    assert reasons[".drafts/"] == "hidden"
    assert reasons["sub/.env"] == "credential file"
    assert reasons["node_modules/"] == "credential or runtime directory"


def test_include_hidden_still_excludes_credentials(tree):
    sel = select_folder_files(tree, recursive=True, include_hidden=True)
    assert _rels(tree, sel.files) == [".drafts/hidden-note.md", ".hidden.md", "a.md", "sub/b.txt"]


def test_non_recursive_reads_top_level_only(tree):
    sel = select_folder_files(tree, recursive=False)
    assert _rels(tree, sel.files) == ["a.md"]
    assert all("/" not in rel.rstrip("/") for rel, _ in sel.skipped)


def test_hidden_is_judged_relative_to_the_chosen_root(tmp_path):
    root = tmp_path / ".vault"  # the root itself may be hidden
    _write(root, "page.md")
    assert _rels(root, select_folder_files(root, recursive=True).files) == ["page.md"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_are_never_followed(tmp_path):
    outside = _write(tmp_path / "elsewhere", "private.txt", SECRET)
    root = tmp_path / "notes"
    _write(root, "a.md")
    (root / "link.txt").symlink_to(outside)
    (root / "linkdir").symlink_to(outside.parent, target_is_directory=True)
    (root / "inside-link.md").symlink_to(root / "a.md")

    for recursive in (False, True):
        sel = select_folder_files(root, recursive=recursive)
        assert _rels(root, sel.files) == ["a.md"]
        reasons = dict(sel.skipped)
        assert reasons["link.txt"] == "symlink"
        assert reasons["inside-link.md"] == "symlink"


def test_ingest_folder_uploads_no_secret(tree, tmp_path):
    outside = _write(tmp_path / "elsewhere", "private.txt", SECRET)
    (tree / "sub" / "link.txt").symlink_to(outside)
    pipe = _pipeline()
    results = pipe.ingest_folder(tree, recursive=True)
    assert len(results) == 2 and all(r.success for r in results)
    assert not any(SECRET in body for body in pipe.s3.store.values())


def test_ingest_folder_ingests_exactly_the_given_selection(tree):
    selection = select_folder_files(tree, recursive=True)
    _write(tree, "late.md", "appeared after the confirmation")
    pipe = _pipeline()
    results = pipe.ingest_folder(tree, recursive=True, selection=selection)
    assert sorted(Path(r.source).name for r in results) == ["a.md", "b.txt"]
    assert not any("appeared after" in body for body in pipe.s3.store.values())


def test_file_replaced_by_a_symlink_after_selection_is_skipped(tree, tmp_path):
    selection = select_folder_files(tree, recursive=True)
    outside = _write(tmp_path / "elsewhere", "private.txt", SECRET)
    (tree / "sub" / "b.txt").unlink()
    (tree / "sub" / "b.txt").symlink_to(outside)
    pipe = _pipeline()
    results = pipe.ingest_folder(tree, recursive=True, selection=selection)
    assert [Path(r.source).name for r in results] == ["a.md"]
    assert not any(SECRET in body for body in pipe.s3.store.values())


def test_parent_folder_swapped_for_a_symlink_after_selection_is_skipped(tree, tmp_path):
    """A folder between the root and a selected file is replaced by a link to a folder
    outside the root that holds a file of the same name: nothing outside is uploaded."""
    selection = select_folder_files(tree, recursive=True)
    outside = tmp_path / "elsewhere"
    _write(outside, "b.txt", SECRET)
    (tree / "sub" / "b.txt").unlink()
    for child in (tree / "sub").iterdir():
        child.unlink()
    (tree / "sub").rmdir()
    (tree / "sub").symlink_to(outside, target_is_directory=True)
    pipe = _pipeline()
    results = pipe.ingest_folder(tree, recursive=True, selection=selection)
    assert [Path(r.source).name for r in results] == ["a.md"]
    assert not any(SECRET in body for body in pipe.s3.store.values())


def test_file_replaced_by_a_fifo_after_selection_is_skipped(tree):
    selection = select_folder_files(tree, recursive=True)
    (tree / "sub" / "b.txt").unlink()
    os.mkfifo(tree / "sub" / "b.txt")
    pipe = _pipeline()
    results = pipe.ingest_folder(tree, recursive=True, selection=selection)
    assert [Path(r.source).name for r in results] == ["a.md"]


# --------------------------------------------------------------------------- CLI


class TestCli:
    @pytest.fixture
    def run(self, tmp_path, monkeypatch):
        import yaml
        from typer.testing import CliRunner

        from agent2kb import main as kb_main

        for key in list(os.environ):
            if key.startswith("KB_"):
                monkeypatch.delenv(key)
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"profiles": {"t": {
            "s3_endpoint": "http://127.0.0.1:1", "access_key": "k", "secret_key": "s",
            "bucket": "bkt", "prefix": "t/", "compile_provider": "groq", "groq_api_key": "g",
        }}}))
        cfg.chmod(0o600)
        seen = {}

        class Pipe:
            def __init__(self, **kw):
                pass

            def ingest_folder(self, folder, recursive=False, **kw):
                seen.update(folder=folder, recursive=recursive, **kw)
                return []

        monkeypatch.setattr(kb_main, "S3Client", lambda profile: _S3())
        monkeypatch.setattr("agent2kb.ingest.base.IngestPipeline", Pipe)

        def invoke(*args, tty=False, stdin=None):
            monkeypatch.setattr(kb_main, "_stdin_is_terminal", lambda: tty)
            result = CliRunner().invoke(
                kb_main.app, ["-p", "t", "--config", str(cfg), "ingest", *args], input=stdin
            )
            return result, seen

        return invoke

    def test_include_hidden_reaches_the_pipeline(self, run, tree):
        result, seen = run("--folder", str(tree), "-r", "--include-hidden", "--actor", "agent")
        assert result.exit_code == 0, result.output
        assert seen["include_hidden"] is True and seen["recursive"] is True
        assert "Skipping" in result.output

    def test_agents_are_never_prompted(self, run, tree):
        result, seen = run("--folder", str(tree), "-r", "--actor", "agent", tty=True)
        assert result.exit_code == 0
        assert "Ingest these" not in result.output and "folder" in seen

    def test_person_at_terminal_sees_the_list_and_can_decline(self, run, tree):
        result, seen = run("--folder", str(tree), "-r", "--actor", "user", tty=True, stdin="n\n")
        assert "sub/b.txt" in result.output and "Ingest these 2 file(s)?" in result.output
        assert ".aws" not in result.output.split("Skipping")[0]
        assert "folder" not in seen  # nothing ingested

    def test_person_confirms(self, run, tree):
        result, seen = run("--folder", str(tree), "-r", "--actor", "user", tty=True, stdin="y\n")
        assert result.exit_code == 0 and "folder" in seen
        # The pipeline gets the list that was shown, not a fresh selection.
        assert _rels(tree, seen["selection"].files) == ["a.md", "sub/b.txt"]

    def test_yes_skips_the_prompt(self, run, tree):
        result, seen = run("--folder", str(tree), "-r", "--actor", "user", "--yes", tty=True)
        assert "Ingest these" not in result.output and "folder" in seen


@pytest.mark.parametrize("name", [
    "prod.env", "terraform.tfvars", "terraform.tfstate", "secrets.yaml", "secret.json",
    "my-service-account.json", ".env.local",
])
def test_more_secret_file_names_are_excluded(name):
    from agent2kb.ingest.base import _excluded_name
    assert _excluded_name(name)


@pytest.mark.parametrize("name", ["notes.md", "environment.md", "secretary.md", "accounts.json"])
def test_ordinary_names_are_not_excluded(name):
    from agent2kb.ingest.base import _excluded_name
    assert not _excluded_name(name)
