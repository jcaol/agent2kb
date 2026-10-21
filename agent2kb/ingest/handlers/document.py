"""Document ingest handler: PDF, DOCX, XLSX conversion."""

from __future__ import annotations

import datetime
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.extras import import_optional
from agent2kb.ingest.base import BaseHandler, build_frontmatter_content
from agent2kb.models import ContentType, IngestResult
from agent2kb.utils import content_hash, is_remote_url, make_raw_key, slugify


class DocumentHandler(BaseHandler):
    """Handle document ingestion: PDF (pypdf), DOCX, XLSX → Markdown; pandoc for others."""

    def process(self, source: str, **kwargs) -> IngestResult:
        file_path = kwargs.get("file_path")
        if not file_path:
            return IngestResult(
                source=source,
                content_type=ContentType.DOCUMENT,
                raw_key="",
                title="",
                success=False,
                error="Document handler requires a file_path.",
            )

        path = Path(file_path)
        ext = path.suffix.lower()

        try:
            if ext in _UNSUPPORTED:
                raise ValueError(f"{path.name}: {_UNSUPPORTED[ext]}")
            if kwargs.get("remote") and ext in _LOCAL_ONLY:
                raise ValueError(f"{path.name}: {_LOCAL_ONLY[ext]}")
            if ext != ".pdf" and zipfile.is_zipfile(path):
                # Office/ODF/EPUB files are zip containers; the download cap only bounds
                # the compressed size, so check what they would expand to first. A file
                # that is not a zip at all is left to the converter's own error.
                check_zip_container(path)
            if ext == ".docx":
                title, body = self._convert_docx(path)
            elif ext == ".xlsx":
                title, body = self._convert_xlsx(path)
            elif ext == ".pdf":
                title, body = self._convert_pdf(path)
            else:
                title, body = self._convert_pandoc(path)

            # A conversion that produced nothing must not become a source: compile would
            # build pages from an empty or placeholder body.
            if not body or not body.strip():
                raise ValueError(f"{path.name}: the conversion produced no text")

            if not title:
                title = path.stem
            body = _cap_body(body, path.name)

            slug = slugify(title)
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            short_hash = content_hash(body)[:8]
            filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = make_raw_key(self.profile.prefix, filename)

            full_content = build_frontmatter_content(
                title=title,
                body=body,
                source=source,
                source_type=ContentType.DOCUMENT,
                tags=self.tags,
                original_link=(
                    source if kwargs.get("remote") or is_remote_url(source) else ""
                ),
                output_language=self.profile.output_language,
            )

            self.s3.upload(raw_key, full_content)
            return IngestResult(
                source=source, content_type=ContentType.DOCUMENT, raw_key=raw_key, title=title
            )

        except Exception as e:
            logger.error(f"Document ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=ContentType.DOCUMENT,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )

    def _convert_docx(self, path: Path) -> tuple[str, str]:
        """Convert DOCX to markdown using python-docx."""
        Document = import_optional("docx", "media", "Reading .docx files").Document

        try:
            doc = Document(str(path))
        except Exception as e:
            raise ValueError(
                f"{path.name} is not a readable .docx file (corrupt or wrong format)"
            ) from e
        title = ""
        lines = []
        budget = _Budget(path.name)

        for para in doc.paragraphs:
            if budget.spent:
                break
            text = para.text.strip()
            budget.take(len(text) + 1)
            if not text:
                lines.append("")
                continue

            style = para.style.name.lower() if para.style else ""
            if "heading 1" in style:
                if not title:
                    title = text
                lines.append(f"# {text}")
            elif "heading 2" in style:
                lines.append(f"## {text}")
            elif "heading 3" in style:
                lines.append(f"### {text}")
            elif "list" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)

        # Extract tables
        for table in doc.tables:
            if budget.spent:
                break
            lines.append("")
            for i, row in enumerate(table.rows):
                if budget.spent:
                    break
                cells = [cell.text.strip() for cell in row.cells]
                line = "| " + " | ".join(cells) + " |"
                budget.take(len(line) + 1)
                lines.append(line)
                if i == 0:
                    lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
            lines.append("")

        return title or path.stem, "\n".join(lines) + budget.note()

    def _convert_xlsx(self, path: Path) -> tuple[str, str]:
        """Convert XLSX/XLS to markdown tables using openpyxl."""
        load_workbook = import_optional("openpyxl", "media", "Reading .xlsx files").load_workbook

        try:
            wb = load_workbook(str(path), read_only=True, data_only=True)
        except Exception as e:
            raise ValueError(
                f"{path.name} is not a readable .xlsx file (corrupt or wrong format)"
            ) from e
        try:
            sections = []
            budget = _Budget(path.name)

            for sheet_name in wb.sheetnames[:_XLSX_MAX_SHEETS]:
                if budget.spent:
                    break
                ws = wb[sheet_name]
                # Rows are read lazily and bounded: a sheet's declared dimension or a cell
                # in column XFD or row 1048576 would otherwise be padded out to millions of
                # empty cells.
                declared_cols = ws.max_column
                if (ws.max_row or 0) > _XLSX_MAX_ROWS or (declared_cols or 0) > _XLSX_MAX_COLS:
                    budget.truncated = True
                rows = ws.iter_rows(
                    max_row=_XLSX_MAX_ROWS,
                    max_col=min(declared_cols or _XLSX_MAX_COLS, _XLSX_MAX_COLS),
                    values_only=True,
                )
                if not declared_cols:
                    # No dimension: rows were padded to the cap, keep each one's own width.
                    rows = (_trim_row(r) for r in rows)
                first = next(rows, None)
                if first is None:
                    continue

                lines = [f"## {sheet_name}", ""]
                headers = [_cell_text(c) for c in first]
                lines.append("| " + " | ".join(headers) + " |")
                lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
                budget.take(sum(len(line) + 1 for line in lines))
                for row in rows:
                    if budget.spent:
                        break
                    line = "| " + " | ".join(_cell_text(c) for c in row) + " |"
                    budget.take(len(line) + 1)
                    lines.append(line)
                sections.append("\n".join(lines) + "\n")
            if len(wb.sheetnames) > _XLSX_MAX_SHEETS:
                budget.truncated = True

        finally:
            wb.close()
        return path.stem, "\n\n".join(sections) + budget.note()

    def _convert_pdf(self, path: Path) -> tuple[str, str]:
        """Extract the text layer of a PDF with pypdf (pandoc cannot read PDF)."""
        pypdf = import_optional("pypdf", "media", "Reading PDF files")

        try:
            reader = pypdf.PdfReader(str(path))
            if reader.is_encrypted:
                reader.decrypt("")  # many PDFs are "encrypted" with an empty password
            budget = _Budget(path.name)
            pages = []
            for i, page in enumerate(reader.pages):
                if i >= _PDF_MAX_PAGES or budget.spent:
                    budget.truncated = True
                    break
                text = (page.extract_text() or "").strip()
                budget.take(len(text) + 2)
                pages.append(text)
            meta_title = ""
            if reader.metadata and reader.metadata.title:
                meta_title = str(reader.metadata.title).strip()
        except Exception as e:
            raise ValueError(
                f"{path.name} is not a readable PDF (corrupt, encrypted or wrong format)"
            ) from e
        body = "\n\n".join(p for p in pages if p)
        if not body.strip():
            raise ValueError(
                f"{path.name} has no extractable text (a scanned PDF needs OCR first)"
            )
        return meta_title or path.stem, body + budget.note()

    def _convert_pandoc(self, path: Path) -> tuple[str, str]:
        """Convert another document format to markdown with pandoc.

        Fails instead of storing an error message as the source body; without pandoc only
        a file that really is UTF-8 text is read, never a binary decoded with replacements.
        """
        try:
            result = subprocess.run(
                ["pandoc", "--sandbox", str(path), "-t", "markdown", "--wrap=none"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            data = path.read_bytes()
            if b"\x00" not in data:
                try:
                    return path.stem, data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            raise ValueError(
                f"{path.name}: converting {path.suffix or 'this'} files needs pandoc "
                "on PATH (https://pandoc.org/installing.html)"
            ) from None
        if result.returncode != 0:
            detail = " ".join((result.stderr or "").split())[:300]
            raise ValueError(f"pandoc could not convert {path.name}: {detail or 'no detail'}")
        return path.stem, result.stdout


# Formats with no reader here. Failing clearly beats storing a garbled body.
_UNSUPPORTED = {
    ".doc": "legacy Word .doc is not supported; save it as .docx and ingest that",
    ".xls": "legacy Excel .xls is not supported; save it as .xlsx and ingest that",
}


# Zip formats only pandoc reads. Its zip reader is not one we can bound from here (it may
# trust headers the size check does not see), so they are accepted from local files only.
_LOCAL_ONLY = {
    ".odt": "OpenDocument files are not ingested from URLs; download it and use --file",
    ".epub": "EPUB files are not ingested from URLs; download it and use --file",
}


# Bounds for documents, which may come from a URL (RemoteFileHandler, PDF links) as well
# as from a local file. Office, ODF and EPUB files are zip containers: a small download can
# declare gigabytes of XML, so the central directory is checked before any parser opens
# it. Python's zipfile (used by python-docx and openpyxl) never inflates a member past its
# declared size, so the declared sizes bound what those readers can expand.
_ZIP_MAX_MEMBERS = 10_000
_ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024  # total declared uncompressed bytes
_ZIP_MAX_RATIO = 100  # total uncompressed / total compressed, once past _ZIP_RATIO_FLOOR
_ZIP_RATIO_FLOOR = 1024 * 1024  # tiny files may compress well without being a threat

# Text extracted from any one document. Past this the body is cut with a visible note:
# a raw source is a compile input, and nothing useful is lost from an LLM prompt by it.
_MAX_BODY_CHARS = 5_000_000
_XLSX_MAX_SHEETS = 100
_XLSX_MAX_ROWS = 20_000  # per sheet, header included
_XLSX_MAX_COLS = 200
_XLSX_MAX_CELL_CHARS = 2_000
_PDF_MAX_PAGES = 2_000


def check_zip_container(path: Path) -> None:
    """Reject a zip whose declared contents are too many, too large or too compressed."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"{path.name} is not a readable document (corrupt zip)") from e
    if len(infos) > _ZIP_MAX_MEMBERS:
        raise ValueError(
            f"{path.name} has {len(infos)} parts (limit {_ZIP_MAX_MEMBERS}); not ingested"
        )
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos)
    if total > _ZIP_MAX_UNCOMPRESSED:
        raise ValueError(
            f"{path.name} would expand to {total // (1024 * 1024)} MiB "
            f"(limit {_ZIP_MAX_UNCOMPRESSED // (1024 * 1024)} MiB); not ingested"
        )
    if total > _ZIP_RATIO_FLOOR and total > _ZIP_MAX_RATIO * max(packed, 1):
        raise ValueError(
            f"{path.name} is compressed more than {_ZIP_MAX_RATIO}:1, "
            "which looks like a decompression bomb; not ingested"
        )
    # The sizes above are declared by the archive itself. Pandoc does not honour them, and
    # Python's ZipExtFile stops at the declared size (a forged CRC then passes), so measure
    # each member by inflating its raw stream to its real end, independent of any header.
    actual = 0
    try:
        with zipfile.ZipFile(path) as zf, path.open("rb") as fp:
            for info in zf.infolist():
                real = _inflated_size(fp, info, _ZIP_MAX_UNCOMPRESSED - actual)
                if real is None:
                    raise ValueError(
                        f"{path.name} expands past "
                        f"{_ZIP_MAX_UNCOMPRESSED // (1024 * 1024)} MiB; not ingested"
                    )
                if real != info.file_size:
                    raise ValueError(
                        f"{path.name} declares sizes that do not match its contents; "
                        "not ingested"
                    )
                actual += real
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error, struct.error) as e:
        raise ValueError(f"{path.name} is not a readable document (corrupt zip)") from e


_LOCAL_HEADER = struct.Struct("<4s5H3I2H")  # local file header, 30 bytes


def _inflated_size(fp, info: zipfile.ZipInfo, budget: int) -> int | None:
    """Real uncompressed size of one member, or None once it exceeds ``budget``.

    Reads the member's local header itself and inflates the deflate stream until the stream
    ends, so a member cannot understate its size to a reader that stops early.
    """
    fp.seek(info.header_offset)
    fields = _LOCAL_HEADER.unpack(fp.read(_LOCAL_HEADER.size))
    if fields[0] != b"PK\x03\x04":
        raise zipfile.BadZipFile("bad local file header")
    fp.seek(fields[-2] + fields[-1], 1)  # file name and extra field
    method = fields[3]
    if method != info.compress_type:
        # A reader that trusts the central directory would inflate what this one skips.
        raise zipfile.BadZipFile("local and central compression methods differ")
    if method == zipfile.ZIP_STORED:
        # Stored data cannot expand; it is bounded by the file itself.
        return info.compress_size if info.compress_size <= budget else None
    if method != zipfile.ZIP_DEFLATED:
        raise zipfile.BadZipFile(f"unsupported compression method {method}")
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    total = 0
    pending = b""
    while not inflater.eof:
        if not pending:
            pending = fp.read(64 * 1024)
            if not pending:
                raise zipfile.BadZipFile("truncated deflate stream")
        out = inflater.decompress(pending, 1024 * 1024)
        pending = inflater.unconsumed_tail
        total += len(out)
        if total > budget:
            return None
    return total


def _cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > _XLSX_MAX_CELL_CHARS:
        text = text[:_XLSX_MAX_CELL_CHARS] + "…"
    return text


def _trim_row(row: tuple) -> tuple:
    end = len(row)
    while end and row[end - 1] is None:
        end -= 1
    return row[:end]


class _Budget:
    """Counts extracted characters against ``_MAX_BODY_CHARS``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.used = 0
        self.truncated = False

    @property
    def spent(self) -> bool:
        """True once the limit is reached; the caller stops and the note is added."""
        if self.used >= _MAX_BODY_CHARS:
            self.truncated = True
            return True
        return False

    def take(self, n: int) -> None:
        self.used += n

    def note(self) -> str:
        if not self.truncated:
            return ""
        logger.warning(f"{self.name}: conversion truncated at the document size limit")
        return "\n\n_(Truncated: the document exceeds the ingest size limit.)_\n"


def _cap_body(body: str, name: str) -> str:
    """Final bound for every converter (pandoc output included)."""
    if len(body) <= _MAX_BODY_CHARS + 200:  # room for a converter's own truncation note
        return body
    logger.warning(f"{name}: converted text truncated at {_MAX_BODY_CHARS} characters")
    return (
        body[:_MAX_BODY_CHARS]
        + "\n\n_(Truncated: the document exceeds the ingest size limit.)_\n"
    )
