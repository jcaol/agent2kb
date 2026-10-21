# Operations

Operating an agent2kb deployment over time: backups, the manifest cache, and the structured run summaries that make cron auditable. Examples use a KB named `alpha` (prefix `kbs/alpha/`); the cron wrappers and the Docker backend run the same commands for you ([project workflow](project-workflow.md#cron), [docker](docker.md)).

## Backup and restore

`a2kb backup` archives `wiki/` + `outputs/` + `raw/` to a local `.tar.gz`, independent of S3. `raw/` is included by default because it is the source of truth: a single archive is sufficient to rebuild the KB without re-ingesting any URL.

```bash
# Default: wiki/, outputs/ and raw/ in one archive
# (.backups/alpha-wiki-<YYYY-MM-DD-HHMMSS>-<random>.tar.gz)
a2kb -p alpha backup

# Custom local directory, keep more of this KB's archives (default keep=30, 0 keeps all)
a2kb -p alpha backup --dir ~/kb-backups --keep 60

# Skip raw/ for a smaller archive when you can re-ingest from URLs
a2kb -p alpha backup --no-raw

# Restore is a dry run by default. --date accepts YYYY-MM-DD (newest match wins)
# or a longer stamp prefix such as YYYY-MM-DD-HHMM.
a2kb -p alpha restore --date 2026-05-13                  # preview only
a2kb -p alpha restore --date 2026-05-13 --apply          # write to S3
a2kb -p alpha restore --date 2026-05-13-1432 --apply     # specific archive
a2kb -p alpha restore --date 2026-05-13 --target raw/ --apply

# Custom archive location
a2kb -p alpha restore --dir ~/kb-backups --date 2026-05-13 --apply

# Deliberately restore another KB's archive into alpha
a2kb -p alpha restore --date 2026-05-13 --from-kb beta --allow-cross-kb --apply
```

**Archive names and retention.** Each archive is named after its KB, with a UTC stamp to the
second (which `--date` matches) and a random suffix, so several KBs (or several runs of one KB) can share a directory
without overwriting each other. `--keep` counts and prunes only the archives of the KB that is
running, and checks each archive's manifest before counting or deleting it: KB names that differ
only by case (`Alpha`, `alpha`) look alike on a file system that does not keep case, and old
profile names can map to the same file name (`a b` and `a_b` both become `a_b`). An archive
whose name matches but whose manifest names another KB, or that cannot be read, is kept and
reported. New archives carry a short identity manifest as their first member as well, so this
check reads only the start of each archive. The archive is written to a temporary file in the same directory and renamed into
place once complete, so an interrupted backup never leaves a truncated archive or replaces a
good one.

**Identity check.** Every archive contains a manifest (`agent2kb-backup.json`: the source KB's
name, bucket and prefix, the creation time, file counts and the agent2kb version). `a2kb
restore` refuses an archive whose KB name or prefix differs from the current KB unless you pass
`--allow-cross-kb`; `--from-kb <name>` looks for that KB's archives instead of the current
one's. A different bucket alone is accepted (with a note), so a KB can be restored into a new
bucket after losing the old one. The manifest itself is never uploaded.

**Archive members.** Restore uploads only regular files under `raw/`, `wiki/` and `outputs/`,
the only members `a2kb backup` writes. Absolute paths, `..`, `.` or empty segments,
backslashes, control characters, names outside those areas, links (symbolic or hard) and
devices are skipped and listed in the output, in a dry run as well; they can only come from a
damaged or crafted archive. The rest of the archive is still restored. A member larger than
256 MiB is skipped the same way, and an archive whose selected members add up to more than
16 GiB is refused before anything is uploaded (each file is held in memory while it is
uploaded; nothing `a2kb backup` writes comes near either limit).

**Older archives.** Archives made by earlier versions (`wiki-YYYY-MM-DD-HHMM.tar.gz`) have no
manifest. They are still found by `--date` and restored, with a warning that their KB cannot be
checked. Because their name does not say which KB they belong to, retention never deletes them;
`a2kb backup` reports how many remain, and you delete them by hand when they are no longer
needed. An older agent2kb release that runs in the same directory does not see the new archives,
as it only prunes `wiki-*.tar.gz`.

**Permissions.** A backup directory that `a2kb backup` creates is mode 0700, and archives are
always 0600, whatever the umask. An existing directory keeps its mode. Archives contain raw
sources and saved answers: when the backup directory is inside a Git repository of your own,
add it to that repository's `.gitignore` (a checkout of agent2kb already ignores `.backups/`).

Existing S3 keys are **overwritten** by `--apply`; there is no merge logic. A separate storage-side backup (versioning or replication) is still recommended. `a2kb backup` is intended for keeping a local copy of a KB, not as a disaster-recovery mechanism.

## Manifest cache

`<prefix>_meta/manifest.json` is the per-KB metadata cache that allows lint, query and index to avoid downloading every page in S3 only to read its frontmatter. The manifest is:

- **Versioned** (`version: 1`): a reader that encounters a different version falls back to per-page downloads.
- **Eventually consistent**: compile, promote and the saving of outputs refresh the entry they have just written. The manifest is never the source of truth.
- **Safe to delete**: `a2kb manifest --rebuild` recreates it from scratch.

Operations:

```bash
# Rebuild from scratch. Run after large bulk imports, or weekly from cron.
a2kb -p alpha manifest --rebuild

# Inspect the current manifest
a2kb -p alpha manifest --show
# > Manifest: 1247 entries, generated_at=2026-05-13T11:25:00
# >   article: 1183
# >   concept: 41
# >   query_output: 22
# >   stub: 1
```

Schema (per entry):

```json
{
  "kbs/alpha/wiki/20260411-llm-overview.md": {
    "hash": "sha256:...",
    "title": "LLM Overview",
    "date": "2026-04-11",
    "tags": ["llm", "rag"],
    "source": "https://example.com/llm-overview",
    "original_link": "https://example.com/llm-overview",
    "kind": "article",
    "wikilink_count": 7,
    "indexed_at": "2026-05-13T10:00:00"
  }
}
```

## Structured compile run summaries

Every `a2kb compile` writes a JSON summary to `<prefix>_ops/compile-runs/YYYYMMDD-HHMMSS.json`:

```json
{
  "started_at": "2026-05-13T11:30:00",
  "profile": "alpha",
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "new_only": true,
  "allow_cloud_fallback": false,
  "cloud_fallback_blocked": false,
  "totals": {
    "files": 12,
    "ok": 11,
    "failed": 1,
    "quarantined": 0,
    "duration_sec": 84.3,
    "tokens_in": 41203,
    "tokens_out": 18954
  },
  "failures": [
    {
      "source_key": "kbs/alpha/raw/2026-05-13-x.md",
      "error": "Rate limit exceeded — retry in 30s",
      "error_class": "rate_limit",
      "transient": true,
      "quarantined": false,
      "duration_sec": 0.3
    }
  ],
  "fallback_attempts": []
}
```

`profile` is the KB name. Error classes are stable: `rate_limit`, `auth`, `content_too_large`, `network`, `provider_blocked`, `empty_response`, `other`. The `transient` flag distinguishes self-healing failures (worth a retry) from human-action ones (`auth`, `provider_blocked`).

Dashboards and alerting can read these summaries instead of parsing logs. Useful questions to ask of them:
- Were there any non-transient failures in the last 24 hours?
- What was the 95th-percentile compile duration over the last week?
- Does `cloud_fallback_blocked: true` appear? This is a signal of a privacy regression.

`quarantined` counts the failed files that this run moved to `raw/.failed/`; they are included
in `failed`.

Logging is best-effort: the writer suppresses exceptions, so an outage of the operations storage cannot cause compile itself to fail.

### Exit status for cron and CI

`a2kb compile` exits `0` even when files fail, as it always has. Pass `--strict` (or set
`KB_COMPILE_STRICT=1`) to exit `1` when any attempted file failed or was quarantined; with
`--all-profiles` the run exits `1` if any KB had a failure. A KB that was skipped because another
compile of it holds the [compile lease](#concurrent-writers) does not count as a failure: the run
exits `0`, or `75` (EX_TEMPFAIL, "try again later") under `--strict` when there was no file
failure. `scripts/kb-cron-compile.sh` uses `--strict` and logs the exit status, so a cron tick
that overlaps a long compile logs `exit: 75`.

### Pending maintenance

After compiling, `a2kb compile` synthesises concept pages and refreshes the embedding index.
Concept synthesis is capped per run and stops when the LLM is unavailable, and the embedding
refresh can fail during a provider outage. Their state is kept in
`<prefix>_ops/maintenance.json`, so a later run with nothing to compile still finishes the work:

- concepts deferred by the per-run cap are synthesised on the next run;
- concept synthesis or an embedding refresh that failed is retried after 15 minutes, doubling
  per consecutive failure up to 24 hours (a run that compiles files always retries);
- the embedding index is refreshed when the wiki listing changed without a raw change, for
  example after a page was edited by hand or promoted.

When none of this is pending, a run with nothing to compile only rebuilds `wiki/index.md`.
The first such run after an upgrade does one incremental embedding refresh to record the
current state. Deleting the file is safe: no concept work is then known to be pending, and the
next run does one incremental embedding refresh.

### Speed caches

Two files under `<prefix>_ops/` let frequent operations skip downloading unchanged objects. Both
are keyed by the S3 ETag that every listing returns, so an object that changed in any way
(compile, hand edit, restore, another device) is downloaded again; an object without an ETag is
never cached. Both are pure caches: a missing, unreadable or foreign-version file means "cold",
which is how a KB behaved before they existed, and deleting them is always safe.

- `_ops/compile-cache.json`: the raw/wiki pairs last verified as up to date, and the metadata
  `wiki/index.md` is built from. A compile run with nothing to do reads this file and
  `maintenance.json` instead of downloading every raw and wiki page (for 100 unchanged pairs:
  2 GETs instead of 300), and does not rewrite an unchanged `index.md`.
- `_ops/url-index.json`: the normalised source URLs of each raw file, for URL dedup on ingest
  and Web Clipper import. An ingest downloads only the raw files added or changed since the
  last lookup instead of all of `raw/`.

### Concurrent writers

Several processes can write to one KB at the same time: a cron compile, an `a2kb query`, an MCP
server (whose tool calls also overlap with each other) and an Obsidian device. Files that are
read, changed and written back use S3 conditional writes (`If-Match` on the ETag read, or
`If-None-Match` for a file that must not exist yet). A writer that loses a race re-reads the
file and applies its change again, so neither change is lost:

| File | What happens on a conflict |
|---|---|
| `outputs/agent/*.md` follow-ups (`ask` with `save=true`, `a2kb query --actor agent`) | re-read and appended again; after 5 conflicts the answer is saved as a new capture |
| new captures and promotes in `outputs/` | named `<timestamp>-<slug>-<id>.md` with a random `<id>`, and never written over an existing key |
| `wiki/log.md` | re-read and the entry prepended again |
| `_meta/manifest.json` | re-read and the entry update applied again |
| `_embeddings/index.json` | re-read; this writer's added, changed and removed vectors are applied on top |

Within one process, agent saves are also serialised by a lock, which covers overlapping MCP tool
calls even on a server without conditional writes.

Limits:

- **Conditional writes need server support.** AWS S3 supports `If-Match` and `If-None-Match`
  on `PutObject`; for other S3-compatible servers (R2, MinIO, SeaweedFS, ...) check the
  provider's documentation, as support varies by product and version. A server that rejects
  the headers (`NotImplemented` or HTTP 501, or `InvalidArgument` / `InvalidRequest` whose
  message names `If-Match`, `If-None-Match` or conditional writes) is detected on the first
  such write; from then on that process writes unconditionally, as before, and logs a warning.
  Other `InvalidArgument` / `InvalidRequest` errors fail the write and leave conditional
  writes on. A server that returns no ETag on GET cannot guard `wiki/log.md`; that log is
  then written unconditionally, with one warning. A
  server that silently ignores them gives no protection: overlapping writers from separate
  processes then behave as last-writer-wins.
- **Caches are last-writer-wins by design.** `_ops/maintenance.json`, `_ops/compile-cache.json`
  and `_ops/url-index.json` are rewritten whole. A lost update only means some work is redone
  on the next run (a pair is re-verified, a URL lookup re-reads a file, a maintenance task runs
  again), never lost content.
- **Compiles of one KB need conditional writes to be serialised** (see the compile lease below).
  Without them two compiles of the same KB can run at once, compile the same raw file and write
  the same wiki page, as before.
- **boto3 older than the conditional-write parameters** (`IfMatch` on `put_object`) is treated
  like a server without support: writes fall back to plain PUTs with a warning.

#### Compile lease

One compile of a KB runs at a time. A compile first creates `<prefix>_ops/compile.lock` with
`If-None-Match`, a small JSON lease naming its holder (a random id, the host name and the pid),
when it started and when it expires. A second compile of the same KB (a cron tick during a
long manual run, or another host) finds the live lease, prints which host and pid hold it to
stderr and skips the KB: exit `0`, or `75` under `--strict`. With `--all-profiles` only the busy
KB is skipped; the others compile.

- **Lifetime and renewal.** A lease expires 10 minutes after it was last renewed, and the
  holder renews it every 2.5 minutes for as long as the compile runs. 10 minutes leaves room
  for a late renewal and for clocks on different hosts that disagree by a few minutes (expiry
  compares the holder's clock with the reader's); keep host clocks in sync (NTP).
- **Crashed compiles.** A lease that was not released (the process was killed, the host went
  away) blocks the KB for at most 10 minutes. The next compile after that takes it over with
  `If-Match` on the stale lease's ETag, so of two compiles that find the same stale lease only
  one wins, and says so on stderr. An unreadable lease file counts as stale. Deleting
  `_ops/compile.lock` by hand releases it at once; only do that when no compile is running.
- **Release.** The lease is deleted when the compile ends, also after an error. A compile only
  deletes a lease that still names it as holder.
- **A stalled holder.** A compile that could not renew for 10 minutes (a suspended laptop) may
  find its lease taken over. It logs a warning and finishes its run, so for that run both
  compiles write to the KB.
- **Servers without conditional writes.** A server that rejects the headers gets the old
  behaviour: one warning, and the compile runs unlocked. A server that accepts and ignores
  them is caught only partly: each compile reads the lease back after creating it and skips
  if it names another holder, which misses two compiles that create and read back at the
  same moment.
- **Cost.** A lease adds a PUT, a DELETE and two small GETs (read back after creating it, and
  before deleting it) to each compile, also to a compile with nothing to do, plus a PUT per
  renewal.

### Quarantined sources

A raw file that fails to compile three times in a row with a non-transient error is moved to
`<prefix>raw/.failed/` (for a whole-bucket admin KB, `raw/.failed/` at the bucket root) and
the removal is noted once in `wiki/log.md`. The source is deleted only after the copy has been
written and read back intact; if that fails the source stays where it is and is retried on the
next run. Compile never picks up files in `raw/.failed/`. To retry one, copy it back to
`raw/` and remove its `compile_failures` frontmatter line.

## Query expansion modes

Cross-language expansion (English and Chinese) uses a built-in dictionary first by default. For high-frequency queries, set the mode to `dict` or `off` to remove the LLM round trip entirely.

```bash
a2kb -p alpha query "..." --expand auto    # default: dictionary first, LLM only as a fallback
a2kb -p alpha query "..." --expand dict    # dictionary only, never calls the LLM
a2kb -p alpha query "..." --expand llm     # always calls the LLM
a2kb -p alpha query "..." --expand off     # no cross-language expansion
```

Expansion results are cached per engine on a normalised token-set key, so repeated queries in a script do not incur repeated LLM calls.

## Lint hygiene

`a2kb lint` checks frontmatter shape, missing source lines on articles, empty bodies, and the visible `> Source:` line on articles. Unresolved wikilinks are **not** reported as errors by default; they are treated as Obsidian ghost nodes, in line with Karpathy's pattern.

```bash
a2kb -p alpha lint                  # default: ghost links are not reported
a2kb -p alpha lint --auto-fix       # rewrite confident wikilink matches, list residuals
a2kb -p alpha lint --dry-run        # preview only: no S3 writes
a2kb -p alpha lint --no-links       # skip the wikilink resolver
a2kb -p alpha lint --deep --llm gemini   # LLM-based semantic health check (slow)
```

`--deep` runs an audit for contradictions and knowledge gaps across the wiki. It is useful periodically but expensive; do not schedule it from cron without a token budget.
