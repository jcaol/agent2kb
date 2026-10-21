"""Config examples in the docs must parse and mean what the text says."""

import re
from pathlib import Path

import yaml

from agent2kb import config_v2

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _yaml_block_after(doc: Path, heading: str) -> dict:
    text = doc.read_text()
    section = text[text.index(heading):]
    block = re.search(r"```yaml\n(.*?)```", section, re.S)
    assert block, f"no yaml block after {heading!r} in {doc.name}"
    return yaml.safe_load(block.group(1))


def test_two_kb_r2_example_gives_each_kb_its_own_bucket_and_key(tmp_path):
    raw = _yaml_block_after(
        DOCS / "configuration.md", "## Several KBs with separate buckets (R2)"
    )
    cfg = config_v2.parse_v2(raw, tmp_path / "config.yaml")
    notes = config_v2.build_profile(cfg, "notes")
    work = config_v2.build_profile(cfg, "work")
    assert (notes.bucket, work.bucket) == ("kb-notes", "kb-work")
    assert notes.access_key != work.access_key
    assert notes.prefix == "kbs/notes/" and work.prefix == "kbs/work/"
    assert notes.s3_endpoint == work.s3_endpoint
    assert notes.region == work.region == "auto"
    assert config_v2.find_prefix_conflict(cfg, "work", work.prefix, work.bucket) is None
