"""Wait for in-flight lane children before the writer is stopped (P9d-11).

Every mission lane runs its work out of process: a search, a fetch, an
AlphaEngine acquisition or a SEC lane run is a child the writer spawned and
tracks through an owner-only ``ticket.json``.  When the writer stops, each
launcher's ``close()`` terminates its running child, and the next tick settles
that ticket as ``orphaned``.  The lane then parks the affected company/spec
pair for its retry interval, which is a day.  Two deploys on 2026-09-07 burned
two slots exactly that way.

The launchers keep their semantics on purpose: a dead pid is never promoted to
success from a summary file on disk.  So the fix is to not stop the writer
while a child is running.  ``install.sh`` calls this module after the new wheel
is installed and before ``launchctl bootout``; it polls the ticket directories
and returns once no ticket is both ``running`` and backed by a live pid, or
once the timeout passes, in which case it says so and the deploy proceeds.

This module only reads tickets.  It never writes them, never signals a child
and never touches Core.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# One directory per launcher; see each launcher's ``tickets_dir``.
TICKET_DIRECTORIES: tuple[str, ...] = ("acquisitions", "discoveries", "fetches", "sec-lane-runs")
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_POLL_SECONDS = 2.0


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_tickets(state_dir: str | Path) -> list[dict[str, Any]]:
    """Tickets that say ``running`` and whose pid is still alive."""

    root = Path(state_dir)
    found: list[dict[str, Any]] = []
    for name in TICKET_DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            continue
        for ticket_path in sorted(directory.glob("*/ticket.json")):
            try:
                record = json.loads(ticket_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # An unreadable ticket is not evidence of a running child.
                continue
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            if not _pid_alive(record.get("pid")):
                continue
            found.append({
                "lane": name,
                "ticket": str(record.get("id")),
                "pid": record.get("pid"),
                "started_at": record.get("started_at"),
            })
    return found


def drain(
    state_dir: str | Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Poll until no child is running or the timeout passes.

    Returns a closed summary: ``drained`` is True when nothing was running at
    the end; ``remaining`` lists what still was.
    """

    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError("timeout must be >= 0 and poll interval > 0")
    started = clock()
    polls = 0
    while True:
        remaining = running_tickets(state_dir)
        polls += 1
        waited = clock() - started
        if not remaining:
            return {"drained": True, "waited_seconds": round(waited, 3), "polls": polls, "remaining": []}
        if waited >= timeout_seconds:
            return {"drained": False, "waited_seconds": round(waited, 3), "polls": polls, "remaining": remaining}
        sleep(min(poll_seconds, max(0.0, timeout_seconds - waited)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help="seconds to wait before giving up (default 600)")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    result = drain(args.state_dir, timeout_seconds=args.timeout, poll_seconds=args.poll)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["drained"] else 1


if __name__ == "__main__":
    sys.exit(main())
