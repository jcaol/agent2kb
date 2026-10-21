"""Output-language policy — single source of truth for every LLM-facing prompt.

A profile's ``output_language`` picks the language compiled wiki pages, concept
pages, query answers, lint reports and digests are written in. Unset means English.

Every supported language is one :class:`LanguageEntry` in :data:`LANGUAGES`, and one
set of renderers turns an entry into each prompt piece (language instruction,
wikilink-naming section, slug hint, concept phrase, query expansion prompt, lint
clause and headings, digest rule). Any other value is passed to the model as free
text ("write in <value>"). The rendered text of every entry is pinned by hash in
``tests/test_language.py``.

Pure string logic only — no S3, no LLM SDKs — so schema, query, lint, ingest and
the digest script entry point can all import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: What an unset ``output_language`` means (English).
DEFAULT_LANGUAGE = "en"

_UNSAFE = re.compile(r"[{}`\r\n\t]")

#: The rules of the language block for every language other than English: the model
#: writes in that language but leaves the common English tech vocabulary alone.
_KEEP_ENGLISH_RULES = (
    "Keep commonly used English IT/tech terms in English — do NOT translate these:",
    "  - Tool/product names: Python, Docker, Kubernetes, React, Git, LLM, API, CLI, etc.",
    "  - Technical concepts: machine learning, embedding, RAG, token, prompt, compile, "
    "deploy, etc.",
    "  - Standard jargon: frontend, backend, middleware, framework, pipeline, etc.",
)

#: English names of the five lint-report sections, with what each one holds.
_REPORT_SECTIONS = (
    ("Contradictions", "claims that conflict across pages"),
    ("Knowledge Gaps", "topics mentioned but never explained"),
    ("Unsourced Claims", "assertions with no raw source backing"),
    ("Suggested Articles", "3-5 new articles to fill gaps"),
    ("Overall Score", "rate KB health 1-10"),
)


def _normalise_key(value: str) -> str:
    """Lookup key for a language value: lowercase, runs of spaces/underscores → ``-``."""
    return re.sub(r"[\s_]+", "-", value.strip().lower())


@dataclass(frozen=True)
class LanguageEntry:
    """Everything the prompts need to know about one output language.

    Optional wording fields default to :attr:`short_name`; they exist for languages
    whose prompts name the language differently in different places.
    """

    #: Canonical code that :func:`effective_language` returns.
    code: str
    #: How the language block names the language ("ALWAYS write in <name>").
    name: str
    #: Plain language name for the concept, expansion, lint and digest prompts.
    short_name: str
    #: Language noun for the concept side of wikilinks ("... Japanese for concepts").
    family: str
    #: Example sentence as it appears after "Example correct style: ", quotes included.
    #: It keeps [[Docker]], [[Kubernetes]] and [[CI/CD]] in English.
    example: str
    #: Other names users may write; matched case-insensitively, spaces and ``_`` as ``-``.
    aliases: tuple[str, ...] = ()
    #: Whether a region subtag still selects this entry (``de-AT`` → ``de``).
    match_primary: bool = False
    #: Substrings of a lowercased question that mark a "list all" request.
    exhaustive_triggers: tuple[str, ...] = ()
    #: Tokens of such a request that are instruction, not subject ("list", "articles").
    exhaustive_stop_words: tuple[str, ...] = ()
    #: Local names of the five lint-report sections, rendered as "Local (English)".
    #: ``None`` keeps the English names alone.
    report_labels: Optional[tuple[str, str, str, str, str]] = None
    #: ``(English, local)`` pairs for the bilingual-wikilink section of the compile
    #: prompt. Empty means one canonical name per concept instead.
    bilingual_examples: tuple[tuple[str, str], ...] = ()
    #: Local half of the ``[[English / <label>]]`` format shown to the model.
    bilingual_label: Optional[str] = None
    #: Wikilinks the bilingual section keeps separate as "only meaningful in one language".
    bilingual_separate: str = "[[API]]"
    #: Local half of the ``english-<label>`` slug shape, and two example slugs.
    slug_label: str = ""
    slug_examples: tuple[str, ...] = ()
    #: Whether output goes through OpenCC (Simplified → Traditional Chinese).
    opencc: bool = False
    #: Whether the Chinese side of the built-in EN↔ZH search dictionary is kept.
    zh_dictionary: bool = False
    #: Whether prompts tell the model to keep English technical terms untranslated.
    keep_english_terms: bool = True
    # Wording overrides; ``None`` falls back to the default rendering.
    write_in: Optional[str] = None
    instruction_rules: tuple[str, ...] = _KEEP_ENGLISH_RULES
    wikilink_rule: Optional[str] = None
    concept_name: Optional[str] = None
    expansion_prompt: Optional[str] = None
    expansion_from: Optional[str] = None
    lint_name: Optional[str] = None
    digest_rule: Optional[str] = None


# ─── Shared data for the Chinese entries ──────────────────────────────

_ZH_TRIGGERS = ("全部", "所有", "列出所有", "列出全部", "每一篇", "每篇")
_ZH_STOP_WORDS = (
    "全部", "所有", "每篇", "每一篇", "列出", "列表",
    "文章", "頁面", "包含", "提到", "提及", "關於",
)
_ZH_REPORT_LABELS = ("矛盾", "知識缺口", "無來源聲明", "建議新文章", "整體評分")
_ZH_HANS_REPORT_LABELS = ("矛盾", "知识缺口", "无来源声明", "建议新文章", "整体评分")
_ZH_HANT_BILINGUAL = (
    ("Open Source", "開源"),
    ("Machine Learning", "機器學習"),
    ("Knowledge Management", "知識管理"),
    ("Prompt Engineering", "提示工程"),
)
_ZH_HANT_SLUGS = ("open-source-開源", "slash-commands-斜線指令")
_ZH_EXAMPLE_HANT = '"用 [[Docker]] 部署 [[Kubernetes]] cluster，透過 [[CI/CD]] pipeline 自動化"'


LANGUAGES: tuple[LanguageEntry, ...] = (
    LanguageEntry(
        code="en",
        name="English",
        short_name="English",
        family="English",
        example=(
            '"Deploy a [[Kubernetes]] cluster with [[Docker]], automated through a '
            '[[CI/CD]] pipeline"'
        ),
        aliases=("english",),
        match_primary=True,
        exhaustive_triggers=(
            "list all", "list every", "every article", "all articles", "all wiki",
            "all pages", "exhaustive", "complete list", "show me all", "find all",
        ),
        exhaustive_stop_words=(
            # Instructional verbs / nouns
            "list", "show", "find", "give", "display", "output", "print", "tell",
            "every", "all", "each", "any", "some",
            # Document/structural terms
            "wiki", "article", "articles", "page", "pages", "file", "files",
            "document", "documents", "entry", "entries", "section",
            # Verbs of mention
            "mention", "mentions", "mentioned", "contain", "contains", "containing",
            "include", "includes", "reference", "references", "about",
        ),
        keep_english_terms=False,
        write_in="English, even when the source material is in another language",
        instruction_rules=(
            "Keep product, tool and proper names in their original form: Python, Docker, "
            "Kubernetes, React, Git, LLM, API, CLI, etc.",
            "Do NOT translate or localise established technical terms, code, commands or URLs",
        ),
        wikilink_rule="Wikilinks should use the most searchable form (the common English name)",
        expansion_prompt=(
            "You are a search term expander for an English-language knowledge base. "
            "Given a query, output English search terms that could match the same topic: "
            "synonyms, abbreviations, alternate spellings and common variants of its key "
            "terms. If the query is in another language, translate its key terms into "
            "English. Output ONLY the terms, one per line, no numbering or explanation. "
            "Max 10 terms."
        ),
    ),
    LanguageEntry(
        code="zh-HK",
        name="Traditional Chinese (繁體中文, Hong Kong usage and standard written style)",
        short_name="Traditional Chinese",
        family="Chinese",
        example=_ZH_EXAMPLE_HANT,
        aliases=("zh-hant-hk",),
        exhaustive_triggers=_ZH_TRIGGERS,
        exhaustive_stop_words=_ZH_STOP_WORDS,
        report_labels=_ZH_REPORT_LABELS,
        bilingual_examples=_ZH_HANT_BILINGUAL,
        bilingual_label="中文",
        slug_label="中文",
        slug_examples=_ZH_HANT_SLUGS,
        opencc=True,
        zh_dictionary=True,
    ),
    LanguageEntry(
        code="yue",
        name="Cantonese-style Traditional Chinese (繁體中文，廣東話語氣)",
        short_name="Traditional Chinese",
        family="Chinese",
        example=_ZH_EXAMPLE_HANT,
        aliases=("zh-yue", "cantonese", "廣東話", "粵語"),
        exhaustive_triggers=_ZH_TRIGGERS,
        exhaustive_stop_words=_ZH_STOP_WORDS,
        report_labels=_ZH_REPORT_LABELS,
        bilingual_examples=_ZH_HANT_BILINGUAL,
        bilingual_label="中文",
        bilingual_separate="[[粵語]], [[API]]",
        slug_label="中文",
        slug_examples=_ZH_HANT_SLUGS,
        opencc=True,
        zh_dictionary=True,
        concept_name="Cantonese Traditional Chinese",
        lint_name="Traditional Chinese (Cantonese style)",
        digest_rule=(
            "Use Cantonese-style Traditional Chinese for prose, keep English\n"
            "  technical jargon (CLI names, model IDs, framework names, file\n"
            '  formats) in English. e.g. "今個禮拜 [[Codex]] 同 [[Claude Code]]\n'
            '  競爭白熱化".'
        ),
    ),
    LanguageEntry(
        code="zh-TW",
        name="Traditional Chinese (繁體中文, Taiwan usage and standard written style)",
        short_name="Traditional Chinese",
        family="Chinese",
        example=_ZH_EXAMPLE_HANT,
        aliases=("zh-hant", "zh-hant-tw", "traditional chinese", "繁體中文"),
        exhaustive_triggers=_ZH_TRIGGERS,
        exhaustive_stop_words=_ZH_STOP_WORDS,
        report_labels=_ZH_REPORT_LABELS,
        bilingual_examples=_ZH_HANT_BILINGUAL,
        bilingual_label="中文",
        bilingual_separate="[[粵語]], [[API]]",
        slug_label="中文",
        slug_examples=_ZH_HANT_SLUGS,
        opencc=True,
        zh_dictionary=True,
    ),
    LanguageEntry(
        code="zh-CN",
        name="Simplified Chinese (简体中文, standard written style)",
        short_name="Simplified Chinese",
        family="Chinese",
        example='"用 [[Docker]] 部署 [[Kubernetes]] cluster，通过 [[CI/CD]] pipeline 自动化"',
        aliases=("zh-hans", "zh-sg", "zh", "simplified chinese", "简体中文"),
        exhaustive_triggers=_ZH_TRIGGERS,
        exhaustive_stop_words=_ZH_STOP_WORDS,
        report_labels=_ZH_HANS_REPORT_LABELS,
        bilingual_examples=(
            ("Open Source", "开源"),
            ("Machine Learning", "机器学习"),
            ("Knowledge Management", "知识管理"),
            ("Prompt Engineering", "提示工程"),
        ),
        slug_label="中文",
        slug_examples=("open-source-开源", "slash-commands-斜线指令"),
        zh_dictionary=True,
    ),
    LanguageEntry(
        code="ja",
        name="Japanese (日本語, polite です・ます style)",
        short_name="Japanese",
        family="Japanese",
        example='"[[Docker]] で [[Kubernetes]] cluster をデプロイし、[[CI/CD]] pipeline で自動化する"',
        aliases=("japanese", "日本語"),
        match_primary=True,
        exhaustive_triggers=("すべて", "全て", "全部", "一覧"),
        exhaustive_stop_words=("すべて", "すべての", "全て", "全ての", "一覧", "記事", "ページ"),
        report_labels=("矛盾", "知識のギャップ", "出典のない主張", "推奨する新記事", "総合評価"),
        bilingual_examples=(
            ("Open Source", "オープンソース"),
            ("Machine Learning", "機械学習"),
            ("Knowledge Management", "ナレッジマネジメント"),
            ("Prompt Engineering", "プロンプトエンジニアリング"),
        ),
        slug_label="日本語",
        slug_examples=("open-source-オープンソース", "machine-learning-機械学習"),
    ),
    LanguageEntry(
        code="ko",
        name="Korean (한국어, formal 합니다체 style)",
        short_name="Korean",
        family="Korean",
        example='"[[Docker]]로 [[Kubernetes]] cluster를 배포하고, [[CI/CD]] pipeline으로 자동화합니다"',
        aliases=("korean", "한국어"),
        match_primary=True,
        exhaustive_triggers=("모든", "전체"),
        exhaustive_stop_words=(
            "모든", "전체", "목록", "나열", "나열해", "나열해줘", "보여줘", "보여주세요",
            "찾아줘", "문서", "문서를", "글", "글을", "페이지", "페이지를",
            "언급한", "언급하는", "포함한", "포함하는", "관한", "대한",
        ),
        report_labels=("모순", "지식 공백", "출처 없는 주장", "추천 신규 문서", "종합 점수"),
        bilingual_examples=(
            ("Open Source", "오픈소스"),
            ("Machine Learning", "머신러닝"),
            ("Knowledge Management", "지식 관리"),
            ("Prompt Engineering", "프롬프트 엔지니어링"),
        ),
        slug_label="한국어",
        slug_examples=("open-source-오픈소스", "machine-learning-머신러닝"),
    ),
    LanguageEntry(
        code="de",
        name="German (Deutsch, standard written style)",
        short_name="German",
        family="German",
        example=(
            '"Mit [[Docker]] einen [[Kubernetes]]-Cluster deployen und über eine '
            '[[CI/CD]]-Pipeline automatisieren"'
        ),
        aliases=("german", "deutsch"),
        match_primary=True,
        exhaustive_triggers=(
            "liste alle", "zeige alle", "finde alle", "alle artikel", "alle seiten",
            "alle einträge", "vollständige liste",
        ),
        exhaustive_stop_words=(
            "liste", "zeige", "finde", "alle", "jede", "jeden", "artikel", "seite", "seiten",
            "eintrag", "einträge", "erwähnen", "erwähnt", "enthalten", "enthält", "über",
            "vollständige", "der", "das", "den", "dem",
        ),
        report_labels=(
            "Widersprüche", "Wissenslücken", "Unbelegte Behauptungen",
            "Vorgeschlagene Artikel", "Gesamtbewertung",
        ),
        bilingual_examples=(
            ("Machine Learning", "Maschinelles Lernen"),
            ("Knowledge Management", "Wissensmanagement"),
            ("Software Architecture", "Softwarearchitektur"),
            ("Data Protection", "Datenschutz"),
        ),
        slug_label="deutsch",
        slug_examples=(
            "machine-learning-maschinelles-lernen", "knowledge-management-wissensmanagement",
        ),
    ),
    LanguageEntry(
        code="es",
        name="Spanish (español, neutral standard written style)",
        short_name="Spanish",
        family="Spanish",
        example=(
            '"Desplegar un cluster de [[Kubernetes]] con [[Docker]] y automatizarlo con un '
            'pipeline de [[CI/CD]]"'
        ),
        aliases=("spanish", "español", "espanol", "castellano"),
        match_primary=True,
        exhaustive_triggers=(
            "lista todos", "lista todas", "muestra todos", "muestra todas",
            "encuentra todos", "encuentra todas", "todos los artículos", "todas las páginas",
            "lista completa",
        ),
        exhaustive_stop_words=(
            "lista", "muestra", "encuentra", "todos", "todas", "cada", "artículo",
            "artículos", "página", "páginas", "mencionan", "menciona", "contienen",
            "contiene", "sobre", "completa", "los", "las", "el", "la", "de", "del", "que",
        ),
        report_labels=(
            "Contradicciones", "Lagunas de conocimiento", "Afirmaciones sin fuente",
            "Artículos sugeridos", "Puntuación general",
        ),
        bilingual_examples=(
            ("Open Source", "Código abierto"),
            ("Machine Learning", "Aprendizaje automático"),
            ("Knowledge Management", "Gestión del conocimiento"),
            ("Prompt Engineering", "Ingeniería de prompts"),
        ),
        slug_label="español",
        slug_examples=("open-source-código-abierto", "machine-learning-aprendizaje-automático"),
    ),
    LanguageEntry(
        code="fr",
        name="French (français, standard written style)",
        short_name="French",
        family="French",
        example=(
            "\"Déployer un cluster [[Kubernetes]] avec [[Docker]] et l'automatiser via un "
            'pipeline [[CI/CD]]"'
        ),
        aliases=("french", "français", "francais"),
        match_primary=True,
        exhaustive_triggers=(
            "liste tous", "liste toutes", "affiche tous", "affiche toutes", "trouve tous",
            "trouve toutes", "tous les articles", "toutes les pages", "liste complète",
        ),
        exhaustive_stop_words=(
            "liste", "affiche", "trouve", "tous", "toutes", "chaque", "article", "articles",
            "page", "pages", "mentionnent", "mentionne", "contiennent", "contient", "sur",
            "complète", "les", "des", "le", "la", "de", "du", "qui",
        ),
        report_labels=(
            "Incohérences", "Lacunes de connaissances", "Affirmations sans source",
            "Articles suggérés", "Note globale",
        ),
        bilingual_examples=(
            ("Machine Learning", "Apprentissage automatique"),
            ("Knowledge Management", "Gestion des connaissances"),
            ("Prompt Engineering", "Ingénierie de prompts"),
            ("Data Protection", "Protection des données"),
        ),
        slug_label="français",
        slug_examples=(
            "machine-learning-apprentissage-automatique",
            "knowledge-management-gestion-des-connaissances",
        ),
    ),
    LanguageEntry(
        code="pt-BR",
        name="Brazilian Portuguese (português do Brasil, standard written style)",
        short_name="Brazilian Portuguese",
        family="Portuguese",
        example=(
            '"Fazer o deploy de um cluster [[Kubernetes]] com [[Docker]] e automatizá-lo com '
            'um pipeline de [[CI/CD]]"'
        ),
        aliases=(
            "pt", "pt_br", "portuguese", "português", "portugues", "brazilian portuguese",
            "português do brasil", "português brasileiro",
        ),
        exhaustive_triggers=(
            "liste todos", "liste todas", "mostre todos", "mostre todas", "encontre todos",
            "encontre todas", "todos os artigos", "todas as páginas", "lista completa",
        ),
        exhaustive_stop_words=(
            "liste", "lista", "mostre", "encontre", "todos", "todas", "cada", "artigo",
            "artigos", "página", "páginas", "mencionam", "menciona", "contêm", "contém",
            "sobre", "completa", "os", "as", "de", "do", "da", "dos", "das", "que",
        ),
        report_labels=(
            "Contradições", "Lacunas de conhecimento", "Afirmações sem fonte",
            "Artigos sugeridos", "Nota geral",
        ),
        bilingual_examples=(
            ("Open Source", "Código aberto"),
            ("Machine Learning", "Aprendizado de máquina"),
            ("Knowledge Management", "Gestão do conhecimento"),
            ("Prompt Engineering", "Engenharia de prompts"),
        ),
        slug_label="português",
        slug_examples=("open-source-código-aberto", "machine-learning-aprendizado-de-máquina"),
    ),
)

_BY_CODE = {entry.code: entry for entry in LANGUAGES}

_ALIASES: dict[str, str] = {}
for _entry in LANGUAGES:
    for _alias in (_entry.code, *_entry.aliases):
        _ALIASES[_normalise_key(_alias)] = _entry.code

_PRIMARY = {entry.code: entry.code for entry in LANGUAGES if entry.match_primary}


def effective_language(value: Optional[str]) -> str:
    """Canonical language code for a profile's ``output_language``.

    ``None``/blank → ``en``. A code, alias or name from :data:`LANGUAGES` (such as
    "Deutsch" or "繁體中文") collapses to its canonical code; any other value is kept
    as free text (sanitised, because it is spliced into prompts that are later
    ``str.format``-ed).
    """
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_LANGUAGE
    key = _normalise_key(raw)
    if key in _ALIASES:
        return _ALIASES[key]
    primary = key.split("-")[0]
    if primary in _PRIMARY:
        return _PRIMARY[primary]
    return _UNSAFE.sub(" ", raw).strip()[:40] or DEFAULT_LANGUAGE


def _free_text_entry(lang: str) -> LanguageEntry:
    """Entry for a value outside the table: the model is told to write in ``lang``.

    An unlisted ``zh-*`` code still counts as Chinese for query expansion, search
    and report headings.
    """
    chinese = lang.startswith("zh-")
    return LanguageEntry(
        code=lang,
        name=lang,
        short_name=lang,
        family=lang,
        example=(
            f'a sentence in {lang} that leaves "[[Docker]]", "[[Kubernetes]]" and '
            '"[[CI/CD]]" in English'
        ),
        report_labels=_ZH_REPORT_LABELS if chinese else None,
        zh_dictionary=chinese,
        expansion_from="Chinese" if chinese else None,
    )


def language_entry(value: Optional[str]) -> LanguageEntry:
    """The table entry for ``output_language`` (a free-text entry when unlisted)."""
    lang = effective_language(value)
    return _BY_CODE.get(lang) or _free_text_entry(lang)


def describe_output_language(value: Optional[str]) -> str:
    """Human-readable effective language for ``a2kb status``."""
    lang = effective_language(value)
    if not (value or "").strip():
        return f"{lang} (default)"
    note = " (Simplified→Traditional conversion on)" if uses_traditional_chinese(value) else ""
    return f"{lang}{note}"


def uses_traditional_chinese(value: Optional[str]) -> bool:
    """Whether OpenCC Simplified → Traditional conversion applies."""
    return language_entry(value).opencc


# ─── Language instruction (compile, concept, query, lint, digest) ─────

def _render_instruction(entry: LanguageEntry) -> str:
    rules = "".join(
        f"{line}\n" if line.startswith("  ") else f"- {line}\n"
        for line in entry.instruction_rules
    )
    wikilink_rule = entry.wikilink_rule or (
        "Wikilinks should use the most searchable form "
        f"(English for tech terms, {entry.family} for concepts)"
    )
    return (
        "## Language\n\n"
        f"- ALWAYS write in {entry.write_in or entry.name}\n"
        f"{rules}"
        f"- Example correct style: {entry.example}\n"
        f"- {wikilink_rule}\n"
    )


# Rendered once so each table language always returns the same string object.
_INSTRUCTIONS = {entry.code: _render_instruction(entry) for entry in LANGUAGES}

#: The ``yue`` language block. ``tests/test_contract.py`` imports it (through
#: ``agent2kb.compile.schema``) to check which language a compile prompt carries.
LANGUAGE_INSTRUCTION = _INSTRUCTIONS["yue"]


def language_instruction(output_language: Optional[str] = None) -> str:
    """The ``## Language`` block for a profile's ``output_language``.

    ``None`` means English. Values outside the table get a generic
    "write in <value>" block that still keeps English tech terms untranslated.
    """
    entry = language_entry(output_language)
    return _INSTRUCTIONS.get(entry.code) or _render_instruction(entry)


# ─── Bilingual wikilinks (compile prompt) ─────────────────────────────

_CANONICAL_WIKILINKS = """### Canonical Wikilink Names (IMPORTANT)

Use ONE canonical name per concept so the graph does not fragment into duplicate pages.

Examples:
- [[Open Source]]  (NOT both [[Open Source]] and [[open-source software]])
- [[Machine Learning]]
- [[Knowledge Management]]
- [[Prompt Engineering]]

Use the name most people would search for. Proper names stay as written: [[Python]], [[Docker]], [[YouTube]], [[OpenAI]]
"""


def bilingual_wikilink_section(output_language: Optional[str] = None) -> str:
    """The wikilink-naming section of the compile prompt for this language."""
    entry = language_entry(output_language)
    if not entry.bilingual_examples:
        return _CANONICAL_WIKILINKS
    (en0, local0), *others = entry.bilingual_examples
    rest = "\n".join(f"- [[{en} / {local}]]" for en, local in others)
    label = entry.bilingual_label or entry.family
    return f"""### Bilingual Wikilinks (IMPORTANT)

For concepts that have BOTH an English name and a {entry.family} name, combine them into ONE wikilink
using the format [[English / {label}]]. This avoids duplicate concept pages.

Examples:
- [[{en0} / {local0}]]  (NOT separate [[{en0}]] and [[{local0}]])
{rest}

Keep these as SEPARATE wikilinks (do NOT merge):
- Proper names that are universally English: [[Python]], [[Docker]], [[YouTube]], [[OpenAI]]
- Concepts only meaningful in one language: {entry.bilingual_separate}
"""


def slug_shape_hint(output_language: Optional[str] = None) -> str:
    """Suffix of the slug-pool prompt teaching the bilingual slug shape."""
    entry = language_entry(output_language)
    if not entry.slug_examples:
        return ""
    examples = ", ".join(f"`{slug}`" for slug in entry.slug_examples)
    return f" — bilingual concepts use ``english-{entry.slug_label}`` (e.g. {examples})"


# ─── Concept page prompt ──────────────────────────────────────────────

def concept_language_phrase(output_language: Optional[str] = None) -> str:
    """Language descriptor spliced into the concept-page prompt."""
    entry = language_entry(output_language)
    name = entry.concept_name or entry.short_name
    if not entry.keep_english_terms:
        return name
    return f"{name} (with English IT terms preserved)"


# ─── Query prompts ────────────────────────────────────────────────────

def query_expansion_prompt(output_language: Optional[str] = None) -> str:
    """System prompt for LLM query-term expansion.

    The knowledge base is assumed to be written in the output language. English
    KBs expand within English (plus translating foreign-language queries into it);
    other languages expand between English and that language.
    """
    entry = language_entry(output_language)
    if entry.expansion_prompt:
        return entry.expansion_prompt
    source = entry.expansion_from or entry.family
    return (
        "You are a multilingual search term expander. "
        "Given a query, output equivalent search terms in the OTHER language "
        f"(English→{entry.short_name}, {source}→English). "
        "Output ONLY the terms, one per line, no numbering or explanation. "
        "Include synonyms and common variants. Max 10 terms."
    )


def keep_expansion_term(term: str, output_language: Optional[str] = None) -> bool:
    """Whether a built-in EN↔ZH dictionary hit is useful for this language.

    The dictionary (:mod:`agent2kb.lang_data`) pairs English with Chinese; for KBs
    that are not Chinese the Chinese side is noise, so only the English equivalents
    are kept.
    """
    if language_entry(output_language).zh_dictionary:
        return True
    return term.isascii()


def exhaustive_triggers() -> tuple[str, ...]:
    """Every table language's "list all" trigger substrings, in table order.

    Applied whatever the KB language is: people ask in English, or in their own
    language, in any KB.
    """
    return tuple(dict.fromkeys(t for e in LANGUAGES for t in e.exhaustive_triggers))


def exhaustive_stop_words() -> frozenset[str]:
    """Instruction tokens of a "list all" request, for every table language."""
    return frozenset(w for e in LANGUAGES for w in e.exhaustive_stop_words)


# ─── Lint report and digest ───────────────────────────────────────────

def health_report_language_clause(output_language: Optional[str] = None) -> str:
    """Language sentence(s) of the lint health-report system prompt."""
    entry = language_entry(output_language)
    clause = f"in {entry.lint_name or entry.short_name}."
    if entry.keep_english_terms:
        clause += " Keep English technical terms as-is."
    return clause


def health_report_headings(output_language: Optional[str] = None) -> tuple[str, ...]:
    """The five report section headings, "Local (English)" when the language has labels."""
    labels = language_entry(output_language).report_labels
    if labels is None:
        return tuple(f"**{en}** — {what}" for en, what in _REPORT_SECTIONS)
    return tuple(
        f"**{local} ({en})** — {what}" for local, (en, what) in zip(labels, _REPORT_SECTIONS)
    )


def digest_language_rule(output_language: Optional[str] = None) -> str:
    """The "Hard rules" language bullet of the weekly digest prompt (no leading ``- ``)."""
    entry = language_entry(output_language)
    if entry.digest_rule:
        return entry.digest_rule
    return (
        f"Write the prose in {entry.short_name}, keep English\n"
        "  technical jargon (CLI names, model IDs, framework names, file\n"
        "  formats) in English."
    )
