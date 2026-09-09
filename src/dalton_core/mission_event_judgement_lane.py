"""P14a: the judgement lane on a tick -- decide, or stay quiet and cheap.

Queueless, like the lanes it follows: what needs doing is derived from the two
ledgers every tick -- events with no judgement -- so its resting state is
silence and nothing can get stuck in it.  Five covered companies with nothing
new produce one query and no child.

It runs after the tracking lane because it reads what that lane records, and
the order is explicit for the reason the registry insists on: an ordering that
emerged from import order would put the brain before its eyes on the day
somebody sorted the module list.

The batch ref is the newest unjudged event, so a tick that fires while nothing
has arrived names the same ticket instead of paying to re-read the same batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "event_judgement_launcher"
JUDGE_MODEL_CONFIG = "event-judgement-model-config.json"
VERIFIER_MODEL_CONFIG = "event-verifier-model-config.json"
TRACKING_POLICY = "tracking-policy.json"


class MissionEventJudgementLaneCoordinator:
    """Launch and settle the judgement lane."""

    def __init__(
        self,
        *,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        pending: Callable[[Mapping[str, Any]], str | None],
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        self.pending = pending
        self._open: str | None = None
        self._last_batch: str | None = None

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            "batch_ref": ticket.get("batch_ref"),
            "judgement_status": summary.get("judgement_status"),
            "judged": summary.get("judged"),
            "refused": summary.get("refused"),
            "decisions": summary.get("decisions"),
            "actions": summary.get("actions"),
            "pool": summary.get("pool"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            newest = self.pending(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if newest is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every event this company has produced has been judged"}
        batch = f"{mission['id']}:{newest}"
        if batch == self._last_batch:
            return {"status": "idle", "settled": settled, "batch_ref": batch,
                    "reason": "this batch has already been dispatched"}
        try:
            ticket = self.launcher.start(batch_ref=batch)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._last_batch = batch
        return {"status": "launched", "ticket_ref": ticket["id"],
                "batch_ref": batch, "settled": settled}


def newest_unjudged(store: Any, mission: Mapping[str, Any]) -> str | None:
    """The newest event with no judgement, or nothing.

    One query against two tables rather than the child's full selection: the
    lane only needs to know whether there is anything to do and what to name
    the run.
    """

    row = store.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        "WHERE j.event_ref IS NULL ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else row["event_id"]


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14a)."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured", "reason": "no judgement lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .event_judgement import EventJudgementAuthority
        from .research_event import ResearchEventAuthority

        # Constructing the two authorities is what installs their schemas; the
        # writer has no other reason to know this lane keeps a judgement
        # ledger, and the tick query below needs both tables to exist.
        ResearchEventAuthority(server.store)
        EventJudgementAuthority(server.store)

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionEventJudgementLaneCoordinator(
            launcher=launcher, mission=mission,
            pending=lambda active: newest_unjudged(server.store, active),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument("--event-judgement-model-config")
    parser.add_argument("--event-verifier-model-config")
    parser.add_argument("--event-judgement-policy")


def build_launcher(args: Any) -> Any | None:
    judge = getattr(args, "event_judgement_model_config", None)
    verifier = getattr(args, "event_verifier_model_config", None)
    if not judge or not verifier:
        return None
    from .event_judgement_launcher import EventJudgementLauncher

    return EventJudgementLauncher(
        state_dir=Path(args.db).expanduser().resolve().parent,
        judge_model_config=judge,
        verifier_model_config=verifier,
        policy_path=getattr(args, "event_judgement_policy", None),
        scheduler_db=getattr(args, "scheduler", None),
    )


def argv_fragment(context: Any) -> list[str]:
    judge = context.state / JUDGE_MODEL_CONFIG
    verifier = context.state / VERIFIER_MODEL_CONFIG
    # Both or neither. A judge with no independent verifier would produce
    # decisions whose verification field exists and means nothing.
    if not (judge.is_file() and verifier.is_file()):
        return []
    argv = [
        "--event-judgement-model-config", str(judge),
        "--event-verifier-model-config", str(verifier),
    ]
    policy = context.state / TRACKING_POLICY
    if policy.is_file():
        argv += ["--event-judgement-policy", str(policy)]
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_event_judgement",
    order=116,
    driver_key="event_judgement",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P14a: one bounded call per unjudged event -- what should change, "
         "why, and against which driver and thesis. Runs after the tracking "
         "lane (86), which is where the events it reads come from, and after "
         "the Initial Screen lane (110), because a company that has just "
         "passed its gate should be tracked before it is judged.",
))


__all__ = [
    "JUDGE_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionEventJudgementLaneCoordinator",
    "TRACKING_POLICY",
    "VERIFIER_MODEL_CONFIG",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "newest_unjudged",
]
