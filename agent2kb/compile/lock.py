"""Per-KB compile lease: one compile of a KB at a time.

Two compiles of the same KB (a cron tick and a manual ``a2kb compile``, or two hosts) used
to run side by side, both compiling the same raw files and writing the same wiki pages.
A compile now holds a lease object, ``<prefix>_ops/compile.lock``:

- It is created with ``If-None-Match: *``, so only one writer can create it.
- It records who holds it (a random holder id, host, pid) and when it expires. The holder
  renews it in the background every ``RENEW_SEC`` with ``If-Match`` on the ETag it last
  wrote, so a long compile keeps it however long it runs.
- A lease whose ``expires`` has passed belongs to a compile that died (killed, host lost)
  without releasing it. It is taken over with ``If-Match`` on the stale lease's ETag, so of
  two compiles that find the same stale lease only one wins.
- It is deleted when the compile ends, also on errors.

``TTL_SEC`` is 10 minutes: long enough that a renewal can be late by several minutes (a busy
host, a slow S3 round trip) and that clocks of different hosts can disagree by a few minutes
without a live lease looking expired, short enough that a crashed compile blocks the next one
for at most that long. Expiry compares the holder's clock (``expires``) with the reader's.

A server that rejects conditional writes gets the old behaviour: a warning, and the compile
runs unlocked. See docs/operations.md, "Concurrent writers".
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from agent2kb.models import ProfileConfig
from agent2kb.s3_client import PreconditionFailed, S3Client, _error_code

LOCK_FILE = "_ops/compile.lock"
TTL_SEC = 600
RENEW_SEC = 150

_unlocked_warned = False


def lock_key(profile: ProfileConfig) -> str:
    return f"{profile.prefix}{LOCK_FILE}"


class CompileLocked(Exception):
    """Another compile holds a live lease on this KB. ``info`` is its lease body."""

    def __init__(self, info: dict):
        super().__init__("another compile of this KB is running")
        self.info = info


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(t: datetime.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _expired(info: dict, now: datetime.datetime) -> bool:
    """True when the lease's ``expires`` has passed. An unreadable lease counts as
    expired: nothing that holds it can renew it in a form we understand."""
    try:
        expires = datetime.datetime.fromisoformat(str(info["expires"]))
    except (KeyError, TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.timezone.utc)
    return expires <= now


def _parse(body: str) -> dict:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class CompileLease:
    """A held lease, or an unlocked stand-in (``key`` is None) when the server cannot
    do conditional writes. Use as a context manager, or call :meth:`release`."""

    s3: S3Client
    key: Optional[str]
    holder: str
    started: str
    etag: str = ""
    took_over: Optional[dict] = None
    lost: bool = False

    def __post_init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mutex = threading.Lock()

    def body(self, now: Optional[datetime.datetime] = None) -> str:
        now = now or _now()
        return json.dumps({
            "holder": self.holder,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started": self.started,
            "renewed": _iso(now),
            "expires": _iso(now + datetime.timedelta(seconds=TTL_SEC)),
        }, indent=2, sort_keys=True)

    def start_renewal(self) -> None:
        if self.key is None or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._renew_loop, name="a2kb-compile-lease", daemon=True
        )
        self._thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(RENEW_SEC):
            if not self.renew():
                return

    def renew(self) -> bool:
        """Extend the lease. False once it is lost (taken over, or the write failed)."""
        with self._mutex:
            if self.key is None or self.lost:
                return False
            try:
                self.etag = self.s3.upload(
                    self.key, self.body(), content_type="application/json", if_match=self.etag
                )
                return True
            except PreconditionFailed:
                if _adopt_if_own(self.s3, self):
                    # Our renewal committed but its response was lost; the SDK's retry
                    # then saw the new ETag and answered 412.
                    return True
                self.lost = True
                logger.warning(
                    "The compile lease was taken over by another compile (this one was "
                    "stalled for longer than the lease lifetime); both may now write to "
                    "the KB."
                )
                return False
            except Exception as e:
                # A transient failure: the lease is still ours until it expires, so the
                # next tick tries again.
                logger.warning(f"Could not renew the compile lease: {e}")
                return True

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        with self._mutex:
            if self.key is None or self.lost:
                return
            try:
                body, _etag = self.s3.download_with_etag(self.key)
            except Exception as e:
                logger.debug(f"compile lease release: could not read it back: {e}")
                return
            # Delete only our own lease; one that was taken over belongs to someone else.
            if _parse(body).get("holder") == self.holder:
                try:
                    self.s3.delete(self.key)
                except Exception as e:
                    logger.warning(f"Could not release the compile lease: {e}")
            self.key = None

    def __enter__(self) -> "CompileLease":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _warn_unlocked() -> None:
    global _unlocked_warned
    if not _unlocked_warned:
        _unlocked_warned = True
        logger.warning(
            "The S3 endpoint does not accept conditional writes, so compiles of one KB are "
            "not locked against each other; running unlocked as before "
            "(see docs/operations.md)."
        )


def acquire(s3: S3Client, profile: ProfileConfig) -> CompileLease:
    """Take the KB's compile lease and start renewing it.

    Raises :class:`CompileLocked` when another compile holds a live lease. On a server
    without conditional writes this returns an unlocked lease after one warning.
    """
    key = lock_key(profile)
    lease = CompileLease(s3=s3, key=key, holder=secrets.token_hex(8), started=_iso(_now()))
    if not s3.conditional_writes:
        _warn_unlocked()
        lease.key = None
        return lease

    # A second round covers a lease that was released between our create and our read.
    for _ in range(2):
        try:
            lease.etag = s3.upload(
                key, lease.body(), content_type="application/json", if_none_match=True
            )
        except PreconditionFailed:
            pass
        else:
            if not s3.conditional_writes:
                # The server rejected the header and upload() fell back to a plain PUT.
                # Remove that write, but only while it is still ours: a plain PUT cannot
                # tell whether another compile's lease was there, or arrived since.
                _warn_unlocked()
                _delete_if_own(s3, lease)
                lease.key = None
                return lease
            _verify_own(s3, lease)
            lease.start_renewal()
            return lease

        try:
            current, etag = s3.download_with_etag(key)
        except Exception as e:
            if _is_missing(e):
                continue
            raise
        info = _parse(current)
        if info.get("holder") == lease.holder:
            # Our create committed but its response was lost and the retry got 412.
            lease.etag = etag
            lease.start_renewal()
            return lease
        if not _expired(info, _now()):
            raise CompileLocked(info)
        try:
            lease.etag = s3.upload(
                key, lease.body(), content_type="application/json", if_match=etag
            )
        except PreconditionFailed:
            if _adopt_if_own(s3, lease):
                lease.took_over = info
                lease.start_renewal()
                return lease
            # Another compile took the stale lease (or renewed it) first.
            current, _ = _read_or_empty(s3, key)
            raise CompileLocked(_parse(current)) from None
        lease.took_over = info
        _verify_own(s3, lease)
        lease.start_renewal()
        return lease
    current, _ = _read_or_empty(s3, key)
    raise CompileLocked(_parse(current))


def _verify_own(s3: S3Client, lease: CompileLease) -> None:
    """Read the lease back. On a server that accepts conditional headers but ignores
    them, two creates both "succeed"; whichever wrote last owns the lease, and the other
    one learns it here (unless both read back before the second write: see the docs)."""
    current, etag = _read_or_empty(s3, lease.key)
    info = _parse(current)
    if info.get("holder") != lease.holder:
        lease.key = None
        raise CompileLocked(info)
    lease.etag = etag or lease.etag


def _adopt_if_own(s3: S3Client, lease: CompileLease) -> bool:
    """After a 412 on our own write: True (and the lease's ETag updated) when the stored
    lease is ours anyway. The SDK retries a PUT whose response was lost; the first attempt
    may have committed, and the retry then fails its condition against our own write."""
    try:
        current, etag = _read_or_empty(s3, lease.key)
    except Exception as e:
        logger.debug(f"compile lease: could not read it back after a conflict: {e}")
        return False
    if _parse(current).get("holder") != lease.holder:
        return False
    lease.etag = etag or lease.etag
    return True


def _delete_if_own(s3: S3Client, lease: CompileLease) -> None:
    try:
        current, _etag = _read_or_empty(s3, lease.key)
        if _parse(current).get("holder") == lease.holder:
            s3.delete(lease.key)
    except Exception as e:
        logger.debug(f"compile lease: could not remove the unlocked write: {e}")


def _read_or_empty(s3: S3Client, key: str) -> tuple[str, str]:
    try:
        return s3.download_with_etag(key)
    except Exception as e:
        if _is_missing(e):
            return "", ""
        raise


def _is_missing(e: Exception) -> bool:
    return _error_code(e) in ("NoSuchKey", "404", "NotFound")
