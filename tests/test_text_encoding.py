"""Text ingest decodes UTF-16/UTF-32 files with a byte-order mark; binaries still fail."""

from __future__ import annotations

import codecs

import pytest

from agent2kb.ingest.handlers.markdown import MarkdownHandler
from agent2kb.models import Frontmatter, ProfileConfig
from agent2kb.utils import decode_text_file, detect_file_type

TEXT = "# Alpha notes\n\nCafé, naïve, 中文 and emoji \U0001f600.\n"


class _S3:
    def __init__(self):
        self.store: dict[str, str] = {}

    def upload(self, key, body, content_type="text/markdown", **_kw):
        self.store[key] = body


def _ingest(path):
    profile = ProfileConfig(name="p", bucket="b", prefix="projects/p/", region="us-east-1")
    s3 = _S3()
    result = MarkdownHandler(profile=profile, s3=s3, tags=[]).process(
        source=str(path), file_path=path
    )
    return result, s3


ENCODED = [
    ("utf-16-le", codecs.BOM_UTF16_LE + TEXT.encode("utf-16-le")),
    ("utf-16-be", codecs.BOM_UTF16_BE + TEXT.encode("utf-16-be")),
    ("utf-16", TEXT.encode("utf-16")),  # platform byte order, BOM included
    ("utf-32-le", codecs.BOM_UTF32_LE + TEXT.encode("utf-32-le")),
    ("utf-32-be", codecs.BOM_UTF32_BE + TEXT.encode("utf-32-be")),
    ("utf-8-sig", TEXT.encode("utf-8-sig")),
]


@pytest.mark.parametrize("name,data", ENCODED, ids=[e[0] for e in ENCODED])
def test_decode_text_file_handles_boms(name, data):
    assert decode_text_file(data) == TEXT


@pytest.mark.parametrize("name,data", ENCODED, ids=[e[0] for e in ENCODED])
def test_utf16_and_utf32_text_files_are_ingested(tmp_path, name, data):
    f = tmp_path / "notes.txt"
    f.write_bytes(data)
    assert detect_file_type(f) != "document"
    result, s3 = _ingest(f)
    assert result.success, result.error
    (body,) = s3.store.values()
    fm, text = Frontmatter.from_yaml_block(body)
    assert fm.title == "Alpha notes"
    assert "Café, naïve, 中文 and emoji \U0001f600." in text
    assert "\x00" not in body and "﻿" not in body


def test_utf16_frontmatter_is_kept(tmp_path):
    f = tmp_path / "clip.md"
    f.write_bytes(("---\ntitle: Alpha clip\ntags: [x]\n---\n\nBody\n").encode("utf-16"))
    result, s3 = _ingest(f)
    assert result.success, result.error
    fm, _ = Frontmatter.from_yaml_block(next(iter(s3.store.values())))
    assert fm.title == "Alpha clip"


@pytest.mark.parametrize("data", [
    b"\x7fELF\x00\x00\x01binary",                         # no BOM, NUL bytes
    b"\xff\xfe" + b"\x00\xd8" * 64,                        # UTF-16 LE BOM, lone surrogates
    b"\xfe\xff" + b"\xdc\x00" * 64,                        # UTF-16 BE BOM, lone surrogates
    b"\xff\xfe\x00\x00" + b"\xff\xff\xff\x7f" * 16,        # UTF-32 BOM, beyond U+10FFFF
    codecs.BOM_UTF16_LE + "text\x00more".encode("utf-16-le"),  # decodes to a NUL
])
def test_binaries_are_still_rejected(tmp_path, data):
    assert decode_text_file(data) is None
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    result, s3 = _ingest(f)
    assert not result.success and "binary" in result.error
    assert s3.store == {}


def test_plain_utf8_unchanged():
    assert decode_text_file(TEXT.encode()) == TEXT
    assert decode_text_file(b"bad \xff byte") == "bad � byte"
