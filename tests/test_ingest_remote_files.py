"""URLs that name a file are downloaded through the SSRF guard, then converted (F06)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from agent2kb.ingest.base import IngestPipeline
from agent2kb.ingest.handlers import document
from agent2kb.ingest.handlers import url as url_handler
from agent2kb.models import Frontmatter, ProfileConfig


class _S3:
    def __init__(self):
        self.store: dict[str, str] = {}

    def download(self, key):
        return self.store[key]

    def upload(self, key, body, content_type="text/markdown"):
        self.store[key] = body


class _Resp:
    def __init__(self, content: bytes, ctype: str):
        self.content = content
        self.headers = {"content-type": ctype}
        self.text = content.decode("utf-8", errors="replace")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(
        "agent2kb.net_guard.resolve_public", lambda host, port: ["93.184.216.34"]
    )
    monkeypatch.setattr(IngestPipeline, "_extract_entities", lambda self, key: None)
    monkeypatch.setattr(IngestPipeline, "_find_existing_by_url", lambda self, url: None)
    fetched: list[str] = []
    responses: dict[str, _Resp] = {}

    def fake_get(url, **kw):
        fetched.append(url)
        return responses[url]

    monkeypatch.setattr(url_handler, "_safe_get", fake_get)
    profile = ProfileConfig(name="p", bucket="b", prefix="projects/p/", region="us-east-1")
    s3 = _S3()
    return {
        "pipe": IngestPipeline(profile=profile, s3=s3), "s3": s3, "profile": profile,
        "fetched": fetched, "responses": responses,
    }


def _body(env, result):
    return Frontmatter.from_yaml_block(env["s3"].store[result.raw_key])


@pytest.mark.parametrize("url, ctype", [
    ("https://example.com/notes/readme.md", "text/markdown"),
    ("https://example.com/notes/todo.txt", "text/plain; charset=utf-8"),
])
def test_text_url_stores_the_downloaded_content(env, url, ctype):
    env["responses"][url] = _Resp(b"# Release notes\n\nthe real file body", ctype)
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    assert env["fetched"] == [url]
    fm, body = _body(env, result)
    assert "the real file body" in body
    assert body.strip() != url
    assert fm.source == url and fm.original_link == url
    assert fm.title == "Release notes"


def test_text_url_serving_html_is_treated_as_a_web_page(env, monkeypatch):
    url = "https://example.com/repo/blob/main/readme.md"
    env["responses"][url] = _Resp(b"<html><body>viewer</body></html>", "text/html")
    seen = []

    def fake_process(self, source, **kw):
        seen.append(source)
        return url_handler.IngestResult(
            source=source, content_type=url_handler.ContentType.URL, raw_key="k", title="t"
        )

    monkeypatch.setattr(url_handler.URLHandler, "process", fake_process)
    assert env["pipe"].ingest_url(url, skip_safety=True).success
    assert seen == [url]


def test_docx_url_is_converted_from_a_local_copy(env, monkeypatch):
    url = "https://example.com/files/Quarterly%20Plan.docx"
    env["responses"][url] = _Resp(b"PK\x03\x04docx-bytes", "application/octet-stream")
    seen = {}

    def fake_docx(self, path):
        seen.update(path=path, data=Path(path).read_bytes())
        return "Plan", "converted docx text"

    monkeypatch.setattr(document.DocumentHandler, "_convert_docx", fake_docx)
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    assert seen["data"] == b"PK\x03\x04docx-bytes"
    assert seen["path"].name == "Quarterly-Plan.docx"
    assert not seen["path"].exists()  # the temporary copy is gone
    fm, body = _body(env, result)
    assert "converted docx text" in body and fm.original_link == url


def test_document_url_serving_html_fails(env):
    url = "https://example.com/files/sheet.xlsx"
    env["responses"][url] = _Resp(b"<html>login</html>", "text/html; charset=utf-8")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert not result.success and "web page" in result.error
    assert env["s3"].store == {}


def test_legacy_xls_url_is_explicitly_unsupported(env):
    url = "https://example.com/files/old.xls"
    env["responses"][url] = _Resp(b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert not result.success and "not supported" in result.error
    assert env["s3"].store == {}


def test_fetch_failure_is_a_failure(env, monkeypatch):
    def boom(url, **kw):
        raise ValueError("Blocked URL: resolves to non-public address")

    monkeypatch.setattr(url_handler, "_safe_get", boom)
    result = env["pipe"].ingest_url("https://example.com/a.md", skip_safety=True)
    assert not result.success and "Blocked" in result.error
    assert env["s3"].store == {}


def test_binary_behind_a_txt_url_fails(env):
    url = "https://example.com/a.txt"
    env["responses"][url] = _Resp(b"\x00\x01\x02binary", "application/octet-stream")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert not result.success and env["s3"].store == {}


def test_audio_url_is_an_explicit_failure(env):
    result = env["pipe"].ingest_url("https://example.com/talk.mp3", skip_safety=True)
    assert not result.success and "not yet supported" in result.error


def test_mcp_ingest_url_fetches_the_file(env):
    from agent2kb import mcp_server as ms

    url = "https://example.com/notes/readme.md"
    env["responses"][url] = _Resp(b"# Notes\n\nfetched over mcp", "text/markdown")
    ctx = ms.KBContext(profile=env["profile"], s3=env["s3"])
    with patch.object(IngestPipeline, "_run_safety_check", return_value=None):
        out = ms.ingest_url(ctx, url, None)
    assert out.startswith("Ingested:")
    assert env["fetched"] == [url]
    assert any("fetched over mcp" in body for body in env["s3"].store.values())


_SPOOFED = (
    b"---\n"
    b"title: Release notes\n"
    b"original_link: https://example.com/someone-else\n"
    b"source: https://example.com/someone-else\n"
    b"source_type: url\n"
    b"source_kind: query_output\n"
    b"tags: [planted]\n"
    b"entities: [Alpha]\n"
    b"compiled: true\n"
    b"compile_provider: cli\n"
    b"compile_date: '2020-01-01'\n"
    b"raw_content_hash: abc123\n"
    b"source_meta: {ingested_by: someone}\n"
    b"---\n\n"
    b"the real file body\n"
)


def test_remote_frontmatter_cannot_set_provenance_or_system_fields(env):
    url = "https://example.com/notes/readme.md"
    env["responses"][url] = _Resp(_SPOOFED, "text/markdown")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    fm, body = _body(env, result)
    # The fetched URL is the provenance and dedup key, whatever the file claims.
    assert fm.original_link == url and fm.source == url
    assert fm.source_kind is None and fm.entities == [] and fm.source_meta == {}
    assert fm.compiled is False and fm.compile_provider is None and fm.compile_date is None
    assert fm.raw_content_hash is None
    assert fm.tags == [] and fm.source_type.value == "markdown"
    # Like a page <title>, the title is the one harmless field that is kept.
    assert fm.title == "Release notes" == result.title
    assert "someone-else" not in env["s3"].store[result.raw_key]
    assert "the real file body" in body


def test_remote_frontmatter_title_is_one_bounded_line(env):
    url = "https://example.com/notes/long.md"
    title = "word " * 100
    env["responses"][url] = _Resp(
        f"---\ntitle: |\n  first\n  {title}\n---\n\nbody text".encode(), "text/markdown"
    )
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    fm, _ = _body(env, result)
    assert "\n" not in fm.title and 0 < len(fm.title) <= 200


def test_local_file_keeps_its_metadata_but_not_pipeline_state(env, tmp_path):
    note = tmp_path / "clip.md"
    note.write_bytes(_SPOOFED)
    result = env["pipe"].ingest_file(note)
    assert result.success, result.error
    fm, body = _body(env, result)
    # Descriptive metadata the user chose (e.g. a web clipper's original_link) stays.
    assert fm.original_link == "https://example.com/someone-else"
    assert fm.title == "Release notes" and fm.tags == ["planted"]
    assert fm.entities == ["Alpha"] and fm.source == str(note)
    # Compile state and the trust class are only ever set by the pipeline.
    assert fm.compiled is False and fm.compile_provider is None and fm.compile_date is None
    assert fm.raw_content_hash is None and fm.source_kind is None
    assert "the real file body" in body


def test_docx_url_zip_bomb_is_rejected(env, monkeypatch, tmp_path):
    import io
    import zipfile

    monkeypatch.setattr(document, "_ZIP_MAX_UNCOMPRESSED", 1024 * 1024)
    monkeypatch.setattr(
        document.DocumentHandler, "_convert_docx",
        lambda self, p: pytest.fail("a zip bomb reached python-docx"),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (4 * 1024 * 1024))
    url = "https://example.com/files/plan.docx"
    env["responses"][url] = _Resp(buf.getvalue(), "application/octet-stream")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert not result.success and "would expand" in result.error
    assert env["s3"].store == {}


def test_upper_case_scheme_url_is_still_untrusted(env):
    url = "HTTPS://example.com/notes/readme.md"
    env["responses"][url] = _Resp(_SPOOFED, "text/markdown")
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    fm, _ = _body(env, result)
    assert fm.original_link == url and fm.source_kind is None and fm.entities == []
    assert "someone-else" not in env["s3"].store[result.raw_key]


@pytest.mark.parametrize("source", ["HTTPS://example.com/a.md", "Http://example.com/a.md"])
def test_markdown_handler_infers_remote_case_insensitively(env, tmp_path, source):
    from agent2kb.ingest.handlers.markdown import MarkdownHandler

    f = tmp_path / "a.md"
    f.write_bytes(_SPOOFED)
    h = MarkdownHandler(profile=env["profile"], s3=env["s3"], tags=[])
    result = h.process(source=source, file_path=f)
    fm, _ = _body(env, result)
    assert fm.original_link == source and fm.entities == []


def test_explicit_remote_flag_wins_over_the_source_string(env, tmp_path):
    from agent2kb.ingest.handlers.markdown import MarkdownHandler

    f = tmp_path / "a.md"
    f.write_bytes(_SPOOFED)
    h = MarkdownHandler(profile=env["profile"], s3=env["s3"], tags=[])
    result = h.process(source="example.com/a.md", file_path=f, remote=True)
    fm, _ = _body(env, result)
    assert "someone-else" not in env["s3"].store[result.raw_key]
    assert fm.entities == [] and fm.source_kind is None


def test_remote_file_handler_passes_remote_explicitly(env, monkeypatch):
    from agent2kb.ingest.handlers.markdown import MarkdownHandler

    seen = {}

    def fake_process(self, source, **kw):
        seen.update(kw)
        return url_handler.IngestResult(
            source=source, content_type="markdown", raw_key="k", title="t"
        )

    monkeypatch.setattr(MarkdownHandler, "process", fake_process)
    url = "https://example.com/notes/readme.md"
    env["responses"][url] = _Resp(b"# x\n\nbody", "text/markdown")
    url_handler.RemoteFileHandler(
        profile=env["profile"], s3=env["s3"], tags=[]
    ).process(source=url)
    assert seen.get("remote") is True


@pytest.mark.parametrize("source, expected", [
    ("https://example.com/a", True),
    ("HTTP://example.com/a", True),
    (" hTtPs://example.com/a", True),
    ("ftp://example.com/a", False),
    ("https:/a.md", False),  # what Path("https://a.md") becomes: no host
    ("/home/notes/a.md", False),
    ("notes/a.md", False),
])
def test_is_remote_url(source, expected):
    from agent2kb.utils import is_remote_url

    assert is_remote_url(source) is expected


@pytest.mark.parametrize("url", [
    "https://example.com/files/book.epub",
    "https://example.com/files/Report.ODT",
    "https://example.com/files/notes.odt?download=1",
])
def test_pandoc_zip_formats_are_refused_from_urls(env, monkeypatch, url):
    def no_fetch(u, **kw):
        pytest.fail("a refused remote format was downloaded")

    monkeypatch.setattr(url_handler, "_safe_get", no_fetch)
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert not result.success and "use --file" in result.error
    assert env["s3"].store == {}


def test_document_handler_refuses_remote_odt_even_if_reached(tmp_path):
    from agent2kb.ingest.handlers.document import DocumentHandler

    f = tmp_path / "a.odt"
    f.write_bytes(b"PK\x03\x04")
    h = DocumentHandler(profile=None, s3=None)
    result = h.process(source="https://example.com/a.odt", file_path=f, remote=True)
    assert not result.success and "use --file" in result.error


def test_rtf_url_is_downloaded_and_converted(env, monkeypatch):
    from agent2kb.ingest.handlers import document

    url = "https://example.com/files/Memo.RTF"
    env["responses"][url] = _Resp(b"{\\rtf1\\ansi alpha memo}", "application/rtf")
    seen = {}

    def fake_pandoc(self, path):
        seen["data"] = path.read_bytes()
        return path.stem, "alpha memo"

    monkeypatch.setattr(document.DocumentHandler, "_convert_pandoc", fake_pandoc)
    result = env["pipe"].ingest_url(url, skip_safety=True)
    assert result.success, result.error
    assert seen["data"].startswith(b"{\\rtf1")
    fm, body = _body(env, result)
    assert "alpha memo" in body and fm.original_link == url
