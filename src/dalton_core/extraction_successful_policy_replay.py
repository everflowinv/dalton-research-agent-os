"""Replay a settled extraction only across irrelevant routing revisions.

The caller first constructs the current source/permission context. This helper
changes no authority: it follows a bounded exact policy lineage, rebuilds the
entire historical Work, and reads its closed successful execution and fees.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .call_budget import resolve_thinking_level
from .contracts import ExecutionInvocation, ModelInvocation, ResultEnvelope
from .extraction_execution_identity import execution_config_binding
from .model_fallback_chain import effective_chain
from .model_router import _policy_wire
from .planner_paid_output_recovery import _router_record
from .store import canonical_json, content_hash

MAX_PRIOR_POLICIES = 32
MAX_ADMISSIONS = 64
PURPOSES = frozenset({"document_extraction", "document_numeric_extraction",
                      "metric_discovery_extraction"})


def _context(base):
    result = {"id": "document-extraction-context:" + content_hash(base)[:32], **base}
    return {**result, "content_hash": content_hash(result)}


def current_policy_context(context, config, policy):
    """Secondary passes start with their own current routing authority."""
    base = {k: v for k, v in context.items() if k not in {"id", "content_hash"}}
    binding = dict(base["model_binding"])
    binding.pop("config_hash_contract_ref", None)
    binding.update(execution_config_binding(config))
    binding.update(routing_policy_ref=policy["policy_version_ref"],
                   routing_policy_hash=content_hash(policy))
    base["model_binding"] = binding
    return _context(base)


def purpose_semantics(policy, purpose):
    if purpose not in PURPOSES:
        raise ValueError("unsupported successful extraction replay purpose")
    checked = _policy_wire(policy)
    if checked != policy:
        raise ValueError("extraction routing policy is not its canonical authority")
    return {"filters": checked["filters"],
            "ordered_preferences": checked["ordered_preferences"],
            "chain": effective_chain(checked, purpose, tier="cheap"),
            "thinking_level": resolve_thinking_level(purpose, policy=checked),
            # The existing shared extraction worker currently routes all three
            # task contracts as document_extraction. Preserve that real route
            # as well as each task's own configured selection and Work budget.
            "worker_chain": effective_chain(checked, "document_extraction"),
            "worker_thinking_level": resolve_thinking_level("document_extraction", policy=checked)}


def _success(writer, router, budget, work, context, policy, purpose, current_policy):
    scheduler = writer._scheduler
    formal = scheduler.formal_result(work.id)
    if formal is None or formal["terminal_state"] != "succeeded":
        return False
    result = ResultEnvelope.from_dict(formal["result_envelope"])
    identity = {k: formal[col] for k, col in (
        ("id", "result_record_id"), ("work_order_id", "work_order_id"),
        ("attempt_number", "attempt_number"), ("result_envelope_id", "result_envelope_id"),
        ("result_envelope_hash", "result_envelope_hash"), ("terminal_state", "terminal_state"),
        ("created_at", "created_at"))}
    if (content_hash(identity) != formal["content_hash"]
            or content_hash(result.to_dict()) != formal["result_envelope_hash"]
            or result.id != formal["result_envelope_id"]
            or result.work_order_ref != work.id or result.status != "succeeded"
            or result.actual_side_effects or result.metadata.get("hermetic_fixture")
            or set(result.outputs) != {"text", "content_hash"}
            or hashlib.sha256(result.outputs["text"].encode()).hexdigest() != result.outputs["content_hash"]):
        raise ValueError("successful extraction formal result drifted")
    route = _router_record(router, "model_route_decisions", "decision_id",
                           "decision_json", "decision_hash", result.metadata["route_decision_ref"])
    profile = _router_record(router, "model_endpoint_profile_versions", "profile_version_ref",
                             "profile_json", "profile_hash", result.metadata["profile_version_ref"])
    if (route["id"] != result.metadata["route_decision_ref"]
            or profile["profile_version_ref"] != result.metadata["profile_version_ref"]
            or route["outcome"] != "selected" or route["work_order_ref"] != work.id
            or route["work_order_hash"] != content_hash(work.to_dict())
            or route["attempt_number"] != formal["attempt_number"]
            or route["policy_version_ref"] != policy["policy_version_ref"]
            or route["policy_hash"] != policy["content_hash"]
            or route["selected_profile_version_ref"] != profile["profile_version_ref"]
            or route["selected_profile_hash"] != profile["content_hash"]
            or route["thinking_level"] != resolve_thinking_level("document_extraction", policy=current_policy)):
        raise ValueError("successful extraction route binding drifted")
    # A changed endpoint/price/permission is not an unrelated policy revision.
    latest = router.connection.execute(
        "SELECT profile_version_ref FROM model_endpoint_profile_versions WHERE profile_id=? "
        "ORDER BY version DESC LIMIT 1", (profile["id"],)).fetchone()
    if latest is None or latest["profile_version_ref"] != profile["profile_version_ref"]:
        return False
    row = writer.store.connection.execute(
        "SELECT invocation_json FROM model_invocations WHERE invocation_id=?",
        (result.invocation_ref,)).fetchone()
    if row is None:
        raise ValueError("successful extraction invocation is absent")
    wire = json.loads(row["invocation_json"])
    invocation = ModelInvocation.from_dict({k: v for k, v in wire.items() if k != "invocation_id"})
    execution = ExecutionInvocation.from_model(invocation).to_dict()
    er = writer.store.connection.execute(
        "SELECT execution_json,content_hash FROM execution_invocations WHERE execution_id=?",
        (invocation.id,)).fetchone()
    if (canonical_json(wire) != row["invocation_json"]
            or er is None or er["execution_json"] != canonical_json(execution)
            or er["content_hash"] != content_hash(execution)
            or invocation.work_order_ref != work.id or invocation.side_effects
            or list(invocation.input_refs) != list(work.input_refs)
            or invocation.actor_ref != "runtime:openclaw-model-broker"
            or invocation.parent_ref != route["id"] or invocation.profile_ref != profile["profile_version_ref"]
            or any(getattr(invocation, k) != profile[p] for k, p in (
                ("provider", "provider"), ("model", "model"),
                ("model_family", "family"), ("runtime_ref", "adapter_ref")))):
        raise ValueError("successful extraction invocation binding drifted")
    rows = budget.connection.execute(
        "SELECT attempt_number,phase FROM thesis_impact_day_admissions WHERE work_order_ref=? "
        "ORDER BY attempt_number,phase LIMIT ?", (work.id, MAX_ADMISSIONS + 1)).fetchall()
    if not rows or len(rows) > MAX_ADMISSIONS:
        return False
    selected = False
    from .budget_pools import mission_pool_scope
    mission = writer.coverage_mission.mission(context["mission_version_ref"])
    expected_scope = {"mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
        "mission_version_hash": mission["content_hash"],
        "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
        "max_daily_cost_micros": int(Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1000000),
        "outer_budget": context["model_binding"]["outer_budget"],
        **mission_pool_scope(mission, operation="dispatch_document_extraction")}
    for row in rows:
        proof = budget.admission(work_order_ref=work.id, attempt_number=row["attempt_number"], phase=row["phase"])
        binding = proof["mission_binding"] or {}
        if (proof["settlement"] is None
                or binding != expected_scope
                or proof["admission"]["policy_version_id"] != context["model_binding"]["budget_policy_ref"]):
            return False
        admitted = _router_record(router, "model_route_decisions", "decision_id",
                                  "decision_json", "decision_hash", proof["admission"]["route_decision_ref"])
        if (admitted["id"] != proof["admission"]["route_decision_ref"]
                or admitted["work_order_ref"] != work.id
                or admitted["work_order_hash"] != content_hash(work.to_dict())
                or admitted["attempt_number"] != row["attempt_number"]):
            raise ValueError("successful extraction reservation route drifted")
        selected |= row["attempt_number"] == formal["attempt_number"]
    return selected


def reusable_successful_context(current, config, writer, router, budget, policy, *, purpose, builder):
    """Return only a fully rebuilt, settled success; never an execution permit."""
    work = builder(current, model_config=config)
    if writer._scheduler.work_order_authority(work.id) is not None:
        return None
    wanted = purpose_semantics(policy, purpose)
    prior = policy
    seen = {policy["policy_version_ref"]}
    for _ in range(MAX_PRIOR_POLICIES):
        ref = prior["prior_version_ref"]
        if ref is None:
            return None
        if ref in seen:
            raise ValueError("extraction routing history contains a cycle")
        seen.add(ref)
        older = _router_record(router, "model_routing_policy_versions", "policy_version_ref",
                               "policy_json", "policy_hash", ref)
        if (older["policy_version_ref"] != ref or older["id"] != prior["id"]
                or older["version"] != prior["version"] - 1):
            raise ValueError("extraction routing history is not an exact version chain")
        prior = older
        if purpose_semantics(older, purpose) != wanted:
            continue
        old_config = {**config, "routing_policy_ref": ref}
        candidate = current_policy_context(current, old_config, older)
        old_work = builder(candidate, model_config=config)
        authority = writer._scheduler.work_order_authority(old_work.id)
        if authority is None:
            continue
        if (authority["work_order"] != old_work.to_dict()
                or authority["work_order_hash"] != content_hash(old_work.to_dict())
                or old_work.question != work.question or dict(old_work.budget) != dict(work.budget)):
            raise ValueError("successful extraction historical Work differs")
        if _success(writer, router, budget, old_work, candidate, older, purpose, policy):
            return candidate
    return None
