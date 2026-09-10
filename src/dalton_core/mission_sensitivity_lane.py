"""P13-M3: one company's sensitivity table per tick, and silence when nothing moved.

Queueless, like the two lanes before it: what needs doing is derived from the
ledger every tick rather than written down and drained, so there is nothing to
get stuck.

What needs doing is one thing. A company whose ForecastModelVersion has been
revised, or whose street estimate has moved, gets its table recomputed against
the current model. Everything else is silence, and the silence is cheap: the
selector hashes the model's ``content_hash`` together with the consensus
fingerprint and compares it with the last projection's. Five companies get five
tables, and then this lane does nothing until one of their models is revised or
a broker changes their mind.

It runs *after* the driver-model lane (order 95 before 96) for the obvious
reason: a sensitivity table is a projection of a model, and computing it from a
model the tick was about to replace would produce a table that is stale before
it is stored.

One child at a time, settled on the following tick. A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .forecast_sensitivity_cli import pending_companies
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget
from .lane_failure_class import Classification, CONTENT_REFUSED

MAX_FAILURE_DETAIL_CHARS = 500
DRIVER_KEY = "mission_sensitivity"
LAUNCHER_KWARG = "forecast_sensitivity_launcher"


class MissionSensitivityLaneCoordinator:
    """Launch and settle the sensitivity lane."""

    def __init__(
        self,
        *,
        store: Any,
        missions: Any,
        models: Any,
        projections: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.store = store
        self.missions = missions
        self.models = models
        self.projections = projections
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        # Runs that failed, keyed by (company, fingerprint), so a company whose
        # table cannot be computed does not consume the slot every tick. Held
        # for this process only: a restart is nearly always a deploy, which is
        # the most likely thing to have fixed it.
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
            # writing one still has to be attributable to the run it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "projection_digest": ticket.get("projection_digest"),
            "sensitivity_status": summary.get("sensitivity_status"),
            "projection_ref": summary.get("projection_ref"),
            "drivers_selected": summary.get("drivers_selected"),
            "bridge_status": summary.get("bridge_status"),
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
        status = str(settled.get("sensitivity_status") or "")
        # ``unavailable:`` is P17b's economic-invariant refusal. It belongs on
        # the same list as ``refused:``: the run succeeded, nothing was
        # published, and retrying the identical digest every tick would refuse
        # the identical way until the assumptions change.
        failed = settled.get("status") != "succeeded" or status.startswith(
            ("refused:", "unavailable:"))
        company_ref = settled.get("company_ref")
        digest = settled.get("projection_digest")
        if failed and company_ref and digest:
            key = f"{company_ref}|{digest}"
            reason = settled.get("failure_reason") or f"last run: {status or settled.get('status')}"
            classification = (Classification(CONTENT_REFUSED, reason, "lane_refusal",
                                              status=status)
                              if status.startswith(("refused:", "unavailable:")) else None)
            settled["failure"] = self.budget.record(
                key, reason=reason, status=status or settled.get("status"),
                classification=classification).as_wire()
        elif company_ref and digest:
            settled["resumed"] = self.budget.clear(f"{company_ref}|{digest}")
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        try:
            pending = pending_companies(
                self.store, self.missions, self.models, self.projections, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not pending:
            return {"status": "idle", "settled": settled,
                    "reason": "every company's sensitivity table matches its model"}
        # A company being held is skipped rather than reported. DXC's tax line
        # is unavailable on live today because its operating income changes
        # sign, and a company whose table cannot be built must not stand in
        # front of the ones whose can.
        held: dict[str, str] = {}
        company_ref = digest = None
        for candidate, _record, _consensus, candidate_digest in pending:
            decision = self.budget.blocked(f"{candidate}|{candidate_digest}")
            if decision is not None:
                held[candidate] = decision.classification.reason
                continue
            company_ref, digest = candidate, candidate_digest
            break
        if company_ref is None:
            return {"status": "held", "settled": settled, "held": held,
                    "reason": "; ".join(f"{ref}: {why}" for ref, why in held.items())}
        try:
            ticket = self.launcher.start(company_ref=company_ref,
                                         projection_digest=digest)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "projection_digest": digest, "ticket_ref": ticket["id"],
            "held": held, "settled": settled,
        }


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13-M3).

    One company's sensitivity table at a time, and only when its model or its
    street has moved. It has no queue and no model call, so with nothing to do
    it costs a few reads and a hash.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no sensitivity lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .forecast_sensitivity import SensitivityProjectionAuthority
        from .model_forecast_driver import ForecastModelAuthority

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionSensitivityLaneCoordinator(
            store=server.store,
            missions=server.coverage_mission,
            # Built here rather than passed in: constructing an authority is
            # what installs its schema, and the writer has no other reason to
            # know this lane keeps projections.
            models=ForecastModelAuthority(server.store),
            projections=SensitivityProjectionAuthority(server.store),
            launcher=launcher,
            mission=mission,
            failure_ledger_dir=getattr(server, "state_dir", None),
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # A flag rather than a path: this child makes no model call, reaches no
    # network and reads no configuration, so there is nothing to point it at.
    parser.add_argument("--forecast-sensitivity-lane", action="store_true")


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "forecast_sensitivity_lane", False):
        return None
    from pathlib import Path as _Path

    from .forecast_sensitivity_launcher import ForecastSensitivityLauncher

    return ForecastSensitivityLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent)


def argv_fragment(context: Any) -> list[str]:
    # Every lane's fragment is gated on the thing that lane needs being on
    # disk. This one needs nothing installed -- no connector, no model, no
    # configuration -- but the invariant that a lane is off until its
    # prerequisite exists holds here too, so it is gated on the one thing it
    # genuinely cannot work without: a Core holding driver models.
    if not (context.state / "core.sqlite").is_file():
        return []
    return ["--forecast-sensitivity-lane"]


LANE = register_lane(LaneSpec(
    operation="dispatch_forecast_sensitivity",
    order=96,
    driver_key="forecast_sensitivity",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P13-M3: which of this model's assumptions actually move the answer, "
         "how far each has moved in the company's own history, and where our "
         "numbers sit against the street's. Runs after the driver-model lane, "
         "which decides the model it is a projection of.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionSensitivityLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
