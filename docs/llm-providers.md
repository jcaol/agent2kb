# LLM providers

agent2kb sends page text to an LLM at compile and query time. You choose where in the `llm:`
block of your [config](configuration.md#the-config-file-v2) (`a2kb setup` writes it for you).

| `llm.provider` | Needs | Note |
|---|---|---|
| `cli` | a logged-in `claude` or `codex` CLI | Uses your subscription login, no API key. Optional fallback CLI. |
| `ollama` | a local Ollama server | Fully local and private. |
| `gemini`, `groq`, `openrouter` | an API key | Cloud: your content leaves your network. |

```yaml
llm:
  provider: cli
  command: claude              # or codex
  model: claude-haiku-5-5      # optional: the CLI's own default otherwise
  effort: high
  aux_model: claude-sonnet-5-5
  aux_effort: medium
  fallback_command: codex      # optional: used when the primary is hard-down
```

Install and log in the CLI first (`npm i -g @anthropic-ai/claude-code`, `claude auth login`;
or `npm i -g @openai/codex`, `codex login`). `a2kb doctor` reports a missing login as a
failure until you have done that.

## Ollama

```yaml
llm:
  provider: ollama
  model: llama3.3:70b                  # any model `ollama list` shows
  ollama_host: http://localhost:11434
```

Without `model:` an Ollama KB uses `llama3.3:70b` (and `a2kb setup --llm ollama` writes that
name), a 70B model that needs tens of GB of disk and memory; pick a smaller one if your
machine cannot hold it. Pull the model first (`ollama pull <model>`). `a2kb doctor` fails
when the configured model is not installed, and warns separately when the embedding model
(`bge-m3` unless `embedding_model` says otherwise) is missing. Embeddings are optional: without
them queries use keyword search only.

## Two purposes

Each KB routes two kinds of work: `compile` (wiki pages; `model` and `effort`) and `aux`
(concept pages, the index summary, lint reports and the weekly digest; `aux_model` and
`aux_effort`, which default to the compile pair). This allows a less expensive model to perform the bulk of the work.
Shell scripts never name models; they call
`python3 -m agent2kb.llm_run --profile <kb> --purpose aux` (prompt on stdin, text on stdout;
exit code 3 means every CLI is down), which uses the same routing. With `provider: cli` it goes
through the CLI and its fallback; with an API provider or Ollama it makes the same call
compile's aux work makes, but to that provider only: unlike compile, it never tries another
API or local provider, even one with a key (exit code 3 then means that provider is
hard-down).

`a2kb status` shows the compile, aux and fallback targets. An old-style profile that names no
provider keeps its original default (Groq); `a2kb setup` writes an explicit choice for new KBs.

## CLI subscription mode and sandboxing

`claude` and `codex` are coding agents, and compile feeds them web content you do not control.
`agent2kb/cli_runner.py` therefore runs them as text-in, text-out transformers:

- Claude Code with a replaced system prompt, no tools, no settings sources, no MCP and no
  session persistence, started in an empty temporary directory;
- codex with `--sandbox read-only`, its features locked down and the prompt on stdin.

Neither CLI inherits agent2kb's own secrets: every `KB_*` variable, `AWS_*` (agent2kb's S3
credentials), `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`,
`VOYAGE_API_KEY` and `GOOGLE_SAFE_BROWSING_KEY` are removed from their environment, and
`OPENAI_API_KEY` from Claude Code's. Each CLI keeps the rest, including its own login variables
(`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY` for codex, `CODEX_HOME`, ...)
and proxies; Claude Code keeps `AWS_*` when `CLAUDE_CODE_USE_BEDROCK` is set.

If a run's output contains one of the KB's own secrets it is refused. If the primary CLI is
hard-down (quota, credits, auth, unknown model) the run switches to the fallback CLI for the
rest of the run; a timeout retries only that item on the fallback. If both are down, compile
stops instead of attempting every remaining file. Check the terms of your CLI
subscription before automating it.

Organisation-managed instructions on a Claude account still apply on top of agent2kb's prompt
and can override `output_language`; see
[configuration](configuration.md#limitation-claude-code-managed-instructions).

## Privacy

Compile and query send page text to whichever provider is configured, and a free Gemini tier
may use prompts for training. What goes where, and how to lock a KB down: [privacy](privacy.md).
