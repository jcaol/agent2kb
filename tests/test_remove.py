"""Tests for `a2kb rm` core logic (agent2kb.remove).

Uses an in-memory fake S3 (no moto dependency) — the remove module was
written as a pure-logic layer specifically so it can be exercised this way.
"""

from __future__ import annotations

import datetime
import json

import pytest

from agent2kb import manifest as _manifest
from agent2kb import remove as rm
from agent2kb.embeddings import _INDEX_KEY_SUFFIX, OLLAMA_EMBED_MODEL
from agent2kb.models import ContentType, Frontmatter, ProfileConfig

PREFIX = "personal/"
EMB_KEY = f"{PREFIX}{_INDEX_KEY_SUFFIX}"


# ─────────────────────────── fakes ──────────────────────────────────


class FakeS3:
    """Minimal in-memory stand-in for S3Client (no prefix enforcement)."""

    def __init__(self, profile: ProfileConfig):
        self.profile = profile
        self.store: dict[str, str] = {}
        self.meta: dict[str, datetime.datetime] = {}

    # -- write/read -------------------------------------------------
    def upload(self, key, body, content_type="text/markdown", *, if_match=None,
               if_none_match=False):
        self.store[key] = body if isinstance(body, str) else body.decode("utf-8")
        return ""

    def download(self, key):
        if key not in self.store:
            raise FileNotFoundError(key)
        return self.store[key]

    def download_with_etag(self, key):
        return self.download(key), ""

    def delete(self, key):
        self.store.pop(key, None)
        self.meta.pop(key, None)

    def exists(self, key):
        return key in self.store

    # -- listing ----------------------------------------------------
    def list_keys(self, sub_prefix="", suffix=""):
        full = self.profile.prefix + sub_prefix
        return sorted(
            k for k in self.store
            if k.startswith(full) and (not suffix or k.endswith(suffix))
        )

    def list_raw_files(self):
        return [k for k in self.list_keys("raw/", ".md") if "/raw/.failed/" not in k]

    def list_wiki_files(self):
        return self.list_keys("wiki/", ".md")

    def list_output_files(self):
        return self.list_keys("outputs/", ".md")

    def get_metadata(self, key):
        return {"last_modified": self.meta.get(key), "size": 0, "content_type": ""}


def _profile() -> ProfileConfig:
    return ProfileConfig(
        name="personal",
        s3_endpoint="http://localhost:8333",
        access_key="x",
        secret_key="y",
        bucket="kb",
        prefix=PREFIX,
    )


def _article(s3, base, *, raw=True, wiki=True, original_link="",
             tags=None, mtime=None):
    """Seed a raw/ + wiki/ article pair into the fake store."""
    fm = Frontmatter(
        title=base.replace(".md", "").replace("-", " "),
        source=original_link,
        source_type=ContentType.URL,
        original_link=original_link,
        tags=tags or [],
    )
    content = fm.to_yaml_block() + "\nbody of " + base
    if raw:
        k = f"{PREFIX}raw/{base}"
        s3.store[k] = content
        if mtime:
            s3.meta[k] = mtime
    if wiki:
        k = f"{PREFIX}wiki/{base}"
        s3.store[k] = content
        if mtime:
            s3.meta[k] = mtime


def _seed_embeddings(s3, wiki_keys):
    # Mirror EmbeddingIndex.save() on-disk shape exactly. With no API keys
    # configured, EmbeddingClient resolves to the local ollama model, so
    # the index ``model`` must match or load() discards it as stale.
    idx = {
        "version": 1,
        "model": OLLAMA_EMBED_MODEL,
        "provider": "ollama",
        "items": [
            {"key": wk, "hash": "h", "vec": [0.1, 0.2]} for wk in wiki_keys
        ],
    }
    s3.store[EMB_KEY] = json.dumps(idx)


def _emb_keys(s3) -> set[str]:
    """Read the embedding index back and return the set of stored keys."""
    data = json.loads(s3.store[EMB_KEY])
    return {it["key"] for it in data.get("items", [])}


def _seed_manifest(s3, wiki_keys):
    m = {
        "version": _manifest.MANIFEST_VERSION,
        "generated_at": "2026-05-31T00:00:00",
        "prefix": PREFIX,
        "entries": {wk: {"title": "t", "kind": "article"} for wk in wiki_keys},
    }
    s3.store[_manifest.manifest_key_for(s3.profile)] = json.dumps(m)


@pytest.fixture
def s3():
    p = _profile()
    client = FakeS3(p)
    _article(client, "20260531-foo-aaaa1111.md", original_link="https://x.com/foo")
    _article(client, "20260501-bar-bbbb2222.md", wiki=False, original_link="https://x.com/bar")
    _article(client, "concepts/baz.md", raw=False)  # concept page, no raw
    # protected structural pages
    client.store[f"{PREFIX}wiki/index.md"] = "# index"
    client.store[f"{PREFIX}wiki/log.md"] = "# log"
    _seed_embeddings(client, [
        f"{PREFIX}wiki/20260531-foo-aaaa1111.md",
        f"{PREFIX}wiki/concepts/baz.md",
    ])
    _seed_manifest(client, [
        f"{PREFIX}wiki/20260531-foo-aaaa1111.md",
        f"{PREFIX}wiki/concepts/baz.md",
    ])
    return client


# ─────────────────────────── collect ────────────────────────────────


class TestCollect:
    def test_pairs_raw_and_wiki_by_basename(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        assert foo.raw_key == f"{PREFIX}raw/20260531-foo-aaaa1111.md"
        assert foo.wiki_key == f"{PREFIX}wiki/20260531-foo-aaaa1111.md"

    def test_raw_only_article(self, s3):
        matches = rm.collect(s3, s3.profile)
        bar = matches["20260501-bar-bbbb2222.md"]
        assert bar.raw_key is not None
        assert bar.wiki_key is None

    def test_wiki_only_concept(self, s3):
        matches = rm.collect(s3, s3.profile)
        baz = matches["concepts/baz.md"]
        assert baz.raw_key is None
        assert baz.wiki_key == f"{PREFIX}wiki/concepts/baz.md"

    def test_protected_pages_excluded(self, s3):
        matches = rm.collect(s3, s3.profile)
        assert "index.md" not in matches
        assert "log.md" not in matches


# ─────────────────────────── selection ──────────────────────────────


class TestSelect:
    def test_by_url(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, url="https://x.com/foo")
        assert [m.basename for m in hits] == ["20260531-foo-aaaa1111.md"]

    def test_by_url_raw_only(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, url="https://x.com/bar")
        assert [m.basename for m in hits] == ["20260501-bar-bbbb2222.md"]

    def test_by_url_no_match(self, s3):
        matches = rm.collect(s3, s3.profile)
        assert rm.select(s3, s3.profile, matches, url="https://nope") == []

    def test_by_id_hash_suffix(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, id="aaaa1111")
        assert [m.basename for m in hits] == ["20260531-foo-aaaa1111.md"]

    def test_by_file_exact(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, file="20260531-foo-aaaa1111.md")
        assert len(hits) == 1

    def test_by_file_without_extension(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, file="20260531-foo-aaaa1111")
        assert len(hits) == 1

    def test_by_slug_substring(self, s3):
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, slug="foo")
        assert [m.basename for m in hits] == ["20260531-foo-aaaa1111.md"]

    def test_slug_does_not_sweep_concepts(self, s3):
        # A concept page must only match by exact --file, never a loose slug.
        matches = rm.collect(s3, s3.profile)
        hits = rm.select(s3, s3.profile, matches, slug="baz")
        assert hits == []
        hits2 = rm.select(s3, s3.profile, matches, file="concepts/baz.md")
        assert len(hits2) == 1


# ─────────────────────────── scope + plan ───────────────────────────


class TestScope:
    def test_resolve(self):
        assert rm.resolve_scope(raw=False, wiki=False) == rm.SCOPE_ALL
        assert rm.resolve_scope(raw=True, wiki=False) == rm.SCOPE_RAW
        assert rm.resolve_scope(raw=False, wiki=True) == rm.SCOPE_WIKI
        assert rm.resolve_scope(raw=True, wiki=True) == rm.SCOPE_ALL

    def test_build_deletions_all(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_ALL)
        layers = sorted(d.layer for d in dels)
        assert layers == ["embedding", "manifest", "raw", "wiki"]

    def test_build_deletions_raw_only(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_RAW)
        assert [d.layer for d in dels] == ["raw"]

    def test_build_deletions_wiki_only(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_WIKI)
        assert sorted(d.layer for d in dels) == ["embedding", "manifest", "wiki"]

    def test_orphan_raw_warning(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        warns = rm.orphaned_raw_warnings([foo], rm.SCOPE_RAW)
        assert len(warns) == 1 and "no raw source" in warns[0]


# ─────────────────────────── execution ──────────────────────────────


class TestApply:
    def test_full_chain_removed(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_ALL)
        report = rm.apply_deletions(s3, s3.profile, dels)

        assert report.objects_deleted == 2  # raw + wiki
        assert report.embeddings_removed == 1
        assert report.manifest_removed == 1
        # objects gone
        assert f"{PREFIX}raw/20260531-foo-aaaa1111.md" not in s3.store
        assert f"{PREFIX}wiki/20260531-foo-aaaa1111.md" not in s3.store
        # embedding entry gone, sibling untouched
        emb = _emb_keys(s3)
        assert f"{PREFIX}wiki/20260531-foo-aaaa1111.md" not in emb
        assert f"{PREFIX}wiki/concepts/baz.md" in emb
        # manifest entry gone
        man = json.loads(s3.store[_manifest.manifest_key_for(s3.profile)])
        assert f"{PREFIX}wiki/20260531-foo-aaaa1111.md" not in man["entries"]

    def test_raw_scope_keeps_derived(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_RAW)
        rm.apply_deletions(s3, s3.profile, dels)

        assert f"{PREFIX}raw/20260531-foo-aaaa1111.md" not in s3.store
        # wiki + embedding + manifest still there
        assert f"{PREFIX}wiki/20260531-foo-aaaa1111.md" in s3.store
        assert f"{PREFIX}wiki/20260531-foo-aaaa1111.md" in _emb_keys(s3)

    def test_apply_is_idempotent(self, s3):
        matches = rm.collect(s3, s3.profile)
        foo = matches["20260531-foo-aaaa1111.md"]
        dels = rm.build_deletions(s3.profile, [foo], rm.SCOPE_ALL)
        rm.apply_deletions(s3, s3.profile, dels)
        # second run deletes nothing, raises nothing
        report = rm.apply_deletions(s3, s3.profile, dels)
        assert report.objects_deleted == 0
        assert report.embeddings_removed == 0


# ─────────────────────────── bulk ───────────────────────────────────


class TestBulk:
    def test_older_than(self, s3):
        now = datetime.datetime.now(datetime.timezone.utc)
        # foo is old, bar is fresh
        s3.meta[f"{PREFIX}raw/20260531-foo-aaaa1111.md"] = now - datetime.timedelta(days=120)
        s3.meta[f"{PREFIX}wiki/20260531-foo-aaaa1111.md"] = now - datetime.timedelta(days=120)
        s3.meta[f"{PREFIX}raw/20260501-bar-bbbb2222.md"] = now - datetime.timedelta(days=5)

        matches = rm.collect(s3, s3.profile)
        selected = rm.select_bulk(
            s3, s3.profile, matches, older_than=datetime.timedelta(days=90)
        )
        names = {m.basename for m in selected}
        assert "20260531-foo-aaaa1111.md" in names
        assert "20260501-bar-bbbb2222.md" not in names

    def test_tag_filter(self, s3):
        now = datetime.datetime.now(datetime.timezone.utc)
        _article(s3, "20260101-tagged-cccc3333.md", original_link="https://x.com/t",
                 tags=["scratch"], mtime=now - datetime.timedelta(days=200))
        # give everything an old mtime so only the tag distinguishes
        for k in list(s3.store):
            if k.endswith(".md"):
                s3.meta[k] = now - datetime.timedelta(days=200)

        matches = rm.collect(s3, s3.profile)
        selected = rm.select_bulk(
            s3, s3.profile, matches,
            older_than=datetime.timedelta(days=90), tag="scratch",
        )
        assert {m.basename for m in selected} == {"20260101-tagged-cccc3333.md"}


# ─────────────────────────── orphan backlinks ───────────────────────


class TestOrphanBacklinks:
    def _match(self, s3, base):
        m = rm.collect(s3, s3.profile)[base]
        rm.enrich(s3, [m])
        return m

    def test_no_links_means_no_warnings(self, s3):
        foo = self._match(s3, "20260531-foo-aaaa1111.md")
        assert rm.find_orphaned_backlinks(s3, s3.profile, [foo]) == []

    def test_derive_link_targets_forms(self):
        m = rm.RemovalMatch(
            basename="20260531-Rahul-on-X-7c3eb3bf.md", title="Rahul on X: a thread"
        )
        got = rm.derive_link_targets(m)
        assert "20260531-Rahul-on-X-7c3eb3bf" in got  # filename stem
        assert "Rahul on X: a thread" in got  # title, verbatim
        assert "Rahul-on-X" in got and "Rahul on X" in got  # de-dated, de-hashed
        assert len(got) == len({t.lower() for t in got})

    def test_derive_link_targets_for_a_concept_page(self):
        got = rm.derive_link_targets(rm.RemovalMatch(basename="concepts/baz.md", title="Baz"))
        assert [t.lower() for t in got] == ["concepts/baz", "baz"]  # title "Baz" is the same text

    def test_short_dedated_names_are_not_used(self):
        got = rm.derive_link_targets(rm.RemovalMatch(basename="20260531-foo-aaaa1111.md"))
        assert "foo" not in got

    def test_reports_links_by_title_stem_slug_and_with_alias_or_anchor(self, s3):
        s3.store[f"{PREFIX}wiki/other.md"] = (
            "---\ntitle: Other\n---\n"
            "by stem [[20260531-foo-aaaa1111]], by title [[20260531 foo aaaa1111]], "
            "slug-ish [[20260531-FOO-aaaa1111|alias]], anchor [[20260531-foo-aaaa1111#part]]"
        )
        foo = self._match(s3, "20260531-foo-aaaa1111.md")
        warns = rm.find_orphaned_backlinks(s3, s3.profile, [foo])
        assert len(warns) == 4 and all(w.startswith("other.md still links to") for w in warns)
        assert all("removed 20260531-foo-aaaa1111.md" in w for w in warns)

    def test_concept_path_form(self, s3):
        s3.store[f"{PREFIX}wiki/other.md"] = "see [[concepts/baz]] and [[baz]]"
        baz = self._match(s3, "concepts/baz.md")
        warns = rm.find_orphaned_backlinks(s3, s3.profile, [baz])
        assert len(warns) == 2

    def test_unrelated_links_and_code_are_ignored(self, s3):
        s3.store[f"{PREFIX}wiki/other.md"] = "[[something else]] and `[[20260531-foo-aaaa1111]]`"
        foo = self._match(s3, "20260531-foo-aaaa1111.md")
        assert rm.find_orphaned_backlinks(s3, s3.profile, [foo]) == []

    def test_link_still_answered_by_a_surviving_page_is_not_orphaned(self, s3):
        # A concept page called "foo bar" survives; an article with the same title goes.
        s3.store[f"{PREFIX}wiki/concepts/foo-bar.md"] = "---\ntitle: Foo Bar\n---\nconcept"
        s3.store[f"{PREFIX}wiki/20260601-foo-bar-cccc3333.md"] = (
            "---\ntitle: Foo Bar\n---\narticle"
        )
        s3.store[f"{PREFIX}wiki/other.md"] = "see [[Foo Bar]] and [[20260601-foo-bar-cccc3333]]"
        gone = self._match(s3, "20260601-foo-bar-cccc3333.md")
        warns = rm.find_orphaned_backlinks(s3, s3.profile, [gone])
        assert len(warns) == 1 and "[[20260601-foo-bar-cccc3333]]" in warns[0]

    def test_other_removed_pages_do_not_report_each_other(self, s3):
        s3.store[f"{PREFIX}wiki/other.md"] = "[[20260531-foo-aaaa1111]]"
        foo = self._match(s3, "20260531-foo-aaaa1111.md")
        other = rm.RemovalMatch(basename="other.md", wiki_key=f"{PREFIX}wiki/other.md")
        assert rm.find_orphaned_backlinks(s3, s3.profile, [foo, other]) == []


# ─────────────────────────── manifest batch ─────────────────────────


class TestManifestBatch:
    def test_remove_entries_single_put(self, s3):
        keys = [
            f"{PREFIX}wiki/20260531-foo-aaaa1111.md",
            f"{PREFIX}wiki/concepts/baz.md",
        ]
        n = _manifest.remove_entries(s3, s3.profile, keys)
        assert n == 2
        man = json.loads(s3.store[_manifest.manifest_key_for(s3.profile)])
        assert man["entries"] == {}

    def test_remove_entries_missing_noop(self, s3):
        n = _manifest.remove_entries(s3, s3.profile, [f"{PREFIX}wiki/ghost.md"])
        assert n == 0


# ─────────────────────────── security: key ownership ────────────────


class TestKeyOwnership:
    """Manifest and embedding-index rows are data. A row naming a key
    outside this profile's raw/ or wiki/ must never reach the deletion
    plan — it used to, so `rm --file saved-answer.md` deleted a user's
    saved query output from outputs/."""

    def test_planted_manifest_row_outside_wiki_is_ignored(self, s3):
        planted = f"{PREFIX}outputs/wiki/saved-answer.md"
        s3.store[planted] = "my saved answer"
        _seed_manifest(s3, [planted])

        matches = rm.collect(s3, s3.profile)
        assert all(m.wiki_key != planted for m in matches.values())
        assert rm.select(s3, s3.profile, matches, url=None, slug=None,
                         file="saved-answer.md", id=None) == []

    def test_planted_embedding_row_outside_wiki_is_ignored(self, s3):
        planted = f"{PREFIX}raw/../outputs/x.md"
        _seed_embeddings(s3, [planted])
        assert all(m.wiki_key != planted for m in rm.collect(s3, s3.profile).values())

    def test_other_profile_keys_are_ignored(self, s3):
        foreign = "projects/other/wiki/20260531-foo-aaaa1111.md"
        _seed_manifest(s3, [foreign])
        assert all(m.wiki_key != foreign for m in rm.collect(s3, s3.profile).values())

    def test_build_deletions_refuses_keys_outside_raw_and_wiki(self, s3):
        bad = rm.RemovalMatch(basename="x.md")
        bad.raw_key = f"{PREFIX}outputs/x.md"
        bad.wiki_key = f"{PREFIX}_meta/manifest.json"
        assert rm.build_deletions(s3.profile, [bad], rm.SCOPE_ALL) == []

    def test_owned_key_rules(self, s3):
        p = s3.profile
        assert rm._owned_key(p, f"{PREFIX}wiki/a.md", "wiki/")
        assert rm._owned_key(p, f"{PREFIX}wiki/concepts/a.md", "wiki/")
        for key in (f"{PREFIX}wiki/../raw/a.md", f"{PREFIX}wiki//a.md",
                    f"{PREFIX}wiki/./a.md", f"{PREFIX}wiki/a.json",
                    f"{PREFIX}outputs/wiki/a.md", "elsewhere/wiki/a.md"):
            assert not rm._owned_key(p, key, "wiki/"), key


# ─────────────────────────── security: bulk gate (CLI) ──────────────


class TestBulkDeleteGate:
    """`rm --older-than` can empty a whole KB in one command. It ignores
    --yes and needs a person at a terminal typing the item count; off a
    terminal it is a dry run. Single-item --yes still works (reingest)."""

    def _run(self, s3, args, *, tty, typed=""):
        from unittest.mock import MagicMock, patch

        from typer.testing import CliRunner

        import agent2kb.main as M

        with patch.object(M, "resolve_profile", return_value=s3.profile), \
             patch.object(M, "load_config", return_value=MagicMock()), \
             patch.object(M, "get_s3", return_value=s3), \
             patch.object(M, "_stdin_is_terminal", return_value=tty), \
             patch("builtins.input", return_value=typed):
            return CliRunner().invoke(M.app, ["--profile", "personal", "rm", *args])

    def _age_everything(self, s3):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        for k in list(s3.store):
            if k.endswith(".md"):
                s3.meta[k] = old

    def _article_count(self, s3):
        return len([k for k in s3.store if "/raw/" in k or "/wiki/2026" in k])

    def test_bulk_off_terminal_deletes_nothing_even_with_yes(self, s3):
        self._age_everything(s3)
        before = self._article_count(s3)
        result = self._run(s3, ["--older-than", "0d", "--yes"], tty=False)
        assert result.exit_code == 2
        assert self._article_count(s3) == before

    def test_bulk_on_terminal_needs_exact_count(self, s3):
        self._age_everything(s3)
        before = self._article_count(s3)
        result = self._run(s3, ["--older-than", "0d", "--yes"], tty=True, typed="y")
        assert result.exit_code == 0
        assert self._article_count(s3) == before  # "y" is not the count

    def test_bulk_on_terminal_with_count_deletes(self, s3):
        self._age_everything(s3)
        matches = rm.collect(s3, s3.profile)
        n = len(rm.select_bulk(s3, s3.profile, matches, older_than=datetime.timedelta(0)))
        result = self._run(s3, ["--older-than", "0d"], tty=True, typed=str(n))
        assert result.exit_code == 0
        assert self._article_count(s3) == 0

    def test_single_item_yes_still_works_off_terminal(self, s3):
        key = f"{PREFIX}raw/20260531-foo-aaaa1111.md"
        result = self._run(s3, ["--file", "20260531-foo-aaaa1111.md", "--yes"], tty=False)
        assert result.exit_code == 0
        assert key not in s3.store


# ─────────────────────────── console output escaping (CLI) ──────────


class TestRmOutputIsLiteral:
    """Wiki text and original links reach the console verbatim, not as Rich markup."""

    def _run(self, s3, args):
        from unittest.mock import MagicMock, patch

        from typer.testing import CliRunner

        import agent2kb.main as M

        with patch.object(M, "resolve_profile", return_value=s3.profile), \
             patch.object(M, "load_config", return_value=MagicMock()), \
             patch.object(M, "get_s3", return_value=s3), \
             patch.object(M, "_stdin_is_terminal", return_value=False):
            return CliRunner().invoke(M.app, ["--profile", "personal", "rm", *args])

    def test_orphaned_link_warning_prints_wikilink_literally(self, s3):
        s3.store[f"{PREFIX}wiki/other.md"] = "---\ntitle: Other\n---\nsee [[concepts/baz]]"
        result = self._run(s3, ["--file", "concepts/baz.md", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "[[concepts/baz]]" in result.output
