"""P13am: decide one company's model on a tick, and stop when there is nothing left.

The specification lane is unlike the acquisition lanes in one way that shapes
this whole module: it has no queue. What needs deciding is a *derived* fact --
a company that has filed statements and has no specification for the structure
those filings disclose -- so it is computed from the ledger every tick rather
than written down and drained. There is nothing to leave stuck.

That also means the natural resting state is silence. Five companies get five
specifications and then the lane does nothing until one of them files something
new, at which point the structure hash moves and exactly that company is
decided about again. A quarter's worth of ticks in between are all "nothing to
decide", which is the correct answer and costs a couple of database reads.

One child at a time, and the ticket is named by the company and the disclosure
together, so a tick that fires while a child is running does not start a second
one for the same judgement.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .company_model_cli import choose_company
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget
from .lane_permission_control import (
    authority_connection, current_permission, record_controlled_failure,
)
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)

MAX_FAILURE_DETAIL_CHARS = 500
DRIVER_KEY = "mission_model_spec"


class MissionModelSpecLaneCoordinator:
    """Launch and settle the company-model-specification lane."""

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.missions = missions
        self.launcher = launcher
        self.mission = mission
        # The judgement in flight, so the next tick can settle it. A launcher
        # cannot be asked "what did you last run" -- it holds one process, not
        # a history -- and the ticket ref is the only handle on the summary.
        self._open: str | None = None
        # Judgements whose run failed, keyed by (company, disclosure), so a
        # doomed company does not consume the slot every five minutes. Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed whatever it was.
        self.budget = lane_budget(DRIVER_KEY, state_dir=failure_ledger_dir)

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
            # writing one still has to be attributable to the judgement it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "state_hash": ticket.get("state_hash"),
            "spec_status": summary.get("spec_status"),
            "spec_ref": summary.get("spec_ref"),
            "cost_micros": summary.get("cost_micros"),
            "revenue_drivers": summary.get("revenue_drivers"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        """Close out the previous tick's child, if it has finished.

        Settling happens here rather than after ``start`` for the obvious
        reason: a child inspected in the same breath it was spawned is always
        still running, and a lane that only ever looks at its own newborn
        never learns anything.
        """

        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        failed = settled.get("status") != "succeeded" or settled.get("spec_status") in (
            "refused", "model_unavailable", "busy", "failed",
        )
        company_ref = settled.get("company_ref")
        state_hash = settled.get("state_hash")
        if failed and company_ref and state_hash:
            key = f"{company_ref}|{state_hash}"
            spec_status = settled.get("spec_status")
            reason = settled.get("failure_reason") or f"last run: {spec_status or settled.get('status')}"
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(spec_status or settled.get("status")),
            ).as_wire()
        elif company_ref and state_hash:
            settled["resumed"] = self.budget.clear(f"{company_ref}|{state_hash}")
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        try:
            company_ref, state = choose_company(self.missions, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if company_ref is None:
            return {"status": "idle", "settled": settled,
                    "reason": "every company has a current specification"}
        # The hash comes from the projection the chooser already built. Two
        # projections of the same company hash differently if they were built
        # with different arguments, and the selector disagreeing with the run
        # about the hash is how this lane first got stuck relaunching one
        # company while the other four waited behind it.
        state_hash = state["state_hash"]
        business_key = f"{company_ref}|{state_hash}"

        permission = current_permission(

            self.budget, business_key, mission, self.launcher,
                connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)))

        held = self.budget.blocked(permission) or self.budget.blocked(business_key)
        if held is not None:
            return {"status": "held", "company_ref": company_ref,
                    "settled": settled, "reason": held.classification.reason,
                    "failure": held.as_wire()}
        try:
            ticket = self.launcher.start(company_ref=company_ref, state_hash=state_hash)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "state_hash": state_hash, "ticket_ref": ticket["id"],
            "settled": settled,
        }


# P13ad installed this for the Initial Screen's own drafting model; P13am
# runs the company model specification lane on it too, for the same reason it
# exists -- both are judgement, not extraction. Deciding that IBM is a mix
# story and Accenture is a headcount business is exactly where a weaker model
# returns something plausible and generic, which looks like a decision and is
# worse than none.
MODEL_SPEC_MODEL_CONFIG = "initial-screen-model-config.json"
LAUNCHER_KWARG = "model_spec_launcher"


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13am).

    One company's model specification at a time.  The lane has no queue: what
    needs deciding is derived from the ledger every tick, so its resting state
    is silence and there is nothing to leave stuck.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no company model lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionModelSpecLaneCoordinator(
            missions=server.coverage_mission,
            launcher=launcher,
            mission=mission,
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # Needs a model, because the judgement is the whole product; without one
    # the child answers "gated" and nothing is written.
    parser.add_argument("--model-spec-model-config")


def build_launcher(args: Any) -> Any | None:
    if args.model_spec_model_config is None:
        return None
    from pathlib import Path as _Path

    from .company_model_launcher import CompanyModelSpecLauncher

    return CompanyModelSpecLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=args.model_spec_model_config,
        scheduler_db=args.scheduler,
    )


def argv_fragment(context: Any) -> list[str]:
    config = context.state / MODEL_SPEC_MODEL_CONFIG
    if not config.is_file():
        return []
    return ["--model-spec-model-config", str(config)]


LANE = register_lane(LaneSpec(
    operation="dispatch_company_model_spec",
    order=90,
    driver_key="company_model_spec",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P13am: how this company should be modelled -- what drives revenue, "
         "how costs behave, which statements it actually needs forecast.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MODEL_SPEC_MODEL_CONFIG",
    "MissionModelSpecLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
