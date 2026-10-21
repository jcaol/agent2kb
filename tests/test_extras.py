"""Optional dependencies: a missing package must name the extra to install."""

import sys

import pytest

from agent2kb.compile.compiler import Compiler
from agent2kb.embeddings import EmbeddingClient
from agent2kb.extras import MissingExtraError, import_optional, require_executable
from agent2kb.models import LLMProvider, ProfileConfig


def _profile(**kw) -> ProfileConfig:
    base = dict(
        name="t", s3_endpoint="http://localhost:1", access_key="a", secret_key="b",
        bucket="test-bucket", prefix="t/",
    )
    base.update(kw)
    return ProfileConfig(**base)


def test_import_optional_returns_installed_module():
    assert import_optional("json", "x", "Testing") is sys.modules["json"]


def test_import_optional_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "somepkg", None)
    with pytest.raises(MissingExtraError) as exc:
        import_optional("somepkg", "myextra", "The Foo feature")
    msg = str(exc.value)
    assert "The Foo feature" in msg and "'somepkg'" in msg
    assert 'pip install "agent2kb[myextra]"' in msg
    assert "\n" not in msg  # the compile fallback chain keeps only the first line


def test_missing_extra_is_import_and_runtime_error():
    err = MissingExtraError("m", "e", "P")
    assert isinstance(err, ImportError) and isinstance(err, RuntimeError)


def test_require_executable_missing(monkeypatch):
    monkeypatch.setattr("agent2kb.extras.shutil.which", lambda name: None)
    with pytest.raises(MissingExtraError, match=r"agent2kb\[media\]"):
        require_executable("yt-dlp", "media", "Ingesting video URLs")


@pytest.mark.parametrize("provider, module, extra, model_kw", [
    (LLMProvider.GROQ, "groq", "groq", dict(groq_api_key="k")),
    (LLMProvider.OPENROUTER, "openai", "openai", dict(openrouter_api_key="k")),
    (LLMProvider.OLLAMA, "ollama", "ollama", {}),
    (LLMProvider.GEMINI, "google.genai", "gemini", dict(gemini_api_key="k")),
])
def test_compile_provider_without_sdk_names_extra(monkeypatch, provider, module, extra, model_kw):
    monkeypatch.setitem(sys.modules, module, None)
    if module == "google.genai":
        monkeypatch.setitem(sys.modules, "google.genai.types", None)
    compiler = Compiler(profile=_profile(compile_provider=provider, **model_kw), s3=None)
    with pytest.raises(MissingExtraError, match=rf"agent2kb\[{extra}\]"):
        compiler._call_provider(provider, [{"role": "user", "content": "hi"}], "")


def test_embedding_providers_without_sdk_name_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    client = EmbeddingClient(_profile(embedding_provider="openai", openai_api_key="k"))
    with pytest.raises(MissingExtraError, match=r"agent2kb\[openai\]"):
        client.embed("text")


def test_docx_without_python_docx_names_media_extra(monkeypatch, tmp_path):
    from agent2kb.ingest.handlers.document import DocumentHandler

    monkeypatch.setitem(sys.modules, "docx", None)
    handler = DocumentHandler(profile=_profile(), s3=None, tags=[])
    with pytest.raises(MissingExtraError, match=r"agent2kb\[media\]"):
        handler._convert_docx(tmp_path / "a.docx")


@pytest.mark.parametrize("handler_mod, cls", [("audio", "AudioHandler"), ("video", "VideoHandler")])
def test_transcribe_without_whisper_names_extra(monkeypatch, tmp_path, handler_mod, cls):
    import importlib

    mod = importlib.import_module(f"agent2kb.ingest.handlers.{handler_mod}")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    handler = getattr(mod, cls)(profile=_profile(), s3=None, tags=[])
    with pytest.raises(MissingExtraError, match=r"agent2kb\[whisper\]"):
        handler._transcribe(tmp_path / "a.wav")


# ─── through the real CLI: the hint must survive rich markup ──────────


def _config_file(tmp_path, **profile):
    import yaml

    base = {
        "s3_endpoint": "http://localhost:8333", "access_key": "a", "secret_key": "b",
        "bucket": "test-bucket", "prefix": "test/", "compile_provider": "groq",
        "groq_api_key": "gsk-test",
    }
    base.update(profile)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"test": base}}))
    return path


def _flat(output: str) -> str:
    return " ".join(output.split())


def test_ingest_hint_keeps_brackets_in_console(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from agent2kb.main import app

    monkeypatch.setitem(sys.modules, "docx", None)
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"PK")
    result = CliRunner().invoke(
        app, ["-p", "test", "--config", str(_config_file(tmp_path)), "ingest", "--file", str(doc)]
    )
    assert 'pip install "agent2kb[media]"' in _flat(result.output)


def test_corrupt_docx_gives_friendly_error(tmp_path):
    from agent2kb.ingest.handlers.document import DocumentHandler

    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    handler = DocumentHandler(profile=_profile(), s3=None, tags=[])
    result = handler.process(source=str(bad), file_path=str(bad))
    assert not result.success
    assert "not a readable .docx" in result.error
    assert "Package not found" not in result.error


@pytest.fixture
def raw_in_s3():
    import boto3
    from moto import mock_aws

    from agent2kb.models import Frontmatter
    from agent2kb.s3_client import S3Client

    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        s3 = S3Client(_profile(prefix="test/"))
        s3._client = conn
        for name in ("one", "two", "three"):
            fm = Frontmatter(title=name, source="http://example.com", source_type="url")
            s3.upload(f"test/raw/{name}.md", fm.to_yaml_block() + "\nbody text")
        yield s3


def test_compile_stops_at_once_when_primary_extra_is_missing(monkeypatch, tmp_path, raw_in_s3):
    from unittest.mock import patch

    from typer.testing import CliRunner

    from agent2kb.main import app

    monkeypatch.setitem(sys.modules, "groq", None)
    calls = []
    real = Compiler._call_provider

    def spy(self, provider, *a, **kw):
        calls.append(provider)
        return real(self, provider, *a, **kw)

    monkeypatch.setattr(Compiler, "_call_provider", spy)
    with patch("agent2kb.main.get_s3", return_value=raw_in_s3):
        result = CliRunner().invoke(
            app, ["-p", "test", "--config", str(_config_file(tmp_path)), "compile"]
        )
    out = _flat(result.output)
    assert result.exit_code == 1
    assert 'pip install "agent2kb[groq]"' in out
    assert "Fallback chain" not in out and "failed" not in out
    assert calls == [LLMProvider.GROQ]  # one attempt, no per-file retries, no fallbacks


def test_fallback_with_missing_extra_is_skipped(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "ollama", None)  # fallback member, extra missing
    monkeypatch.setitem(sys.modules, "groq", None)
    compiler = Compiler(
        profile=_profile(compile_provider=LLMProvider.OPENROUTER, openrouter_api_key="k"), s3=None
    )
    seen = []

    def fake(provider, messages, model, purpose="compile"):
        seen.append(provider)
        if provider is LLMProvider.OPENROUTER:
            raise RuntimeError("upstream 500")
        return Compiler._call_provider(compiler, provider, messages, model, purpose)

    monkeypatch.setattr(compiler, "_call_provider", fake)
    with pytest.raises(RuntimeError) as exc:
        compiler._call_llm_with_fallback([{"role": "user", "content": "hi"}])
    assert "upstream 500" in str(exc.value)
    assert "agent2kb[" not in str(exc.value)  # skipped fallbacks do not clutter the chain
