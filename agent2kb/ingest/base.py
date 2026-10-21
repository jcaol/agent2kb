"""Base ingest pipeline with auto-detection and handler dispatch."""

from __future__ import annotations

import datetime
from pathlib import Path

from loguru import logger

from agent2kb.errors import describe_error
from agent2kb.models import ContentType, Frontmatter, IngestResult, LLMProvider, ProfileConfig
from agent2kb.s3_client import S3Client
from agent2kb.utils import detect_file_type, detect_url_type, ensure_traditional_chinese


def _interactive() -> bool:
    """True when stdin is a TTY (human at keyboard)."""
    import sys
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()



def _short_reason(exc: BaseException, profile: ProfileConfig, limit: int = 120) -> str:
    """One short line naming why something failed, with the profile's credentials masked."""
    from agent2kb.cli_runner import profile_secrets

    text = " ".join(str(exc).split())
    for secret in profile_secrets(profile):
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

class BaseHandler:
    """Base class for all ingest handlers."""

    def __init__(self, profile: ProfileConfig, s3: S3Client, tags: list[str] | None = None):
        self.profile = profile
        self.s3 = s3
        self.tags = tags or []

    def process(self, source: str, **kwargs) -> IngestResult:
        raise NotImplementedError


class IngestPipeline:
    """Main ingest coordinator with auto-detection."""

    def __init__(self, profile: ProfileConfig, s3: S3Client, tags: list[str] | None = None):
        self.profile = profile
        self.s3 = s3
        self.tags = tags or []

    def _get_handler(self, content_type: str) -> BaseHandler:
        """Get the appropriate handler for a content type."""
        from agent2kb.ingest.handlers.audio import AudioHandler
        from agent2kb.ingest.handlers.document import DocumentHandler
        from agent2kb.ingest.handlers.image import ImageHandler
        from agent2kb.ingest.handlers.markdown import MarkdownHandler
        from agent2kb.ingest.handlers.url import URLHandler
        from agent2kb.ingest.handlers.video import VideoHandler

        handler_map = {
            "url": URLHandler,
            "video": VideoHandler,
            "audio": AudioHandler,
            "document": DocumentHandler,
            "image": ImageHandler,
            "markdown": MarkdownHandler,
            "text": MarkdownHandler,
            "pdf_url": URLHandler,
        }
        handler_cls = handler_map.get(content_type, MarkdownHandler)
        return handler_cls(profile=self.profile, s3=self.s3, tags=self.tags)

    def ingest_url(self, url: str, *, actor: str = "user",
                   skip_safety: bool = False) -> IngestResult:
        """Ingest content from a URL.

        After successful ingest, runs entity extraction to populate the
        raw file's frontmatter with named entities (people, products,
        concepts). Compile uses this to avoid duplicate concept pages
        and to seed wikilinks deterministically.
        """
        content_type = detect_url_type(url)
        logger.info(f"Detected URL type: {content_type} for {url}")

        if not skip_safety:
            safety = self._run_safety_check(url)
            if safety and safety.is_flagged:
                if actor != "user" or not _interactive():
                    logger.warning(f"Safety-flagged URL skipped (actor={actor}): {url}")
                    return IngestResult(
                        source=url,
                        content_type=ContentType(content_type) if content_type else ContentType.URL,
                        raw_key="",
                        title="(safety-flagged, skipped)",
                        success=False,
                        error=f"Safety check flagged: {'; '.join(safety.reasons)}",
                    )
                if not self._confirm_flagged_url(safety):
                    return IngestResult(
                        source=url,
                        content_type=ContentType(content_type) if content_type else ContentType.URL,
                        raw_key="",
                        title="(safety-flagged, user declined)",
                        success=False,
                        error="User declined after safety warning",
                    )

        # Pre-ingest URL dedup: if any existing raw already records this URL
        # in its `original_link` we skip re-ingesting. Saves bandwidth and
        # avoids near-duplicate raws when the page body changes slightly.
        existing = self._find_existing_by_url(url)
        if existing:
            logger.info(f"URL already ingested → {existing}, skipping")
            return IngestResult(
                source=url,
                content_type=ContentType(content_type) if content_type else ContentType.URL,
                raw_key=existing,
                title="(already ingested)",
                success=True,
            )
        from agent2kb.ingest.handlers.url import RemoteFileHandler

        if content_type in RemoteFileHandler.KINDS:
            # .md / .txt / .docx / .xlsx URLs: download first, then convert as a local file.
            handler = RemoteFileHandler(profile=self.profile, s3=self.s3, tags=self.tags)
        else:
            handler = self._get_handler(content_type)
        result = handler.process(source=url)
        if result.success and result.raw_key:
            self._extract_entities(result.raw_key)
        return result

    def ingest_file(self, file_path: Path) -> IngestResult:
        """Ingest a local file."""
        content_type = detect_file_type(file_path)
        logger.info(f"Detected file type: {content_type} for {file_path}")
        handler = self._get_handler(content_type)
        result = handler.process(source=str(file_path), file_path=file_path)
        if result.success and result.raw_key:
            self._extract_entities(result.raw_key)
        return result

    def _run_safety_check(self, url: str):
        """Run pre-ingest safety check. Returns SafetyResult or None."""
        try:
            from agent2kb.ingest.safety import check_url
            return check_url(
                url,
                google_sb_key=self.profile.google_safe_browsing_key,
                privacy_mode=self.profile.privacy_mode,
            )
        except Exception as e:
            logger.debug(f"Safety check failed (allowing ingest): {e}")
            return None

    def _confirm_flagged_url(self, safety) -> bool:
        """Interactive prompt for human user to override a flagged URL."""
        from rich.console import Console

        from agent2kb.ingest.safety import format_warning
        console = Console()
        console.print(format_warning(safety))
        try:
            answer = input("Proceed anyway? [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _find_existing_by_url(self, url: str) -> str | None:
        """Return the raw key whose source is this URL, if any.

        URLs are compared after normalisation (tracking parameters, host
        aliases, scheme and trailing slash removed), so re-sharing a page
        with ``?fbclid=`` or as ``twitter.com`` no longer re-ingests it.
        Frontmatters are fetched in parallel.
        """
        from agent2kb.dedup import RawIndex

        try:
            return RawIndex(self.s3).by_url(url)
        except Exception as e:
            logger.debug(f"URL dedup check failed (allowing ingest): {e}")
            return None

    def _extract_entities(self, raw_key: str) -> None:
        """LLM-extract named entities and write them into the raw frontmatter.

        Best-effort: any error (no LLM key, JSON parse fail, network)
        leaves the raw file unchanged. The compile pipeline degrades to
        the prior LLM-only wikilink discovery when ``entities`` is empty.

        Cost is one short LLM call per ingest (~200 input + ~50 output
        tokens). At Gemini Flash free tier this is effectively free; on
        paid CLIs it's ~$0.001/ingest. Kept off the hot ``compile`` path
        deliberately so the cost is paid once at ingest, not every refresh.

        Privacy: under ``privacy_mode`` the excerpt (up to 4000 chars of the source)
        must not go to a cloud provider, so extraction only runs with a local one.
        """
        if self.profile.privacy_mode and self.profile.compile_provider is not LLMProvider.OLLAMA:
            logger.info(
                "privacy_mode is on and the compile provider is not local: "
                "skipping ingest-time entity extraction (content is not sent to the LLM)."
            )
            return
        try:
            content = self.s3.download(raw_key)
            fm, body = Frontmatter.from_yaml_block(content)
            if fm.entities:
                # Already extracted (e.g. re-ingest). Don't redo work.
                return
            entities = self._llm_entities(fm.title, body)
            if not entities:
                return
            fm.entities = entities
            full = fm.to_yaml_block() + "\n" + body
            self.s3.upload(raw_key, full)
            logger.info(f"Entities for {raw_key.split('/')[-1]}: {entities[:5]}...")
        except Exception as e:
            logger.warning(
                f"Entity extraction skipped for {raw_key.split('/')[-1]}: "
                f"{_short_reason(e, self.profile)}"
            )

    def _llm_entities(self, title: str, body: str) -> list[str]:
        """Call the active compile provider for a single entity-extraction turn.

        Prompt insists on JSON-only output to make parsing robust. The
        provider's existing fallback chain handles transient failures.
        """
        import json as _json

        from agent2kb.compile.compiler import Compiler

        # Cap context — entity extraction works fine on the lead 4k chars,
        # and longer payloads waste tokens for no marginal recall.
        excerpt = body[:4000]
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract named entities from a knowledge-base source "
                    "for later wikilinking. Return a JSON array of 3-15 strings "
                    "with the canonical name of each entity (people, companies, "
                    "products, technical concepts, frameworks, libraries). "
                    "Examples: [\"PostgreSQL\", \"Kubernetes\", \"OpenAI\"]. "
                    "Use the exact form a user would search for. Do NOT include "
                    "generic words (e.g. \"AI\", \"agent\", \"open source\"). "
                    "Output ONLY the JSON array, no prose, no markdown fence."
                ),
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\nContent:\n{excerpt}",
            },
        ]
        try:
            compiler = Compiler(profile=self.profile, s3=self.s3)
            raw = compiler._call_llm_with_fallback(messages).strip()
            # Strip code fence if model added one despite the instruction.
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = _json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()][:15]
        except Exception as e:
            logger.warning(f"Entity extraction call failed: {_short_reason(e, self.profile)}")
        return []

    def ingest_folder(
        self,
        folder: Path,
        recursive: bool = False,
        *,
        include_hidden: bool = False,
        selection: FolderSelection | None = None,
    ) -> list[IngestResult]:
        """Ingest the files that ``select_folder_files`` picks from a folder.

        ``selection`` is a selection made (and shown to the user) earlier; exactly
        those files are ingested instead of selecting again, so a file that appears
        in the folder after the confirmation is not uploaded with it.
        """
        results = []
        if selection is None:
            selection = select_folder_files(
                folder, recursive=recursive, include_hidden=include_hidden
            )
        for rel, reason in selection.skipped:
            logger.info(f"Skipped {rel} ({reason})")
        for fp in selection.files:
            problem = _escapes_folder(folder, fp)
            if problem:
                # Changed since it was selected (a link swapped in for the file or a
                # parent folder): it could point anywhere.
                logger.warning(f"Skipped {fp} ({problem} after selection)")
                continue
            try:
                result = self.ingest_file(fp)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to ingest {fp}: {e}")
                results.append(
                    IngestResult(
                        source=str(fp),
                        content_type=ContentType.TEXT,
                        raw_key="",
                        title=fp.name,
                        success=False,
                        error=describe_error(e, self.profile),
                    )
                )
        return results


# Folder ingest uploads whatever it picks to S3 (and, through entity extraction and compile,
# possibly to a cloud LLM), so a project tree or home folder must not leak credentials.
# These directories are skipped at any depth, even with include_hidden: they hold keys,
# tokens (an Obsidian vault's .obsidian/ keeps plugin settings such as S3 keys) or bulky
# runtime state, never knowledge. A single file can still be ingested with --file.
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".ssh", ".gnupg", ".aws", ".azure", ".gcloud", ".kube", ".docker", ".config",
    ".password-store", ".kb", ".obsidian",
    "node_modules", "__pycache__", ".venv", "venv", "site-packages", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".Trash",
})
_EXCLUDED_FILES = frozenset({
    ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", ".htpasswd",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
})
_EXCLUDED_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".kdbx", ".jks", ".keystore",
    ".env", ".tfvars", ".tfstate",
})
# Stems that name a secrets file whatever its format (secrets.yaml, secret.json, ...).
_EXCLUDED_STEMS = frozenset({"secret", "secrets"})


def _escapes_folder(folder: Path, fp: Path) -> str | None:
    """Re-check a selected file right before it is read; a reason, or None when safe.

    Selection walks the folder without following links, but the tree can change before
    the file is read: the file, or any folder between the root and it, may since have been
    replaced by a symlink. So: no path segment below the root may be a link, the resolved
    path must stay inside the resolved root, and the file must open without following a
    final link (``O_NOFOLLOW``) as a regular file that is the one the path names now.

    The handlers then open the file again by path (pandoc, pypdf and the media tools need
    a path), so a swap in the moment between this check and that open is not caught.
    """
    import os
    import stat

    root = Path(folder)
    try:
        rel = fp.relative_to(root)
    except ValueError:
        return "outside the folder"
    current = root
    for part in rel.parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return "a symlink in its path"
        except OSError:
            return "no longer readable"
    try:
        if not fp.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            return "resolves outside the folder"
    except (OSError, RuntimeError):
        return "no longer readable"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(fp, flags)
    except OSError:
        return "no longer readable"
    try:
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(opened.st_mode):
        return "not a regular file"
    try:
        named = os.stat(fp)
    except OSError:
        return "no longer readable"
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        return "replaced"
    return None


class FolderSelection:
    """What a folder ingest reads: ``files`` (paths) and ``skipped`` (relative path, reason)."""

    def __init__(self) -> None:
        self.files: list[Path] = []
        self.skipped: list[tuple[str, str]] = []


def _excluded_name(name: str) -> bool:
    lower = name.lower()
    return (
        lower in _EXCLUDED_FILES
        or lower.startswith(".env")
        or Path(lower).suffix in _EXCLUDED_SUFFIXES
        or Path(lower).stem in _EXCLUDED_STEMS
        or ("service-account" in lower and lower.endswith(".json"))
    )


def select_folder_files(
    folder: Path, recursive: bool = False, *, include_hidden: bool = False
) -> FolderSelection:
    """Pick the regular files a folder ingest may read, and say why the others are skipped.

    Paths are judged relative to ``folder``: a hidden segment (``.name``) anywhere below it
    excludes the file unless ``include_hidden``; credential and runtime directories and
    credential file names are always excluded. Symlinks (to files or directories) are never
    followed: a link can point outside the chosen folder, and one planted in a cloned
    repository would be enough to upload an unrelated local file.
    """
    import os

    root = Path(folder)
    selection = FolderSelection()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root)
        keep = []
        if not recursive:
            dirnames[:] = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix() + "/"
            if (current / d).is_symlink():
                selection.skipped.append((rel, "symlink"))
            elif d in _EXCLUDED_DIRS:
                selection.skipped.append((rel, "credential or runtime directory"))
            elif d.startswith(".") and not include_hidden:
                selection.skipped.append((rel, "hidden"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = current / name
            rel = (rel_dir / name).as_posix()
            if path.is_symlink():
                selection.skipped.append((rel, "symlink"))
            elif _excluded_name(name):
                selection.skipped.append((rel, "credential file"))
            elif name.startswith(".") and not include_hidden:
                selection.skipped.append((rel, "hidden"))
            elif path.is_file():  # no FIFOs, sockets or devices
                selection.files.append(path)
    selection.files.sort()
    return selection


def build_frontmatter_content(
    title: str,
    body: str,
    source: str,
    source_type: ContentType,
    tags: list[str] | None = None,
    original_link: str = "",
    output_language: str | None = None,
) -> str:
    """Build a complete markdown file with YAML frontmatter.

    Converts simplified Chinese to traditional Chinese when the profile's
    ``output_language`` is zh-HK or zh-TW; other languages (and unset, English) are left as-is.
    """
    title = ensure_traditional_chinese(title, output_language)
    body = ensure_traditional_chinese(body, output_language)

    fm = Frontmatter(
        title=title,
        source=source,
        source_type=source_type,
        date=datetime.datetime.now().strftime("%Y-%m-%d"),
        tags=tags or [],
        original_link=original_link,
    )
    return fm.to_yaml_block() + "\n" + body
