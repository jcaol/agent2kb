"""The "Past N Days" recap at the top of index.md follows recent_days."""

import datetime

import pytest

from agent2kb.compile.compiler import Compiler
from agent2kb.models import ProfileConfig


class _S3:
    def __init__(self, index=""):
        self.index = index

    def download(self, key):
        if not self.index:
            raise FileNotFoundError(key)
        return self.index


def _compiler(days, index="", summary="Themes of the period."):
    c = Compiler.__new__(Compiler)
    c.profile = ProfileConfig(name="notes", s3_endpoint="http://e", access_key="a",
                              secret_key="s", bucket="b", prefix="kbs/notes/",
                              recent_days=days)
    c.s3 = _S3(index)
    c.calls = []

    def fake_summary(articles, label):
        c.calls.append(label)
        return summary

    c._generate_weekly_summary = fake_summary
    return c


def _entries(*ages):
    today = datetime.date.today()
    return [((today - datetime.timedelta(days=a)).isoformat(), f"page-{a}", f"Page {a}", "", "")
            for a in ages]


def test_default_keeps_the_seven_day_section_and_marker():
    c = _compiler(7)
    lines = c._build_recent_digest(_entries(1, 3, 20), "kbs/notes/wiki/index.md")
    assert lines[0] == "## Past 7 Days (2 articles)"
    assert "<!-- digest-count:2 -->" in lines
    assert c.calls == ["past 7 days"]


def test_longer_window_covers_more_pages_and_records_itself():
    c = _compiler(30)
    lines = c._build_recent_digest(_entries(1, 3, 20, 45), "kbs/notes/wiki/index.md")
    assert lines[0] == "## Past 30 Days (3 articles)"
    assert "<!-- digest-count:3:30 -->" in lines
    assert c.calls == ["past 30 days"]


def test_zero_turns_the_section_and_its_llm_call_off():
    c = _compiler(0)
    assert c._build_recent_digest(_entries(1, 2), "kbs/notes/wiki/index.md") == []
    assert c.calls == []


def test_cached_summary_is_reused_only_for_the_same_window():
    old = (
        "# Index\n\n## Past 7 Days (2 articles)\n\n> Cached themes.\n\n"
        "- x\n<!-- digest-count:2 -->\n"
    )
    same = _compiler(7, index=old)
    assert "> Cached themes." in same._build_recent_digest(_entries(1, 2), "k")
    assert same.calls == []
    changed = _compiler(14, index=old)
    changed._build_recent_digest(_entries(1, 2), "k")
    assert changed.calls == ["past 14 days"]


@pytest.mark.parametrize("bad", [-1, 367])
def test_out_of_range_values_are_refused(bad):
    with pytest.raises(ValueError):
        ProfileConfig(name="x", s3_endpoint="http://e", access_key="a", secret_key="s",
                      bucket="b", prefix="x/", recent_days=bad)


def test_recent_days_comes_from_config_and_env(tmp_path, monkeypatch):
    import yaml

    from agent2kb.config import resolve_profile

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "version": 2,
        "storage": {"endpoint": "http://e", "bucket": "b", "access_key": "a", "secret_key": "s"},
        "defaults": {"recent_days": 14},
        "kbs": {"notes": {}, "monthly": {"recent_days": 30}},
    }))
    assert resolve_profile("notes", cfg).recent_days == 14
    assert resolve_profile("monthly", cfg).recent_days == 30
    monkeypatch.setenv("KB_RECENT_DAYS", "0")
    assert resolve_profile("notes", cfg).recent_days == 0
