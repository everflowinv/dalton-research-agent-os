"""Put an ISO-8601 UTC timestamp at the start of every stderr line of a daemon.

launchd sends each daemon's fd 2 straight to ``~/Library/Logs/Dalton/*.stderr.log``.
Nothing on that path added a time, so a 16 MB ``writer.stderr.log`` could not
answer "how many of these happened today" or "did this stop after the deploy".
The 2026-09-30 review could only place a deploy by counting line numbers.

The writer, the controller and the control server all write to stderr the same
way: ``print(..., file=sys.stderr)``, ``traceback.print_exc()``, the http.server
``log_message`` / ``handle_error`` hooks and ``warnings``.  All of these go
through ``sys.stderr``, so one wrapper around that object covers all three
processes without changing any call site.

What it cannot cover is bytes written to fd 2 underneath Python: a C extension,
or a child process that inherits the descriptor.  Dalton's lane children write
their own ``run.log`` under the ticket directory, so in practice this is the
interpreter's own crash output, which is rare and already carries a traceback.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, TextIO


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TimestampedStream(io.TextIOBase):
    """A text stream that prefixes ``<ISO time> `` to every line it forwards.

    A line is stamped when its first character is written, not when it is
    finished, so a partial line followed later by its end still carries the
    time it began.  Writes are serialised with a lock because the writer
    serves connections from many threads and a stamp must not land in the
    middle of another thread's line.
    """

    def __init__(self, target: TextIO, *, clock: Callable[[], str] = iso_now) -> None:
        super().__init__()
        self._target = target
        self._clock = clock
        self._lock = threading.Lock()
        self._at_line_start = True

    @property
    def target(self) -> TextIO:
        return self._target

    # -- the TextIO surface callers actually use -------------------------------

    def write(self, text: str) -> int:  # type: ignore[override]
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        with self._lock:
            pieces: list[str] = []
            for line in text.splitlines(keepends=True):
                if self._at_line_start:
                    pieces.append(f"{self._clock()} ")
                pieces.append(line)
                self._at_line_start = line.endswith(("\n", "\r"))
            self._target.write("".join(pieces))
        return len(text)

    def flush(self) -> None:
        with contextlib.suppress(ValueError):
            self._target.flush()

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return bool(getattr(self._target, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._target.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._target, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._target, "errors", None)

    def __getattr__(self, name: str) -> Any:
        # ``buffer``, ``line_buffering`` and friends: behave like the target.
        return getattr(self._target, name)


@contextlib.contextmanager
def timestamped_stderr(*, clock: Callable[[], str] = iso_now) -> Iterator[TextIO]:
    """Stamp ``sys.stderr`` for the duration of a daemon's ``main``.

    Idempotent: if stderr is already stamped (a daemon ``main`` that calls
    another one), it is left alone.  The previous stream is restored on exit,
    so a test that calls ``main`` in-process does not leave the wrapper behind.
    """

    current = sys.stderr
    if isinstance(current, TimestampedStream) or current is None:
        yield current
        return
    stamped = TimestampedStream(current, clock=clock)
    sys.stderr = stamped
    try:
        yield stamped
    finally:
        stamped.flush()
        if sys.stderr is stamped:
            sys.stderr = current


__all__ = ["TimestampedStream", "iso_now", "timestamped_stderr"]
