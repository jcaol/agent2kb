"""Configurable output language (``output_language``) and coherent defaults.

An unset ``output_language`` means English (no OpenCC). Every table language renders
its prompts from one code path; the rendered text of each is pinned by hash so any
accidental wording change fails.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
import yaml
from moto import mock_aws

from agent2kb.compile.compiler import Compiler
from agent2kb.compile.schema import (
    CONCEPT_SYNTHESIS_PROMPT,
    LANGUAGE_INSTRUCTION,
    WIKI_COMPILE_SYSTEM_PROMPT,
    build_compile_prompt,
    compile_system_prompt,
    concept_synthesis_prompt,
    language_instruction,
)
from agent2kb.config import resolve_profile
from agent2kb.ingest.base import build_frontmatter_content
from agent2kb.language import (
    describe_output_language,
    digest_language_rule,
    effective_language,
    uses_traditional_chinese,
)
from agent2kb.lint import WikiLinter
from agent2kb.models import ContentType, LLMProvider, ProfileConfig
from agent2kb.query import QueryEngine
from agent2kb.s3_client import S3Client
from agent2kb.utils import ensure_traditional_chinese

CJK = re.compile(r"[぀-ヿ㐀-鿿]")
SIMPLIFIED = "机器学习是人工智能的分支"


def _h(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()[:12]


def _profile(**kw) -> ProfileConfig:
    return ProfileConfig(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
        **kw,
    )


@pytest.fixture
def mock_s3():
    def _make(profile):
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        return client

    with mock_aws():
        yield _make


class TestLanguageInstruction:
    @pytest.mark.parametrize("value", ["yue", "YUE", "zh-yue", "Cantonese", "粵語"])
    def test_yue_is_byte_identical(self, value):
        assert language_instruction(value) is LANGUAGE_INSTRUCTION
        assert _h(LANGUAGE_INSTRUCTION) == "d4d17905a688"

    def test_english_has_no_chinese(self):
        text = language_instruction("en")
        assert "English" in text
        assert not CJK.search(text)
        assert "Cantonese" not in text

    def test_en_region_tag_normalises(self):
        assert language_instruction("en-US") == language_instruction("en")

    def test_traditional_taiwan(self):
        text = language_instruction("zh-TW")
        assert "Traditional Chinese" in text
        assert "Cantonese" not in text and "廣東話" not in text

    @pytest.mark.parametrize("value", ["zh-HK", "zh_hk", "ZH-hk", "zh-Hant-HK"])
    def test_zh_hk_is_neutral_traditional_chinese(self, value):
        text = language_instruction(value)
        assert text is not LANGUAGE_INSTRUCTION
        assert "Hong Kong usage" in text and "繁體中文" in text
        assert "Cantonese" not in text and "廣東話" not in text

    def test_simplified(self):
        text = language_instruction("zh-CN")
        assert "Simplified Chinese" in text
        assert "Traditional" not in text

    def test_japanese(self):
        assert "Japanese" in language_instruction("ja")

    def test_unknown_is_generic_and_keeps_tech_terms(self):
        text = language_instruction("Klingon")
        assert "ALWAYS write in Klingon\n" in text
        assert "do NOT translate these" in text
        assert not CJK.search(text)

    def test_free_text_cannot_break_str_format(self):
        cleaned = effective_language("Spa{nish}\n`x`")
        assert not any(ch in cleaned for ch in "{}`\n")
        concept_synthesis_prompt("a{b}").format(concept="c", ref_count=1, snippets="s")


class TestDefaultIsEnglish:
    """Unset or blank ``output_language`` behaves exactly like ``en``."""

    BLANKS = [None, "", "  "]

    @pytest.mark.parametrize("value", BLANKS)
    def test_instruction_is_english(self, value):
        assert effective_language(value) == "en"
        assert language_instruction(value) == language_instruction("en")
        assert language_instruction(value) is not LANGUAGE_INSTRUCTION
        assert compile_system_prompt(value) == compile_system_prompt("en")
        assert concept_synthesis_prompt(value) == concept_synthesis_prompt("en")

    @pytest.mark.parametrize("value", BLANKS)
    def test_no_opencc_conversion(self, value):
        assert not uses_traditional_chinese(value)
        assert ensure_traditional_chinese(SIMPLIFIED, value) == SIMPLIFIED

    @pytest.mark.parametrize("value", BLANKS)
    def test_expansion_prompt_is_english(self, value):
        from agent2kb.language import query_expansion_prompt

        assert query_expansion_prompt(value) == query_expansion_prompt("en")
        assert "Traditional Chinese" not in query_expansion_prompt(value)

    @pytest.mark.parametrize("value", BLANKS)
    def test_digest_rule_is_english(self, value):
        rule = digest_language_rule(value)
        assert rule == digest_language_rule("en")
        assert "English" in rule
        assert not CJK.search(rule)

    @pytest.mark.parametrize("value", BLANKS)
    def test_describe_says_default_english(self, value):
        described = describe_output_language(value)
        assert "en" in described and "default" in described


class TestCompilePrompts:
    def test_yue_compile_prompt_unchanged(self):
        # The Cantonese prompt used to be zh-HK's; it moved to yue byte for byte.
        yue = compile_system_prompt("yue")
        assert _h(yue) == "0ac71b368b80"
        assert compile_system_prompt("Cantonese") == yue
        assert "[[Open Source / 開源]]" in yue
        # The importable constant is the yue rendering.
        assert WIKI_COMPILE_SYSTEM_PROMPT == yue

    def test_yue_concept_prompt_unchanged(self):
        assert _h(CONCEPT_SYNTHESIS_PROMPT) == "0399f70a0227"
        assert concept_synthesis_prompt("yue") == CONCEPT_SYNTHESIS_PROMPT
        assert "Cantonese Traditional Chinese" in concept_synthesis_prompt("yue")

    def test_zh_hk_compile_prompt_has_no_cantonese(self):
        zh_hk = compile_system_prompt("zh-HK")
        assert zh_hk != compile_system_prompt("yue")
        assert "Hong Kong usage" in zh_hk
        for word in ("Cantonese", "廣東話", "粵語"):
            assert word not in zh_hk

    def test_build_compile_prompt_default_is_english(self):
        msgs = build_compile_prompt("body", "src", "url", ["t"])
        assert msgs[0]["content"] == compile_system_prompt("en")
        msgs = build_compile_prompt("body", "src", "url", ["t"], output_language="zh-HK")
        assert msgs[0]["content"] == compile_system_prompt("zh-HK")

    def test_english_compile_prompt_has_no_chinese(self):
        msgs = build_compile_prompt("body", "src", "url", ["t"], output_language="en")
        system = msgs[0]["content"]
        assert "ALWAYS write in English" in system
        assert not CJK.search(system)
        assert "knowledge compiler" in system
        assert "{BILINGUAL_SECTION}" not in system and "{LANGUAGE_INSTRUCTION}" not in system

    def test_japanese_compile_prompt_uses_japanese_bilingual_links(self):
        system = compile_system_prompt("ja")
        assert "[[Open Source / オープンソース]]" in system
        assert "[[開源]]" not in system

    def test_english_concept_prompt(self):
        prompt = concept_synthesis_prompt("en")
        assert "Write a concise concept page in English that" in prompt
        assert not CJK.search(prompt)
        prompt.format(concept="c", ref_count=1, snippets="s")

    def test_entities_example_is_neutral(self):
        msgs = build_compile_prompt("b", entities=["PostgreSQL"])
        content = msgs[1]["content"]
        # The example in the prompt is a generic one, and the list is exactly what was passed.
        assert '"PostgreSQL"' in content
        assert "Entities: [[PostgreSQL]]" in content


class TestOpenCC:
    def test_hk_tw_convert(self):
        for lang in ("zh-HK", "yue", "zh-TW"):
            assert ensure_traditional_chinese(SIMPLIFIED, lang) != SIMPLIFIED
            assert uses_traditional_chinese(lang)

    @pytest.mark.parametrize("lang", [None, "", "en", "zh-CN", "ja", "Spanish"])
    def test_other_languages_never_convert(self, lang):
        assert ensure_traditional_chinese(SIMPLIFIED, lang) == SIMPLIFIED
        assert not uses_traditional_chinese(lang)

    def test_ingest_respects_language(self):
        kwargs = dict(
            title=SIMPLIFIED, body=SIMPLIFIED, source="s", source_type=ContentType.TEXT
        )
        assert SIMPLIFIED in build_frontmatter_content(output_language="en", **kwargs)
        assert SIMPLIFIED in build_frontmatter_content(**kwargs)
        assert SIMPLIFIED not in build_frontmatter_content(output_language="zh-HK", **kwargs)

    @pytest.mark.parametrize(
        "lang, converted",
        [("en", False), ("zh-CN", False), (None, False), ("zh-HK", True), ("zh-TW", True)],
    )
    def test_compile_conversion_follows_language(self, mock_s3, lang, converted):
        from agent2kb.models import Frontmatter

        profile = _profile(output_language=lang)
        s3 = mock_s3(profile)
        fm = Frontmatter(title="T", source="s", source_type="url")
        s3.upload("test/raw/t.md", fm.to_yaml_block() + "\nraw body")
        compiler = Compiler(profile=profile, s3=s3, provider=LLMProvider.OLLAMA)
        seen = []

        def fake(messages, purpose="compile"):
            seen.append(messages[0]["content"])
            return f"# T\n\n{SIMPLIFIED} [[AI]]"

        with patch.object(compiler, "_call_llm_with_fallback", side_effect=fake):
            results = compiler.compile_all(new_only=False)
        assert results[0].success
        assert (SIMPLIFIED in s3.download(results[0].wiki_key)) is (not converted)
        assert seen[0] == compile_system_prompt(lang)


class TestQueryAndLint:
    @staticmethod
    def _capture(profile, s3, fn):
        calls = []

        def fake(self, messages, purpose="compile"):
            calls.append(messages)
            return "ok\n\nmore"

        with patch.object(Compiler, "_call_llm_with_fallback", fake):
            fn(profile, s3)
        return calls[-1]

    def test_yue_query_prompts_unchanged(self, mock_s3):
        profile = _profile(output_language="yue")
        s3 = mock_s3(profile)
        answer = self._capture(
            profile, s3,
            lambda p, s: QueryEngine(p, s)._llm_answer("q?", "ctx", ["open-source-開源"]),
        )
        expand = self._capture(profile, s3, lambda p, s: QueryEngine(p, s)._llm_expand("hello"))
        assert _h(answer) == "4fa72740383a"
        assert _h(expand) == "0a27b783ee5d"

    def test_yue_lint_prompt_unchanged(self, mock_s3):
        from agent2kb.models import Frontmatter

        profile = _profile(output_language="yue")
        s3 = mock_s3(profile)
        s3.upload("test/wiki/a.md", Frontmatter(title="A", tags=["x"]).to_yaml_block() + "\nb\n")
        msgs = self._capture(profile, s3, lambda p, s: WikiLinter(p, s).deep_health_check())
        assert "Traditional Chinese (Cantonese style)" in msgs[0]["content"]
        assert "**矛盾 (Contradictions)**" in msgs[0]["content"]

    def test_english_answer_prompt(self, mock_s3):
        profile = _profile(output_language="en")
        s3 = mock_s3(profile)
        msgs = self._capture(
            profile, s3,
            lambda p, s: QueryEngine(p, s)._llm_answer("q?", "ctx", ["open-source"]),
        )
        system = msgs[0]["content"]
        assert "ALWAYS write in English" in system
        assert not CJK.search(system)

    def test_english_expansion_prompt_stays_in_english(self, mock_s3):
        profile = _profile(output_language="en")
        s3 = mock_s3(profile)
        msgs = self._capture(profile, s3, lambda p, s: QueryEngine(p, s)._llm_expand("k8s"))
        system = msgs[0]["content"]
        assert "English" in system
        assert "Traditional Chinese" not in system

    def test_english_dictionary_expansion_drops_chinese_terms(self, mock_s3):
        zh = _profile(output_language="zh-HK")
        s3 = mock_s3(zh)
        assert "商業模式" in QueryEngine(zh, s3)._expand_cross_language("x", ["business model"])
        for en in (_profile(output_language="en"), _profile()):
            got = QueryEngine(en, s3)._expand_cross_language("x", ["business model"])
            assert not any(CJK.search(t) for t in got)

    def test_english_lint_prompt(self, mock_s3):
        from agent2kb.models import Frontmatter

        profile = _profile(output_language="en")
        s3 = mock_s3(profile)
        s3.upload("test/wiki/a.md", Frontmatter(title="A", tags=["x"]).to_yaml_block() + "\nb\n")
        msgs = self._capture(profile, s3, lambda p, s: WikiLinter(p, s).deep_health_check())
        system = msgs[0]["content"]
        assert "in English." in system
        assert not CJK.search(system)
        assert "1. **Contradictions**" in system

    def test_weekly_summary_prompt(self, mock_s3):
        rows = [("a", "b", "Title", " [x]")]
        yue, en = _profile(output_language="yue"), _profile(output_language="en")
        s3 = mock_s3(yue)
        msgs = self._capture(
            yue, s3, lambda p, s: Compiler(p, s)._generate_weekly_summary(rows, "W1")
        )
        assert _h(msgs) == "3841974b0cbc"
        msgs = self._capture(
            en, s3, lambda p, s: Compiler(p, s)._generate_weekly_summary(rows, "W1")
        )
        assert not CJK.search(msgs[0]["content"])
        unset = self._capture(
            _profile(), s3, lambda p, s: Compiler(p, s)._generate_weekly_summary(rows, "W1")
        )
        assert unset == msgs


class TestConfigPlumbing:
    def test_default_is_unset(self):
        assert _profile().output_language is None

    def test_env_override(self):
        data = {
            "profiles": {
                "test": {
                    "s3_endpoint": "http://localhost:8333",
                    "access_key": "k",
                    "secret_key": "s",
                    "bucket": "bucket",
                    "prefix": "test/",
                    "output_language": "zh-TW",
                }
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            try:
                assert resolve_profile("test", config_path=Path(f.name)).output_language == "zh-TW"
                with patch.dict(os.environ, {"KB_OUTPUT_LANGUAGE": "en"}):
                    profile = resolve_profile("test", config_path=Path(f.name))
                assert profile.output_language == "en"
            finally:
                Path(f.name).unlink()

    def test_describe_for_status(self):
        assert "en" in describe_output_language(None)
        assert "default" in describe_output_language(None)
        assert "zh-HK" in describe_output_language("zh-HK")
        assert describe_output_language("en") == "en"
        assert "conversion" in describe_output_language("zh-TW")

    def test_status_shows_output_language(self, mock_s3):
        from io import StringIO

        from rich.console import Console

        from agent2kb.status import show_status

        profile = _profile(output_language="en")
        buf = StringIO()
        show_status(profile, mock_s3(profile), Console(file=buf, width=140))
        assert "Output Language" in buf.getvalue()
        assert "en" in buf.getvalue()

    def test_digest_rule_yue_matches_script_text(self):
        rule = digest_language_rule("yue")
        assert "Cantonese-style Traditional Chinese" in rule
        assert "今個禮拜" in rule
        assert not CJK.search(digest_language_rule("en"))

    def test_llm_run_prints_language_rule(self, capsys):
        from agent2kb import llm_run

        with patch.object(llm_run, "resolve_profile", return_value=_profile(output_language="en")):
            assert llm_run.main(["--profile", "x", "--print-language-rule"]) == 0
        assert "English" in capsys.readouterr().out


class TestCoherentDefaults:
    def test_default_provider_and_model_match(self):
        p = _profile()
        assert p.compile_provider is LLMProvider.GROQ
        assert p.default_model == "llama-3.3-70b-versatile"

    def test_cli_fallback_disabled_by_default(self):
        from agent2kb.cli_runner import fallback_target_for

        assert fallback_target_for(_profile()) is None

    def test_cli_profile_without_model_gets_cli_default(self):
        assert _profile(compile_provider="cli").default_model == "claude-haiku-5-5"
        codex = _profile(compile_provider="cli", cli_command="codex")
        assert codex.default_model == "gpt-6-luna"

    def test_explicit_model_is_respected(self):
        p = _profile(compile_provider="cli", cli_command="codex", default_model="gpt-6-sol")
        assert p.default_model == "gpt-6-sol"

    def test_fallback_without_model_uses_cli_default(self):
        from agent2kb.cli_runner import fallback_target_for

        t = fallback_target_for(_profile(cli_fallback_command="codex"))
        assert (t.command, t.model) == ("codex", "gpt-6-luna")

    def test_existing_explicit_config_is_untouched(self):
        p = _profile(
            compile_provider="cli",
            default_model="claude-haiku-5-5",
            cli_command="claude",
            cli_fallback_command="codex",
            cli_fallback_model="gpt-6-luna",
        )
        assert p.cli_fallback_command == "codex"
        assert p.cli_fallback_model == "gpt-6-luna"


class TestLanguageNames:
    @pytest.mark.parametrize(
        "name, code",
        [
            ("English", "en"), ("english", "en"), ("Japanese", "ja"), ("日本語", "ja"),
            ("Traditional Chinese", "zh-TW"), ("繁體中文", "zh-TW"),
            ("Simplified Chinese", "zh-CN"), ("简体中文", "zh-CN"),
            ("Cantonese", "yue"), ("廣東話", "yue"), ("粵語", "yue"), ("zh-yue", "yue"),
            ("zh-Hant-HK", "zh-HK"),
        ],
    )
    def test_natural_language_aliases(self, name, code):
        from agent2kb.language import query_expansion_prompt

        assert effective_language(name) == code
        if code == "en":
            assert "English→English" not in query_expansion_prompt(name)

    def test_cantonese_alias_is_yue_byte_identical(self):
        assert language_instruction("Cantonese") is LANGUAGE_INSTRUCTION


class TestClaudeManagedNote:
    NOTE = "org-managed instructions"

    def _status(self, mock_s3, **kw):
        from io import StringIO

        from rich.console import Console

        from agent2kb.status import show_status

        profile = _profile(**kw)
        buf = StringIO()
        show_status(profile, mock_s3(profile), Console(file=buf, width=160))
        return buf.getvalue()

    def test_shown_for_claude_with_custom_language(self, mock_s3):
        out = self._status(mock_s3, compile_provider="cli", output_language="en")
        assert self.NOTE in out

    def test_shown_for_claude_with_default_language(self, mock_s3):
        # Unset now means English, which org-managed instructions may override too.
        assert self.NOTE in self._status(mock_s3, compile_provider="cli")

    def test_shown_for_every_language_but_hidden_for_codex_only(self, mock_s3):
        assert self.NOTE in self._status(
            mock_s3, compile_provider="cli", output_language="zh-HK"
        )
        out = self._status(
            mock_s3, compile_provider="cli", cli_command="codex", output_language="en"
        )
        assert self.NOTE not in out


#: sha256[:12] of each language-dependent prompt piece, per language. These pin the
#: rendered text byte for byte so a refactor of the language table cannot drift it.
#: ``Klingon`` and ``zh-MO`` exercise the free-text fallback.
PROMPT_PINS = {
    "en": {
        "language_instruction": "016f4e26fac3",
        "bilingual_wikilink_section": "5cf4831aaedf",
        "slug_shape_hint": "12ae32cb1ec0",
        "concept_language_phrase": "32889a931b08",
        "query_expansion_prompt": "0371bea8020a",
        "health_report_language_clause": "c866d575952a",
        "health_report_headings": "453f7fb841ff",
        "digest_language_rule": "5780a9fe3682",
        "compile": "5140743951f2",
        "concept": "3958ba7dbabd",
    },
    "zh-HK": {
        "language_instruction": "d514535c8426",
        "bilingual_wikilink_section": "0a5f92286a3d",
        "slug_shape_hint": "e7fd0536dbf9",
        "concept_language_phrase": "323c3a6489b6",
        "query_expansion_prompt": "9b584fda22ea",
        "health_report_language_clause": "15bde9ffb711",
        "health_report_headings": "456c2b3272e8",
        "digest_language_rule": "429593eb267e",
        "compile": "05eda76d6829",
        "concept": "f527766efcc8",
    },
    # Until the zh-HK/yue split these were zh-HK's pins; yue must keep them exactly.
    "yue": {
        "language_instruction": "d4d17905a688",
        "bilingual_wikilink_section": "5e1c66a578ce",
        "slug_shape_hint": "e7fd0536dbf9",
        "concept_language_phrase": "4c771c347f44",
        "query_expansion_prompt": "9b584fda22ea",
        "health_report_language_clause": "b0fc77776e32",
        "health_report_headings": "456c2b3272e8",
        "digest_language_rule": "eef37c68a1cb",
        "compile": "0ac71b368b80",
        "concept": "0399f70a0227",
    },
    "zh-TW": {
        "language_instruction": "80872e906b68",
        "bilingual_wikilink_section": "5e1c66a578ce",
        "slug_shape_hint": "e7fd0536dbf9",
        "concept_language_phrase": "323c3a6489b6",
        "query_expansion_prompt": "9b584fda22ea",
        "health_report_language_clause": "15bde9ffb711",
        "health_report_headings": "456c2b3272e8",
        "digest_language_rule": "429593eb267e",
        "compile": "bd810ca6a146",
        "concept": "f527766efcc8",
    },
    "zh-CN": {
        "language_instruction": "ceaa3197f971",
        "bilingual_wikilink_section": "bc6485695702",
        "slug_shape_hint": "cea3308f0cab",
        "concept_language_phrase": "85db6c120cd4",
        "query_expansion_prompt": "6dd34c883fa8",
        "health_report_language_clause": "e6fe7caa2ed1",
        "health_report_headings": "b053c9142b6e",  # Simplified labels
        "digest_language_rule": "42e85d012cce",
        "compile": "a0f5a529896f",
        "concept": "2bcb49fc66a4",
    },
    "ja": {
        "language_instruction": "1c162aa5e096",
        "bilingual_wikilink_section": "ba2e727bc2cb",
        "slug_shape_hint": "114517a46387",
        "concept_language_phrase": "7dbf2833aa1f",
        "query_expansion_prompt": "8b260f753943",
        "health_report_language_clause": "03e94b5ff706",
        "health_report_headings": "14a5ac5744e1",
        "digest_language_rule": "37b777807a4c",
        "compile": "a95d0b0b3be9",
        "concept": "f8fd3e350d09",
    },
    "Klingon": {
        "language_instruction": "9ccd434df85f",
        "bilingual_wikilink_section": "5cf4831aaedf",
        "slug_shape_hint": "12ae32cb1ec0",
        "concept_language_phrase": "2d069cff16ce",
        "query_expansion_prompt": "974517fc9b45",
        "health_report_language_clause": "cc89b5095d01",
        "health_report_headings": "453f7fb841ff",
        "digest_language_rule": "c4e02d812943",
        "compile": "dc3fa8a8a88c",
        "concept": "5882621614ce",
    },
    "zh-MO": {
        "language_instruction": "34001813649a",
        "bilingual_wikilink_section": "5cf4831aaedf",
        "slug_shape_hint": "12ae32cb1ec0",
        "concept_language_phrase": "2b9d46b4c7a8",
        "query_expansion_prompt": "687edd27711e",
        "health_report_language_clause": "600e187fbbe1",
        "health_report_headings": "456c2b3272e8",
        "digest_language_rule": "ca4ddab87ba7",
        "compile": "21b5e5121c5a",
        "concept": "3144d0f3e121",
    },
    "ko": {
        "language_instruction": "1db2b80743a1",
        "bilingual_wikilink_section": "c7904a736e35",
        "slug_shape_hint": "54806f7da26d",
        "concept_language_phrase": "142d7ad863b0",
        "query_expansion_prompt": "112f4aaea59d",
        "health_report_language_clause": "788020d96e84",
        "health_report_headings": "bcd0b39dfe32",
        "digest_language_rule": "2fb0f96536ff",
        "compile": "6dc1b3032731",
        "concept": "83ee0a916a6f",
    },
    "de": {
        "language_instruction": "8faec5f9808d",
        "bilingual_wikilink_section": "f3a4a08d0566",
        "slug_shape_hint": "6fe8bc34a165",
        "concept_language_phrase": "74d1b30ae222",
        "query_expansion_prompt": "507c4f91197d",
        "health_report_language_clause": "2f4a123586c1",
        "health_report_headings": "289cb401a08e",
        "digest_language_rule": "60b0c8a9b4bc",
        "compile": "8c7d7fa809da",
        "concept": "e4abd7145be3",
    },
    "es": {
        "language_instruction": "5bb05231a9dc",
        "bilingual_wikilink_section": "a41424e69da6",
        "slug_shape_hint": "9b68fdbd5236",
        "concept_language_phrase": "53e613b8b712",
        "query_expansion_prompt": "30a64e99f889",
        "health_report_language_clause": "a2745075506e",
        "health_report_headings": "712e94b3290c",
        "digest_language_rule": "3f77226fc275",
        "compile": "00964fd94502",
        "concept": "524d9f4312a9",
    },
    "fr": {
        "language_instruction": "c2bb8802a505",
        "bilingual_wikilink_section": "e456a3385946",
        "slug_shape_hint": "95559a94bd70",
        "concept_language_phrase": "66967240513e",
        "query_expansion_prompt": "dd6606578dae",
        "health_report_language_clause": "97bc773fa11f",
        "health_report_headings": "cc7072fcabdf",
        "digest_language_rule": "ef2fd46f2a62",
        "compile": "7ec0a3ba8d51",
        "concept": "c790634bd76e",
    },
    "pt-BR": {
        "language_instruction": "9e019b057f46",
        "bilingual_wikilink_section": "43f60cc029ee",
        "slug_shape_hint": "67fb244980a1",
        "concept_language_phrase": "33de3645a020",
        "query_expansion_prompt": "006765ae7a4f",
        "health_report_language_clause": "c814788836c5",
        "health_report_headings": "52887935f9c8",
        "digest_language_rule": "ef9d4a4fe58c",
        "compile": "11081a0d3611",
        "concept": "6eab22e6de80",
    },
}

#: (answer, expand, lint, weekly-summary) message hashes, per language.
MESSAGE_PINS = {
    "en": ("65d7c4edb845", "0a23eb0aa040", "1e7f00608f37", "b2fcfe5e505e"),
    "zh-HK": ("689d9c13c2ba", "0a27b783ee5d", "cde7c5b650ac", "0ce34ed72a02"),
    "yue": ("04c8ec5dde1b", "0a27b783ee5d", "8f1eef09c59c", "3841974b0cbc"),
    "zh-TW": ("ea6def6b7a1f", "0a27b783ee5d", "cde7c5b650ac", "c49a13d3c881"),
    "zh-CN": ("78c05bc8c0fa", "3128873762c0", "f0b84be49c99", "6ffebb99ab61"),
    "ja": ("c5c7e3e1d526", "fde228e0c5b2", "1ef3a83e040c", "27112d39028e"),
}


class TestPromptPins:
    """Every rendered language piece stays byte-identical across refactors."""

    @pytest.mark.parametrize("lang", sorted(PROMPT_PINS))
    def test_prompt_pieces(self, lang):
        from agent2kb import language as lang_mod

        got = {
            name: _h(getattr(lang_mod, name)(lang))
            for name in PROMPT_PINS[lang]
            if name not in ("compile", "concept")
        }
        got["compile"] = _h(compile_system_prompt(lang))
        got["concept"] = _h(concept_synthesis_prompt(lang))
        assert got == PROMPT_PINS[lang]

    @pytest.mark.parametrize("lang", sorted(MESSAGE_PINS))
    def test_full_messages(self, mock_s3, lang):
        from agent2kb.models import Frontmatter

        profile = _profile(output_language=lang)
        s3 = mock_s3(profile)
        s3.upload("test/wiki/a.md", Frontmatter(title="A", tags=["x"]).to_yaml_block() + "\nb\n")
        cap = TestQueryAndLint._capture
        rows = [("a", "b", "Title", " [x]")]
        got = (
            cap(
                profile, s3,
                lambda p, s: QueryEngine(p, s)._llm_answer("q?", "ctx", ["open-source"]),
            ),
            cap(profile, s3, lambda p, s: QueryEngine(p, s)._llm_expand("hello")),
            cap(profile, s3, lambda p, s: WikiLinter(p, s).deep_health_check()),
            cap(profile, s3, lambda p, s: Compiler(p, s)._generate_weekly_summary(rows, "W1")),
        )
        assert tuple(_h(m) for m in got) == MESSAGE_PINS[lang]


#: (code, language name in prompts, aliases) for the languages added alongside ja.
NEW_LANGUAGES = [
    ("ko", "Korean", ["Korean", "korean", "한국어", "ko-KR", "KO"]),
    ("de", "German", ["German", "Deutsch", "de-DE", "de_AT"]),
    ("es", "Spanish", ["Spanish", "Español", "espanol", "es-MX"]),
    ("fr", "French", ["French", "Français", "francais", "fr-CA"]),
    (
        "pt-BR",
        "Brazilian Portuguese",
        ["pt", "pt_br", "PT-BR", "Portuguese", "Português", "Brazilian Portuguese"],
    ),
]


class TestTableLanguages:
    """ko, de, es, fr and pt-BR get the same treatment as ja."""

    @pytest.mark.parametrize("code, name, aliases", NEW_LANGUAGES)
    def test_aliases_resolve(self, code, name, aliases):
        assert effective_language(code) == code
        for alias in aliases:
            assert effective_language(alias) == code

    @pytest.mark.parametrize("code, name, aliases", NEW_LANGUAGES)
    def test_instruction(self, code, name, aliases):
        text = language_instruction(code)
        assert f"ALWAYS write in {name} (" in text
        assert "do NOT translate these" in text
        example = next(line for line in text.splitlines() if "Example correct style" in line)
        for link in ("[[Docker]]", "[[Kubernetes]]", "[[CI/CD]]"):
            assert link in example
        assert "Cantonese" not in text and not re.search(r"[一-鿿]", text)

    @pytest.mark.parametrize("code, name, aliases", NEW_LANGUAGES)
    def test_every_prompt_piece_names_the_language(self, code, name, aliases):
        from agent2kb.language import (
            bilingual_wikilink_section,
            health_report_headings,
            health_report_language_clause,
            query_expansion_prompt,
            slug_shape_hint,
        )

        assert "### Bilingual Wikilinks" in bilingual_wikilink_section(code)
        assert "bilingual concepts use ``english-" in slug_shape_hint(code)
        assert concept_synthesis_prompt(code).count(f"in {name} (with English IT terms") == 1
        assert f"English→{name}," in query_expansion_prompt(code)
        assert health_report_language_clause(code) == (
            f"in {name}. Keep English technical terms as-is."
        )
        assert digest_language_rule(code).startswith(f"Write the prose in {name},")
        headings = health_report_headings(code)
        assert len(headings) == 5 and all(" (" in h and ")** — " in h for h in headings)
        concept_synthesis_prompt(code).format(concept="c", ref_count=1, snippets="s")

    @pytest.mark.parametrize("code, name, aliases", NEW_LANGUAGES)
    def test_no_opencc(self, code, name, aliases):
        assert not uses_traditional_chinese(code)
        assert ensure_traditional_chinese(SIMPLIFIED, code) == SIMPLIFIED
        assert describe_output_language(code) == code

    @pytest.mark.parametrize("code, name, aliases", NEW_LANGUAGES)
    def test_dictionary_keeps_english_side_only(self, code, name, aliases):
        from agent2kb.language import keep_expansion_term

        assert keep_expansion_term("business model", code)
        assert not keep_expansion_term("商業模式", code)

    def test_european_portuguese_is_not_folded_into_brazilian(self):
        assert effective_language("pt-PT") == "pt-PT"


class TestLanguageTable:
    def test_every_entry_is_complete(self):
        from agent2kb.language import LANGUAGES

        for entry in LANGUAGES:
            assert entry.code and entry.name and entry.short_name and entry.family
            for link in ("[[Docker]]", "[[Kubernetes]]", "[[CI/CD]]"):
                assert link in entry.example, entry.code
            assert entry.aliases, entry.code
            assert entry.exhaustive_triggers and entry.exhaustive_stop_words, entry.code
            if entry.code != "en":
                assert entry.keep_english_terms, entry.code
                assert entry.report_labels and len(entry.report_labels) == 5, entry.code
                assert len(entry.bilingual_examples) == 4, entry.code
                assert entry.slug_label and len(entry.slug_examples) == 2, entry.code

    def test_codes_and_aliases_are_unique(self):
        from agent2kb.language import LANGUAGES, _normalise_key

        seen: dict[str, str] = {}
        for entry in LANGUAGES:
            for alias in (entry.code, *entry.aliases):
                key = _normalise_key(alias)
                assert seen.setdefault(key, entry.code) == entry.code, key

    def test_first_class_languages(self):
        from agent2kb.language import LANGUAGES

        assert [e.code for e in LANGUAGES] == [
            "en", "zh-HK", "yue", "zh-TW", "zh-CN", "ja", "ko", "de", "es", "fr", "pt-BR",
        ]

    def test_only_traditional_chinese_uses_opencc(self):
        from agent2kb.language import LANGUAGES

        assert {e.code for e in LANGUAGES if e.opencc} == {"zh-HK", "zh-TW", "yue"}

    def test_every_non_english_language_has_localised_headings(self):
        from agent2kb.language import health_report_headings

        english = health_report_headings("en")
        for code in ("zh-HK", "yue", "zh-TW", "zh-CN", "ja", "ko", "de", "es", "fr", "pt-BR"):
            assert health_report_headings(code) != english
        assert health_report_headings("Klingon") == english


class TestExhaustiveIntent:
    """"List all" requests are recognised in every table language, in any KB."""

    QUESTIONS = [
        "list all articles about redis",
        "列出所有 redis 文章",
        "redis に関するすべての記事",
        "redis 관련 모든 문서를 보여줘",
        "Liste alle Artikel über Redis",
        "Lista todos los artículos sobre Redis",
        "Liste tous les articles sur Redis",
        "Liste todos os artigos sobre Redis",
    ]

    @pytest.mark.parametrize("lang", [None, "en", "zh-HK", "ja", "ko", "de", "es", "fr", "pt-BR"])
    @pytest.mark.parametrize("question", QUESTIONS)
    def test_triggers_fire_whatever_the_kb_language(self, mock_s3, lang, question):
        profile = _profile(output_language=lang)
        assert QueryEngine(profile, mock_s3(profile))._is_exhaustive_intent(question)

    def test_ordinary_questions_do_not_trigger(self, mock_s3):
        engine = QueryEngine(_profile(), mock_s3(_profile()))
        for q in ("how does redis cluster work?", "Wie funktioniert ein paralleler Build?"):
            assert not engine._is_exhaustive_intent(q)

    def test_existing_triggers_and_stop_words_are_kept(self):
        from agent2kb.query import _EXHAUSTIVE_STOP_WORDS, _EXHAUSTIVE_TRIGGERS

        for t in ("list all", "find all", "exhaustive", "全部", "所有", "列出所有", "每篇"):
            assert t in _EXHAUSTIVE_TRIGGERS
        for w in ("list", "wiki", "articles", "mention", "全部", "文章", "關於"):
            assert w in _EXHAUSTIVE_STOP_WORDS

    @pytest.mark.parametrize(
        "question",
        [
            "Liste alle Artikel über Redis",
            "Lista todos los artículos sobre Redis",
            "Liste tous les articles sur Redis",
            "Liste todos os artigos sobre Redis",
        ],
    )
    def test_instruction_words_are_stripped_from_the_subject(self, mock_s3, question):
        from agent2kb.models import Frontmatter

        profile = _profile()
        s3 = mock_s3(profile)
        s3.upload("test/wiki/r.md", Frontmatter(title="R").to_yaml_block() + "\nredis notes\n")
        s3.upload("test/wiki/o.md", Frontmatter(title="O").to_yaml_block() + "\nother\n")
        out = QueryEngine(profile, s3)._exhaustive_listing(question)
        assert "r.md" in out and "o.md" not in out
