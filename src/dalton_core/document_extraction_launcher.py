"""Launch and settle the document extraction child (P9d-17a, ADR-0005).

Same shape as the fetch and search launchers: a single slot, owner-only
tickets under ``<state>/extractions/<ticket>/`` (``ticket.json``,
``summary.json``, ``run.log``), a finished child's own summary adopted after a
writer restart, and a coordinator the controller tick drives.

The coordinator launches one child per tick while any review is awaiting
extraction, except that a child which found nothing to draft holds the lane
until the awaiting count changes or an hour passes, so a fully drafted queue
does not spawn a process every five minutes.
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
from typing import Any, Callable, Sequence

from .child_tickets import adopt_finished_child
from .coverage_mission import CoverageMissionAuthority
from .store import canonical_json

TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "document-extraction"
DEFAULT_MAX_WINDOWS_PER_TICK = 2
IDLE_HOLD = timedelta(hours=1)
_TICKET_RE = re.compile(r"document-extraction:[0-9a-f]{24}\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")


class ExtractionLaunchError(RuntimeError):
    pass


class ExtractionLaunchRejected(ValueError):
    pass


class ExtractionLaunchConflict(RuntimeError):
    pass


class ExtractionTicketNotFound(LookupError):
    pass


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class DocumentExtractionLauncher:
    def __init__(
        self,
        *,
        state_dir: str | Path,
        model_config_path: str | Path,
        spool_dir: str | Path | None = None,
        scheduler_db: str | Path | None = None,
        connector_governance: str | Path | None = None,
        web_fetch_governance: str | Path | None = None,
        mode_args: Sequence[str] = (),
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.model_config_path = Path(model_config_path).expanduser().resolve()
        self.spool_dir = None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        self.scheduler_db = None if scheduler_db is None else Path(scheduler_db).expanduser().resolve()
        self.connector_governance = None if connector_governance is None else Path(connector_governance).expanduser().resolve()
        self.web_fetch_governance = None if web_fetch_governance is None else Path(web_fetch_governance).expanduser().resolve()
        self.mode_args = tuple(mode_args)
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tickets_dir = _secure_dir(self.state_dir / "extractions")
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(self, *, requested_by: str | None, max_windows: int, ticket_dir: Path) -> list[str]:
        command = [
            self.python_executable, "-m", "dalton_core.document_extraction_cli",
            "--state-dir", str(self.state_dir),
            "--model-config", str(self.model_config_path),
            "--summary-dir", str(ticket_dir),
            "--max-windows", str(max_windows),
            "--quiet",
        ]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        if self.connector_governance is not None:
            command += ["--connector-governance", str(self.connector_governance)]
        if self.web_fetch_governance is not None:
            command += ["--web-fetch-governance", str(self.web_fetch_governance)]
        if requested_by is not None:
            command += ["--requested-by", requested_by]
        command += list(self.mode_args)
        return command

    def start(self, *, requested_by: str | None = None, max_windows: int = DEFAULT_MAX_WINDOWS_PER_TICK) -> dict[str, Any]:
        if requested_by is not None and _HUMAN_RE.fullmatch(requested_by) is None \
                and _AUTOMATION_RE.fullmatch(requested_by) is None:
            raise ExtractionLaunchRejected("requested_by must be a human: or automation: principal")
        if not isinstance(max_windows, int) or isinstance(max_windows, bool) or not 1 <= max_windows <= 50:
            raise ExtractionLaunchRejected("max_windows must be 1..50")
        if not self.model_config_path.is_file():
            raise ExtractionLaunchRejected("document extraction model configuration is missing")
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise ExtractionLaunchConflict(f"extraction {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(canonical_json({
                "requested_by": requested_by, "max_windows": max_windows, "started_at": started_at,
                "model_config": str(self.model_config_path),
            }).encode("utf-8")).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(requested_by=requested_by, max_windows=max_windows, ticket_dir=ticket_dir)
            log_fd = os.open(str(ticket_dir / "run.log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = subprocess.Popen(
                    command, cwd=str(self.state_dir), stdin=subprocess.DEVNULL,
                    stdout=log_fd, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                os.close(log_fd)
            record = {
                "schema_version": TICKET_SCHEMA_VERSION, "id": ticket_id,
                "requested_by": requested_by, "max_windows": max_windows,
                "model_config_path": str(self.model_config_path),
                "started_at": started_at, "pid": process.pid,
                "status": "running", "exit_code": None, "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            return dict(record)

    def status(self, ticket_ref: str) -> dict[str, Any]:
        if not isinstance(ticket_ref, str) or _TICKET_RE.fullmatch(ticket_ref) is None:
            raise ExtractionLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ExtractionTicketNotFound(ticket_ref) from exc
        with self._lock:
            process = None
            if self._current is not None and self._current[0] == ticket_ref:
                process = self._current[1]
            if record["status"] == "running":
                if process is not None:
                    code = process.poll()
                    if code is not None:
                        record["exit_code"] = code
                        record["completed_at"] = _wire_time(self.clock())
                        record["status"] = "succeeded" if code == 0 else "failed"
                        _write_owner_only(path, record)
                elif not self._pid_alive(record.get("pid")):
                    now = _wire_time(self.clock())
                    if not adopt_finished_child(record, path.with_name("summary.json"), now=now):
                        record["status"] = "orphaned"
                        record["completed_at"] = now
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**record, "summary": summary}

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        with self._lock:
            current = self._current
        if current is None:
            return None
        return current[1].wait(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is not None and current[1].poll() is None:
            current[1].terminate()
            try:
                current[1].wait(timeout=5)
            except subprocess.TimeoutExpired:
                current[1].kill()


class DocumentExtractionCoordinator:
    """Drive the extraction child from the controller tick."""

    def __init__(
        self,
        *,
        missions: CoverageMissionAuthority,
        launcher: Any,
        max_windows_per_tick: int = DEFAULT_MAX_WINDOWS_PER_TICK,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.missions = missions
        self.launcher = launcher
        self.max_windows_per_tick = int(max_windows_per_tick)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._latest_path = Path(launcher.tickets_dir) / "latest.json"

    def _latest(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._latest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _awaiting(self) -> int:
        return int(self.missions.connection.execute(
            "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'"
        ).fetchone()[0])

    def dispatch_once(self) -> dict[str, Any]:
        latest = self._latest()
        result: dict[str, Any] = {"awaiting": self._awaiting()}
        if latest is not None:
            try:
                ticket = self.launcher.status(latest["ticket"])
            except LookupError:
                ticket = None
            if ticket is not None:
                if ticket["status"] == "running":
                    return {**result, "status": "busy", "ticket_ref": ticket["id"]}
                summary = ticket.get("summary") or {}
                result["last"] = {
                    "ticket_ref": ticket["id"], "status": ticket["status"],
                    "drafted": len(summary.get("drafted", [])), "stop_reason": summary.get("stop_reason"),
                    "failure_reason": summary.get("failure_reason"),
                    "reviews_complete": summary.get("reviews_complete"),
                }
                if latest.get("settled") is not True:
                    latest = {**latest, "settled": True, "status": ticket["status"],
                              "drafted": result["last"]["drafted"], "stop_reason": summary.get("stop_reason"),
                              "completed_at": ticket.get("completed_at"), "awaiting_at_launch": latest.get("awaiting_at_launch")}
                    _write_owner_only(self._latest_path, latest)
        if result["awaiting"] == 0:
            return {**result, "status": "idle"}
        if latest is not None and latest.get("settled") and latest.get("stop_reason") in ("nothing_to_draft",) \
                and latest.get("awaiting_at_launch") == result["awaiting"]:
            completed = latest.get("completed_at")
            if completed and self.clock() - datetime.fromisoformat(completed) < IDLE_HOLD:
                return {**result, "status": "held", "reason": "nothing to draft since the last run; queue unchanged"}
        if latest is not None and latest.get("settled") and str(latest.get("stop_reason", "")).startswith("gated"):
            completed = latest.get("completed_at")
            if completed and self.clock() - datetime.fromisoformat(completed) < IDLE_HOLD:
                return {**result, "status": "held", "reason": latest["stop_reason"]}
        try:
            ticket = self.launcher.start(max_windows=self.max_windows_per_tick)
        except Exception as exc:
            name = type(exc).__name__
            return {**result, "status": "busy" if name.endswith("Conflict") else "rejected", "reason": f"{name}: {exc}"}
        _write_owner_only(self._latest_path, {"ticket": ticket["id"], "settled": False,
                                              "awaiting_at_launch": result["awaiting"]})
        return {**result, "status": "launched", "ticket_ref": ticket["id"], "max_windows": self.max_windows_per_tick}


__all__ = [
    "DEFAULT_MAX_WINDOWS_PER_TICK",
    "DocumentExtractionCoordinator",
    "DocumentExtractionLauncher",
    "ExtractionLaunchConflict",
    "ExtractionLaunchError",
    "ExtractionLaunchRejected",
    "ExtractionTicketNotFound",
    "TICKET_PREFIX",
]
