"""P14e lane: turn the plan's inquiries into bounded loops, at most N a tick.

Queueless, like the lanes beside it.  There is no research-task queue because
there is nothing to queue: the plan already ranks its inquiries, the loop
authority already knows which of them were admitted, and a queue would be a
third copy of a fact two authorities already hold.  Each tick the lane reads
the ranking, admits the best inquiry that is not already a task, and stops.

Three things it refuses to do, and they are the owner's boundary rather than
an implementation detail:

* it admits nothing when the mission does not grant ``research_task`` or no
  ad-hoc ProbeTemplate has been published -- the two versioned owner acts that
  replace the hard-coded ``adhoc_research_enabled = False``;
* it admits nothing more once the day's ad-hoc pool -- 25% of the mission's
  ``max_daily_cost_usd`` -- is reserved, and says ``skipped:pool_exhausted``
  rather than borrowing from the extraction the mission actually exists for;
* it never admits an inquiry twice, because the loop carries the inquiry's
  content hash and the authority refuses a second loop for it.

Advancing a task is not this lane's job either.  ``bounded_planner_driver``
already walks every active loop once a tick, proposes, admits, executes the
probe and records the outcome; a research task is one of those loops.  What
this lane adds after admission is settlement: it reads back which tasks
finished and in what terminal state, so the controller's summary answers "what
came of the ad-hoc research" without anyone opening the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
    wire_time,
    write_owner_only,
)
from .lane_registry import LaneSpec, register_lane

# The lane exists where its own configuration does.  A research task spends the
# mission's money on questions nobody wrote down in advance, so it is opt-in at
# install time and absent everywhere the owner has not asked for it.
LANE_CONFIG = "research-task-lane.json"
LAUNCHER_KWARG = "research_task_launcher"
IDLE_HOLD = timedelta(hours=1)
# A failed or orphaned child is held the same hour rather than respawned every
# five minutes: whatever refused it will still refuse it in one minute.
FAILURE_HOLD = timedelta(hours=1)


class ResearchTaskCoordinator:
    """Admit at most N inquiries a tick, settle what finished, hold otherwise."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any | None,
        budget_db: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        # C2b: the mission day ledger, so the pool this lane admits against is
        # read net of what the planner has already spent from it today rather
        # than of reservations alone. Taken from the writer's own configuration
        # rather than guessed from a filename beside the Core: the ledger's
        # location is something the installation decided, and a lane that
        # guesses it wrong reports a full pool with no way to tell.
        self.budget_db = budget_db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- reading the world -------------------------------------------------

    def _mission(self) -> dict[str, Any] | None:
        from .coverage_mission import CoverageMissionAuthority

        pointer = self.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            return None
        return CoverageMissionAuthority(self.store).mission(
            pointer["mission_version_id"]
        )

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

    def _signature(self, plan_ref: str | None) -> dict[str, Any]:
        """What would make another admission pass worth a process.

        The plan is the input; the count of admitted tasks is the output.  When
        neither has moved, the same pass would refuse the same inquiries for
        the same reasons, and spawning a child to find that out is waste.
        """

        def count(sql: str) -> int:
            try:
                return int(self.store.connection.execute(sql).fetchone()[0])
            except Exception:  # noqa: BLE001 - an absent table means zero
                return 0

        return {
            "plan_ref": plan_ref,
            "tasks": count(
                "SELECT COUNT(*) FROM bounded_planner_loop_versions "
                "WHERE json_extract(record_json,'$.admission.source')='inquiry'"),
            "templates": count(
                "SELECT COUNT(*) FROM bounded_probe_template_versions"),
        }

    def settle(self) -> dict[str, Any]:
        """What became of the tasks already admitted."""

        from .bounded_planner_loop import INQUIRY_ADMISSION_SOURCE, BoundedPlannerAuthority

        authority = BoundedPlannerAuthority(self.store)
        running = 0
        finished: list[dict[str, Any]] = []
        for loop in authority.admitted_loops(INQUIRY_ADMISSION_SOURCE):
            terminal = authority.terminal(loop["id"])
            if terminal is None:
                running += 1
                continue
            finished.append({
                "task_ref": loop["loop_ref"],
                "terminal_state": terminal["terminal_state"],
                "rounds_used": len(authority.rounds(loop["id"])),
            })
        return {"running": running, "finished": finished}

    # -- the tick ----------------------------------------------------------

    def dispatch_once(self) -> dict[str, Any]:
        from .research_task import bindable_templates, grant, pool_state
        from .bounded_planner_loop import BoundedPlannerAuthority

        if self.launcher is None:
            return {"status": "unconfigured",
                    "reason": "no research task lane on this writer"}
        mission = self._mission()
        if mission is None:
            return {"status": "idle", "reason": "no active mission"}
        authority = BoundedPlannerAuthority(self.store)
        retired = getattr(self.launcher, "retired_templates", ())
        decision = grant(mission, bindable_templates(authority, retired=retired))
        settled = self.settle()
        if not decision["granted"]:
            # The switch, stated rather than hidden: the reasons are the two
            # owner acts that are missing.
            return {"status": "not_granted", "reasons": decision["reasons"],
                    **settled}
        day = self.clock().astimezone(timezone.utc).date().isoformat()
        state = pool_state(
            authority, mission, day=day, budget_db=self.budget_db)
        result: dict[str, Any] = {"pool": state, **settled}
        latest = self._latest()
        if latest is not None and latest.get("ticket_ref"):
            try:
                ticket = self.launcher.status(latest["ticket_ref"])
            except (LaneChildRejected, LaneChildTicketNotFound):
                ticket = None
            if ticket is not None:
                if ticket["status"] == "running":
                    return {**result, "status": "busy", "ticket_ref": ticket["id"]}
                summary = ticket.get("summary") or {}
                result["last"] = {
                    "ticket_ref": ticket["id"], "status": ticket["status"],
                    "summary_status": summary.get("status"),
                    "admitted": summary.get("admitted"),
                    "refused": summary.get("refused"),
                    "failure_reason": summary.get("failure_reason"),
                }
                if ticket["status"] in {"failed", "orphaned"}:
                    # The hold starts when the failure was seen, not when the
                    # child was launched: a run that took fifty minutes to
                    # fail would otherwise be retried ten minutes later, and a
                    # run that failed instantly would be held for an hour from
                    # a timestamp that meant something else.
                    failed_at = latest.get("failed_at")
                    if failed_at is None:
                        failed_at = wire_time(self.clock())
                        write_owner_only(self._latest_path(), {
                            **latest, "failed_at": failed_at,
                        })
                    if self._within(failed_at, FAILURE_HOLD):
                        return {**result, "status": "held",
                                "reason": "上一轮准入失败了，等一轮再重试",
                                "failed_at": failed_at}
        from .coverage_mission import CoverageMissionAuthority

        plan = CoverageMissionAuthority(self.store).latest_research_plan(mission["id"])
        if plan is None:
            return {**result, "status": "idle", "reason": "no research plan yet"}
        if state["remaining_micros"] <= 0:
            # C2 will name three more pools; this is the first, and the word
            # the cockpit reads is the one C2 generalises.
            return {**result, "status": "skipped:pool_exhausted",
                    "reason": "今天的专项研究预算已经用完"}
        signature = self._signature(plan["plan_id"])
        if (
            latest is not None
            and latest.get("idle_signature") == signature
            and self._within(latest.get("idle_at"), IDLE_HOLD)
        ):
            return {**result, "status": "held", "signature": signature,
                    "reason": "计划和已派发的专项研究都没有变化"}
        try:
            ticket = self.launcher.start(
                plan_ref=plan["plan_id"], signature=str(signature["tasks"]),
            )
        except LaneChildConflict as exc:
            return {**result, "status": "busy", "reason": str(exc)}
        except LaneChildRejected as exc:
            return {**result, "status": "unconfigured", "reason": str(exc)}
        write_owner_only(self._latest_path(), {
            "ticket_ref": ticket["id"], "started_at": ticket["started_at"],
            "idle_signature": signature, "idle_at": wire_time(self.clock()),
        })
        return {**result, "status": "launched", "ticket_ref": ticket["id"]}

    def _within(self, moment: Any, window: timedelta) -> bool:
        """Whether ``moment`` is recent enough that the hold still stands."""

        if not moment:
            return False
        try:
            since = datetime.fromisoformat(moment)
        except (TypeError, ValueError):
            return False
        return self.clock() - since < window


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14e): admit the plan's inquiries as bounded loops."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no research task lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = ResearchTaskCoordinator(
            store=server.store, launcher=launcher,
            budget_db=(server._planner_model_config or {}).get("budget_db"),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--research-task-lane", type=Path, default=None,
        help="Ad-hoc research lane configuration. Omit and the lane is absent: "
             "a research task spends the mission's budget on a question the "
             "checklist did not anticipate, so it is opt-in.",
    )


def lane_configuration(path: Path) -> dict[str, Any]:
    """This deployment's two knobs, defaulted rather than demanded.

    ``retired_templates`` is the revocation lever the Core cannot offer: a
    probe template version is append-only and has no status column, so an owner
    who wants an admitted template to stop being bound tonight names it here.
    """

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    requested = value.get("max_admissions_per_tick", 1)
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        requested = 1
    retired = value.get("retired_templates") or []
    if not isinstance(retired, list):
        retired = []
    return {
        "max_admissions_per_tick": min(requested, 3),
        "retired_templates": tuple(
            item for item in retired if isinstance(item, str) and item
        ),
    }


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "research_task_lane", None) is None:
        return None
    from .research_task_launcher import ResearchTaskLauncher

    configuration = lane_configuration(args.research_task_lane)
    return ResearchTaskLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        max_admissions_per_tick=configuration["max_admissions_per_tick"],
        retired_templates=configuration["retired_templates"],
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / LANE_CONFIG
    if not config.is_file():
        return []
    return ["--research-task-lane", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_research_task",
    order=150,
    driver_key="research_task",
    # The pool is not declared here: C2's LANE_POOLS already names this lane's
    # as ``adhoc``, and its own test pins that no registered lane overrides the
    # table.  Declaring it would be a second answer to a question that has one.
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P14e: a planner inquiry becomes a bounded loop with its own budget; "
         "the bounded planner driver advances it like any other loop.",
))


__all__ = [
    "FAILURE_HOLD",
    "IDLE_HOLD",
    "LANE",
    "LANE_CONFIG",
    "LAUNCHER_KWARG",
    "ResearchTaskCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "lane_configuration",
]
