"""Document conversion: real PDF text, and failures that never become sources (F05)."""

import subprocess
import sys

import pytest

from agent2kb.ingest.handlers import document
from agent2kb.ingest.handlers.document import DocumentHandler
from agent2kb.models import Frontmatter, ProfileConfig


class _S3:
    def __init__(self):
        self.store: dict[str, str] = {}

    def download(self, key):
        return self.store[key]

    def upload(self, key, body, content_type="text/markdown"):
        self.store[key] = body


def _handler():
    profile = ProfileConfig(name="p", bucket="b", prefix="projects/p/", region="us-east-1")
    return DocumentHandler(profile=profile, s3=_S3(), tags=[])


def make_pdf(text: str | None) -> bytes:
    """A minimal valid one-page PDF; ``text`` None gives a page with no text layer."""
    stream = b"" if text is None else (
        b"BT /F1 24 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_valid_pdf_text_is_ingested(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(make_pdf("Hello quarterly notes"))
    h = _handler()
    result = h.process(source=str(pdf), file_path=pdf)
    assert result.success, result.error
    _, body = Frontmatter.from_yaml_block(h.s3.store[result.raw_key])
    assert "Hello quarterly notes" in body


def test_pdf_never_goes_to_pandoc(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")
    monkeypatch.setattr(
        DocumentHandler, "_convert_pandoc", lambda self, p: pytest.fail("pandoc used for PDF")
    )
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf("Some text"))
    assert _handler().process(source=str(pdf), file_path=pdf).success


def test_pdf_without_text_fails(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(make_pdf(None))
    h = _handler()
    result = h.process(source=str(pdf), file_path=pdf)
    assert not result.success and "no extractable text" in result.error
    assert h.s3.store == {}


def test_corrupt_pdf_fails(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"\x00\x01 not a pdf")
    h = _handler()
    result = h.process(source=str(pdf), file_path=pdf)
    assert not result.success and h.s3.store == {}


def test_missing_pypdf_names_the_extra(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # simulate the package being absent
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf("x"))
    h = _handler()
    result = h.process(source=str(pdf), file_path=pdf)
    assert not result.success
    assert "agent2kb[media]" in result.error
    assert h.s3.store == {}


def test_failing_pandoc_is_a_failure_not_a_source(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Unknown input format")

    monkeypatch.setattr(document.subprocess, "run", fake_run)
    f = tmp_path / "book.odt"
    f.write_bytes(b"PK\x03\x04 zip")
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert not result.success
    assert "pandoc could not convert" in result.error
    assert h.s3.store == {}


def test_empty_conversion_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(DocumentHandler, "_convert_pandoc", lambda self, p: ("T", "  \n"))
    f = tmp_path / "empty.odt"
    f.write_bytes(b"x")
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert not result.success and "no text" in result.error
    assert h.s3.store == {}


def _no_pandoc(cmd, **kw):
    raise FileNotFoundError("pandoc")


def test_without_pandoc_binary_is_not_decoded_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(document.subprocess, "run", _no_pandoc)
    f = tmp_path / "book.odt"
    f.write_bytes(b"PK\x03\x04\x00\x00\xff\xfe binary")
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert not result.success and "needs pandoc" in result.error
    assert h.s3.store == {}


def test_without_pandoc_utf8_text_is_still_read(tmp_path, monkeypatch):
    monkeypatch.setattr(document.subprocess, "run", _no_pandoc)
    f = tmp_path / "notes.rst"
    f.write_text("Title\n=====\n\nplain notes")
    result = _handler().process(source=str(f), file_path=f)
    assert result.success, result.error


@pytest.mark.parametrize("name", ["old.doc", "old.xls"])
def test_legacy_office_formats_are_explicitly_unsupported(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"\xd0\xcf\x11\xe0 legacy")
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert not result.success and "not supported" in result.error
    assert h.s3.store == {}


@pytest.mark.parametrize("name", ["book.odt", "book.epub", "letter.rtf"])
def test_pandoc_formats_go_to_the_document_handler(name):
    from pathlib import Path

    from agent2kb.utils import detect_file_type

    assert detect_file_type(Path(name)) == "document"


def test_unknown_binary_file_is_not_ingested_as_text(tmp_path):
    from agent2kb.ingest.handlers.markdown import MarkdownHandler

    f = tmp_path / "archive.bin"
    f.write_bytes(b"\x7fELF\x00\x00\x01binary")
    h = _handler()
    result = MarkdownHandler(profile=h.profile, s3=h.s3, tags=[]).process(
        source=str(f), file_path=f
    )
    assert not result.success and "binary" in result.error
    assert h.s3.store == {}


# --- bounds for untrusted documents: zip bombs, huge sheets, huge PDFs ---------------------


def _zip(path, members: dict[str, bytes]):
    import zipfile

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _no_convert(*args, **kwargs):
    pytest.fail("a rejected container reached the converter")


@pytest.mark.parametrize("name", ["bomb.docx", "bomb.xlsx", "bomb.odt"])
def test_zip_bomb_is_rejected_before_conversion(tmp_path, monkeypatch, name):
    monkeypatch.setattr(document, "_ZIP_MAX_UNCOMPRESSED", 1024 * 1024)
    for conv in ("_convert_docx", "_convert_xlsx", "_convert_pandoc"):
        monkeypatch.setattr(DocumentHandler, conv, _no_convert)
    f = _zip(tmp_path / name, {"word/document.xml": b"\0" * (2 * 1024 * 1024)})
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert not result.success and "would expand" in result.error
    assert h.s3.store == {}


def test_highly_compressed_zip_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_ZIP_RATIO_FLOOR", 1024)
    monkeypatch.setattr(DocumentHandler, "_convert_docx", _no_convert)
    f = _zip(tmp_path / "a.docx", {"word/document.xml": b"a" * (512 * 1024)})
    result = _handler().process(source=str(f), file_path=f)
    assert not result.success and "decompression bomb" in result.error


def test_zip_with_too_many_members_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_ZIP_MAX_MEMBERS", 5)
    monkeypatch.setattr(DocumentHandler, "_convert_xlsx", _no_convert)
    f = _zip(tmp_path / "a.xlsx", {f"xl/part{i}.xml": b"x" for i in range(6)})
    result = _handler().process(source=str(f), file_path=f)
    assert not result.success and "parts" in result.error


def test_ordinary_docx_still_converts(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Plan", level=1)
    d.add_paragraph("the quarterly notes")
    f = tmp_path / "plan.docx"
    d.save(str(f))
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert result.success, result.error
    _, body = Frontmatter.from_yaml_block(h.s3.store[result.raw_key])
    assert "the quarterly notes" in body and "Truncated" not in body


def test_docx_text_is_bounded(tmp_path, monkeypatch):
    docx = pytest.importorskip("docx")
    monkeypatch.setattr(document, "_MAX_BODY_CHARS", 1000)
    d = docx.Document()
    for i in range(200):
        d.add_paragraph(f"paragraph number {i} with some words")
    f = tmp_path / "long.docx"
    d.save(str(f))
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert result.success, result.error
    _, body = Frontmatter.from_yaml_block(h.s3.store[result.raw_key])
    assert "paragraph number 0 " in body and "paragraph number 199" not in body
    assert "Truncated" in body and len(body) < 1500


def test_xlsx_far_cell_does_not_expand_into_padding(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(document, "_XLSX_MAX_ROWS", 50)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"], ws["B1"], ws["A2"], ws["B2"] = "name", "value", "alpha", 1
    ws["XFD1048576"] = "far away"  # declares a 16384 x 1048576 sheet
    f = tmp_path / "far.xlsx"
    wb.save(str(f))
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert result.success, result.error
    _, body = Frontmatter.from_yaml_block(h.s3.store[result.raw_key])
    table = [line for line in body.splitlines() if line.startswith("|")]
    assert table[0].startswith("| name | value |") and table[2].startswith("| alpha | 1 |")
    assert len(table) <= 51  # header, separator, at most 49 more rows
    assert max(line.count("|") for line in table) <= document._XLSX_MAX_COLS + 1
    assert "Truncated" in body


def test_ordinary_xlsx_output_is_unchanged(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append(["name", "value"])
    ws.append(["alpha", 1])
    ws.append(["beta", None])
    f = tmp_path / "small.xlsx"
    wb.save(str(f))
    _, body = _handler()._convert_xlsx(f)
    assert body == (
        "## Sheet\n\n| name | value |\n| --- | --- |\n| alpha | 1 |\n| beta |  |\n"
    )


def test_xlsx_body_and_cells_are_bounded(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setattr(document, "_MAX_BODY_CHARS", 2000)
    monkeypatch.setattr(document, "_XLSX_MAX_CELL_CHARS", 10)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["h"])
    for i in range(500):
        ws.append([f"row {i} " + "x" * 50])
    f = tmp_path / "big.xlsx"
    wb.save(str(f))
    _, body = _handler()._convert_xlsx(f)
    assert "x" * 11 not in body
    assert "row 499" not in body and "Truncated" in body and len(body) < 2500


class _FakePage:
    def __init__(self, text, calls):
        self._text, self._calls = text, calls

    def extract_text(self):
        self._calls.append(1)
        return self._text


def _fake_pypdf(monkeypatch, pages: int, text: str):
    import types

    calls: list[int] = []

    class Reader:
        is_encrypted = False
        metadata = None

        def __init__(self, path):
            self.pages = (_FakePage(f"{text} {i}", calls) for i in range(pages))

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
    return calls


def test_pdf_page_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_PDF_MAX_PAGES", 3)
    calls = _fake_pypdf(monkeypatch, pages=10_000, text="page")
    f = tmp_path / "many.pdf"
    f.write_bytes(b"%PDF-1.4")
    _, body = _handler()._convert_pdf(f)
    assert len(calls) == 3
    assert "page 2" in body and "page 3" not in body and "Truncated" in body


def test_pdf_text_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_MAX_BODY_CHARS", 1000)
    calls = _fake_pypdf(monkeypatch, pages=10_000, text="y" * 400)
    f = tmp_path / "long.pdf"
    f.write_bytes(b"%PDF-1.4")
    _, body = _handler()._convert_pdf(f)
    assert len(calls) == 3 and "Truncated" in body


def test_pandoc_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_MAX_BODY_CHARS", 1000)
    monkeypatch.setattr(DocumentHandler, "_convert_pandoc", lambda self, p: ("T", "z" * 5000))
    f = tmp_path / "book.rst"
    f.write_text("x")
    h = _handler()
    result = h.process(source=str(f), file_path=f)
    assert result.success, result.error
    _, body = Frontmatter.from_yaml_block(h.s3.store[result.raw_key])
    assert body.startswith("z" * 1000) and "z" * 1001 not in body and "Truncated" in body


def _understate_sizes(path, real_size, fake_size):
    """Rewrite every declared uncompressed size in a stored zip from real to fake."""
    import struct
    raw = path.read_bytes()
    raw = raw.replace(struct.pack("<I", real_size), struct.pack("<I", fake_size))
    path.write_bytes(raw)


def test_zip_with_lying_sizes_is_rejected(tmp_path, monkeypatch):
    # Pandoc does not honour the declared sizes, so the check must not trust them either.
    monkeypatch.setattr(document, "_ZIP_MAX_UNCOMPRESSED", 1024 * 1024)
    monkeypatch.setattr(document, "_ZIP_RATIO_FLOOR", 1 << 40)
    monkeypatch.setattr(DocumentHandler, "_convert_pandoc", _no_convert)
    real = 2 * 1024 * 1024 + 7
    f = _zip(tmp_path / "lie.odt", {"content.xml": b"\0" * real})
    _understate_sizes(f, real, 100)
    result = _handler().process(source=str(f), file_path=f)
    assert not result.success
    # Measured from the raw deflate stream, so the understated header cannot hide it.
    assert "expands past" in result.error


def test_zip_with_understated_size_under_cap_is_rejected(tmp_path, monkeypatch):
    # Even below the cap, a size that does not match the stream is a crafted archive.
    monkeypatch.setattr(document, "_ZIP_RATIO_FLOOR", 1 << 40)
    monkeypatch.setattr(DocumentHandler, "_convert_pandoc", _no_convert)
    real = 64 * 1024 + 7
    f = _zip(tmp_path / "lie.odt", {"content.xml": b"\0" * real})
    _understate_sizes(f, real, 100)
    result = _handler().process(source=str(f), file_path=f)
    assert not result.success and "do not match" in result.error


def test_zip_real_expansion_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "_ZIP_MAX_UNCOMPRESSED", 1024 * 1024)
    f = _zip(tmp_path / "a.odt", {f"p{i}.xml": b"\0" * (300 * 1024) for i in range(4)})
    # Declared total is over the cap too, so this pins the cap rather than the lie.
    with pytest.raises(ValueError):
        document.check_zip_container(f)


def test_zip_with_mismatched_compression_method_is_rejected(tmp_path, monkeypatch):
    # Local header says stored while the central directory says deflated.
    import struct
    monkeypatch.setattr(DocumentHandler, "_convert_pandoc", _no_convert)
    f = _zip(tmp_path / "mix.odt", {"content.xml": b"\0" * 4096})
    raw = bytearray(f.read_bytes())
    assert raw[:4] == b"PK\x03\x04"
    raw[8:10] = struct.pack("<H", 0)  # local header compression method
    f.write_bytes(bytes(raw))
    result = _handler().process(source=str(f), file_path=f)
    assert not result.success and "corrupt zip" in result.error
