"""P10c lane: one Initial Screen drafting child per tick, at most.

Same shape as the fetch and extraction lanes: a single slot, owner-only
tickets on disk, a summary the controller can read back after a restart, and
a hold when there is nothing new to write, so a finished queue does not spawn
a process every five minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .child_tickets import adopt_finished_child
from .launch_drain import _pid_alive
from .store import canonical_json

TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "initial-screen"
TICKETS_DIRNAME = "initial-screens"
_TICKET_RE = re.compile(r"^initial-screen:[0-9a-f]{24}$")
IDLE_HOLD = timedelta(hours=1)


class InitialScreenLaunchError(RuntimeError):
    """The lane could not start."""


class InitialScreenLaunchRejected(ValueError):
    """The request was refused before anything ran."""


class InitialScreenLaunchConflict(RuntimeError):
    """A child is already running in this slot."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class InitialScreenLauncher:
    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path,
        scheduler_db: str | Path | None = None,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.model_config_path = Path(model_config_path).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db).expanduser().resolve()
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tickets_dir = _secure_dir(self.state_dir / TICKETS_DIRNAME)
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def start(self) -> dict[str, Any]:
        if not self.model_config_path.is_file():
            raise InitialScreenLaunchRejected("the drafting model configuration is missing")
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise InitialScreenLaunchConflict(f"initial screen {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(canonical_json({
                "started_at": started_at, "model_config": str(self.model_config_path),
            }).encode("utf-8")).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = [
                self.python_executable, "-m", "dalton_core.initial_screen_cli",
                "--state-dir", str(self.state_dir),
                "--model-config", str(self.model_config_path),
                "--summary-dir", str(ticket_dir),
                "--quiet",
            ]
            if self.scheduler_db is not None:
                command += ["--scheduler-db", str(self.scheduler_db)]
            log_fd = os.open(str(ticket_dir / "run.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = subprocess.Popen(
                    command, cwd=str(self.state_dir), stdin=subprocess.DEVNULL,
                    stdout=log_fd, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                os.close(log_fd)
            record = {
                "schema_version": TICKET_SCHEMA_VERSION, "id": ticket_id,
                "model_config_path": str(self.model_config_path), "started_at": started_at,
                "pid": process.pid, "status": "running", "exit_code": None, "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            return dict(record)

    def status(self, ticket_ref: str) -> dict[str, Any]:
        if not isinstance(ticket_ref, str) or _TICKET_RE.fullmatch(ticket_ref) is None:
            raise InitialScreenLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        if not path.is_file():
            raise InitialScreenLaunchRejected("initial screen ticket was not found")
        record = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            current = self._current
        if current is not None and current[0] == ticket_ref:
            code = current[1].poll()
            if code is not None and record["status"] == "running":
                record.update({
                    "status": "succeeded" if code == 0 else "failed", "exit_code": code,
                    "completed_at": _wire_time(self.clock()),
                })
                _write_owner_only(path, record)
        elif record["status"] == "running" and not _pid_alive(record.get("pid")):
            # A deploy restarted the writer while this child was running.  Its
            # own summary says how it ended; without this the ticket stays
            # "running" forever and the lane never starts another one.
            now = _wire_time(self.clock())
            if adopt_finished_child(record, path.with_name("summary.json"), now=now):
                _write_owner_only(path, record)
            else:
                record.update({"status": "orphaned", "exit_code": None, "completed_at": now})
                _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        if summary_path.is_file():
            record["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        return record

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

    def close(self) -> None:
        with self._lock:
            current = self._current
        if current is not None and current[1].poll() is None:
            current[1].terminate()


class InitialScreenCoordinator:
    """Launch at most one drafting child per tick, and hold when nothing changed."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: InitialScreenLauncher | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _latest_path(self) -> Path:
        return self.launcher.tickets_dir / "latest.json"

    def _latest(self) -> dict[str, Any] | None:
        if self.launcher is None:
            return None
        path = self._latest_path()
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _signature(self) -> dict[str, int]:
        """What would make a new draft worth running: more Claims, or a new stage."""

        def count(sql: str) -> int:
            try:
                return int(self.store.connection.execute(sql).fetchone()[0])
            except Exception:  # noqa: BLE001 - an absent table means zero
                return 0

        return {
            "claims": count("SELECT COUNT(*) FROM claim_versions"),
            "retirements": count(
                "SELECT COUNT(*) FROM claim_retirement_decisions WHERE decision='retired'"),
            "stages": count("SELECT COUNT(*) FROM coverage_mission_stage_records"),
            "deliverables": count("SELECT COUNT(*) FROM mission_deliverable_versions"),
        }

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "no initial screen launcher on this writer"}
        latest = self._latest()
        result: dict[str, Any] = {}
        if latest is not None and latest.get("ticket_ref"):
            try:
                ticket = self.launcher.status(latest["ticket_ref"])
            except InitialScreenLaunchRejected:
                ticket = None
            if ticket is not None:
                if ticket["status"] == "running":
                    return {"status": "busy", "ticket_ref": ticket["id"]}
                result["last"] = {
                    "ticket_ref": ticket["id"], "status": ticket["status"],
                    "summary_status": (ticket.get("summary") or {}).get("status"),
                    "drafted": (ticket.get("summary") or {}).get("drafted"),
                    "gate": ((ticket.get("summary") or {}).get("gate") or {}).get("passed"),
                    "failure_reason": (ticket.get("summary") or {}).get("failure_reason"),
                }
        signature = self._signature()
        if latest is not None and latest.get("idle_signature") == signature:
            held_since = latest.get("idle_at")
            if held_since:
                try:
                    moment = datetime.fromisoformat(held_since)
                except ValueError:
                    moment = None
                if moment is not None and self.clock() - moment < IDLE_HOLD:
                    # P13aa: "nothing to write" and "the last attempt broke"
                    # are different facts and were reported as the same one.
                    # Live, eight sections had been failing on a scheduler
                    # conflict for two days while this said the ledger simply
                    # had not moved -- which sent the reader looking at the
                    # ledger, where nothing was wrong. The hold is right either
                    # way (an hour, then it retries); the reason has to say
                    # which case it is.
                    failed = (result.get("last") or {}).get("status") == "failed"
                    return {**result, "status": "held", "signature": signature,
                            "reason": ("上一轮跑失败了，等一轮再重试"
                                       if failed else
                                       "上一轮没有可写的内容，账本也没有变化")}
        try:
            ticket = self.launcher.start()
        except InitialScreenLaunchConflict as exc:
            return {**result, "status": "busy", "reason": str(exc)}
        except InitialScreenLaunchRejected as exc:
            return {**result, "status": "unconfigured", "reason": str(exc)}
        _write_owner_only(self._latest_path(), {
            "ticket_ref": ticket["id"], "started_at": ticket["started_at"],
            "idle_signature": signature, "idle_at": _wire_time(self.clock()),
        })
        return {**result, "status": "launched", "ticket_ref": ticket["id"]}


__all__ = [
    "IDLE_HOLD",
    "InitialScreenCoordinator",
    "InitialScreenLaunchConflict",
    "InitialScreenLaunchError",
    "InitialScreenLaunchRejected",
    "InitialScreenLauncher",
    "TICKET_PREFIX",
]
