"""Pydantic models for agent2kb configuration and data structures."""

from __future__ import annotations

import datetime
import re
from enum import Enum
from typing import Literal, Optional

from loguru import logger
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

_CLI_COMMAND_RE = re.compile(r"[A-Za-z0-9._-]+")
_BUCKET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_IP_SHAPED_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def bucket_name_problem(name: str) -> Optional[str]:
    """Why ``name`` is not a valid S3 bucket name, or ``None`` if it is.

    The common rules every S3-compatible provider shares: 3-63 characters,
    lowercase letters, digits, hyphens and dots, starting and ending with a
    letter or digit, no consecutive dots, not shaped like an IP address.
    """
    if not 3 <= len(name) <= 63:
        return f"'{name}' is {len(name)} characters; bucket names must be 3-63"
    if not _BUCKET_NAME_RE.fullmatch(name):
        return (
            f"'{name}' has characters or an edge that S3 does not allow; use lowercase "
            "letters, digits, hyphens and dots, starting and ending with a letter or digit"
        )
    if ".." in name:
        return f"'{name}' contains consecutive dots"
    if _IP_SHAPED_RE.fullmatch(name):
        return f"'{name}' looks like an IP address"
    return None


class LLMProvider(str, Enum):
    GROQ = "groq"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    GEMMA4 = "gemma4"
    GEMINI = "gemini"
    CLI = "cli"


#: Models a local (Ollama-served) provider uses when the profile names none. The groq
#: default below is no Ollama tag, so these must win for these providers. Keep in sync with
#: ``compile.compiler.DEFAULT_MODELS`` (a test checks it).
LOCAL_DEFAULT_MODELS = {
    LLMProvider.OLLAMA: "llama3.3:70b",
    LLMProvider.GEMMA4: "gemma4:27b",
}


class ProfileConfig(BaseModel):
    """A single profile configuration."""

    name: str
    # Admin powers (whole-bucket access, ``_meta/``, ``--all-profiles``), the v2 replacement
    # for the magic name "super". Only honoured when it comes from the user-level config;
    # a project config, env var or CLI flag can never set it (see config.resolve_profile).
    # None = not set: old profiles keep their old meaning (admin iff named "super").
    role: Optional[Literal["member", "admin"]] = None
    # Optional so a profile can target real AWS S3 (no endpoint) and use
    # boto3's default credential chain (no keys). See ``validate_s3_credentials``.
    s3_endpoint: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    bucket: str
    prefix: str = Field(description="Must end with '/' for project profiles, empty for super")
    compile_provider: LLMProvider = LLMProvider.GROQ
    groq_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    voyage_api_key: Optional[str] = None
    # Optional Voyage cross-encoder rerank pass after retrieval (adds
    # one API call per a2kb query, +10-20% precision@10). Defaults to on
    # when voyage_api_key is set, off otherwise — see resolution in
    # ``agent2kb.embeddings.Reranker``.
    rerank_enabled: Optional[bool] = None
    # Pin specific embedding / rerank model IDs in the profile so a new
    # generation (voyage-5-lite, voyage-rerank-3, gemini-embedding-002,
    # ...) is a config flip, not a agent2kb release. When None, the
    # client falls back to the per-provider default constants in
    # ``agent2kb.embeddings`` — same shape as ``default_model`` for
    # compile, just split out per concern.
    embedding_model: Optional[str] = None
    rerank_model: Optional[str] = None
    ollama_host: str = "http://localhost:11434"

    # Language of compiled wiki pages, concept pages, query answers, lint
    # reports and digests. Unset means English. Known codes (agent2kb.language.LANGUAGES):
    # en, de, es, fr, pt-BR, ja, ko, zh-TW, zh-HK, zh-CN, yue; any other value is passed to
    # the LLM as free text ("Write in <value>"). Simplified→Traditional (OpenCC) conversion
    # runs only for the Traditional Chinese entries.
    output_language: Optional[str] = None

    # Embedding provider for semantic search. None → auto-pick based on
    # which API key is set (gemini > openai > ollama). Set explicitly
    # to override (e.g. force ollama even when gemini_api_key is set).
    embedding_provider: Optional[str] = None
    # Defaults form one coherent set: the default provider is groq, so the
    # default model is a groq model. With compile_provider=cli and no explicit
    # default_model, it resolves to the CLI's own default (see validate_defaults).
    default_model: str = "llama-3.3-70b-versatile"
    # CLI provider. `cli_command` is "claude" or "codex"; `default_model` and
    # `cli_reasoning_effort` drive wiki-page compilation.
    cli_command: str = "claude"
    cli_reasoning_effort: str = "high"
    # Model for the lighter, judgement-heavy jobs around compile: concept
    # pages, the weekly index summary, lint reports, and the weekly digest.
    # Unset → same model and effort as compile.
    aux_model: Optional[str] = None
    aux_effort: Optional[str] = None
    # Second CLI used when the primary is hard-down (out of credits, quota,
    # bad auth) or times out on an item. Reuses that CLI's own login, so no
    # API key is needed. Disabled ("") unless set: a fallback to a CLI the user
    # may not have installed would only add a second failure. When set without
    # `cli_fallback_model`, that CLI's default model is used.
    cli_fallback_command: str = ""
    cli_fallback_model: str = ""
    cli_fallback_effort: str = "high"
    concept_threshold: int = 2
    # Days covered by the recap at the top of wiki/index.md ("Past N Days": an LLM summary
    # of the period's themes plus the new pages). 0 turns the section off, and with it the
    # LLM call that writes the summary.
    recent_days: int = 7

    # Privacy controls (default: keep current behaviour).
    #
    # Scope is INGEST-ONLY. These flags affect the URL extraction backend
    # (Jina Reader) and the image vision backend (the KB's claude / codex CLI,
    # Gemini, the Claude CLI). They do NOT change the compile provider — if
    # compile_provider points at a cloud service (groq, openrouter, gemini,
    # cli), content you compile will still be sent to that service. The
    # compile CLI prints a one-line
    # privacy banner before every run so the posture is visible, and raises
    # a warning in that banner when privacy_mode is True but compile is
    # going to a non-local provider.
    #
    # - allow_jina_reader=False → skip r.jina.ai URL extraction (ingest).
    # - allow_cloud_vision=False → skip all image OCR (ingest): Gemini and the
    #   claude/codex CLI alike, since each sends the image to its vendor.
    # - privacy_mode=True → forces both of the above to False regardless of
    #   their individual values. It does NOT force compile_provider to local.
    allow_jina_reader: bool = True
    allow_cloud_vision: bool = True
    privacy_mode: bool = False

    # Google Safe Browsing v4 API key (optional).
    # Free: 10K lookups/day. Get one at https://console.cloud.google.com
    # under APIs & Services → Safe Browsing API. When set, pre-ingest URL
    # checks query Google before downloading. Skipped under privacy_mode.
    google_safe_browsing_key: Optional[str] = None

    # ── Provider-neutral S3 settings (all optional; None → boto3 default) ──
    # Region name; use "auto" for Cloudflare R2.
    region: Optional[str] = None
    # "auto" | "path" | "virtual". None → boto3 default (auto).
    addressing_style: Optional[str] = None
    # "when_supported" | "when_required". None → boto3 default. boto3 >= 1.36
    # adds CRC32 checksums to uploads; some S3-compatible servers reject them,
    # so set "when_required" for those (see docs/storage-providers.md).
    checksum_mode: Optional[str] = None
    # Named profile from ~/.aws/config to take credentials from. Only honoured
    # from the user-level config or env (see config.resolve_profile).
    aws_profile: Optional[str] = None

    @field_validator("recent_days")
    @classmethod
    def _recent_days_in_range(cls, v: int) -> int:
        if not 0 <= v <= 366:
            raise ValueError("recent_days must be between 0 (off) and 366")
        return v

    @field_validator("bucket")
    @classmethod
    def warn_bucket_name(cls, value: str) -> str:
        # Warn only: legacy configs may name buckets that a self-hosted server accepts.
        problem = bucket_name_problem(value)
        if problem:
            logger.warning(f"bucket name: {problem}")
        return value

    # Path of the project-level .kb/config.yaml that contributed values, set by
    # config.resolve_profile; shown in banners so a folder-chosen target is visible.
    _project_source: Optional[str] = PrivateAttr(default=None)

    @field_validator("cli_command", "cli_fallback_command")
    @classmethod
    def validate_cli_command(cls, value: str) -> str:
        """These are executed, so accept only a bare command name resolved via PATH.

        A config file (possibly a project's ``.kb/config.yaml``) must not be able to
        point agent2kb at an arbitrary path or shell fragment. ``""`` disables the
        fallback.
        """
        value = (value or "").strip()
        if value and not _CLI_COMMAND_RE.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a bare command name. Use e.g. 'claude' or 'codex' "
                "(letters, digits, '.', '_' and '-' only — no paths); it is looked up on PATH."
            )
        return value

    @model_validator(mode="after")
    def validate_s3_credentials(self) -> "ProfileConfig":
        # Treat empty strings as "not set" so a blank config value means
        # "use the default credential chain", not "empty key".
        for field in ("s3_endpoint", "access_key", "secret_key", "region", "aws_profile"):
            if getattr(self, field) == "":
                setattr(self, field, None)
        if bool(self.access_key) != bool(self.secret_key):
            missing = "secret_key" if self.access_key else "access_key"
            raise ValueError(
                f"access_key and secret_key must be set together ('{missing}' is missing). "
                "Set both, or leave both empty to use the AWS default credential chain."
            )
        if self.addressing_style not in (None, "auto", "path", "virtual"):
            raise ValueError(
                "addressing_style must be one of auto, path, virtual "
                f"(got {self.addressing_style!r})"
            )
        if self.checksum_mode not in (None, "when_supported", "when_required"):
            raise ValueError(
                "checksum_mode must be one of when_supported, when_required "
                f"(got {self.checksum_mode!r})"
            )
        return self

    @model_validator(mode="after")
    def validate_defaults(self) -> "ProfileConfig":
        # A CLI profile that never pinned a model gets its CLI's default, not
        # the groq default (which no CLI accepts).
        if "default_model" not in self.model_fields_set and (
            self.compile_provider is LLMProvider.CLI
        ):
            from agent2kb.cli_models import default_model_for

            self.default_model = default_model_for(self.cli_command)
        elif (
            "default_model" not in self.model_fields_set
            and self.compile_provider in LOCAL_DEFAULT_MODELS
        ):
            self.default_model = LOCAL_DEFAULT_MODELS[self.compile_provider]
        return self

    @model_validator(mode="after")
    def validate_prefix(self) -> "ProfileConfig":
        if self.name != "super" and self.prefix and not self.prefix.endswith("/"):
            self.prefix = self.prefix + "/"
        return self

    @property
    def is_super(self) -> bool:
        return self.name == "super" or self.role == "admin"


class KBConfig(BaseModel):
    """Top-level configuration containing all profiles."""

    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)

    def get_profile(self, name: str) -> ProfileConfig:
        if name not in self.profiles:
            raise ValueError(f"Profile '{name}' not found. Available: {list(self.profiles.keys())}")
        return self.profiles[name]


class ContentType(str, Enum):
    URL = "url"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    MARKDOWN = "markdown"
    TEXT = "text"
    PDF_URL = "pdf_url"
    IMAGE = "image"


class SourceKind(str, Enum):
    """High-level trust classification for a page.

    Distinct from ``ContentType`` (which is the *medium*: url/video/pdf/...).
    ``SourceKind`` says how much we should trust the page's content as a
    factual source — primary sources (``raw``, ``article``) vs LLM-derived
    syntheses (``concept``, ``query_output``, ``digest``) vs hand-pinned
    references (``topic``) vs known-placeholder shells (``stub``).
    """

    RAW = "raw"  # raw/ — primary source-of-truth
    ARTICLE = "article"  # wiki/<date>-<slug>.md — LLM compile of a single raw source
    CONCEPT = "concept"  # wiki/concepts/<slug>.md — synthesized across articles
    TOPIC = "topic"  # wiki/topics/<slug>.md — curated reference
    QUERY_OUTPUT = "query_output"  # outputs/ — captured query answer
    DIGEST = "digest"  # outputs/weekly/ etc — periodic consolidation
    STUB = "stub"  # placeholder for a ghost wikilink
    META = "meta"  # index.md, log.md, _meta/


# Trust-level groupings derived from SourceKind. Compile prompts read
# this when labeling context: primary > derived > placeholder.
_TRUST_LEVEL: dict[str, str] = {
    SourceKind.RAW.value: "source",
    SourceKind.ARTICLE.value: "source",
    SourceKind.TOPIC.value: "source",
    SourceKind.CONCEPT.value: "derived",
    SourceKind.QUERY_OUTPUT.value: "derived",
    SourceKind.DIGEST.value: "derived",
    SourceKind.STUB.value: "placeholder",
    SourceKind.META.value: "meta",
}


class Frontmatter(BaseModel):
    """YAML frontmatter for all wiki and raw files."""

    title: str = ""
    source: str = ""
    source_type: ContentType = ContentType.TEXT
    date: str = Field(default_factory=lambda: datetime.datetime.now().strftime("%Y-%m-%d"))
    tags: list[str] = Field(default_factory=list)
    original_link: str = ""
    compiled: bool = False
    compile_provider: Optional[str] = None
    compile_model: Optional[str] = None
    compile_date: Optional[str] = None
    raw_content_hash: Optional[str] = None

    # Entities extracted at ingest time. List of canonical names (people,
    # products, technical concepts) found in the body. Compile reads this
    # to (a) seed wikilinks rather than relying on the LLM to spot every
    # entity, (b) deduplicate concept page creation against existing
    # concepts, (c) feed the auto-backlink system. Empty for raw files
    # ingested before entity extraction was added.
    entities: list[str] = Field(default_factory=list)

    # Trust classification for downstream consumers (compile prompts,
    # query rerankers, manifest tooling). ``None`` = legacy file — the
    # caller can fall back to the s3 key prefix (``raw/`` vs ``wiki/``
    # vs ``outputs/``) to infer the kind.
    source_kind: Optional[SourceKind] = None

    # Free-form provenance bag for source-specific metadata that doesn't
    # warrant a top-level field. Examples (none required):
    #   ingested_by, captured_at, source_title, source_author,
    #   source_channel, source_message_id, source_thread_id,
    #   source_permalink, attachment_name, attachment_mime_type,
    #   attachment_size.
    # Keep this namespace flat — nested dicts work but most consumers
    # treat values as strings. Adding a field here doesn't require a
    # schema migration: unknown keys are passed through verbatim by
    # ``_normalize_meta``. New callers (Slack/Telegram/email
    # ingest) should stuff channel-specific metadata here rather than
    # expanding the top-level model.
    source_meta: dict = Field(default_factory=dict)

    @property
    def derived(self) -> bool:
        """True iff this page is LLM-synthesized rather than a primary source."""
        if self.source_kind is None:
            return False
        return _TRUST_LEVEL.get(self.source_kind.value) == "derived"

    @property
    def trust_level(self) -> str:
        """``source`` | ``derived`` | ``placeholder`` | ``meta`` | ``unknown``."""
        if self.source_kind is None:
            return "unknown"
        return _TRUST_LEVEL.get(self.source_kind.value, "unknown")

    @classmethod
    def infer_kind_from_key(cls, s3_key: str) -> SourceKind:
        """Heuristic fallback for legacy pages that have no ``source_kind``.

        Used by manifest/compile/query when the YAML didn't carry a
        kind. Path conventions match the storage model in CLAUDE.md.
        """
        key = s3_key.lower()
        if "/raw/" in key or key.startswith("raw/"):
            return SourceKind.RAW
        # Check the more-specific outputs subfolders before the generic
        # ``/outputs/`` check, otherwise weekly/monthly digests fall
        # through and get mis-labelled as plain query_output.
        if "/outputs/weekly/" in key or "/outputs/monthly/" in key:
            return SourceKind.DIGEST
        if "/outputs/" in key or key.startswith("outputs/"):
            return SourceKind.QUERY_OUTPUT
        if "/concepts/" in key:
            return SourceKind.CONCEPT
        if "/topics/" in key:
            return SourceKind.TOPIC
        if key.endswith("/index.md") or key.endswith("/log.md") or "/_meta/" in key:
            return SourceKind.META
        return SourceKind.ARTICLE

    def to_yaml_block(self) -> str:
        """Serialize to YAML frontmatter block."""
        import yaml

        data = self.model_dump(exclude_none=True, mode="json")
        return f"---\n{yaml.dump(data, default_flow_style=False, allow_unicode=True)}---\n"

    @classmethod
    def from_yaml_block(cls, text: str) -> tuple["Frontmatter", str]:
        """Parse YAML frontmatter from text. Returns (frontmatter, body).

        Coerces common shape mismatches produced by tools that write
        frontmatter differently from our scalar model (Obsidian Properties,
        Obsidian Web Clipper, etc.) — for example, Obsidian Properties
        normalizes scalar frontmatter fields to single-element YAML lists.

        If an individual field still cannot be coerced, it is dropped with
        a debug log, and the remaining fields are preserved. This avoids
        the previous silent-failure bug where a single bad field returned
        a completely empty Frontmatter and lost source provenance.
        """
        import yaml

        if not text.startswith("---"):
            return cls(), text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return cls(), text

        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            logger.debug(f"Frontmatter YAML parse failed: {e}")
            return cls(), parts[2].lstrip("\n")

        if not isinstance(meta, dict):
            return cls(), parts[2].lstrip("\n")
        body = parts[2].lstrip("\n")

        normalized = cls._normalize_meta(meta)

        # Try the happy path first — all fields together.
        try:
            return cls(**normalized), body
        except ValidationError:
            pass

        # Fallback: construct field-by-field, dropping only the problems.
        safe: dict = {}
        for field_name, value in normalized.items():
            try:
                cls.model_validate({field_name: value})
                safe[field_name] = value
            except ValidationError as e:
                logger.debug(
                    f"Frontmatter field '{field_name}' dropped: {e.errors()[0]['msg']}"
                )
        return cls(**safe), body

    @classmethod
    def _normalize_meta(cls, meta: dict) -> dict:
        """Coerce raw YAML meta into shapes our Pydantic fields accept.

        This is the compatibility layer for Obsidian Properties, Web
        Clipper, Dataview, and other tools that write frontmatter in
        shapes our strict model doesn't natively accept.

        Rules:
        - Drop keys that aren't known Frontmatter fields.
        - Scalar string fields (title, source, source_type, date,
          original_link): if the value is a list with one element, unwrap
          it. This handles Obsidian Properties normalizing scalars to
          single-element lists.
        - tags: accept list (kept), string (split on comma), or None (→ []).
        - date/compile_date: if datetime/date object, isoformat to string.
        - None values on scalar string fields → empty string.
        """
        import datetime as _dt

        known = set(cls.model_fields.keys())
        filtered = {k: v for k, v in meta.items() if k in known}

        scalar_str_fields = {
            "title", "source", "source_type", "date", "original_link",
            "compile_provider", "compile_model", "compile_date",
            "raw_content_hash", "source_kind",
        }

        out: dict = {}
        for k, v in filtered.items():
            # None → appropriate empty (model default will kick in if dropped,
            # but empty string is explicit for string fields).
            if v is None:
                if k == "tags":
                    out[k] = []
                elif k in scalar_str_fields:
                    out[k] = ""
                continue

            # Obsidian Properties-style: [single_value] → single_value for scalars.
            if k in scalar_str_fields and isinstance(v, list):
                if len(v) == 1:
                    v = v[0]
                elif len(v) == 0:
                    v = ""
                else:
                    # Multi-element list on a scalar field — join to preserve data.
                    v = ", ".join(str(x) for x in v)

            # date / compile_date may come in as datetime objects from YAML
            # timestamps; stringify to the YYYY-MM-DD form the model expects.
            if k in {"date", "compile_date"} and isinstance(
                v, (_dt.date, _dt.datetime)
            ):
                v = v.strftime("%Y-%m-%d")

            # tags: accept comma-separated string as a user-friendly shape.
            if k == "tags":
                if isinstance(v, str):
                    v = [t.strip() for t in v.split(",") if t.strip()]
                elif not isinstance(v, list):
                    v = [str(v)]

            # All remaining scalar str fields: coerce to str (e.g. an int
            # slug number would otherwise trip Pydantic's strict mode).
            if k in scalar_str_fields and not isinstance(v, str):
                v = str(v)

            # source_meta: must be a dict. Coerce empty/None to {}, drop
            # non-dict values rather than letting them blow up the parse.
            if k == "source_meta":
                if not isinstance(v, dict):
                    logger.debug(
                        f"Frontmatter source_meta not a dict (got {type(v).__name__}); dropping"
                    )
                    continue

            out[k] = v

        return out


class CompileResult(BaseModel):
    """Result of compiling a raw file into wiki format."""

    source_key: str
    wiki_key: str
    title: str
    content: str
    wikilinks: list[str] = Field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    # True when this failure moved the source to raw/.failed/. Still a failure:
    # it stays in the run's results so totals and exit status count it.
    quarantined: bool = False
    # Compile stats for monitoring
    duration_sec: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


class IngestResult(BaseModel):
    """Result of ingesting content."""

    source: str
    content_type: ContentType
    raw_key: str
    title: str
    success: bool = True
    error: Optional[str] = None
