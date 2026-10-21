#!/usr/bin/env bash
# kb-cron-digest.sh — Weekly KB consolidation digest via the profile's LLM CLI
#
# Usage: kb-cron-digest.sh <project-dir> <profile-name>
# Example: kb-cron-digest.sh /path/to/myproject myproject
#
# Unattended pipeline:
#   1. Gather 7-day activity (recent raw/, wiki/, concepts/, outputs/)
#   2. Build a digest prompt and pipe it to `python3 -m agent2kb.llm_run`,
#      which uses the profile's provider: for `cli`, its `aux_model` /
#      `aux_effort` (falling back to the compile model) through the same
#      isolated CLI and fallback chain compile uses; for an API or Ollama
#      provider, the same call compile's aux work makes. Both the snapshot and
#      the LLM call run under KB_PYTHON and resolve the profile the way `a2kb`
#      does. The model is deliberately NOT named in this script: a
#      hard-coded model name goes stale silently when the model is retired,
#      while the Python side migrates.
#   3. The model returns a markdown body
#   4. `a2kb promote --as weekly` lands it at outputs/weekly/<stamp>-<slug>-<id>.md
#
# Designed to run from supercronic — KB_ACTOR=agent is forced so the
# resulting save records `source_meta.actor=agent` regardless of the
# cron environment's TTY state.
#
# Environment overrides:
#   A2KB_BIN  — absolute path to the `a2kb` executable (the older KB_BIN is still read).
#               Defaults to PATH lookup.
#   KB_PYTHON — interpreter that can import agent2kb. Defaults to the one the
#               `a2kb` executable runs under (its shebang, an `env` shebang's
#               interpreter or pip's `#!/bin/sh` trampoline) if that imports
#               agent2kb, else python3 with a warning in the log.
#   HOME      — required by the claude / codex CLIs to find their logins.
#               Cron/supercronic MUST pass this; the script will refuse to
#               guess a user.
#   TMPDIR    — where the private work folder is made (default /tmp).
#
# Logs to <project-dir>/kb-digest.log with timestamps.
#
# The digest is drafted in a private folder (0700, files 0600) under TMPDIR that is removed
# on exit. If promote fails, the folder is kept for debugging and its path is logged.

set -euo pipefail
# The draft holds KB content: keep every file this script creates private.
umask 077

# Cron-resilient env. Refuse to guess HOME — the cron unit / supercronic
# environment is responsible for setting it. Falling back to a hard-coded
# username silently lets the script run as the wrong user when something
# else is mis-configured.
: "${HOME:?HOME must be set in the cron environment (needed by the LLM CLI to find its login)}"
export PATH="${PATH:-${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin}"

PROJECT_DIR="${1:?Usage: $0 <project-dir> <profile-name>}"
PROFILE="${2:?Usage: $0 <project-dir> <profile-name>}"
# Absolute, so the log path still points into the project after the `cd` below.
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
LOG="${PROJECT_DIR}/kb-digest.log"
KB="${A2KB_BIN:-${KB_BIN:-$(command -v a2kb 2>/dev/null || echo a2kb)}}"

# Print the interpreter the `a2kb` executable runs under, read from its first lines:
#   #!/venv/bin/python [args]                    the interpreter itself
#   #!/usr/bin/env [-S] python3 [args]           the named interpreter, looked up on PATH
#   #!/bin/sh, then '''exec' <python> "$0" "$@"  pip's trampoline for long venv paths
a2kb_interpreter() {
    local first cand word
    local -a words
    first="$(head -n 1 "$KB" 2>/dev/null || true)"
    case "$first" in
        "#!"*) first="${first:2}" ;;
        *) return 0 ;;
    esac
    read -r -a words <<<"$first" || true
    [ "${#words[@]}" -gt 0 ] || return 0
    cand="${words[0]}"
    case "${cand##*/}" in
        env)
            cand=""
            for word in "${words[@]:1}"; do
                case "$word" in
                    -* | *=*) continue ;;
                    *) cand="$(command -v "$word" 2>/dev/null || true)"; break ;;
                esac
            done
            ;;
        sh | bash | dash)
            cand="$(sed -n "2s/^'''exec' \(.*\) \"\$0\" \"\$@\"\$/\1/p" "$KB" 2>/dev/null || true)"
            cand="${cand#\"}"
            cand="${cand%\"}"
            ;;
    esac
    printf '%s\n' "$cand"
}

# Same interpreter as `a2kb` itself, so agent2kb is importable without relying on cron's
# PYTHONPATH. An explicit KB_PYTHON is used as given; a derived one must import agent2kb,
# else python3 is used and the log says so.
KB_PYTHON_WARNING=""
if [ -z "${KB_PYTHON:-}" ]; then
    KB_PYTHON="$(a2kb_interpreter)"
    if [ -z "$KB_PYTHON" ] || ! "$KB_PYTHON" -c 'import agent2kb' >/dev/null 2>&1; then
        KB_PYTHON_WARNING="no interpreter that imports agent2kb found for ${KB}; using python3 (set KB_PYTHON to choose one)"
        KB_PYTHON=python3
    fi
fi

# ISO week number (used in the title and filename slug) — gives a
# deterministic, dedup-friendly identifier across regenerations.
WEEK="$(date -u +%V)"
YEAR="$(date -u +%Y)"
TODAY="$(date -u +%Y-%m-%d)"
# GNU date (Linux) takes -d, BSD date (macOS) takes -v.
START_DATE="$(date -u -d '7 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-7d +%Y-%m-%d)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/kb-digest.XXXXXX")"
KEEP_WORKDIR=0
cleanup() {
    if [ "$KEEP_WORKDIR" = 1 ]; then
        return 0
    fi
    rm -rf -- "$WORKDIR"
}
trap cleanup EXIT
TMPFILE="$(mktemp "${WORKDIR}/kb-digest-${YEAR}-W${WEEK}.XXXXXX.md")"

# Force actor=agent so the promote records correctly. Cron has no TTY
# so isatty()-based fallback would resolve to "agent" anyway, but
# being explicit beats relying on the side-effect.
export KB_ACTOR=agent

cd "$PROJECT_DIR"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') digest start (profile: $PROFILE, week: ${YEAR}-W${WEEK}) ===" >> "$LOG"
if [ -n "$KB_PYTHON_WARNING" ]; then
    echo "WARNING: $KB_PYTHON_WARNING" | tee -a "$LOG" >&2
fi

# ---------------------------------------------------------------
# Gather 7-day activity snapshot
# ---------------------------------------------------------------
# There is no single `a2kb recent` command yet, so the snapshot is read
# through the S3 client with an inline Python script. The output is passed
# to the prompt as plain text, and the model uses it to identify themes.
# The profile is resolved exactly as `a2kb -p <profile>` resolves it (user-level v2
# config, project pointer or legacy project file, KB_* variables), from the project
# directory. The heredoc is quoted and the name passed through the environment, so
# nothing from the command line is spliced into the Python source.
# A heredoc inside $( ) breaks bash 3.2, the /bin/bash on macOS, when its text holds quotes
# or parentheses; the helper writes to a file instead.
KB_DIGEST_PROFILE="$PROFILE" "$KB_PYTHON" - > "${WORKDIR}/snapshot.txt" 2>>"$LOG" <<'PYEOF'
import os, re, datetime
from collections import Counter
from agent2kb.config import resolve_profile
from agent2kb.models import Frontmatter
from agent2kb.s3_client import S3Client
from agent2kb.utils import extract_wikilinks

prof = resolve_profile(os.environ["KB_DIGEST_PROFILE"])
s3 = S3Client(prof)

cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
cutoff_stamp = cutoff.strftime("%Y%m%d")

def recent_keys(sub_prefix, limit=120):
    out = []
    for k in s3.list_keys(sub_prefix=sub_prefix, suffix=".md"):
        fn = k.split("/")[-1]
        if fn[:8] >= cutoff_stamp:
            out.append(k)
    return sorted(out)[-limit:]

raw_keys = recent_keys("raw/")
wiki_keys = recent_keys("wiki/")
concept_keys = recent_keys("wiki/concepts/")
output_keys = recent_keys("outputs/")

# Entity tally + sample article slugs per entity, so the digest LLM
# can cite a concrete article when explaining what an entity is about.
entity_counts = Counter()
entity_sources: dict[str, list[str]] = {}
for k in wiki_keys:
    try:
        body = s3.download(k)
        slug = k.split("/")[-1].replace(".md", "")
        for link in extract_wikilinks(body):
            target = link.split("|", 1)[0].split("#", 1)[0].strip()
            if target:
                entity_counts[target] += 1
                entity_sources.setdefault(target, [])
                if slug not in entity_sources[target] and len(entity_sources[target]) < 3:
                    entity_sources[target].append(slug)
    except Exception:
        continue

def fmt_block(label, keys, with_summary=False):
    if not keys:
        return f"### {label}\n(none in window)\n"
    lines = [f"### {label}"]
    for k in keys[-30:]:
        fn = k.split("/")[-1].replace(".md", "")
        title = fn
        summary_line = ""
        try:
            c = s3.download(k)
            fm, body = Frontmatter.from_yaml_block(c)
            title = (fm.title or fn)[:160]
            if with_summary:
                # First non-empty, non-frontmatter, non-heading paragraph
                # capped at 240 chars — gives the LLM content (not just
                # titles) without bloating the prompt.
                first_para = ""
                for para in (body or "").split("\n\n"):
                    p = para.strip()
                    if not p or p.startswith(("#", ">", "---", "[[", "|")):
                        continue
                    first_para = re.sub(r"\s+", " ", p)[:240]
                    break
                if first_para:
                    summary_line = f"\n    ↳ {first_para}"
        except Exception:
            pass
        lines.append(f"- {fn}  —  {title}{summary_line}")
    return "\n".join(lines) + "\n"

# Previous-week digest fetch for cross-week pattern section.
_now = datetime.datetime.now(datetime.timezone.utc)
_year, _week, _ = _now.isocalendar()
if _week == 1:
    prev_year, prev_week = _year - 1, 52
else:
    prev_year, prev_week = _year, _week - 1
prev_slug = f"{prev_year}-W{prev_week:02d}"
prev_digest_body = ""
try:
    for k in s3.list_keys(sub_prefix="outputs/weekly/", suffix=".md"):
        if prev_slug in k:
            c = s3.download(k)
            _fm, _body = Frontmatter.from_yaml_block(c)
            prev_digest_body = (_body or "").strip()
            break
except Exception:
    pass

print(f"Window: {cutoff.strftime('%Y-%m-%d')} → {_now.strftime('%Y-%m-%d')}")
print(f"Counts: raw={len(raw_keys)} wiki={len(wiki_keys)} concepts={len(concept_keys)} outputs={len(output_keys)}")
print()
# Raw + wiki get one-paragraph summaries so the LLM can write about
# CONTENT instead of just titles. Outputs + concepts stay slug-only.
print(fmt_block("Recent raw ingests", raw_keys, with_summary=True))
print(fmt_block("Recent wiki articles (compiled)", [k for k in wiki_keys if "/concepts/" not in k and "/topics/" not in k], with_summary=True))
print(fmt_block("New / refreshed concept pages", concept_keys))
print(fmt_block("Recent outputs (query answers)", output_keys))

print("### Top 30 entities by mention this window")
for entity, count in entity_counts.most_common(30):
    srcs = entity_sources.get(entity, [])
    src_str = f"  (seen in: {', '.join(srcs)})" if srcs else ""
    print(f"- {entity}: {count}{src_str}")

print()
print(f"### Previous week's digest ({prev_slug})")
if prev_digest_body:
    print(prev_digest_body[:4000])
else:
    print("(no prior digest found — this is the first digest, or the prior week was skipped)")
PYEOF
ACTIVITY_SNAPSHOT="$(cat "${WORKDIR}/snapshot.txt")"

if [ -z "$ACTIVITY_SNAPSHOT" ]; then
    echo "FAIL: activity snapshot empty (python helper failed?)" >> "$LOG"
    exit 1
fi

# ---------------------------------------------------------------
# Digest prompt — brevity rule encoded explicitly
# ---------------------------------------------------------------
# read -d '' rather than $(cat <<...): see the note on the snapshot above.
IFS= read -r -d '' PROMPT <<'PROMPT_EOF' || true
You are producing a Knowledge-Base weekly digest for a power user who
ingests dozens of articles per week and wants a substantive synthesis
— NOT a table of contents. The KB contents and recent activity are
listed below. Read them carefully (titles, the one-line content
summary under each, the entity tally with source slugs, AND last
week's digest for comparison) then write a 500-1500 word markdown
body following EXACTLY this shape:

# KB Digest — Week ${WEEK_PLACEHOLDER}, ${YEAR_PLACEHOLDER} (${START_PLACEHOLDER} → ${TODAY_PLACEHOLDER})

## Headline
<one tight sentence naming the dominant theme of the week>

## What dominated
<2-4 short paragraphs (NOT bullets) synthesising the main threads.
Group articles by topic cluster (e.g. "agent frameworks", "AI
content pipelines", "obsidian/KB tooling"). For each cluster name
the 2-4 strongest articles by their slug, summarise what they
collectively argue or demonstrate, and call out the most useful
takeaway. Cite article slugs inline as `[[slug]]` so the reader can
jump.>

## Top entities
For 6-10 entities (drawn from the tally below, preferring those
with ≥3 mentions), write one line each in this exact shape:
- [[entity-slug]] — N mentions. <one-sentence why-it-matters, drawing on the source articles listed in the tally>

## Cross-week pattern
<2-4 sentences comparing this week to the previous digest (which is
included verbatim at the end of the snapshot). What's NEW vs.
last week? What persisted? What dropped off? If the prior digest is
missing, write a single sentence saying so and skip comparison.>

## New concept pages
- [[concepts/slug]]
- ...
(if any; skip the section entirely if none)

## Open tensions
<contradictions, disagreements, or competing approaches you can spot
ACROSS the week's articles — e.g. "[[article-a]] argues X but
[[article-b]] reports the opposite outcome". Be specific. Skip the
section if you cannot find any.>

## Suggested follow-ups
- <concrete actions: promote slug X to wiki/topics/ if it's
  becoming a hub; resolve naming ambiguity between [[a]] and
  [[a-2]]; schedule a re-ingest of Y because article Z superseded
  it>

## Reading list
- <3-6 raw ingests from this week that the user has NOT yet queried
  (i.e. no entry in "Recent outputs" matches the topic) and that
  look most worth a deeper read. Cite the raw slug verbatim and add
  one sentence on why it deserves attention.>

Hard rules:
- ${LANGRULE_PLACEHOLDER}
- 1500 words is the CEILING, not the target. Aim for the depth the
  week's content warrants. A quiet week (raw=0 or near-zero) should
  produce a 100-word digest stating so — DO NOT enrich for the sake
  of enriching.
- All entity AND article references must use [[wikilink]] form so
  Obsidian resolves them.
- Treat the one-line content summaries (the "↳ ..." lines) as
  primary evidence — use them to write actual content claims rather
  than just listing titles.
- Output ONLY the markdown body — no preamble, no commentary, no
  code fences around the whole document.

==================== ACTIVITY SNAPSHOT ====================
${SNAPSHOT_PLACEHOLDER}
==================== END SNAPSHOT ====================
PROMPT_EOF
PROMPT="${PROMPT%$'\n'}"

# Substitute placeholders. envsubst-style ${VAR} markers are used
# because heredoc and double-quote substitution interact poorly with the
# backticks and braces embedded in the prompt template.
# The language rule comes from the profile's output_language, never from this
# script (an unset language means English).
LANGRULE="$("$KB_PYTHON" -m agent2kb.llm_run --profile "$PROFILE" --print-language-rule 2>>"$LOG")" \
    || LANGRULE="Write the prose in the language of the existing KB content, keeping English technical jargon in English."
PROMPT="${PROMPT//\$\{WEEK_PLACEHOLDER\}/${WEEK}}"
PROMPT="${PROMPT//\$\{LANGRULE_PLACEHOLDER\}/${LANGRULE}}"
PROMPT="${PROMPT//\$\{YEAR_PLACEHOLDER\}/${YEAR}}"
PROMPT="${PROMPT//\$\{START_PLACEHOLDER\}/${START_DATE}}"
PROMPT="${PROMPT//\$\{TODAY_PLACEHOLDER\}/${TODAY}}"
PROMPT="${PROMPT//\$\{SNAPSHOT_PLACEHOLDER\}/${ACTIVITY_SNAPSHOT}}"

# ---------------------------------------------------------------
# Run the profile's LLM (non-interactive)
# ---------------------------------------------------------------
echo "--- Activity snapshot lines ---" >> "$LOG"
echo "$ACTIVITY_SNAPSHOT" | head -20 >> "$LOG"
echo "--- Calling agent2kb.llm_run (purpose: aux) ---" >> "$LOG"

# Model, effort, isolation and fallback all come from the profile via
# agent2kb.llm_run. Exit 3 means every configured CLI is down.
if ! "$KB_PYTHON" -m agent2kb.llm_run --profile "$PROFILE" --purpose aux \
        --system "You write concise weekly knowledge-base digests in Markdown, following the brief exactly." \
        <<<"$PROMPT" > "$TMPFILE" 2>>"$LOG"; then
    echo "FAIL: agent2kb.llm_run returned non-zero" >> "$LOG"
    exit 1
fi

if [ ! -s "$TMPFILE" ]; then
    echo "FAIL: LLM produced empty output" >> "$LOG"
    exit 1
fi

# Strip any stray codex preamble (e.g. "OpenAI Codex v0.x" header that
# some codex versions emit before the actual response). Keep only
# content from the first H1 onwards.
if grep -q '^# ' "$TMPFILE"; then
    CLEANFILE="$(mktemp "${WORKDIR}/clean.XXXXXX")"
    awk '/^# / {found=1} found {print}' "$TMPFILE" > "$CLEANFILE"
    mv -- "$CLEANFILE" "$TMPFILE"
fi

WORD_COUNT=$(wc -w < "$TMPFILE")
echo "--- Digest length: ${WORD_COUNT} words ---" >> "$LOG"

# ---------------------------------------------------------------
# Promote to outputs/weekly/
# ---------------------------------------------------------------
if ! "$KB" -p "$PROFILE" promote --file "$TMPFILE" --as weekly --actor agent \
     --slug "${YEAR}-W${WEEK}" --delete-source >> "$LOG" 2>&1; then
    # Keep the draft for debugging; it stays in the private (0700) work folder.
    KEEP_WORKDIR=1
    echo "FAIL: a2kb promote returned non-zero; draft kept at ${TMPFILE}" >> "$LOG"
    exit 1
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') digest done (${YEAR}-W${WEEK}, ${WORD_COUNT} words) ===" >> "$LOG"
