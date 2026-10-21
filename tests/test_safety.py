"""Tests for pre-ingest URL safety checker."""

import pytest

from agent2kb.ingest.safety import (
    RiskLevel,
    SafetyResult,
    check_url,
    format_warning,
)


class TestLocalHeuristics:
    def test_clean_url(self):
        result = check_url("https://github.com/anthropics/claude-code")
        assert not result.is_flagged
        assert result.risk == RiskLevel.CLEAN

    def test_ip_only_host(self):
        result = check_url("http://192.168.1.100/malware.exe")
        assert result.is_flagged
        assert any("IP-only" in r for r in result.reasons)

    def test_suspicious_tld(self):
        result = check_url("https://free-money.tk/claim")
        assert result.is_flagged
        assert any(".tk" in r for r in result.reasons)

    def test_url_shortener(self):
        result = check_url("https://bit.ly/3xAbCdE")
        assert result.is_flagged
        assert any("shortener" in r for r in result.reasons)

    def test_cyrillic_homoglyph(self):
        result = check_url("https://аpple.com/login")  # Cyrillic а
        assert result.is_flagged
        assert result.risk == RiskLevel.HIGH
        assert any("Cyrillic" in r for r in result.reasons)

    def test_excessive_subdomains(self):
        result = check_url("https://a.b.c.d.e.example.com/page")
        assert result.is_flagged
        assert any("subdomain" in r for r in result.reasons)

    def test_phishing_keywords_in_path(self):
        result = check_url("https://example.com/login/verify-account")
        assert result.is_flagged
        assert any("phishing" in r.lower() or "keyword" in r.lower() for r in result.reasons)

    def test_at_symbol_in_url(self):
        result = check_url("http://legitimate.com@evil.com/path")
        assert result.is_flagged
        assert any("@" in r for r in result.reasons)

    def test_long_url(self):
        result = check_url("https://example.com/" + "a" * 2100)
        assert result.is_flagged
        assert any("long URL" in r for r in result.reasons)

    def test_non_standard_port(self):
        result = check_url("https://example.com:9999/page")
        assert result.is_flagged
        assert any("port" in r.lower() for r in result.reasons)

    def test_standard_ports_ok(self):
        for port in (80, 443, 8080, 8443):
            result = check_url(f"https://example.com:{port}/page")
            port_flags = [r for r in result.reasons if "port" in r.lower()]
            assert not port_flags, f"Port {port} should not be flagged"

    def test_multiple_signals_escalate_to_high(self):
        # IP + phishing path + non-standard port = 3 signals → HIGH
        result = check_url("http://192.168.1.1:9999/login/verify")
        assert result.risk == RiskLevel.HIGH


class TestRiskAggregation:
    def test_single_low_signal(self):
        result = check_url("https://example.xyz/normal-page")
        assert result.risk in (RiskLevel.LOW, RiskLevel.CLEAN)

    def test_shortener_medium(self):
        result = check_url("https://bit.ly/abc")
        assert result.risk == RiskLevel.MEDIUM


class TestSafetyResult:
    def test_clean_not_flagged(self):
        r = SafetyResult(url="https://example.com")
        assert not r.is_flagged

    def test_flagged_with_reasons(self):
        r = SafetyResult(url="x", risk=RiskLevel.HIGH, reasons=["bad"])
        assert r.is_flagged


class TestFormatWarning:
    def test_format_includes_reasons(self):
        r = SafetyResult(url="x", risk=RiskLevel.MEDIUM, reasons=["IP-only host", "bad TLD"])
        text = format_warning(r)
        assert "IP-only host" in text
        assert "bad TLD" in text
        assert "medium" in text.lower()


class TestGoogleSafeBrowsing:
    def test_skipped_in_privacy_mode(self):
        result = check_url(
            "https://example.com",
            google_sb_key="fake-key",
            privacy_mode=True,
        )
        # Should not attempt API call and return clean
        assert result.risk == RiskLevel.CLEAN

    def test_skipped_without_key(self):
        result = check_url("https://example.com", google_sb_key=None)
        assert result.risk == RiskLevel.CLEAN


class TestRealWorldUrls:
    """Sanity checks for URLs that should pass."""

    @pytest.mark.parametrize("url", [
        "https://github.com/garrytan/gbrain",
        "https://news.ycombinator.com/item?id=12345",
        "https://arxiv.org/abs/2401.12345",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://en.wikipedia.org/wiki/Knowledge_base",
        "https://twitter.com/elonmusk/status/123456",
        "https://medium.com/@user/article-title-abc123",
    ])
    def test_legitimate_urls_pass(self, url):
        result = check_url(url)
        assert not result.is_flagged, f"{url} should not be flagged: {result.reasons}"
