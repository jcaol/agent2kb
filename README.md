<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb: the knowledge compiler for agent teams"></a>
</p>

<p align="center">
  <b>agent2kb turns everything your agents find into a living, cited Markdown wiki.</b><br>
  An LLM compiles it in your own bucket; agents read it over MCP,
  people read it in Obsidian or any Markdown editor.
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#try-it">Try it</a> ·
  <a href="docs/first-run.md">First run</a> ·
  <a href="#why-agent2kb">Why agent2kb</a> ·
  <a href="#documentation">Docs</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="License: AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="MCP server">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="Any S3 storage">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Runs fully local with Ollama">
</p>

<p align="center">
  <b>English</b> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-TW.md">繁體中文</a>
</p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**A headless knowledge compiler for agent teams.** Agents and bots submit raw sources (web
pages, videos, PDFs, notes); an LLM compiles them into a self-maintaining wiki of interlinked
Markdown pages; agents read it through the CLI or MCP, and people read it in Obsidian or
any other Markdown editor. Each
project has its own isolated wiki in any S3-compatible bucket, and no application needs to
remain open.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="How agent2kb works: a2kb ingest puts sources in raw/, a2kb compile turns them into
    wiki/ pages with an LLM, agents read the wiki over the CLI and MCP, people read it in
    Obsidian, and saved answers in outputs/ feed the next compile">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **Ingest once, compile unattended.** Cron runs compile, lint, backup and digest, and the
  pipeline falls back to another LLM when one is unavailable. Agents only need `a2kb ingest`.
- **One knowledge base (KB) per project.** Each KB has its own prefix and, where the storage
  provider allows it, its own credentials, so an agent working on one KB cannot access the
  others.
- **One wiki for agents and people.** Agents use an MCP server or the CLI; people read plain
  Markdown with `[[wikilinks]]` in Obsidian or any Markdown editor. Saved answers feed the
  next compile, so the wiki
  grows in the areas that are queried most often.
- **Designed for untrusted content and for privacy.** Web pages and file downloads go through an
  SSRF guard (video URLs, which `yt-dlp` fetches itself, are the documented exception),
  `claude` and `codex` run without tools, new KBs keep cloud side-channels disabled, and a
  fully local setup with Ollama is supported.

It follows Andrej Karpathy's
[LLM wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) and
turns it into a server-side tool.

### What your team sees in a wiki editor

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="Illustration: a compiled agent2kb page open in Obsidian, with frontmatter tags,
    the article with wikilinks, a graph view of related pages and a backlinks panel">
</p>

Every compiled page arrives as plain Markdown with its properties (`source`, `date`, tags,
`source_kind`), a summary, structured sections and `[[wikilinks]]`, so Obsidian's graph view,
backlinks and tag search work out of the box, and `index.md` opens with a "Past 7 days" recap
of what the KB learned that week. Unresolved links stay as ghost nodes until a later
compile or `a2kb stubs` fills them. *Illustration built from the sample in
[examples/first-note.md](examples/first-note.md); see [docs/obsidian.md](docs/obsidian.md) to
connect your vault.*

Obsidian is the reader agent2kb documents and recommends, because it renders `[[wikilinks]]`,
the graph and backlinks and can sync from S3 with the Remotely Save plugin. The files are
ordinary Markdown, so any editor that reads them works; tools other than Obsidian need their own
way to sync from S3 and have not been tested.

### A recap of what is new, written for you

Every compile refreshes the top of `wiki/index.md` with a recap of the last 7 days: a short
summary of the themes the new pages share, written by the LLM from those pages, followed by
links to each of them. Nobody has to write it, and it keeps up with the KB, so a teammate
returning from leave, or a learner who checks in once a week, sees at a glance what the KB has
learned. An example (fictional content):

```markdown
## Past 7 Days (4 articles)

> This week the KB focused on safer releases. Two articles compare canary and blue-green
> deployments and agree that automated rollback matters more than the strategy itself; a
> third shows how feature flags keep unfinished work out of a release, and an incident review
> traces an outage to a missing health check, linking it to [[Rollback]] and [[Monitoring]].

- 2026-10-20 [[20261020-Canary-Releases-in-Practice-1a2b3c4d|Canary Releases in Practice]]
- 2026-10-19 [[20261019-Blue-Green-Deployments-5e6f7a8b|Blue-Green Deployments]]
- 2026-10-17 [[20261017-Feature-Flags-for-Safe-Releases-9c0d1e2f|Feature Flags for Safe Releases]]
- 2026-10-15 [[20261015-Incident-Review-Missing-Health-Check-3a4b5c6d|Incident Review: Missing Health Check]]
```

The window is a setting (`recent_days`: `30` for a monthly recap, `0` to turn it off), and an
optional weekly digest writes a longer recap to `outputs/weekly/`.

### Every device and every agent, one KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="One KB in your S3 bucket, shared by people on desktop, iPhone and Android through
    Obsidian, and by agents through the CLI and MCP; compile runs on a schedule beside it">
</picture>

The KB lives in your S3 bucket, not on one machine, so the same compiled wiki reaches every
device: Obsidian on macOS, Windows and Linux, on iPhone and iPad, and on Android (through the
Remotely Save plugin), and agents on any laptop, server or CI runner (through the CLI or MCP).
Compile runs on a schedule next to the storage, so pages keep improving while every device is
off. Phones need an HTTPS endpoint (AWS S3, Cloudflare R2, or the Docker backend with its HTTPS
profile); see [docs/obsidian.md](docs/obsidian.md). The maintainer has run this setup with
Obsidian on several desktop and mobile devices against a self-hosted SeaweedFS backend on
earlier versions; the current release's recipes have not yet been re-run end to end.

## Try it

Once the prerequisites below are in place, the first page takes a few minutes; creating a cloud
account, a bucket and keys takes longer and is described in
[storage providers](docs/storage-providers.md).

Prerequisites: Linux or macOS with a POSIX shell (the platforms the package declares; Windows
is untested), Python 3.10 or later with its `venv` module (on Debian and Ubuntu:
`sudo apt install python3-venv`), and `git`. Then:

- **Storage:** an S3-compatible bucket and a key for it (AWS S3 and Cloudflare R2 recipes:
  [storage providers](docs/storage-providers.md)), or the
  [Docker backend](#docker-backend-in-10-minutes), which includes storage.
- **An LLM:** the `claude` or `codex` CLI, installed and logged in
  (`npm i -g @anthropic-ai/claude-code` and `claude auth login`, or `npm i -g @openai/codex`
  and `codex login`); or an API key (Gemini, Groq, OpenRouter); or a local Ollama server.

### Install

Install into a virtual environment, so that agent2kb and its dependencies stay separate from
the system Python (many Linux distributions refuse a system-wide `pip install` with an
`externally-managed-environment` error):

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # once: an isolated environment in .venv/
source .venv/bin/activate                   # in every new terminal (zsh and bash)
python -m pip install --upgrade pip
python -m pip install ".[all]"              # source install until the first PyPI release
a2kb --help                                 # should print the command list
```

`python -m pip` installs into the active environment, whichever `pip` is first on your `PATH`.
In a later terminal, run `source .venv/bin/activate` in the checkout again before using `a2kb`,
or call `.venv/bin/a2kb` directly (for example from cron). `deactivate` leaves the environment.

`[all]` installs every provider SDK, document and video ingest and the MCP server. A smaller
install is enough for text, Markdown and web pages with a `claude` or `codex` CLI login:

```bash
python -m pip install .                     # core only (checked: installs, `a2kb --help` runs)
python -m pip install ".[mcp]"              # core plus the MCP server for agents
```

What each feature needs in addition to the core install (`a2kb doctor` lists what is missing):

| Feature | Python extra | External program |
|---|---|---|
| Markdown, text, web pages | none | none |
| `claude` / `codex` CLI as the LLM | none | the CLI, logged in |
| Gemini, Groq, OpenRouter, Ollama | `gemini`, `groq`, `openai`, `ollama` | an Ollama server, for Ollama |
| Embeddings and rerank (optional) | `voyage`, `openai`, `gemini` or `ollama`; `fast` for faster vector search | none |
| `.docx`, `.xlsx` documents | `media` | none |
| PDF | `media` | none |
| Other document formats (for example `.epub`) | none | `pandoc` |
| Video URLs (YouTube and others) | `media` (yt-dlp) | `ffmpeg` |
| Local audio and video transcription | `whisper` (heavy; **not** part of `all`) | `ffmpeg` |
| MCP server (`a2kb mcp`) | `mcp` | none |

Extras combine: `python -m pip install ".[mcp,media]"`. Python packages never install
`ffmpeg` or `pandoc`; use your system's package manager (`apt install ffmpeg pandoc`,
`brew install ffmpeg pandoc`).

### First KB

```bash
a2kb setup                        # wizard: storage, LLM, language, privacy, your first KB (notes)
a2kb doctor                       # checks config, S3 access, LLM login, external tools

a2kb ingest --file examples/first-note.md       # a small sample that ships with the repo
a2kb compile                      # raw/ -> wiki/ (incremental; --force recompiles everything)
a2kb query "What is the codename of the alpha project?"
```

The [first-run tutorial](docs/first-run.md) walks through the same steps with the expected
output of each (the raw file name, the wiki page, the cited answer), connects Obsidian, and has
a recovery table for the usual problems. To ingest your own sources use
`a2kb ingest --url <url>`, `--file <path>` or `--folder <dir>`.

If you run any command before a KB exists, `a2kb` offers to start `a2kb setup` for you (only
in a terminal; scripts, cron and agents get the usual error).

`a2kb doctor` reports a missing `claude` or `codex` login as a failure. This is expected until
you have logged in; API keys and Ollama require no login. While you have exactly one KB, the
`-p` option can be omitted; with several KBs, use `a2kb -p notes ...`. `a2kb list` shows your
KBs, and `a2kb new myproject --here` writes a `.kb/config.yaml` pointer so that `a2kb` works in
a project folder without `-p`. The package is named `agent2kb`, and the only command it
installs is `a2kb` (an `alias kb=a2kb` can provide a shorter form).

## Why agent2kb

Agent teams accumulate knowledge in chat logs, scratch notes and browser tabs. Placing all of
it in a vector store returns fragments rather than understanding; a single shared vault mixes
projects and has no process that keeps it consistent; and neither approach gives the people who
rely on the knowledge a way to audit it.

agent2kb separates the work into three stages. **Collection** is deliberately simple: any
process can run `ingest` to place a source in `raw/`. **Understanding** is the compile step:
an LLM turns the sources into pages with titles, summaries, tags and links, rewrites only what
has changed, and labels what it derived itself so that raw sources take precedence when they
disagree. **Use** happens through two interfaces over the same files: `a2kb query` and MCP for
agents, and Obsidian for people. Everything is stored as Markdown in your own bucket, so it can
be read, compared, backed up and edited by hand.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="What makes agent2kb different: 1. compiles, not just retrieves; 2. one isolated KB
    per project; 3. agents and people, one wiki; 4. knowledge that compounds; 5. your bucket,
    no lock-in; 6. safe with untrusted content">
</picture>

### How it compares

| | Vector store / RAG | Shared notes vault | Agent session memory | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| Readable pages, not fragments | ❌ chunks | ✅ hand-written | ❌ transcripts | ✅ **LLM-compiled** |
| Kept consistent without manual work | ❌ | ❌ | ❌ | ✅ **incremental compile + lint** |
| Separate, isolated knowledge per project | ⚠️ per index | ❌ one vault | ⚠️ per agent | ✅ **one prefix and key per KB** |
| Agents and people use the same source | ❌ agents only | ⚠️ people only | ❌ agents only | ✅ **MCP + Markdown editor** |
| Every answer cites its sources | ⚠️ | ❌ | ❌ | ✅ |
| Plain files in storage you own | ❌ | ✅ | ❌ | ✅ **any S3 bucket** |
| Can run with no third party at all | ⚠️ | ✅ | ❌ | ✅ **Ollama + local S3** |

Every file carries YAML frontmatter (`title`, `source`, `date`, `tags`, `source_kind`).
`source_kind` records trust: raw sources and curated topics are primary; concept pages and
saved answers are derived. See [docs/compounding-loop.md](docs/compounding-loop.md).

## Features

| | |
|---|---|
| **Ingest** | URLs, YouTube and other video, audio, PDF/DOCX/XLSX, images (OCR), Markdown, Obsidian Web Clipper clippings; duplicate detection, safety check before fetching |
| **Compile** | Incremental raw-to-wiki compile with `[[wikilinks]]`, concept pages and ghost-link stubs; an index that opens with a "Past 7 days" recap, and an optional weekly digest; output in English or any language you name |
| **Query** | Keyword search with optional embeddings and reranking, cited answers and search-term expansion; saved answers in `outputs/` close the compounding loop |
| **Agent access** | MCP server (`a2kb mcp`, stdio or bearer-protected HTTP), read-only by default; `ask` and `ingest_url` are opt-in |
| **Isolation** | Per-KB prefixes enforced by the client; per-KB credentials where the provider can scope them |
| **LLMs** | `claude` / `codex` CLI logins (sandboxed, with a fallback CLI), Gemini, Groq, OpenRouter, Ollama; separate model for bulk compile and for aux jobs |
| **Operations** | `a2kb doctor`, `lint`, `backup` and `restore`, a run summary for every compile, cron wrappers in `scripts/` |
| **Docker backend** | One `docker compose up`: SeaweedFS with per-KB keys, cron, an MCP server per KB, optional HTTPS |

## Quick start (local)

[Try it](#try-it) covers the complete path, and the [first-run tutorial](docs/first-run.md)
shows the expected result of each step. Additional notes:

- `a2kb setup --non-interactive` takes everything from flags and `KB_*` variables, for CI and
  provisioning ([flags](docs/configuration.md#non-interactive-setup)).
- Storage presets: AWS S3, Cloudflare R2, Backblaze B2, MinIO, SeaweedFS, Wasabi, other
  ([storage providers](docs/storage-providers.md)).
- Compile, lint, backup and stubs run on a schedule: see
  [Keeping the wiki compiled](#keeping-the-wiki-compiled).
- Every command and flag: [docs/commands.md](docs/commands.md).

## Keeping the wiki compiled

`a2kb ingest` only stores sources in `raw/`; pages appear when `a2kb compile` runs. You can run
it by hand, but the usual setup runs it **on a schedule**, so anything an agent or a person
ingests becomes a page without anyone waiting for it:

| How you run agent2kb | Who schedules compile |
|---|---|
| Docker backend | Done for you: compile every 15 minutes, lint daily, backup twice a day, stubs daily (`KB_CRON_DIGEST=1` adds a weekly digest) |
| Native install (Linux, macOS) | You add a cron entry, using the wrappers in `scripts/` |
| Occasional use | Run `a2kb compile` yourself after ingesting |

A minimal crontab for a native install (`crontab -e`; adjust the paths):

```cron
# every 15 minutes: compile new or changed sources (no LLM use when nothing changed)
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# daily: structural lint, and placeholder pages for often-linked missing topics
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# twice a day: local backup archive of the KB
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

The wrappers log to `kb-*.log` in the project folder and call compile with `--strict`, so a
failed compile exits non-zero for monitoring. Overlapping runs are safe: while one compile of a
KB holds its lease in storage, another run of the same KB skips that tick (exit 75 under
`--strict`) and the next tick picks the work up. The full template is
`scripts/kb-crontab.example`; scheduling details are in
[docs/project-workflow.md](docs/project-workflow.md#cron).

**Which LLM compiles is a setting, not part of the schedule.** Cron only runs `a2kb compile`;
the KB's `llm:` block decides who does the work:

| `llm.provider` | What compiles | Cost and privacy |
|---|---|---|
| `cli` with `command: claude` or `codex` | The Claude Code or Codex CLI you are logged in to, run without tools in a sandbox | Uses your subscription; content goes to that provider |
| `ollama` | A local Ollama model (`model: llama3.3:70b`, or any model you have pulled) | Fully local; needs a machine that can run the model |
| `gemini`, `groq`, `openrouter` | That provider's API with your key | Billed (or free-tier) API usage; content goes to that provider |

With `provider: cli`, `fallback_command` names a second CLI (for example `codex` behind
`claude`) that takes over for the rest of the run when the first is out of credits or logged
out. `model` and `aux_model` pick the models for page compilation and for the lighter jobs
(concept pages, lint reports, digest); both default to the provider's recommended choice.
Details: [docs/llm-providers.md](docs/llm-providers.md).

## Settings and customisation

Everything is set in one user-level file, `~/.kb/config.yaml` (mode 0600; `a2kb setup`
writes it, and `KB_CONFIG` can point elsewhere). Settings under `defaults:` apply to every KB;
the same keys under one KB override them for that KB only:

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # English by default; any language name works
  privacy_mode: false          # true: no ingest-time cloud calls at all
  allow_jina_reader: false     # true: hard-to-read web pages are fetched through jina.ai
  allow_cloud_vision: false    # true: images are sent to a cloud model for OCR
kbs:
  notes: {}                    # uses the defaults
  research:
    output_language: Spanish   # this KB writes its pages and answers in Spanish
  client-work:
    llm: { provider: cli, command: codex }   # this KB is compiled by the Codex CLI instead
```

The quickest start is the `claude` or `codex` CLI you are already logged in to: no API key and
no extra service. For a KB whose content must never leave your machine, set
`llm: { provider: ollama }` and run a local model with Ollama (it needs a machine that can run
the model; see [LLM providers](docs/llm-providers.md#ollama)).

| To change | Set | More |
|---|---|---|
| Language of pages and answers | `output_language` (per KB or in `defaults`) | [Output language](docs/configuration.md#output-language) |
| The "Past N days" recap in the index | `recent_days` (7 by default, `0` = off) | [Recent-pages recap](docs/configuration.md#recent-pages-recap) |
| Which LLM compiles and answers | the `llm:` block (per KB allowed) | [LLM providers](docs/llm-providers.md) |
| What may leave your network | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` | [Privacy](docs/privacy.md) |
| Where a KB is stored | `storage:` (shared) or a KB's own `storage:` and `prefix` | [Storage providers](docs/storage-providers.md) |
| Which KB a folder uses | `.kb/config.yaml` with `kb: <name>` in that folder (`a2kb new <name> --here`) | [Configuration](docs/configuration.md#using-a-kb-from-a-project-folder) |
| A one-off override | `KB_*` environment variables, for example `KB_OUTPUT_LANGUAGE=ja` | [Environment variables](docs/configuration.md#environment-variables) |

`a2kb config show` prints the effective value of every setting and which file or variable it
came from; `a2kb doctor` checks that the result works. Secrets (storage keys, API keys) belong
only in this user-level file or in environment variables, never in a project folder.

## Docker backend in 10 minutes

```bash
cp .env.example .env                  # name your KBs (KB_NAMES=research,work), pick an LLM
docker compose up -d                  # SeaweedFS (S3) + cron jobs + MCP servers; keys are generated
docker compose exec kb claude auth login                                # once, if KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # Remotely Save settings
docker compose exec kb a2kb connect agent research --show-secret      # `claude mcp add ...` with URL and token
```

This generates S3 storage with a separate scoped key per KB, the bucket, the configuration, a
cron schedule and one read-only MCP server per KB. Set `DOMAIN=kb.example.com` and use
`--profile https` to obtain automatic HTTPS (for Obsidian on a phone and for remote agents), or
configure your own S3 storage instead. The compose services are named `kb` and `kb-mcp`; the
command inside them is `a2kb`. The walk-through, key rotation and test record are in
[docs/docker.md](docs/docker.md).

## Connect Obsidian

```bash
a2kb connect obsidian notes                # Remotely Save settings for KB notes
a2kb connect obsidian notes --show-secret  # the same, including the secret key
```

This works for every setup: AWS S3, Cloudflare R2, other providers and the Docker backend
(there: `docker compose exec kb a2kb connect obsidian <kb>`). It prints the endpoint, region,
bucket, URL style and the KB's remote prefix (for example `kbs/notes/`) for the community
plugin **Remotely Save** ("S3 or compatible"). With the prefix set, the vault root is the KB:
`raw/`, `wiki/` and `outputs/` appear as folders. For a KB that uses the AWS credential chain
the sheet shows placeholders for the key, because a device should get its own key, never the
machine's AWS login.

Pick the key for the plugin according to what the device does:

- **Viewing only:** a read-only key (list and read) and a pull-only sync direction.
- **Editing and Web Clipper:** two-way sync uploads and deletes files, so it needs a key that
  can list, read, write and delete in the KB's prefix: a separate key for the device, not the
  one agent2kb uses, so that it can be revoked on its own.

Leave the plugin's encryption password empty for the KB (agent2kb reads plain Markdown), and
start with a new, empty vault. Compile and sync run on separate clocks: a page reaches the
device at the first sync after the compile, and sync runs only while Obsidian is open. The
full guide, including both recipes, the folder layout and a first sync check, is
[docs/obsidian.md](docs/obsidian.md).

Unresolved `[[wikilinks]]` are intentional ghost nodes that a later compile resolves;
`assets/obsidian-snippets/dim-ghost-links.css` dims them, and `a2kb stubs` can create
placeholder pages for the most frequently referenced ones.

## Connect your agents (MCP)

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # read-only: search, read_page, list_recent, kb_info
```

`--allow-llm` adds the `ask` tool and `--allow-write` adds `ingest_url`; HTTP mode requires a
bearer token. Configuration snippets for Codex and Claude Desktop are in
[docs/mcp.md](docs/mcp.md). To instruct an agent when to consult the KB,
`a2kb scaffold rules --target <file>` writes a "KB query discipline" section into its
instruction file (for example `AGENTS.md`).

## Compared with other approaches

- **Plain Obsidian sync.** Synchronising a vault provides storage and a reader, but nothing
  writes the pages, and every project shares one folder. agent2kb adds the compiler,
  per-project isolation and unattended operation, while Obsidian remains the reader. For a
  single local vault that you want to query from your editor, a plugin that runs inside the
  vault is the simpler choice.
- **Vector-RAG services.** These return the text fragments closest to a question. agent2kb
  compiles sources into readable pages with links and provenance, so the result can be
  browsed, corrected and compared, and retrieval runs over those pages. Embeddings are optional.
- **Agent session-memory tools.** These record what an agent did and said. agent2kb compiles
  external sources, and the answers you choose to save, into a shared reference wiki. The two
  approaches complement each other: session memory answers "what was tried", and agent2kb
  answers "what is known".

## Security and privacy

- Compile and query send page text to the configured LLM, and a free Gemini tier may use
  prompts for training. New KBs keep Jina Reader and cloud image OCR **disabled**, and every
  compile and ingest prints a banner that names the effective provider. For sensitive material,
  use `llm.provider: ollama`. Details: [docs/privacy.md](docs/privacy.md).
- Web pages and file downloads pass through an SSRF guard that blocks private addresses,
  checks every redirect and pins the connection. **Video URLs are not covered:** `yt-dlp`
  resolves names and follows redirects itself, so do not ingest video URLs from untrusted
  hosts on a machine that can reach internal services. Over MCP, video URLs are accepted only
  from YouTube or `--ingest-allow-domain` hosts. Details:
  [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) and
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf).
- `claude` and `codex` run without tools and without MCP.
- KB isolation has two layers: a prefix check in agent2kb, and per-KB credentials scoped by the
  storage provider. Only the second layer protects against a leaked key.
  **SeaweedFS versions older than 4.34 are affected by CVE-2026-55874**, which allowed a per-KB
  key to copy another KB's objects; the Docker backend pins version 4.48. See
  [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
- Report vulnerabilities privately: [SECURITY.md](SECURITY.md).

## FAQ

**Is my data used for training?** agent2kb has no telemetry and sends data only to the storage
and the LLM that you configure. Whether an LLM provider trains on prompts is determined by that
provider's policy: a free Gemini tier may do so, whereas a local Ollama server keeps data on
your machine. You remain responsible for holding the rights to the content you ingest.

**Which LLMs are supported?** A logged-in `claude` or `codex` CLI (no API key required),
Gemini, Groq, OpenRouter and Ollama. See [docs/llm-providers.md](docs/llm-providers.md).

**Can it run fully locally?** Yes. Use Ollama for compile and embeddings, SeaweedFS or MinIO
for storage, and enable `privacy_mode`. Your content is then never sent to a third party.

**Does it need a GPU?** No. Only a local Ollama model benefits from one, because compilation
through a CLI or an API runs remotely. Local audio transcription (the `whisper` extra) is
optional.

**Can people edit pages?** Yes, in Obsidian or any Markdown editor. A page is regenerated only
when its raw source is recompiled. For hand-written pages that must never be regenerated, use
`a2kb promote --as wiki-topic`.

**Which languages are supported?** Sources can be in any language. Pages and answers are
written in English unless you set `output_language` to another language, for a KB or for all
of them.

**How do I see what is new?** Open `wiki/index.md`: it starts with an automatic recap of the
last 7 days (see [the example](#a-recap-of-what-is-new-written-for-you)). Set `recent_days`
per KB to change the window or `0` to turn it off. The optional weekly digest
(`scripts/kb-cron-digest.sh`, or `KB_CRON_DIGEST=1` in Docker) writes a longer recap of the
week to `outputs/weekly/`.

## Roadmap

agent2kb is a pre-1.0 release. The CLI, the config formats and the S3 layout are pinned by
contract tests, but some rough edges should be expected.

- Publish the package to PyPI (until then, install from source).
- Verify further storage providers: only SeaweedFS has been tested against a real server, and
  R2, B2, MinIO, AWS and Wasabi follow their documentation.
- Re-run the full `docker compose` flow and an Obsidian sync on several devices for this
  release, including real HTTPS certificates (both were used with earlier versions).
- Not planned: a web interface (reading is done through Obsidian, the CLI or MCP) and agent
  session memory.

## Documentation

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | First run with a sample file: ingest, compile, query, Obsidian, recovery |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian and Remotely Save: keys, sync direction, encryption, timing |
| [docs/configuration.md](docs/configuration.md) | Config file, KBs, admin role, `KB_*` variables, output language |
| [docs/commands.md](docs/commands.md) | Every command and flag |
| [docs/llm-providers.md](docs/llm-providers.md) | Providers, CLI sandboxing, routing and fallback |
| [docs/storage-providers.md](docs/storage-providers.md) | AWS S3 and R2 recipes, B2, MinIO, SeaweedFS, Wasabi, isolation, common errors |
| [docs/docker.md](docs/docker.md) | The Docker backend: compose, Obsidian, agents, HTTPS, key rotation |
| [docs/mcp.md](docs/mcp.md) | MCP server, tools, HTTP mode, security model |
| [docs/privacy.md](docs/privacy.md) | What each stage sends to which third party |
| [docs/project-workflow.md](docs/project-workflow.md) | Daily usage, `source_meta`, cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | How saved answers feed the next compile |
| [docs/operations.md](docs/operations.md) | Backup, manifest, run summaries, lint hygiene |
| [CHANGELOG.md](CHANGELOG.md) | Releases and upgrade notes |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the compatibility contract and code rules;
coding agents should read [CLAUDE.md](CLAUDE.md). Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Trademarks

agent2kb is an independent open-source project. It is not affiliated with, endorsed by or
sponsored by Obsidian, Anthropic, OpenAI, Google, Amazon Web Services, Cloudflare or any other
company or product named in this repository. All product names, logos and trademarks are the
property of their respective owners and are used only to describe compatibility.

## License

agent2kb is licensed under the [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). You may use, modify and redistribute it under those terms; if you modify it
and let others use it over a network (for example as a hosted service or a shared MCP
server), the AGPL requires you to offer them the source of your modified version.

**Commercial licence.** If the AGPL's terms do not suit your use, for example embedding
agent2kb in a closed-source product or service, a separate commercial licence may be
available from the maintainers: see [agent2kb.com](https://agent2kb.com) or write to
[licensing@agent2kb.com](mailto:licensing@agent2kb.com).

Contributions are accepted under the [Contributor Licence Agreement](CLA.md), which lets the
project offer both licences.
