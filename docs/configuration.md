# Configuration

agent2kb manages **knowledge bases (KBs)**: each one is a folder of raw sources and
compiled wiki pages, stored under its own prefix in an S3-compatible bucket. Settings
(storage, LLM, language, privacy) live in one user-level file; a documented set of `KB_*`
[environment variables](#environment-variables) and command-line flags override it.

## Quick start

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
pip install ".[all]"  # source install until the first PyPI release
claude auth login     # or `codex login`: log the LLM CLI in first (not needed for API keys or Ollama)
a2kb setup            # wizard: storage, LLM, language, privacy, your first KB
a2kb list             # the KBs you have
cd ~/research && a2kb new research --here   # (optional) another KB, selected by this folder
a2kb -p research ingest --url https://example.com/article
```

`a2kb setup` writes the config described below with mode `0600`, creates the first KB and
finishes with `a2kb doctor --read-only`. For Docker or CI use `a2kb setup --non-interactive`
(see [Non-interactive setup](#non-interactive-setup)).

## The config file (v2)

Location, first match wins: `--config <path>`, `$KB_CONFIG`, `~/.kb/config.yaml`.
A file is v2 when it says `version: 2` or has a `kbs:` section.

```yaml
version: 2
storage:                 # the default connection, shared by your KBs
  endpoint: https://<account>.r2.cloudflarestorage.com   # leave out for AWS S3
  region: auto
  bucket: my-kb
  access_key: ...        # or leave both keys out: AWS credential chain (env, ~/.aws, role)
  secret_key: ...
  checksum_mode: when_required
llm:
  provider: cli          # cli (claude / codex login), ollama, gemini, groq, openrouter
  command: claude
  # model / effort / aux_model / aux_effort / fallback_command ... are optional
defaults:                # any setting, applied to every KB below
  output_language: en
  allow_jina_reader: false
  allow_cloud_vision: false
kbs:
  research: {}           # prefix is automatic: kbs/research/
  acme:
    prefix: clients/acme/
    storage: {access_key: ..., secret_key: ...}   # only if this KB needs its own keys
    output_language: de
  boss:
    role: admin          # whole-bucket access, see "Admin KBs"
```

**This file is the only place where secrets are stored for KBs created with the v2 format.**
Keep it private: agent2kb creates it with mode `0600`, and `a2kb doctor` warns if the
permissions are wider.

How a KB's settings are put together, later wins:
`defaults` < `storage` < `llm` < the KB's own `storage` < the KB's own `llm` < settings
written directly inside the KB. Then, as for every KB: `KB_*` environment variables, then
command-line flags.

| Block | Keys |
|---|---|
| `storage` | `endpoint`, `region`, `bucket`, `access_key`, `secret_key`, `checksum_mode`, `addressing_style`, `aws_profile` (see [storage providers](storage-providers.md)) |
| `llm` | `provider`, `command`, `model`, `effort`, `aux_model`, `aux_effort`, `fallback_command`, `fallback_model`, `fallback_effort`, `ollama_host`, `groq_api_key` / `openrouter_api_key` / `gemini_api_key` / `openai_api_key` / `voyage_api_key`, embedding settings |
| `defaults`, KB entry | any other KB setting: `output_language`, `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision`, `concept_threshold`, `recent_days`, ... |
| KB entry only | `prefix`, `role`, `storage`, `llm` |

An unknown or misspelled key is reported as an error that names the file and the allowed keys.

KB names are lowercase letters, digits and `-` (2-63 characters, `^[a-z0-9][a-z0-9-]{1,62}$`
for `a2kb new`). The name `super` is reserved for old-style configs.

### Prefixes

A KB without `prefix:` gets `kbs/<name>/`. The prefix is where its `raw/`, `wiki/` and
`outputs/` live; two KBs in one bucket must not overlap (`a2kb new` refuses overlapping
prefixes). Automatic prefixes apply to v2 KBs only: an old profile keeps exactly the
prefix its config names.

### Admin KBs

`role: admin` replaces the reserved profile name `super` in a v2 file: an admin KB can read and write
the whole bucket (including other KBs and `_meta/`) and is what `compile --all-profiles`
needs. An admin KB with no `prefix:` has the empty prefix (whole bucket). Admin KBs are

- only ever read from the user-level file (project config, env vars and flags cannot
  change them or grant the role),
- never selected automatically: `$KB_PROFILE` or a project pointer naming an admin KB is
  ignored, you must pass `-p <name>`,
- not something any command creates for you (`a2kb new` makes members only): edit the file by hand.

## Several KBs with separate buckets (R2)

On providers whose keys cannot be limited to a prefix, such as Cloudflare R2, KBs are isolated
from each other's key holders only by giving each KB **its own bucket and its own key**
([isolation](storage-providers.md#isolation-between-kbs)). The v2 file expresses that with a
`storage:` block inside each KB, which overrides the shared one:

```yaml
version: 2
storage:                  # shared by every KB: the account's S3 endpoint
  endpoint: https://<account-id>.r2.cloudflarestorage.com
  region: auto
  checksum_mode: when_required
llm:
  provider: cli
  command: claude
kbs:
  notes:
    storage:
      bucket: kb-notes
      access_key: <Access Key ID of a token limited to kb-notes>
      secret_key: <its Secret Access Key>
  work:
    storage:
      bucket: kb-work
      access_key: <Access Key ID of a token limited to kb-work>
      secret_key: <its Secret Access Key>
```

Each KB keeps its default prefix (`kbs/notes/` in `kb-notes`, `kbs/work/` in `kb-work`);
prefixes only have to differ within one bucket. After `a2kb setup` the first KB's bucket and
key sit in the shared `storage:` block, which works as well: a KB that names its own bucket
and key overrides them.

**What `a2kb new` does and does not do.** `a2kb new <name>` adds a KB to the **shared**
bucket, signed with the **shared** key: it has no option for another bucket or key. On R2 that
puts the new KB in the first KB's bucket, where the first KB's token can read it. For a KB with
its own bucket, add it by hand instead:

| Who | Step |
|---|---|
| Storage administrator (Cloudflare dashboard) | Create the bucket (`kb-work`); create an API token with **Object Read & Write** limited to that bucket, and copy its Access Key ID and Secret Access Key ([R2 recipe](storage-providers.md#cloudflare-r2)). |
| You | Back up the config (`cp -p ~/.kb/config.yaml ~/.kb/config.yaml.manual-bak`) and add the `work:` entry above to `kbs:` in `~/.kb/config.yaml` (the file stays mode `0600`). |
| You | `a2kb -p work doctor`: the bucket, the prefix listing and the write probe must pass with the new key (a denied bucket check alone is only a warning: doctor then tests the prefix). |
| You | `a2kb -p work init`: creates the KB's skeleton (`raw/`, `wiki/`, `outputs/`, `index.md`, `log.md`, `concepts/`); it only adds what is missing. |
| You | `a2kb list` shows the KB; `a2kb -p work connect obsidian` prints its Obsidian settings (a separate vault and a separate device token per KB, see [obsidian](obsidian.md)). |

`a2kb -p work config show` lists the effective settings with the layer each value comes
from (secrets masked), so you can check that `bucket` and the keys come from
`kbs.work.storage`. With the Docker backend and external storage, see
[docker.md](docker.md#several-kbs-on-external-storage).

## Using a KB from a project folder

Put a **pointer** in the folder: `.kb/config.yaml` containing only

```yaml
kb: research
```

`a2kb new research --here` writes it. If `research` already exists in your user-level file,
the same command only writes the pointer (no S3 call, no change to that file), so it also
attaches an existing KB to a folder. From that folder or any subfolder `a2kb ...` then works
without `-p`. A pointer holds no secrets and may be committed. It may contain nothing but
`kb:`: if it also holds credentials (`access_key`, `secret_key`, `s3_endpoint`,
`aws_profile`, `*_api_key`) agent2kb stops with an error telling you to move them to the
user-level file, and any other key is an error too.

Selection order: `-p/--profile`, then `$KB_PROFILE`, then the nearest `.kb/config.yaml`,
then, if your user-level file defines exactly one KB (and it is not an admin KB), that KB.
After `a2kb setup` you therefore need no `-p` until you add a second KB.

## Environment variables

These `KB_*` variables are read by `a2kb`; each overrides the config file (and a project
pointer), and a command-line flag overrides the variable. Names follow the old flat setting
names, so `KB_COMPILE_PROVIDER` sets `llm.provider`.

| Variable | Setting |
|---|---|
| `KB_CONFIG` | Path of the user-level config file (instead of `~/.kb/config.yaml`) |
| `KB_PROFILE` | Which KB to use (what `-p` sets). Ignored for admin KBs |
| `KB_ACTOR` | `user` or `agent`: who is asking, which decides whether `query` saves by default |
| `KB_S3_ENDPOINT`, `KB_S3_REGION`, `KB_S3_ADDRESSING_STYLE`, `KB_S3_CHECKSUM_MODE` | `storage.endpoint`, `region`, `addressing_style`, `checksum_mode` |
| `KB_BUCKET`, `KB_PREFIX` | `storage.bucket`; the KB's prefix |
| `KB_ACCESS_KEY`, `KB_SECRET_KEY`, `KB_AWS_PROFILE` | `storage.access_key`, `secret_key`, `aws_profile` (see [credentials](#where-credentials-may-come-from)) |
| `KB_COMPILE_PROVIDER`, `KB_DEFAULT_MODEL` | `llm.provider`, `llm.model` |
| `KB_CLI_COMMAND`, `KB_CLI_REASONING_EFFORT` | `llm.command`, `llm.effort` |
| `KB_AUX_MODEL`, `KB_AUX_EFFORT` | `llm.aux_model`, `llm.aux_effort` |
| `KB_CLI_FALLBACK_COMMAND`, `KB_CLI_FALLBACK_MODEL`, `KB_CLI_FALLBACK_EFFORT` | `llm.fallback_command`, `fallback_model`, `fallback_effort` |
| `KB_GROQ_API_KEY`, `KB_OPENROUTER_API_KEY`, `KB_GEMINI_API_KEY`, `KB_OPENAI_API_KEY`, `KB_VOYAGE_API_KEY` | API keys of the corresponding providers |
| `KB_OLLAMA_HOST` | Address of the Ollama server |
| `KB_EMBEDDING_PROVIDER`, `KB_EMBEDDING_MODEL` | `embedding_provider`, `embedding_model` |
| `KB_RERANK_MODEL`, `KB_RERANK_ENABLED` | `rerank_model`, `rerank_enabled` (boolean) |
| `KB_GOOGLE_SAFE_BROWSING_KEY` | `google_safe_browsing_key`, for the pre-ingest URL safety check |
| `KB_OUTPUT_LANGUAGE` | `output_language` |
| `KB_RECENT_DAYS` | `recent_days` (days in the index recap; 0 = off) |
| `KB_PRIVACY_MODE`, `KB_ALLOW_JINA_READER`, `KB_ALLOW_CLOUD_VISION` | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` (boolean); see [privacy](privacy.md) |
| `KB_MCP_TOKEN` | Bearer token for `a2kb mcp --http` ([mcp](mcp.md)) |
| `KB_COMPILE_STRICT` | `1`/`true` makes `a2kb compile` behave as with `--strict`: exit `1` when any attempted file failed or was quarantined, `75` when another compile of the KB was running ([commands](commands.md)) |

Boolean variables accept `1`, `true`, `yes` or `on`, and `0`, `false`, `no` or `off`. Any other
value is rejected with an error, so that a misspelt value cannot silently disable a privacy
switch. Settings that are not listed above (for example `concept_threshold`) can be set only in
the configuration file. The Docker backend has its own variables, which are described in
`.env.example` and [docker](docker.md).

## Setup commands

| Command | What it does |
|---|---|
| `a2kb setup` | First-run wizard. Asks for a storage preset (aws, r2, b2, minio, seaweedfs, wasabi, other), keys (hidden input) or the AWS credential chain, checks the connection and offers to create the bucket (asks first), picks the LLM (detects a logged-in `claude` / `codex` and a running Ollama without reading any token), the output language (default `en`) and two privacy questions, then creates the first KB and runs `a2kb doctor --read-only`. Ingest-time cloud services are **off** unless you opt in: `allow_jina_reader` sends every URL you ingest to jina.ai, `allow_cloud_vision` sends images to a cloud vision model. |
| `a2kb setup --non-interactive` | Same with flags / env and no prompts: `--preset`, `--endpoint`, `--region`, `--account-id`, `--bucket`, `--access-key`, `--secret-key` (or `KB_ACCESS_KEY` / `KB_SECRET_KEY` / `KB_BUCKET` / `KB_S3_ENDPOINT` / `KB_S3_REGION`), `--aws-chain`, `--create-bucket`, `--llm`, `--model`, `--api-key` (or `KB_<PROVIDER>_API_KEY`), `--language`, `--allow-jina` / `--allow-cloud-vision`, `--kb-name`, `--prefix`, `--here`. |
| `a2kb new <name>` | Adds a KB to the v2 file and creates its S3 skeleton (`raw/`, `wiki/`, `outputs/`, `index.md`, `log.md`). `--prefix` picks another prefix, `--here` writes the pointer in the current folder. It never touches existing KBs, and refuses when the prefix already holds objects unless you pass `--adopt` (then nothing existing is overwritten). |
| `a2kb list` | Lists every KB you can use: v2 KBs, old profiles and the current folder's config, with prefix and role (`--json` for scripts). Never prints secrets. |
| `a2kb config show` | The effective settings of the selected KB, secrets masked, and which layer each value came from (default, config file block, project config, env, flag). |
| `a2kb config migrate --profile <p>` | Prints the v2 equivalent of an old profile (dry-run). `--write` appends it to a v2 file after confirmation; `--as <name>` renames it (needed for `super`, which becomes an admin KB). See [Legacy configs](#legacy-v1-configs). |

Running `a2kb setup` against an existing v2 file keeps its `storage`, `llm` and `defaults`
blocks; a flag that contradicts a stored value (for example another `--bucket`) is an error
naming the flag, so nothing is silently ignored. `a2kb setup` checks all inputs before it
creates anything and writes the file once.

`a2kb setup` and `a2kb new` rewrite only v2 files. PyYAML drops comments, so the previous
file is first copied to `config.yaml.bak` (mode `0600`).

## Non-interactive setup

`a2kb setup --non-interactive` never prompts, so it suits Docker, CI and provisioning
scripts. Give the secrets through the environment (flags show up in `ps`):

```bash
export KB_ACCESS_KEY=...  KB_SECRET_KEY=...      # or --aws-chain, see below
a2kb setup --non-interactive \
  --preset minio --endpoint http://minio:9000 --bucket my-kb --create-bucket \
  --llm claude --kb-name notes
```

It fails with a message naming the first missing flag, before anything is created.

| Flag (env) | Meaning |
|---|---|
| `--preset` | `aws`, `r2`, `b2`, `minio`, `seaweedfs`, `wasabi` or `other`. Default: `other` when `--endpoint` is given, else `aws`. |
| `--endpoint` (`KB_S3_ENDPOINT`) | S3 endpoint URL. Not for AWS. R2 builds it from `--account-id`, B2 and Wasabi from `--region`. |
| `--region` (`KB_S3_REGION`), `--account-id`, `--addressing-style` | Provider specifics; each preset has sensible defaults. |
| `--bucket` (`KB_BUCKET`) | Bucket name (3+ characters). Required. |
| `--create-bucket` | Create the bucket if it does not exist; without it a missing bucket is an error. |
| `--access-key` (`KB_ACCESS_KEY`), `--secret-key` (`KB_SECRET_KEY`) | Both or neither. Prefer the env vars. |
| `--aws-chain` | No keys: use the AWS credential chain (env, `~/.aws`, role). |
| `--llm` | Required: `claude`, `codex`, `ollama`, `gemini`, `groq` or `openrouter`. `claude` and `codex` need that CLI logged in (`claude auth login`, `codex login`). |
| `--model`, `--api-key` (`KB_<PROVIDER>_API_KEY`) | Model override; API key for gemini, groq or openrouter. |
| `--language` (`KB_OUTPUT_LANGUAGE`) | Output language, default `en`. |
| `--allow-jina` / `--no-allow-jina`, `--allow-cloud-vision` / `--no-allow-cloud-vision` | Ingest-time cloud services; both are **off** unless you opt in. |
| `--kb-name`, `--prefix` | Name of the first KB (default `notes`) and its prefix (default `kbs/<name>/`). |
| `--here` | Also write the `.kb/config.yaml` pointer in the current folder. |
| `--adopt` | Use a prefix that already holds objects (nothing is overwritten). |

## Where credentials may come from

An empty `access_key`/`secret_key` makes agent2kb sign requests with whatever AWS credentials
your machine has. Because `.kb/config.yaml` is found by walking up from the current
directory, it may come from a repository you cloned, so a project file is not trusted to
decide **where your data goes or whose credentials sign it**:

- `aws_profile` and the AWS credential chain can only be enabled in the user-level file or
  by env/flags, never by a project config.
- A project config may set the storage target (`s3_endpoint`, `region`, `bucket`, `prefix`,
  `addressing_style`) **only if it also carries its own `access_key` and `secret_key`**.
  A file without its own keys that names a target would make agent2kb use keys that are
  not the file's (yours from the user-level config, or your AWS credentials) against a
  host, bucket or prefix the file chose, so agent2kb stops with an error instead.
- Keys you name yourself (`KB_ACCESS_KEY` + `KB_SECRET_KEY`, or `--access-key` +
  `--secret-key`) count as your choice, but only for a project-chosen `bucket`, `prefix` or
  `addressing_style`. A project-chosen `s3_endpoint` or `region` (the host that receives the
  signed requests) always needs the project file's own keys: env/flag keys are never sent to
  a host a repo picked, unless you also set the endpoint/region yourself
  (`KB_S3_ENDPOINT`, `KB_S3_REGION` or the flags).
- A KB defined in your v2 file accepts only a `kb: <name>` pointer in a project folder; an
  old-style (flat or `profiles:`) project config that applies to it is an error.
- No non-admin KB may have an empty (or `/`) prefix, whichever layer sets it.
- A project config cannot set `role`, and empty strings in it never blank out values from
  the user-level config.

Other settings (language, models, privacy flags) can still be overridden by a project
config. Settings that must be shared or hold secrets belong in the user-level file.

A project `.kb/config.yaml` that contains its own credentials is a complete KB definition:
a2kb writes wherever it points. Run `a2kb` only in folders that you trust, as you would with `make`.
`a2kb compile` and `a2kb ingest` print the bucket, endpoint host and prefix they are about to
use, plus the project config file when one contributed values, so you can see it.

## Legacy (v1) configs

These formats keep working exactly as before; nothing is migrated automatically.

- **Flat project config**: `.kb/config.yaml` with `name:`, `s3_endpoint`, `access_key`,
  `secret_key`, `bucket`, `prefix: projects/<name>/`, ... A project folder with its own
  keys is self-contained. (A flat project config without its own keys can no longer set
  the bucket, prefix, endpoint or region of a profile whose keys live in the user-level
  file; see "Where credentials may come from".) Tools that read a key from this file (for example
  `groq_api_key`) keep working because the file is not touched.
- **Multi-profile user file**: `~/.kb/config.yaml` with `profiles:`. The profile named
  `super` keeps its admin powers and is never autodetected.
- A missing setting means the old behaviour, with one exception: Jina Reader and cloud
  vision are allowed and prefixes are exactly what the config says, but a missing
  `output_language` means **English**; set it to choose another language. The
  privacy-first defaults apply only to KBs created with `a2kb setup` / `a2kb new`.
- `KB_*` environment variables, `-p`, `$KB_PROFILE`, autodetect from `name:` and the S3
  layout `projects/<name>/{raw,wiki,outputs}/` are unchanged.

Old and new can coexist: a v2 file may also contain a `profiles:` section, and
`KB_CONFIG=<file>` points agent2kb at a v2 file while your old `~/.kb/config.yaml` stays as
it is (while `KB_CONFIG` is set, `a2kb` reads only that file). `a2kb setup` and `a2kb new` refuse to
modify an old-format file and offer a new path instead.

To move an old profile into a v2 file, `a2kb config migrate` shows the result first:

```bash
cd ~/work/legacy                          # folder with the old .kb/config.yaml
a2kb config migrate --profile legacy        # dry-run, secrets masked
a2kb config migrate --profile legacy --write   # append to the v2 file (asks first)
```

It keeps the same S3 prefix (no object is moved), leaves the old file untouched, and
writes explicit values wherever the v2 file's `defaults` would otherwise change the KB's
behaviour (for example `output_language: en` for a profile that never set one, when the v2
file's default is another language). Switching
the project folder to a `kb: <name>` pointer is a separate, manual step.

## Recent-pages recap

Every compile refreshes `wiki/index.md`, which opens with a "Past N Days" section: a two- or
three-sentence summary of the themes added in that window, written by the KB's aux model, and a
list of the new pages. `recent_days` (env `KB_RECENT_DAYS`) sets N: 7 by default, any value up to
366, or `0` to leave the section out, which also skips its LLM call. The summary is cached and
written again only when the number of pages in the window, or the window itself, changes.

```yaml
defaults:
  recent_days: 7        # the default
kbs:
  course-notes:
    recent_days: 30     # a monthly recap for this KB
  archive:
    recent_days: 0      # no recap
```

## Output language

`output_language` (env `KB_OUTPUT_LANGUAGE`) sets the language of compiled wiki pages,
concept pages, query answers, lint reports and the weekly digest.

| Value | Effect |
|---|---|
| unset, `en`, `en-*`, "English" | English (the default) |
| `de`, `de-*`, "German", "Deutsch" | German |
| `es`, `es-*`, "Spanish", "Español", "Castellano" | Spanish |
| `fr`, `fr-*`, "French", "Français" | French |
| `pt-BR`, `pt`, "Portuguese", "Português", "Brazilian Portuguese" | Brazilian Portuguese |
| `ja`, `ja-*`, "Japanese", "日本語" | Japanese (です・ます style) |
| `ko`, `ko-*`, "Korean", "한국어" | Korean (합니다체 style) |
| `zh-TW`, `zh-Hant`, `zh-Hant-TW`, "Traditional Chinese", "繁體中文" | Traditional Chinese (Taiwan usage) |
| `zh-HK`, `zh-Hant-HK` | Traditional Chinese (Hong Kong usage) |
| `zh-CN`, `zh`, `zh-Hans`, `zh-SG`, "Simplified Chinese", "简体中文" | Simplified Chinese |
| `yue`, `zh-yue`, "Cantonese" | Cantonese, written in Traditional Chinese |
| anything else | passed to the model as "write in <value>" |

Every language other than English keeps common English IT terms (Docker, API, pipeline)
untranslated, writes bilingual `[[English / local]]` wikilinks for concepts that have both
names, and gets lint-report headings in the form "local (English)".

Names are case-insensitive, and spaces or `_` count as `-` (`pt_br` is `pt-BR`); `en-*`
means any region tag such as `en-GB`. Simplified-to-Traditional conversion (OpenCC) is applied to
compiled output and ingested text only when the language is `zh-HK` or `zh-TW`.
`a2kb status` shows the effective language.

### Limitation: Claude Code managed instructions

With `llm.provider: cli` and `llm.command: claude` (or a Claude `llm.fallback_command`), agent2kb
replaces Claude Code's system prompt with its own. If the account is under an
organisation policy that injects managed instructions (for example "always answer in
language X"), Claude Code applies them on top of that prompt, so they can win over
`output_language`. agent2kb does not try to bypass managed policy. If your output comes
back in the wrong language, check the account's managed instructions, or use a different
CLI (`codex`) or provider for that KB. `a2kb status` prints a reminder when this
combination is configured.
