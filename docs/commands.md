# Command reference

Every command is `a2kb [global options] <command> [options]`. `a2kb <command> --help` (or `-h`)
lists the flags of a command and needs no configured KB. `a2kb --help` groups the commands into
Getting started, Daily use, Agents, Maintenance and Backup.

## Global options

| Option | Meaning |
|---|---|
| `-p`, `--profile <kb>` | The KB to use. Without it: `$KB_PROFILE`, then the nearest `.kb/config.yaml`, then your only KB. An admin KB is never selected automatically ([configuration](configuration.md#admin-kbs)). |
| `--config <path>` | User-level config file (default: `$KB_CONFIG`, else `~/.kb/config.yaml`). |
| `-v`, `--verbose` | Verbose logging (to stderr) and full tracebacks. |
| `--s3-endpoint`, `--access-key`, `--secret-key`, `--bucket`, `--prefix` | Override the matching setting for this run. Prefer `KB_*` variables for secrets: flags show up in `ps`. |
| `--version` | Print the version. |

Exit codes: `0` success, `1` failure (including a failed `ingest`, a `status` that cannot reach
storage, a `doctor` check that fails and a `compile --strict` run with failed files), `2`
configuration error, `75` a `compile --strict` run that skipped a KB because another compile of
it was running. Without `--strict`, `compile` exits `0` even when files fail or the KB is busy;
the failures are printed and recorded in the [run summary](operations.md#structured-compile-run-summaries).

## Set up and inspect

| Command | What it does |
|---|---|
| `a2kb setup` | First-run wizard: storage, LLM, language, privacy, then your first KB. [`--non-interactive`](configuration.md#non-interactive-setup) for CI and Docker. Any other command run in a terminal before a KB exists offers to start it. |
| `a2kb new <name>` | Add a KB to your config and create its S3 skeleton. `--prefix`, `--here` (write the `.kb/config.yaml` pointer), `--adopt` (use a prefix that already holds objects). |
| `a2kb init` | Create the S3 skeleton (`raw/`, `wiki/`, `outputs/`, `index.md`, `log.md`) for an existing KB. |
| `a2kb list` | The KBs you can use, with prefix and role; `--json`. Never prints secrets. |
| `a2kb config show` | Effective settings of the selected KB, secrets masked, with the layer each value came from; `--json`. |
| `a2kb config migrate` | Print the v2 equivalent of an old-style profile (dry run); `--write` appends it, `--as <name>`, `--to <file>`. |
| `a2kb doctor` | Check config, storage, LLM login and external tools. `--fix` creates a missing bucket (asks first), `--read-only` skips the write probe, `--json`. |
| `a2kb status` | KB info, page counts, recent activity and the configured LLM targets. |
| `a2kb connect obsidian [kb]` | Print the Remotely Save settings for a KB (Docker, AWS, R2 and other providers). `--show-secret` includes the secret key. A KB that uses the AWS credential chain gets the settings with placeholders for a separate device key. See [obsidian](obsidian.md). |
| `a2kb connect agent [kb]` | Print the `claude mcp add` command (and alternatives) for a KB. `--url`, `--show-secret`. |

## Collect and compile

| Command | What it does |
|---|---|
| `a2kb ingest` | Ingest into `raw/`: `--url`, `--file` or `--folder` (`--recursive`, `--include-hidden`, `--yes`), plus `--tags`, `--actor user\|agent`, `--skip-safety`. Detects web pages, video, audio, documents, images and Markdown. PDF, `.docx` and `.xlsx` need the `media` extra (PDF text is read with `pypdf`; a scanned PDF without a text layer fails); `.odt`, `.epub` and `.rtf` need `pandoc` on `PATH`; legacy `.doc` and `.xls` are not supported (save them as `.docx` / `.xlsx`). A conversion that fails or yields no text fails the ingest instead of storing an error as the source. Markdown and text files are read as UTF-8; a UTF-16 or UTF-32 file is read when it starts with a byte-order mark (as Windows tools write it), and any other file with NUL bytes is refused as binary. Documents are bounded: a zip-based file (`.docx`, `.xlsx`, `.odt`, `.epub`, ...) that would expand past 200 MiB, has more than 10,000 parts or is compressed more than 100:1 is refused (the real expanded size is counted, not only what the archive declares), and the extracted text stops at 5 million characters, 2,000 PDF pages or, per spreadsheet, 100 sheets of 20,000 rows by 200 columns (the source then ends with a truncation note). A `--url` naming a `.md`, `.txt`, `.pdf`, `.docx`, `.xlsx` or `.rtf` file is downloaded (through the SSRF guard) and converted like a local file; `.odt` and `.epub` URLs are refused before download (pandoc reads them, and its zip reader cannot be bounded from here: download the file and use `--file`); audio URLs are not supported. Video URLs are fetched by `yt-dlp`, which does its own DNS and redirects outside the SSRF guard (only the first host is checked; see [privacy](privacy.md#video-urls-yt-dlp)), with downloads capped at 1 GiB. Frontmatter in a downloaded file is not trusted: only its `title` is kept and the rest (`original_link`, tags, entities, compile state) is rebuilt from the URL. A local file keeps its frontmatter (title, date, tags, `original_link`, entities), except `compiled`, `compile_*`, `raw_content_hash` and `source_kind`, which only the pipeline sets. A folder ingest skips hidden files and folders (unless `--include-hidden`), and always skips symlinks, credential and runtime folders (`.git`, `.ssh`, `.aws`, `.kb`, `.obsidian`, `node_modules`, ...) and credential files (`.env*`, `*.env`, `*.tfvars`, `secrets.*`, `*service-account*.json`, `*.pem`, `*.key`, `id_rsa`, ...); it prints how many paths it skipped. Right before reading each file it checks again that neither the file nor a folder above it (below the chosen root) has become a symlink, that it still resolves inside the root and that it is a regular file; otherwise the file is skipped with a warning. A recursive folder ingest by a person at a terminal lists the files and asks first (`--yes` skips the question); agents and cron jobs are never asked. Use `--file` for a single file you mean to ingest. |
| `a2kb clip` | Move Obsidian Web Clipper clippings from `wiki/Clippings/` into `raw/`; `--keep` leaves the originals. |
| `a2kb compile` | Compile `raw/` into `wiki/`, incremental by default. `--force` recompiles everything, `--llm <provider>` overrides the provider, `--all-profiles` (admin KB only), `--allow-cloud-fallback` (privacy risk: lets a local provider fall back to the cloud), `--strict` (or `KB_COMPILE_STRICT=1`: exit `1` when any attempted file failed or was quarantined, `75` when the KB was skipped because another compile of it is running; the shipped cron wrapper uses it). One compile per KB runs at a time ([compile lease](operations.md#concurrent-writers)). |
| `a2kb embed` | Build or refresh the semantic search index (`--force` re-embeds every page). Compile runs it at the end, so you rarely need it. |
| `a2kb stubs` | Write placeholder pages for ghost wikilinks; `--dry-run`, `--min-refs <n>` (default 5). |
| `a2kb promote --file <md>` | Push a local Markdown file into the KB: `--as outputs` (default), `--as weekly` or `--as wiki-topic` (a pinned page compile never regenerates); `--slug`, `--delete-source`. |

## Read and ask

| Command | What it does |
|---|---|
| `a2kb query "<question>"` | Search and answer with citations. `--top-k`, `--actor user\|agent`, `--save` / `--no-save`, `--expand auto\|dict\|llm\|off`. |
| `a2kb mcp` | Serve the KB over MCP (stdio, or `--http HOST:PORT`). `--allow-llm`, `--allow-write`, `--ingest-allow-domain`, `--token`, `--no-auth`, `--allow-origin`, `--allow-host`. See [mcp](mcp.md). |
| `a2kb outputs ls` | List saved answers: `--actor`, `--since`, `--limit`, `--titles`. |
| `a2kb outputs prune --older-than <d>` | Delete old saved answers; a dry run unless `--apply`; `--actor agent` keeps your own. |
| `a2kb scaffold rules` | Stamp the "KB query discipline" section into an agent's instruction file: `--target <file>`, `--upgrade`, `--force`, `--dry-run`. |

## Maintain

| Command | What it does |
|---|---|
| `a2kb lint` | Check page health. `--auto-fix`, `--dry-run`, `--no-links`, `--deep` (LLM audit, `--llm`). |
| `a2kb backfill-source` | Add the visible `> Source:` line to old pages. A dry run unless `--apply`. |
| `a2kb rm` (alias `a2kb delete`) | Remove a raw item and its derived pages: `--url`, `--slug`, `--file` or `--id`, `--raw` / `--wiki`, `--dry-run`, `--yes`, `--reingest`. Bulk mode (`--older-than`, `--tag`) needs a terminal and asks for the count. |
| `a2kb backup` | Local `tar.gz` archive of `raw/`, `wiki/` and `outputs/`, named and pruned per KB: `--dir`, `--keep`, `--no-raw`. |
| `a2kb restore --date <date>` | Restore an archive. A dry run unless `--apply`; `--target`, `--dir`. Refuses another KB's archive unless `--allow-cross-kb`; `--from-kb <name>` selects that KB's archives. |
| `a2kb manifest` | `--rebuild` or `--show` the metadata cache. |

See [operations](operations.md) for backup, manifest and run summaries, and
[project-workflow](project-workflow.md) for a daily routine.

## Docker backend

`a2kb bootstrap` holds the logic behind `docker compose`; it is hidden from `a2kb --help`.
See [docker](docker.md#how-it-works).
