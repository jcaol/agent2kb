# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.2] - 2026-10-21

Initial public release of **agent2kb**, a headless knowledge compiler for agent teams.
The command is `a2kb`; install from source until the first PyPI release
(`pip install ".[all]"`).

### Highlights

- **Ingest, compile, query.** Ingest URLs, video, audio, documents, images, Markdown and
  Obsidian Web Clipper clippings into `raw/`; an LLM compiles them into interlinked wiki pages
  (`[[wikilinks]]`, concept pages, ghost-link stubs); answers you save flow back in as derived
  context (the compounding loop).
- **One isolated wiki per project** in any S3-compatible bucket: per-KB prefixes
  (`kbs/<name>/`) enforced in the client, and per-KB credentials where the provider can scope
  them.
- **Agents and humans on one wiki.** An MCP server (`a2kb mcp`, stdio or bearer-protected
  HTTP; read-only by default, `ask` and `ingest_url` opt-in) for agents; Obsidian, through
  Remotely Save, for people.
- **Providers.** `claude` and `codex` CLI logins (sandboxed, with a fallback CLI), Gemini,
  Groq, OpenRouter and Ollama, with separate model routing for compile and for aux jobs
  (concept pages, lint reports, digest).
- **Unattended operation.** `a2kb doctor`, cron wrappers for compile, lint, backup, stubs and
  a weekly digest, local backup and restore, structured run summaries, and automatic fallback
  when an LLM provider is down.
- **Built for untrusted content and privacy.** SSRF-guarded fetching, no tools for the LLM
  CLIs, privacy mode, new KBs with cloud side-channels (Jina Reader, cloud image OCR) off,
  secrets only in the user-level config.
- **Docker backend.** One `docker compose up` brings SeaweedFS with a scoped key per KB and a
  separate Obsidian key, the cron schedule, one read-only MCP server per KB and optional
  HTTPS; `a2kb connect` prints the Obsidian and agent settings.

### Features

- **Setup and configuration (v2).** `a2kb setup` (wizard and `--non-interactive`),
  `a2kb new`, `a2kb list`, `a2kb config show`, `a2kb config migrate`. The user-level
  `~/.kb/config.yaml` (`version: 2` with `storage`, `llm`, `defaults`, `kbs`) is the only
  place secrets live; a project folder holds a pointer (`.kb/config.yaml` with `kb: <name>`).
  `role: admin` marks a whole-bucket KB. A single KB is selected automatically. Old
  `profiles:` and flat project configs keep working. See `docs/configuration.md`.
- **Commands.** `ingest`, `compile`, `query`, `embed`, `lint` (`--deep`), `stubs`, `clip`,
  `promote`, `rm`, `backup` / `restore`, `manifest`, `outputs`, `status`, `scaffold rules`,
  `connect`, `mcp`. `a2kb <command> --help` works without a configured KB, and `a2kb --help`
  groups the commands. See `docs/commands.md`.
- **`a2kb doctor [--fix] [--read-only] [--yes] [--json]`** checks config, storage access, LLM
  login (no tokens spent), external tools, missing provider packages and privacy settings,
  and says what to fix.
- **Provider-neutral storage.** `region`, `addressing_style`, `checksum_mode` and
  `aws_profile` settings; endpoint and keys are optional (boto3's credential chain). Presets
  for AWS, R2, B2, MinIO, SeaweedFS and Wasabi. See `docs/storage-providers.md`.
- **Output language.** `output_language` (`KB_OUTPUT_LANGUAGE`) for compiled pages, query
  answers, lint reports and the digest; English by default for new KBs.
- **Privacy switches.** `privacy_mode`, `allow_jina_reader` and `allow_cloud_vision`, also as
  `KB_PRIVACY_MODE`, `KB_ALLOW_JINA_READER` and `KB_ALLOW_CLOUD_VISION`. Entity extraction at
  ingest is skipped under `privacy_mode` unless the provider is local. Every compile and
  ingest prints a banner naming the effective provider. See `docs/privacy.md`.
- **Optional dependency extras**: `groq`, `openai`, `ollama`, `gemini`, `voyage`, `fast`,
  `media`, `whisper`, `mcp`, `all`, `dev`. A missing package fails with
  `pip install "agent2kb[<extra>]"`.
- Readable one-line errors with a hint for common S3 failures; `-v` keeps the traceback and
  sends logs to stderr, so that `a2kb -v doctor --json` remains valid JSON and the MCP stdio protocol is not disturbed.
- Contract tests (`tests/test_contract.py`) pin the CLI, config, S3 layout and raw filename
  surface.

### Security

- Every outbound fetch goes through `agent2kb/net_guard.py`: DNS failures are refused, every
  resolved address must be globally routable (IPv4-in-IPv6 forms included), redirects are
  followed by hand with each hop re-validated, the connection is pinned to the validated
  IP, and bodies are capped at 100 MB.
- The HTTP mode of `a2kb mcp` is guarded against DNS rebinding and browser origins;
  `ingest_url` accepts video URLs only from YouTube or allow-listed domains; Jina Reader is
  never used under `--ingest-allow-domain`.
- `aws_profile`, the AWS credential chain and keyless storage settings are honoured only from
  the user-level config, env or flags, never from a project `.kb/config.yaml`; a project
  config that names an endpoint or region must carry its own keys.
- The Docker backend pins SeaweedFS 4.48. Older than 4.34 is affected by CVE-2026-55874 (a
  per-KB key could copy another KB's objects); see `docs/storage-providers.md`.
- URLs whose host is a shorthand numeric IPv4 form (`0177.0.0.1`, `2130706433`, `0x7f.0.0.1`,
  `127.1`) are refused before any lookup. Resolvers read them differently (macOS treated
  `0177.0.0.1` as the public `177.0.0.1`, glibc as loopback), so only canonical dotted quads pass.
- Raised dependency floors past releases with published vulnerabilities: `pypdf>=6.19`,
  `pydantic>=2.4`, `markdownify>=0.14.1`, and `mcp>=1.30,!=2.0.*,!=2.1.*,<3`.
- `a2kb ingest --folder` no longer uploads hidden files, files inside hidden folders,
  credential and runtime folders (`.aws`, `.ssh`, `.git`, `.kb`, `.obsidian`,
  `node_modules`, ...), credential files (`.env*`, `*.env`, `*.pem`, `id_rsa`, `*.tfvars`,
  `*.tfstate`, `secret(s).*`, `*service-account*.json`, ...) or symlinks, which could point
  outside the chosen folder. A recursive ingest at a terminal lists the files and asks first;
  agents and cron are never asked. `--include-hidden` and `--yes` are listed under Added.
- Folder ingest re-checks each selected file right before reading it: a file or parent folder
  swapped for a symlink since selection, a path that now resolves outside the folder, or
  something other than a regular file (a FIFO, a device) is skipped with a warning. A swap in
  the moment between that check and the handler opening the file is not caught.
- Image OCR through the KB's `claude` / `codex` CLI now obeys `privacy_mode` and
  `allow_cloud_vision` like the other cloud OCR routes; before, it ran with both switches off.
  KBs using `llm.provider: cli` with `allow_cloud_vision: false` now store image placeholders
  instead of OCR text.
- A `.md` / `.txt` downloaded by URL ingest no longer trusts its own frontmatter: it could set
  `original_link` (the duplicate-detection key) to another URL, or `source_kind`, entities and
  compile state. Only its title is kept; the rest is rebuilt from the fetched URL, whatever
  the case of its scheme (`HTTPS://` included). Local Markdown keeps its frontmatter, except
  `compiled`, `compile_*`, `raw_content_hash` and `source_kind`, which are now reset at ingest.
- `.odt` and `.epub` URLs (CLI `--url` and MCP `ingest_url`) are refused before download:
  only pandoc reads them, and its zip handling cannot be bounded by the size check. Local
  `--file` / `--folder` ingest of these formats is unchanged.
- Document ingest refuses zip containers (`.docx`, `.xlsx`, `.odt`, `.epub`, ...) that would
  expand past 200 MiB, hold more than 10,000 parts or compress more than 100:1, before any
  parser opens them; the download cap only bounded the compressed size. Every member's raw
  stream is inflated to its real end and counted; a member whose declared size does not match,
  or whose local and central compression methods differ, is refused, so understated sizes in
  the archive no longer bypass the cap. Extracted text is capped at 5 million characters, PDFs
  at 2,000 pages and spreadsheets at 100 sheets of 20,000 rows by 200 columns (read lazily); a
  capped source ends with a truncation note.
- `a2kb restore` validates archive members before uploading: absolute paths, `..`/`.`/empty
  segments, backslashes, control characters, names outside `raw/`, `wiki/` and `outputs/`, and
  links or devices are skipped and reported instead of being written (or failing mid-restore).
- Backup archives are created 0600 and new backup directories 0700, whatever the umask.
- `.gitignore` and `.dockerignore` now cover `.backups/`, the cron wrapper logs (`kb-*.log`),
  `.env.*` (except `.env.example`) and `*.bak` config backups.
- `a2kb query` and `a2kb lint --deep` print the LLM's answer literally: brackets in it such as
  `[/x]` no longer crash the command, `[[wikilinks]]` are no longer mangled, and an injected
  `[link=...]` no longer renders as a hyperlink. Page names, links, URLs, keys and config values
  shown by `ingest`, `rm`, `promote`, `outputs`, `manifest`, `status`, `claude-md` and the
  Docker bootstrap commands are escaped the same way.
- `kb-cron-digest.sh` drafts the digest in a private folder under `$TMPDIR` (0700, files 0600,
  `umask 077`) and removes it on exit. Before, stripping the CLI preamble replaced the 0600
  draft with a umask-mode copy in `/tmp`, readable by other users, and a failed promote left it
  there; now a failed promote keeps the private folder and logs its path. A new
  `kb-digest.log` is created 0600.
- A member KB's S3 key is checked as sent: keys with empty or `.` segments (`kbs//notes/x`,
  `./kbs/notes/x`, `kbs/./notes/x`) or the bare prefix without its slash are refused. Before,
  the check normalised the key but S3 received the original, so these passed for prefix
  `kbs/notes/` while naming objects outside it. Admin KBs are unchanged.
- The config `.bak` written before each v2 config change, and the legacy `save_config`, are
  created 0600 and replaced atomically; before, they existed briefly with the umask's mode
  (the legacy writer also wrote in place).
- Video URL ingest passes `--` before the URL to every `yt-dlp` call (the title lookup lacked
  it) and caps downloads with `--max-filesize 1G`. The docs now say that yt-dlp fetches outside
  the SSRF guard on the CLI path.
- `docs/docker.md` now says that `kb-mcp` can read the `claude` / `codex` logins in `kb-home`,
  and shows an opt-in override that mounts only `~/.kb` for servers that do not use `ask` with
  a CLI provider. The default mount is unchanged.
- The `claude` and `codex` CLIs no longer inherit agent2kb's secrets from the environment:
  `KB_*`, `AWS_*` (kept for Claude Code on Bedrock), the Groq, OpenRouter, Gemini, Google,
  Voyage and Safe Browsing keys, and `OPENAI_API_KEY` for Claude Code. Their own login
  variables are kept. See `docs/llm-providers.md`.
- `a2kb restore` skips archive members larger than 256 MiB and refuses an archive whose
  selected members exceed 16 GiB, before uploading anything.
- The Docker bootstrap's `DOMAIN` and `ACME_EMAIL` checks no longer accept a value that ends
  in a newline (the patterns used `$`).

### Conventions

- **Naming.** The distribution and Python package are `agent2kb`; the only command is `a2kb`.
  There is deliberately no bare `kb` entry point; add `alias kb=a2kb` if you want the short
  form. "KB" stays the noun for a knowledge base: the `.kb/` and `~/.kb/config.yaml`
  locations, the `KB_*` environment variables, the `kbs/<name>/` prefix (and the legacy
  `projects/<name>/`), raw file naming and frontmatter fields are stable and pinned by the
  contract tests.
- **Exit codes.** `0` success, `1` failure (a failed `a2kb ingest`, a `status` that cannot
  reach storage, a failed `doctor` check), `2` configuration error (one line with a hint).
- **Raw keys** end in `-<hash8>` (`<date>-<slug>-<hash8>.md`) for every source type, so two
  same-day ingests with the same title never overwrite each other.
- **Transcription** without `faster-whisper` fails with an install hint instead of uploading
  a placeholder.
- **Licence:** GNU AGPL v3.0 (`AGPL-3.0-only`), with contributions under a Contributor
  Licence Agreement ([CLA.md](CLA.md)) so that a commercial licence can also be offered.
  Contributors sign once with a pull request comment (CLA Assistant Lite workflow).

### Added

- First-class output in German (`de`), Spanish (`es`), French (`fr`), Brazilian Portuguese
  (`pt-BR`), Japanese (`ja`), Korean (`ko`), Traditional Chinese (`zh-TW`, `zh-HK`), Simplified
  Chinese (`zh-CN`) and Cantonese (`yue`), all at the same depth: language block, bilingual
  wikilinks, slug hint, query expansion, localised lint-report headings, digest rule and
  natural-language aliases such as "Deutsch" or "한국어".
- "List all" queries are recognised in every supported language (for example "alle Artikel",
  "tous les articles", "모든", "すべて"), whatever the KB language.

- `recent_days` (env `KB_RECENT_DAYS`) sets the window of the "Past N Days" recap at the top
  of `wiki/index.md`: 7 by default, up to 366, or `0` to leave it out and skip its LLM call.
- Running a command before any KB exists, in a terminal, offers to start `a2kb setup`; without
  a terminal (cron, agents, MCP) the error and exit code 2 are unchanged.
- `a2kb compile --strict` (or `KB_COMPILE_STRICT=1`) exits `1` when any attempted file failed
  or was quarantined, including with `--all-profiles`, and `75` when a KB was skipped because
  another compile of it is running. Without it the exit status is unchanged (a skipped KB
  exits `0`). The run summary gains `totals.quarantined` and a per-failure `quarantined` flag.
- One compile per KB at a time: a compile holds a lease in `<prefix>_ops/compile.lock`
  (created with If-None-Match, renewed every 2.5 minutes, expires after 10 minutes without
  renewal, taken over with If-Match once stale, deleted when the run ends). A second compile of
  the same KB, including one KB of `--all-profiles`, skips it with a message on stderr and exits
  `0`, or `75` under `--strict`. Servers without conditional writes run unlocked, as before,
  after one warning. See `docs/operations.md`.
- `a2kb restore --from-kb <name>` selects another KB's archives, and `--allow-cross-kb` allows
  restoring an archive whose manifest names a different KB (refused by default, see Fixed).
- `a2kb ingest --folder --include-hidden` opts back into hidden files and folders (credential
  paths and symlinks stay excluded), and `--yes` skips the confirmation that a recursive ingest
  at a terminal now shows (see Security).
- Docker: `kb-provision` reports `.env` LLM and language settings (`KB_LLM`, `KB_LLM_MODEL`,
  `KB_LLM_API_KEY`, `KB_LLM_OLLAMA_HOST`, `KB_LANGUAGE`) that differ from the saved config
  (keys are compared, never shown). The saved config still wins; the new opt-in
  `KB_RECONFIGURE=1` applies them to the `llm:` block and `output_language` only, keeping
  storage, KBs, keys and data, with the previous file saved as `config.yaml.bak`.
- ETag-keyed speed caches under `<prefix>_ops/`: `compile-cache.json` lets a compile run with
  nothing to do skip downloading unchanged raw/wiki pairs and index metadata (100 unchanged
  pairs: 2 GETs instead of 300) and leaves an unchanged `index.md` alone; `url-index.json`
  lets URL dedup download only new or changed raw files per ingest. Both are optional and safe
  to delete; KBs without them behave as before.
- The MCP server keeps the embedding index loaded between calls and reloads it when its ETag
  changes. Search, agentic expansion and exhaustive listings fetch pages in parallel batches.
- Documentation: `docs/first-run.md`, a first-run tutorial with a sample file
  (`examples/first-note.md`) that `a2kb setup` points to when it finishes; `docs/obsidian.md`,
  one Obsidian guide for every storage setup (viewing-only and editable key recipes, sync
  direction, encryption, compile versus sync timing, folder layout, a first sync check).
- Documentation: step-by-step AWS S3 and Cloudflare R2 recipes in
  `docs/storage-providers.md` (bucket, credentials, a full prefix policy for AWS, R2 token
  permissions and jurisdictional endpoints, provisioning versus runtime identities, a field
  mapping from the provider console to the wizard, the config and Remotely Save; not yet run
  end to end); a two-KB R2 example in `docs/configuration.md` and `docs/docker.md`; and, in
  `docs/docker.md` and `.env.example`, which `.env` settings are read only when the config is
  first created and a non-destructive procedure for changing them afterwards.

### Changed

- `output_language` defaults to English when a KB leaves it unset; set it per KB or under
  `defaults` to write in another language. `a2kb doctor` reminds you while it is unset.
- The project home is https://agent2kb.com: it is the package's `Homepage` link, and outbound
  fetches now name it in their User-Agent (`agent2kb/<version>; +https://agent2kb.com`) so
  site operators can see who is calling.
- `a2kb setup`: the cloud-vision question now names every OCR route ingest may use (the KB's
  `claude` or `codex` CLI, Gemini, the Claude CLI), and the R2 preset note points to
  `docs/storage-providers.md`.
- `a2kb backup` names archives `<kb>-wiki-YYYY-MM-DD-HHMMSS-<random>.tar.gz` (UTC) and adds a
  manifest member (`agent2kb-backup.json`) recording the source KB. `--keep` prunes only the
  current KB's archives; archives in the old `wiki-YYYY-MM-DD-HHMM.tar.gz` format are no longer
  pruned, as they do not record their KB. Old archives still restore, with a warning.
- `scripts/kb-cron-compile.sh` runs `compile --strict` and logs the exit status even when the
  run fails, so cron sees failed compiles.
- `a2kb connect obsidian` for a KB that uses the AWS credential chain now prints the settings
  sheet (endpoint, region, bucket, prefix) with placeholders for a separate device key and
  exits 0, instead of stopping with an error. The sheet also says to leave the plugin's
  encryption password empty.
- New query captures are named `outputs/[agent/]<timestamp>-<slug>-<id>.md` (`<id>`: 8 random
  hex characters) and are never written over an existing key. Existing captures keep their
  names; the `<timestamp>-<slug>` prefix is unchanged.
- `docker-compose.yml` no longer fills in defaults for `KB_LLM` / `KB_LANGUAGE`, so unset
  values are not compared with the saved config (a new config still defaults to `claude` and
  `en`).
- README: install into a virtual environment, a feature-to-extra table, a smaller install
  path beside `[all]`, and `a2kb connect obsidian` as the handoff for every provider.

### Fixed

- The test suite ignores `KB_*` variables exported in the developer's shell, which had made
  config tests fail after a manual `a2kb setup`.
- `kb-cron-digest.sh` runs under bash 3.2, the `/bin/bash` on macOS: it no longer puts heredocs
  inside `$( )`, which that version fails to parse.
- `scripts/docker-e2e.sh` and `kb-cron-digest.sh` run on macOS: they no longer rely on GNU-only
  `sed -i` and `date -d`. CI also runs the test suite on macOS.
- `a2kb setup` checks the new KB with the same `KB_*` overrides every later command applies, so
  a custom `KB_OLLAMA_HOST` is no longer reported as an unreachable local Ollama.
- The AWS policy guide no longer says a versioned bucket needs `s3:ListBucketVersions`:
  agent2kb lists with `ListObjectsV2`, which needs `s3:ListBucket`.
- PDF ingest reads the text layer with `pypdf` (added to the `media` extra); it used pandoc,
  which cannot read PDF. A failed or empty conversion now fails the ingest instead of
  uploading `[Document conversion failed]` as the source, and without pandoc a binary file
  is no longer decoded as text. `.odt`, `.epub` and `.rtf` go to pandoc; legacy `.doc` /
  `.xls` fail with a clear message; an unknown binary file is no longer ingested as text.
- URLs ending in `.md` / `.txt` were stored as the literal URL text, and `.docx` / `.xlsx`
  URLs always failed. `a2kb ingest --url` and MCP `ingest_url` now download such files
  through the SSRF guard and convert them like local files; a `.md` URL that serves an HTML
  page is ingested as a web page. An `.rtf` URL is downloaded and converted with pandoc like a
  local `.rtf` file; it was previously treated as a web page.
- Text and Markdown ingest accepts UTF-16 and UTF-32 files that start with a byte-order mark
  (they were refused as binary because of their NUL bytes) and drops a UTF-8 BOM, so
  frontmatter after it is recognised. Files with NUL bytes and no BOM are still refused.
- `a2kb ingest --folder` uploads exactly the files it listed and the user confirmed; it used
  to select the folder again after the prompt, so a file added in between was uploaded unseen.
- Compile on a whole-bucket admin KB (empty prefix) no longer writes the compiled page over
  the raw source: wiki and quarantine keys are derived relative to the KB prefix, and a
  destination equal to its source is refused before any write. `a2kb status` and `a2kb rm`
  use the same derivation.
- Quarantine after repeated compile failures deletes the source only after the
  `raw/.failed/` copy has been read back intact; otherwise the source stays in place. A
  whole-bucket KB's root-level `raw/.failed/` is now excluded from compile. Quarantined
  files stay in the run's results and totals as failures.
- A compile with nothing to compile now finishes pending maintenance: concept synthesis
  deferred by the per-run cap, concept synthesis or an embedding refresh that failed (retried
  with exponential backoff, 15 minutes up to 24 hours) and embeddings left stale by hand-edited
  wiki pages. The state lives in `<prefix>_ops/maintenance.json`; with nothing pending the
  no-op run stays cheap.
- Overlapping agent saves (MCP `ask` with `save=true`, `a2kb query --actor agent`) no longer
  lose a follow-up: the merge uses an S3 conditional write with re-read and retry, plus a
  per-process lock. `wiki/log.md`, `_meta/manifest.json` and `_embeddings/index.json` updates
  are conditional too, so concurrent writers re-apply their change instead of overwriting.
  Servers that reject conditional headers fall back to plain writes with a warning; see
  `docs/operations.md`.
- Two captures of the same question within one second no longer overwrite each other, and
  `a2kb promote --as outputs` / `--as weekly` no longer overwrite an earlier promote of the
  same slug in the same second: keys are `<timestamp>-<slug>-<8 hex>.md`, written with
  If-None-Match. The `outputs/` layout and the `<timestamp>-<slug>` prefix are unchanged.
- A conditional write that committed but whose response was lost (the SDK's retry then gets
  412) now counts once: a compile no longer skips itself as "locked" and leaves its lease
  behind, a lease renewal no longer reports a false takeover, and `wiki/log.md`, agent
  follow-ups, query captures and promote outputs are not written twice. On a server that
  returns no ETag, `wiki/log.md` is written without a condition (one warning) instead of
  silently dropping the entry after five failed attempts.
- An unrelated `InvalidArgument` / `InvalidRequest` from a conditional PUT no longer switches
  conditional writes off for the rest of the process (a long-running MCP server stayed
  unprotected); only `NotImplemented`, HTTP 501, an error naming the conditional headers, or a
  botocore without the parameters does. On that fallback the compile lease's plain write is
  removed only while it still names this compile, never another compile's lease.
- Backups of different KBs, or two runs in the same minute, no longer overwrite each other,
  and retention no longer deletes another KB's archives. Archives are written to a temporary
  file and renamed, so an interrupted backup leaves no partial archive.
- `a2kb backup --keep` checks each matching archive's manifest before counting or pruning it, so
  KBs whose names differ only by case (on a file system that does not keep case) or map to the
  same file label never prune each other's archives; such archives are kept and reported. New
  archives start with a short identity manifest so the check stays cheap.
- `a2kb restore` refuses an archive whose manifest names a different KB (name or prefix);
  `--allow-cross-kb` overrides this.
- `kb-cron-digest.sh` resolves the KB the way `a2kb -p` does (v2 `kb:` pointers, the Docker
  layout with a user-level config only, legacy project files) and runs its snapshot under
  `KB_PYTHON`; it previously failed unless `<project>/.kb/config.yaml` was a full legacy file.
  It finds the interpreter `a2kb` runs under for `#!/usr/bin/env python3` shebangs and pip's
  `#!/bin/sh` trampoline (long venv paths), not only a direct shebang, and checks that it
  imports agent2kb; otherwise it uses `python3` and logs a warning. An explicit `KB_PYTHON` is
  used as given. A relative project folder now logs to `<project>/kb-digest.log`; the log path
  used to be resolved again after the script changed into the folder.
- `python3 -m agent2kb.llm_run` honours an API or Ollama `compile_provider` instead of always
  calling a CLI. It calls only that provider: unlike compile, a cloud provider never falls back
  to another provider that has a key, and a local one never to a cloud one. Exit codes are
  unchanged.
- `a2kb setup` and `a2kb new` work with prefix-scoped keys: a `HeadBucket` 403 now means
  "cannot inspect the bucket", and access is checked by listing the KB's own prefix (setup
  used to probe `kbs/`). A missing bucket (404) is still reported as missing.
- `a2kb new <name> --here` for a KB that already exists now attaches the current folder to
  it (writes only `.kb/config.yaml`) instead of failing; the tip printed by `a2kb new` works.
  Without `--here`, or with `--prefix`/`--adopt`, an existing name is still an error.
- `a2kb setup --llm ollama` without `--model` stores the Ollama default (`llama3.3:70b`), and
  an Ollama (or `gemma4`) profile without a model no longer resolves to the Groq model name.
- `a2kb doctor` checks that the configured Ollama chat model is installed (failure) and,
  separately, the Ollama embedding model (warning only: keyword search still works).

### Known limitations

- Pre-1.0: expect rough edges.
- Only SeaweedFS has been tested against a real server; the other providers follow their
  documentation. The `docker compose up` flow is exercised by CI, not yet by the maintainers
  on a real host. See `docs/docker-testing.md`.
- Local audio and video transcription needs the heavy `whisper` extra.
- No web UI: reading is Obsidian, the CLI or MCP.
