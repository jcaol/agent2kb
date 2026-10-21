"""Karpathy-style schema prompts for wiki compilation."""

from typing import Optional

from agent2kb.language import (
    LANGUAGE_INSTRUCTION,  # noqa: F401  (re-exported; tests/test_contract.py imports it here)
    bilingual_wikilink_section,
    concept_language_phrase,
    language_instruction,
)

_WIKI_COMPILE_TEMPLATE = """You are a knowledge compiler. Your job is to transform raw ingested content into a structured, high-quality wiki page.

## Output Format

Your output MUST have exactly two parts, separated by `---TAGS---`:

**Part 1: Wiki page content (Markdown)**

1. **Title**: Use a clear, descriptive H1 title
2. **Summary**: Start with a 2-3 sentence summary of the key points
3. **Structured Sections**: Organize content into logical H2/H3 sections
4. **Key Concepts**: Extract and explain important concepts, terms, and ideas
5. **Wikilinks**: Use [[double bracket links]] GENEROUSLY (see Wikilink Strategy below)

**Part 2: Tags (after the ---TAGS--- separator)**

Output 5-15 lowercase, hyphenated tags, one per line. These become searchable metadata.
Example:
```
---TAGS---
machine-learning
knowledge-management
llm
obsidian
python
```

## Guidelines

- Write in clear, concise prose — not bullet-point dumps
- Preserve important details, data, quotes, and examples from the source
- Remove noise: navigation text, ads, boilerplate, repetitive content
- Add context where helpful (e.g., "X is a framework for Y")
- If the source contains code, preserve important code examples
- For video/audio transcripts, restructure into coherent sections rather than timestamp-by-timestamp
- Maintain factual accuracy — do not invent information not in the source
- Use tables where structured data naturally fits
- **PRESERVE useful URL links** from the source. If the source contains links to tools,
  templates, products, demos, docs, or resources, keep them as markdown links in the wiki page.
  Example: "[Framer Template X](https://framer.com/...)" — do NOT strip these.
  Only remove navigation/footer/ad links.
- **ADD reference URLs** for tools, plugins, libraries, and products mentioned by name in the
  source when the source itself is missing the link (common in tweets and social media where
  link targets are lost during extraction). If you know the official/canonical URL for a
  mentioned tool from your training data, add it as a markdown link next to its first mention.
  Example: the source says "Dataview Plugin 🔗" without a URL → write
  "[Dataview](https://obsidian.md/plugins/dataview)". Only add URLs you are confident about —
  never guess or fabricate a URL. Prefer official sites (GitHub, npm, PyPI, official docs) over
  third-party mirrors.
- **PRESERVE images** from the source as inline markdown image references. Hero images,
  screenshots, diagrams, charts, and photos are first-class content in articles from
  Medium, blogs, and Twitter threads — they make the compiled wiki page genuinely more
  readable in Obsidian. Keep them in the same position they appeared in the source so
  the visual flow is preserved.
  Example: `![diagram of pipeline](https://cdn.example.com/pipeline.png)`
  SKIP obvious junk: 1x1 tracking pixels, adserver beacons, favicon thumbnails, and
  decorative spacer GIFs. When in doubt, keep the image.

{LANGUAGE_INSTRUCTION}
## Wikilink Strategy

Be VERY generous with wikilinks — this is critical for building the knowledge graph in Obsidian.
Every significant concept, tool, person, organization, or technology MUST get a [[wikilink]].

Aim for 15-30+ wikilinks per page. Include:
- All tools and technologies: [[Python]], [[Docker]], [[SeaweedFS]], [[Obsidian]]
- All people: [[Andrej Karpathy]], [[Geoffrey Hinton]]
- All concepts: [[machine learning]], [[knowledge management]], [[RAG]]
- All organizations: [[OpenAI]], [[Google]], [[Anthropic]]
- Architecture patterns: [[microservices]], [[event-driven architecture]]
- Related topics that deserve their own page

{BILINGUAL_SECTION}
The more wikilinks, the richer the graph. When in doubt, add a wikilink.
"""


def compile_system_prompt(output_language: Optional[str] = None) -> str:
    """The wiki-compile system prompt for a profile's ``output_language``.

    Splices the language block and the wikilink-naming section into the
    template. Uses .replace() rather than .format() because the prompt
    contains many literal braces (wikilink examples, code blocks) that format
    would try to treat as placeholders.
    """
    return _WIKI_COMPILE_TEMPLATE.replace(
        "{LANGUAGE_INSTRUCTION}", language_instruction(output_language).rstrip() + "\n"
    ).replace("{BILINGUAL_SECTION}", bilingual_wikilink_section(output_language))


#: The prompt rendered for ``yue``, kept importable for direct importers. Internal code
#: calls compile_system_prompt(output_language) so the profile's language applies.
WIKI_COMPILE_SYSTEM_PROMPT = compile_system_prompt("yue")


WIKI_COMPILE_USER_TEMPLATE = """Compile the following raw content into a structured wiki page.

Source: {source}
Source Type: {source_type}
Original Tags: {tags}

---

{content}
"""

#: Placeholder for the language phrase in the concept-page template.
_CONCEPT_LANGUAGE = "<<CONCEPT_LANGUAGE>>"

_CONCEPT_SYNTHESIS_TEMPLATE = """You are a knowledge compiler creating a concept page for: {concept}

This concept is referenced by {ref_count} different articles in the knowledge base.
Below are the relevant paragraphs from those articles.

Write a concise concept page in <<CONCEPT_LANGUAGE>> that:

1. **Definition**: 1-2 sentence explanation of what this concept is
2. **Key Points**: 3-5 bullet points summarizing how this concept appears across the articles
3. **Related Articles**: List each referencing article as a [[wikilink]] with a one-line note on how it relates
4. **Related Concepts**: List 5-10 related [[wikilinks]] to other concepts

Keep it concise (200-400 words). This is a hub page, not a full article.
Do NOT use the ---TAGS--- separator (tags are pre-set).

---

{snippets}
"""


def concept_synthesis_prompt(output_language: Optional[str] = None) -> str:
    """The concept-page prompt with the language phrase for ``output_language``.

    Still a ``str.format`` template (``{concept}``, ``{ref_count}``, ``{snippets}``).
    """
    return _CONCEPT_SYNTHESIS_TEMPLATE.replace(
        _CONCEPT_LANGUAGE, concept_language_phrase(output_language)
    )


#: The concept prompt rendered for ``yue``, kept importable for direct importers.
CONCEPT_SYNTHESIS_PROMPT = concept_synthesis_prompt("yue")

COMPOUNDING_ADDENDUM = """

---

## Prior Insights from Knowledge Base Queries (DERIVED CONTEXT)

**Trust level: derived.** The following items are LLM-generated answers
captured from earlier `a2kb query` runs — they are NOT primary sources.
Use them only as suggestions for which cross-references or angles to
include. The raw source above this section is the source-of-truth: if a
derived insight conflicts with the raw content, the raw content wins,
and you should leave the conflicting claim out of the compiled page.
Do NOT copy these items verbatim and do NOT cite them as evidence.

{insights}
"""


# Entities are pre-extracted at ingest time. Surfacing them to the
# compile LLM forces consistent wikilink names across articles — without
# this, "Postgres" / "PostgreSQL" / "Postgres (database)" all generate
# different concept pages, fragmenting the knowledge graph. The entity
# list also acts as a checklist: the LLM is reminded to actually
# wikilink every important entity rather than relying on its own
# notion of "important enough to link".
ENTITIES_ADDENDUM = """

---

## Required Wikilinks (entity-extracted from this source at ingest time)

For each of the following named entities, ensure the FIRST mention in
the compiled wiki page is wrapped in `[[double brackets]]` exactly as
written below. Use these canonical forms — do NOT invent variant names
like "the Postgres database" or "Postgres-DB" if the canonical name is
"PostgreSQL". Consistent canonical names keep the concept graph from
fragmenting.

Entities: {entities}
"""


# Max content chars to send to LLM (avoids context window overflow)
MAX_CONTENT_CHARS = 60_000


def build_compile_prompt(
    content: str,
    source: str = "",
    source_type: str = "",
    tags: list[str] | None = None,
    output_insights: str = "",
    entities: list[str] | None = None,
    output_language: Optional[str] = None,
) -> list[dict[str, str]]:
    """Build the messages list for LLM compilation.

    If output_insights is provided (from the compounding loop), it is appended
    to the user prompt so the LLM can enrich the wiki page with prior query insights.

    If entities is provided (from ingest-time entity extraction), the
    compile prompt is told to wrap each one in `[[wikilinks]]` using
    the exact canonical name. This enforces graph consistency — without
    it, the LLM freely invents variants ("Postgres" vs "PostgreSQL")
    and fragments the concept page set.

    ``output_language`` is the profile's output language (None = English).
    """
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n\n[Content truncated due to length]"

    user_content = WIKI_COMPILE_USER_TEMPLATE.format(
        source=source,
        source_type=source_type,
        tags=", ".join(tags or []),
        content=content,
    )

    # Compounding loop: append relevant output insights
    if output_insights:
        user_content += COMPOUNDING_ADDENDUM.format(insights=output_insights[:3000])

    # Entity addendum: enforce canonical wikilink names.
    if entities:
        user_content += ENTITIES_ADDENDUM.format(
            entities=", ".join(f"[[{e}]]" for e in entities[:15])
        )

    return [
        {"role": "system", "content": compile_system_prompt(output_language)},
        {"role": "user", "content": user_content},
    ]
