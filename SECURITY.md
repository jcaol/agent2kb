# Security policy

## Reporting a vulnerability

Please report security problems **privately** through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a vulnerability**
(a GitHub security advisory). Do not open a public issue or pull request for a
vulnerability.

Please include the version (`a2kb --version`), the steps you performed, the expected result
and the actual result. A proof of concept is helpful. This is a volunteer-run project: the
maintainers aim to acknowledge a report within 7 days and will keep the reporter informed about
the fix.
Reporters are credited in the advisory unless they ask not to be.

Only the latest release receives security fixes.

## Scope

In scope:

- Bypassing the prefix enforcement in `agent2kb/s3_client.py` (a member KB reading or
  writing outside its own prefix, `kbs/<name>/` by default, or touching `_meta/`), or a
  project config or environment variable granting `role: admin`
- Escaping the isolation of the CLI providers in `agent2kb/cli_runner.py` (the claude or
  codex CLI reading files, running tools or reaching MCP servers while compiling
  untrusted content)
- Credential leakage: keys written to logs, error messages, wiki pages or S3
- SSRF or unsafe fetching in URL ingest (`agent2kb/ingest/safety.py`, `utils.validate_url`)
- Path traversal in ingest, backup, restore, `a2kb rm` or the MCP server
- Prompt-injection paths that let ingested content change what agent2kb does (as opposed
  to what the model writes)

Out of scope:

- Weaknesses in the S3 server, the LLM provider or the claude/codex CLIs themselves
  (report those upstream)
- A user who points a KB at an LLM provider they do not trust, or who disables a
  safety setting such as `privacy_mode` or enables `allow_jina_reader`
- Wiki content that is wrong or misleading because the source was (LLM output is not
  verified)
- Denial of service through very large inputs on your own machine

## What to know about data flow

- **`a2kb compile`, `a2kb query` and `a2kb ingest` send content to the LLM provider you
  configure.** With a cloud provider (Groq, Gemini, OpenRouter, or the claude/codex
  CLI under your login) that content leaves your machine. Use `llm.provider: ollama`
  for sensitive material. See [docs/privacy.md](docs/privacy.md).
- **CLI providers are sandboxed.** `agent2kb/cli_runner.py` runs `claude` with a replaced
  system prompt, no tools, no settings sources, no MCP and no session persistence, from
  an empty temporary directory, and `codex` with `--sandbox read-only` and the prompt on
  stdin. Both are coding agents and compile feeds them untrusted web content; never call
  them without these flags.
- **Credentials** live in the user-level `~/.kb/config.yaml` (written with mode 0600) or in
  `KB_*` environment variables. A project folder only holds a `kb: <name>` pointer; never
  commit a `config.yaml` that contains keys. Rotate any key that has been committed, even
  briefly.
- **KB isolation** is enforced twice: by the storage provider's credential scope (IAM
  policy, application key, SeaweedFS identity) and by the prefix check in agent2kb.
  Configure both; the prefix check alone is not a security boundary against someone who
  has the S3 keys. Keep the key of an admin KB (`role: admin`, whole bucket) out of
  project folders and agent hosts.
- **SeaweedFS older than 4.34** is affected by CVE-2026-55874: a key limited to one KB
  could copy another KB's objects. The Docker backend pins 4.48; see
  [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
