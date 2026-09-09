"""P13o lane: one planner child per tick, at most.

Same shape as the fetch, extraction and Initial Screen lanes -- a single slot,
owner-only tickets on disk, a summary the controller reads back after a
restart -- because a planner call is budgeted at 300 seconds and the writer
abandons a request after 30.

The hold is different from the other lanes', and the difference matters. The
child already refuses to pay twice for the same world: it hashes the research
state and replays an existing plan for that exact hash. So the question here
is not "is there work" but "is it worth spawning a process to find out", and
the answer is a cheap count of the tables the state is derived from. When
those have not moved, neither has the state, and the process is not spawned.
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
TICKET_PREFIX = "research-plan"
TICKETS_DIRNAME = "research-plans"
_TICKET_RE = re.compile(r"^initial-screen:[0-9a-f]{24}$")
IDLE_HOLD = timedelta(hours=1)


class PlannerLaunchError(RuntimeError):
    """The lane could not start."""


class PlannerLaunchRejected(ValueError):
    """The request was refused before anything ran."""


class PlannerLaunchConflict(RuntimeError):
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


class ResearchPlannerLauncher:
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
            raise PlannerLaunchRejected("the planner model configuration is missing")
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise PlannerLaunchConflict(f"research plan {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(canonical_json({
                "started_at": started_at, "model_config": str(self.model_config_path),
            }).encode("utf-8")).hexdigest()[:24]
            ticket_id = f"{TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = [
                self.python_executable, "-m", "dalton_core.research_planner_cli",
                "--state-dir", str(self.state_dir),
                "--model-config", str(self.model_config_path),
                "--summary-dir", str(ticket_dir),
                # The plans directory tells the state which specs are actually
                # planned, so an item with no route reads as blocked rather
                # than merely empty.
                "--discovery-plans", str(self.state_dir / "discovery-plans"),
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
            raise PlannerLaunchRejected(f"ticket_ref must be {TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        if not path.is_file():
            raise PlannerLaunchRejected("research plan ticket was not found")
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


class ResearchPlannerCoordinator:
    """Launch at most one drafting child per tick, and hold when nothing changed."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: ResearchPlannerLauncher | None,
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
        """A cheap proxy for "has the research state moved".

        Not the state itself: building that reads the whole checklist, and the
        point of this is to decide whether spawning a process is worth it. Each
        count is a table the state is derived from, so a change in any of them
        can change the plan and a change in none of them cannot.
        """

        def count(sql: str) -> int:
            try:
                return int(self.store.connection.execute(sql).fetchone()[0])
            except Exception:  # noqa: BLE001 - an absent table means zero
                return 0

        return {
            # What is held, and what has been read out of it.
            "documents": count("SELECT COUNT(*) FROM coverage_mission_discovered_documents"),
            "reviews_open": count(
                "SELECT COUNT(*) FROM coverage_mission_document_reviews "
                "WHERE state='awaiting_human_extraction'"),
            "claims": count("SELECT COUNT(*) FROM claim_versions"),
            "figures": count(
                "SELECT COUNT(*) FROM coverage_mission_document_figures f "
                "LEFT JOIN coverage_mission_document_figure_retractions r "
                "ON r.figure_id=f.figure_id WHERE r.figure_id IS NULL"),
            # Retracted the same way figures are: withdrawing 170 wrongly
            # attributed observations changes what the planner should decide,
            # so it has to change the signature that decides whether to ask.
            "metrics": count(
                "SELECT COUNT(*) FROM coverage_mission_metric_observations o "
                "LEFT JOIN coverage_mission_metric_observation_retractions r "
                "ON r.observation_id=o.observation_id WHERE r.observation_id IS NULL"),
            # A new mission version is a new goal, which is always worth a plan.
            "mission_versions": count("SELECT COUNT(*) FROM coverage_mission_versions"),
        }

    def dispatch_once(self) -> dict[str, Any]:
        if self.launcher is None:
            return {"status": "unconfigured", "reason": "no research plan launcher on this writer"}
        latest = self._latest()
        result: dict[str, Any] = {}
        if latest is not None and latest.get("ticket_ref"):
            try:
                ticket = self.launcher.status(latest["ticket_ref"])
            except PlannerLaunchRejected:
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
                    return {**result, "status": "held",
                            "reason": "研究状态没有变化，没有必要重新决策",
                            "signature": signature}
        try:
            ticket = self.launcher.start()
        except PlannerLaunchConflict as exc:
            return {**result, "status": "busy", "reason": str(exc)}
        except PlannerLaunchRejected as exc:
            return {**result, "status": "unconfigured", "reason": str(exc)}
        _write_owner_only(self._latest_path(), {
            "ticket_ref": ticket["id"], "started_at": ticket["started_at"],
            "idle_signature": signature, "idle_at": _wire_time(self.clock()),
        })
        return {**result, "status": "launched", "ticket_ref": ticket["id"]}


__all__ = [
    "IDLE_HOLD",
    "ResearchPlannerCoordinator",
    "PlannerLaunchConflict",
    "PlannerLaunchError",
    "PlannerLaunchRejected",
    "ResearchPlannerLauncher",
    "TICKET_PREFIX",
]
