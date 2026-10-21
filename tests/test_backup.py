"""`a2kb backup` / `a2kb restore`: per-KB archive names, retention, atomic writes,
private file modes and the KB identity check on restore."""

import io
import json
import os
import stat
import tarfile
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

import agent2kb.main as M
from agent2kb import backup as B
from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client

BUCKET = "notes-bucket"


def _profile(name, prefix=None, bucket=BUCKET):
    return ProfileConfig(
        name=name, s3_endpoint="http://localhost:5000", access_key="testing",
        secret_key="testing", bucket=bucket, prefix=prefix or f"kbs/{name}/",
    )


@pytest.fixture
def kbs():
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket=BUCKET)
        clients = {}
        for name in ("alpha", "beta"):
            c = S3Client(_profile(name))
            c._client = conn
            c.upload(f"kbs/{name}/wiki/page.md", f"# {name} page")
            c.upload(f"kbs/{name}/raw/20260101-src-aaaa1111.md", f"raw {name}")
            c.upload(f"kbs/{name}/outputs/answer.md", f"answer {name}")
            clients[name] = c
        yield clients


def _run(kbs, name, *args):
    with patch.object(M, "resolve_profile", side_effect=lambda n, *a, **k: kbs[n].profile), \
         patch.object(M, "load_config", return_value=MagicMock()), \
         patch.object(M, "get_s3", side_effect=lambda: kbs[M.get_current_profile().name]):
        return CliRunner().invoke(M.app, ["--profile", name, *args])


def _members(path):
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


class TestBackup:
    def test_two_kbs_same_dir_same_moment_keep_both(self, kbs, tmp_path):
        d = tmp_path / "bk"
        assert _run(kbs, "alpha", "backup", "--dir", str(d)).exit_code == 0
        assert _run(kbs, "beta", "backup", "--dir", str(d)).exit_code == 0
        assert _run(kbs, "alpha", "backup", "--dir", str(d)).exit_code == 0
        assert len(B.kb_archives(d, "alpha")) == 2
        (beta,) = B.kb_archives(d, "beta")
        assert _members(beta.path)["wiki/page.md"] == b"# beta page"

    def test_manifest_records_kb_and_counts(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        (a,) = B.kb_archives(d, "alpha")
        manifest = json.loads(_members(a.path)[B.MANIFEST_NAME])
        assert manifest["kb"] == {"name": "alpha", "bucket": BUCKET, "prefix": "kbs/alpha/"}
        assert manifest["files"] == 3 and manifest["counts"]["raw/"] == 1
        assert manifest["include_raw"] is True and manifest["agent2kb_version"]

    def test_no_raw_flag(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d), "--no-raw")
        (a,) = B.kb_archives(d, "alpha")
        assert not any(n.startswith("raw/") for n in _members(a.path))

    def test_retention_only_prunes_own_archives_never_legacy(self, kbs, tmp_path):
        d = tmp_path / "bk"
        d.mkdir()
        legacy = d / "wiki-2026-01-01-1200.tar.gz"
        legacy.write_bytes(b"old")
        for _ in range(3):
            _run(kbs, "alpha", "backup", "--dir", str(d), "--keep", "2")
        _run(kbs, "beta", "backup", "--dir", str(d), "--keep", "1")
        assert len(B.kb_archives(d, "alpha")) == 2
        assert len(B.kb_archives(d, "beta")) == 1
        assert legacy.exists()

    def test_keep_zero_prunes_nothing(self, kbs, tmp_path):
        d = tmp_path / "bk"
        for _ in range(2):
            _run(kbs, "alpha", "backup", "--dir", str(d), "--keep", "0")
        assert len(B.kb_archives(d, "alpha")) == 2

    def test_private_modes_regardless_of_umask(self, kbs, tmp_path):
        d = tmp_path / "new" / "bk"
        old = os.umask(0o002)
        try:
            assert _run(kbs, "alpha", "backup", "--dir", str(d)).exit_code == 0
        finally:
            os.umask(old)
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
        (a,) = B.kb_archives(d, "alpha")
        assert stat.S_IMODE(a.path.stat().st_mode) == 0o600

    def test_interrupted_backup_leaves_no_archive_and_keeps_previous(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        (first,) = B.kb_archives(d, "alpha")
        before = first.path.read_bytes()
        with patch.object(kbs["alpha"], "download_bytes", side_effect=RuntimeError("boom")):
            result = _run(kbs, "alpha", "backup", "--dir", str(d))
        assert result.exit_code != 0
        assert [a.path for a in B.kb_archives(d, "alpha")] == [first.path]
        assert first.path.read_bytes() == before
        assert not list(d.glob(".a2kb-backup-*"))


def _legacy_archive(path, files):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class TestRestore:
    def test_round_trip(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        kbs["alpha"].upload("kbs/alpha/wiki/page.md", "damaged")
        (a,) = B.kb_archives(d, "alpha")
        date = a.stamp[:10]
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", date, "--apply")
        assert r.exit_code == 0, r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "# alpha page"
        assert B.MANIFEST_NAME not in r.output.replace("Using", "")

    def test_picks_own_archive_when_another_kb_is_newer(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        _run(kbs, "beta", "backup", "--dir", str(d))
        (a,) = B.kb_archives(d, "alpha")
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", a.stamp[:10])
        assert r.exit_code == 0 and a.path.name in r.output

    def test_mismatched_manifest_refused_without_flag(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "beta", "backup", "--dir", str(d))
        (b,) = B.kb_archives(d, "beta")
        # An archive renamed to the old format still carries its manifest.
        renamed = d / "wiki-2030-01-01-0000.tar.gz"
        b.path.rename(renamed)
        args = ("restore", "--dir", str(d), "--date", "2030-01-01", "--apply")
        r = _run(kbs, "alpha", *args)
        assert r.exit_code == 1 and "--allow-cross-kb" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "# alpha page"
        r = _run(kbs, "alpha", *args, "--allow-cross-kb")
        assert r.exit_code == 0 and "Cross-KB restore" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "# beta page"

    def test_from_kb_selects_other_kb_but_still_needs_consent(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "beta", "backup", "--dir", str(d))
        (b,) = B.kb_archives(d, "beta")
        args = ("restore", "--dir", str(d), "--date", b.stamp[:10], "--from-kb", "beta")
        r = _run(kbs, "alpha", *args)
        assert r.exit_code == 1
        assert _run(kbs, "alpha", *args, "--allow-cross-kb").exit_code == 0

    def test_same_kb_new_bucket_is_allowed(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        (a,) = B.kb_archives(d, "alpha")
        with tarfile.open(a.path, "r:gz") as tar:
            m = B.read_manifest(tar)
        assert B.identity_mismatch(m, _profile("alpha", bucket="other-bucket")) == []
        assert B.identity_mismatch(m, _profile("alpha", prefix="kbs/x/")) == ["prefix"]

    def test_legacy_archive_restores_with_warning(self, kbs, tmp_path):
        d = tmp_path / "bk"
        d.mkdir()
        _legacy_archive(d / "wiki-2025-05-13-1432.tar.gz", {"wiki/page.md": b"legacy"})
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", "2025-05-13", "--apply")
        assert r.exit_code == 0, r.output
        assert "no manifest" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "legacy"
        # The exact old stamp still selects it.
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", "2025-05-13-1432")
        assert r.exit_code == 0 and "wiki-2025-05-13-1432.tar.gz" in r.output

    def test_missing_archive_exit_1(self, kbs, tmp_path):
        r = _run(kbs, "alpha", "restore", "--dir", str(tmp_path), "--date", "1999-01-01")
        assert r.exit_code == 1 and "No archive" in r.output

    def test_glob_characters_in_date_rejected(self, kbs, tmp_path):
        r = _run(kbs, "alpha", "restore", "--dir", str(tmp_path), "--date", "*")
        assert r.exit_code == 1


def test_archive_names_do_not_cross_match_similar_kb_names(tmp_path):
    (tmp_path / "alpha-wiki-wiki-2026-01-01-000000-abcdef.tar.gz").write_bytes(b"")
    (tmp_path / "alpha-wiki-2026-01-01-000000-abcdef.tar.gz").write_bytes(b"")
    assert [a.path.name for a in B.kb_archives(tmp_path, "alpha")] == [
        "alpha-wiki-2026-01-01-000000-abcdef.tar.gz"
    ]
    assert len(B.kb_archives(tmp_path, "alpha-wiki")) == 1


def _crafted_archive(path, entries):
    """``entries``: (name, type, data, linkname) tuples, written as given."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, data, linkname in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = linkname
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)


class TestRestoreSanitisesMembers:
    UNSAFE = [
        ("/etc/evil.md", tarfile.REGTYPE, b"x", ""),
        ("wiki/../../beta/wiki/page.md", tarfile.REGTYPE, b"x", ""),
        ("../kbs/beta/wiki/page.md", tarfile.REGTYPE, b"x", ""),
        ("wiki/./page.md", tarfile.REGTYPE, b"x", ""),
        ("wiki//page.md", tarfile.REGTYPE, b"x", ""),
        ("wiki\\..\\..\\beta\\page.md", tarfile.REGTYPE, b"x", ""),
        ("wiki/bad\x01name.md", tarfile.REGTYPE, b"x", ""),
        ("_meta/schema.md", tarfile.REGTYPE, b"x", ""),
        ("wiki/link.md", tarfile.SYMTYPE, b"", "/etc/passwd"),
        ("wiki/hard.md", tarfile.LNKTYPE, b"", "wiki/page.md"),
        ("wiki/dev", tarfile.CHRTYPE, b"", ""),
        ("wiki/fifo", tarfile.FIFOTYPE, b"", ""),
    ]

    @pytest.mark.parametrize("entry", UNSAFE, ids=lambda e: repr(e[0]))
    def test_member_problem_flags_unsafe_members(self, entry):
        name, kind, data, linkname = entry
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        assert B.member_problem(info)

    def test_empty_name_is_flagged(self):
        assert B.member_problem(tarfile.TarInfo("")) == "empty name"

    @pytest.mark.parametrize("name", ["wiki/page.md", "raw/20260101-src-aaaa1111.md",
                                      "outputs/agent/a.md", "wiki/topics/alpha notes.md"])
    def test_ordinary_members_pass(self, name):
        assert B.member_problem(tarfile.TarInfo(name)) is None

    def test_crafted_archive_restores_only_safe_members(self, kbs, tmp_path):
        d = tmp_path / "bk"
        d.mkdir()
        entries = [("wiki/page.md", tarfile.REGTYPE, b"restored", ""), *self.UNSAFE]
        entries.append(("wiki/sub", tarfile.DIRTYPE, b"", ""))
        _crafted_archive(d / "wiki-2025-05-13-1432.tar.gz", entries)
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", "2025-05-13",
                 "--target", "", "--apply")
        assert r.exit_code == 0, r.output
        assert "Restored 1 files" in r.output
        assert f"Skipped {len(self.UNSAFE)} archive member(s)" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "restored"
        # Nothing landed in the other KB, outside the prefix or in _meta/.
        assert kbs["beta"].download("kbs/beta/wiki/page.md") == "# beta page"
        conn = kbs["alpha"]._client
        keys = [o["Key"] for o in conn.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
        assert all(k.startswith(("kbs/alpha/", "kbs/beta/")) for k in keys), keys
        assert set(k for k in keys if k.startswith("kbs/alpha/")) == {
            "kbs/alpha/wiki/page.md", "kbs/alpha/raw/20260101-src-aaaa1111.md",
            "kbs/alpha/outputs/answer.md",
        }

    def test_oversized_member_is_flagged(self):
        info = tarfile.TarInfo("wiki/huge.md")
        info.size = B.MAX_RESTORE_MEMBER_BYTES + 1
        assert B.member_problem(info) == "larger than 256 MiB"
        info.size = B.MAX_RESTORE_MEMBER_BYTES
        assert B.member_problem(info) is None

    def test_archive_over_the_total_limit_restores_nothing(self, kbs, tmp_path, monkeypatch):
        d = tmp_path / "bk"
        d.mkdir()
        _crafted_archive(d / "wiki-2025-05-13-1432.tar.gz", [
            ("wiki/page.md", tarfile.REGTYPE, b"restored", ""),
            ("wiki/other.md", tarfile.REGTYPE, b"restored too", ""),
        ])
        monkeypatch.setattr(B, "MAX_RESTORE_TOTAL_BYTES", 10)
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", "2025-05-13",
                 "--apply")
        assert r.exit_code == 1, r.output
        assert "Nothing was restored" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "# alpha page"

    def test_dry_run_reports_skipped_members_too(self, kbs, tmp_path):
        d = tmp_path / "bk"
        d.mkdir()
        _crafted_archive(d / "wiki-2025-05-13-1432.tar.gz", [
            ("wiki/page.md", tarfile.REGTYPE, b"restored", ""),
            ("wiki/link.md", tarfile.SYMTYPE, b"", "/etc/passwd"),
        ])
        r = _run(kbs, "alpha", "restore", "--dir", str(d), "--date", "2025-05-13")
        assert r.exit_code == 0, r.output
        assert "Would restore 1 files" in r.output and "'wiki/link.md': link" in r.output
        assert kbs["alpha"].download("kbs/alpha/wiki/page.md") == "# alpha page"


def _named_archive(d, kb_label, stamp, manifest_name):
    """An archive file named for ``kb_label`` whose manifest records ``manifest_name``."""
    path = d / f"{kb_label}-wiki-{stamp}-abcdef.tar.gz"
    body = json.dumps({"format": 1, "kb": {"name": manifest_name, "prefix": "x/"}}).encode()
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(B.MANIFEST_NAME)
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return path


class TestRetentionIdentity:
    """A file system that does not keep case shows `Alpha`'s archives under `alpha`'s
    name; retention must still prune only the KB's own archives."""

    def test_lookalike_archives_of_another_kb_are_never_pruned(self, tmp_path):
        d = tmp_path
        other = [_named_archive(d, "alpha", f"2026-01-0{i}-000000", "Alpha") for i in (1, 2)]
        own = [_named_archive(d, "alpha", f"2026-02-0{i}-000000", "alpha") for i in (1, 2, 3)]
        removed, foreign = B.prune(d, "alpha", keep=2)
        assert removed == [own[0]]
        assert sorted(foreign) == sorted(other)
        assert all(p.exists() for p in other + own[1:])
        # The other KB's retention leaves alpha's archives alone the same way.
        assert B.prune(d, "Alpha", keep=1)[0] == []

    def test_own_archives_still_count_exactly_when_mixed(self, tmp_path):
        d = tmp_path
        own_old = _named_archive(d, "alpha", "2026-01-01-000000", "alpha")
        other_new = _named_archive(d, "alpha", "2026-03-01-000000", "Alpha")
        own_new = _named_archive(d, "alpha", "2026-02-01-000000", "alpha")
        removed, foreign = B.prune(d, "alpha", keep=2)
        assert removed == [] and foreign == [other_new]
        assert own_old.exists() and own_new.exists()

    def test_unreadable_archive_is_kept(self, tmp_path):
        broken = tmp_path / "alpha-wiki-2026-01-01-000000-abcdef.tar.gz"
        broken.write_bytes(b"not a gzip file")
        _named_archive(tmp_path, "alpha", "2026-02-01-000000", "alpha")
        removed, foreign = B.prune(tmp_path, "alpha", keep=1)
        assert removed == [] and foreign == [broken] and broken.exists()

    def test_backup_reports_kept_lookalikes(self, kbs, tmp_path):
        d = tmp_path / "bk"
        d.mkdir()
        other = _named_archive(d, "alpha", "2020-01-01-000000", "Alpha")
        r = _run(kbs, "alpha", "backup", "--dir", str(d), "--keep", "1")
        assert r.exit_code == 0, r.output
        assert other.exists() and "manifest names another KB" in r.output
        assert len(B.kb_archives(d, "alpha")) == 2

    def test_new_archives_start_with_an_identity_manifest(self, kbs, tmp_path):
        d = tmp_path / "bk"
        _run(kbs, "alpha", "backup", "--dir", str(d))
        (a,) = B.kb_archives(d, "alpha")
        with tarfile.open(a.path, "r:gz") as tar:
            first = tar.next()
            assert first.name == B.MANIFEST_NAME
            assert json.loads(tar.extractfile(first).read())["kb"]["name"] == "alpha"
            # Lookup by name still returns the full manifest written last.
            assert B.read_manifest(tar)["files"] == 3
        assert B.archive_kb_name(a.path) == "alpha"

    def test_manifest_only_at_the_end_is_still_read(self, tmp_path):
        path = tmp_path / "alpha-wiki-2026-01-01-000000-abcdef.tar.gz"
        body = json.dumps({"kb": {"name": "alpha"}}).encode()
        with tarfile.open(path, "w:gz") as tar:
            for name, data in (("wiki/page.md", b"x"), (B.MANIFEST_NAME, body)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        assert B.archive_kb_name(path) == "alpha"
