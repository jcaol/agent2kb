"""Promote orphan markdown files into the KB.

Promote takes a local markdown file (e.g. one synthesized by an agent in
its worktree, or a hand-edited reference) and pushes it into the right
S3 location:

- ``--as outputs``: drops it into ``outputs/<timestamp>-<slug>-<id>.md``. The
  next compile run will fold it into relevant wiki pages via the
  Karpathy compounding loop. Use for one-shot answers, query results,
  or transient reference material that should fade as the wiki absorbs it.

- ``--as weekly``: drops a digest into ``outputs/weekly/<timestamp>-<slug>-<id>.md``
  with ``source_kind: digest`` and ``source_meta.period: weekly``. Use for
  periodic consolidation pages (insight-only, not detailed summaries)
  produced on a weekly cadence (by a person or an agent). Digests
  don't feed the compounding loop — they're terminal artefacts.

- ``--as wiki-topic``: drops it into ``wiki/topics/<slug>.md`` with a
  ``pinned: true`` frontmatter flag. Compile will not regenerate it
  (topics have no raw counterpart so compile_all skips them naturally),
  and lint will treat it as a first-class wiki page for wikilink
  resolution. Use for permanent consolidated references / cheatsheets /
  "from-zero learning guides" that should keep their hand-crafted
  structure.

All modes preserve the source file's existing frontmatter where
present, supplementing missing fields rather than overwriting.
"""

from __future__ import annotations

import datetime
import secrets
from pathlib import Path
from typing import Literal, Optional

import yaml
from loguru import logger

from agent2kb.models import Frontmatter, ProfileConfig, SourceKind
from agent2kb.s3_client import PreconditionFailed, S3Client
from agent2kb.utils import extract_wikilinks, slugify

PromoteMode = Literal["outputs", "wiki-topic", "weekly"]

#: New keys tried before giving up when If-None-Match keeps finding the key taken.
_CREATE_ATTEMPTS = 3


def _upload_new_output(s3: S3Client, key_base: str, text: str) -> str:
    """Write ``<key_base>-<8 hex>.md`` without replacing an existing object.

    The timestamp in ``key_base`` has one-second resolution, so two promotes
    of files with the same slug within a second used to share a key and the
    second silently replaced the first. Same scheme as query captures.
    """
    for attempt in range(_CREATE_ATTEMPTS):
        dest_key = f"{key_base}-{secrets.token_hex(4)}.md"
        try:
            s3.upload(dest_key, text, if_none_match=True)
            return dest_key
        except PreconditionFailed:
            if _holds(s3, dest_key, text):
                return dest_key  # our write committed; the SDK's retry got 412
            if attempt == _CREATE_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def _holds(s3: S3Client, key: str, text: str) -> bool:
    try:
        return s3.download(key) == text
    except Exception:
        return False


def _derive_slug(path: Path, fm: Frontmatter, override: str | None) -> str:
    """Pick a slug for the destination filename.

    Preference order:
      1. CLI --slug override.
      2. Frontmatter ``title`` field, slugified.
      3. The source filename without extension.
    """
    if override:
        return slugify(override, max_length=80)
    if fm.title:
        return slugify(fm.title, max_length=80)
    return slugify(path.stem, max_length=80) or "untitled"


def _extract_h1(body: str) -> str:
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def promote(
    profile: ProfileConfig,
    s3: S3Client,
    file_path: Path,
    mode: PromoteMode = "outputs",
    slug_override: str | None = None,
    delete_source: bool = False,
    actor: Optional[str] = None,
) -> str:
    """Promote a local markdown file to the KB. Returns the destination S3 key.

    ``actor`` ("user" / "agent") is recorded in ``source_meta`` for
    audit purposes. Caller-supplied value wins; falls back to ``None``
    (no record) — main.py's CLI wrapper resolves via the same
    ``_resolve_actor`` helper used by ``a2kb query``.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    raw_text = file_path.read_text(encoding="utf-8")
    fm, body = Frontmatter.from_yaml_block(raw_text)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if not fm.date:
        fm.date = today
    if not fm.title:
        fm.title = _extract_h1(body) or file_path.stem

    slug = _derive_slug(file_path, fm, slug_override)

    if mode == "outputs":
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        key_base = f"{profile.prefix}outputs/{timestamp}-{slug}"
        # Tags help downstream tools distinguish promoted material from
        # native query outputs. Leave existing tags intact.
        tags_set = set(fm.tags or [])
        tags_set.update({"output", "promote"})
        fm.tags = sorted(tags_set)
        if actor:
            fm.source_meta = {**(fm.source_meta or {}), "actor": actor}
        new_text = fm.to_yaml_block() + "\n" + body.lstrip("\n")

    elif mode == "weekly":
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        key_base = f"{profile.prefix}outputs/weekly/{timestamp}-{slug}"
        # Mark as digest in both source_kind and tags. The trust-level
        # mapping (agent2kb.models._TRUST_LEVEL) classifies digest as
        # "derived" — same as query_output — so compile prompts won't
        # treat digest claims as primary evidence.
        tags_set = set(fm.tags or [])
        tags_set.update({"digest", "weekly"})
        fm.tags = sorted(tags_set)
        fm.source_kind = SourceKind.DIGEST
        meta = dict(fm.source_meta or {})
        meta["period"] = "weekly"
        if actor:
            meta["actor"] = actor
        fm.source_meta = meta
        if not fm.source:
            fm.source = "kb digest"
        new_text = fm.to_yaml_block() + "\n" + body.lstrip("\n")

    elif mode == "wiki-topic":
        dest_key = f"{profile.prefix}wiki/topics/{slug}.md"
        # Provenance trail in frontmatter mirrors the inline [[wikilinks]]
        # so tooling can trace the source set without re-parsing the body.
        # Strip wikilink display aliases: "[[foo|Foo Bar]]" → "foo".
        sources = sorted({wl.split("|", 1)[0] for wl in extract_wikilinks(body)})

        # Bypass Frontmatter pydantic model so we can carry fields it
        # doesn't know about (pinned, sources, pin_source). The reader
        # side (compile/lint) reads these via raw YAML parsing.
        fm_dict = fm.model_dump(exclude_none=True, mode="json")
        tags_set = set(fm_dict.get("tags") or [])
        tags_set.update({"topic", "pinned"})
        fm_dict["tags"] = sorted(tags_set)
        fm_dict.update(
            pinned=True,
            pin_source="promote",
            promoted_date=today,
            sources=sources,
        )
        yaml_block = yaml.dump(
            fm_dict, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        new_text = f"---\n{yaml_block}---\n\n{body.lstrip(chr(10))}"

    else:
        raise ValueError(f"Unknown promote mode: {mode}")

    if mode == "wiki-topic":
        # A topic's key is its identity: promoting the same slug again
        # updates the page on purpose.
        s3.upload(dest_key, new_text)
    else:
        dest_key = _upload_new_output(s3, key_base, new_text)
    logger.info(f"Promoted {file_path} → {dest_key}")

    if delete_source:
        file_path.unlink()
        logger.info(f"Deleted source: {file_path}")

    return dest_key
