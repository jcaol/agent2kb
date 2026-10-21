"""Auto-generate stub concept pages for ghost wikilink targets.

Why this exists
---------------
agent2kb's lint policy intentionally tolerates orphan wikilinks as
Karpathy-style ghost-node TODOs (visible "concepts the system thinks
should exist but haven't been compiled yet"). Undecorated, those links
look identical to real ones — clicking ``[[Atomic]]`` in Obsidian's
reading view drops the user on a "Create new page" prompt, which is
misleading for read-only KB consumption and especially for non-author
viewers of a shared vault.

Server-side fix that needs zero client setup: when a wiki page or
query output references ``[[Foo]]`` and ``concepts/foo.md`` doesn't
exist, write a real stub page at that slug whose body is a clearly-
marked placeholder listing the pages that mentioned the concept.
Click → reader lands on a self-explanatory stub instead of an empty
create-prompt. The Karpathy compounding loop is preserved (the stubs
are still graph-visible and now have a breadcrumb of where the concept
was first surfaced) and the next ``a2kb compile`` pass can use the
referrer list to flesh the stub out.

Conservative filters keep stub-clutter bounded:

* skip blacklisted acronyms (e.g. don't create ``concepts/json.md``)
* skip targets shorter than 3 chars or shaped like a URL
* skip targets that already resolve to an existing page (real or stub)
* never overwrite a non-stub page (no clobbering real concept content)
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from typing import Optional

from loguru import logger

from agent2kb.lint import (
    _is_blacklisted_acronym,
    _resolve_link,
    build_resolution_fast,
)
from agent2kb.models import Frontmatter, ProfileConfig, SourceKind
from agent2kb.s3_client import S3Client
from agent2kb.utils import extract_wikilinks, slugify

_MIN_TARGET_LEN = 3
_URL_HINTS = re.compile(r"(?:https?://|\.com\b|\.io\b|\.net\b|\.org\b|\.dev\b)", re.IGNORECASE)
_STUB_TAG = "stub"


def _should_create_stub(target: str) -> bool:
    """Filter out targets that aren't worth a stub page.

    The blacklist + length + URL guards mirror the auto-fix matcher's
    skip rules in lint.py — short tokens and acronyms have too many
    plausible expansions to be safe to materialize as concept pages.
    """
    t = target.strip()
    if len(t) < _MIN_TARGET_LEN:
        return False
    if _is_blacklisted_acronym(t):
        return False
    if _URL_HINTS.search(t):
        return False
    return True


def _stub_slug(target: str) -> str:
    """Canonicalize a broken target into a slug for the stub filename.

    Mirrors the bilingual-aware resolution candidates in ``_resolve_link``
    so the slug a stub gets created at is the same slug the resolver
    would *look for* the next time the link is rendered. Without this
    parity, a stub would get filed under one slug while the link still
    appears broken because the resolver tries a different one.
    """
    t = re.sub(r"\s*/\s*", "-", target.strip())
    return slugify(t).lower()


def _format_stub_body(title: str, referrers: set[str]) -> str:
    """Build the stub body — placeholder warning + back-links to referrers.

    The referrer list does double-duty: it tells a human reader who
    landed on the stub *why* this concept was deemed worth a page
    (which articles invoked it), and it gives Obsidian's graph view
    real edges back to the citing pages so the stub isn't a leaf node.
    """
    lines = [
        f"# {title}",
        "",
        (
            "> **Stub** — this concept page is an auto-generated placeholder. "
            "The full content has not been compiled yet. A future `a2kb compile` "
            "pass (or a human reviewer) can use the referrer list below as a "
            "starting point."
        ),
        "",
        "## Mentioned by",
        "",
    ]
    for ref in sorted(referrers):
        slug = ref.split("/")[-1].replace(".md", "")
        lines.append(f"- [[{slug}]]")
    return "\n".join(lines) + "\n"


class StubGenerator:
    """Create or refresh stub concept pages for ghost wikilink targets."""

    def __init__(self, profile: ProfileConfig, s3: S3Client):
        self.profile = profile
        self.s3 = s3

    def generate(
        self, dry_run: bool = False, min_referrers: int = 5
    ) -> dict[str, int]:
        """Scan wiki + outputs for broken wikilinks, create/refresh stubs.

        ``min_referrers`` is the floor for how many distinct pages must
        mention a ghost target before it earns a stub. Pre-Track-A/B
        compile output produced thousands of one-off LLM hallucinations
        like ``[[Atomic]]`` mentioned in a single article — materializing
        all of those as concept pages would explode wiki/ by 6× and bury
        real signal in noise. Default 5 mirrors the Roam/Logseq
        "unlinked references" heuristic: a concept worth a page is one
        the system has surfaced from multiple independent contexts.

        Returns ``{candidates, created, refreshed, skipped, low_refs, errors}``.

        * ``candidates`` — distinct broken targets discovered.
        * ``created``    — new stub pages written.
        * ``refreshed``  — existing stubs whose referrer list was updated.
        * ``skipped``    — candidates filtered out (acronym, too-short,
                           URL-shaped, or slug collides with a real page).
        * ``low_refs``   — candidates dropped for failing the
                           ``min_referrers`` floor.
        * ``errors``     — pages that failed to read or stubs that failed
                           to write — logged individually via loguru.
        """
        resolution, _ = build_resolution_fast(self.s3)
        wiki_keys = self.s3.list_wiki_files()
        output_keys = self.s3.list_output_files()

        broken: dict[str, set[str]] = defaultdict(set)
        errors = 0
        for key in wiki_keys + output_keys:
            try:
                body = self.s3.download(key)
            except Exception as e:
                logger.warning(f"stubs: cannot read {key}: {e}")
                errors += 1
                continue
            for raw in extract_wikilinks(body):
                target = raw.split("|", 1)[0].split("#", 1)[0].strip()
                if not target:
                    continue
                if _resolve_link(target, resolution):
                    continue
                broken[target].add(key)

        # Slug-merge pass — multiple display forms (``workflow`` and
        # ``Workflow``, or ``Open Source`` and ``open-source``) collapse
        # to the same canonical slug, so they should produce one merged
        # stub with the union of referrers. Without this the iteration
        # below would write each variant in turn and the last one wins,
        # silently dropping referrers from earlier variants.
        merged: dict[str, dict] = {}  # slug -> {title, referrers}
        for target, referrers in broken.items():
            slug = _stub_slug(target)
            if not slug:
                continue
            entry = merged.setdefault(slug, {"title": target, "referrers": set()})
            entry["referrers"] |= referrers
            # Keep the title with the most mentions — proxy for the
            # variant the LLM/author considers canonical.
            if len(referrers) > len(broken.get(entry["title"], set())):
                entry["title"] = target

        stats = {
            "candidates": len(merged),
            "created": 0,
            "refreshed": 0,
            "skipped": 0,
            "low_refs": 0,
            "errors": errors,
        }
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        # Process highest-mention ghosts first so a manual run that gets
        # interrupted at least covers the most-referenced concepts.
        ordered = sorted(
            merged.items(), key=lambda kv: -len(kv[1]["referrers"])
        )
        for slug, entry in ordered:
            target = entry["title"]
            referrers = entry["referrers"]
            if len(referrers) < min_referrers:
                # Sorted highest-first — once we cross the threshold,
                # every remaining target also fails it. Tally rest, break.
                stats["low_refs"] = sum(
                    1 for e in merged.values()
                    if len(e["referrers"]) < min_referrers
                )
                break
            if not _should_create_stub(target):
                stats["skipped"] += 1
                continue
            stub_key = f"{self.profile.prefix}wiki/concepts/{slug}.md"

            existing_is_stub = self._classify_existing(stub_key)
            if existing_is_stub is None:
                # File exists but is NOT a stub — leave the real page alone.
                stats["skipped"] += 1
                continue

            fm = Frontmatter(
                title=target,
                date=today,
                tags=[_STUB_TAG, "ghost"],
                source="kb stubs (auto-generated placeholder)",
                source_kind=SourceKind.STUB,
                compiled=False,
            )
            content = fm.to_yaml_block() + "\n" + _format_stub_body(target, referrers)

            action = "refresh" if existing_is_stub else "create"
            if dry_run:
                logger.info(
                    f"stubs (dry-run) would {action} {stub_key} "
                    f"({len(referrers)} referrer{'s' if len(referrers) != 1 else ''})"
                )
            else:
                try:
                    self.s3.upload(stub_key, content)
                    logger.info(
                        f"stubs: {action}d {stub_key} "
                        f"({len(referrers)} referrer{'s' if len(referrers) != 1 else ''})"
                    )
                except Exception as e:
                    logger.error(f"stubs: failed to write {stub_key}: {e}")
                    stats["errors"] += 1
                    continue

            if existing_is_stub:
                stats["refreshed"] += 1
            else:
                stats["created"] += 1

        return stats

    def _classify_existing(self, stub_key: str) -> Optional[bool]:
        """Probe whether a key already exists at the target stub path.

        Returns:
          * ``True``  — a stub page lives here (safe to overwrite/refresh).
          * ``False`` — nothing lives here (safe to create).
          * ``None``  — a non-stub real page lives here (DO NOT TOUCH).

        Encoding the three states into one return keeps callers from
        racing the existence check against the tag check.
        """
        try:
            existing = self.s3.download(stub_key)
        except Exception:
            return False  # Doesn't exist — create case.
        try:
            fm, _ = Frontmatter.from_yaml_block(existing)
        except Exception:
            # Couldn't parse frontmatter — assume real content, don't clobber.
            return None
        if _STUB_TAG in (fm.tags or []):
            return True
        return None
