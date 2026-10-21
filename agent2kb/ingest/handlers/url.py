"""URL ingest handler: fetches web pages and converts to markdown."""

from __future__ import annotations

import datetime

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.ingest.base import BaseHandler, build_frontmatter_content
from agent2kb.models import ContentType, IngestResult
from agent2kb.net_guard import BlockedURLError, allowlist_active
from agent2kb.utils import content_hash, make_raw_key, slugify, validate_url

# Re-export for backward compat
_validate_url = validate_url


def _safe_get(url: str, max_redirects: int = 5):
    """Fetch URL via the SSRF guard: every redirect hop re-validated, connection pinned."""
    from agent2kb.net_guard import safe_get

    return safe_get(url, max_redirects=max_redirects)


def _describe_fetch_error(exc: Exception) -> str:
    """One short line for why the direct fetch failed (HTTP status, timeout, ...)."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:200]


class URLHandler(BaseHandler):
    """Handle web URL ingestion: fetch HTML, extract content, convert to markdown."""

    def process(self, source: str, **kwargs) -> IngestResult:
        try:
            _validate_url(source)

            # Try direct fetch first (handles PDFs, standard HTML)
            html = None
            fetch_error = ""
            try:
                response = _safe_get(source)
                content_type_header = response.headers.get("content-type", "")
                if "application/pdf" in content_type_header or source.lower().endswith(".pdf"):
                    return self._handle_pdf_url(source, response.content)
                html = response.text
            except BlockedURLError:
                # The SSRF guard refused this URL (or a redirect hop). That is a policy
                # decision, not a network failure: a third-party reader must not be used
                # to fetch what we just refused to fetch ourselves.
                raise
            except Exception as e:
                fetch_error = _describe_fetch_error(e)
                logger.debug(f"Direct fetch failed ({e}), trying Jina Reader")

            # Extract content: Jina Reader → readability → BeautifulSoup.
            # Privacy: skip Jina Reader when privacy_mode or allow_jina_reader=False.
            # A domain allow-list (a2kb mcp --ingest-allow-domain) also rules it out: Jina
            # fetches and follows redirects from its own servers, outside our control.
            allow_cloud = (
                self.profile.allow_jina_reader
                and not self.profile.privacy_mode
                and not allowlist_active()
            )
            try:
                title, md_content = _extract_content(html or "", source, allow_cloud=allow_cloud)
            except Exception as e:
                if fetch_error:
                    # "Document is empty" alone hides the real cause (403, timeout, ...).
                    raise ValueError(
                        f"Could not fetch the page ({fetch_error}) and no fallback produced "
                        f"content ({e}). URL: {source}"
                    ) from e
                raise

            # Guard: reject extractions that are too short to be real content.
            # Common causes: login walls ("Don't miss what's happening"),
            # cookie consent pages, empty SPA shells, rate-limit pages.
            min_content_chars = 100
            if len(md_content.strip()) < min_content_chars:
                raise ValueError(
                    f"Extracted content too short ({len(md_content.strip())} chars, "
                    f"min {min_content_chars}). Likely a login wall or empty page. "
                    + (f"Direct fetch failed: {fetch_error}. " if fetch_error else "")
                    + f"URL: {source}"
                )

            # Build filename with hash to prevent collisions
            slug = slugify(title)
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            short_hash = content_hash(md_content)[:8]
            filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = make_raw_key(self.profile.prefix, filename)

            # Build full content with frontmatter
            full_content = build_frontmatter_content(
                title=title,
                body=md_content,
                source=source,
                source_type=ContentType.URL,
                tags=self.tags,
                original_link=source,
                output_language=self.profile.output_language,
            )

            self.s3.upload(raw_key, full_content)
            logger.info(f"Ingested URL: {source} → {raw_key}")

            return IngestResult(
                source=source,
                content_type=ContentType.URL,
                raw_key=raw_key,
                title=title,
            )

        except Exception as e:
            logger.error(f"URL ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=ContentType.URL,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )

    def _handle_pdf_url(self, source: str, content: bytes) -> IngestResult:
        """Handle PDF URL by downloading and extracting text."""
        import tempfile
        from pathlib import Path

        temp_path = None
        try:
            from agent2kb.ingest.handlers.document import DocumentHandler

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(content)
                temp_path = Path(f.name)

            doc_handler = DocumentHandler(profile=self.profile, s3=self.s3, tags=self.tags)
            return doc_handler.process(source=source, file_path=temp_path, remote=True)

        except Exception as e:
            return IngestResult(
                source=source,
                content_type=ContentType.PDF_URL,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)


class RemoteFileHandler(BaseHandler):
    """Fetch a URL that names a file (.md, .txt, .docx, .xlsx, ...) and convert the download.

    Acquisition and conversion are separate: the bytes come through the SSRF guard
    (``safe_get``), land in a private temporary directory, and the handler for the local
    file type converts them, so a remote file gets the same treatment as ``--file``.
    """

    # URL types (``detect_url_type``) this handler fetches; each is converted by the
    # handler of the same local file type.
    KINDS = frozenset({"markdown", "text", "document"})

    def process(self, source: str, **kwargs) -> IngestResult:
        import tempfile
        from pathlib import Path, PurePosixPath
        from urllib.parse import unquote, urlparse

        from agent2kb.ingest.base import IngestPipeline
        from agent2kb.utils import detect_url_type

        kind = detect_url_type(source)
        content_type = ContentType.DOCUMENT if kind == "document" else ContentType.TEXT
        try:
            if kind not in self.KINDS:
                raise ValueError(f"Remote files of this type are not supported: {source}")
            from agent2kb.ingest.handlers.document import _LOCAL_ONLY

            suffix = PurePosixPath(unquote(urlparse(source).path)).suffix.lower()
            if suffix in _LOCAL_ONLY:
                # Refused before anything is downloaded; DocumentHandler checks again.
                raise ValueError(_LOCAL_ONLY[suffix])
            _validate_url(source)
            response = _safe_get(source)
            header = response.headers.get("content-type", "").lower()
            if "html" in header:
                if kind == "document":
                    raise ValueError(
                        f"The server returned a web page, not a document file. URL: {source}"
                    )
                # e.g. a repository viewer page for README.md: treat it as a web page.
                return URLHandler(profile=self.profile, s3=self.s3, tags=self.tags).process(
                    source=source
                )
            data = response.content
            if not data:
                raise ValueError(f"The download is empty. URL: {source}")

            remote = PurePosixPath(unquote(urlparse(source).path))
            # Only a slug of the remote name is used locally: never a path from the URL.
            name = (slugify(remote.stem) or "download") + remote.suffix.lower()
            with tempfile.TemporaryDirectory(prefix="kb-dl-") as tmp:
                local = Path(tmp) / name
                local.write_bytes(data)
                pipeline = IngestPipeline(profile=self.profile, s3=self.s3, tags=self.tags)
                handler = pipeline._get_handler(kind)
                # remote=True: the copy is untrusted downloaded content, not a user's file.
                return handler.process(source=source, file_path=local, remote=True)
        except Exception as e:
            logger.error(f"Remote file ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=content_type,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )


def _extract_content(html: str, url: str, allow_cloud: bool = True) -> tuple[str, str]:
    """Extract main content using Jina Reader → readability → BeautifulSoup.

    Jina Reader handles Twitter/X, Reddit, GitHub, HN, and general articles
    much better than readability-lxml. When allow_cloud is False (privacy
    mode), only local readability/BeautifulSoup are used.
    """
    if allow_cloud:
        title, md = _try_jina_reader(url)
        if md:
            return title, _clean_markdown(md)

    # Local fallback: readability-lxml + markdownify (works offline)
    return _try_readability(html, url)


def _try_jina_reader(url: str) -> tuple[str, str]:
    """Try Jina Reader API for content extraction.

    Handles Twitter/X, Reddit, Medium, GitHub, and general articles.
    Free tier, no API key needed. Returns clean markdown.
    Falls back silently on failure.
    """
    from agent2kb.net_guard import safe_get

    try:
        reader_url = f"https://r.jina.ai/{url}"
        if allowlist_active():  # never hand an allow-listed fetch to a third party
            return "", ""
        resp = safe_get(reader_url, timeout=20, raise_for_status=False)
        if resp.status_code != 200 or not resp.text.strip():
            return "", ""

        raw = resp.text.strip()

        # Jina Reader returns metadata headers then markdown content
        title = ""
        md_lines = []
        in_content = False
        for line in raw.split("\n"):
            if line.startswith("Title: "):
                title = line[7:].strip()
            elif line.startswith("Markdown Content:"):
                in_content = True
            elif in_content:
                md_lines.append(line)

        md_content = "\n".join(md_lines).strip()
        if not md_content:
            # Some responses don't have the "Markdown Content:" header
            md_content = raw

        if len(md_content) < 50:
            return "", ""

        logger.info(f"Jina Reader extracted {len(md_content)} chars from {url}")
        return title, md_content
    except Exception as e:
        logger.debug(f"Jina Reader failed for {url}: {e}")
        return "", ""


def _try_readability(html: str, url: str) -> tuple[str, str]:
    """Fallback: readability-lxml + markdownify."""
    from markdownify import markdownify

    title = ""
    try:
        from readability import Document

        doc = Document(html, url=url)
        title = doc.short_title() or ""
        clean_html = doc.summary()
    except ImportError:
        logger.warning("readability-lxml not installed, falling back to basic extraction")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        for tag in soup.find_all(["nav", "header", "footer", "script", "style", "aside"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup.find("body")
        clean_html = str(main) if main else str(soup)

    if not title:
        from urllib.parse import urlparse

        title = urlparse(url).path.split("/")[-1] or urlparse(url).netloc

    # Preserve images (![](url) markdown) — they're first-class content in
    # modern articles (screenshots, diagrams, Medium images). If tracking
    # pixels slip through, the compile-time LLM is instructed to skip them.
    md_content = markdownify(clean_html, heading_style="ATX")
    return title, _clean_markdown(md_content)


def _clean_markdown(text: str) -> str:
    """Clean up converted markdown."""
    import re

    # Remove excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # Remove leading/trailing whitespace per line
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()
