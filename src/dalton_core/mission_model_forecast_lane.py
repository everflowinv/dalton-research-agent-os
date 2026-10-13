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

from .company_financial_statement_structure import FinancialStatementStructureError
from .company_model_forecast import model_digest, forecast_reporting_boundary
from .company_model_forecast_cli import pending_companies, ForecastSpecificationPending
from .economic_invariants import FORECAST_INVARIANT_CONTRACT_HASH
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
DRIVER_KEY = "mission_model_forecast"
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
        failure_ledger_dir: Any | None = None,
    ) -> None:
        self.missions = missions
        self.models = models
        self.launcher = launcher
        self.mission = mission
        self._open: str | None = None
        # Runs that failed, keyed by (company, model digest, validator contract),
        # so a company whose
        # model cannot be built does not consume the slot every tick. Held for
        # the exact failure remains held across restarts. A deliberately
        # versioned validator correction gets one new identity; an unrelated
        # deploy does not.
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
            "model_digest": ticket.get("model_digest"),
            "validator_contract_hash": ticket.get("validator_contract_hash"),
            "forecast_status": summary.get("forecast_status"),
            "model_version_ref": summary.get("model_version_ref"),
            "forecast_lines_written": summary.get("forecast_lines_written"),
            "results_computed": summary.get("results_computed"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        state_dir = getattr(self.launcher, 'state_dir', None)
        if state_dir is not None and summary.get('family_partition_failures'):
            from .forecast_family_failure_feedback import pointer as family_pointer
            try:
                hint = family_pointer(state_dir, ticket_ref, summary)
            except (OSError, ValueError, KeyError, TypeError):
                hint = None
            if hint:
                settled['family_failure_pointer'] = hint
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
        contract_hash = settled.get("validator_contract_hash")
        if failed and company_ref and digest and contract_hash:
            key = f"{company_ref}|{digest}|{contract_hash}"
            reason = settled.get("failure_reason") or f"last run: {status or settled.get('status')}"
            if settled.get('family_failure_pointer'):
                reason = settled['family_failure_pointer'] + '; ' + reason
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(status or settled.get("status")),
            ).as_wire()
        elif company_ref and digest and contract_hash:
            settled["resumed"] = self.budget.clear(
                f"{company_ref}|{digest}|{contract_hash}")
        return settled

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission", "settled": settled}
        pending_error = None
        waiting_specifications = {}
        try:
            pending = pending_companies(self.missions, self.models, mission,
                                        waiting_specifications=waiting_specifications)
        except ForecastSpecificationPending as exc:
            pending = []
            waiting_specifications = exc.specifications
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            pending = []
            pending_error = f"{type(exc).__name__}: {exc}"
        # Acquisition is a separate governed lane. A financial gap is a source
        # request even when its model is current or the model builder is held.
        state_dir=getattr(self.launcher,"state_dir",None)
        if state_dir is not None:
            from .forecast_material_gaps import open_gap_requests
            pending_tables={c:t for c,_s,t in pending}
            for company in mission.get("universe") or []:
                ref=company.get("company_ref") if isinstance(company,dict) else None
                if not ref or ref in waiting_specifications:continue
                spec=self.missions.latest_company_model_spec(ref)
                if spec is None:continue
                record=self.models.latest(ref)
                table=pending_tables.get(ref,{"periods":[(record or {}).get("forecast_policy",{}).get("latest_actual_end")]})
                failure = None
                if pending_error is not None:
                    from .company_model_inputs import build_model_inputs
                    try:
                        table = build_model_inputs(self.missions, spec)
                    except Exception as exc:
                        # A company's own source gap, never another company's
                        # batch exception attributed to every issuer.
                        failure = f"{type(exc).__name__}: {exc}"
                open_gap_requests(state_dir,spec,table,record=record,failure=failure)
        if pending_error is not None:
            return {"status": "unavailable", "settled": settled, "reason": pending_error}
        if not pending:
            if waiting_specifications:
                return {"status": "idle", "settled": settled,
                        "reason_code": "model_specification_pending",
                        "reason": "模型规格尚待补齐可执行的驱动公式",
                        "waiting_specifications": waiting_specifications}
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
        unavailable: dict[str, str] = {}
        company_ref = spec = table = digest = None
        note_context = None
        try:
            from .financial_note_context import (
                FinancialNoteContextError, FinancialNoteReadContext,
            )
            if (getattr(self.missions, "store", None) is not None
                    and getattr(self.launcher, "state_dir", None) is not None):
                note_context = FinancialNoteReadContext(
                    store=self.missions.store, state_dir=self.launcher.state_dir,
                )
            for candidate, candidate_spec, candidate_table in pending:
                try:
                    definition = candidate_spec.get("financial_statement_structure")
                    needs_note = (
                        isinstance(definition, dict)
                        and bool(definition.get("note_evidence"))
                    )
                    candidate_context = (
                        note_context.projection(candidate)
                        if needs_note and note_context is not None else None
                    )
                    candidate_resolver = (
                        None if candidate_context is None else
                        lambda ref, held=candidate_context: note_context.resolver(
                            ref, expected_context=held,
                        )
                    )
                    candidate_digest = model_digest(
                        candidate_spec, candidate_table,
                        note_evidence_resolver=candidate_resolver,
                        financial_note_context=candidate_context,
                        reporting_boundary=forecast_reporting_boundary(self.missions, self.models.connection, candidate, table=candidate_table),
                    )
                except (FinancialStatementStructureError,
                        FinancialNoteContextError) as exc:
                    unavailable[candidate] = f"{type(exc).__name__}: {exc}"
                    if state_dir is not None:
                        open_gap_requests(state_dir,candidate_spec,candidate_table,record=self.models.latest(candidate),failure=str(exc))
                    continue
                business_key = (
                    f"{candidate}|{candidate_digest}|"
                    f"{FORECAST_INVARIANT_CONTRACT_HASH}"
                )
                permission = current_permission(
                    self.budget, business_key, mission, self.launcher,
                    connection=authority_connection(
                        getattr(self, "store", None), getattr(self, "missions", None),
                        getattr(self, "models", None)))
                decision = (self.budget.blocked(permission)
                            or self.budget.blocked(business_key))
                if decision is not None:
                    held[candidate] = decision.classification.reason
                    continue
                company_ref, spec, table = candidate, candidate_spec, candidate_table
                digest = candidate_digest
                break
        finally:
            if note_context is not None:
                note_context.close()
        if company_ref is None:
            if unavailable:
                reasons = {**held, **unavailable}
                return {
                    "status": "unavailable", "settled": settled,
                    "held": held, "unavailable": unavailable,
                    "reason": "; ".join(
                        f"{ref}: {why}" for ref, why in sorted(reasons.items())
                    ),
                }
            return {"status": "held", "settled": settled, "held": held,
                    "reason": "; ".join(f"{ref}: {why}" for ref, why in held.items())}
        try:
            ticket = self.launcher.start(
                company_ref=company_ref, model_digest=digest,
                validator_contract_hash=FORECAST_INVARIANT_CONTRACT_HASH)
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
            "validator_contract_hash": FORECAST_INVARIANT_CONTRACT_HASH,
            "held": held, "unavailable": unavailable, "settled": settled,
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
            failure_ledger_dir=getattr(server, "state_dir", None),
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
    failure_ledger="mission_model_forecast",
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
