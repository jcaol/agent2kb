"""ETag-based skipping in the embedding refresh.

Refreshing embeddings used to download every wiki page on every compile to
hash it, which on a few thousand pages costs minutes of serial round trips
while typically finding nothing changed. The index now remembers the ETag
each vector was built from, so an unchanged page is recognised from the
listing alone.
"""

from __future__ import annotations

import pytest

from agent2kb.embeddings import EmbeddingIndex, EmbeddingItem


@pytest.fixture
def index(monkeypatch):
    """An index with no provider wired up — these tests never embed."""
    idx = EmbeddingIndex.__new__(EmbeddingIndex)
    idx.items = {}
    idx._dirty = False
    idx._loaded = True
    return idx


class TestIsCurrent:
    def test_matching_etag_is_current(self, index):
        index.items["k"] = EmbeddingItem("k", "h", [0.1], source_etag="abc")
        assert index.is_current("k", "abc") is True

    def test_changed_etag_is_not_current(self, index):
        index.items["k"] = EmbeddingItem("k", "h", [0.1], source_etag="abc")
        assert index.is_current("k", "def") is False

    def test_unknown_key_is_not_current(self, index):
        assert index.is_current("missing", "abc") is False

    def test_entry_without_etag_is_not_current(self, index):
        """Vectors written before ETag tracking must be verified by hash
        once rather than trusted blindly."""
        index.items["k"] = EmbeddingItem("k", "h", [0.1])
        assert index.is_current("k", "abc") is False

    def test_empty_etag_never_matches(self, index):
        """A provider that returns no ETag must not make everything look
        unchanged."""
        index.items["k"] = EmbeddingItem("k", "h", [0.1], source_etag="")
        assert index.is_current("k", "") is False


class TestRecordEtag:
    def test_stamps_legacy_entry_and_marks_dirty(self, index):
        index.items["k"] = EmbeddingItem("k", "h", [0.1])
        index.record_etag("k", "abc")

        assert index.items["k"].source_etag == "abc"
        assert index.items["k"].vector == [0.1]  # vector untouched
        assert index._dirty is True

    def test_noop_when_etag_unchanged(self, index):
        index.items["k"] = EmbeddingItem("k", "h", [0.1], source_etag="abc")
        index.record_etag("k", "abc")
        assert index._dirty is False

    def test_noop_for_unknown_key(self, index):
        index.record_etag("missing", "abc")
        assert index.items == {}
        assert index._dirty is False
