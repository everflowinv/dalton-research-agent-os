"""P14a: the judgement lane on a tick -- decide, or stay quiet and cheap.

Queueless, like the lanes it follows: what needs doing is derived from the two
ledgers every tick -- events with no judgement -- so its resting state is
silence and nothing can get stuck in it.  Five covered companies with nothing
new produce one query and no child.

It runs after the tracking lane because it reads what that lane records, and
the order is explicit for the reason the registry insists on: an ordering that
emerged from import order would put the brain before its eyes on the day
somebody sorted the module list.

Each batch names one mission-scoped event group and its configuration. A held
group does not prevent the next eligible group from advancing.
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
from .lane_failure_ledger import lane_budget
from .lane_permission_control import record_controlled_failure

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
        pending: Callable[[Mapping[str, Any]], Any],
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.launcher = launcher
        self.mission = mission
        self.pending = pending
        self._open: str | None = None
        self.budget = lane_budget("mission_event_judgement", state_dir=failure_ledger_dir)

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
            "group_key": ticket.get("group_key"),
            "company_ref": ticket.get("company_ref"),
            "event_ref": ticket.get("event_ref"),
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
        group_key = settled.get("group_key")
        status = str(settled.get("judgement_status") or settled.get("status") or "")
        if int(settled.get("judged") or 0) == 0 and int(settled.get("refused") or 0) > 0:
            status = "refused"
        failed = (settled.get("status") != "succeeded"
                  or (int(settled.get("judged") or 0) == 0
                      and int(settled.get("refused") or 0) > 0)
                  or status.startswith(("refused", "gated", "busy", "unavailable")))
        if group_key and failed:
            reason = settled.get("failure_reason") or f"last run: {status}"
            settled["failure"] = record_controlled_failure(
                self.budget, group_key, self.mission() or {}, self.launcher,
                reason=reason, status=status,
            ).as_wire()
        elif group_key:
            settled["resumed"] = self.budget.clear(group_key)
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            candidates = self.pending(mission)
            signature = getattr(self.launcher, "configuration_signature", None)
            configuration = signature() if signature is not None else None
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not candidates:
            return {"status": "idle", "settled": settled,
                    "reason": "every event this company has produced has been judged"}
        if isinstance(candidates, str):
            candidates = [{"company_ref": "legacy", "event_ref": candidates,
                           "event_refs": (candidates,), "group_hash": "legacy",
                           "group_key": f"{mission['id']}|{candidates}"}]
        if self._open is not None:
            return {"status": "busy", "settled": settled,
                    "reason": "the previous judgement batch is still running"}
        held = {}
        chosen = None
        for candidate in candidates:
            candidate = dict(candidate)
            candidate.setdefault("event_refs", (candidate["event_ref"],))
            candidate.setdefault("group_hash", candidate["group_key"])
            candidate["failure_key"] = candidate["group_key"]
            if configuration is not None:
                candidate["failure_key"] += f"|configuration:{configuration}"
            decision = self.budget.blocked(candidate["failure_key"])
            if decision is None:
                chosen = candidate
                break
            held[candidate["failure_key"]] = decision.classification.reason
        if chosen is None:
            return {"status": "held", "settled": settled, "held": held,
                    "reason": "all pending event groups are durably held"}
        batch = chosen["failure_key"]
        try:
            ticket = self.launcher.start(
                batch_ref=batch, company_ref=chosen["company_ref"],
                event_refs=tuple(chosen["event_refs"]),
                event_group_hash=chosen["group_hash"],
                group_key=chosen["failure_key"])
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {"status": "launched", "ticket_ref": ticket["id"],
                "batch_ref": batch, "group_key": chosen["failure_key"],
                "company_ref": chosen["company_ref"], "held": held,
                "settled": settled}


def newest_unjudged(store: Any, mission: Mapping[str, Any]) -> str | None:
    """The newest event with no judgement, or nothing.

    One query against two tables rather than the child's full selection: the
    lane only needs to know whether there is anything to do and what to name
    the run.
    """

    universe = sorted({str(item.get("company_ref")) for item in mission.get("universe", ())
                       if isinstance(item, Mapping) and item.get("company_ref")})
    if not universe:
        return None
    mission_ref = mission.get("mission_ref")
    if not isinstance(mission_ref, str) or not mission_ref:
        return None
    placeholders = ",".join("?" for _ in universe)
    row = store.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "JOIN coverage_mission_versions mv "
        "ON mv.mission_version_id=e.mission_version_ref "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        f"WHERE j.event_ref IS NULL AND mv.mission_ref=? "
        f"AND e.company_ref IN ({placeholders}) "
        "ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1",
        [mission_ref, *universe],
    ).fetchone()
    return None if row is None else row["event_id"]


def pending_event_groups(store: Any, missions: Any,
                         mission: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Mission-scoped eligible groups, newest first, with durable identities."""
    from .event_judgement_cli import (
        buyback_group_key, incremental_group_hash, unjudged_event_groups,
    )
    from .event_judgement import EventJudgementAuthority
    from .research_event import ResearchEventAuthority
    from .tracking_cadence import screen_passed_companies
    events = ResearchEventAuthority(store)
    judgements = EventJudgementAuthority(store)
    allowed_versions = {
        row["mission_version_id"] for row in store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_versions "
            "WHERE mission_ref=?", (mission["mission_ref"],)
        ).fetchall()
    }
    candidates = []
    for company_ref in screen_passed_companies(missions, mission):
        for group in unjudged_event_groups(
                events, judgements, company_ref=company_ref, limit=None,
                mission_version_refs=tuple(sorted(allowed_versions)),
                newest_first=True):
            group_type = buyback_group_key(group[0]) or ("event", group[0]["id"])
            group_hash = incremental_group_hash(group)
            identity = "|".join((mission["id"], company_ref, *group_type,
                                  group_hash))
            candidates.append({
                "company_ref": company_ref, "event_ref": group[0]["id"],
                "event_refs": tuple(sorted(item["id"] for item in group)),
                "group_hash": group_hash,
                "group_key": identity, "occurred_at": max(
                    str(item["occurred_at"]) for item in group),
            })
    return sorted(candidates, key=lambda item: (item["occurred_at"], item["group_key"]),
                  reverse=True)


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
            pending=lambda active: pending_event_groups(
                server.store, server.coverage_mission, active),
            failure_ledger_dir=getattr(server, "state_dir", None),
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
    "pending_event_groups",
]
