"""P15d: propose at most one conviction call, for at most one company, a tick.

Queueless like the lanes around it, and quieter than any of them.  What needs
doing is derived from the Ledger every tick by the same deterministic gate the
child runs: a company that has passed its Initial Screen, holds an active
thesis, stands somewhere the market does not, and whose material can source
the market's view.  Everything else is skipped before a child is spawned,
which on the live Core today is every company -- and that is the correct
answer, not an outage.

Two rules bound the noise, and they are different rules.

**Idempotence** is per (company, evidence fingerprint).  A tick that fires
again over unchanged theses, debates, consensus metrics and calendar rows
would produce the same call, so it produces none.  This is the same shape the
debate-map lane uses and for the same reason: a fingerprint cannot disagree
with the child, because it is not an opinion about what to do.

**The weekly cap** is per company and is not the same thing.  Evidence moves
several times a week for a covered company; conviction does not.  A call a
week is what a person can read and answer, and the cap is enforced in the
authority against the stored week rather than in this coordinator's memory,
because a coordinator's memory does not survive a restart and a restart is
precisely what would produce a second call on a Monday morning.

One child at a time, settled on the following tick.  A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

from typing import Any, Callable

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget
from .lane_permission_control import (
    authority_connection, current_permission, record_controlled_failure,
)

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "conviction_call_launcher"
CONVICTION_MODEL_CONFIG = "initial-screen-model-config.json"

# Outcomes about this moment rather than about this evidence: the scheduler had
# the request in flight, or there was no route just then.  The next tick can do
# the work, so the company is not held back.
TRANSIENT_STATUSES: frozenset[str] = frozenset({"busy", "model_unavailable"})
DRIVER_KEY = "mission_conviction"
# The one outcome that changes what the next tick sees: a proposal now exists
# under this fingerprint, so the selector skips the company on its own.
PUBLISHED_STATUS = "fresh"


class MissionConvictionLaneCoordinator:
    """Launch and settle the conviction-call lane."""

    def __init__(
        self,
        *,
        store: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        # Runs that produced nothing, keyed by (company, fingerprint), so a
        # company whose call was refused does not take the slot every five
        # minutes while the others never get a turn.  Process-local: a restart
        # is nearly always a deploy, which is the likeliest thing to have fixed
        # whatever it was.
        self.budget = lane_budget(DRIVER_KEY, state_dir=failure_ledger_dir)

    # -- settling ---------------------------------------------------------

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
            # From the ticket, not the summary: a child that died before
            # writing one still has to be attributable to the run it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "evidence_fingerprint": ticket.get("evidence_fingerprint"),
            "call_status": summary.get("call_status"),
            "proposal_ref": summary.get("proposal_ref"),
            "direction": summary.get("direction"),
            "eligible": summary.get("eligible"),
            "gate_reasons": summary.get("gate_reasons"),
            "rubric_findings": summary.get("rubric_findings"),
            "cost_micros": summary.get("cost_micros"),
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
        call_status = settled.get("call_status")
        published = (
            settled.get("status") == "succeeded" and call_status == PUBLISHED_STATUS
        )
        hold = not published
        company_ref = settled.get("company_ref")
        fingerprint = settled.get("evidence_fingerprint")
        if hold and company_ref and fingerprint:
            key = f"{company_ref}|{fingerprint}"
            reason = settled.get("failure_reason") or f"last run: {call_status or settled.get('status')}"
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(call_status or settled.get("status")),
            ).as_wire()
        elif company_ref and fingerprint:
            settled["resumed"] = self.budget.clear(f"{company_ref}|{fingerprint}")
        return settled

    # -- the tick ---------------------------------------------------------

    def _members(self, mission: Any) -> list[str]:
        """Companies past the Initial Screen, in the mission's own order.

        Read the same way residency is read (P14a): across *every* version of
        the mission, because a stage record binds the version it was written
        under and publishing a new one does not copy the old records forward.
        """

        from .tracking_cadence import screen_passed_companies

        from .coverage_mission import CoverageMissionAuthority

        try:
            return screen_passed_companies(CoverageMissionAuthority(self.store), mission)
        except Exception:  # noqa: BLE001 - an unreadable spine is no members
            return []

    def _choose(self, mission: Any) -> tuple[str | None, str | None, str | None]:
        """The first eligible company that is not already answered or held.

        The gate is run here as well as in the child, and deliberately so: a
        child costs a process and a model call, and the whole value of the
        deterministic gate is that a company we agree with the market about is
        refused for free.  The child re-runs it because the evidence can move
        between the tick and the spawn, and the child's copy is the one that
        gets written into the proposal.
        """

        from datetime import datetime, timezone

        from .conviction_call import (
            ConvictionCallAuthority, precheck, table_exists, week_key,
        )
        from .conviction_call_cli import fingerprint_of, gate_inputs

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        authority = (
            ConvictionCallAuthority(self.store) if table_exists(self.store.connection)
            else None
        )
        blocked: tuple[str, str, str] | None = None
        for company_ref in self._members(mission):
            inputs = gate_inputs(self.store, company_ref)
            gate = precheck(
                company_ref=company_ref,
                theses=inputs["theses"],
                open_debates=inputs["open_debates"],
                consensus_gap=inputs["consensus_gap"],
                dossier_variant_view=inputs["dossier_variant_view"],
            )
            if not gate["eligible"]:
                continue
            fingerprint = fingerprint_of(inputs, gate)
            if authority is not None:
                if any(record["evidence_fingerprint"] == fingerprint
                       for record in authority.proposals(company_ref)):
                    continue
                if authority.calls_this_week(company_ref, now):
                    blocked = blocked or (
                        company_ref, fingerprint,
                        f"{company_ref} already has a call in {week_key(now)}")
                    continue
            business_key = f"{company_ref}|{fingerprint}"

            permission = current_permission(

                self.budget, business_key, mission, self.launcher,
                connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)))

            decision = (self.budget.blocked(permission)

                        or self.budget.blocked(business_key))
            if decision is not None:
                blocked = blocked or (company_ref, fingerprint,
                                      decision.classification.reason)
                continue
            return company_ref, fingerprint, None
        if blocked is not None:
            return blocked
        return None, None, None

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            company_ref, fingerprint, held = self._choose(mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if company_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "no covered company can be shown to differ from the "
                              "market today"}
        if held is not None:
            return {"status": "held", "company_ref": company_ref,
                    "settled": settled, "reason": held}
        try:
            ticket = self.launcher.start(
                company_ref=company_ref, fingerprint=fingerprint)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "evidence_fingerprint": fingerprint, "ticket_ref": ticket["id"],
            "settled": settled,
        }


# -- the lane ---------------------------------------------------------------

def dispatch(server: Any, params: Any) -> dict[str, Any]:
    """Controller tick (P15d).  At most one call proposal, for one company."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no conviction-call lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionConvictionLaneCoordinator(
            store=server.store, launcher=launcher, mission=mission,
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--conviction-call-model-config", default=None,
        help="Model configuration the conviction-call lane drafts with. Omit "
             "and the lane is absent: the gate is deterministic but the call "
             "itself is an argument, and there is no argument without a model.",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "conviction_call_model_config", None) is None:
        return None
    from pathlib import Path as _Path

    from .conviction_call_launcher import ConvictionCallLauncher

    return ConvictionCallLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.conviction_call_model_config,
        scheduler_db=getattr(args, "scheduler", None),
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / CONVICTION_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--conviction-call-model-config", str(config)]


# C2's budget pools.  A conviction call is the end of covering a company -- it
# reads the theses, the map and the model that coverage produced -- so it
# belongs in the coverage pool rather than the event-response pool that pays
# for reacting to today's news.  That is also where an unassigned lane and an
# unassigned purpose land by default, so both ends are already correct and
# ``LaneSpec.budget_pool`` is deliberately left unset: the explicit rows belong
# in ``budget_pools.LANE_POOLS`` / ``PURPOSE_POOLS``, which is not this slice's
# file, and they are named in the report's integration list.
LANE = register_lane(LaneSpec(
    operation="dispatch_conviction_call",
    # 141: after the crowd-source feed at 140, which is one of the places a
    # market narrative comes from, and before the research-task lane at 150.
    # A call written before the week's sales notes and posts were read would
    # be a call about last week's market view.
    order=141,
    driver_key="conviction_call",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P15d: automation proposes one high-conviction call at a time -- "
         "direction, variant view, consensus gap, event pathway, risk/reward "
         "against the Playbook's standards -- and a person accepts, rejects "
         "or defers it.",
))


__all__ = [
    "CONVICTION_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "PUBLISHED_STATUS",
    "TRANSIENT_STATUSES",
    "MissionConvictionLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
