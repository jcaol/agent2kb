"""Tests for utility functions."""

from pathlib import Path

from agent2kb.utils import (
    _is_suspicious_domain,
    content_hash,
    detect_file_type,
    detect_url_type,
    ensure_traditional_chinese,
    extract_wikilinks,
    make_raw_key,
    make_wiki_key,
    sanitize_wiki_urls,
    slugify,
)


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "Hello-World"

    def test_special_chars(self):
        result = slugify("What is AI? A primer!")
        assert "What-is-AI" in result

    def test_unicode_latin(self):
        result = slugify("Café résumé")
        assert "Café" in result

    def test_unicode_cjk(self):
        result = slugify("機器學習入門")
        assert "機器學習入門" in result

    def test_unicode_mixed(self):
        result = slugify("Python 機器學習 Guide")
        assert "Python" in result
        assert "機器學習" in result

    def test_max_length(self):
        long = "a" * 200
        assert len(slugify(long, max_length=50)) <= 50

    def test_empty_fallback(self):
        result = slugify("!@#$%^&*()")
        assert result == "untitled"


class TestContentHash:
    def test_string(self):
        h = content_hash("hello")
        assert len(h) == 12
        assert h == content_hash("hello")

    def test_bytes(self):
        h = content_hash(b"hello")
        assert len(h) == 12

    def test_different_content(self):
        assert content_hash("a") != content_hash("b")


class TestDetectUrlType:
    def test_youtube(self):
        assert detect_url_type("https://www.youtube.com/watch?v=abc") == "video"
        assert detect_url_type("https://youtu.be/abc") == "video"

    def test_pdf(self):
        assert detect_url_type("https://example.com/paper.pdf") == "pdf_url"

    def test_regular_url(self):
        assert detect_url_type("https://example.com/article") == "url"

    def test_audio(self):
        assert detect_url_type("https://example.com/podcast.mp3") == "audio"

    def test_document(self):
        assert detect_url_type("https://example.com/doc.docx") == "document"


class TestDetectFileType:
    def test_video(self):
        assert detect_file_type(Path("video.mp4")) == "video"

    def test_audio(self):
        assert detect_file_type(Path("song.mp3")) == "audio"

    def test_document(self):
        assert detect_file_type(Path("report.docx")) == "document"
        assert detect_file_type(Path("data.xlsx")) == "document"

    def test_markdown(self):
        assert detect_file_type(Path("notes.md")) == "markdown"

    def test_unknown(self):
        assert detect_file_type(Path("data.csv")) == "text"


class TestExtractWikilinks:
    def test_basic(self):
        assert extract_wikilinks("About [[Python]] and [[ML]]") == ["Python", "ML"]

    def test_no_links(self):
        assert extract_wikilinks("No links here") == []

    def test_nested_brackets(self):
        assert extract_wikilinks("Check [[Deep Learning]]") == ["Deep Learning"]

    def test_code_is_not_a_link(self):
        text = (
            "Real [[Page One]] and `[[inline]]` and ``[[double tick]]``.\n"
            "```md\n[[fenced]]\n```\n"
            "~~~\n[[tilde fenced]]\n~~~\n"
            "After [[Page Two]]"
        )
        assert extract_wikilinks(text) == ["Page One", "Page Two"]

    def test_unbalanced_backtick_does_not_hide_links(self):
        assert extract_wikilinks("a ` tick then [[Real]]") == ["Real"]

    def test_generated_index_has_no_links(self):
        from agent2kb.skeleton import build_index_md

        assert extract_wikilinks(build_index_md("demo", "2026-01-01")) == []


class TestChineseConversion:
    def test_simplified_to_traditional(self):
        result = ensure_traditional_chinese("机器学习是人工智能的分支", "zh-HK")
        assert "機器學習" in result
        assert "人工智慧" in result or "人工智能" in result

    def test_traditional_unchanged(self):
        original = "機器學習是人工智慧的分支"
        result = ensure_traditional_chinese(original, "zh-HK")
        assert result == original

    def test_default_language_does_not_convert(self):
        original = "机器学习是人工智能的分支"
        assert ensure_traditional_chinese(original) == original

    def test_english_unchanged(self):
        original = "Machine Learning is great"
        assert ensure_traditional_chinese(original) == original

    def test_mixed_content(self):
        text = "使用Python进行机器学习"
        result = ensure_traditional_chinese(text, "zh-HK")
        assert "Python" in result
        assert "機器學習" in result

    def test_empty_string(self):
        assert ensure_traditional_chinese("") == ""


class TestMakeKeys:
    def test_raw_key(self):
        assert make_raw_key("personal/", "test.md") == "personal/raw/test.md"

    def test_wiki_key(self):
        assert make_wiki_key("projects/a/", "test.md") == "projects/a/wiki/test.md"


class TestURLSafety:
    """Tests for URL sanitization in compiled wiki content."""

    def test_legitimate_urls_unchanged(self):
        text = (
            "Check [Dataview](https://github.com/blacksmithgu/obsidian-dataview) "
            "and [Obsidian](https://obsidian.md) for details."
        )
        assert sanitize_wiki_urls(text) == text

    def test_suspicious_tld_flagged(self):
        text = "[Free Tool](https://evil-tool.tk/download)"
        result = sanitize_wiki_urls(text)
        assert "⚠ unverified" in result
        assert "https://evil-tool.tk/download" in result

    def test_bare_ip_domain_flagged(self):
        text = "[Login](http://192.168.1.100/login)"
        result = sanitize_wiki_urls(text)
        assert "⚠ unverified" in result

    def test_excessive_subdomain_nesting_flagged(self):
        text = "[Phish](https://a.b.c.d.e.f.example.com/steal)"
        result = sanitize_wiki_urls(text)
        assert "⚠ unverified" in result

    def test_normal_subdomains_ok(self):
        text = "[Docs](https://docs.github.com/en/actions)"
        assert sanitize_wiki_urls(text) == text

    def test_suspicious_domain_detection(self):
        assert _is_suspicious_domain("evil.tk") is True
        assert _is_suspicious_domain("phish.ml") is True
        assert _is_suspicious_domain("a.b.c.d.e.f.example.com") is True
        assert _is_suspicious_domain("192.168.1.1") is True
        assert _is_suspicious_domain("github.com") is False
        assert _is_suspicious_domain("obsidian.md") is False
        assert _is_suspicious_domain("medium.com") is False

    def test_multiple_links_only_bad_ones_flagged(self):
        text = (
            "Good: [GitHub](https://github.com/repo) "
            "Bad: [Free](https://scam.tk/get) "
            "Also good: [Docs](https://docs.python.org)"
        )
        result = sanitize_wiki_urls(text)
        assert "[GitHub](https://github.com/repo)" in result
        assert "⚠ unverified" in result
        assert "[Docs](https://docs.python.org)" in result
        assert result.count("⚠") == 1

    def test_wikilinks_not_affected(self):
        text = "Use [[Dataview]] and [[Tasks]] plugins."
        assert sanitize_wiki_urls(text) == text
