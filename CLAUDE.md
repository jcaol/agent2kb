# CLAUDE.md

Guide for coding agents (and humans) working on this repository. For setup and the
contribution process see [CONTRIBUTING.md](CONTRIBUTING.md); for what the tool does see
[README.md](README.md).

## What this is

agent2kb is a headless knowledge compiler: sources are ingested into `raw/`, an LLM compiles
them into interlinked Markdown pages in `wiki/`, and agents (CLI, MCP) and humans
(Obsidian) read the result. Each knowledge base (KB) lives under its own prefix in an
S3-compatible bucket. It follows Karpathy's LLM-wiki pattern.

## Build and test

```bash
pip install -e ".[dev,all]"
pytest -q                       # tests/integration/ skips itself without KB_IT_S3_ENDPOINT
ruff check .                    # line length 100
a2kb --help                     # the entry point is agent2kb.main:app
```

While developing, run the code you are editing (`python -m agent2kb.main ...` from the
checkout), not an `a2kb` that may be installed elsewhere on the machine and pointed at real
data. Never run tests or experiments against a bucket that holds real knowledge.

## Architecture

```
ingest (agent2kb/ingest/)  ->  raw/   ->  compile (agent2kb/compile/)  ->  wiki/
                                                                         |
            outputs/  <-  query (agent2kb/query.py), mcp (agent2kb/mcp_server.py)
               |
               +--> fed back into the next compile as derived context
```

- **Ingest** detects the input type (URL, video, audio, document, image, markdown) and
  writes a file with YAML frontmatter to `raw/`. Handlers live in
  `agent2kb/ingest/handlers/`; shared logic in `ingest/base.py`; URL safety in
  `ingest/safety.py`.
- **Compile** turns raw files into wiki pages, incremental by default. The provider
  abstraction is in `compile/compiler.py`; prompts and page schema in `compile/schema.py`.
  Providers: `cli` (the `claude` or `codex` CLI), Groq, OpenRouter, Gemini, Ollama.
- **Query** searches the wiki (keyword, optional embeddings and rerank) and asks the LLM
  to answer with citations. Saved answers land in `outputs/` and feed the next compile
  labelled as derived context, never as a primary source.
- **MCP** (`agent2kb/mcp_server.py`) serves one KB read-only by default; `ask` and
  `ingest_url` are opt-in flags.
- **Config** (`config.py`, `config_v2.py`) merges flags, `KB_*` env vars, the project
  `.kb/config.yaml` pointer and the user-level `~/.kb/config.yaml` (format v2: `storage`,
  `llm`, `defaults`, `kbs`; `role: admin` marks a whole-bucket KB). Old `profiles:` and flat
  files stay supported as legacy. See `docs/configuration.md`.
- **Docker backend**: `docker-compose.yml`, `Dockerfile` and `agent2kb/bootstrap.py` (the
  hidden `a2kb bootstrap` commands compose runs: keys, tokens, provisioning, crontab, health);
  `agent2kb/connect.py` prints the Obsidian and agent settings. See `docs/docker.md`.
- **Ops**: `doctor.py` (health checks), `ops.py` (run summaries), `manifest.py`
  (metadata cache), `status.py`, `remove.py` (`a2kb rm`), `outputs.py`, `promote.py`.

### Storage model

One bucket, prefix-isolated per KB: `<prefix>raw/`, `<prefix>wiki/`, `<prefix>outputs/`,
plus `_meta/` (schema prompts, logs) which is admin-only. Every wiki page carries YAML
frontmatter (`title`, `source`, `date`, `tags`, `source_kind`, ...). The layout and file
naming are a compatibility contract (see below).

### LLM providers and the CLI runner

`agent2kb/cli_runner.py` runs the `claude` and `codex` CLIs as isolated text transformers:
Claude Code with a replaced system prompt, no tools, no settings sources, no MCP, no
session persistence, from an empty temporary directory; codex with `--sandbox read-only`
and the prompt on stdin. Both are coding agents and compile feeds them untrusted web
content, so they must never be called without these flags. Organisation-level managed
instructions still apply to Claude Code; they are admin policy and are not bypassed.

Each KB routes two purposes: `compile` (wiki pages) and `aux` (concept pages, index
summary, lint reports, digest). `CliRouter` adds an optional fallback CLI: a hard-down
primary (credits, quota, auth, unknown model) switches to the fallback for the rest of the
run; when both are down compile stops with `ProviderExhaustedError`. `agent2kb/cli_models.py`
owns model policy (per-CLI defaults, retired slugs, family-aware resolution).

All providers must handle rate limits with exponential backoff and automatic fallback.

## Rules

1. **S3 only through `agent2kb/s3_client.py`.** It enforces the KB's prefix on every
   operation. Never call boto3 from a command handler.
2. **`_meta/` is admin-only.** A member KB (anything without `role: admin`) must never read
   or write there.
3. **CLI LLM calls only through `agent2kb/cli_runner.py`**, with its isolation flags. Never
   spawn `claude` or `codex` directly.
4. **Shell scripts never name models.** They call
   `python3 -m agent2kb.llm_run --profile <p> --purpose aux` (prompt on stdin, text on
   stdout; exit 3 means every CLI is down). A hard-coded model name goes stale silently.
5. **Privacy warnings.** Anything that sends content to a third party (cloud LLM, Jina
   Reader, cloud vision, embeddings) must be reflected in `docs/privacy.md` and surface a
   warning to the user. New KBs are privacy-first: those paths are opt-in. Sensitive KBs
   should be able to run fully local (Ollama).
6. **Every outbound fetch goes through `agent2kb/net_guard.py`** (`safe_get`): it blocks
   private and loopback addresses, validates every redirect hop, pins the connect IP and
   caps the response size. Do not call `httpx` or `requests` directly for user-supplied
   URLs.
7. **Escape console output.** Text that came from a page, a file, an error or a config
   value must go through `rich.markup.escape` before it reaches `console.print`, or it can
   be interpreted as markup.
8. **Optional dependencies** are imported lazily with `agent2kb.extras.import_optional`, so a
   missing package says which extra to install. The core install stays small.
9. **Secrets** live only in the user-level config (mode 0600) or the environment. Never log
   them, never put them in a project-level config, and keep them out of error messages.
10. **Logs and diagnostics go to stderr.** In stdio MCP mode stdout carries the protocol.

## Naming

The project, distribution and Python package are `agent2kb`; the console script is `a2kb`
(there is no bare `kb` entry point). "KB" still means knowledge base everywhere else: the
`.kb/` and `~/.kb/` config locations, the `KB_*` environment variables, the `kbs/<name>/`
S3 prefix, the `kb-*` cron wrappers in `scripts/` and the frontmatter `source` labels
(`kb query`, `kb stubs`, `kb digest`) are data contracts that existing KBs depend on.
Say `a2kb` when you mean the command.

## The compatibility contract

`tests/test_contract.py` pins the public surface: the `a2kb` command and its flags and exit
codes, KB selection, config formats and `KB_*` variables, the S3 layout, raw file
naming, frontmatter fields, and `python3 -m agent2kb.llm_run`.

- Do not break it without a deprecation path: keep the old behaviour, warn, and record it
  in `CHANGELOG.md`.
- A missing new config field must mean the old behaviour.
- If a contract test has to change, say why in the commit message. A changed assertion is
  a breaking change.
- New behaviour belongs behind new config or new commands, applied to KBs created the new
  way.

## Testing notes

- Unit tests use moto for S3 and never need credentials or network access.
- Tests that need a missing optional SDK simulate its absence.
- Use neutral example names in tests, docs and prompts (`notes`, `alpha`, `example.com`),
  never real projects, people, hosts or paths.
- Integration tests (`tests/integration/`) need a throwaway S3 endpoint
  (`KB_IT_S3_ENDPOINT`); they create and delete their own prefix.

## Docs

`README.md` is the front door and stays short; detail lives in `docs/` (commands,
configuration, providers, storage, Docker, MCP, privacy, operations). When you change a
command, flag, config key or `KB_*` variable, update the page that documents it and
`CHANGELOG.md`. Examples use `a2kb`, `example.com`, KB names like `notes` and `alpha`, and the
v2 config; the old formats appear only in the legacy sections.

`README.md` is translated into `README.<lang>.md` (ja, de, es, pt-BR, fr, ko, zh-TW); the English
file is the reference. When the README changes, update the translations in the same change, or
at least keep their headings, anchors, tables and code blocks in step.

## Style

Match the surrounding code: naming, comment density, idiom. Comments explain why, not
what. Ruff must be clean (line length 100). Keep commits focused and describe behaviour
changes in `CHANGELOG.md`.
