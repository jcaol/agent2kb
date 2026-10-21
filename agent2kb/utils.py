"""Shared utilities for agent2kb."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from agent2kb.language import uses_traditional_chinese


def slugify(text: str, max_length: int = 80) -> str:
    """Convert text to a filesystem-safe slug. Supports CJK and other Unicode."""
    text = unicodedata.normalize("NFKC", text)
    # Keep unicode letters, numbers, spaces, hyphens
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    if not text:
        # Fallback: use hash if slug is empty (shouldn't happen with Unicode support)
        text = "untitled"
    return text[:max_length]


def content_hash(content: str | bytes) -> str:
    """Generate a short hash for content deduplication."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:12]


#: Byte-order marks of the Unicode encodings a text file may carry, longest first: the
#: UTF-32 LE mark starts with the UTF-16 LE one.
_TEXT_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def decode_text_file(data: bytes) -> Optional[str]:
    """Decode the bytes of a text file, or return None when they look binary.

    A UTF-16 or UTF-32 file is full of NUL bytes, so it is recognised by its byte-order
    mark and decoded strictly; a binary that merely starts with such bytes almost never
    decodes cleanly (lone surrogates, code points past U+10FFFF) and is rejected, as is
    text that decodes to NUL characters. Without a BOM, a NUL in the first 8 KiB means
    binary, and anything else is read as UTF-8 with replacements, as before.
    """
    for bom, encoding in _TEXT_BOMS:
        if data.startswith(bom):
            try:
                text = data[len(bom):].decode(encoding)
            except UnicodeDecodeError:
                if encoding == "utf-8":
                    return data[len(bom):].decode("utf-8", errors="replace")
                return None
            return None if "\x00" in text else text
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def is_remote_url(source: str) -> bool:
    """True when ``source`` is an http(s) URL, whatever the case of its scheme."""
    try:
        parsed = urlparse(source.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def detect_url_type(url: str) -> str:
    """Detect the type of content from a URL."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()

    # YouTube
    if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
        return "video"

    # Direct file extensions
    ext_map = {
        ".pdf": "pdf_url",
        ".mp4": "video",
        ".webm": "video",
        ".mp3": "audio",
        ".wav": "audio",
        ".m4a": "audio",
        ".ogg": "audio",
        ".docx": "document",
        ".xlsx": "document",
        ".xls": "document",
        # Refused by DocumentHandler for downloads (pandoc formats); listed so they get a
        # clear error instead of being read as a web page.
        ".odt": "document",
        ".epub": "document",
        # RTF is plain text markup, not a zip, so the download cap bounds it; pandoc
        # converts it like a local --file.
        ".rtf": "document",
        ".md": "markdown",
        ".txt": "text",
    }
    for ext, content_type in ext_map.items():
        if path_lower.endswith(ext):
            return content_type

    return "url"


def detect_file_type(file_path: Path) -> str:
    """Detect content type from a local file."""
    ext = file_path.suffix.lower()
    ext_map = {
        ".mp4": "video",
        ".webm": "video",
        ".mkv": "video",
        ".avi": "video",
        ".mp3": "audio",
        ".wav": "audio",
        ".m4a": "audio",
        ".ogg": "audio",
        ".flac": "audio",
        ".pdf": "document",
        ".docx": "document",
        ".doc": "document",
        ".xlsx": "document",
        ".xls": "document",
        # pandoc reads these; without the mapping they fell through to "text"
        ".odt": "document",
        ".epub": "document",
        ".rtf": "document",
        ".md": "markdown",
        ".markdown": "markdown",
        ".txt": "text",
        ".rst": "text",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
        ".webp": "image",
        ".bmp": "image",
        ".svg": "image",
    }
    return ext_map.get(ext, "text")


_CODE_FENCE_RE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")


def extract_wikilinks(text: str) -> list[str]:
    """Extract [[wikilinks]] from text, ignoring fenced code blocks and inline code.

    Obsidian does not link inside code, so a literal ``[[example]]`` written as code (as in
    the generated index) is documentation, not a link to a page that must exist.
    """
    text = _CODE_FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def validate_url(url: str) -> None:
    """Block SSRF-prone URLs. Shared across all ingest handlers.

    Checks scheme, blocked names, and that every resolved address is public;
    fails closed when the name does not resolve. Raises ValueError if unsafe.
    See ``agent2kb.net_guard`` for the fetch-time guarantees (redirects, pinning).
    """
    from agent2kb.net_guard import validate_url as _validate

    _validate(url)


def make_raw_key(prefix: str, filename: str) -> str:
    """Build the S3 key for a raw file."""
    return f"{prefix}raw/{filename}"


def make_wiki_key(prefix: str, filename: str) -> str:
    """Build the S3 key for a wiki file."""
    return f"{prefix}wiki/{filename}"


# ─── Chinese Simplified → Traditional Conversion ──────────────────────


_opencc_converter = None


def _get_opencc():
    """Lazy-load OpenCC converter (s2tw: Simplified → Taiwan Traditional)."""
    global _opencc_converter
    if _opencc_converter is None:
        try:
            from opencc import OpenCC

            _opencc_converter = OpenCC("s2tw")
        except ImportError:
            return None
    return _opencc_converter


def _has_simplified_chinese(text: str) -> bool:
    """Detect if text contains simplified Chinese characters.

    Compares a sample of CJK characters against their traditional equivalents.
    If conversion changes them, simplified Chinese is present.
    """
    converter = _get_opencc()
    if not converter:
        return False
    # Extract CJK characters
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    if not cjk:
        return False
    # Sample up to 200 characters for performance
    sample = "".join(cjk[:200])
    converted = converter.convert(sample)
    return converted != sample


def ensure_traditional_chinese(text: str, output_language: Optional[str] = None) -> str:
    """Convert any simplified Chinese in text to traditional Chinese.

    Preserves all non-Chinese content (English, code, etc.) untouched.
    Only converts if simplified Chinese is detected.

    ``output_language`` is the profile's setting: the conversion only applies
    when it is ``zh-HK`` or ``zh-TW``; when it is unset (English), ``en``, ``zh-CN`` or any
    other language the text is returned as-is.
    """
    if not uses_traditional_chinese(output_language):
        return text
    converter = _get_opencc()
    if not converter:
        return text
    if not _has_simplified_chinese(text):
        return text
    return converter.convert(text)


# ─── URL Safety for Compiled Wiki Content ────────────────────────────


#: TLDs commonly used by free-registration phishing/spam domains.
_SUSPICIOUS_TLDS = frozenset({
    ".tk", ".ml", ".ga", ".cf", ".gq",  # Freenom free TLDs
    ".top", ".xyz", ".buzz", ".icu", ".surf",  # cheap bulk-reg TLDs
    ".work", ".click", ".link", ".rest",
})

#: URL shortener domains whose destinations should be expanded.
_SHORTENER_DOMAINS = frozenset({
    "t.co", "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "is.gd",
    "buff.ly", "adf.ly", "bl.ink", "shor.by", "rebrand.ly",
    "dlvr.it", "lnkd.in", "youtu.be",
})


def _is_suspicious_domain(host: str) -> bool:
    """Heuristic checks for obviously-suspicious domains."""
    if not host:
        return True

    # Bare IP address as domain (not a CDN, almost always suspicious)
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    # Known-bad TLDs
    for tld in _SUSPICIOUS_TLDS:
        if host.endswith(tld):
            return True

    # Excessive subdomain nesting (>4 dots) — common in phishing
    if host.count(".") > 4:
        return True

    return False


def _expand_shortener(url: str) -> str | None:
    """Follow a URL shortener redirect and return the destination.

    Returns None if expansion fails or the destination is itself suspicious.
    Uses a HEAD request with a short timeout so compile isn't slowed.
    """
    try:
        from agent2kb.net_guard import safe_get

        # Guarded fetch: compiled wiki text is LLM output derived from untrusted pages,
        # so the shortener URL must not be able to bounce us into the local network.
        resp = safe_get(url, method="HEAD", timeout=5, raise_for_status=False)
        dest = resp.extensions.get("final_url", "")
        if dest and dest != url:
            return dest
    except Exception:
        pass
    return None


def sanitize_wiki_urls(text: str) -> str:
    """Post-process compiled wiki content to flag or expand URLs.

    Two passes:
    1. Expand URL shorteners inline so the reader sees the real destination.
    2. Flag links with suspicious domains with a visible [⚠ unverified] marker
       so Obsidian readers know to check before clicking.

    Does NOT strip links — only annotates. The user can still click through
    if they want; the marker just provides awareness.
    """
    # Find all markdown links: [text](url) and bare URLs
    md_link_pattern = re.compile(r'\[([^\]]*)\]\((https?://[^)]+)\)')

    def _process_link(match: re.Match) -> str:
        link_text = match.group(1)
        url = match.group(2)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        # Pass 1: expand shorteners inline
        if host in _SHORTENER_DOMAINS:
            expanded = _expand_shortener(url)
            if expanded:
                expanded_host = urlparse(expanded).hostname or ""
                if _is_suspicious_domain(expanded_host):
                    return f"[{link_text} ⚠ unverified]({expanded})"
                return f"[{link_text}]({expanded})"

        # Pass 2: flag suspicious domains
        if _is_suspicious_domain(host):
            return f"[{link_text} ⚠ unverified]({url})"

        return match.group(0)  # unchanged

    return md_link_pattern.sub(_process_link, text)
