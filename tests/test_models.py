"""Tests for Pydantic models."""

import pytest

from agent2kb.models import (
    CompileResult,
    ContentType,
    Frontmatter,
    IngestResult,
    KBConfig,
    LLMProvider,
    ProfileConfig,
    SourceKind,
)


class TestProfileConfig:
    def test_prefix_auto_appends_slash(self):
        p = ProfileConfig(
            name="proj",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="projects/proj",
        )
        assert p.prefix == "projects/proj/"

    def test_prefix_already_has_slash(self):
        p = ProfileConfig(
            name="proj",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="projects/proj/",
        )
        assert p.prefix == "projects/proj/"

    def test_super_empty_prefix(self):
        p = ProfileConfig(
            name="super",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="",
        )
        assert p.is_super
        assert p.prefix == ""

    def test_default_provider_is_groq(self):
        p = ProfileConfig(
            name="test",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="test/",
        )
        assert p.compile_provider == LLMProvider.GROQ


class TestKBConfig:
    def test_get_profile(self):
        profile = ProfileConfig(
            name="proj",
            s3_endpoint="http://localhost:8333",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
            prefix="proj/",
        )
        config = KBConfig(profiles={"proj": profile})
        assert config.get_profile("proj").name == "proj"

    def test_get_profile_not_found(self):
        config = KBConfig()
        with pytest.raises(ValueError, match="not found"):
            config.get_profile("nonexistent")


class TestFrontmatter:
    def test_to_yaml_block(self):
        fm = Frontmatter(
            title="Test",
            source="http://example.com",
            source_type=ContentType.URL,
            tags=["test", "example"],
        )
        block = fm.to_yaml_block()
        assert block.startswith("---\n")
        assert block.endswith("---\n")
        assert "title: Test" in block

    def test_from_yaml_block(self):
        text = "---\ntitle: Test\nsource: http://example.com\nsource_type: url\n---\nHello body"
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "Test"
        assert fm.source == "http://example.com"
        assert body == "Hello body"

    def test_from_yaml_block_no_frontmatter(self):
        text = "Just plain text"
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == ""
        assert body == "Just plain text"

    def test_roundtrip(self):
        fm = Frontmatter(
            title="Roundtrip Test",
            source="test.md",
            source_type=ContentType.MARKDOWN,
            tags=["a", "b"],
        )
        block = fm.to_yaml_block()
        fm2, body = Frontmatter.from_yaml_block(block + "\nBody content")
        assert fm2.title == "Roundtrip Test"
        assert fm2.tags == ["a", "b"]
        assert body == "Body content"


class TestFrontmatterToleranceForWebClipper:
    """Regression tests for the silent-failure bug where one bad field wiped
    the whole frontmatter.

    Obsidian Properties + Obsidian Web Clipper commonly produce frontmatter
    shapes that don't match our scalar Pydantic fields. Before the fix, any
    type mismatch caused a ValidationError that was swallowed and replaced
    with an empty Frontmatter, silently losing source provenance.
    """

    def test_obsidian_properties_all_fields_as_single_lists(self):
        """Obsidian Properties normalizes scalars to 1-element lists."""
        text = (
            "---\n"
            "title:\n"
            "  - Claude Code 2026\n"
            "source:\n"
            "  - https://medium.com/foo/bar\n"
            "tags:\n"
            "  - clippings\n"
            "---\n\n"
            "Body.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "Claude Code 2026"
        assert fm.source == "https://medium.com/foo/bar"
        assert fm.tags == ["clippings"]

    def test_tags_as_comma_separated_string(self):
        """User custom template writes tags: 'a, b, c' instead of a list."""
        text = (
            "---\n"
            "title: Test\n"
            "source: https://example.com\n"
            'tags: "clippings, ai, claude-code"\n'
            "---\n\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "Test"
        assert fm.source == "https://example.com"
        assert fm.tags == ["clippings", "ai", "claude-code"]

    def test_date_as_yaml_timestamp(self):
        """YAML parses `2026-04-11` into a datetime.date; model expects str."""
        text = (
            "---\n"
            "title: Test\n"
            "source: https://example.com\n"
            "date: 2026-04-11\n"
            "---\n\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "Test"
        assert fm.date == "2026-04-11"

    def test_null_scalar_fields_dont_break_parse(self):
        """Empty template variables produce `field:` with a null value."""
        text = (
            "---\n"
            "title: Test\n"
            "source: https://example.com\n"
            "original_link:\n"
            "compile_provider:\n"
            "---\n\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "Test"
        assert fm.source == "https://example.com"
        assert fm.original_link == ""

    def test_extra_web_clipper_fields_are_dropped_silently(self):
        """Web Clipper writes author/published/description — not ours to keep."""
        text = (
            "---\n"
            'title: "A post"\n'
            'source: "https://example.com/post"\n'
            'author: "Jane Doe"\n'
            'published: "2026-04-11"\n'
            'description: "A description"\n'
            "tags:\n"
            "  - clippings\n"
            "---\n\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        assert fm.title == "A post"
        assert fm.source == "https://example.com/post"
        assert fm.tags == ["clippings"]
        # Extra fields aren't attributes on our model
        assert not hasattr(fm, "author")
        assert not hasattr(fm, "published")
        assert not hasattr(fm, "description")

    def test_one_bad_field_does_not_destroy_others(self):
        """A single uncoercable field must not return an entirely empty FM.

        This is the key guarantee — the old parser would return cls() (all
        defaults) on any ValidationError, silently losing provenance.
        """
        # compile_date is typed Optional[str]; feeding it a dict should fail
        # but the rest should survive.
        text = (
            "---\n"
            'title: "Test"\n'
            'source: "https://example.com"\n'
            "compile_date:\n"
            "  nested: object\n"
            "---\n\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        # Critical: title and source survived the bad compile_date
        assert fm.title == "Test"
        assert fm.source == "https://example.com"


class TestFrontmatterProvenance:
    """Frontmatter provenance fields.

    source_meta keeps channel-specific provenance out of the top-level
    field set; source_kind carries the trust classification consumers
    use to weight context.
    """

    def test_source_meta_roundtrip(self):
        """Arbitrary channel metadata survives a write→read cycle."""
        fm = Frontmatter(
            title="A Slack message",
            source="slack",
            source_meta={
                "source_channel": "#engineering",
                "source_author": "alice",
                "source_message_id": "1700000000.000100",
                "attachment_name": "diagram.png",
                "attachment_size": "42313",
            },
        )
        block = fm.to_yaml_block()
        fm2, _ = Frontmatter.from_yaml_block(block + "\nbody")
        assert fm2.source_meta["source_channel"] == "#engineering"
        assert fm2.source_meta["attachment_name"] == "diagram.png"
        assert fm2.source_meta["source_author"] == "alice"

    def test_source_meta_default_is_empty_dict(self):
        fm = Frontmatter(title="t", source="s")
        assert fm.source_meta == {}

    def test_source_kind_derived_property(self):
        for kind, expected in [
            (SourceKind.RAW, False),
            (SourceKind.ARTICLE, False),
            (SourceKind.CONCEPT, True),
            (SourceKind.QUERY_OUTPUT, True),
            (SourceKind.STUB, False),
            (SourceKind.META, False),
        ]:
            fm = Frontmatter(title="t", source="s", source_kind=kind)
            assert fm.derived is expected, kind

    def test_trust_level_mapping(self):
        assert Frontmatter(title="t", source_kind=SourceKind.RAW).trust_level == "source"
        assert Frontmatter(title="t", source_kind=SourceKind.ARTICLE).trust_level == "source"
        assert Frontmatter(title="t", source_kind=SourceKind.CONCEPT).trust_level == "derived"
        assert (
            Frontmatter(title="t", source_kind=SourceKind.QUERY_OUTPUT).trust_level
            == "derived"
        )
        assert (
            Frontmatter(title="t", source_kind=SourceKind.STUB).trust_level
            == "placeholder"
        )
        # Legacy (no kind set) → unknown, not a crash.
        assert Frontmatter(title="t").trust_level == "unknown"
        assert Frontmatter(title="t").derived is False

    def test_infer_kind_from_key(self):
        assert Frontmatter.infer_kind_from_key("personal/raw/foo.md") == SourceKind.RAW
        assert (
            Frontmatter.infer_kind_from_key("personal/outputs/x.md")
            == SourceKind.QUERY_OUTPUT
        )
        assert (
            Frontmatter.infer_kind_from_key("p/wiki/concepts/llm.md")
            == SourceKind.CONCEPT
        )
        assert (
            Frontmatter.infer_kind_from_key("p/wiki/topics/foo.md") == SourceKind.TOPIC
        )
        assert (
            Frontmatter.infer_kind_from_key("p/wiki/index.md") == SourceKind.META
        )
        assert (
            Frontmatter.infer_kind_from_key("p/_meta/schema.md") == SourceKind.META
        )
        assert (
            Frontmatter.infer_kind_from_key("p/wiki/20260411-foo.md")
            == SourceKind.ARTICLE
        )

    def test_source_meta_non_dict_is_dropped_not_fatal(self):
        """Frontmatter from a tool that writes source_meta as a string must not crash."""
        text = (
            "---\n"
            "title: Test\n"
            "source: https://example.com\n"
            "source_meta: 'a stray string'\n"
            "---\nBody.\n"
        )
        fm, body = Frontmatter.from_yaml_block(text)
        # Bad source_meta is dropped silently, other fields survive.
        assert fm.title == "Test"
        assert fm.source_meta == {}

    def test_source_kind_string_value_parses(self):
        text = (
            "---\n"
            "title: T\n"
            "source: x\n"
            "source_kind: query_output\n"
            "---\nBody."
        )
        fm, _ = Frontmatter.from_yaml_block(text)
        assert fm.source_kind == SourceKind.QUERY_OUTPUT
        assert fm.derived is True
        assert fm.trust_level == "derived"


class TestIngestResult:
    def test_success(self):
        r = IngestResult(
            source="http://example.com",
            content_type=ContentType.URL,
            raw_key="personal/raw/test.md",
            title="Test",
        )
        assert r.success is True
        assert r.error is None

    def test_failure(self):
        r = IngestResult(
            source="http://example.com",
            content_type=ContentType.URL,
            raw_key="",
            title="",
            success=False,
            error="connection failed",
        )
        assert r.success is False


class TestCompileResult:
    def test_with_wikilinks(self):
        r = CompileResult(
            source_key="raw/test.md",
            wiki_key="wiki/test.md",
            title="Test",
            content="About [[Python]] and [[Machine Learning]]",
            wikilinks=["Python", "Machine Learning"],
        )
        assert len(r.wikilinks) == 2
