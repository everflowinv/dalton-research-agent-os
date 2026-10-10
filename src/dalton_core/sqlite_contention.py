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
import sys
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

# How long a connector lane child (web fetch/search, AlphaEngine, Guidepoint)
# keeps retrying its governed call when a shared SQLite file stays locked.
# 2026-09-29: eleven of seventeen post-deploy web fetches died at
# ``scheduler.enqueue`` on one 30 s busy wait and were settled as
# ``acquisition_failed`` / ``unknown_failure``; the lock had simply not been
# released yet.  Each try already waits the full busy timeout, so this is a
# handful of tries with a short pause between them, well inside a lane tick.
LANE_LOCK_RETRY_SECONDS = 120.0
LANE_LOCK_RETRY_INITIAL_DELAY = 1.0
LANE_LOCK_RETRY_MAX_DELAY = 5.0

# 2026-10-07: a paid call's day-ledger admission (``cockpit_model``).  Live
# 10-07 the publication worker logged 152 "gave up on database is locked" in
# its last 400 stderr lines, and almost none of them had printed a single
# "retry 1": the first ``BEGIN IMMEDIATE`` had already used the whole 60 s
# that the lease-release deadline allowed, so the admission was given up
# without ever being retried.  An admission is idempotent (keyed on WorkOrder,
# attempt and phase) and nothing is spent until it lands, so it waits longer
# than a lease release does, with a longer busy wait per try and a jittered
# back-off between tries -- the worker's four threads and every lane child
# that lost the same race must not all come back at the same instant.
ADMISSION_LOCK_RETRY_SECONDS = 240.0
ADMISSION_BUSY_TIMEOUT_SECONDS = 60.0
ADMISSION_LOCK_RETRY_INITIAL_DELAY = 0.5
ADMISSION_LOCK_RETRY_MAX_DELAY = 20.0

# The closed failure code a lane summary carries when the lock never cleared.
SQLITE_LOCKED_FAILURE_CODE = "sqlite_locked"

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
    jitter: Callable[[float], float] | None = None,
) -> T:
    """Run ``operation``, retrying only SQLite lock errors, for a bounded time.

    ``jitter`` maps each back-off to the wait actually slept (it is still
    capped by the time left); :func:`equal_jitter` is the usual choice when
    several processes lose the same lock at once.

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
            wait = min(delay, max_delay)
            if jitter is not None:
                wait = max(0.0, float(jitter(wait)))
            wait = min(wait, remaining)
            if on_retry is not None:
                on_retry(attempt, exc, wait)
            sleep(wait)
            delay = min(delay * 2, max_delay)


def equal_jitter(wait: float, *, uniform: Callable[[float, float], float] | None = None) -> float:
    """Half the back-off fixed, half random: spreads retries without starving one."""

    import random

    draw = uniform or random.uniform
    return wait / 2 + draw(0.0, wait / 2)


def retry_admission_on_sqlite_lock(
    operation: Callable[[], T],
    *,
    deadline_seconds: float = ADMISSION_LOCK_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    jitter: Callable[[float], float] = equal_jitter,
) -> T:
    """A day-ledger admission: bounded exponential back-off with jitter."""

    return retry_on_sqlite_lock(
        operation,
        deadline_seconds=deadline_seconds,
        initial_delay=ADMISSION_LOCK_RETRY_INITIAL_DELAY,
        max_delay=ADMISSION_LOCK_RETRY_MAX_DELAY,
        sleep=sleep,
        monotonic=monotonic,
        on_retry=on_retry,
        jitter=jitter,
    )


def retry_lane_call_on_sqlite_lock(
    operation: Callable[[], T],
    *,
    label: str,
    deadline_seconds: float = LANE_LOCK_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> T:
    """Run a lane child's governed call, riding out a locked SQLite file.

    ``operation`` must be safe to repeat: every connector core replays by its
    request id -- registration and enqueue are idempotent, and once the runner
    journal holds the request a repeat takes the durable replay path instead
    of the network.  The caller therefore builds its request once, outside
    ``operation``, so every try carries the same ids.
    """

    def note(attempt: int, exc: BaseException, wait: float) -> None:
        message = (
            f"{label}: try {attempt} hit {type(exc).__name__}: {exc}; "
            f"retrying in {wait:.1f}s"
        )
        if log is not None:
            log(message)
        else:
            print(message, file=sys.stderr, flush=True)

    return retry_on_sqlite_lock(
        operation,
        deadline_seconds=deadline_seconds,
        initial_delay=LANE_LOCK_RETRY_INITIAL_DELAY,
        max_delay=LANE_LOCK_RETRY_MAX_DELAY,
        sleep=sleep,
        monotonic=monotonic,
        on_retry=note,
    )


def sqlite_lock_failure(exc: BaseException) -> dict[str, Any]:
    """Summary fields for a lane child whose SQLite lock never cleared.

    A lock is contention, not a verdict on the source: the result is marked
    retryable with a closed code, so the settle path keeps the document or
    dispatch eligible for retry instead of reading it as an unknown failure.
    """

    where = sqlite_failure_location(exc.__traceback__)
    name = where.rsplit("/", 1)[-1] if where else "a shared database"
    return {
        "failure_code": SQLITE_LOCKED_FAILURE_CODE,
        "failure_reason": (
            f"{SQLITE_LOCKED_FAILURE_CODE}: {type(exc).__name__}: {exc} "
            f"({name}); retryable"
        ),
        "failure_retryable": True,
    }


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
    "ADMISSION_BUSY_TIMEOUT_SECONDS",
    "ADMISSION_LOCK_RETRY_SECONDS",
    "equal_jitter",
    "retry_admission_on_sqlite_lock",
    "LANE_LOCK_RETRY_SECONDS",
    "LEASE_RELEASE_RETRY_SECONDS",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_BUSY_TIMEOUT_SECONDS",
    "SQLITE_LOCKED_FAILURE_CODE",
    "is_sqlite_lock_error",
    "retry_lane_call_on_sqlite_lock",
    "retry_on_sqlite_lock",
    "sqlite_failure_location",
    "sqlite_lock_failure",
    "traceback_digest",
]
