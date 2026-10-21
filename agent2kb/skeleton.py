"""Create a KB's S3 skeleton: raw/, wiki/, outputs/, index.md, log.md, concepts/.

Shared by ``a2kb init`` and ``a2kb new``. Idempotent: existing objects are left alone.
"""

from __future__ import annotations

import datetime

from agent2kb.models import ProfileConfig
from agent2kb.s3_client import S3Client


def create_skeleton(profile: ProfileConfig, s3: S3Client, console) -> list[str]:
    """Create what is missing under ``profile.prefix``; returns the labels created."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # S3 has no real folders — create placeholder .gitkeep files
    raw_key = f"{profile.prefix}raw/.gitkeep"
    wiki_key = f"{profile.prefix}wiki/.gitkeep"
    outputs_key = f"{profile.prefix}outputs/.gitkeep"

    created = []
    for key, label in [(raw_key, "raw/"), (wiki_key, "wiki/"), (outputs_key, "outputs/")]:
        if not s3.exists(key):
            s3.upload(key, "", content_type="text/plain")
            created.append(label)
            console.print(f"  [green]✓[/green] Created {label}")
        else:
            console.print(f"  [dim]─[/dim] {label} already exists")

    # Create index.md — the content-oriented catalog (Karpathy pattern)
    index_key = f"{profile.prefix}wiki/index.md"
    if not s3.exists(index_key):
        s3.upload(index_key, build_index_md(profile.name, today))
        created.append("wiki/index.md")
        console.print("  [green]✓[/green] Created wiki/index.md")

    # Create log.md — append-only chronological record (Karpathy pattern)
    log_key = f"{profile.prefix}wiki/log.md"
    if not s3.exists(log_key):
        s3.upload(log_key, build_log_md(profile.name, today))
        created.append("wiki/log.md")
        console.print("  [green]✓[/green] Created wiki/log.md")

    # Create wiki/concepts/ placeholder
    concepts_key = f"{profile.prefix}wiki/concepts/.gitkeep"
    if not s3.exists(concepts_key):
        s3.upload(concepts_key, "", content_type="text/plain")
        console.print("  [green]✓[/green] Created wiki/concepts/")

    return created


def build_index_md(name: str, date: str) -> str:
    """Build the wiki index.md following Karpathy's LLM Wiki pattern."""
    return f"""---
title: "{name} Knowledge Base"
date: "{date}"
tags: [index]
---

# {name} Knowledge Base

> Following the [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern by Andrej Karpathy.
> Raw sources are compiled into structured, interlinked wiki pages.

## Categories

_Categories will appear here as content is compiled._

## Recent Pages

_Recent wiki pages will be listed here._

## How This Works

1. **Ingest** — Add raw sources (URLs, documents, videos, audio)
2. **Compile** — LLM transforms raw content into structured wiki pages with `[[wikilinks]]`
3. **Query** — Search and ask questions across the knowledge base
4. **Lint** — Check for broken links, missing metadata, contradictions
"""


def build_log_md(name: str, date: str) -> str:
    """Build the append-only log.md following Karpathy's pattern."""
    return f"""---
title: "{name} Log"
date: "{date}"
tags: [log, meta]
---

# {name} — Activity Log

> Append-only chronological record of ingests, compiles, queries, and lint passes.

## {date}

- Project initialised
"""
