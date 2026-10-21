"""Audio ingest handler: transcribe audio files."""

from __future__ import annotations

import datetime
from pathlib import Path

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.extras import import_optional
from agent2kb.ingest.base import BaseHandler, build_frontmatter_content
from agent2kb.ingest.handlers.video import _format_time
from agent2kb.models import ContentType, IngestResult
from agent2kb.utils import content_hash, make_raw_key, slugify


class AudioHandler(BaseHandler):
    """Handle audio file ingestion: transcribe and summarize."""

    def process(self, source: str, **kwargs) -> IngestResult:
        file_path = kwargs.get("file_path")

        try:
            if file_path:
                path = Path(file_path)
            else:
                # If source is a URL, we'd need to download first
                return IngestResult(
                    source=source,
                    content_type=ContentType.AUDIO,
                    raw_key="",
                    title="",
                    success=False,
                    error="Audio URL download not yet supported. Use --file for local audio.",
                )

            title = path.stem
            transcript = self._transcribe(path)

            body = f"# {title}\n\n## Transcript\n\n{transcript}"

            # Hash suffix so two same-day ingests with one title don't overwrite.
            slug = slugify(title)
            date_str = datetime.datetime.now().strftime("%Y%m%d")
            short_hash = content_hash(body)[:8]
            filename = f"{date_str}-{slug}-{short_hash}.md"
            raw_key = make_raw_key(self.profile.prefix, filename)

            full_content = build_frontmatter_content(
                title=title,
                body=body,
                source=source,
                source_type=ContentType.AUDIO,
                tags=self.tags,
                output_language=self.profile.output_language,
            )

            self.s3.upload(raw_key, full_content)
            return IngestResult(
                source=source, content_type=ContentType.AUDIO, raw_key=raw_key, title=title
            )

        except Exception as e:
            logger.error(f"Audio ingest failed for {source}: {e}")
            return IngestResult(
                source=source,
                content_type=ContentType.AUDIO,
                raw_key="",
                title="",
                success=False,
                error=describe_error(e, self.profile),
            )

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
