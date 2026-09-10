"""P13-M2: build one company's driver model on a tick, and stop when nothing has moved.

Queueless, like the specification lane and for the same reason: what needs
doing is *derived* from the ledger every tick rather than written down and
drained. There is nothing to get stuck.

Two things need doing, and only two. A company with a specification and no
driver model gets its first one. A company whose model estimated a quarter the
filings now cover gets those estimates answered, with the future left alone.
Nothing here re-forecasts because a document arrived: what a filing or an event
*means* for the quarters ahead is a judgement, it carries a decision word, and
it reaches the model as an explicit revision from whoever made it.

Silence is therefore the resting state and it is cheap. Five companies get five
models and then this lane does nothing until one of them reports.

One child at a time, settled on the following tick. A child inspected in the
same breath it was spawned is always still running, and a lane that only ever
looks at its own newborn never learns anything.
"""

from __future__ import annotations

from typing import Any, Callable

from .company_model_forecast import model_digest
from .company_model_forecast_cli import pending_companies
from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "model_forecast_launcher"


class MissionModelForecastLaneCoordinator:
    """Launch and settle the driver-model lane."""

    def __init__(
        self,
        *,
        missions: Any,
        models: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.missions = missions
        self.models = models
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        # Runs that failed, keyed by (company, digest), so a company whose
        # model cannot be built does not consume the slot every tick. Held for
        # this process only: a restart is nearly always a deploy, which is the
        # most likely thing to have fixed it.
        self._failed: dict[str, str] = {}

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
            "model_digest": ticket.get("model_digest"),
            "forecast_status": summary.get("forecast_status"),
            "model_version_ref": summary.get("model_version_ref"),
            "forecast_lines_written": summary.get("forecast_lines_written"),
            "results_computed": summary.get("results_computed"),
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
        status = str(settled.get("forecast_status") or "")
        # ``unavailable:`` is P17b's economic-invariant refusal. It belongs on
        # the same list as ``refused:``: the run succeeded, nothing was
        # published, and retrying the identical digest every tick would refuse
        # the identical way until the assumptions change.
        failed = settled.get("status") != "succeeded" or status.startswith(
            ("refused:", "unavailable:"))
        company_ref = settled.get("company_ref")
        digest = settled.get("model_digest")
        if failed and company_ref and digest:
            self._failed[f"{company_ref}|{digest}"] = (
                settled.get("failure_reason")
                or f"last run: {status or settled.get('status')}"
            )
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        try:
            pending = pending_companies(self.missions, self.models, mission)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not pending:
            return {"status": "idle", "settled": settled,
                    "reason": "every company has a current driver model"}
        # The digest comes from the table the chooser already built, for the
        # reason the specification lane found the hard way: two projections of
        # the same company hash differently if they were built with different
        # arguments, and a selector that disagrees with the run about the hash
        # names a different ticket every time.
        #
        # And a company being held is skipped rather than reported, because one
        # company whose model cannot be built must not stand in front of the
        # other four. IBM's specification binds no filed revenue concept at
        # all; without this the lane would hand IBM back every tick forever.
        held: dict[str, str] = {}
        company_ref = spec = table = digest = None
        for candidate, candidate_spec, candidate_table in pending:
            candidate_digest = model_digest(candidate_spec, candidate_table)
            reason = self._failed.get(f"{candidate}|{candidate_digest}")
            if reason is not None:
                held[candidate] = reason
                continue
            company_ref, spec, table = candidate, candidate_spec, candidate_table
            digest = candidate_digest
            break
        if company_ref is None:
            return {"status": "held", "settled": settled, "held": held,
                    "reason": "; ".join(f"{ref}: {why}" for ref, why in held.items())}
        try:
            ticket = self.launcher.start(company_ref=company_ref, model_digest=digest)
        except LaneChildConflict as exc:
            return {"status": "busy", "company_ref": company_ref, "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "company_ref": company_ref,
                    "settled": settled, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {
            "status": "launched", "company_ref": company_ref,
            "model_digest": digest, "ticket_ref": ticket["id"],
            "held": held, "settled": settled,
        }


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13-M2).

    One company's driver model at a time, and only the two things this lane is
    allowed to do: a first model for a company that has none, or the actuals
    for a quarter that has been filed. It has no queue and no model call, so
    with nothing to do it costs a few reads.
    """

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no driver model lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        from .model_forecast_driver import ForecastModelAuthority

        def mission() -> Any:
            pointer = server.store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer "
                "ORDER BY mission_ref LIMIT 1"
            ).fetchone()
            return (None if pointer is None
                    else server.coverage_mission.mission(pointer["mission_version_id"]))

        coordinator = MissionModelForecastLaneCoordinator(
            missions=server.coverage_mission,
            # Built here rather than passed in: constructing the authority is
            # what installs its schema, and the writer has no other reason to
            # know this lane keeps versioned models.
            models=ForecastModelAuthority(server.store),
            launcher=launcher,
            mission=mission,
        )
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    # A flag rather than a path: this child makes no model call, reaches no
    # network and reads no configuration, so there is nothing to point it at.
    parser.add_argument("--model-forecast-lane", action="store_true")


def build_launcher(args: Any) -> Any | None:
    if not getattr(args, "model_forecast_lane", False):
        return None
    from pathlib import Path as _Path

    from .model_forecast_launcher import ModelForecastLauncher

    return ModelForecastLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent)


def argv_fragment(context: Any) -> list[str]:
    # Every lane's fragment is gated on the thing that lane needs being on
    # disk -- a governance record, a model configuration -- and stays off
    # without it. This lane needs nothing installed: no connector, no model,
    # no configuration. What it does need is a Core to read the
    # specifications and the filings out of, so that is what it is gated on,
    # and the invariant that a lane is off until its prerequisite exists
    # holds for this one too.
    if not (context.state / "core.sqlite").is_file():
        return []
    return ["--model-forecast-lane"]


LANE = register_lane(LaneSpec(
    operation="dispatch_company_model_forecast",
    order=95,
    driver_key="company_model_forecast",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P13-M2: this company's driver model -- what we assume about each "
         "driver each quarter, what follows from it, and what the filings "
         "later said. Runs after the specification lane, which decides the "
         "drivers it rests on.",
))


__all__ = [
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "MissionModelForecastLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
