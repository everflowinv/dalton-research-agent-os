"""One bounded model call for the cockpit (P9d-18, ADR-0006).

The cockpit answers ad-hoc questions and drafts goal or steering proposals
with the same model the extraction lane uses, under the same authorities:

- the route comes from the model router under the extraction routing policy;
- the spend is admitted in the shared day-budget ledger against the active
  mission's daily caps (paid calls and cost), exactly as an extraction window
  is, so a question costs the mission a paid call and the ledger fails closed
  when the day is exhausted;
- the WorkOrder is enqueued, leased and completed in the scheduler, so a
  repeated request replays the persisted result instead of paying twice.

Nothing here writes to the Core: the answer is a cockpit artifact, never a
Claim.  The control process owns no Core write handle and gets none.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import ResultEnvelope, WorkOrder
from .document_extraction import validate_model_config
from .model_accounting import ModelAccountingError, _route_estimate_micros
from .model_router import ModelRouter
from .openclaw_model_adapter import OpenClawModelAdapter, OpenClawModelAdapterError
from .scheduler import Scheduler
from .store import content_hash
from .thesis_impact_budget import ThesisImpactBudgetError, ThesisImpactBudgetStore

WORKER_REF = "worker:cockpit-model:0.1"
SCHEMA_VERSION = "0.1"
# "draft" is P10c: one section of a mission deliverable, drafted by the lane
# rather than by the owner, under the same route, budget and replay.
# P13n: "plan" is the research planner deciding what to work on next. It is a
# cockpit-shaped call -- one bounded, budgeted, replayable model call against
# the mission -- but it is not the cockpit answering the owner, so it is named
# rather than folded into "ask".
# P13al: "model_spec" is deciding how one company should be modelled -- what
# drives its revenue, how its costs behave, which statements matter, which
# operating metrics the market watches. Named rather than folded into "plan"
# because it is a judgement about a company, not about this system's own work,
# and the two are routed and budgeted separately.
# Q1: "quality" is the research quality loop grading one artefact against a
# frozen rubric. Named rather than folded into "ask" because it is the system
# reading its own output against a standard, and a judge that competes with the
# owner's questions for the same budget line should be visible as its own line.
PURPOSES = frozenset({"ask", "goal", "steer", "draft", "plan", "model_spec", "quality"})
# Room for the completion write after the model answers, so a call that
# finishes right on its timeout still has a live lease to complete against.
_LEASE_GRACE_SECONDS = 30.0
# P13aa: which definition of "the same request" produced this identity.
#
# Version 1 hashed a wall-clock created_at into the WorkOrder wire while
# leaving it out of the id, so the same question asked twice reused one
# idempotency key with two different hashes -- a permanent conflict, and every
# Initial Screen section sat in it for two days. Fixing the definition is not
# enough on its own: the keys written under the old definition still hold the
# old hash, so a corrected request collides with its own history. A changed
# identity definition is a changed identity, and it is versioned like every
# other frozen definition here rather than quietly reusing the old keys.
IDENTITY_VERSION = 2


class CockpitModelError(RuntimeError):
    """The call was refused or failed; the message is safe to show."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def build_work(*, purpose: str, request_id: str, prompt: str, mission_version_ref: str,
               max_input_tokens: int, max_output_tokens: int, max_cost_usd: float, max_seconds: int,
               created_at: str | None = None) -> WorkOrder:
    if purpose not in PURPOSES:
        raise CockpitModelError("unknown cockpit model purpose")
    if len(prompt.encode("utf-8")) > max_input_tokens:
        raise CockpitModelError("the question and its context exceed the model input bound")
    identity = {"identity_version": IDENTITY_VERSION,
                "purpose": purpose, "request_id": request_id,
                "mission_version_ref": mission_version_ref,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    digest = content_hash(identity)
    at = created_at or _now()
    return WorkOrder(
        schema_version=SCHEMA_VERSION, id=f"work:cockpit-{purpose}-{digest[:32]}",
        created_at=at, updated_at=at, question=prompt, requested_capabilities=("research",),
        runtime_profile_ref="runtime-profile:dalton-model-broker:0.1",
        budget={"max_input_tokens": max_input_tokens, "max_output_tokens": max_output_tokens,
                "max_total_tokens": max_input_tokens + max_output_tokens,
                "max_cost_usd": max_cost_usd, "max_seconds": max_seconds},
        idempotency_key=f"cockpit:{purpose}:{digest}", declared_side_effects=(), status="ready",
        input_refs=(), metadata={"control_plane": "cockpit", "purpose": purpose, "request_id": request_id,
                                 "mission_version_ref": mission_version_ref},
    )


def _failure(work: WorkOrder, code: str, route_ref: str | None) -> ResultEnvelope:
    identity = {"work_order_ref": work.id, "code": code, "route_ref": route_ref}
    return ResultEnvelope(
        schema_version=SCHEMA_VERSION, id=f"result:cockpit-control-{content_hash(identity)[:32]}",
        created_at=_now(), work_order_ref=work.id,
        invocation_ref=f"invocation:not-started:{content_hash(identity)[:32]}", status="failed",
        outputs={}, actual_side_effects=(), usage_refs=(), artifact_refs=(), error={"code": code},
        metadata={"control_plane_failure": True, "route_decision_ref": route_ref},
    )


def _cost_micros(invocation: Any, route: Mapping[str, Any], profile: Mapping[str, Any], reserved: int) -> tuple[int, str]:
    usage = dict(getattr(invocation, "usage", {}) or {})
    raw = usage.get("raw_provider_telemetry", {}).get("cost", {}) if isinstance(usage.get("raw_provider_telemetry"), Mapping) else {}
    if isinstance(raw, Mapping) and raw.get("available") is True and isinstance(raw.get("usd"), (int, float)) \
            and not isinstance(raw.get("usd"), bool) and float(raw["usd"]) >= 0:
        return int((Decimal(str(raw["usd"])) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)), "actual"
    try:
        return _route_estimate_micros(route, profile), "estimated"
    except ModelAccountingError:
        return reserved, "reserved"


class CockpitModel:
    """Run one bounded, budgeted, replayable model call for the cockpit."""

    def __init__(self, model_config: Mapping[str, Any], *, scheduler_db: str | Path,
                 adapter_factory: Callable[..., Any] | None = None, clock: Callable[[], datetime] | None = None,
                 max_input_tokens: int = 120_000, max_output_tokens: int = 3_000, max_cost_usd: float = 0.05,
                 timeout_seconds: int = 120) -> None:
        self.config = validate_model_config(model_config)
        self.scheduler_db = str(Path(scheduler_db).expanduser().resolve())
        self.adapter_factory = adapter_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_cost_usd = max_cost_usd
        self.timeout_seconds = timeout_seconds

    def _adapter(self, router: ModelRouter) -> Any:
        if self.adapter_factory is not None:
            return self.adapter_factory(router)
        config = self.config
        return OpenClawModelAdapter(
            config["broker_socket"], route_resolver=router.get_decision, auth_client_id=config["broker_client_id"],
            auth_key_provider=lambda: Path(config["broker_auth_key"]).read_bytes().strip(),
            expected_agent_id=config["expected_agent_id"], timeout_seconds=float(self.timeout_seconds),
        )

    def call(self, *, purpose: str, request_id: str, prompt: str, mission: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``{text, replayed, cost_micros, cost_status, work_order_ref, ...}`` or raise."""
        # P13aa: the WorkOrder id is content-addressed on (purpose, request_id,
        # mission version, prompt) and deliberately excludes the clock -- but
        # the scheduler hashes the whole wire, which carried a wall-clock
        # created_at. So asking the identical question a second time produced
        # the same idempotency key with a different hash, which is the
        # scheduler's definition of a conflict: "this request is bound to
        # different content; ask again". Asking again could never help.
        #
        # Live, every Initial Screen section had been failing that way since
        # 2026-09-07 -- eight sections, every run, for two days, on a
        # deliverable whose inputs were by then complete.
        #
        # The timestamp is therefore derived from the same thing the id is: the
        # mission version this work belongs to. The scheduler still records its
        # own insertion time, so nothing loses the real clock; what goes into
        # the identity now agrees with the identity. (The SEC lane froze its
        # perception snapshot's generated_at for exactly this reason.)
        created_at = mission.get("created_at") or self.clock().isoformat(timespec="microseconds")
        work = build_work(purpose=purpose, request_id=request_id, prompt=prompt, mission_version_ref=mission["id"],
                          max_input_tokens=self.max_input_tokens, max_output_tokens=self.max_output_tokens,
                          max_cost_usd=self.max_cost_usd, max_seconds=self.timeout_seconds,
                          created_at=created_at)
        scope = {"mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
                 "mission_version_hash": mission["content_hash"],
                 "max_daily_paid_calls": int(mission["budget"]["max_daily_paid_calls"]),
                 "max_daily_cost_micros": int(Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000)}
        # P13s: the lease has to outlast the call it covers. The scheduler's
        # default lease is 30 s and its ceiling 60; a cockpit call is allowed
        # up to its own timeout, and a reasoning model on a large prompt takes
        # longer than either. When the lease lapsed mid-call the completion was
        # refused with "attempt is not the current leased attempt" -- the work
        # was done and paid for, and the answer was thrown away.
        lease_seconds = float(self.timeout_seconds) + _LEASE_GRACE_SECONDS
        # The lease bounds are a frozen versioned policy: the same
        # policy_version_id with different settings is a conflict, and the
        # shared "scheduler-policy-0.1" is sized for calls that finish in
        # seconds. So this names its own version after the bound it needs --
        # the id and the settings can never disagree, and the lanes that are
        # fast keep the policy they have.
        with Scheduler(
            self.scheduler_db,
            policy_version_id=f"scheduler-policy-lease-{int(lease_seconds)}s-0.1",
            max_lease_seconds=lease_seconds,
            max_total_lease_seconds=lease_seconds * 2,
        ) as scheduler:
            if scheduler.enqueue(work)["status"] == "conflict":
                raise CockpitModelError("this request is bound to different content; ask again")
            formal = scheduler.formal_result(work.id)
            replayed = formal is not None
            cost_micros, cost_status = 0, "replayed"
            if formal is None:
                lease = scheduler.claim(WORKER_REF, work_order_id=work.id,
                                        lease_seconds=lease_seconds)
                if lease is None:
                    raise CockpitModelError("this request is already running")
                attempt = lease["attempt"]["attempt_number"]
                with ModelRouter(self.config["model_router_db"]) as router, \
                        ThesisImpactBudgetStore(self.config["budget_db"]) as budget:
                    prompt_bytes = len(prompt.encode("utf-8"))
                    route = router.route(
                        work, attempt_number=attempt, capability="research",
                        policy_version_ref=self.config["routing_policy_ref"],
                        credential_slot_refs=self.config["credential_slot_refs"], required_modalities=("text",),
                        required_context_tokens=prompt_bytes + self.max_output_tokens,
                        estimated_input_tokens=prompt_bytes, estimated_output_tokens=self.max_output_tokens,
                        idempotency_key=f"cockpit-route:{work.id}:{attempt}",
                    )["decision"]
                    result: ResultEnvelope
                    if route["outcome"] != "selected":
                        result = _failure(work, "MODEL_ROUTE_REJECTED", route["id"])
                        failure = "no model route is available right now"
                    else:
                        profile = router.get_profile(route["selected_profile_version_ref"])
                        reserved = int(Decimal(str(self.max_cost_usd)) * 1_000_000)
                        try:
                            admission = budget.admit(
                                policy_version_id=self.config["budget_policy_ref"],
                                day=self.clock().astimezone(timezone.utc).date().isoformat(),
                                work_order_ref=work.id, attempt_number=attempt, phase="assessment",
                                route_decision_ref=route["id"], reserved_micros=reserved, mission_binding=scope,
                            )
                        except ThesisImpactBudgetError as exc:
                            admission = None
                            result = _failure(work, "BUDGET_REFUSED", route["id"])
                            failure = f"today's research budget refused the call: {exc}"
                        if admission is not None:
                            try:
                                invocation, result = self._adapter(router).execute(work, route, profile)
                                cost_micros, cost_status = _cost_micros(invocation, route, profile, reserved)
                                failure = None
                            except OpenClawModelAdapterError as exc:
                                result = _failure(work, "MODEL_ADAPTER_REJECTED_OR_FAILED", route["id"])
                                cost_micros, cost_status = 0, "failed"
                                failure = f"the model call failed: {exc}"
                            budget.settle(admission["admission_id"], actual_micros=cost_micros)
                    completion = scheduler.complete(work.id, attempt, WORKER_REF, lease["lease_token"], result,
                                                    idempotency_key=f"cockpit-complete:{work.id}:{attempt}")
                    if completion["status"] == "conflict":
                        raise CockpitModelError("the request completion conflicted; ask again")
                    if failure is not None:
                        raise CockpitModelError(failure)
                formal = scheduler.formal_result(work.id)
            if formal is None or formal["terminal_state"] != "succeeded":
                raise CockpitModelError("the model call did not succeed")
            envelope = formal["result_envelope"]
            text = envelope.get("outputs", {}).get("text")
            if not isinstance(text, str):
                raise CockpitModelError("the model returned no text")
            return {"text": text, "replayed": replayed, "cost_micros": cost_micros, "cost_status": cost_status,
                    "work_order_ref": work.id, "result_envelope_ref": formal["result_envelope_id"],
                    "invocation_ref": envelope.get("invocation_ref"),
                    "route_decision_ref": envelope.get("metadata", {}).get("route_decision_ref")}


def unwrap_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort: the first JSON object in a model reply, fence or prose around it tolerated."""
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        candidates.insert(0, body.strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


__all__ = ["CockpitModel", "CockpitModelError", "PURPOSES", "WORKER_REF", "build_work", "unwrap_json_object"]
