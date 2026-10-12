"""A bounded bundle of independent settings errors, never financial corrections.

Only three rules are collected: annual shares, annual EPS and OCF base refs.
Source, arithmetic and historical validation remain the full parser's job.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from .store import canonical_json, content_hash

CONTRACT_REF = "model-spec-independent-settings-diagnostics:0.1"
ERROR_CODE = "independent_settings_bundle"
MAX_FINDINGS = 12
MAX_ROOT_REPAIR_RECORDS = 64


def _shape(value, schema):
    """Validate the actual closed output schema before following any refs."""
    if "oneOf" in schema:
        return sum(_shape(value, s) for s in schema["oneOf"]) == 1
    kind = schema.get("type")
    allowed = kind if isinstance(kind, list) else [kind]
    kinds = {"null": value is None, "object": isinstance(value, dict),
             "array": isinstance(value, list), "string": isinstance(value, str),
             "integer": type(value) is int,
             "number": type(value) in (int, float), "boolean": type(value) is bool}
    if kind is not None and not any(kinds.get(k, False) for k in allowed):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if (not set(schema.get("required", ())) <= set(value)
                or schema.get("additionalProperties") is False and not set(value) <= set(properties)
                or len(value) < schema.get("minProperties", 0)):
            return False
        return all(_shape(v, properties[k]) for k, v in value.items() if k in properties)
    if isinstance(value, list):
        return (schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
                and all(_shape(v, schema.get("items", {})) for v in value))
    if isinstance(value, str):
        return (schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
                and ("pattern" not in schema or re.search(schema["pattern"], value) is not None))
    if type(value) in (int, float):
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    return True


def _closed_refs(body):
    structure = body["financial_statement_structure"]
    lines = structure["lines"]
    refs = [v["ref"] for v in lines]
    if len(set(refs)) != len(refs):
        return False
    for name in ("revenue_drivers", "expense_lines", "operating_metrics"):
        named = [v["ref"] for v in body[name]]
        if len(set(named)) != len(named):
            return False
    driver_refs = {v["ref"] for v in body["revenue_drivers"]}
    if any(v.get("driver_ref") is not None and v["driver_ref"] not in driver_refs
           for v in body["expense_lines"]):
        return False
    formulas = structure["formulas"]
    outputs = [v["output_ref"] for v in formulas]
    if len(set(outputs)) != len(outputs) or not set(outputs) <= set(refs):
        return False
    graph = {}
    for f in formulas:
        inputs = ([v["line_ref"] for v in f["terms"]] if f["operator"] == "sum"
                  else [f["numerator_ref"], f["denominator_ref"]])
        if not set(inputs) <= set(refs):
            return False
        graph[f["output_ref"]] = inputs
    if any(v["kind"] == "derived" and v["ref"] not in graph for v in lines):
        return False
    visiting, done = set(), set()
    def acyclic(ref):
        if ref in done:
            return True
        if ref in visiting:
            return False
        visiting.add(ref)
        if not all(acyclic(v) for v in graph.get(ref, ())):
            return False
        visiting.remove(ref)
        done.add(ref)
        return True
    if not all(acyclic(ref) for ref in graph):
        return False
    roles = [v["role"] for v in body["cash_flow_companion"]["lines"]]
    return len(set(roles)) == len(roles)


def _cash_capability(structure, ref):
    lines = {v["ref"]: v for v in structure["lines"]}
    line = lines.get(ref)
    if (line is None or line["statement"] != "income" or line["period_kind"] != "duration"
            or re.fullmatch(r"[a-z]{3}", line["unit"].casefold()) is None):
        return False
    if line["kind"] == "filed" and line["forecast_method"] in {
            "quarterly_growth", "year_over_year_growth", "seasonal_factor", "share_of_line"}:
        return True
    try:
        from .cash_flow_derived_base import derived_base_is_forecastable
    except ImportError:
        return False
    return derived_base_is_forecastable(structure, ref)


def diagnose(body):
    """No edits; malformed/ambiguous dependencies never authorize a bundle."""
    from .company_model_spec import OUTPUT_SCHEMA
    report = {"contract_ref": CONTRACT_REF, "parent_body_hash": content_hash(body),
              "status": "blocked", "reason": "closed_shape_or_refs_unproved", "findings": []}
    if not _shape(body, OUTPUT_SCHEMA) or not _closed_refs(body):
        return report
    findings = []
    def add(rule, paths, choices):
        findings.append({"rule": rule, "paths": paths,
                         "original_values": [_at(body, p) for p in paths], "choices": choices})
    structure = body["financial_statement_structure"]
    lines_by_ref = {v["ref"]: v for v in structure["lines"]}
    formulas = {v["output_ref"]: v for v in structure["formulas"]}
    for i, line in enumerate(structure["lines"]):
        prefix = ["financial_statement_structure", "lines", i]
        if (line["role"] == "diluted_weighted_average_shares" and line["kind"] == "filed"
                and isinstance(line["concept"], str)
                and line["unit"].casefold() == "shares" and line["period_kind"] == "duration"
                and line["statement"] == "income" and line["annual_semantics"] != "direct_annual"):
            add("shares_annual_semantics", [prefix + ["annual_semantics"]], [["direct_annual"]])
        if (line["role"] == "diluted_eps" and line["statement"] == "income"
                and line["period_kind"] == "duration"
                and re.fullmatch(r"[a-z]{3}_per_share", line["unit"].casefold())
                and line["annual_semantics"] not in {"direct_annual", "annual_ratio"}):
            formula = formulas.get(line["ref"])
            if line["kind"] == "derived" and (not formula or formula["operator"] != "divide"
                    or lines_by_ref[formula["numerator_ref"]]["role"] != "diluted_eps_numerator"
                    or lines_by_ref[formula["denominator_ref"]]["role"] != "diluted_weighted_average_shares"):
                continue
            add("eps_annual_semantics", [prefix + ["annual_semantics"]], [["direct_annual", "annual_ratio"]])
    for i, line in enumerate(body["cash_flow_companion"]["lines"]):
        if (line["role"] == "operating_cash_flow" and line["concept"] is not None
                and line["forecast_method"] == "share_of_line"
                and not _cash_capability(structure, line["forecast_base_ref"])):
            prefix = ["cash_flow_companion", "lines", i]
            # Only the existing explicit-gap repair is authorized, never
            # substitution of revenue or another economic forecasting base.
            add("operating_cash_base_reference", [prefix + [field] for field in
                ("forecast_method", "forecast_base_ref", "because")], {
                    "forecast_methods": ["unavailable"],
                    "forecast_base_refs": [None],
                    "unavailable_base": None})
    try:
        from .cash_flow_derived_base import CONTRACT_REF as cash_contract
    except ImportError:
        cash_contract = "cash-flow-filed-base:legacy"
    return {**report, "status": "overflow" if len(findings) > MAX_FINDINGS else "complete",
            "reason": None, "cash_base_contract_ref": cash_contract,
            "finding_count": len(findings), "findings": findings if len(findings) <= MAX_FINDINGS else [],
            "overflow_findings_hash": content_hash(findings) if len(findings) > MAX_FINDINGS else None}


def _at(body, path):
    value = body
    for key in path:
        value = value[key]
    return copy.deepcopy(value)


def validate_parent(body, proof):
    from .company_model_spec import CompanyModelSpecError
    expected = diagnose(body)
    if (expected["status"] != "complete" or len(expected["findings"]) < 2
            or canonical_json(expected) != canonical_json(proof)):
        raise CompanyModelSpecError("independent diagnostic bundle differs from its exact parent")


def assert_only_allowed_changed(original, repaired, proof):
    from .company_model_spec import CompanyModelSpecError
    validate_parent(original, proof)
    frozen = copy.deepcopy(repaired)
    try:
        paths = []
        for finding in proof["findings"]:
            for path, old in zip(finding["paths"], finding["original_values"], strict=True):
                if tuple(path) in paths or _at(original, path) != old:
                    raise ValueError("ambiguous path")
                paths.append(tuple(path))
                _at(repaired, path)  # Existing fields only; no additions/deletions.
                target = frozen
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = copy.deepcopy(old)
            values = [_at(repaired, p) for p in finding["paths"]]
            if finding["rule"] in {"shares_annual_semantics", "eps_annual_semantics"}:
                if values[0] not in finding["choices"][0]:
                    raise ValueError("invalid annual choice")
            else:
                method, base, because = values
                if (not isinstance(because, str) or not because.strip() or len(because) > 400
                        or method != "unavailable" or base is not None):
                    raise ValueError("invalid cash choice")
        if canonical_json(frozen) != canonical_json(original):
            raise ValueError("whole body drift")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CompanyModelSpecError("bundle repair changed fields outside its exact allowed paths") from exc


def error_for(body):
    from .company_model_spec import CompanyModelSpecError
    proof = diagnose(body)
    if proof["status"] == "overflow":
        return CompanyModelSpecError("independent settings diagnostics exceed the fixed finding limit", proof=proof)
    if proof["status"] == "complete" and len(proof["findings"]) >= 2:
        return CompanyModelSpecError("independent settings errors: " + ", ".join(
            v["rule"] for v in proof["findings"]), code=ERROR_CODE, proof=proof)
    return None


def _row(connection, query, params):
    cursor = connection.execute(query, params)
    row = cursor.fetchone()
    return None if row is None else dict(zip((v[0] for v in cursor.description), row))


def strong_result(connection, work_ref):
    """Formal and receipt must close the same immutable envelope."""
    import json
    from .contracts import ResultEnvelope
    fr = _row(connection, "SELECT * FROM scheduler_formal_results WHERE work_order_id=?", (work_ref,))
    if fr is None:
        return None
    result = ResultEnvelope.from_dict(json.loads(fr["result_envelope_json"])).to_dict()
    formal = {k: fr[col] for k, col in (("id", "result_record_id"),
        ("work_order_id", "work_order_id"), ("attempt_number", "attempt_number"),
        ("result_envelope_id", "result_envelope_id"), ("result_envelope_hash", "result_envelope_hash"),
        ("terminal_state", "terminal_state"), ("created_at", "created_at"))}
    rr = _row(connection, "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?", (result["id"],))
    if rr is None:
        raise ValueError("repair result receipt missing")
    receipt = {k: rr[k] for k in ("result_envelope_id", "work_order_id", "attempt_number",
                                  "result_envelope_hash", "outcome", "created_at")}
    if (canonical_json(result) != fr["result_envelope_json"]
            or content_hash(result) != fr["result_envelope_hash"]
            or content_hash(formal) != fr["content_hash"] or content_hash(receipt) != rr["content_hash"]
            or result["work_order_ref"] != work_ref or fr["work_order_id"] != work_ref
            or rr["work_order_id"] != work_ref or fr["result_envelope_id"] != result["id"]
            or rr["result_envelope_hash"] != fr["result_envelope_hash"]
            or rr["result_envelope_json"] != fr["result_envelope_json"]
            or rr["attempt_number"] != fr["attempt_number"]
            or rr["outcome"] != result["status"] or fr["terminal_state"] != result["status"]):
        raise ValueError("repair formal/receipt/envelope hash differs")
    return {**fr, "result_envelope": result}


def _same_slot_no_send(connection, work, config):
    """Permit only strong no-send terminal proof with zero settled admissions.

    This is not recovery authorization. Cockpit's original route/capacity
    policy still decides whether a successor is due, its epoch and its budget.
    """
    from datetime import datetime, timezone
    from .cockpit_model import _proved_capacity_busy_terminal
    from .planner_paid_output_recovery import recovery_readers
    formal = strong_result(connection, work["id"])
    if formal is None:
        # Scheduler exhausts a retryable capacity result using two immutable
        # events, rather than writing a formal failed ResultEnvelope.
        terminal = _row(connection, "SELECT * FROM scheduler_attempt_events WHERE work_order_id=? "
                        "ORDER BY event_seq DESC LIMIT 1", (work["id"],))
        if (terminal is None or terminal["state"] != "failed"
                or terminal["reason"] != "retry_exhausted_after_retryable_result"):
            return False
        prior = _row(connection, "SELECT * FROM scheduler_attempt_events WHERE event_id=?",
                     (terminal["prior_event_id"],))
        if (prior is None or prior["state"] != "retryable" or prior["work_order_id"] != work["id"]
                or prior["attempt_number"] != terminal["attempt_number"]):
            return False
        for event in (terminal, prior):
            wire = {"schema_version": "0.1", "wire_version": "0.2", "id": event["event_id"],
                "created_at": event["created_at"], "work_order_ref": event["work_order_id"],
                "attempt_number": event["attempt_number"], "state": event["state"],
                "lease_ref": event["lease_revision_id"], "result_envelope_ref": event["result_envelope_id"],
                "result_envelope_hash": event["result_envelope_hash"], "reason": event["reason"],
                "not_before": event["not_before"], "prior_event_ref": event["prior_event_id"]}
            if event["wire_version"] != "0.2" or content_hash(wire) != event["content_hash"]:
                return False
        rr = _row(connection, "SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?",
                  (prior["result_envelope_id"],))
        if rr is None:
            return False
        import json
        from .contracts import ResultEnvelope
        result = ResultEnvelope.from_dict(json.loads(rr["result_envelope_json"])).to_dict()
        receipt = {k: rr[k] for k in ("result_envelope_id", "work_order_id", "attempt_number",
                                      "result_envelope_hash", "outcome", "created_at")}
        if (canonical_json(result) != rr["result_envelope_json"]
                or content_hash(result) != rr["result_envelope_hash"]
                or content_hash(receipt) != rr["content_hash"]
                or result["status"] != "retryable" or rr["outcome"] != "retryable"
                or result["id"] != rr["result_envelope_id"]
                or rr["result_envelope_id"] != prior["result_envelope_id"]
                or result["work_order_ref"] != work["id"] or rr["work_order_id"] != work["id"]
                or rr["attempt_number"] != terminal["attempt_number"]
                or prior["result_envelope_hash"] != rr["result_envelope_hash"]):
            return False
        formal = {"terminal_state": "failed", "result_envelope": result,
                  "attempt_number": terminal["attempt_number"]}
    if formal is None or formal["terminal_state"] != "failed" or config is None:
        return False
    result = formal["result_envelope"]
    metadata = result["metadata"]
    code = (result.get("error") or {}).get("code")
    route_ref = metadata.get("route_decision_ref")
    identity = {"work_order_ref": work["id"], "code": code, "route_ref": route_ref}
    suffix = content_hash(identity)[:32]
    if (metadata.get("control_plane_failure") is not True or metadata.get("dispatch_state") is not None
            or result["id"] != "result:cockpit-control-" + suffix
            or result["invocation_ref"] != "invocation:not-started:" + suffix
            or result["outputs"] or result["usage_refs"] or result["actual_side_effects"]
            or not (_proved_capacity_busy_terminal(formal)
                    or code == "MODEL_ROUTE_REJECTED" and not metadata.get("chain_failures"))):
        return False
    with recovery_readers(config["model_router_db"], config["budget_db"],
                          now=datetime.now(timezone.utc)) as (router, budget):
        if budget is None:
            return False
        if route_ref is not None:
            route = router.get_decision(route_ref)
            if (route["work_order_ref"] != work["id"] or route["work_order_hash"] != content_hash(work)
                    or route["attempt_number"] != formal["attempt_number"]):
                return False
        admissions = budget.connection.execute("SELECT attempt_number,phase FROM thesis_impact_day_admissions "
            "WHERE work_order_ref=? ORDER BY attempt_number,phase LIMIT 65", (work["id"],)).fetchall()
        if len(admissions) > 64 or code != "MODEL_ROUTE_REJECTED" and not admissions:
            return False
        for attempt, phase in admissions:
            proof = budget.admission(work_order_ref=work["id"], attempt_number=attempt, phase=phase)
            binding = proof.get("mission_binding") or {}
            if (proof["settlement"] is None or proof["settlement"]["actual_micros"] != 0
                    or binding.get("mission_version_ref") != work["metadata"].get("mission_version_ref")
                    or binding.get("mission_version_hash") != work["metadata"].get("mission_version_hash")):
                return False
            route = router.get_decision(proof["admission"]["route_decision_ref"])
            if (route["work_order_ref"] != work["id"] or route["work_order_hash"] != content_hash(work)
                    or route["attempt_number"] != attempt):
                return False
    return True


def repair_slot_used(connection, root_proof, *, exclude_work=None, same_binding=None, config=None):
    """Any recorded repair under this exact root consumes its existing slot.

    Terminal failure, in-flight and unknown-cost Works are not empty slots.
    Invalid authority fails closed. No results/fees are changed or inferred.
    """
    import json
    rows = connection.execute(
        "SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders "
        "WHERE work_order_id >= 'work:cockpit-model_spec-' AND work_order_id < 'work:cockpit-model_spec.' "
        "AND (json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? "
        "OR (json_extract(work_order_json,'$.metadata.structured_output_repair.root_original') IS NULL "
        "AND json_extract(work_order_json,'$.metadata.structured_output_repair.parent_text_sha256')=? "
        "AND json_extract(work_order_json,'$.metadata.structured_output_repair.state_hash')=? "
        "AND json_extract(work_order_json,'$.metadata.structured_output_repair.task_hash')=?)) "
        "LIMIT ?", (root_proof["work_order_ref"], (same_binding or {}).get("original_text_sha256"),
                      (same_binding or {}).get("state_hash"), (same_binding or {}).get("task_hash"),
                      MAX_ROOT_REPAIR_RECORDS + 1)).fetchall()
    if len(rows) > MAX_ROOT_REPAIR_RECORDS:
        raise ValueError("repair root history exceeds the bounded proof window")
    for work_ref, wire, digest in rows:
        if work_ref == exclude_work:
            continue  # The exact existing child may be replayed, never replaced.
        work = json.loads(wire)
        if canonical_json(work) != wire or content_hash(work) != digest:
            raise ValueError("repair slot Work hash differs")
        metadata = work.get("metadata") or {}
        binding = metadata.get("structured_output_repair") or {}
        if (metadata.get("purpose") != "model_spec"
                or binding.get("root_original") != root_proof):
            raise ValueError("repair slot root authority differs")
        if same_binding is not None and binding == same_binding and _same_slot_no_send(connection, work, config):
            continue
        return True
    return False
