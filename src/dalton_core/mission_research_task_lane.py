"""P14e lane: turn the plan's inquiries into bounded loops, at most N a tick.

Queueless, like the lanes beside it.  There is no research-task queue because
there is nothing to queue: the plan already ranks its inquiries, the loop
authority already knows which of them were admitted, and a queue would be a
third copy of a fact two authorities already hold.  Each tick the lane reads
the ranking, admits the best inquiry that is not already a task, and stops.

Three things it refuses to do, and they are the owner's boundary rather than
an implementation detail:

* ordinary ad-hoc research is absent until its own config and an executable
  ProbeTemplate exist. A configured directed-document executor implicitly
  enables only the producer for planner-selected registered originals; it
  cannot admit an ordinary inquiry and still requires the mission's exact
  research/model/staging grants;
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
from .lane_failure_ledger import lane_budget
from .lane_permission_control import (
    permission_key, clear_obsolete_permissions, record_controlled_failure,
)
from .store import canonical_json

# Ordinary ad-hoc work exists where its own configuration does. Directed
# document execution has a separate closed switch and implicitly supplies a
# directed-only producer, because an executor without an admission producer is
# inert. That mode cannot create an ad-hoc probe loop.
LANE_CONFIG = "research-task-lane.json"
LAUNCHER_KWARG = "research_task_launcher"
IDLE_HOLD = timedelta(hours=1)
# A failed or orphaned child is held the same hour rather than respawned every
# five minutes: whatever refused it will still refuse it in one minute.
FAILURE_HOLD = timedelta(hours=1)
DRIVER_KEY = "research_task"
MAX_TRANSIENT_FAILURES = 3


class ResearchTaskCoordinator:
    """Admit at most N inquiries a tick, settle what finished, hold otherwise."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any | None,
        budget_db: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        failure_ledger_dir: Any | None = None,
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
        self.failure_budget = lane_budget(
            DRIVER_KEY,
            state_dir=(failure_ledger_dir if failure_ledger_dir is not None
                       else getattr(launcher, "state_dir", None)),
            clock=self.clock, max_transient_failures=MAX_TRANSIENT_FAILURES,
        )

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

        from .document_research_inventory import document_inventory_signature

        state_dir = getattr(self.launcher, "state_dir", None)
        document_signature = (
            None if state_dir is None
            else document_inventory_signature(self.store, Path(state_dir))
        )
        return {
            "plan_ref": plan_ref,
            "configuration": (self.launcher.configuration_signature()
                              if hasattr(self.launcher, "configuration_signature") else None),
            "document_inventory": document_signature,
            "tasks": count(
                "SELECT COUNT(*) FROM bounded_planner_loop_versions "
                "WHERE json_extract(record_json,'$.admission.source')='inquiry'"),
            "document_admissions": count(
                "SELECT COUNT(*) FROM mission_document_research_admissions"),
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
        configuration = (self.launcher.configuration()
                         if hasattr(self.launcher, "configuration") else {})
        retired = configuration.get("retired_templates", getattr(self.launcher, "retired_templates", ()))
        decision = grant(mission, bindable_templates(authority, retired=retired))
        settled = self.settle()
        from .coverage_mission import CoverageMissionAuthority

        plan = CoverageMissionAuthority(self.store).latest_research_plan(mission["id"])
        if plan is None:
            return {"status": "idle", "reason": "no research plan yet", **settled}
        has_directed = any(
            isinstance(inquiry, Mapping) and "directed_document" in inquiry
            for inquiry in plan.get("inquiries", ())
        )
        directed_only = configuration.get("directed_only") is True
        if directed_only and not has_directed:
            return {
                "status": "idle",
                "reason": "current plan has no directed document inquiry",
                **settled,
            }
        directed_granted = has_directed and {
            "research_task", "model_run", "stage_record",
        }.issubset(set(mission["autonomy"]["may_write"]))
        top_permission = permission_key(
            "permission|research_task", mission, self.launcher, connection=self.store.connection)
        clear_obsolete_permissions(self.failure_budget, top_permission, scope_prefix="permission|")
        if (directed_only or not decision["granted"]) and not directed_granted:
            # The switch, stated rather than hidden: the reasons are the two
            # owner acts that are missing.
            missing = sorted(
                {"research_task", "model_run", "stage_record"}
                - set(mission["autonomy"]["may_write"])
            )
            reasons = list(decision["reasons"])
            reasons.extend(f"mission_missing_{item}" for item in missing)
            permission = self.failure_budget.blocked(top_permission)
            if permission is None:
                permission = self.failure_budget.record(
                    top_permission, status="gated:not permitted " + ",".join(reasons))
            return {"status": "not_granted", "reasons": reasons,
                    "failure": permission.as_wire(), **settled}
        self.failure_budget.retire(
            top_permission,
            reason=("directed_document_grant_available"
                    if directed_granted else "mission_grant_available"),
        )
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
                if (ticket["status"] in {"failed", "orphaned"}
                        or summary.get("failure_reason") == "not_granted"):
                    # The hold starts when the failure was seen, not when the
                    # child was launched: a run that took fifty minutes to
                    # fail would otherwise be retried ten minutes later, and a
                    # run that failed instantly would be held for an hour from
                    # a timestamp that meant something else.
                    failed_item = canonical_json(latest.get("idle_signature") or {})
                    if latest.get("failure_ticket_ref") != ticket["id"]:
                        failure = record_controlled_failure(
                            self.failure_budget, failed_item, mission, self.launcher,
                            connection=self.store.connection,
                            reason=str(summary.get("failure_reason") or "research task failed"),
                            status=("gated" if summary.get("failure_reason") == "not_granted"
                                    else ticket["status"]),
                        )
                        write_owner_only(self._latest_path(), {
                            **latest, "failure_ticket_ref": ticket["id"],
                        })
                        result["last"]["failure"] = failure.as_wire()
                else:
                    succeeded_item = canonical_json(latest.get("idle_signature") or {})
                    resumed = self.failure_budget.clear(succeeded_item)
                    if resumed:
                        result["last"]["resumed"] = resumed
        if state["remaining_micros"] <= 0 and not directed_granted:
            # C2 will name three more pools; this is the first, and the word
            # the cockpit reads is the one C2 generalises.
            return {**result, "status": "skipped:pool_exhausted",
                    "reason": "今天的专项研究预算已经用完"}
        signature = self._signature(plan["plan_id"])
        failure_item = canonical_json(signature)
        dependency_probe = any(
            row["item_key"] == failure_item
            for row in self.failure_budget.parked_items()
        )
        control = permission_key(failure_item, mission, self.launcher,
                                 connection=self.store.connection)
        permission_recovered = any(
            row["item_key"] != control for row in self.failure_budget.permission_items())
        for row in (self.failure_budget.parked_items() + self.failure_budget.terminal_items()
                    + self.failure_budget.permission_items()):
            if row["item_key"] not in {failure_item, control}:
                self.failure_budget.retire(row["item_key"])
        blocked = self.failure_budget.blocked(control) or self.failure_budget.blocked(failure_item)
        if blocked is not None:
            return {**result, "status": blocked.action, "signature": signature,
                    "reason": self.failure_budget.failure_reason(failure_item),
                    "failure": blocked.as_wire()}
        if (
            latest is not None
            and latest.get("idle_signature") == signature
            and self._within(latest.get("idle_at"), IDLE_HOLD)
            and not dependency_probe
            and not permission_recovered
        ):
            return {**result, "status": "held", "signature": signature,
                    "reason": "计划和已派发的专项研究都没有变化"}
        try:
            ticket = self.launcher.start(
                plan_ref=plan["plan_id"], signature=canonical_json(signature),
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
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--research-task-lane", type=Path, default=None,
        help="Ad-hoc research lane configuration. Omit to keep ad-hoc work absent; "
             "a separately configured document-research lane may still build its "
             "directed-only admission producer.",
    )


def lane_configuration(path: Path) -> dict[str, Any]:
    """This deployment's two knobs, defaulted rather than demanded.

    ``retired_templates`` is the revocation lever the Core cannot offer: a
    probe template version is append-only and has no status column, so an owner
    who wants an admitted template to stop being bound tonight names it here.
    """

    from .call_budget import default_run_budget
    from .research_task import ResearchTaskError, validate_task_budget
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except (OSError, ValueError) as exc:
        raise ResearchTaskError("research task configuration cannot be read") from exc
    if not isinstance(value, dict) or set(value) - {
        "max_admissions_per_tick", "retired_templates", "task_budget",
    }:
        raise ResearchTaskError("research task configuration has an invalid shape")
    requested = value.get("max_admissions_per_tick",
                          default_run_budget("research_task")["max_admissions_per_tick"])
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise ResearchTaskError("max_admissions_per_tick must be a positive integer")
    retired = value.get("retired_templates", [])
    if not isinstance(retired, list) or any(
        not isinstance(item, str) or not item.strip() for item in retired
    ):
        raise ResearchTaskError("retired_templates must be a list of nonempty refs")
    return {
        "max_admissions_per_tick": requested,
        "retired_templates": tuple(retired),
        "task_budget": validate_task_budget(value.get("task_budget", {})),
    }


def build_launcher(args: Any) -> Any | None:
    research_task_path = getattr(args, "research_task_lane", None)
    document_lane_path = getattr(args, "mission_document_research_lane", None)
    if research_task_path is None and document_lane_path is None:
        return None
    from .research_task_launcher import ResearchTaskLauncher

    directed_only = research_task_path is None
    if directed_only:
        # A configured directed-document executor also needs its admission
        # producer.  This does not turn on ad-hoc probes: the child carries a
        # closed directed-only mode and skips every ordinary inquiry.
        from .mission_document_research_lane import (
            lane_configuration as document_lane_configuration,
        )

        document_lane_configuration(document_lane_path)
        from .call_budget import default_run_budget
        from .research_task import validate_task_budget

        configuration = {
            "max_admissions_per_tick": default_run_budget(
                "research_task"
            )["max_admissions_per_tick"],
            "retired_templates": (),
            "task_budget": validate_task_budget({}),
        }
    else:
        configuration = lane_configuration(research_task_path)
    return ResearchTaskLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        max_admissions_per_tick=configuration["max_admissions_per_tick"],
        retired_templates=configuration["retired_templates"],
        task_budget=configuration["task_budget"],
        config_path=research_task_path,
        planner_scheduler_db=getattr(args, "scheduler", None),
        planner_model_config_path=getattr(args, "research_planner_model_config", None),
        directed_only=directed_only,
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
         "or, under the document lane's switch, an exact registered-original "
         "admission. The bounded planner driver advances ordinary loops.",
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
