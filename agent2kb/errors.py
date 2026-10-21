"""Translate S3 / botocore failures into one-line messages with a fix hint.

Without this, a wrong key or a missing bucket surfaces as a 40-line rich
traceback. ``translate`` maps the common failures to a ``FriendlyError``;
anything it does not recognise returns ``None`` so the caller can let the
original exception propagate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from botocore import exceptions as bex

from agent2kb.models import ProfileConfig

#: Exit status for a translated (expected, user-fixable) error.
EXIT_FRIENDLY_ERROR = 1
#: Exit status for a config problem (bad/missing profile, rejected credential source).
EXIT_CONFIG_ERROR = 2


@dataclass(frozen=True)
class FriendlyError:
    """A recognised failure: what went wrong, and what to try."""

    message: str
    hint: str
    # Stable short id (e.g. "bad_credentials") so `a2kb doctor` can branch on it.
    code: str = ""


_BUCKET_NAME_HINT = (
    "Bucket names are 3-63 characters: lowercase letters, digits, hyphens and dots, "
    "starting and ending with a letter or digit. Fix `bucket:` in your config."
)


def _where(profile: Optional[ProfileConfig]) -> str:
    if profile is None:
        return ""
    endpoint = profile.s3_endpoint or "AWS S3"
    return f" (endpoint {endpoint}, bucket '{profile.bucket}')"


def _client_error(e: bex.ClientError, profile: Optional[ProfileConfig]) -> FriendlyError:
    err = e.response.get("Error", {})
    code = str(err.get("Code", ""))
    op = e.operation_name or "request"
    bucket = profile.bucket if profile else "<bucket>"
    prefix = profile.prefix if profile else ""

    if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidSecurityToken"):
        what = (
            "access key is not recognised"
            if code == "InvalidAccessKeyId"
            else (
                "secret key does not match the access key"
                if code == "SignatureDoesNotMatch"
                else "session token is invalid"
            )
        )
        return FriendlyError(
            f"S3 rejected the credentials: the {what}.",
            "Check access_key / secret_key (or KB_ACCESS_KEY / KB_SECRET_KEY) and that they "
            "belong to this endpoint. Run `a2kb doctor` for a full check.",
            "bad_credentials",
        )
    if code in ("ExpiredToken", "ExpiredTokenException"):
        return FriendlyError(
            "The S3 session token has expired.",
            "Refresh your AWS credentials (e.g. `aws sso login`) and retry.",
            "expired_token",
        )
    if code == "RequestTimeTooSkewed":
        return FriendlyError(
            "Your system clock differs from the S3 server by more than 15 minutes.",
            "Sync the clock (e.g. enable NTP) and retry.",
            "clock_skew",
        )
    if code in ("NoSuchBucket", "NotFound") or (code == "404" and op == "HeadBucket"):
        return FriendlyError(
            f"Bucket '{bucket}' does not exist.",
            "Create it with `a2kb doctor --fix`, or in your provider's console, "
            "or fix `bucket:` in your config.",
            "no_such_bucket",
        )
    if code == "InvalidBucketName":
        return FriendlyError(
            f"Invalid bucket name '{bucket}'.", _BUCKET_NAME_HINT, "bad_bucket_name"
        )
    if code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
        owned = code == "BucketAlreadyOwnedByYou"
        return FriendlyError(
            f"Bucket '{bucket}' already exists"
            + ("." if owned else " and belongs to someone else."),
            "Bucket names are global on some providers; pick another name in `bucket:`."
            if not owned
            else "Nothing to do — it is already yours.",
            "bucket_exists",
        )
    if code in (
        "AuthorizationHeaderMalformed",
        "PermanentRedirect",
        "301",
        "IllegalLocationConstraintException",
    ):
        return FriendlyError(
            "The bucket lives in a different region than the one this client is using.",
            "Set `region:` in your config to the bucket's region (use `auto` for Cloudflare R2).",
            "wrong_region",
        )
    if code in ("AccessDenied", "403", "Forbidden", "AllAccessDisabled", "AccountProblem"):
        scope = f" under '{prefix}'" if prefix else ""
        return FriendlyError(
            f"Access denied (403) on {op}{_where(profile)}.",
            f"The key is wrong, or it lacks permission{scope}. Check access_key / secret_key, "
            "and that the key's policy (IAM / application-key scope) covers this bucket and "
            "prefix. `a2kb doctor` shows which step fails.",
            "access_denied",
        )
    if code in ("404", "NoSuchKey"):
        return FriendlyError(
            f"Object not found ({op}).",
            "The key does not exist under this profile's prefix.",
            "no_such_key",
        )
    if code in ("SlowDown", "ServiceUnavailable", "503", "RequestLimitExceeded", "Throttling"):
        return FriendlyError(
            "The S3 server is throttling or temporarily unavailable.",
            "Wait a moment and retry; reduce parallel runs if this repeats.",
            "throttled",
        )
    if (
        code
        in (
            "XAmzContentSHA256Mismatch",
            "InvalidRequest",
            "NotImplemented",
            "MissingContentLength",
            "BadDigest",
            "InvalidDigest",
        )
        or "checksum" in str(err.get("Message", "")).lower()
    ):
        return FriendlyError(
            f"The S3 server rejected the request ({code}); likely a checksum incompatibility.",
            "Set `checksum_mode: when_required` in your config (KB_S3_CHECKSUM_MODE).",
            "checksum",
        )
    msg = str(err.get("Message", "")).strip()
    return FriendlyError(
        f"S3 error {code or 'unknown'} on {op}" + (f": {msg}" if msg else "") + ".",
        "Run `a2kb doctor` to check endpoint, credentials and bucket; "
        "use -v for the full traceback.",
        "s3_error",
    )


def translate(
    exc: BaseException, profile: Optional[ProfileConfig] = None
) -> Optional[FriendlyError]:
    """Map ``exc`` to a ``FriendlyError``, or ``None`` if it is not a known S3 failure."""
    if isinstance(exc, bex.ClientError):
        return _client_error(exc, profile)

    if isinstance(exc, (bex.NoCredentialsError, bex.PartialCredentialsError)):
        return FriendlyError(
            "No S3 credentials found.",
            "Set access_key and secret_key in your config (or KB_ACCESS_KEY / KB_SECRET_KEY), "
            "or leave both empty to use the AWS credential chain "
            "(AWS_ACCESS_KEY_ID env vars, ~/.aws/credentials, an IAM role, or `aws_profile:`).",
            "no_credentials",
        )
    if isinstance(exc, bex.ProfileNotFound):
        return FriendlyError(
            f"AWS profile not found: {exc}",
            "Check `aws_profile:` in your config against the profiles in ~/.aws/config.",
            "no_aws_profile",
        )
    if isinstance(exc, bex.SSLError):
        return FriendlyError(
            f"TLS/SSL error talking to the S3 endpoint{_where(profile)}.",
            "Check the certificate (self-signed? expired?) or whether the endpoint should be "
            "http:// instead of https://.",
            "ssl",
        )
    if isinstance(exc, (bex.ConnectTimeoutError, bex.ReadTimeoutError)):
        return FriendlyError(
            f"Timed out talking to the S3 endpoint{_where(profile)}.",
            "Check the endpoint URL and port, your network or VPN, and that the server is up.",
            "timeout",
        )
    if isinstance(
        exc, (bex.EndpointConnectionError, bex.ProxyConnectionError, bex.ConnectionClosedError)
    ):
        # The exception carries the full object URL; show just the server.
        url = (profile.s3_endpoint if profile else None) or getattr(exc, "kwargs", {}).get(
            "endpoint_url"
        )
        if url and not (profile and profile.s3_endpoint):
            parts = urlparse(url)
            url = f"{parts.scheme}://{parts.netloc}"
        return FriendlyError(
            f"Cannot reach the S3 endpoint{f' {url}' if url else ''}.",
            "Check the s3_endpoint URL and port, and that the server is running "
            "(try `curl -I <endpoint>`). Run `a2kb doctor` for a full check.",
            "unreachable",
        )
    if isinstance(exc, ValueError) and str(exc).startswith("Invalid endpoint"):
        return FriendlyError(
            f"Invalid s3_endpoint: {exc}",
            "The endpoint must be a full URL such as https://s3.example.com "
            "or http://localhost:8333.",
            "bad_endpoint",
        )
    if isinstance(exc, bex.ParamValidationError):
        text = str(exc)
        if "Invalid bucket name" in text:
            bucket = profile.bucket if profile else "<bucket>"
            return FriendlyError(
                f"Invalid bucket name '{bucket}'.", _BUCKET_NAME_HINT, "bad_bucket_name"
            )
        return FriendlyError(
            f"Invalid S3 request parameter: {text.splitlines()[-1] if text else exc}",
            "Check `bucket:` and `prefix:` in your config; use -v for the full traceback.",
            "bad_param",
        )
    if isinstance(exc, (bex.InvalidEndpointConfigurationError, bex.EndpointResolutionError)):
        return FriendlyError(
            f"Invalid S3 endpoint/region configuration: {exc}",
            "Check `s3_endpoint:` and `region:` in your config.",
            "bad_endpoint",
        )
    return None


def describe_error(exc: BaseException, profile: Optional[ProfileConfig] = None) -> str:
    """One-line text for ``exc``: the translated message and hint, else ``str(exc)``.

    For code that stores an error string (e.g. ``IngestResult.error``) instead of
    letting the exception reach the CLI hook.
    """
    friendly = translate(exc, profile)
    return f"{friendly.message} {friendly.hint}" if friendly else str(exc)
