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
timeout passes, in which case deployment stops before replacing the runtime.  A child
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
from datetime import datetime
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


def _process_command_matches(pid: int, expected: list[Any]) -> bool | None:
    """Compare a live process to recorded argv; None means unavailable."""

    try:
        proc = Path("/proc") / str(pid) / "cmdline"
        if proc.is_file():
            raw = proc.read_bytes()
            if not raw:
                return None
            actual = [part.decode() for part in raw.split(b"\0") if part]
            wanted = [str(part) for part in expected]
            if not actual:
                return None
            actual_name = Path(actual[0]).name.lower()
            wanted_name = Path(wanted[0]).name.lower()
            executable_matches = (
                actual_name == wanted_name
                or (actual_name.startswith("python") and wanted_name.startswith("python"))
            )
            return executable_matches and actual[1:] == wanted[1:]
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        rendered = completed.stdout.strip()
        wanted_rendered = " ".join(str(part) for part in expected)
        if rendered == wanted_rendered:
            return True
        # BSD ps loses argv boundaries and quoting. When the executable or an
        # argument contains spaces, a non-equal rendering is ambiguous and
        # cannot prove PID reuse.
        if any(" " in str(part) for part in expected):
            return None
        executable, separator, arguments = rendered.partition(" ")
        wanted_executable = Path(str(expected[0])).name.lower()
        actual_executable = Path(executable).name.lower()
        executable_matches = (
            actual_executable == wanted_executable
            or (actual_executable.startswith("python")
                and wanted_executable.startswith("python"))
        )
        if not executable_matches:
            return False
        return bool(separator and arguments == " ".join(str(part) for part in expected[1:]))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return None


def _process_started_at(pid: int) -> float | None:
    """Best-effort process start timestamp on Linux and macOS."""

    try:
        proc_stat = Path("/proc") / str(pid) / "stat"
        if proc_stat.is_file():
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            start_ticks = int(fields[19])
            boot_line = next(
                line for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
                if line.startswith("btime ")
            )
            return float(boot_line.split()[1]) + start_ticks / float(os.sysconf("SC_CLK_TCK"))
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        parsed = time.strptime(completed.stdout.strip(), "%a %b %d %H:%M:%S %Y")
        return time.mktime(parsed)
    except (OSError, StopIteration, ValueError, subprocess.SubprocessError):
        return None


def _ticket_process_matches(record: dict[str, Any]) -> bool | None:
    """True/False for proved identity; None when the OS cannot prove it."""

    pid = record.get("pid")
    command = record.get("command")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    command_match: bool | None = None
    if isinstance(command, list) and command:
        command_match = _process_command_matches(pid, command)
        if command_match is False:
            return False
    started = record.get("started_at")
    if not isinstance(started, str):
        return command_match
    try:
        parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return command_match
        ticket_time = parsed.timestamp()
    except ValueError:
        return command_match
    process_time = _process_started_at(pid)
    if process_time is None:
        return command_match
    # The ticket is written immediately after Popen. Whole-second macOS ps
    # truncates the birth time, so allow the process to precede the ticket by
    # three seconds. A process born after the ticket is definitively a reused
    # PID even when argv is identical.
    return ticket_time - 3.0 <= process_time <= ticket_time


def running_tickets(state_dir: str | Path) -> list[dict[str, Any]]:
    """Tickets that say ``running`` and whose pid is still alive."""

    root = Path(state_dir)
    found: list[dict[str, Any]] = []
    # All launchers use state/<lane>/<ticket>/ticket.json. Discover that
    # bounded shape so a new lane cannot silently escape the deploy drain.
    # No recursive scan into raw spool, archives or backups is needed.
    directories = sorted({path.parent.parent for path in root.glob("*/*/ticket.json")})
    for directory in directories:
        name = directory.name
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
            identity = _ticket_process_matches(record)
            if identity is False:
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
