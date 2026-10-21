"""S3 client wrapper with mandatory prefix enforcement for agent2kb.

All S3 operations MUST go through this module. Never use boto3 directly
in command handlers. Every operation is scoped to the active profile's prefix.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from loguru import logger

from agent2kb.models import ProfileConfig, bucket_name_problem

# Parallel fan-out for the scan-heavy paths (compile's up-to-date check,
# the embedding refresh, `a2kb status`). Measured against a remote SeaweedFS
# at ~74ms per object: 16 workers cut a 2,300-object scan from 171s to 15s,
# while 32 workers were slower again — the endpoint starts queueing. Keep
# the boto3 connection pool at least this large or threads block on sockets.
_MAX_WORKERS = 16


def _is_aws_endpoint(endpoint: Optional[str]) -> bool:
    """True when ``endpoint`` is unset (boto3 picks AWS) or an amazonaws.com host."""
    if not endpoint:
        return True
    host = (urlparse(endpoint if "//" in endpoint else f"//{endpoint}").hostname or "").lower()
    return host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn")


def _error_code(exc: Exception) -> str:
    """The S3 error code of a botocore ``ClientError`` (empty for anything else)."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", ""))


def _botocore_supports_checksum_config() -> bool:
    """botocore >= 1.36 has ``request_checksum_calculation``; older versions reject it."""
    return "request_checksum_calculation" in BotoConfig.OPTION_DEFAULTS


def build_boto_config(profile: ProfileConfig) -> BotoConfig:
    """Botocore config for a profile.

    The signature, retry and pool settings are what SeaweedFS profiles have
    always used. Provider-neutral extras (addressing style, checksum mode) are
    only added when configured. ``checksum_mode`` unset means boto3's default for
    every endpoint, so existing profiles behave exactly as before.
    """
    kwargs: dict = {
        "signature_version": "s3v4",
        "retries": {"max_attempts": 3, "mode": "adaptive"},
        "max_pool_connections": _MAX_WORKERS * 2,
    }
    if profile.addressing_style:
        kwargs["s3"] = {"addressing_style": profile.addressing_style}

    # boto3 >= 1.36 adds CRC32 checksums to every PUT and some S3-compatible
    # servers reject them; those profiles set ``checksum_mode: when_required``.
    mode = profile.checksum_mode
    if mode and _botocore_supports_checksum_config():
        kwargs["request_checksum_calculation"] = mode
        kwargs["response_checksum_validation"] = mode
    return BotoConfig(**kwargs)


#: Quarantine folder inside ``raw/`` for sources that failed compile too often.
QUARANTINE_DIR = ".failed/"


def area_relative(prefix: str, key: str, area: str) -> Optional[str]:
    """Return ``key`` relative to ``<prefix><area>`` (``area`` such as ``"raw/"``).

    ``None`` when the key is not under that folder or has empty, ``.`` or ``..``
    segments. Works for the empty prefix of a whole-bucket admin KB, where a raw
    key is ``raw/x.md`` with no leading slash for a ``"/raw/"`` match to find.
    """
    head = f"{prefix}{area}"
    if not key.startswith(head):
        return None
    rel = key[len(head):]
    if not rel or any(seg in ("", ".", "..") for seg in rel.split("/")):
        return None
    return rel


def wiki_key_for_raw(prefix: str, raw_key: str) -> str:
    """The wiki page compiled from ``raw_key``: ``<prefix>raw/a.md`` -> ``<prefix>wiki/a.md``.

    Raises ``ValueError`` for a key outside ``<prefix>raw/`` or inside the
    quarantine folder, so a caller can never derive a destination equal to the
    source and overwrite it.
    """
    rel = area_relative(prefix, raw_key, "raw/")
    if rel is None or rel.startswith(QUARANTINE_DIR):
        raise ValueError(f"not a raw source key of this KB: {raw_key!r}")
    return f"{prefix}wiki/{rel}"


def quarantine_key_for_raw(prefix: str, raw_key: str) -> str:
    """Where a persistently failing source is moved: ``<prefix>raw/.failed/<rel>``."""
    rel = area_relative(prefix, raw_key, "raw/")
    if rel is None or rel.startswith(QUARANTINE_DIR):
        raise ValueError(f"not a raw source key of this KB: {raw_key!r}")
    return f"{prefix}raw/{QUARANTINE_DIR}{rel}"


class PreconditionFailed(Exception):
    """A conditional write lost a race: the object changed (``if_match``),
    already exists (``if_none_match``) or was deleted since it was read.
    The caller re-reads and retries, or picks another key."""


#: Error codes a server answers with when it does not implement conditional
#: PUT headers. Such a provider gets plain PUTs for the rest of the process.
_CONDITIONAL_UNSUPPORTED = frozenset({"NotImplemented", "501"})
#: Generic request errors that count only when their message names the conditional
#: headers: the same codes also report unrelated problems (a bad checksum, a bad key),
#: and one of those must not switch conditional writes off for a long-lived process.
_CONDITIONAL_REJECTED = frozenset({"InvalidArgument", "InvalidRequest"})
_CONDITIONAL_HEADER_RE = re.compile(r"if-?(none-?)?match|conditional", re.IGNORECASE)
_UNKNOWN_CONDITIONAL_PARAM_RE = re.compile(r'Unknown parameter in input: "If(None)?Match"')


def _conditional_unsupported(exc: Exception) -> bool:
    """True when a conditional PUT failed because the server (or an old botocore)
    does not support the If-Match / If-None-Match headers at all."""
    if type(exc).__name__ == "ParamValidationError":
        return bool(_UNKNOWN_CONDITIONAL_PARAM_RE.search(str(exc)))
    code = _error_code(exc)
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        response = {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code in _CONDITIONAL_UNSUPPORTED or status == 501:
        return True
    if code in _CONDITIONAL_REJECTED:
        message = str(response.get("Error", {}).get("Message", ""))
        return bool(_CONDITIONAL_HEADER_RE.search(message))
    return False


@dataclass(frozen=True)
class S3Object:
    """One listed object. ``etag`` lets callers detect changes without
    downloading the body — a list page carries it for free."""

    key: str
    etag: str
    size: int
    last_modified: Optional[str] = None


class S3Client:
    """Prefix-enforced S3 client for agent2kb."""

    def __init__(self, profile: ProfileConfig):
        self.profile = profile
        self.bucket = profile.bucket
        self.prefix = profile.prefix
        client_kwargs: dict = {
            "endpoint_url": profile.s3_endpoint,
            "config": build_boto_config(profile),
        }
        if profile.region:
            client_kwargs["region_name"] = profile.region
        # Both keys set → use them; both empty → boto3's default credential
        # chain (env, ~/.aws, IAM role). The model rejects exactly one.
        if profile.access_key and profile.secret_key:
            client_kwargs["aws_access_key_id"] = profile.access_key
            client_kwargs["aws_secret_access_key"] = profile.secret_key
        if profile.aws_profile:
            self._client = boto3.Session(profile_name=profile.aws_profile).client(
                "s3", **client_kwargs
            )
        else:
            self._client = boto3.client("s3", **client_kwargs)
        #: False once the endpoint (or an old botocore) has rejected a
        #: conditional PUT; later writes then fall back to plain PUTs.
        self.conditional_writes = True

    def _enforce_prefix(self, key: str) -> str:
        """Ensure the key is within the allowed prefix. Raises on violation.

        Segment-exact: prefix "projects/proj/" must NOT match "projects/proj2/...".
        The key S3 receives is the one checked, so a member KB's key may not
        contain empty, "." or ".." segments: S3 does not normalise them, and
        "kbs//notes/x" or "kbs/./notes/x" would otherwise pass a normalised
        comparison while naming an object outside the prefix.
        """
        # Reject path traversal attempts
        if ".." in key.split("/") or key.startswith("/"):
            raise PermissionError(f"Access denied: path traversal detected in key '{key}'.")

        if self.profile.is_super:
            return key

        prefix_clean = self.prefix.rstrip("/")
        if not prefix_clean:
            # Empty prefix (should only be super, but guard anyway)
            return key

        segments = key.split("/")
        # A trailing "" is a listing prefix ending in "/"; anywhere else it is "//".
        if any(seg in ("", ".") for seg in segments[:-1]) or segments[-1] == ".":
            raise PermissionError(
                f"Access denied: key '{key}' has an empty or '.' path segment."
            )
        if not key.startswith(prefix_clean + "/"):
            raise PermissionError(
                f"Access denied: key '{key}' is outside profile prefix '{self.prefix}'. "
                f"Profile '{self.profile.name}' can only access keys under '{self.prefix}'."
            )
        return key

    def _enforce_meta_access(self, key: str) -> None:
        """Block non-super profiles from accessing _meta/ prefix."""
        if not self.profile.is_super and key.startswith("_meta/"):
            raise PermissionError(
                "Access denied: '_meta/' prefix is reserved for admin KBs (role: admin) and the "
                "legacy `super` profile. "
                f"Profile '{self.profile.name}' cannot access '{key}'."
            )

    # ── Bucket-level operations ───────────────────────────────────────
    # Not prefix-scoped. Only call these from explicit commands (``a2kb doctor
    # --fix``) — never implicitly from ingest/compile/query.

    def bucket_exists(self) -> bool:
        """True if the bucket exists and is reachable with these credentials.

        A 404 / NoSuchBucket answer is ``False``. Any other failure (403, no
        network, bad credentials) raises, because "unknown" must not read as
        "missing" — the caller would offer to create a bucket that is there.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except self._client.exceptions.ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchBucket", "NotFound"):
                return False
            raise

    def create_bucket(self) -> None:
        """Create the profile's bucket. Explicit use only (``a2kb doctor --fix``)."""
        problem = bucket_name_problem(self.bucket)
        if problem:
            raise ValueError(f"Invalid bucket name: {problem}")
        kwargs: dict = {"Bucket": self.bucket}
        region = self._client.meta.region_name
        # AWS rejects a LocationConstraint of us-east-1 and requires one for every
        # other region. Non-AWS servers (R2 "auto", MinIO, SeaweedFS) don't use it.
        if _is_aws_endpoint(self.profile.s3_endpoint) and region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        self._client.create_bucket(**kwargs)
        logger.info(f"Created bucket {self.bucket}")

    def list_one(self, sub_prefix: str = "") -> list[str]:
        """List at most one key under prefix + sub_prefix (cheap access probe)."""
        full_prefix = self.prefix + sub_prefix
        if not self.profile.is_super:
            self._enforce_prefix(full_prefix)
        resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=full_prefix, MaxKeys=1)
        return [o["Key"] for o in resp.get("Contents", [])]

    def upload(
        self,
        key: str,
        body: str | bytes,
        content_type: str = "text/markdown",
        *,
        if_match: Optional[str] = None,
        if_none_match: bool = False,
    ) -> str:
        """Upload content to S3 under the enforced prefix. Returns the new ETag.

        ``if_match`` (an ETag from a read) makes the write fail with
        :class:`PreconditionFailed` when the object changed or vanished since;
        ``if_none_match`` makes it fail when the key already exists. Both are
        S3 conditional writes. A server that rejects the headers is detected
        once and then written to unconditionally, as before
        (``self.conditional_writes`` turns False); one that silently ignores
        them gives no protection, see docs/operations.md.
        """
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)

        if isinstance(body, str):
            body = body.encode("utf-8")

        kwargs: dict = {"Bucket": self.bucket, "Key": key, "Body": body,
                        "ContentType": content_type}
        conditional = self.conditional_writes and (if_match or if_none_match)
        if conditional:
            if if_match:
                kwargs["IfMatch"] = if_match if if_match.startswith('"') else f'"{if_match}"'
            if if_none_match:
                kwargs["IfNoneMatch"] = "*"
        try:
            response = self._client.put_object(**kwargs)
        except Exception as e:
            if not conditional:
                raise
            code = _error_code(e)
            if code in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                raise PreconditionFailed(key) from None
            if if_match and code in ("NoSuchKey", "404", "NotFound"):
                raise PreconditionFailed(key) from None
            if _conditional_unsupported(e):
                logger.warning(
                    "The S3 endpoint does not accept conditional writes; concurrent "
                    "updates fall back to last-writer-wins (see docs/operations.md)."
                )
                self.conditional_writes = False
                kwargs.pop("IfMatch", None)
                kwargs.pop("IfNoneMatch", None)
                response = self._client.put_object(**kwargs)
            else:
                raise
        logger.debug(f"Uploaded s3://{self.bucket}/{key}")
        return (response.get("ETag") or "").strip('"')

    def download_with_etag(self, key: str) -> tuple[str, str]:
        """Download a UTF-8 object and its ETag, for a later ``upload(if_match=...)``."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)

        response = self._client.get_object(Bucket=self.bucket, Key=key)
        try:
            content = response["Body"].read().decode("utf-8")
        finally:
            response["Body"].close()
        return content, (response.get("ETag") or "").strip('"')

    def head_etag(self, key: str) -> Optional[str]:
        """The object's ETag from a HEAD request, or ``None`` when it does not exist."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except self._client.exceptions.ClientError as e:
            if _error_code(e) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return (response.get("ETag") or "").strip('"')

    def download(self, key: str) -> str:
        """Download content from S3. Returns UTF-8 decoded string."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)

        response = self._client.get_object(Bucket=self.bucket, Key=key)
        try:
            content = response["Body"].read().decode("utf-8")
        finally:
            response["Body"].close()
        logger.debug(f"Downloaded s3://{self.bucket}/{key}")
        return content

    def download_bytes(self, key: str) -> bytes:
        """Download raw bytes from S3."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)

        response = self._client.get_object(Bucket=self.bucket, Key=key)
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def list_keys(self, sub_prefix: str = "", suffix: str = "") -> list[str]:
        """List all keys under prefix + sub_prefix, optionally filtered by suffix."""
        full_prefix = self.prefix + sub_prefix
        if not self.profile.is_super:
            self._enforce_prefix(full_prefix)

        keys = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if suffix and not k.endswith(suffix):
                    continue
                keys.append(k)
        return keys

    def list_objects(self, sub_prefix: str = "", suffix: str = "") -> list[S3Object]:
        """List objects under prefix + sub_prefix with their ETags.

        Same cost as :meth:`list_keys` — the ETag and size ride along in the
        list response — but lets a caller skip downloading bodies that have
        not changed. On this workload a full listing is ~0.6s against ~171s
        to download the same objects one by one.
        """
        full_prefix = self.prefix + sub_prefix
        if not self.profile.is_super:
            self._enforce_prefix(full_prefix)

        objects = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if suffix and not k.endswith(suffix):
                    continue
                objects.append(
                    S3Object(
                        key=k,
                        # Strip the quotes S3 wraps ETags in so stored values
                        # compare cleanly across providers.
                        etag=(obj.get("ETag") or "").strip('"'),
                        size=obj.get("Size", 0),
                        last_modified=(
                            obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S")
                            if obj.get("LastModified")
                            else None
                        ),
                    )
                )
        return objects

    def download_many(
        self, keys: list[str], on_error: Optional[Callable[[str, Exception], None]] = None
    ) -> dict[str, str]:
        """Download many keys in parallel. Returns {key: content}.

        Keys that fail are omitted rather than raising, so one unreadable
        object cannot abort a whole scan; pass ``on_error`` to observe them.
        These paths are latency-bound, not CPU-bound, so threads are the
        right tool despite the GIL.
        """
        if not keys:
            return {}

        results: dict[str, str] = {}

        def fetch(key: str) -> tuple[str, Optional[str]]:
            try:
                return key, self.download(key)
            except Exception as e:  # noqa: BLE001 - reported via on_error
                if on_error:
                    on_error(key, e)
                else:
                    logger.debug(f"download_many skipped {key}: {e}")
                return key, None

        workers = min(_MAX_WORKERS, len(keys))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, content in pool.map(fetch, keys):
                if content is not None:
                    results[key] = content
        return results

    def exists(self, key: str) -> bool:
        """Check if a key exists in S3."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except self._client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            raise

    def delete(self, key: str) -> None:
        """Delete a key from S3."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)

        self._client.delete_object(Bucket=self.bucket, Key=key)
        logger.debug(f"Deleted s3://{self.bucket}/{key}")

    def get_metadata(self, key: str) -> dict:
        """Get object metadata (last modified, size, etc.)."""
        key = self._enforce_prefix(key)
        self._enforce_meta_access(key)
        response = self._client.head_object(Bucket=self.bucket, Key=key)
        return {
            "last_modified": response["LastModified"],
            "size": response["ContentLength"],
            "content_type": response.get("ContentType", ""),
        }

    def list_raw_files(self) -> list[str]:
        """List all files in the raw/ folder for this profile.

        Skips the raw/.failed/ quarantine subfolder — those are held aside
        after repeated compile failures and should not be retried automatically.
        """
        keys = self.list_keys(sub_prefix="raw/", suffix=".md")
        # Relative to the prefix, so a whole-bucket KB (prefix "") also skips
        # its root-level raw/.failed/.
        head = f"{self.prefix}raw/{QUARANTINE_DIR}"
        return [k for k in keys if not k.startswith(head)]

    def list_wiki_files(self) -> list[str]:
        """List all files in the wiki/ folder for this profile."""
        return self.list_keys(sub_prefix="wiki/", suffix=".md")

    def list_output_files(self) -> list[str]:
        """List all files in the outputs/ folder for this profile."""
        return self.list_keys(sub_prefix="outputs/", suffix=".md")

    def list_all_prefixes(self) -> list[str]:
        """List all top-level prefixes (super profile only)."""
        if not self.profile.is_super:
            raise PermissionError(
                "Only an admin KB (role: admin) or the legacy `super` profile can list prefixes."
            )

        prefixes = set()
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Delimiter="/"):
            for prefix_obj in page.get("CommonPrefixes", []):
                prefixes.add(prefix_obj["Prefix"])
        return sorted(prefixes)

    def get_last_modified(self, key: str) -> Optional[str]:
        """Get the last modified timestamp for a key."""
        try:
            meta = self.get_metadata(key)
            return meta["last_modified"].isoformat()
        except Exception:
            return None
