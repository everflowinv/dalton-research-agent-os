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
from typing import Any

from .bounded_probe_executor import (
    WORKER_REF,
    execute_probe_work_order,
)
from .public_http_transport import PublicHttpTransport
from .scheduler import Scheduler
from .writer_client import WriterClient


DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_PROBES_PER_TICK = 1
DEFAULT_FILED_WINDOW_DAYS = 400


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

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "BoundedPlannerDriverConfig":
        expected = {
            "writer_socket", "token_config", "scheduler_db", "user_agent",
            "max_response_bytes", "timeout_seconds", "max_probes_per_tick",
            "filed_window_days", "observation_mandate_version_ref",
            "doctrine_pack_version_ref", "doctrine_pack_version_hash",
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
            **paths, **numbers,
        )


class BoundedPlannerDriver:
    """Advance every active loop by at most one probe per tick."""

    def __init__(
        self,
        config: BoundedPlannerDriverConfig,
        *,
        client: WriterClient | None = None,
        transport: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.config = config
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

    def run_once(self) -> dict[str, Any]:
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
                    skipped.append({
                        "loop_version_ref": loop["loop_version_ref"],
                        "reason": f"doctrine_context_unavailable:{type(exc).__name__}",
                    })
                    continue
                proposal = self.client.call(
                    "bounded_planner_propose_next_with_context",
                    {"planner_context_pack_ref": context["id"]},
                )
            else:
                proposal = self.client.call("bounded_planner_propose_next", {
                    "loop_version_ref": loop["loop_version_ref"],
                })
            status = proposal.get("status")
            if status in {"terminal", "pending_round"}:
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
            with Scheduler(self.config.scheduler_db) as scheduler:
                work_id = round_wire["work_order_ref"]
                authority = scheduler.work_order_authority(work_id)
                if authority is None:
                    raise BoundedPlannerDriverError(
                        "admitted probe WorkOrder is missing from Scheduler"
                    )
                envelope = execute_probe_work_order(
                    authority["work_order"],
                    transport=self.transport,
                    user_agent=self.config.user_agent,
                    max_response_bytes=int(self.config.max_response_bytes),
                    timeout_seconds=float(self.config.timeout_seconds),
                    filed_window_days=int(self.config.filed_window_days),
                    clock=self.clock,
                )
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
            entry = {
                "loop_version_ref": loop["loop_version_ref"],
                "kind": "probe",
                "round_ref": round_wire["id"],
                "work_order_ref": round_wire["work_order_ref"],
                "outcome_status": outcome.get("status"),
                "outcome_kind": (outcome.get("outcome") or {}).get("outcome_kind"),
            }
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
            executed.append(entry)
            probes += 1
        return {
            "status": "completed" if executed else "idle",
            "active_loop_count": len(loops),
            "probes_executed": probes,
            "executed": executed,
            "skipped": skipped,
        }


__all__ = [
    "BoundedPlannerDriver",
    "BoundedPlannerDriverConfig",
    "BoundedPlannerDriverError",
]
