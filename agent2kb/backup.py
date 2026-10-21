"""Local archives of a KB (`a2kb backup` / `a2kb restore`).

Archive names carry the KB name, a second-resolution UTC stamp and a random suffix:
``<kb>-wiki-YYYY-MM-DD-HHMMSS-<hex6>.tar.gz``. Two KBs, or two runs of one KB, sharing a
directory therefore never overwrite each other, and retention only ever counts and prunes
the archives of the KB that is running. The archive also holds a manifest member that
records which KB it came from, so a restore can refuse to write one KB's pages into another,
and retention checks it before deleting anything.

Archives written before this format (``wiki-YYYY-MM-DD-HHMM.tar.gz``, no manifest) say
nothing about their KB. They still restore, with a warning, and retention never deletes
them: in a shared directory they may belong to any KB.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import secrets
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent2kb import __version__

#: Name of the manifest member inside an archive. It is never restored to S3.
MANIFEST_NAME = "agent2kb-backup.json"
MANIFEST_FORMAT = 1

DIR_MODE = 0o700
FILE_MODE = 0o600

_LEGACY_RE = re.compile(r"^wiki-(\d{4}-\d{2}-\d{2}-\d{4})\.tar\.gz$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def file_label(kb_name: str) -> str:
    """The KB name as it appears in archive file names (path-safe)."""
    return _UNSAFE_CHARS.sub("_", kb_name) or "kb"


def _archive_re(kb_name: str) -> re.Pattern[str]:
    # The stamp is all digits, so `alpha` never matches `alpha-wiki-...` archives of a
    # KB named `alpha-wiki`: after `alpha-wiki-` this pattern needs a digit.
    return re.compile(
        rf"^{re.escape(file_label(kb_name))}-wiki-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}})"
        r"-[0-9a-f]{6}\.tar\.gz$"
    )


@dataclass(frozen=True)
class Archive:
    path: Path
    stamp: str  # YYYY-MM-DD-HHMMSS (legacy archives are padded with "00")
    legacy: bool


def kb_archives(out_dir: Path, kb_name: str) -> list[Archive]:
    """New-format archives of one KB in ``out_dir``, oldest first."""
    pattern = _archive_re(kb_name)
    found = []
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            m = pattern.match(p.name)
            if m and p.is_file():
                found.append(Archive(p, m.group(1), legacy=False))
    return sorted(found, key=lambda a: (a.stamp, a.path.name))


def legacy_archives(out_dir: Path) -> list[Archive]:
    """Old-format ``wiki-YYYY-MM-DD-HHMM.tar.gz`` archives in ``out_dir``, oldest first."""
    found = []
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            m = _LEGACY_RE.match(p.name)
            if m and p.is_file():
                found.append(Archive(p, m.group(1) + "00", legacy=True))
    return sorted(found, key=lambda a: (a.stamp, a.path.name))


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) as 0700, whatever the umask.

    A directory that already exists is left as it is: it may be shared on purpose, and
    the archives inside are 0600 regardless.
    """
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in missing:
        os.chmod(created, DIR_MODE)


def _new_archive_path(out_dir: Path, kb_name: str, now: datetime.datetime) -> Path:
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    while True:
        path = out_dir / f"{file_label(kb_name)}-wiki-{stamp}-{secrets.token_hex(3)}.tar.gz"
        if not path.exists():
            return path


def identity(profile) -> dict:
    return {"name": profile.name, "bucket": profile.bucket, "prefix": profile.prefix}


def write_archive(s3, profile, out_dir: Path, targets: list[str], include_raw: bool):
    """Archive ``targets`` (sub-prefixes) of the KB into a new file in ``out_dir``.

    The archive is built in a 0600 temporary file in the same directory and renamed into
    place only once it is complete, so an interrupted run never leaves a truncated
    archive under a real archive name. Returns ``(path, file_count, total_bytes)``.
    """
    ensure_private_dir(out_dir)
    now = datetime.datetime.now(datetime.timezone.utc)
    counts: dict[str, int] = {}
    total_bytes = 0
    file_count = 0

    fd, tmp_name = tempfile.mkstemp(prefix=".a2kb-backup-", suffix=".tmp", dir=out_dir)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            mtime = int(now.timestamp())
            # A short identity-only manifest first, so retention can tell whose archive
            # this is without decompressing all of it. The full one below comes last
            # and wins for readers that look the member up by name.
            _add_json(tar, MANIFEST_NAME, {
                "format": MANIFEST_FORMAT,
                "kb": identity(profile),
                "created_at": now.isoformat(timespec="seconds"),
            }, mtime)
            for sub_prefix in targets:
                counts[sub_prefix] = 0
                for key in s3.list_keys(sub_prefix=sub_prefix):
                    rel = key[len(profile.prefix):] if key.startswith(profile.prefix) else key
                    if rel.endswith("/") or rel.endswith(".gitkeep"):
                        continue
                    data = s3.download_bytes(key)
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(data)
                    info.mtime = mtime
                    tar.addfile(info, io.BytesIO(data))
                    total_bytes += len(data)
                    file_count += 1
                    counts[sub_prefix] += 1
            # Written last: its presence also says the listing above ran to completion.
            manifest = {
                "format": MANIFEST_FORMAT,
                "kb": identity(profile),
                "created_at": now.isoformat(timespec="seconds"),
                "agent2kb_version": __version__,
                "include_raw": include_raw,
                "counts": counts,
                "files": file_count,
                "bytes": total_bytes,
            }
            _add_json(tar, MANIFEST_NAME, manifest, mtime)
            fh.flush()
            os.fsync(fh.fileno())
        path = _new_archive_path(out_dir, profile.name, now)
        os.replace(tmp, path)
        os.chmod(path, FILE_MODE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path, file_count, total_bytes


def archive_kb_name(path: Path) -> Optional[str]:
    """The KB name recorded in an archive's manifest, or None when it has none or the
    archive cannot be read.

    Reads only the first member when that is the identity manifest; archives written
    before it was added have their manifest at the end and are read in full.
    """
    try:
        with tarfile.open(path, "r:gz") as tar:
            first = tar.next()
            manifest = None
            if first is not None and first.name == MANIFEST_NAME and first.isreg():
                manifest = _json_member(tar, first)
            if manifest is None:
                manifest = read_manifest(tar)
    except (OSError, tarfile.TarError, EOFError, zlib.error):
        return None
    kb = (manifest or {}).get("kb")
    name = kb.get("name") if isinstance(kb, dict) else None
    return name if isinstance(name, str) else None


def _add_json(tar: tarfile.TarFile, name: str, data: dict, mtime: int) -> None:
    body = json.dumps(data, indent=2, sort_keys=True).encode()
    info = tarfile.TarInfo(name=name)
    info.size = len(body)
    info.mtime = mtime
    tar.addfile(info, io.BytesIO(body))


def prune(out_dir: Path, kb_name: str, keep: int) -> tuple[list[Path], list[Path]]:
    """Delete this KB's oldest new-format archives beyond ``keep``. Legacy ones are kept.

    Returns ``(removed, foreign)``: ``foreign`` are archives whose file name matched but
    whose manifest names another KB (or that cannot be read); they are never deleted and
    do not count towards ``keep``. File names alone are not proof: KB names that differ
    only by case (``Alpha``, ``alpha``) look alike on a file system that does not keep
    case, and legacy profile names can map to the same path-safe label (``a b``, ``a_b``).
    Every candidate is therefore checked against its manifest (cheap: new archives carry
    an identity manifest as their first member). ``keep`` of 0 or less prunes nothing,
    as before.
    """
    named = kb_archives(out_dir, kb_name)
    if keep <= 0 or len(named) <= keep:
        return [], []
    own: list[Archive] = []
    foreign: list[Path] = []
    for a in named:
        if archive_kb_name(a.path) == kb_name:
            own.append(a)
        else:
            foreign.append(a.path)
    removed = []
    for old in own[: max(len(own) - keep, 0)]:
        old.path.unlink()
        removed.append(old.path)
    return removed, foreign


_DATE_RE = re.compile(r"^[0-9][0-9-]*$")


def valid_date(date: str) -> bool:
    return bool(_DATE_RE.match(date))


def find_archive(out_dir: Path, kb_name: str, date: str) -> Optional[Archive]:
    """Newest archive of ``kb_name`` (or a legacy one) whose stamp starts with ``date``.

    ``date`` is ``YYYY-MM-DD``, an old ``YYYY-MM-DD-HHMM`` stamp or a full new stamp.
    An exact legacy name (``wiki-<date>.tar.gz``) is preferred, as before.
    """
    for a in legacy_archives(out_dir):
        if a.path.name == f"wiki-{date}.tar.gz":
            return a
    matches = [a for a in kb_archives(out_dir, kb_name) if a.stamp.startswith(date)]
    matches += [a for a in legacy_archives(out_dir) if a.stamp.startswith(date)]
    if not matches:
        return None
    return max(matches, key=lambda a: (a.stamp, not a.legacy, a.path.name))


def read_manifest(tar: tarfile.TarFile) -> Optional[dict]:
    """The archive's manifest, or None for an old archive that has none."""
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError:
        return None
    return _json_member(tar, member)


def _json_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> Optional[dict]:
    f = tar.extractfile(member)
    if f is None:
        return None
    try:
        data = json.loads(f.read())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


#: The areas ``a2kb backup`` archives. A restore writes nothing outside them.
RESTORABLE_AREAS = ("raw/", "wiki/", "outputs/")

#: Restore reads each member into memory to upload it. Ingest caps extracted text at 5
#: million characters, so no file `a2kb backup` writes comes near these; a larger member
#: (or archive) is damaged or crafted.
MAX_RESTORE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_RESTORE_TOTAL_BYTES = 16 * 1024 * 1024 * 1024

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def member_problem(member: tarfile.TarInfo) -> Optional[str]:
    """Why ``member`` must not be restored, or None when it is a safe file.

    ``a2kb backup`` only writes regular files named ``<area>/<relative key>``. Anything
    else came from a damaged or crafted archive: links and devices have no meaning as S3
    objects, and an absolute path, a ``..`` or ``.`` segment, a backslash or a name outside
    the archived areas could aim the upload at another key than the one shown. The S3
    client still enforces the KB prefix; this check runs first, so such members are
    reported and skipped instead of failing (or landing) halfway through a restore.
    """
    name = member.name
    if not member.isreg():
        if member.issym() or member.islnk():
            return "link"
        return "not a regular file"
    if not name:
        return "empty name"
    if name.startswith("/"):
        return "absolute path"
    if "\\" in name:
        return "backslash in name"
    if _CONTROL_CHARS.search(name):
        return "control character in name"
    segments = name.split("/")
    if ".." in segments:
        return "'..' segment"
    if any(seg in ("", ".") for seg in segments):
        return "empty or '.' segment"
    if not name.startswith(RESTORABLE_AREAS):
        return "outside raw/, wiki/ and outputs/"
    if member.size > MAX_RESTORE_MEMBER_BYTES:
        return f"larger than {MAX_RESTORE_MEMBER_BYTES // (1024 * 1024)} MiB"
    return None


def identity_mismatch(manifest: dict, profile) -> list[str]:
    """Fields (name, prefix) in which the archive's KB differs from ``profile``.

    The bucket is not compared: restoring into a new bucket after losing the old one is
    what the archive is for. An unreadable manifest counts as a mismatch.
    """
    kb = manifest.get("kb")
    if not isinstance(kb, dict):
        return ["name", "prefix"]
    mine = identity(profile)
    return [f for f in ("name", "prefix") if kb.get(f) != mine[f]]
