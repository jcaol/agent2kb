# First run: from one Markdown file to a page in Obsidian

This walk-through takes a small sample file that ships with the repository,
[`examples/first-note.md`](../examples/first-note.md), through the whole pipeline: ingest,
compile, query, Obsidian. The sample is fictional and its facts are fixed, so you can check each
result by eye. It assumes:

- agent2kb is installed and `a2kb --help` works (README, [Install](../README.md#install));
- `a2kb setup` has finished and created a KB, here called `notes` (the wizard's default name);
- `a2kb doctor` reports no failures.

Run the commands from the root of your checkout, so that `examples/first-note.md` is found.
While you have only one KB, `-p notes` can be left out.

## 1. Ingest the sample

```bash
a2kb -p notes ingest --file examples/first-note.md
```

**Expected:** one line such as

```
✓ Ingested file → kbs/notes/raw/YYYYMMDD-Alpha-Project-Facts-1a2b3c4d.md
```

The raw file name is the date of the ingest, the title taken from the file's first `# ` heading
(`Alpha Project Facts`, spaces replaced by `-`), and the first eight hexadecimal characters of a
hash of the content. `kbs/notes/` is the KB's prefix; it differs if you chose another one.
Ingesting the same file again on another day creates a second raw file, because the date is
part of the name.

## 2. Compile

```bash
a2kb -p notes compile
```

**Expected:** a privacy banner naming the LLM provider, then
`✓ Compiled 1 files (...s, ... tokens)` and one line for the sample. The page is written to
`kbs/notes/wiki/` under **the same file name as the raw file**
(`wiki/YYYYMMDD-Alpha-Project-Facts-<hash>.md`), and `wiki/index.md` and `wiki/log.md` are
updated. Depending on the configuration, compile may also create concept pages under
`wiki/concepts/`. Running `compile` again without changes does nothing for this file
(incremental compile).

A compile needs a working LLM. If it fails, see [Recovery](#recovery).

## 3. Query

```bash
a2kb -p notes query "What is the codename of the alpha project?"
```

**Expected:** an answer that says **Lantern**, with a numbered citation such as `[1]`, followed
by a `## Sources` section whose entry links the page from step 2:
`` - `[1]` [[YYYYMMDD-Alpha-Project-Facts-<hash>|...]] ``. The exact wording of the answer
depends on the model. Because you typed the query yourself, the answer is also saved to
`outputs/<timestamp>-<question>-<id>.md` (`--no-save` skips that), and the next compile reads it as
derived context.

## 4. Look at it in Obsidian

```bash
a2kb -p notes connect obsidian --show-secret
```

prints the settings for the Remotely Save plugin. Follow [obsidian.md](obsidian.md): create a
**new, empty vault**, enter the settings including the remote prefix (`kbs/notes/`), leave the
plugin's encryption password empty, and run a **manual sync**.

**Expected:** the vault contains `raw/`, `wiki/` and `outputs/`. Open
`wiki/YYYYMMDD-Alpha-Project-Facts-<hash>.md`: the compiled page about the alpha project, with
frontmatter (`title`, `source`, `date`, `tags`, `source_kind`) and `[[wikilinks]]`. Your saved
answer from step 3 is in `outputs/`.

## 5. Sync, compile, sync

Compile runs on the machine with agent2kb (by hand or by cron); sync runs only while Obsidian is
open. Check the round trip once before you turn on automatic sync:

1. Ingest another file (or edit `examples/first-note.md` and ingest it again), then
   `a2kb -p notes compile`.
2. Run a manual sync in Obsidian. The new page appears in `wiki/`.

[obsidian.md](obsidian.md#6-first-check-sync-compile-sync) has the full check, including notes
and Web Clipper files written on the device.

## Recovery

| Symptom | Likely cause and fix |
|---|---|
| `a2kb: command not found` | The virtual environment is not active in this terminal. Run `source .venv/bin/activate` in the checkout (README, [Install](../README.md#install)), or call `.venv/bin/a2kb` directly. |
| `externally-managed-environment` from `pip` | You are installing into the system Python. Create and activate a virtual environment first (README, [Install](../README.md#install)). |
| `the access key is not recognised`, `the secret key does not match the access key` | Wrong key, a key for another account or endpoint, or (R2) a Cloudflare API token value instead of the S3 Access Key ID and Secret Access Key pair. See [storage providers](storage-providers.md#common-errors). |
| `Access denied (403)` | The key does not cover this bucket or this KB's prefix (`kbs/notes/`). A key limited to one prefix is denied the whole-bucket check but works when it may list the prefix; it cannot create a missing bucket. See the AWS recipe in [storage providers](storage-providers.md#aws-s3). |
| `Bucket '...' does not exist` | Create it at the provider, or with `a2kb doctor --fix`. |
| `File not found: examples/first-note.md` | Run the command from the root of the checkout, or give the full path. |
| `doctor` fails on the `claude` or `codex` login | Log in first: `claude auth login` or `codex login`. |
| Compile fails with an unknown model, quota or credit error | Check `llm:` in the config (`a2kb config show`) and the provider's account; see [LLM providers](llm-providers.md). With Ollama, the model must be pulled first (`ollama pull <model>`). |
| The page is missing in Obsidian | No sync has run since the compile, the remote prefix is wrong, or Obsidian was closed. Run a manual sync; see [obsidian.md](obsidian.md#troubleshooting). |
| The answer does not mention Lantern | Check that step 2 compiled the sample (`wiki/log.md`), then ask again. Answers come from the model and can vary. |

When the first run works, the reference pages take over: [commands](commands.md),
[configuration](configuration.md), [daily workflow](project-workflow.md) and
[operations](operations.md).
