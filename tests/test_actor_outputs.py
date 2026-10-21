"""Tests for the 2026-05-14 actor / dedup / outputs management work.

Pins three contracts:
  P1. ``a2kb query --actor`` writes to the correct subdirectory and the
      actor lands on frontmatter ``source_meta``.
  P3. Agent-actor saves dedup against same-hit-set captures from the
      last hour by merging into the prior file rather than allocating
      a new one.
  P2. ``a2kb outputs ls / prune`` filters by actor + age correctly and
      never deletes user captures unless explicitly asked.
"""

from __future__ import annotations

import datetime

import boto3
import pytest
from moto import mock_aws

from agent2kb import outputs as _o
from agent2kb.models import Frontmatter, ProfileConfig, SourceKind
from agent2kb.query import QueryEngine, SearchHit
from agent2kb.s3_client import S3Client


@pytest.fixture
def profile():
    return ProfileConfig(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
    )


@pytest.fixture
def mock_s3(profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(profile)
        client._client = conn
        yield client


def _seed_hits(engine: QueryEngine, keys: list[str]) -> None:
    """Stub last_hits so save_output picks up a deterministic hit-set."""
    engine.last_hits = [
        SearchHit(key=k, title=k.split("/")[-1], score=1.0, snippet="", body="")
        for k in keys
    ]


class TestActorRouting:
    def test_user_save_lands_at_outputs_root(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        _seed_hits(engine, ["test/wiki/a.md"])
        key = engine.save_output("what is A", "the answer", actor="user")
        assert key.startswith("test/outputs/")
        assert "/outputs/agent/" not in key

    def test_agent_save_lands_in_agent_subfolder(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        _seed_hits(engine, ["test/wiki/b.md"])
        key = engine.save_output("what is B", "ans", actor="agent")
        assert key.startswith("test/outputs/agent/")

    def test_actor_recorded_in_source_meta(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        _seed_hits(engine, ["test/wiki/a.md"])
        key = engine.save_output("Q", "A", actor="user")
        fm, _ = Frontmatter.from_yaml_block(mock_s3.download(key))
        assert fm.source_meta.get("actor") == "user"
        assert fm.source_kind == SourceKind.QUERY_OUTPUT

    def test_invalid_actor_raises(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        _seed_hits(engine, ["test/wiki/a.md"])
        with pytest.raises(ValueError):
            engine.save_output("Q", "A", actor="banana")


class TestAgentDedup:
    """Same hit-set, agent actor, ≤1h → merged into prior file."""

    def test_dedup_merges_same_hit_set(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        hits = ["test/wiki/foo.md", "test/wiki/bar.md"]
        _seed_hits(engine, hits)
        first = engine.save_output("what is foo", "ans1", actor="agent")

        # Second agent query against the SAME hit set should merge
        _seed_hits(engine, hits)
        second = engine.save_output("features of foo", "ans2", actor="agent")
        assert first == second, "Same hit-set agent saves must coalesce"

        merged = mock_s3.download(first)
        fm, body = Frontmatter.from_yaml_block(merged)
        assert (fm.source_meta or {}).get("also_asked") == ["features of foo"]
        assert "ans1" in body
        assert "ans2" in body
        assert "Follow-up" in body

    def test_dedup_skipped_for_different_hit_set(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        _seed_hits(engine, ["test/wiki/foo.md"])
        first = engine.save_output("what is foo", "ans1", actor="agent")

        _seed_hits(engine, ["test/wiki/bar.md"])  # different hit-set
        second = engine.save_output("what is bar", "ans2", actor="agent")
        assert first != second, "Different hit-sets must not merge"

    def test_dedup_does_not_apply_to_user_saves(self, profile, mock_s3):
        engine = QueryEngine(profile=profile, s3=mock_s3)
        hits = ["test/wiki/foo.md"]

        _seed_hits(engine, hits)
        first = engine.save_output("foo q1", "a1", actor="user")
        _seed_hits(engine, hits)
        second = engine.save_output("foo q2", "a2", actor="user")
        assert first != second, "User actor must always allocate a new file"

    def test_dedup_skipped_when_hit_set_empty(self, profile, mock_s3):
        """No hits → no signature → never merge (safety)."""
        engine = QueryEngine(profile=profile, s3=mock_s3)
        engine.last_hits = []  # explicit
        first = engine.save_output("q1", "a1", actor="agent")
        engine.last_hits = []
        second = engine.save_output("q2", "a2", actor="agent")
        assert first != second


class TestOutputsListing:
    def _seed(self, mock_s3, key: str, *, actor: str = "user") -> None:
        fm = Frontmatter(
            title=f"Q: {key.split('/')[-1]}",
            source="kb query",
            source_kind=SourceKind.QUERY_OUTPUT,
            source_meta={"actor": actor},
        )
        mock_s3.upload(key, fm.to_yaml_block() + "\nbody\n")

    def test_ls_includes_both_user_and_agent_paths(self, profile, mock_s3):
        self._seed(mock_s3, "test/outputs/20260512-100000-a.md", actor="user")
        self._seed(
            mock_s3, "test/outputs/agent/20260513-100000-b.md", actor="agent"
        )
        entries = _o.list_entries(mock_s3)
        actors = {e.actor for e in entries}
        assert actors == {"user", "agent"}

    def test_ls_filter_by_actor_agent(self, profile, mock_s3):
        self._seed(mock_s3, "test/outputs/20260512-100000-a.md")
        self._seed(mock_s3, "test/outputs/agent/20260513-100000-b.md", actor="agent")
        entries = _o.list_entries(mock_s3, actor="agent")
        assert len(entries) == 1
        assert entries[0].actor == "agent"

    def test_ls_filter_by_since(self, profile, mock_s3):
        # Old file: well outside any reasonable --since window
        self._seed(mock_s3, "test/outputs/20200101-100000-old.md")
        # New file: dated today
        today_stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._seed(mock_s3, f"test/outputs/{today_stamp}-fresh.md")

        recent = _o.list_entries(mock_s3, since=datetime.timedelta(days=1))
        keys = {e.key.split("/")[-1] for e in recent}
        assert any("fresh" in k for k in keys)
        assert not any("old" in k for k in keys)

    def test_actor_classification_from_path(self):
        assert _o._actor_for_key("p/outputs/x.md") == "user"
        assert _o._actor_for_key("p/outputs/agent/x.md") == "agent"


class TestOutputsPrune:
    def _seed(self, mock_s3, key: str) -> None:
        fm = Frontmatter(
            title="Q", source="kb query", source_kind=SourceKind.QUERY_OUTPUT
        )
        mock_s3.upload(key, fm.to_yaml_block() + "\nbody\n")

    def test_prune_dry_run_keeps_files(self, profile, mock_s3):
        # 2020 file is older than any realistic window
        self._seed(mock_s3, "test/outputs/agent/20200101-100000-old.md")
        doomed = _o.prune(
            mock_s3, profile, older_than=datetime.timedelta(days=30), apply=False
        )
        assert len(doomed) == 1
        # File still exists
        assert mock_s3.exists("test/outputs/agent/20200101-100000-old.md")

    def test_prune_apply_deletes_only_doomed(self, profile, mock_s3):
        self._seed(mock_s3, "test/outputs/agent/20200101-100000-old.md")
        today_stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._seed(mock_s3, f"test/outputs/agent/{today_stamp}-fresh.md")

        doomed = _o.prune(
            mock_s3,
            profile,
            older_than=datetime.timedelta(days=30),
            actor="agent",
            apply=True,
        )
        assert len(doomed) == 1
        # Old gone, fresh stays
        assert not mock_s3.exists(
            "test/outputs/agent/20200101-100000-old.md"
        )
        assert mock_s3.exists(f"test/outputs/agent/{today_stamp}-fresh.md")

    def test_prune_actor_filter_protects_user(self, profile, mock_s3):
        """Pruning --actor agent must NOT touch user captures."""
        self._seed(mock_s3, "test/outputs/20200101-100000-old-user.md")
        self._seed(mock_s3, "test/outputs/agent/20200101-100000-old-agent.md")

        doomed = _o.prune(
            mock_s3,
            profile,
            older_than=datetime.timedelta(days=30),
            actor="agent",
            apply=True,
        )
        assert len(doomed) == 1
        assert doomed[0].actor == "agent"
        assert mock_s3.exists("test/outputs/20200101-100000-old-user.md")


class TestDurationParsing:
    @pytest.mark.parametrize(
        "text,expected_seconds",
        [
            ("60s", 60),
            ("5m", 5 * 60),
            ("2h", 2 * 3600),
            ("7d", 7 * 86400),
            ("2w", 2 * 7 * 86400),
        ],
    )
    def test_valid_durations(self, text, expected_seconds):
        assert _o.parse_duration(text).total_seconds() == expected_seconds

    @pytest.mark.parametrize("text", ["", "abc", "10", "10y", "1.5d"])
    def test_invalid_durations_raise(self, text):
        with pytest.raises(ValueError):
            _o.parse_duration(text)
