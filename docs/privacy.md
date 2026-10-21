# Privacy

agent2kb's pipeline crosses several trust boundaries. This document spells out which step sends what to which third party, and how to restrict the pipeline for sensitive projects. agent2kb itself has no telemetry: content is sent only to the storage and the services that you configure.

## TL;DR

| Stage | Default Provider | What is sent | How to make it local |
|-------|------------------|--------------|----------------------|
| URL extraction | r.jina.ai (Jina Reader), only if `allow_jina_reader` is on (**off** for KBs created with `a2kb setup` / `a2kb new`) | Full URL + extracted page text | `allow_jina_reader: false` |
| Entity extraction at ingest | Same as compile | First 4000 characters of each ingested source (one short LLM call per ingest, also for `a2kb mcp --allow-write` `ingest_url`) | `llm.provider: ollama`, or `privacy_mode: true` (then skipped unless the provider is local) |
| Image OCR / vision | The KB's `claude` / `codex` CLI (with `llm.provider: cli`), then Gemini, then the Claude CLI; only if `allow_cloud_vision` is on (**off** for new KBs) and `privacy_mode` is off | Image bytes | `allow_cloud_vision: false` |
| Compile | The configured `llm.provider`: `cli` (your `claude` / `codex` login), Groq, Gemini, OpenRouter, Ollama | Raw source body | `llm.provider: ollama` |
| Embeddings | Auto-picked: Gemini > OpenAI > Ollama (unless `embedding_provider` is set) | Page bodies | `embedding_provider: ollama` |
| Query | Same as compile (LLM rerank + answer) | Page snippets + question | `llm.provider: ollama` |
| Weekly digest (`kb-cron-digest.sh`, opt-in) | The KB's provider (aux routing): with `provider: cli` the CLI and its fallback CLI; an API or Ollama provider is used alone, never falling back to another provider | Titles, first paragraphs and entity counts of the week's pages, plus last week's digest | `llm.provider: ollama`, or leave the digest off |

A KB created the old way (an old-style `profiles:` entry that sets none of these) keeps the original behaviour: Jina Reader and cloud vision allowed. See [configuration](configuration.md#legacy-v1-configs).

The `a2kb compile` and `a2kb ingest` commands print a one-line **privacy banner** at the start of every run, naming the effective provider. If a sensitive KB would be compiled by a cloud provider, this banner is the final warning before content is sent.

## Video URLs (yt-dlp)

`a2kb ingest --url` hands video URLs to `yt-dlp`, which resolves names and follows redirects
itself, outside agent2kb's SSRF guard (`agent2kb.net_guard`): agent2kb checks only the first
host, so a video site that redirects can make this machine fetch from wherever it points.
Downloads are capped with yt-dlp's `--max-filesize 1G` (applied when the size is known in
advance) and a 600-second timeout. Over MCP, video URLs are accepted only from YouTube or
from `--ingest-allow-domain` hosts (see [MCP fetch safety](mcp.md#fetch-safety-ssrf)). Do not
ingest video URLs from hosts you do not trust on a machine that can reach internal services.

## Cloud LLM considerations

- **Gemini free tier**: Google's policy states that prompts and responses on the free Gemini API may be used to improve its products, including training. For content that must not become part of a future training set, use the paid Vertex or Gemini API, or switch to **Ollama** for full privacy.
- **Groq**: the provider states a zero-data-retention policy, but the request still leaves your network. Review the provider's terms before using it for regulated content.
- **OpenRouter**: passes requests through to upstream providers (Anthropic, Mistral, etc.). Each upstream provider has its own retention policy. Avoid OpenRouter for sensitive data.
- **Claude / Codex CLI** (`llm.provider: cli`): uses your own `claude` or `codex` login. The retention terms are those of the plan under which you are authenticated. agent2kb runs them as isolated text transformers (no tools, no MCP, no settings, read-only sandbox) because compile feeds them untrusted web content; see [LLM providers](llm-providers.md#cli-subscription-mode-and-sandboxing).

## Per-KB controls

A fully local KB in your `~/.kb/config.yaml` ([configuration](configuration.md)):

```yaml
version: 2
llm:
  provider: ollama                  # local compile, query and entity extraction
  ollama_host: http://localhost:11434
kbs:
  sensitive:
    embedding_provider: ollama      # local embeddings
    allow_jina_reader: false        # no URL extraction via r.jina.ai
    allow_cloud_vision: false       # no Gemini/Claude/Codex image OCR
    privacy_mode: true              # master switch, see below
```

The same three switches can be set per run with `KB_PRIVACY_MODE`, `KB_ALLOW_JINA_READER` and
`KB_ALLOW_CLOUD_VISION` (boolean), or per KB in the `defaults:` block.

Image OCR through your own `claude` or `codex` login counts as cloud vision: the image still
goes to Anthropic or OpenAI, so it follows `allow_cloud_vision` and `privacy_mode` like Gemini
does. With OCR off, an image is stored as a placeholder naming the file and its size.

`privacy_mode: true` forces the two side-channel flags (`allow_jina_reader`,
`allow_cloud_vision`) off regardless of their individual values, and skips ingest-time entity
extraction unless the compile provider is local (Ollama), so nothing from an ingested source
reaches a cloud LLM at ingest. It does **not** change `llm.provider`: compile itself still
follows it. If the KB is also on `llm.provider: gemini`, the privacy banner will warn that
compile is going to a cloud provider; switch the provider explicitly.

## What the compounding loop captures

`outputs/` files are LLM-generated answers. If your compile provider is a cloud service, those saved answers were generated with cloud-side context. They are then **fed back into future compile runs** as derived context. This is the compounding loop, and it is intentional.

Implications:

- A leaked output may contain phrasing that the LLM derived from several raw sources. Treat `outputs/` with the same sensitivity as `raw/`.
- The compile prompt explicitly labels prior outputs as **derived** so that they are not cited as evidence, although they can still influence wording.
- For a sensitive project, run `a2kb query --no-save` to skip the capture, or use `llm.provider: ollama`.

## Source verification

The following safeguards are always active in the compile and query paths:

- **URL safety**: compile expands shorteners and flags suspicious domains in compiled wikilinks (`sanitize_wiki_urls` in `agent2kb/utils.py`).
- **Visible source line**: articles must carry a `> Source:` line in the body that links to the original URL, so that a reader can verify provenance without parsing YAML. Lint flags missing ones; `a2kb backfill-source --apply` adds them.
- **`source_kind` trust labels**: derived pages (concepts, query_outputs) are marked as such on frontmatter, so that downstream readers and the compile prompt can weigh them below primary sources.
