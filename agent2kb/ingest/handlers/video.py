"""Video ingest handler: download + transcribe + summarize."""

from __future__ import annotations

import datetime
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.extras import import_optional, require_executable
from agent2kb.ingest.base import BaseHandler, build_frontmatter_content
from agent2kb.models import ContentType, IngestResult
from agent2kb.utils import content_hash, make_raw_key, slugify, validate_url

# yt-dlp fetches on its own (DNS, redirects), outside agent2kb.net_guard; validate_url
# only checks the first host. Cap what it may download (yt-dlp's --max-filesize, applied
# when the size is known up front) on top of the 600 s timeout.
YTDLP_MAX_FILESIZE = "1G"


class VideoHandler(BaseHandler):
    """Handle video ingestion: yt-dlp download, whisper transcription."""

    def process(self, source: str, **kwargs) -> IngestResult:
        file_path = kwargs.get("file_path")

        try:
            if file_path:
                return self._process_local_video(source, Path(file_path))
            else:
                return self._process_video_url(source)
        except Exception as e:
            logger.error(f"Video ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=ContentType.VIDEO,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )

    def _process_video_url(self, url: str) -> IngestResult:
        """Download video from URL using yt-dlp, then transcribe."""
        validate_url(url)
        require_executable("yt-dlp", "media", "Ingesting video URLs")
        with tempfile.TemporaryDirectory() as tmpdir:
            # Get video info first
            title = self._get_video_title(url)

            # Download audio only (more efficient for transcription)
            audio_path = Path(tmpdir) / "audio.wav"
            cmd = [
                "yt-dlp",
                "-x",
                "--audio-format",
                "wav",
                "-o",
                str(audio_path.with_suffix("")),
                "--no-playlist",
                "--max-filesize",
                YTDLP_MAX_FILESIZE,
                "--",  # Prevent URL from being interpreted as flags
                url,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)

            # Find the actual output file (yt-dlp may add extension)
            audio_files = list(Path(tmpdir).glob("audio*"))
            if not audio_files:
                raise FileNotFoundError("yt-dlp did not produce audio output")
            audio_file = audio_files[0]

            # Transcribe
            transcript = self._transcribe(audio_file)

            # Build output
            body = f"# {title}\n\n## Transcript\n\n{transcript}"
            slug = slugify(title)
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            short_hash = content_hash(body)[:8]
            filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = make_raw_key(self.profile.prefix, filename)

            full_content = build_frontmatter_content(
                title=title,
                body=body,
                source=url,
                source_type=ContentType.VIDEO,
                tags=self.tags,
                original_link=url,
                output_language=self.profile.output_language,
            )

            self.s3.upload(raw_key, full_content)
            return IngestResult(
                source=url, content_type=ContentType.VIDEO, raw_key=raw_key, title=title
            )

    def _process_local_video(self, source: str, file_path: Path) -> IngestResult:
        """Process a local video file."""
        title = file_path.stem
        transcript = self._transcribe(file_path)

        body = f"# {title}\n\n## Transcript\n\n{transcript}"
        slug = slugify(title)
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        short_hash = content_hash(body)[:8]
        filename = f"{date_str}-{slug}-{short_hash}.md"
        raw_key = make_raw_key(self.profile.prefix, filename)

        full_content = build_frontmatter_content(
            title=title,
            body=body,
            source=source,
            source_type=ContentType.VIDEO,
            tags=self.tags,
            output_language=self.profile.output_language,
        )

        self.s3.upload(raw_key, full_content)
        return IngestResult(
            source=source, content_type=ContentType.VIDEO, raw_key=raw_key, title=title
        )

    def _get_video_title(self, url: str) -> str:
        """Get video title using yt-dlp."""
        try:
            result = subprocess.run(
                ["yt-dlp", "--get-title", "--no-playlist", "--", url],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return url.split("/")[-1]

    def _transcribe(self, audio_path: Path) -> str:
        """Transcribe audio using faster-whisper."""
        WhisperModel = import_optional(
            "faster_whisper", "whisper", "Transcribing audio and video"
        ).WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio_path), beam_size=5)

        lines = []
        for segment in segments:
            timestamp = f"[{_format_time(segment.start)} → {_format_time(segment.end)}]"
            lines.append(f"{timestamp} {segment.text.strip()}")
        return "\n".join(lines)


def _format_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
