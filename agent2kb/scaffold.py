"""Project-rules scaffolding (2026-05-14: ``a2kb scaffold rules``).

A canonical Knowledge-Base Query Discipline section that can be
stamped into any project's ``PROJECT.md`` (or ``AGENTS.md`` /
``CLAUDE.md``) idempotently. The block is delimited by HTML-comment
markers so subsequent versions can replace it precisely:

    <!-- kb-scaffold: project-rules start v0.9.0 -->
    ## Knowledge-Base Query Discipline (agent2kb 0.9.0)
    ...content...
    <!-- kb-scaffold: project-rules end -->

Three modes:
  * ``insert``  : add the block once (refuse if a marker block already
                  exists — keeps idempotency honest).
  * ``upgrade`` : find an existing marker block, replace its body with
                  the current template. Use after a agent2kb upgrade.
  * ``force``   : same as upgrade but falls back to a heading match
                  (``## Knowledge-Base Query Discipline``) for
                  first-time migration from a hand-written section.

Rationale for the markered approach: hand-written rule sections drift
across projects as the agent2kb surface evolves (actor flags today,
typed-graph tomorrow). Markers let the operator run one upgrade
command per project instead of merging diffs by hand.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from loguru import logger

# Bumped whenever the template content changes in a way operators
# should know about. Older blocks still upgrade cleanly — the version
# is informational, not a compatibility gate. Tracks the agent2kb minor
# version that shipped the template so a quick ``grep "v"`` on
# PROJECT.md tells you whether a profile is on the current rules.
TEMPLATE_VERSION = "0.3.6"

_TEMPLATE_PATH = Path(__file__).parent / "scaffold_templates" / "project_rules.md"

_START_RE = re.compile(
    r"<!--\s*kb-scaffold:\s*project-rules\s+start\s+v(\S+?)\s*-->"
)
_END_RE = re.compile(r"<!--\s*kb-scaffold:\s*project-rules\s+end\s*-->")

# Heading match for legacy hand-written sections (no marker yet).
# Tolerant of the trailing parenthetical version note we used in
# pre-scaffold revisions ("(MANDATORY for chat replies)",
# "(REQUIRED for chat replies)", "(agent2kb 0.9.0)", …).
_HEADING_RE = re.compile(
    r"^##\s+Knowledge-Base\s+Query\s+Discipline.*$",
    re.MULTILINE,
)


def render(profile_name: str) -> str:
    """Materialize the template for ``profile_name`` and the current version."""
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    text = text.replace("{{profile}}", profile_name)
    text = text.replace("{{version}}", TEMPLATE_VERSION)
    return text


def find_marker_block(content: str) -> Optional[tuple[int, int, str]]:
    """Return ``(start, end, prior_version)`` for an existing marker block.

    The start/end indices wrap the comments themselves so a replacement
    can simply slice and concatenate.
    """
    m_start = _START_RE.search(content)
    if not m_start:
        return None
    m_end = _END_RE.search(content, m_start.end())
    if not m_end:
        return None
    return (m_start.start(), m_end.end(), m_start.group(1))


def find_heading_block(content: str) -> Optional[tuple[int, int]]:
    """Locate a legacy hand-written ``## Knowledge-Base Query Discipline`` section.

    Returns ``(start, end)`` where end is the offset of the next ``##``
    heading or end-of-file. Used by ``--force`` to migrate pre-scaffold
    rule sections into the markered form.
    """
    m = _HEADING_RE.search(content)
    if not m:
        return None
    start = m.start()
    next_h = re.search(r"^##\s", content[m.end():], re.MULTILINE)
    end = m.end() + next_h.start() if next_h else len(content)
    return (start, end)


def _insert_before_decisions_log(content: str, block: str) -> str:
    """Append the block right before ``## Decisions Log`` (or at EOF).

    Spacing invariant: the block carries its own trailing newline, so we
    never add an extra one. This keeps ``insert`` followed by
    ``upgrade`` idempotent — both paths produce the same shape:
    ``<preamble>\\n\\n<block ending in \\n>``.
    """
    if not block.endswith("\n"):
        block = block + "\n"
    m = re.search(r"^##\s+Decisions Log", content, re.MULTILINE)
    if m:
        before = content[: m.start()].rstrip() + "\n\n"
        after = content[m.start():]
        return before + block + "\n" + after
    base = content.rstrip()
    if base:
        base += "\n\n"
    return base + block


def upsert(
    target_path: Path,
    profile_name: str,
    *,
    mode: str = "insert",
    dry_run: bool = False,
) -> dict:
    """Insert / upgrade / force-migrate the rule block.

    Returns a dict ``{"action": str, "target": str, "new_content": str}``.
    For dry-runs, ``action`` is prefixed with ``DRY-RUN: ``.

    Refusal conditions are intentional:
      * ``insert`` + existing marker block → raise (use ``--upgrade``).
      * ``upgrade`` + no marker block → raise (use ``--force`` for
        migration from a pre-scaffold hand-written section).

    The ``force`` mode is the migration tool: it locates an existing
    block by either marker or heading, and replaces it with the current
    template. After a ``--force`` pass, subsequent runs can use the
    cheaper ``--upgrade`` flag.
    """
    if mode not in {"insert", "upgrade", "force"}:
        raise ValueError(f"mode must be insert|upgrade|force, got {mode!r}")

    rendered = render(profile_name)
    content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""

    existing = find_marker_block(content)

    if mode == "insert" and existing:
        raise RuntimeError(
            f"{target_path}: marker block already exists "
            f"(v{existing[2]}). Use --upgrade to refresh, or --force "
            f"to migrate a non-markered hand-written section."
        )

    if mode == "upgrade" and not existing:
        raise RuntimeError(
            f"{target_path}: no marker block found. Use --force for "
            f"first-time migration from a hand-written section, or "
            f"--insert if the file genuinely has no rule section yet."
        )

    if existing and mode in {"upgrade", "force"}:
        s, e, prev_version = existing
        # Splice spacing: the rendered template carries its own trailing
        # newline. If the tail already starts with one (it normally does
        # — it's the newline that previously terminated the block), drop
        # exactly one so we don't accumulate an extra blank line every
        # time --upgrade runs.
        tail = content[e:]
        if tail.startswith("\n"):
            tail = tail[1:]
        new_content = content[:s] + rendered + tail
        action = (
            f"upgrade v{prev_version} → v{TEMPLATE_VERSION}"
            if prev_version != TEMPLATE_VERSION
            else f"re-render (already at v{TEMPLATE_VERSION})"
        )
    elif mode == "force":
        heading = find_heading_block(content)
        if heading:
            s, e = heading
            # Re-establish standard section spacing around the
            # replacement: blank line before, blank line after. Same
            # invariant as ``_insert_before_decisions_log`` so a
            # subsequent ``--upgrade`` is a true no-op.
            head = content[:s].rstrip() + "\n\n"
            tail = content[e:].lstrip("\n")
            block = rendered if rendered.endswith("\n") else rendered + "\n"
            sep = "\n" if tail else ""
            new_content = head + block + sep + tail
            action = f"force-replace hand-written section → v{TEMPLATE_VERSION}"
        else:
            new_content = _insert_before_decisions_log(content, rendered)
            action = f"insert v{TEMPLATE_VERSION} (no prior section found)"
    else:
        new_content = _insert_before_decisions_log(content, rendered)
        action = f"insert v{TEMPLATE_VERSION}"

    if dry_run:
        return {
            "action": f"DRY-RUN: would {action}",
            "target": str(target_path),
            "new_content": new_content,
        }

    target_path.write_text(new_content, encoding="utf-8")
    logger.info(f"scaffold rules: {action} → {target_path}")
    return {
        "action": action,
        "target": str(target_path),
        "new_content": new_content,
    }
