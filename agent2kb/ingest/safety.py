"""Pre-ingest URL safety checker.

Two-layer approach:
1. Local heuristics (zero-cost, always runs)
2. Google Safe Browsing API (free tier, optional — needs API key)

When privacy_mode is active, only local heuristics run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from loguru import logger

from agent2kb import __version__


class RiskLevel(str, Enum):
    CLEAN = "clean"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class SafetyResult:
    url: str
    risk: RiskLevel = RiskLevel.CLEAN
    reasons: list[str] = field(default_factory=list)

    @property
    def is_flagged(self) -> bool:
        return self.risk != RiskLevel.CLEAN


# ── Suspicious TLDs frequently used in phishing/scam campaigns ──────
_SUSPECT_TLDS = frozenset({
    ".tk", ".ml", ".ga", ".cf", ".gq",  # Freenom free domains
    ".top", ".xyz", ".buzz", ".rest", ".icu",
    ".cam", ".click", ".link", ".surf", ".monster",
})

# ── Known URL shorteners that could hide redirect chains ────────────
_SHORTENER_DOMAINS = frozenset({
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly",
    "is.gd", "buff.ly", "rb.gy", "cutt.ly", "shorturl.at",
    "tiny.cc", "lnkd.in",
})

# ── Regex patterns for common phishing traits ──────────────────────
_IP_HOST_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_HOMOGLYPH_RE = re.compile(r"[а-яА-Яа-я]")  # Cyrillic in domain
_EXCESS_SUBDOMAIN_RE = re.compile(r"^([^.]+\.){4,}")  # ≥4 subdomain levels
_DATA_EXFIL_RE = re.compile(r"(password|login|signin|verify|secure|account|update|confirm)", re.I)


def check_url(url: str, *, google_sb_key: str | None = None,
              privacy_mode: bool = False) -> SafetyResult:
    result = SafetyResult(url=url)

    _check_local_heuristics(url, result)

    if google_sb_key and not privacy_mode:
        _check_google_safe_browsing(url, google_sb_key, result)

    if result.reasons:
        result.risk = _aggregate_risk(result.reasons)

    return result


def _check_local_heuristics(url: str, result: SafetyResult) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    full = url.lower()

    if _IP_HOST_RE.match(host):
        result.reasons.append(f"IP-only host ({host}) — no domain reputation")

    tld_match = next((t for t in _SUSPECT_TLDS if host.endswith(t)), None)
    if tld_match:
        result.reasons.append(f"Suspicious TLD ({tld_match})")

    if host in _SHORTENER_DOMAINS:
        result.reasons.append(f"URL shortener ({host}) — final destination unknown")

    if _HOMOGLYPH_RE.search(host):
        result.reasons.append("Domain contains Cyrillic characters (possible homoglyph attack)")

    if _EXCESS_SUBDOMAIN_RE.match(host):
        parts = host.split(".")
        result.reasons.append(f"Excessive subdomain depth ({len(parts)} levels)")

    if _DATA_EXFIL_RE.search(path):
        result.reasons.append(f"Path contains phishing keywords ({path})")

    if len(url) > 2000:
        result.reasons.append(f"Abnormally long URL ({len(url)} chars)")

    if parsed.scheme == "http" and _DATA_EXFIL_RE.search(full):
        result.reasons.append("Unencrypted HTTP with credential-related keywords")

    if "@" in (parsed.netloc or ""):
        result.reasons.append("URL contains @ symbol (potential credential phishing)")

    port = parsed.port
    if port and port not in (80, 443, 8080, 8443):
        result.reasons.append(f"Non-standard port ({port})")


def _check_google_safe_browsing(url: str, api_key: str, result: SafetyResult) -> None:
    """Query Google Safe Browsing v4 Lookup API."""
    import httpx

    endpoint = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
    payload = {
        "client": {"clientId": "agent2kb", "clientVersion": __version__},
        "threatInfo": {
            "threatTypes": [
                "MALWARE",
                "SOCIAL_ENGINEERING",
                "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION",
            ],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url}],
        },
    }
    try:
        resp = httpx.post(
            endpoint,
            params={"key": api_key},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            matches = data.get("matches", [])
            for match in matches:
                threat = match.get("threatType", "UNKNOWN")
                result.reasons.append(f"Google Safe Browsing: {threat}")
        elif resp.status_code == 400:
            logger.debug(f"Safe Browsing API bad request: {resp.text}")
        else:
            logger.debug(f"Safe Browsing API error {resp.status_code}")
    except Exception as e:
        logger.debug(f"Safe Browsing API call failed: {e}")


def _aggregate_risk(reasons: list[str]) -> RiskLevel:
    text = " ".join(reasons).lower()
    if "google safe browsing" in text:
        return RiskLevel.HIGH
    if any(kw in text for kw in ("homoglyph", "cyrillic", "credential")):
        return RiskLevel.HIGH
    if len(reasons) >= 3:
        return RiskLevel.HIGH
    if any(kw in text for kw in ("ip-only", "shortener", "phishing")):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def format_warning(result: SafetyResult) -> str:
    """Human-readable warning string for Rich console."""
    from rich.markup import escape

    lines = [f"[yellow]⚠ Safety check flagged this URL (risk: {result.risk.value})[/yellow]"]
    for r in result.reasons:
        lines.append(f"  • {escape(r)}")
    return "\n".join(lines)
