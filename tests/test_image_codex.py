"""Tests for the codex-CLI image OCR path (0.15.0+).

Pins the provider-chain semantics:
  compile_provider=cli + cli_command=codex → codex exec -i first
  compile_provider=cli + cli_command=claude → claude -p first
  else + allow_cloud_vision → Gemini Flash → claude CLI fallback
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from agent2kb.ingest.handlers.image import ImageHandler
from agent2kb.models import LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client


@pytest.fixture
def codex_profile(tmp_path):
    return ProfileConfig(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
        compile_provider=LLMProvider.CLI,
        cli_command="codex",
        default_model="gpt-5.6-luna",
    )


@pytest.fixture
def cloud_profile():
    return ProfileConfig(
        name="test",
        s3_endpoint="http://localhost:5000",
        access_key="testing",
        secret_key="testing",
        bucket="test-bucket",
        prefix="test/",
        compile_provider=LLMProvider.GEMINI,
        gemini_api_key="g-test",
        allow_cloud_vision=True,
    )


@pytest.fixture
def mock_s3(codex_profile):
    with mock_aws():
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        client = S3Client(codex_profile)
        client._client = conn
        yield client


@pytest.fixture
def tiny_image(tmp_path) -> Path:
    """Write a 1×1 PNG so the handler has a real file to read."""
    # 1×1 transparent PNG, hex-decoded
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000005000156c2640f0000000049454e44ae426082"
    )
    p = tmp_path / "tiny.png"
    p.write_bytes(png_bytes)
    return p


class TestCodexImagePath:
    def test_codex_used_when_profile_says_codex(
        self, codex_profile, mock_s3, tiny_image, monkeypatch
    ):
        from agent2kb.cli_runner import CliResult

        h = ImageHandler(profile=codex_profile, s3=mock_s3, tags=[])
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None
        )
        seen = {}

        def fake_codex(target, system, user, *, timeout, image):
            seen.update(target=target, image=image)
            return CliResult("# Extracted text\nbody here\n> Visual: a 1×1 png", target)

        with patch("agent2kb.cli_runner.run_codex", side_effect=fake_codex):
            result = h.process("img-source", file_path=str(tiny_image))

        assert result.success
        assert seen["target"].command == "codex"
        # The image is staged into its own directory, not read in place.
        assert seen["image"] != str(tiny_image)
        assert seen["image"].endswith(".png")
        from agent2kb.models import Frontmatter
        raw = mock_s3.download(result.raw_key)
        fm, body = Frontmatter.from_yaml_block(raw)
        assert "Extracted text" in body
        assert "Visual" in body

    def test_claude_used_when_profile_says_claude(
        self, codex_profile, mock_s3, tiny_image, monkeypatch
    ):
        """claude OCR gets Read access to a staged copy's directory only."""
        from agent2kb.cli_runner import CliResult

        codex_profile.cli_command = "claude"
        codex_profile.default_model = "claude-haiku-5-5"
        h = ImageHandler(profile=codex_profile, s3=mock_s3, tags=[])
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None
        )
        seen = {}

        def fake_claude(target, system, user, *, timeout, read_dir):
            seen.update(target=target, read_dir=read_dir, user=user)
            return CliResult("claude saw text", target)

        with patch("agent2kb.cli_runner.run_claude", side_effect=fake_claude):
            result = h.process("src", file_path=str(tiny_image))

        assert seen["target"].model == "claude-haiku-5-5"
        assert seen["read_dir"] != str(tiny_image.parent)
        assert seen["read_dir"] in seen["user"]
        from agent2kb.models import Frontmatter
        _, body = Frontmatter.from_yaml_block(mock_s3.download(result.raw_key))
        assert "claude saw text" in body

    def test_codex_skipped_when_not_installed_falls_to_cloud(
        self, codex_profile, mock_s3, tiny_image, monkeypatch
    ):
        # Codex profile says codex but shutil.which returns None
        codex_profile.allow_cloud_vision = True
        codex_profile.gemini_api_key = "g-test"  # so cloud fallback can fire
        h = ImageHandler(profile=codex_profile, s3=mock_s3, tags=[])

        monkeypatch.setattr("shutil.which", lambda cmd: None)
        with patch.object(h, "_extract_with_gemini", return_value="from gemini"):
            result = h.process("src", file_path=str(tiny_image))

        from agent2kb.models import Frontmatter
        raw = mock_s3.download(result.raw_key)
        fm, body = Frontmatter.from_yaml_block(raw)
        assert "from gemini" in body

    def test_cloud_path_unchanged_when_profile_is_cloud(
        self, cloud_profile, tiny_image, monkeypatch
    ):
        """A profile that doesn't use cli compile shouldn't trigger codex."""
        with mock_aws():
            conn = boto3.client("s3", region_name="us-east-1")
            conn.create_bucket(Bucket="test-bucket")
            s3 = S3Client(cloud_profile)
            s3._client = conn

            h = ImageHandler(profile=cloud_profile, s3=s3, tags=[])
            with patch.object(h, "_extract_with_gemini", return_value="gemini text") as g, \
                 patch.object(h, "_extract_with_codex") as cod:
                h.process("src", file_path=str(tiny_image))

            assert g.called
            assert not cod.called, "codex path must NOT fire for non-cli profile"

    def test_privacy_mode_blocks_the_profile_cli_too(
        self, codex_profile, mock_s3, tiny_image, monkeypatch
    ):
        """privacy_mode=True means no cloud vision at all. The CLI is the user's own
        login, but codex / claude still send the image to their vendor (F03)."""
        codex_profile.privacy_mode = True
        codex_profile.allow_cloud_vision = False
        h = ImageHandler(profile=codex_profile, s3=mock_s3, tags=[])
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None
        )
        with patch("agent2kb.cli_runner.run_codex") as run_codex:
            result = h.process("src", file_path=str(tiny_image))

        assert result.success
        assert not run_codex.called
        from agent2kb.models import Frontmatter
        _, body = Frontmatter.from_yaml_block(mock_s3.download(result.raw_key))
        assert "Image OCR disabled by privacy settings" in body


# compile provider x (allow_cloud_vision, privacy_mode): OCR may leave the machine only
# when allow_cloud_vision is on and privacy_mode is off, whatever the provider (F03).
_PROVIDERS = [
    ("cli-codex", LLMProvider.CLI, "codex"),
    ("cli-claude", LLMProvider.CLI, "claude"),
    ("api-gemini", LLMProvider.GEMINI, "claude"),
    ("api-groq", LLMProvider.GROQ, "claude"),
    ("local-ollama", LLMProvider.OLLAMA, "claude"),
]
_FLAGS = [(True, False), (True, True), (False, False), (False, True)]


@pytest.mark.parametrize("label, provider, cli", _PROVIDERS, ids=[p[0] for p in _PROVIDERS])
@pytest.mark.parametrize(
    "allow_vision, privacy", _FLAGS, ids=[f"vision={a}-privacy={b}" for a, b in _FLAGS]
)
def test_ocr_privacy_matrix(
    codex_profile, mock_s3, tiny_image, monkeypatch, label, provider, cli, allow_vision, privacy
):
    from agent2kb.cli_runner import CliResult

    codex_profile.compile_provider = provider
    codex_profile.cli_command = cli
    codex_profile.gemini_api_key = "g-test"
    codex_profile.allow_cloud_vision = allow_vision
    codex_profile.privacy_mode = privacy
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    calls = []

    def fake_codex(target, *a, **k):
        calls.append("codex")
        return CliResult("", target)  # empty: let the chain continue

    def fake_claude(target, *a, **k):
        calls.append("claude")
        return CliResult("", target)

    h = ImageHandler(profile=codex_profile, s3=mock_s3, tags=[])

    def fake_gemini(*a, **k):
        calls.append("gemini")
        return ""

    with patch("agent2kb.cli_runner.run_codex", side_effect=fake_codex), \
         patch("agent2kb.cli_runner.run_claude", side_effect=fake_claude), \
         patch.object(h, "_extract_with_gemini", side_effect=fake_gemini):
        result = h.process("src", file_path=str(tiny_image))

    assert result.success
    if allow_vision and not privacy:
        assert calls, "cloud OCR is allowed and should be attempted"
        if provider is LLMProvider.CLI:
            assert calls[0] == cli, "the profile CLI goes first"
    else:
        assert calls == [], f"no cloud OCR route may run, got {calls}"


class TestGeminiVisionUsesTheCurrentSdk:
    def test_calls_google_genai_client_with_an_image_part(self, cloud_profile):
        import base64
        from types import SimpleNamespace

        seen = {}

        class FakeClient:
            def __init__(self, api_key):
                seen["api_key"] = api_key
                self.models = SimpleNamespace(generate_content=self.generate_content)

            def generate_content(self, model, contents):
                seen["model"], seen["contents"] = model, contents
                return SimpleNamespace(text="  text in the picture \n")

        fake_genai = SimpleNamespace(Client=FakeClient)
        fake_types = SimpleNamespace(
            Part=SimpleNamespace(
                from_bytes=lambda data, mime_type: ("part", data, mime_type)
            )
        )
        modules = {"google.genai": fake_genai, "google.genai.types": fake_types}

        def fake_import(module, extra, feature):
            assert extra == "gemini"
            return modules[module]

        h = ImageHandler(profile=cloud_profile, s3=None, tags=[])
        with patch("agent2kb.ingest.handlers.image.import_optional", fake_import):
            b64 = base64.b64encode(b"PNGDATA").decode()
            out = h._extract_with_gemini(b64, "image/png", "a.png")
        assert out == "text in the picture"
        assert seen["api_key"] == "g-test" and seen["model"] == "gemini-2.5-flash"
        assert seen["contents"][1] == ("part", b"PNGDATA", "image/png")
