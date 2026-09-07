"""Wait for in-flight lane children before the writer is stopped (P9d-11).

Every mission lane runs its work out of process: a search, a fetch, an
AlphaEngine acquisition or a SEC lane run is a child the writer spawned and
tracks through an owner-only ``ticket.json``.  When the writer stops, each
launcher's ``close()`` terminates its running child, and the next tick settles
that ticket as ``orphaned``.  The lane then parks the affected company/spec
pair for its retry interval, which is a day.  Two deploys on 2026-09-07 burned
two slots exactly that way.

So the fix is to not stop the writer while a child is running.  ``install.sh``
stops the controller (which launches a child every tick), calls this module,
and only then stops the writer; it polls the ticket directories and returns
once no ticket is both ``running`` and backed by a live pid, or once the
timeout passes, in which case it says so and the deploy proceeds.  A child
that has exited but not yet been reaped by the writer (a zombie) counts as
exited: the writer reaps only on its next tick, and ``kill(pid, 0)`` succeeds
on a zombie, which is what made the first three drains wait their full
timeout.  Work that finishes in the settlement gap is recovered by the
launchers themselves; see ``child_tickets``.

This module only reads tickets.  It never writes them, never signals a child
and never touches Core.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# One directory per launcher; see each launcher's ``tickets_dir``.
TICKET_DIRECTORIES: tuple[str, ...] = ("acquisitions", "discoveries", "extractions", "fetches", "sec-lane-runs")
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_POLL_SECONDS = 2.0


def _is_zombie(pid: int) -> bool:
    """True when the process has exited but its parent has not reaped it.

    The writer reaps a lane child only when it next polls the handle, on its
    next tick, so for up to five minutes an exited child is a zombie.
    ``kill(pid, 0)`` still succeeds on one, which made the drain wait its full
    timeout three deploys running.  On Linux ``/proc`` says so directly; on
    macOS ``ps`` is the only portable witness.
    """

    proc_stat = Path("/proc") / str(pid) / "stat"
    if proc_stat.exists():
        try:
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return bool(fields) and fields[0] == "Z"
        except (OSError, IndexError, ValueError):
            return False
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out.startswith("Z")


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


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
