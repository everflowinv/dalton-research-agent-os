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
from typing import Any, Callable, Mapping

from .child_tickets import adopt_finished_child
from .lane_child_launcher import (
    MARKER_NAME, PROCESS_STARTED_AT, read_reconciliation_marker, ticket_identity,
    write_reconciliation_marker)
from .lane_registry import LaneSpec, register_lane
from .launch_drain import _is_zombie, _pid_alive
from .store import canonical_json

# 0.2 adds what 0.1 never wrote: the child argv, so a reused pid can be told
# from the real child, and a settled ``completed_at``/``exit_code`` pair. 1,039
# of 1,039 tickets under ``research-plans`` were stuck at ``running`` because
# this lane had its own launcher and only the *current* handle was ever polled.
TICKET_SCHEMA_VERSION = "0.2"
TICKET_PREFIX = "research-plan"
TICKETS_DIRNAME = "research-plans"
# The prefix this lane actually mints. It read ``initial-screen`` -- the
# lane this launcher was copied from -- so ``status`` rejected every ticket
# it was ever asked about, the coordinator swallowed that as "no ticket",
# and no planner ticket was ever settled.
_TICKET_RE = re.compile(rf"^{re.escape(TICKET_PREFIX)}:[0-9a-f]{{24}}$")
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


def _wire_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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
        # Every child, not just the newest: an overwritten handle is never
        # waited on, and an unwaited child stays a zombie whose pid answers
        # "alive" to every later liveness question about its ticket.
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        self.startup_reconciliation = self.reconcile_startup()

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def reconcile_startup(self) -> dict[str, Any]:
        """Settle planner tickets left ``running`` by a previous process.

        Only tickets started before this interpreter are judged. A child that
        finished and left a terminal summary is adopted from it; otherwise a
        dead or zombie pid, or a pid proved to run something else, is
        ``orphaned`` with the reason written into the ticket.
        """

        marker_path = self.tickets_dir / MARKER_NAME
        marker = read_reconciliation_marker(marker_path)
        cutoff = marker["checked_through_mtime"]
        carried = set(marker["running"])
        checked = 0
        skipped = 0
        newest = cutoff
        seen = 0
        still_running: list[str] = []
        settled: list[dict[str, Any]] = []
        for path in sorted(self.tickets_dir.glob("*/ticket.json")):
            # A settled ticket is never rewritten; one unchanged since the last
            # reconciliation and not running then cannot be running now.
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            seen += 1
            newest = max(newest, mtime)
            ticket_id = f"{TICKET_PREFIX}:{path.parent.name}"
            if mtime <= cutoff and ticket_id not in carried:
                skipped += 1
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            checked += 1
            started = _wire_moment(record.get("started_at"))
            if started is not None and started >= PROCESS_STARTED_AT:
                still_running.append(ticket_id)
                continue
            pid = record.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 \
                    and _is_zombie(pid):
                reason = "子进程已退出但未被回收（僵尸进程），票据不再代表在跑的工作"
            elif not _pid_alive(pid):
                reason = "记录的进程号已不存在：写入服务在子进程之后重启"
            elif ticket_identity(record, path) is False:
                reason = "进程号已被无关进程复用，票据与该进程不对应"
            else:
                still_running.append(ticket_id)
                continue
            now = _wire_time(self.clock())
            if adopt_finished_child(record, path.with_name("summary.json"), now=now):
                outcome = record["status"]
            else:
                record.update({"status": "orphaned", "exit_code": None,
                               "completed_at": now, "orphaned_reason": reason})
                outcome = "orphaned"
            try:
                _write_owner_only(path, record)
            except OSError:
                continue
            settled.append({"id": record.get("id"), "pid": pid,
                            "status": outcome, "reason": reason})
        if seen or marker_path.exists():
            write_reconciliation_marker(marker_path, newest, still_running,
                                        at=_wire_time(self.clock()))
        return {"lane": TICKETS_DIRNAME, "running_tickets_checked": checked,
                "unchanged_tickets_skipped": skipped,
                "still_running": len(still_running),
                "settled_count": len(settled), "settled": settled}

    def _settle_locked(self, ticket_id: str, code: int) -> None:
        path = self._ticket_path(ticket_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(record, dict) or record.get("status") != "running":
            return
        record.update({"status": "succeeded" if code == 0 else "failed",
                       "exit_code": code, "completed_at": _wire_time(self.clock())})
        try:
            _write_owner_only(path, record)
        except OSError:
            pass

    def _reap_locked(self) -> list[str]:
        reaped: list[str] = []
        for ticket_id, process in list(self._children.items()):
            code = process.poll()
            if code is None:
                continue
            self._children.pop(ticket_id, None)
            self._settle_locked(ticket_id, code)
            reaped.append(ticket_id)
        return reaped

    def reap(self) -> list[str]:
        with self._lock:
            return self._reap_locked()

    def start(self) -> dict[str, Any]:
        if not self.model_config_path.is_file():
            raise PlannerLaunchRejected("the planner model configuration is missing")
        with self._lock:
            self._reap_locked()
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
                "pid": process.pid, "command": command,
                "status": "running", "exit_code": None, "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._children[ticket_id] = process
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
            process = self._children.get(ticket_ref)
            if process is not None:
                code = process.poll()
                if code is not None:
                    self._children.pop(ticket_ref, None)
                    if record["status"] == "running":
                        record.update({
                            "status": "succeeded" if code == 0 else "failed",
                            "exit_code": code,
                            "completed_at": _wire_time(self.clock()),
                        })
                        _write_owner_only(path, record)
            elif record["status"] == "running" and (
                    not _pid_alive(record.get("pid"))
                    or ticket_identity(record, path) is False):
                # A deploy restarted the writer while this child was running.  Its
                # own summary says how it ended; without this the ticket stays
                # "running" forever and the lane never starts another one.
                now = _wire_time(self.clock())
                if adopt_finished_child(record, path.with_name("summary.json"), now=now):
                    _write_owner_only(path, record)
                else:
                    record.update({
                        "status": "orphaned", "exit_code": None, "completed_at": now,
                        "orphaned_reason": "记录的子进程已不存在或进程号被复用，"
                                           "票据不再代表在跑的工作",
                    })
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        if summary_path.is_file():
            record["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        return record

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

    def close(self) -> None:
        """Stop and reap every planner child this launcher started."""

        with self._lock:
            children = list(self._children.items())
            self._children.clear()
            self._current = None
        for _ticket_id, process in children:
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass


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

    def _signature(self) -> dict[str, Any]:
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

        from .dossier_repair_feedback import dossier_repair_feedback_signature
        from .document_research_inventory import document_inventory_signature
        from .document_research_feedback import document_research_feedback_signature

        return {
            # What is held, and what has been read out of it.
            "documents": count("SELECT COUNT(*) FROM coverage_mission_discovered_documents"),
            "document_versions": document_inventory_signature(self.store, Path(self.store.path).parent),
            "document_research_feedback": document_research_feedback_signature(self.store),
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
            # Dossier gaps are owner-only child feedback rather than Core
            # Evidence, so no SQL count can see them. Bind the exact verified
            # latest-ticket projection instead.
            "dossier_feedback": dossier_repair_feedback_signature(
                Path(self.store.path).parent),
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


# P13o: the planner lane exists only when its configuration does, which is
# only when the owner named a planner model at install time. Its model is far
# more expensive than the extraction model, so it is opt-in.
PLANNER_MODEL_CONFIG = "research-planner-model-config.json"
LAUNCHER_KWARG = "research_planner_launcher"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13o).

    One planner child at a time; the coordinator holds when the state has not
    moved, and the child itself refuses to pay twice for the same world.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no research planner lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = ResearchPlannerCoordinator(
            store=server.store, launcher=launcher,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--research-planner-model-config", type=Path, default=None,
        help="Planner model configuration written by research_planner_setup. Omit and "
             "the planner lane is absent: it decides what the research works on next, "
             "and its model is far more expensive than the extraction model.",
    )


def build_launcher(args: Any) -> Any | None:
    if args.research_planner_model_config is None:
        return None
    return ResearchPlannerLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        model_config_path=args.research_planner_model_config,
        scheduler_db=args.scheduler,
    )


def argv_fragment(context: Any) -> list[str]:
    # The planner rides on the writer's extraction wiring; without an
    # extraction model configuration the writer has no lane host to hang it on.
    if context.extraction_model_config_path is None:
        return []
    config = context.state / PLANNER_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--research-planner-model-config", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_research_plan",
    order=100,
    driver_key="research_plan",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P13o: decides what the research works on next.",
))


__all__ = [
    "IDLE_HOLD",
    "LANE",
    "LAUNCHER_KWARG",
    "PLANNER_MODEL_CONFIG",
    "ResearchPlannerCoordinator",
    "PlannerLaunchConflict",
    "PlannerLaunchError",
    "PlannerLaunchRejected",
    "ResearchPlannerLauncher",
    "TICKET_PREFIX",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
