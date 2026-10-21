"""yt-dlp invocations: the URL never reads as an option, and downloads are size-capped."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent2kb.ingest.handlers import video
from agent2kb.models import ProfileConfig


def _handler():
    profile = ProfileConfig(name="notes", bucket="b", prefix="notes/")
    return video.VideoHandler(profile=profile, s3=MagicMock(), tags=[])


def test_every_yt_dlp_call_ends_options_before_the_url_and_caps_size(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-o" in cmd:
            out = cmd[cmd.index("-o") + 1]
            open(out + ".wav", "wb").close()
        return SimpleNamespace(returncode=0, stdout="A title\n")

    url = "https://www.youtube.com/watch?v=abc"
    with patch.object(video, "validate_url"), \
         patch.object(video, "require_executable"), \
         patch.object(subprocess, "run", side_effect=fake_run), \
         patch.object(video.VideoHandler, "_transcribe", return_value="[00:00] hi"):
        result = _handler().process(url)

    assert result.success, result.error
    assert len(calls) == 2
    for cmd in calls:
        assert cmd[0] == "yt-dlp"
        assert cmd[-2:] == ["--", url]
    download = next(c for c in calls if "-x" in c)
    assert download[download.index("--max-filesize") + 1] == video.YTDLP_MAX_FILESIZE
