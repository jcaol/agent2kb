# Project workflow

How to run agent2kb day to day for a project KB. A personal KB works the same way: pass its
name to `-p`.

## KB setup

Each project gets its own KB, defined in your user-level `~/.kb/config.yaml` (v2). The
storage connection and the LLM are shared; the KB entry names the project:

```yaml
version: 2
storage:
  endpoint: http://s3.local:9000
  bucket: kb
  access_key: <key>
  secret_key: <secret>
llm:
  provider: gemini
  gemini_api_key: <key>
defaults:
  output_language: en
  allow_jina_reader: false
  allow_cloud_vision: false
kbs:
  alpha:                            # prefix is automatic: kbs/alpha/
    embedding_provider: ollama      # local: keeps embeddings out of the cloud
    privacy_mode: false
```

`a2kb setup` writes the first version of this file and `a2kb new alpha` adds a KB to it (it
also creates the S3 skeleton). If the storage provider can scope credentials to a prefix,
give the KB its own `storage: {access_key: ..., secret_key: ...}` so the storage itself
refuses access outside `kbs/alpha/`; agent2kb additionally refuses any operation outside the
KB's prefix. See [storage providers](storage-providers.md#isolation-between-kbs).

In the project folder, put a pointer so `a2kb` finds the KB without `-p`:

```bash
cd ~/work/alpha && a2kb new alpha --here    # or create .kb/config.yaml containing: kb: alpha
```

The pointer holds no secrets and can be committed. An **admin KB** (`role: admin`) can read
and write the whole bucket, including `_meta/`, and is the only kind allowed to run
`compile --all-profiles`. Do not use it for everyday work; see
[configuration](configuration.md#admin-kbs).

## Daily loop

```bash
# 1. Ingest. Detects the type: URL, PDF, video, audio, image, Markdown.
a2kb -p alpha ingest --url https://example.com/talk
a2kb -p alpha ingest --folder ~/inbox/alpha       # batch a folder
a2kb -p alpha ingest --file ./meeting-notes.md

# 2. Compile. Incremental by default (skips unchanged raw files).
a2kb -p alpha compile

# 3. Query. Saves to outputs/ unless --no-save.
a2kb -p alpha query "how does the X subsystem handle backpressure"

# 4. Periodically: rebuild the semantic index after a batch of new pages.
a2kb -p alpha embed
```

## Stubs (ghost-link materialisation)

The compile prompt freely emits wikilinks to concepts that may not have their own page yet
(Karpathy's "ghost nodes"). `a2kb stubs` writes placeholder pages for the most-referenced
ghosts, so that Obsidian's graph view remains complete:

```bash
a2kb -p alpha stubs                 # write placeholders
a2kb -p alpha stubs --dry-run       # preview only, no S3 writes
```

Stubs are stamped with `source_kind: stub` and a `ghost` tag, so they can be located easily and
are replaced by a later compile once the real source has been ingested.

## Promote

`a2kb promote` pushes a **local** Markdown file (notes an agent wrote in its working
directory, or a hand-edited reference) into the KB. It does not operate on existing S3
outputs: it is the upload side.

```bash
# Default: lands in outputs/<timestamp>-<slug>.md and feeds the compounding loop
a2kb -p alpha promote --file ./agent-notes.md

# Pinned topic page under wiki/topics/<slug>.md, kept verbatim (compile does not regenerate it)
a2kb -p alpha promote --file ./x-subsystem-internals.md --as wiki-topic --slug x-subsystem-internals

# Delete the local file after a successful upload
a2kb -p alpha promote --file ./agent-notes.md --delete-source
```

Rule of thumb: `--as outputs` (default) for transient material the wiki should absorb;
`--as wiki-topic` for permanent references you will cite from other pages.

## Clip

`a2kb clip` takes no file argument. It scans `wiki/Clippings/` (where Obsidian Web Clipper and
Remotely Save deposit clippings) and moves each one into `raw/` with normalised frontmatter,
so the normal compile pipeline picks them up on the next `a2kb compile`.

```bash
a2kb -p alpha clip                  # move clippings to raw/, delete them from wiki/Clippings/
a2kb -p alpha clip --keep           # same, but leave the originals in wiki/Clippings/

a2kb -p alpha compile               # turn the moved clippings into wiki pages
```

To bypass compile and write a page directly from a local file, use
`a2kb promote --file ... --as wiki-topic` instead.

## Backfill source

Older wiki pages may lack the visible `> Source:` line (a link to the original URL) at the top
of the body. `lint` flags them; `backfill-source` fixes them. It is a dry run unless you pass
`--apply`:

```bash
a2kb -p alpha backfill-source            # preview which pages would change
a2kb -p alpha backfill-source --apply    # rewrite them
```

## Channel-specific metadata (`source_meta`)

For sources the URL and file detectors do not cover (Slack channels, chat rooms, email), put
channel-specific provenance into `source_meta` in the raw frontmatter. The bag is free-form;
keys used elsewhere:

```yaml
source_meta:
  source_channel: "#engineering"
  source_author: "alice"
  source_message_id: "1700000000.000100"
  source_thread_id: "1699999000.000050"
  source_permalink: "https://chat.example.com/archives/C0000/p1700000000000100"
  attachment_name: "diagram.png"
  attachment_mime_type: "image/png"
  attachment_size: "42313"
  ingested_by: "chat-bot v1.3"
  captured_at: "2026-05-13T11:25:00Z"
```

The core CLI does not depend on any specific channel. `source_meta` is the place for
caller-specific data, which keeps the top-level frontmatter model stable.

## Cron

The simplest unattended deployment runs `compile` on a schedule. The wrappers in `scripts/`
(`kb-cron-compile.sh`, `kb-cron-lint.sh`, `kb-cron-backup.sh`, `kb-cron-stubs.sh`,
`kb-cron-digest.sh`) take a project folder and a KB name, and
`scripts/kb-crontab.example` shows a full schedule for supercronic. They log to
`kb-compile.log`, `kb-backup.log` and `kb-digest.log` in the project folder, and `a2kb backup`
writes to `.backups/` there; these can contain source URLs, summaries and private content, so
keep them out of version control (add `kb-*.log` and `.backups/` to the project's
`.gitignore`). `kb-cron-digest.sh` runs its Python steps under the interpreter `a2kb` was
installed with (read from the `a2kb` executable, including `#!/usr/bin/env` shebangs and pip's
`#!/bin/sh` trampoline); set `KB_PYTHON` to choose another, and `A2KB_BIN` to point at a
specific `a2kb`. It drafts the digest in a private folder (mode 0700, files 0600) under
`$TMPDIR` (default `/tmp`) and removes it on exit; when promote fails, the folder is kept and
its path is written to `kb-digest.log`. Files it creates, the log included, are 0600. The
[Docker backend](docker.md) generates one for you. A bare crontab works too:

```cron
# every 30 min, incremental compile
*/30 * * * * a2kb -p alpha compile >> ~/.kb/cron.log 2>&1

# nightly materialisation of stubs
30 3 * * *   a2kb -p alpha stubs   >> ~/.kb/cron.log 2>&1

# weekly manifest refresh, as a precaution against drift in incremental updates
0  4 * * 0   a2kb -p alpha manifest --rebuild >> ~/.kb/cron.log 2>&1
```

Each `a2kb compile` run also writes a structured JSON summary to
`<prefix>_ops/compile-runs/YYYYMMDD-HHMMSS.json`; see [operations](operations.md) for the
schema. Dashboards and alerting can read those instead of grepping logs.
