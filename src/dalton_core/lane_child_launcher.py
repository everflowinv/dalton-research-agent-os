"""P13aj: the ticket machinery every lane child launcher was carrying its own copy of.

Four launchers already existed, each spawning one child at a time against the
writer's state directory and each with its own version of the same forty lines:
a secure ticket directory, an owner-only ticket file, one-at-a-time locking,
and a ``status`` that has to answer honestly when the writer restarted under a
running child.

That last part is why this is shared rather than copied. A child whose process
is gone and whose ticket still says ``running`` is **orphaned**, not failed and
not succeeded, and it must never be read as succeeded from a stray summary file
lying next to it -- a summary can be written by a run that then died. Getting
that wrong in one copy and right in three is exactly the failure this codebase
keeps finding, and it is worth one implementation.

What a lane still owns is its own: the ticket prefix, the directory it writes
under, and the command. Everything below is the part that was never different.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

TICKET_SCHEMA_VERSION = "0.1"


class LaneChildError(RuntimeError):
    """The lane cannot be launched as configured."""


class LaneChildRejected(ValueError):
    """The launch was refused before any process started."""


class LaneChildConflict(RuntimeError):
    """A child of this lane is already running."""


class LaneChildTicketNotFound(LookupError):
    """No ticket with that ref."""


def wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_owner_only(path: Path, value: Any) -> None:
    # The directory is made here rather than assumed. A child that refuses
    # before it has done anything else writes its refusal summary through this
    # function, and S3 found those children dying on FileNotFoundError instead:
    # the run directory is created when a ticket is started, and a child that
    # never got that far had nowhere to put the sentence explaining why.
    # Losing the explanation is worse than the refusal it explains.
    #
    # Through secure_dir, not mkdir: every other directory this launcher makes
    # is 0700, and a plain mkdir takes whatever the umask happens to be. A
    # ticket file is written 0600 into it either way, but the directory listing
    # names the company and the run, and the whole point of the file mode is
    # that this is nobody else's business.
    secure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process with that id: alive, just not ours to signal.
        return True
    return True


def process_matches(pid: Any, expected: Any) -> bool:
    """Whether a live PID still runs the exact child argv recorded at launch."""

    if not pid_alive(pid) or not isinstance(expected, list) or not expected:
        return False
    try:
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.is_file():
            actual = [part.decode() for part in proc.read_bytes().split(b"\0") if part]
        else:
            completed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                text=True, timeout=2, check=False)
            if completed.returncode != 0:
                return False
            # BSD ps renders argv joined by spaces rather than preserving the
            # original quoting. Compare that rendering to the recorded argv;
            # splitting it would incorrectly split a Python ``-c`` program.
            rendered = completed.stdout.strip()
            executable, separator, arguments = rendered.partition(" ")
            expected_executable = Path(str(expected[0])).name.lower()
            actual_executable = Path(executable).name.lower()
            executable_matches = (
                actual_executable == expected_executable
                or (actual_executable.startswith("python")
                    and expected_executable.startswith("python"))
            )
            return (executable_matches and separator
                    and arguments == " ".join(str(part) for part in expected[1:]))
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return False
    expected = [str(part) for part in expected]
    return bool(actual) and Path(actual[0]).name == Path(expected[0]).name \
        and actual[1:] == expected[1:]


class LaneChildLauncher:
    """One child at a time for one lane, with tickets a restart cannot confuse.

    Subclasses set ``TICKET_PREFIX``, ``TICKETS_DIRNAME`` and ``CHILD_MODULE``,
    and build their own argument list in ``_command``.
    """

    TICKET_PREFIX = "lane-run"
    TICKETS_DIRNAME = "lane-runs"
    CHILD_MODULE = ""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        if not self.state_dir.is_dir():
            raise LaneChildError("lane state directory is missing")
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        self.tickets_dir = secure_dir(self.state_dir / self.TICKETS_DIRNAME)
        self._ticket_re = re.compile(rf"^{re.escape(self.TICKET_PREFIX)}:[0-9a-f]{{24}}$")

    # -- subclass seam -----------------------------------------------------

    def _command(self, *, ticket_dir: Path, **kwargs: Any) -> list[str]:
        raise NotImplementedError

    # -- tickets -----------------------------------------------------------

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def spawn(self, *, digest: str, record: Mapping[str, Any],
              **command_kwargs: Any) -> dict[str, Any]:
        """Start one child and return its ticket.

        ``digest`` names the run; a caller derives it from the parameters so
        that the same request is the same ticket. ``record`` is whatever the
        lane wants kept beside the run.
        """

        if not re.fullmatch(r"[0-9a-f]{24}", digest or ""):
            raise LaneChildRejected("ticket digest must be 24 hex characters")
        ticket_id = f"{self.TICKET_PREFIX}:{digest}"
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise LaneChildConflict(f"{self.TICKET_PREFIX} child is already running")
            for path in sorted(self.tickets_dir.glob("*/ticket.json")):
                try:
                    persisted = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if persisted.get("status") != "running":
                    continue
                if process_matches(persisted.get("pid"), persisted.get("command")):
                    if persisted.get("id") == ticket_id:
                        return dict(persisted)
                    raise LaneChildConflict(
                        f"{self.TICKET_PREFIX} child is already running")
                persisted["status"] = "orphaned"
                persisted["completed_at"] = wire_time(self.clock())
                write_owner_only(path, persisted)
            ticket_dir = secure_dir(self.tickets_dir / digest)
            command = self._command(ticket_dir=ticket_dir, **command_kwargs)
            log_path = ticket_dir / "run.log"
            log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = subprocess.Popen(
                    command, cwd=str(self.state_dir), stdin=subprocess.DEVNULL,
                    stdout=log_fd, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                os.close(log_fd)
            ticket = {
                "schema_version": TICKET_SCHEMA_VERSION,
                "id": ticket_id,
                **dict(record),
                "started_at": wire_time(self.clock()),
                "pid": process.pid,
                "command": command,
                "status": "running",
                "exit_code": None,
                "completed_at": None,
            }
            write_owner_only(self._ticket_path(ticket_id), ticket)
            self._current = (ticket_id, process)
            return dict(ticket)

    def status(self, ticket_ref: str) -> dict[str, Any]:
        """The ticket, settled if the child is no longer running.

        A ticket that still says ``running`` with no live process is
        **orphaned**: the writer restarted, or the child died without updating
        it. It is not read as succeeded from a summary file lying beside it,
        because a summary can be written by a run that then died -- guessing
        success from a stray file is how a failed run becomes a fact.
        """

        if not isinstance(ticket_ref, str) or self._ticket_re.fullmatch(ticket_ref) is None:
            raise LaneChildRejected(f"ticket_ref must be {self.TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LaneChildTicketNotFound(ticket_ref) from exc
        with self._lock:
            process = None
            if self._current is not None and self._current[0] == ticket_ref:
                process = self._current[1]
            if record["status"] == "running":
                if process is not None:
                    code = process.poll()
                    if code is not None:
                        record["exit_code"] = code
                        record["completed_at"] = wire_time(self.clock())
                        record["status"] = "succeeded" if code == 0 else "failed"
                        write_owner_only(path, record)
                elif not pid_alive(record.get("pid")):
                    record["status"] = "orphaned"
                    record["completed_at"] = wire_time(self.clock())
                    write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except ValueError:
                summary = None
        return {**record, "summary": summary}

    def wait(self, timeout: float | None = None) -> int | None:
        """Test hook: wait for the current child to finish."""

        with self._lock:
            current = self._current
        if current is None:
            return None
        try:
            return current[1].wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is None or current[1].poll() is not None:
            return
        current[1].terminate()
        try:
            current[1].wait(timeout=5)
        except subprocess.TimeoutExpired:
            current[1].kill()


__all__ = [
    "LaneChildConflict",
    "LaneChildError",
    "LaneChildLauncher",
    "LaneChildRejected",
    "LaneChildTicketNotFound",
    "TICKET_SCHEMA_VERSION",
    "pid_alive",
    "process_matches",
    "secure_dir",
    "wire_time",
    "write_owner_only",
]
