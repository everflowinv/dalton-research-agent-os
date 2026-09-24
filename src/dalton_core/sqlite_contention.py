"""One answer to "how long does a writer wait for SQLite's write lock".

2026-09-24: after a deploy, six legacy cockpit results and four
publication-worker products failed with ``OperationalError: database is
locked`` at the model router's ``BEGIN IMMEDIATE``.  The router and the
scheduler waited 5 s for the write lock and the day-ledger inherited
``sqlite3.connect``'s implicit 5 s; the controller, the writer and every lane
child share those three files, so a burst of children routing at once queued
past five seconds and the loser raised instead of waiting its turn.  The same
error then hit the backstop's own ``scheduler.complete``, which only printed
it, and the attempt's lease stayed held for its frozen 2h10m.

This module is the one place the wait is defined, plus the two things the
cockpit needs around it: recognising the error that is worth retrying, and a
bounded retry for the calls that must not be lost (completing or releasing a
lease), and naming *which* database a failure came from.
"""

from __future__ import annotations

import sqlite3
import time
import traceback
from typing import Any, Callable, TypeVar

# The wait every writer of the shared router, scheduler and day ledger gives
# another writer's transaction before ``database is locked``.  A route or a
# completion holds the lock for milliseconds; thirty seconds absorbs a burst
# of lane children without letting a wedged writer stall a caller forever.
SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)

# How long a lease completion keeps retrying a locked scheduler before it
# gives up and leaves a durable record for the next ask to reclaim.
LEASE_RELEASE_RETRY_SECONDS = 60.0

_LOCK_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)

T = TypeVar("T")


def is_sqlite_lock_error(exc: BaseException | None) -> bool:
    """Whether ``exc`` is SQLite refusing the lock, which a retry can cure."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(text in message for text in _LOCK_MESSAGES)


def retry_on_sqlite_lock(
    operation: Callable[[], T],
    *,
    deadline_seconds: float = LEASE_RELEASE_RETRY_SECONDS,
    initial_delay: float = 0.25,
    max_delay: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run ``operation``, retrying only SQLite lock errors, for a bounded time.

    Anything that is not a lock error propagates at once.  A lock error is
    retried with doubling back-off until ``deadline_seconds`` have elapsed
    since the first try; the last lock error then propagates.  Every caller in
    this package passes an operation that is idempotent (the scheduler's
    completion carries an idempotency key), so a retry after an ambiguous
    failure replays rather than doubles.
    """

    started = monotonic()
    delay = initial_delay
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_error(exc):
                raise
            remaining = deadline_seconds - (monotonic() - started)
            if remaining <= 0:
                raise
            wait = min(delay, max_delay, remaining)
            if on_retry is not None:
                on_retry(attempt, exc, wait)
            sleep(wait)
            delay = min(delay * 2, max_delay)


def sqlite_failure_location(tb: Any) -> str | None:
    """The database file a failure was raised against, read off its frames.

    Every SQLite owner here keeps its file as ``self.path`` beside
    ``self.connection``; the innermost frame whose ``self`` has both is the
    store that raised.  ``None`` when no frame names one.
    """

    found: str | None = None
    while tb is not None:
        owner = tb.tb_frame.f_locals.get("self")
        path = getattr(owner, "path", None)
        if isinstance(path, str) and hasattr(owner, "connection"):
            found = path
        tb = tb.tb_next
    return found


def traceback_digest(exc: BaseException, *, frames: int = 6) -> list[str]:
    """The last few frames as ``file:line in function``, newest last."""

    summary = traceback.extract_tb(exc.__traceback__)[-frames:]
    return [
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"
        for frame in summary
    ]


__all__ = [
    "LEASE_RELEASE_RETRY_SECONDS",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_BUSY_TIMEOUT_SECONDS",
    "is_sqlite_lock_error",
    "retry_on_sqlite_lock",
    "sqlite_failure_location",
    "traceback_digest",
]
