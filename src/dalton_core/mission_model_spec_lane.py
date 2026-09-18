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

from .company_model_cli import (
    REPAIR_CONTRACT_HASH, REPAIR_CONTRACT_REF, REQUEST_IN_FLIGHT_STATUS,
    choose_company, filed_classifications,
)
from .company_model_state import (
    DEFAULT_MODEL_SPEC_PROMPT_BYTES, CompanyModelPromptBudgetError,
)
from .company_model_spec import TASK_HASH
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
# Six hours, the same number the dossier and gate lanes use for the same
# purpose.  A refusal this lane recorded keeps the company out for that long
# and is then asked once more, instead of for ever.
#
# Live on 2026-09-18 the lane answered ``held: every pending company is durably
# held`` on every tick, and its last two runs were from 2026-09-17 02:43 and
# 02:59 -- both ``spec_status: "refused"`` on a *structure* rule.  A structural
# refusal classifies as ``transient``/``unmapped``, so three ticks spent the
# transient budget and the item was ``held`` with nothing in the system that
# could ever clear it: the count does not decay, and the key named the company,
# the disclosure, the task and the repair *policy* -- not the repair contract
# the verdict was actually reached under.
CONTENT_REFUSAL_COOLDOWN_SECONDS = 6 * 60 * 60


def business_key(
    *, company_ref: str, state_hash: str, task_hash: str,
    repair_policy_hash: str, validation_hash: str,
    repair_contract_hash: str = REPAIR_CONTRACT_HASH,
) -> str:
    """What a refusal of this company's specification was *about*.

    The company and the disclosure, obviously.  The task hash, because a
    different question is different work.  And -- new -- the repair contract:
    which structural rules the model was allowed to be shown and asked to fix.
    A refusal reached when ``EPS numerator must use the company-specific
    diluted EPS numerator role`` was a whole refusal is not a statement about a
    system in which that rule is repaired once before anyone gives up, and
    keeping the old verdict keyed as though it were is exactly what made a
    reviewed change to those rules unreachable.
    """

    return (
        f"{company_ref}|{state_hash}|{task_hash}|{repair_policy_hash}|"
        f"financial-validation:{validation_hash}|"
        f"repair-contract:{repair_contract_hash}"
    )


def retire_superseded_contracts(budget: Any, current: str) -> list[str]:
    """Retire this company's blocks that were decided under other contracts.

    The key already changed, so those blocks can never be consulted again; this
    is what keeps them from accumulating in the ledger and in the tick result's
    ``failure_budget`` counts, and what puts a readable ``superseded`` row in
    the ledger saying *why* a durable hold stopped applying.  Matched on the
    part of the key that names the work -- the company, the disclosure and the
    task -- so a genuinely different judgement is never retired by accident.
    """

    prefix = "|".join(current.split("|")[:3]) + "|"
    marker = f"repair-contract:{REPAIR_CONTRACT_HASH}"
    retired = []
    for row in budget.blocked_items():
        item = str(row["item_key"])
        if not item.startswith(prefix):
            continue
        # ``current`` itself, and everything the permission control decorates
        # it with, is this contract's own bookkeeping.  Retiring that would
        # abolish the failure budget rather than age it out.
        if item.startswith(current) or marker in item:
            continue
        if budget.retire(item, reason=(
                "decided under a superseded repair contract; the current "
                f"one is {REPAIR_CONTRACT_REF}")):
            retired.append(item)
    return sorted(retired)


class MissionModelSpecLaneCoordinator:
    """Launch and settle the company-model-specification lane."""

    def __init__(
        self,
        *,
        missions: Any,
        launcher: Any,
        mission: Callable[[], dict[str, Any] | None],
        failure_ledger_dir: Any | None = None,
        cooldown_seconds: int = CONTENT_REFUSAL_COOLDOWN_SECONDS,
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
        self.budget = lane_budget(
            DRIVER_KEY, state_dir=failure_ledger_dir,
            block_ttl_seconds=int(cooldown_seconds))

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
        raw_codes = summary.get("failure_codes")
        failure_codes = (
            sorted({code for code in raw_codes
                    if isinstance(code, str) and 1 <= len(code) <= 80})
            if isinstance(raw_codes, list) else []
        )
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            # From the ticket, not the summary: a child that died before
            # writing one still has to be attributable to the judgement it was
            # spawned for, or it can never be held back from being retried.
            "company_ref": ticket.get("company_ref"),
            "state_hash": ticket.get("state_hash"),
            "task_hash": ticket.get("task_hash"),
            "repair_policy_hash": ticket.get("repair_policy_hash"),
            "financial_validation_contract_hash": ticket.get(
                "financial_validation_contract_hash"),
            "spec_status": summary.get("spec_status"),
            "spec_ref": summary.get("spec_ref"),
            "cost_micros": summary.get("cost_micros"),
            "failure_codes": failure_codes,
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
        spec_status = settled.get("spec_status")
        # Nothing was refused and nothing was spent: the Scheduler is holding a
        # lease on this exact request because another process is paying for
        # this judgement right now.  Charging a failure budget for that turns a
        # five-minute wait into a durable hold -- and the wait is free, because
        # the claim fails before any model call is made.
        if spec_status == REQUEST_IN_FLIGHT_STATUS:
            settled["waiting"] = (
                "another process holds the Scheduler lease on this request; "
                "the next tick replays its result rather than paying again")
            return settled
        failed = settled.get("status") != "succeeded" or spec_status in (
            "refused", "model_unavailable", "busy", "failed", "gated",
            "stale_input",
        )
        company_ref = settled.get("company_ref")
        state_hash = settled.get("state_hash")
        task_hash = settled.get("task_hash")
        repair_policy_hash = settled.get("repair_policy_hash")
        validation_hash = settled.get("financial_validation_contract_hash")
        if (failed and company_ref and state_hash and task_hash
                and repair_policy_hash and validation_hash):
            key = business_key(
                company_ref=company_ref, state_hash=state_hash,
                task_hash=task_hash, repair_policy_hash=repair_policy_hash,
                validation_hash=validation_hash)
            reason = settled.get("failure_reason") or f"last run: {spec_status or settled.get('status')}"
            if "PROVIDER_BUDGET_EXCEEDED" in (settled.get("failure_codes") or []):
                reason += " [PROVIDER_BUDGET_EXCEEDED]"
            settled["failure"] = record_controlled_failure(
                self.budget, key, self.mission() or {}, self.launcher,
                reason=reason, connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)), status=str(spec_status or settled.get("status")),
            ).as_wire()
        elif (company_ref and state_hash and task_hash and repair_policy_hash
              and validation_hash):
            settled["resumed"] = self.budget.clear(business_key(
                company_ref=company_ref, state_hash=state_hash,
                task_hash=task_hash, repair_policy_hash=repair_policy_hash,
                validation_hash=validation_hash))
        return settled

    def settle_only(self) -> dict[str, Any]:
        """Harvest the current child without selecting or launching another."""

        if self._open is None:
            return {"status": "idle", "settled": None}
        settled = self._settle_open()
        return {
            "status": (
                "unavailable" if settled is None
                else "running" if settled.get("status") == "running"
                else "settled"
            ),
            "settled": settled,
        }

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        mission = self.mission()
        if mission is None:
            return {"status": "unconfigured", "reason": "no mission",
                    "settled": settled}
        excluded: set[str] = set()
        held_companies: dict[str, Any] = {}
        superseded: list[str] = []
        try:
            repair_policy_hash = self.launcher.repair_policy_hash()
            if hasattr(self.launcher, "state_projection_config"):
                state_projection = self.launcher.state_projection_config()
                numeric_context_policy = state_projection["numeric_context_policy"]
                prompt_byte_limit = state_projection["prompt_byte_limit"]
            else:
                numeric_context_policy = (
                    self.launcher.numeric_context_policy()
                    if hasattr(self.launcher, "numeric_context_policy") else None
                )
                prompt_byte_limit = DEFAULT_MODEL_SPEC_PROMPT_BYTES
        except Exception as exc:  # noqa: BLE001 - malformed config cannot launch
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        try:
            classifications = (
                filed_classifications(self.missions.store)
                if getattr(self.missions, "store", None) is not None else {})
        except Exception as exc:  # noqa: BLE001 - authority drift cannot break the tick
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        while True:
            try:
                from .financial_note_context import FinancialNoteReadContext
                note_context = None
                if (getattr(self.missions, "store", None) is not None
                        and getattr(self.launcher, "state_dir", None) is not None):
                    note_context = FinancialNoteReadContext(
                        store=self.missions.store,
                        state_dir=self.launcher.state_dir,
                    )
                try:
                    company_ref, state = choose_company(
                        self.missions, mission,
                        classifications=classifications,
                        numeric_context_policy=numeric_context_policy,
                        prompt_byte_limit=prompt_byte_limit,
                        exclude_company_refs=frozenset(excluded),
                        financial_note_context=note_context)
                finally:
                    if note_context is not None:
                        note_context.close()
            except CompanyModelPromptBudgetError as exc:
                return {
                    "status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "prompt_budget_report": exc.report,
                }
            except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
                return {"status": "unavailable", "settled": settled,
                        "reason": f"{type(exc).__name__}: {exc}"}
            if company_ref is None:
                if held_companies:
                    result = {"status": "held", "settled": settled,
                              "reason": "every pending company is held",
                              "held": held_companies}
                    if superseded:
                        result["superseded"] = superseded
                    if len(held_companies) == 1:
                        ref, failure = next(iter(held_companies.items()))
                        result.update({"company_ref": ref, "failure": failure,
                                       "reason": failure["reason"]})
                    return result
                return {"status": "idle", "settled": settled,
                        **({} if not superseded else {"superseded": superseded}),
                        "reason": "every company has a current specification"}
            # The hash comes from the projection the chooser already built.
            # Keeping that exact state beside the company also lets a held
            # first candidate be skipped without rebuilding a different one.
            state_hash = state["state_hash"]
            key = business_key(
                company_ref=company_ref, state_hash=state_hash,
                task_hash=TASK_HASH, repair_policy_hash=repair_policy_hash,
                validation_hash=self.launcher.financial_validation_contract_hash())
            # A verdict reached under an older repair contract is about work
            # nobody will ask this question of again: its key no longer
            # matches, so retire it rather than leaving it in the ledger and in
            # the tick's ``failure_budget`` counts for ever.
            retired = retire_superseded_contracts(self.budget, key)
            if retired:
                superseded.extend(retired)

            permission = current_permission(
                self.budget, key, mission, self.launcher,
                connection=authority_connection(
                    getattr(self, "store", None), getattr(self, "missions", None),
                    getattr(self, "models", None)))

            held = self.budget.blocked(permission)
            budget_park = self.budget.parked(key)
            if (
                held is None and budget_park is not None
                and budget_park.classification.dependency == "model_budget"
            ):
                # A terminal Scheduler result has no retry authority.  In
                # particular, a legacy BUDGET_REFUSED may represent either a
                # proved no-send admission refusal or a provider response
                # rejected for exceeding the Work's output-token authority. Neither
                # becomes safe merely because the generic dependency-probe
                # interval elapsed (or the writer restarted).
                held = budget_park
            controlled_reentry = None
            if held is not None and held.classification.dependency == "model_budget":
                recovery = getattr(self.launcher, "controlled_budget_reentry", None)
                controlled_reentry = (
                    None if recovery is None else recovery(
                        business_key=key, current_permission=permission,
                        mission=dict(mission), company_ref=company_ref,
                        state_hash=state_hash, task_hash=TASK_HASH,
                        repair_policy_hash=repair_policy_hash))
                if controlled_reentry is not None:
                    held = None
            if held is None and controlled_reentry is None:
                held = self.budget.blocked(key)
            if held is None:
                break
            held_companies[company_ref] = held.as_wire()
            excluded.add(company_ref)
        try:
            launch_kwargs = {
                "company_ref": company_ref, "state_hash": state_hash,
                "task_hash": TASK_HASH, "repair_policy_hash": repair_policy_hash,
            }
            if controlled_reentry is not None:
                launch_kwargs["controlled_reentry"] = controlled_reentry
            ticket = self.launcher.start(**launch_kwargs)
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
            "repair_policy_hash": repair_policy_hash,
            "repair_contract_ref": REPAIR_CONTRACT_REF,
            "settled": settled, "held": held_companies,
            **({} if not superseded else {"superseded": superseded}),
        }


# P13ad installed this for the Initial Screen's own drafting model; P13am
# runs the company model specification lane on it too, for the same reason it
# exists -- both are judgement, not extraction. Deciding that IBM is a mix
# story and Accenture is a headcount business is exactly where a weaker model
# returns something plausible and generic, which looks like a decision and is
# worse than none.
MODEL_SPEC_MODEL_CONFIG = "initial-screen-model-config.json"
LAUNCHER_KWARG = "model_spec_launcher"


def _coordinator(server: Any) -> MissionModelSpecLaneCoordinator | None:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return None
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
    return coordinator


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P13am).

    One company's model specification at a time.  The lane has no queue: what
    needs deciding is derived from the ledger every tick, so its resting state
    is silence and there is nothing to leave stuck.
    """

    coordinator = _coordinator(server)
    if coordinator is None:
        return {"status": "unconfigured",
                "reason": "no company model lane on this writer"}
    return coordinator.dispatch_once()


def settle(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Lightweight child harvest; it never selects or launches work."""

    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        return {"status": "idle", "settled": None}
    return coordinator.settle_only()


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
    "LANE", "settle",
    "CONTENT_REFUSAL_COOLDOWN_SECONDS",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "REQUEST_IN_FLIGHT_STATUS",
    "business_key",
    "retire_superseded_contracts",
    "MODEL_SPEC_MODEL_CONFIG",
    "MissionModelSpecLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
]
