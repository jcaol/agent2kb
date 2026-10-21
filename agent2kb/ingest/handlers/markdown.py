"""Markdown/text ingest handler: pass through with frontmatter."""

from __future__ import annotations

import datetime
from pathlib import Path

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.ingest.base import BaseHandler, build_frontmatter_content
from agent2kb.models import ContentType, Frontmatter, IngestResult
from agent2kb.utils import (
    content_hash,
    decode_text_file,
    is_remote_url,
    make_raw_key,
    slugify,
)


class MarkdownHandler(BaseHandler):
    """Handle markdown and plain text file ingestion."""

    def process(self, source: str, **kwargs) -> IngestResult:
        file_path = kwargs.get("file_path")

        try:
            if file_path:
                path = Path(file_path)
                # Unknown extensions land here as text; a binary file must fail rather
                # than become a source full of replacement characters. UTF-16/32 text
                # with a byte-order mark is decoded, not mistaken for binary.
                content = decode_text_file(path.read_bytes())
                if content is None:
                    raise ValueError(
                        f"{path.name} looks like a binary file of an unsupported type"
                    )
                title = path.stem
            else:
                # source is inline text or URL to raw text
                content = source
                title = source[:60]

            # The fetching handler says so explicitly (remote=True); a source that merely
            # looks like a URL, in any letter case, is treated as untrusted too.
            remote = bool(kwargs.get("remote")) or is_remote_url(source)
            source_type = (
                ContentType.MARKDOWN
                if (file_path and Path(file_path).suffix.lower() in (".md", ".markdown"))
                else ContentType.TEXT
            )

            if content.startswith("---") and not remote:
                # A local file the user chose: keep its descriptive metadata (title, date,
                # tags, original_link from a web clipper, entities, source_meta) but never
                # the pipeline's own state, which only ingest and compile may set.
                fm, body = Frontmatter.from_yaml_block(content)
                if fm.title:
                    title = fm.title
                fm.tags = list(set(fm.tags + self.tags))
                fm.source = source
                _reset_system_fields(fm)
                full_content = fm.to_yaml_block() + "\n" + body
            else:
                body = content
                if content.startswith("---"):
                    # A downloaded file is untrusted: its frontmatter could claim another
                    # original_link (the dedup key), a source_kind, entities or compile
                    # state. Only the title is taken, as URL ingest takes a page <title>;
                    # everything else is rebuilt as for any other fetched page.
                    fm, body = Frontmatter.from_yaml_block(content)
                    title = _clean_title(fm.title) or title
                else:
                    # Extract title from first heading if available
                    for line in content.split("\n"):
                        line = line.strip()
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break

                full_content = build_frontmatter_content(
                    title=title,
                    body=body,
                    source=source,
                    source_type=source_type,
                    tags=self.tags,
                    original_link=source if remote else "",
                    output_language=self.profile.output_language,
                )

            slug = slugify(title)
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            short_hash = content_hash(content)[:8]
            filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = make_raw_key(self.profile.prefix, filename)

            self.s3.upload(raw_key, full_content)
            return IngestResult(
                source=source,
                content_type=ContentType.MARKDOWN,
                raw_key=raw_key,
                title=title,
            )

        except Exception as e:
            logger.error(f"Markdown ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=ContentType.MARKDOWN,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )


# Longest title kept from a downloaded file's frontmatter; a page <title> is similar.
_MAX_TITLE_CHARS = 200


def _clean_title(value: str) -> str:
    """A frontmatter title from an untrusted file: one line, bounded length."""
    return " ".join(str(value or "").split())[:_MAX_TITLE_CHARS]


def _reset_system_fields(fm: Frontmatter) -> None:
    """Clear fields only the pipeline writes, so a source file cannot pre-set them.

    ``compiled``/``compile_*``/``raw_content_hash`` describe compile state, and
    ``source_kind`` is the trust class (a raw file is always a primary source, inferred
    from its key when unset).
    """
    fm.compiled = False
    fm.compile_provider = None
    fm.compile_model = None
    fm.compile_date = None
    fm.raw_content_hash = None
    fm.source_kind = None
