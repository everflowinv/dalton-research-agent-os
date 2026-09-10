"""Periodic driver for admitted Tier 1 bounded planner loops.

The driver is a controller-side component in the mould of the weekly brief
coordinator: each wake it lists loops that have not reached a terminal state,
asks the deterministic planner for the next proposal through the writer's
core-principal RPC, admits accepted proposals (which enqueues the probe
WorkOrder in Scheduler authority), executes at most a bounded number of
read-only probe WorkOrders per tick through the public SEC transport, and
records the resulting source-level ResearchOutcome.  The Core keeps freezing
scope, permissions, parameters, budgets and terminal gates; this module only
turns the crank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .bounded_probe_executor import (
    WORKER_REF,
    BoundedProbeExecutionError,
    execute_probe_work_order,
)
from .budget_pools import POOL_EXHAUSTED_REASON, POOL_EXHAUSTED_STATUS
from .lane_registry import RESERVED_DRIVER_KEYS, tick_lanes
from .public_http_transport import PublicHttpTransport
from .scheduler import Scheduler
from .store import content_hash
from .writer_client import WriterClient


DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_PROBES_PER_TICK = 1
DEFAULT_FILED_WINDOW_DAYS = 400
# What one loop's planner call is allowed to cost when the deployment does not
# say.  Named because P14e's ad-hoc pool reserves against exactly this number
# and a second copy of a price is a price that drifts.
DEFAULT_PLANNER_MAX_COST_USD = 0.5
# C2: the two ledgers the tick writes to and reads from, both beside the
# scheduler in the state directory. Named here rather than in the config
# because the config is a closed shape every installed service.json matches.
TICK_LEDGER_FILENAME = "tick-ledger.sqlite"
BUDGET_LEDGER_FILENAME = "thesis-impact-budget.sqlite"


class BoundedPlannerDriverError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BoundedPlannerDriverConfig:
    writer_socket: Path
    token_config: Path
    scheduler_db: Path
    user_agent: str
    max_response_bytes: int
    timeout_seconds: float
    max_probes_per_tick: int
    filed_window_days: int
    observation_mandate_version_ref: str | None
    doctrine_pack_version_ref: str | None
    doctrine_pack_version_hash: str | None
    planner_routing_policy_ref: str | None
    planner_credential_slot_refs: tuple[str, ...] | None
    planner_model_router_db: Path | None
    planner_broker_socket: Path | None
    planner_broker_auth_key: Path | None
    planner_broker_client_id: str
    planner_expected_agent_id: str
    planner_max_cost_usd: float

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "BoundedPlannerDriverConfig":
        expected = {
            "writer_socket", "token_config", "scheduler_db", "user_agent",
            "max_response_bytes", "timeout_seconds", "max_probes_per_tick",
            "filed_window_days", "observation_mandate_version_ref",
            "doctrine_pack_version_ref", "doctrine_pack_version_hash",
            "planner_routing_policy_ref", "planner_credential_slot_refs",
            "planner_model_router_db", "planner_broker_socket",
            "planner_broker_auth_key", "planner_broker_client_id",
            "planner_expected_agent_id", "planner_max_cost_usd",
        }
        if set(raw) != expected:
            raise BoundedPlannerDriverError(
                "bounded planner driver config has an invalid closed shape"
            )
        paths = {}
        for field in ("writer_socket", "token_config", "scheduler_db"):
            value = raw[field]
            if not isinstance(value, str) or not value.strip():
                raise BoundedPlannerDriverError(f"{field} must be an absolute path")
            path = Path(value)
            if not path.is_absolute():
                raise BoundedPlannerDriverError(f"{field} must be an absolute path")
            paths[field] = path
        user_agent = raw["user_agent"]
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise BoundedPlannerDriverError("user_agent must be non-empty text")
        observation_mandate = raw["observation_mandate_version_ref"]
        if observation_mandate is not None and (
            not isinstance(observation_mandate, str) or not observation_mandate.strip()
        ):
            raise BoundedPlannerDriverError(
                "observation_mandate_version_ref must be non-empty text or null"
            )
        planner_policy = raw["planner_routing_policy_ref"]
        planner_slots_raw = raw["planner_credential_slot_refs"]
        planner_router_db = raw["planner_model_router_db"]
        planner_broker_socket = raw["planner_broker_socket"]
        planner_broker_key = raw["planner_broker_auth_key"]
        planner_client_id = raw["planner_broker_client_id"]
        planner_agent = raw["planner_expected_agent_id"]
        if not isinstance(planner_agent, str) or not planner_agent:
            raise BoundedPlannerDriverError(
                "planner_expected_agent_id must be non-empty text"
            )
        planner_max_cost = raw["planner_max_cost_usd"]
        planner_configured = planner_policy is not None
        if planner_configured != (planner_slots_raw is not None) or (
            planner_configured and (
                planner_router_db is None or planner_broker_socket is None
                or planner_broker_key is None
            )
        ):
            raise BoundedPlannerDriverError(
                "planner model wiring requires policy, credential slots, "
                "router db and broker paths together"
            )
        if planner_configured:
            if not isinstance(planner_policy, str) or not planner_policy.strip():
                raise BoundedPlannerDriverError(
                    "planner_routing_policy_ref must be non-empty text"
                )
            if (
                not isinstance(planner_slots_raw, list)
                or not planner_slots_raw
                or any(not isinstance(item, str) or not item for item in planner_slots_raw)
            ):
                raise BoundedPlannerDriverError(
                    "planner_credential_slot_refs must be a non-empty string array"
                )
            for field, value in (
                ("planner_model_router_db", planner_router_db),
                ("planner_broker_socket", planner_broker_socket),
                ("planner_broker_auth_key", planner_broker_key),
            ):
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise BoundedPlannerDriverError(f"{field} must be an absolute path")
            if not isinstance(planner_client_id, str) or not planner_client_id:
                raise BoundedPlannerDriverError(
                    "planner_broker_client_id must be non-empty text"
                )
            if (
                isinstance(planner_max_cost, bool)
                or not isinstance(planner_max_cost, (int, float))
                or planner_max_cost <= 0
            ):
                raise BoundedPlannerDriverError(
                    "planner_max_cost_usd must be positive"
                )
        else:
            planner_slots_raw = None
        doctrine_ref = raw["doctrine_pack_version_ref"]
        doctrine_hash = raw["doctrine_pack_version_hash"]
        if (doctrine_ref is None) != (doctrine_hash is None):
            raise BoundedPlannerDriverError(
                "doctrine pack ref and hash must be configured together"
            )
        for value, label in ((doctrine_ref, "doctrine_pack_version_ref"),
                             (doctrine_hash, "doctrine_pack_version_hash")):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise BoundedPlannerDriverError(f"{label} must be non-empty text or null")
        numbers = {}
        for field, default in (
            ("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
            ("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            ("max_probes_per_tick", DEFAULT_MAX_PROBES_PER_TICK),
            ("filed_window_days", DEFAULT_FILED_WINDOW_DAYS),
        ):
            value = raw[field]
            if value is None:
                value = default
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BoundedPlannerDriverError(f"{field} must be a number")
            if value <= 0:
                raise BoundedPlannerDriverError(f"{field} must be positive")
            numbers[field] = value
        return cls(  # type: ignore[arg-type]
            user_agent=user_agent,
            observation_mandate_version_ref=observation_mandate,
            doctrine_pack_version_ref=doctrine_ref,
            doctrine_pack_version_hash=doctrine_hash,
            planner_routing_policy_ref=planner_policy,
            planner_credential_slot_refs=(
                None if planner_slots_raw is None else tuple(planner_slots_raw)
            ),
            planner_model_router_db=(
                None if planner_router_db is None else Path(planner_router_db)
            ),
            planner_broker_socket=(
                None if planner_broker_socket is None else Path(planner_broker_socket)
            ),
            planner_broker_auth_key=(
                None if planner_broker_key is None else Path(planner_broker_key)
            ),
            planner_broker_client_id=(planner_client_id or "client:dalton-core"),
            planner_expected_agent_id=planner_agent,
            planner_max_cost_usd=float(
                planner_max_cost if planner_max_cost is not None
                else DEFAULT_PLANNER_MAX_COST_USD
            ),
            **paths, **numbers,
        )


# Every key ``run_once`` puts in its summary that is not a lane's. Kept beside
# the summary itself so a new one is added here, where it is visible, rather
# than only in the registry.
_RESERVED_SUMMARY_KEYS: frozenset[str] = frozenset({
    "status", "active_loop_count", "probes_executed", "executed", "skipped",
    "mission_sec_dispatch", "forecast_reconciliation", "tick_ledger",
})


def _refused_probe_envelope(work: Mapping[str, Any], exc: BaseException) -> dict[str, Any]:
    """A failed ResultEnvelope for a probe the executor would not run.

    The executors refuse a WorkOrder outside their scope or operation by
    raising, and that raise leaves the round admitted with no outcome -- which
    is the one state a loop cannot leave.  It stays pending forever, every
    later tick refuses to materialize its context, and nothing about the
    summary says the loop is stuck rather than merely quiet.

    A refusal is a source that could not be read, which the loop already has a
    word for.  Recording it as a failed round spends one of the loop's rounds
    and lets the next proposal be a terminal one; the loop ends, honestly,
    instead of stalling.
    """

    identity = {
        "work_order_ref": work.get("id"),
        "refusal": f"{type(exc).__name__}: {exc}",
    }
    digest = content_hash(identity)[:32]
    return {
        "schema_version": "0.1",
        "id": f"result:bounded-probe-refused:{digest}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "work_order_ref": work.get("id"),
        "invocation_ref": f"invocation:bounded-probe-refused:{digest}",
        "status": "failed",
        "outputs": {},
        # Nothing ran, so nothing was touched: the empty set is a subset of
        # whatever the template declared, which is what the outcome checks.
        "actual_side_effects": [],
        "usage_refs": [],
        "artifact_refs": [],
        "error": {
            "code": "PROBE_EXECUTOR_REFUSED",
            "message": f"{type(exc).__name__}: {exc}",
        },
        "metadata": {"probe": "refused", "bytes_written": 0},
    }


class BoundedPlannerDriver:
    """Advance every active loop by at most one probe per tick."""

    def __init__(
        self,
        config: BoundedPlannerDriverConfig,
        *,
        client: WriterClient | None = None,
        transport: Any | None = None,
        clock: Any | None = None,
        tick_ledger_path: Path | str | None = None,
    ) -> None:
        self.config = config
        # C2: the tick ledger lives beside the scheduler, in the same state
        # directory, and is found rather than configured. Adding a field to
        # ``BoundedPlannerDriverConfig`` would have changed a closed config
        # shape that every installed service.json has to match, for a file
        # whose location was never in doubt.
        self.tick_ledger_path = Path(
            tick_ledger_path if tick_ledger_path is not None
            else config.scheduler_db.parent / TICK_LEDGER_FILENAME
        )
        # The day ledger is likewise found, and is read only to record what
        # the day's pools moved by. Absent, unreadable or not yet migrated, it
        # contributes nothing and the tick is unaffected.
        self.budget_db_path = config.scheduler_db.parent / BUDGET_LEDGER_FILENAME
        if client is None:
            # Lazy import avoids a module-load cycle with writer_server.
            from .writer_server import load_principals

            principal = load_principals(config.token_config).get("core")
            if principal is None:
                raise BoundedPlannerDriverError("core writer principal is unavailable")
            client = WriterClient(
                str(config.writer_socket), principal.token, timeout=60
            )
        self.client = client
        self.transport = transport or PublicHttpTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _advance_round(
        self, loop: Mapping[str, Any], round_wire: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute one admitted round and record its outcome, or hold it.

        Three outcomes, and the difference between the last two is the whole
        point of this method:

        ``{"kind": "probe", "entry": ...}``
            The probe ran (or was refused by its own executor, which is a
            source that could not be read) and the round has an outcome.
        ``{"kind": "hold", "reason": ...}``
            The *transport* was transiently unavailable -- the writer is busy
            or restarting, and the AlphaEngine branch is a writer RPC.  The
            round keeps its admission and no outcome is written, so the next
            tick picks it up again.

        A blanket ``except Exception`` used to make those two the same thing:
        a writer that was restarting turned a coverage item permanently
        ``source_unavailable``, and it was never retried.  Only the executor's
        own refusal is terminal now.

        Holding is only honest because this method can resume: a round that
        already has a formal ResultEnvelope is not executed again, and
        ``run_once`` calls this for a loop whose latest round never finished.
        Without that, "hold" would have been a synonym for "stall".
        """

        entry: dict[str, Any] = {
            "loop_version_ref": loop["loop_version_ref"],
            "kind": "probe",
            "round_ref": round_wire["id"],
            "work_order_ref": round_wire["work_order_ref"],
        }
        probe_refusal: str | None = None
        with Scheduler(self.config.scheduler_db) as scheduler:
            work_id = round_wire["work_order_ref"]
            authority = scheduler.work_order_authority(work_id)
            if authority is None:
                raise BoundedPlannerDriverError(
                    "admitted probe WorkOrder is missing from Scheduler"
                )
            work = authority["work_order"]
            if scheduler.formal_result(work_id) is None:
                operation = (work.get("metadata") or {}).get("operation")
                try:
                    if operation == "alphaengine_get_document":
                        envelope = self.client.call("bounded_alphaengine_probe", {
                            "work_order": work,
                        })
                    else:
                        envelope = execute_probe_work_order(
                            work,
                            transport=self.transport,
                            user_agent=self.config.user_agent,
                            max_response_bytes=int(self.config.max_response_bytes),
                            timeout_seconds=float(self.config.timeout_seconds),
                            filed_window_days=int(self.config.filed_window_days),
                            clock=self.clock,
                        )
                except BoundedProbeExecutionError as exc:
                    # The executor read the WorkOrder and refused it: wrong
                    # scope, wrong operation, unusable locator.  That is a
                    # decision about this probe and it will not change on a
                    # retry, so it is recorded as a round the loop has spent.
                    envelope = _refused_probe_envelope(work, exc)
                    probe_refusal = envelope["error"]["message"]
                except Exception as exc:  # noqa: BLE001 - transient, not terminal
                    return {
                        "kind": "hold",
                        "reason": f"probe_transport_unavailable:{type(exc).__name__}",
                    }
                lease = scheduler.claim(WORKER_REF, work_order_id=work_id)
                if lease is None:
                    raise BoundedPlannerDriverError(
                        "admitted probe WorkOrder could not be claimed"
                    )
                scheduler.complete(
                    work_id,
                    lease["attempt"]["attempt_number"],
                    WORKER_REF,
                    lease["lease_token"],
                    envelope,
                    idempotency_key=f"bounded-probe-complete:{envelope['id']}",
                )
        outcome = self.client.call("bounded_planner_record_outcome", {
            "round_ref": round_wire["id"],
        })
        entry["outcome_status"] = outcome.get("status")
        entry["outcome_kind"] = (outcome.get("outcome") or {}).get("outcome_kind")
        if probe_refusal is not None:
            entry["probe_refused"] = probe_refusal
        return {"kind": "probe", "entry": entry}

    def _model_proposal(self, context: Mapping[str, Any],
                        *, pool: str | None = None) -> dict[str, Any]:
        """One bounded model attempt for a loop that can act on the answer."""

        params: dict[str, Any] = {
            "context_pack_ref": context["id"],
            "max_input_tokens": 16_000,
            "max_output_tokens": 1_200,
            "max_cost_usd": self.config.planner_max_cost_usd,
            "max_seconds": 180,
        }
        # C2b: which capacity pool this loop's call spends from, as the active
        # loops projection reported it. The writer checks it against the loop
        # record and refuses a disagreement, so this is a declaration rather
        # than an instruction.
        if pool is not None:
            params["pool"] = pool
        try:
            return self.client.call("llm_planner_execute", params)
        except Exception as exc:  # noqa: BLE001 - one loop's failure is not the tick's
            return {"status": f"unavailable:{type(exc).__name__}"}

    def run_once(self) -> dict[str, Any]:
        started_at = self.clock()
        try:
            mission_dispatch = self.client.call("dispatch_coverage_mission_sec_lane", {})
        except Exception as exc:
            mission_dispatch = {"status": f"unavailable:{type(exc).__name__}"}
        # P9c: pending forecast-vs-actual pairs are reconciled every tick under
        # the mission grant; the writer reports skips instead of hiding them.
        try:
            forecast_reconciliation = self.client.call("reconcile_forecasts", {})
        except Exception as exc:
            forecast_reconciliation = {"status": f"unavailable:{type(exc).__name__}"}
        # P14-0: one lane, one registry entry.  The tick used to be eleven
        # near-identical try/except blocks, and adding a lane meant adding a
        # twelfth here and remembering that the order matters -- the filings
        # index goes before web search because they share a fetch slot.  The
        # order is now a number on the LaneSpec, which is at least somewhere a
        # person can read it.  The semantics are unchanged: one lane's failure
        # is that lane's, named by exception type, and never the tick's.
        #
        # The lane results are spread last into the summary below, so a lane
        # driver key naming one of this tick's own keys would overwrite it
        # silently.  RESERVED_DRIVER_KEYS is that list and the registry
        # refuses a lane that claims one; this asserts the two have not drifted
        # apart, because the failure they prevent is invisible.
        assert RESERVED_DRIVER_KEYS == _RESERVED_SUMMARY_KEYS
        lanes: dict[str, Any] = {}
        for spec in tick_lanes():
            try:
                lanes[spec.driver_key] = self.client.call(spec.operation, {})
            except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
                lanes[spec.driver_key] = {"status": f"unavailable:{type(exc).__name__}"}
        listing = self.client.call("bounded_planner_active_loops", {})
        loops = listing["loops"]
        executed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        probes = 0
        for loop in loops:
            if probes >= self.config.max_probes_per_tick:
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": "probe_budget_reached",
                })
                continue
            context = None
            if self.config.doctrine_pack_version_ref is not None:
                try:
                    context = self.client.call(
                        "materialize_bounded_planner_context",
                        {
                            "loop_version_ref": loop["loop_version_ref"],
                            "doctrine_pack_version_ref": (
                                self.config.doctrine_pack_version_ref
                            ),
                            "doctrine_pack_version_hash": (
                                self.config.doctrine_pack_version_hash
                            ),
                            "as_of": datetime.now(timezone.utc).isoformat(
                                timespec="microseconds"
                            ),
                        },
                    )
                except Exception as exc:
                    # Materializing is free and refuses outright while a round
                    # is pending, so this is where a stalled loop is learned --
                    # before any model call.  A pending round is not a doctrine
                    # failure: it is a round that was admitted and never
                    # finished, and the deterministic planner below hands it
                    # back so this tick can finish it.
                    if "round is pending" not in str(exc):
                        skipped.append({
                            "loop_version_ref": loop["loop_version_ref"],
                            "reason": (
                                f"doctrine_context_unavailable:{type(exc).__name__}"
                            ),
                        })
                        continue
            if context is None:
                proposal = self.client.call("bounded_planner_propose_next", {
                    "loop_version_ref": loop["loop_version_ref"],
                })
            else:
                remaining = context.get("remaining_budget") or {}
                if int(remaining.get("rounds_remaining", 1)) < 1:
                    # A loop with no round left cannot probe, so a model asked
                    # what to probe next is money spent on an answer that
                    # cannot be admitted.  The deterministic planner is free
                    # and can still propose the terminal this loop needs.
                    executed_model = {"status": "budget_exhausted"}
                else:
                    executed_model = self._model_proposal(
                        context, pool=loop.get("pool"))
                if (executed_model.get("status") == "rejected"
                        and executed_model.get("reason") == POOL_EXHAUSTED_REASON):
                    # C2b: this loop's pool is spent for today. That is a
                    # budget decision, not a fault and not an outage, so the
                    # loop is held rather than handed to the free
                    # deterministic planner: falling through would admit a
                    # round the pool said no to, and the loop would arrive at
                    # tomorrow with one fewer round and nothing to show. The
                    # writer refused before leasing anything, so nothing was
                    # billed and nothing has to be undone.
                    skipped.append({
                        "loop_version_ref": loop["loop_version_ref"],
                        "reason": POOL_EXHAUSTED_REASON,
                        "pool": executed_model.get("pool"),
                        "lane_status": POOL_EXHAUSTED_STATUS,
                    })
                    continue
                if executed_model.get("status") == "proposal_ready":
                    proposal = executed_model["proposal"]
                elif executed_model.get("status") == "core_action":
                    # The coordinator already submitted the deterministic
                    # hard-control proposal (e.g. coverage-complete terminate);
                    # re-proposing would only return a duplicate forever.
                    proposal = executed_model["result"]
                else:
                    # One bounded model attempt per tick; the deterministic
                    # doctrine-aware planner remains the safety net.
                    proposal = self.client.call(
                        "bounded_planner_propose_next_with_context",
                        {"planner_context_pack_ref": context["id"]},
                    )
            status = proposal.get("status")
            if status == "pending_round":
                # The round this loop is waiting on.  Finishing it is the
                # whole reason a transport failure may hold instead of writing
                # a terminal outcome: a hold that nothing ever resumed would
                # be a stall with a friendlier name.
                round_wire = proposal.get("round")
                if round_wire is None:
                    skipped.append({
                        "loop_version_ref": loop["loop_version_ref"],
                        "reason": status,
                    })
                    continue
                resumed = self._advance_round(loop, round_wire)
                if resumed["kind"] == "hold":
                    skipped.append({
                        "loop_version_ref": loop["loop_version_ref"],
                        "reason": resumed["reason"],
                        "round_ref": round_wire["id"],
                    })
                    continue
                entry = resumed["entry"]
                entry["resumed"] = True
                executed.append(entry)
                probes += 1
                continue
            if status == "terminal":
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": status,
                })
                continue
            if status != "fresh":
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": f"proposal_{status}",
                })
                continue
            action = proposal.get("action") or {}
            admitted = self.client.call("bounded_planner_admit_proposal", {
                "proposal_ref": proposal["id"],
            })
            if admitted.get("status") == "terminal":
                executed.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "kind": "terminal",
                    "terminal_state": admitted["terminal_event"]["terminal_state"],
                })
                continue
            if admitted.get("status") != "fresh":
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": f"admission_{admitted.get('status')}",
                })
                continue
            if action.get("kind") != "probe":
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": f"action_{action.get('kind')}",
                })
                continue
            round_wire = admitted["round"]
            advanced = self._advance_round(loop, round_wire)
            if advanced["kind"] == "hold":
                skipped.append({
                    "loop_version_ref": loop["loop_version_ref"],
                    "reason": advanced["reason"],
                    "round_ref": round_wire["id"],
                })
                continue
            entry = advanced["entry"]
            if self.config.observation_mandate_version_ref is not None:
                try:
                    observation = self.client.call(
                        "bounded_planner_record_observation",
                        {
                            "round_ref": round_wire["id"],
                            "mandate_version_ref": (
                                self.config.observation_mandate_version_ref
                            ),
                        },
                    )
                except Exception as exc:
                    # An observation question is attention, never a probe
                    # result; a scope or mandate gap must not kill the tick.
                    entry["observation_status"] = (
                        f"unrecorded:{type(exc).__name__}"
                    )
                else:
                    entry["observation_status"] = observation.get("status")
                    if observation.get("question_ref") is not None:
                        entry["observation_question_ref"] = observation["question_ref"]
                    if observation.get("lane_status") is not None:
                        entry["mission_lane_status"] = observation["lane_status"]
                    if observation.get("lane_ticket_ref") is not None:
                        entry["mission_lane_ticket_ref"] = observation["lane_ticket_ref"]
            executed.append(entry)
            probes += 1
        summary = {
            "status": "completed" if executed else "idle",
            "active_loop_count": len(loops),
            "probes_executed": probes,
            "executed": executed,
            "skipped": skipped,
            "mission_sec_dispatch": mission_dispatch,
            "forecast_reconciliation": forecast_reconciliation,
            **lanes,
        }
        summary["tick_ledger"] = self._record_tick(summary, started_at=started_at)
        return summary

    def _record_tick(
        self, summary: Mapping[str, Any], *, started_at: datetime,
    ) -> dict[str, Any]:
        """Append this tick to the ledger, and say so if that failed.

        C2. Before this the summary was a return value that ``service`` wrote
        into ``run/heartbeat.json`` and the next tick overwrote, so the idle
        ratio, lane stalls and spend by pool were not slow to compute -- they
        were gone. The write is the last thing a tick does and is never
        allowed to be the thing that fails it, but it is also never allowed to
        fail quietly: a bookkeeper nobody can tell has stopped is worse than
        no bookkeeper at all.
        """

        from .budget_pools import day_pool_spend_at, pool_for_operation
        from .lane_registry import tick_lanes
        from .tick_ledger import TickLedger

        ended = self.clock()
        try:
            pool_spend = day_pool_spend_at(
                self.budget_db_path,
                day=ended.astimezone(timezone.utc).date().isoformat(),
            )
            operations, pools = {}, {}
            for spec in tick_lanes():
                operations[spec.driver_key] = spec.operation
                pools[spec.driver_key] = pool_for_operation(spec.operation)
            with TickLedger(self.tick_ledger_path, clock=self.clock) as ledger:
                return ledger.append_tick(
                    summary, started_at=started_at, ended_at=ended,
                    pool_spend=pool_spend, lane_operations=operations,
                    lane_pools=pools,
                )
        except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a tick
            return {"status": f"unrecorded:{type(exc).__name__}",
                    "reason": str(exc)[:200]}


__all__ = [
    "BoundedPlannerDriver",
    "BoundedPlannerDriverConfig",
    "BoundedPlannerDriverError",
]
