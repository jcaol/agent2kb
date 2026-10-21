"""Ingest-side hardening: entity-extraction privacy, fetch User-Agent, surfaced fetch errors."""

from unittest.mock import patch

import httpx
import pytest
from loguru import logger

from agent2kb import PROJECT_URL, __version__
from agent2kb import net_guard as ng
from agent2kb.ingest.base import IngestPipeline
from agent2kb.ingest.handlers import url as url_handler
from agent2kb.models import LLMProvider, ProfileConfig


def _profile(**kw) -> ProfileConfig:
    base = dict(name="p", bucket="b", prefix="projects/p/", region="us-east-1")
    base.update(kw)
    return ProfileConfig(**base)


class _S3:
    def __init__(self):
        self.store = {"projects/p/raw/a.md": "---\ntitle: A\n---\n\nsecret body\n"}

    def download(self, key):
        return self.store[key]

    def upload(self, key, body, content_type="text/markdown"):
        self.store[key] = body


class TestEntityExtractionPrivacy:
    def _run(self, profile):
        s3 = _S3()
        with patch.object(IngestPipeline, "_llm_entities", return_value=["X"]) as llm:
            IngestPipeline(profile=profile, s3=s3)._extract_entities("projects/p/raw/a.md")
        return llm, s3

    def test_privacy_mode_with_cloud_provider_sends_nothing(self):
        llm, s3 = self._run(_profile(privacy_mode=True, compile_provider=LLMProvider.GROQ))
        llm.assert_not_called()
        assert "entities" not in s3.store["projects/p/raw/a.md"]

    @pytest.mark.parametrize("provider", [LLMProvider.GEMINI, LLMProvider.CLI])
    def test_privacy_mode_blocks_every_non_local_provider(self, provider):
        llm, _ = self._run(_profile(privacy_mode=True, compile_provider=provider))
        llm.assert_not_called()

    def test_privacy_mode_with_ollama_still_extracts(self):
        llm, s3 = self._run(_profile(privacy_mode=True, compile_provider=LLMProvider.OLLAMA))
        llm.assert_called_once()
        assert "X" in s3.store["projects/p/raw/a.md"]

    def test_without_privacy_mode_nothing_changes(self):
        llm, _ = self._run(_profile(privacy_mode=False, compile_provider=LLMProvider.GROQ))
        llm.assert_called_once()

    def test_skip_logs_one_line(self):

        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
        try:
            self._run(_profile(privacy_mode=True, compile_provider=LLMProvider.GROQ))
        finally:
            logger.remove(sink)
        assert sum("skipping ingest-time entity extraction" in x for x in lines) == 1


class TestEntityExtractionFailureIsVisible:
    def _profile_with_key(self):
        return _profile(compile_provider=LLMProvider.GROQ, groq_api_key="gsk_SECRETVALUE123")

    def _lines(self, profile, boom):
        s3 = _S3()
        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            with patch.object(IngestPipeline, "_llm_entities", side_effect=boom):
                IngestPipeline(profile=profile, s3=s3)._extract_entities("projects/p/raw/a.md")
        finally:
            logger.remove(sink)
        return lines, s3

    def test_failure_is_a_warning_with_a_short_reason(self):
        lines, s3 = self._lines(self._profile_with_key(), RuntimeError("rate limited"))
        assert len(lines) == 1
        assert "a.md" in lines[0] and "RuntimeError: rate limited" in lines[0]
        assert "entities" not in s3.store["projects/p/raw/a.md"]

    def test_the_reason_never_carries_credentials_and_is_short(self):
        boom = RuntimeError("401 for key gsk_SECRETVALUE123 " + "x" * 500)
        lines, _ = self._lines(self._profile_with_key(), boom)
        assert "gsk_SECRETVALUE123" not in lines[0] and "***" in lines[0]
        assert len(lines[0]) < 300

    def test_llm_call_failure_is_a_warning_too(self):
        from agent2kb.compile.compiler import Compiler

        lines = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            with patch.object(Compiler, "_call_llm_with_fallback", side_effect=ValueError("bad")):
                out = IngestPipeline(profile=self._profile_with_key(), s3=_S3())._llm_entities(
                    "t", "body"
                )
        finally:
            logger.remove(sink)
        assert out == [] and any("ValueError: bad" in x for x in lines)


class TestUserAgent:
    def test_constant_is_descriptive_and_uses_the_single_url_constant(self):
        assert ng.USER_AGENT == ng.build_user_agent(__version__, PROJECT_URL)
        assert ng.USER_AGENT.startswith(f"Mozilla/5.0 (compatible; agent2kb/{__version__}")

    def test_placeholder_project_url_is_never_sent(self):
        placeholder = "https://github.com/OWNER/agent2kb"
        assert "OWNER" not in ng.build_user_agent("1.0", placeholder)
        assert ng.build_user_agent("1.0", placeholder) == "Mozilla/5.0 (compatible; agent2kb/1.0)"
        real = ng.build_user_agent("1.0", "https://example.org/x/agent2kb")
        assert real.endswith("; +https://example.org/x/agent2kb)")
        assert "OWNER" not in ng.USER_AGENT

    @pytest.mark.parametrize("literal", [False, True])
    def test_sent_on_pinned_and_literal_requests(self, monkeypatch, literal):
        host = "93.184.216.34" if literal else "site.example"
        monkeypatch.setattr(ng, "resolve_public", lambda h, p: ["93.184.216.34"])
        seen = []

        def handler(request):
            seen.append(request.headers["user-agent"])
            return httpx.Response(200, text="x")

        ng.safe_get(f"https://{host}/", transport=httpx.MockTransport(handler))
        assert seen == [ng.USER_AGENT]


class _FakeS3:
    def __init__(self):
        self.uploads = {}

    def upload(self, key, body, content_type="text/markdown"):
        self.uploads[key] = body


class TestFetchErrorSurfaced:
    @pytest.fixture
    def handler(self, monkeypatch):
        monkeypatch.setattr(url_handler, "_validate_url", lambda u: None)
        return url_handler.URLHandler(
            profile=_profile(allow_jina_reader=False), s3=_FakeS3(), tags=[]
        )

    def _forbidden(self, url):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "403", request=request, response=httpx.Response(403, request=request)
        )

    def test_real_reason_replaces_document_is_empty(self, handler, monkeypatch):
        monkeypatch.setattr(url_handler, "_safe_get", self._forbidden)
        result = handler.process("https://site.example/a")
        assert not result.success
        assert "HTTP 403" in result.error and "Could not fetch the page" in result.error

    def test_reason_added_to_too_short_message(self, handler, monkeypatch):
        monkeypatch.setattr(url_handler, "_safe_get", self._forbidden)
        monkeypatch.setattr(url_handler, "_extract_content", lambda *a, **k: ("t", "tiny"))
        result = handler.process("https://site.example/a")
        assert "too short" in result.error and "HTTP 403" in result.error

    def test_timeout_reason(self, handler, monkeypatch):
        def slow(url):
            raise httpx.ReadTimeout("timed out")

        monkeypatch.setattr(url_handler, "_safe_get", slow)
        assert "timed out" in handler.process("https://site.example/a").error

    def test_success_path_has_no_noise(self, handler, monkeypatch):
        resp = httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        monkeypatch.setattr(url_handler, "_safe_get", lambda u: resp)
        monkeypatch.setattr(url_handler, "_extract_content", lambda *a, **k: ("T", "body " * 40))
        assert handler.process("https://site.example/a").success
