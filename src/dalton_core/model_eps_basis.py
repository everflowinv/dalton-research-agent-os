"""Read-only EPS definition proof from an exact formal forecast model.

Income divided by shares, an EPS label, or a US-GAAP namespace alone cannot
establish the definition. A statutory diluted-EPS divide must have its exact
filed tie replayed and remain bound to the same projected formula.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .store import content_hash

RULE_REF = "rule:model-eps-basis:exact-filed-diluted-eps:1"


def unknown(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "basis": None, "proof": None, "reason": reason}


def model_eps_basis(record: Mapping[str, Any]) -> dict[str, Any]:
    """Prove GAAP only from a validated statutory EPS structure, never a label."""
    from .model_forecast_driver import is_structured_schema, validate_forecast_model

    try:
        wire = dict(record)
        wire.pop("status", None)  # publish() receipt status is outside the record.
        model = validate_forecast_model(wire)
    except Exception:
        return unknown("模型身份及报表结构证明未通过核验，EPS口径未知")
    if not is_structured_schema(model.get("schema_version")):
        return unknown("历史模型没有完整报表结构及申报EPS定义证明，EPS口径未知")
    structure = model["financial_statement_structure"]
    replay = model["financial_statement_structure_replay"]
    binding = model["forecast_structure_binding"]
    lines = {item["ref"]: item for item in structure.get("lines") or []}
    eps_lines = [item for item in lines.values() if item.get("role") == "diluted_eps"]
    if len(eps_lines) != 1:
        return unknown("模型没有唯一的稀释EPS定义，EPS口径未知")
    eps = eps_lines[0]
    formulas = [item for item in structure.get("formulas") or []
                if item.get("output_ref") == eps["ref"]]
    if len(formulas) != 1:
        return unknown("稀释EPS没有唯一的公司利润／股份公式，EPS口径未知")
    formula = formulas[0]
    numerator = lines.get(formula.get("numerator_ref")) or {}
    denominator = lines.get(formula.get("denominator_ref")) or {}
    if (formula.get("operator") != "divide"
            or formula.get("tie_out_concept") != "us-gaap:EarningsPerShareDiluted"
            or eps.get("statement") != "income"
            or numerator.get("role") != "diluted_eps_numerator"
            or denominator.get("role") != "diluted_weighted_average_shares"
            or denominator.get("kind") != "filed"
            or denominator.get("concept") != "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
            or numerator.get("statement") != "income"
            or denominator.get("statement") != "income"
            or not formula.get("evidence_refs")):
        return unknown("利润和股份未完整绑定申报GAAP稀释EPS定义，不能推断EPS口径")
    reports = [item for item in replay.get("formulas") or []
               if item.get("output_ref") == eps["ref"]]
    if (len(reports) != 1 or reports[0].get("status") != "validated"
            or reports[0].get("tie_mode") != "exact"
            or not reports[0].get("tested_periods")):
        return unknown("申报稀释EPS定义缺少精确历史验证，EPS口径未知")
    results = [item for item in model.get("results") or []
               if item.get("role") == "diluted_eps"]
    expected = {"operator": "divide", "numerator_ref": numerator["ref"],
                "denominator_ref": denominator["ref"]}
    try:
        result_formula = json.loads(results[0]["formula"]) if len(results) == 1 else None
    except (TypeError, ValueError, KeyError):
        result_formula = None
    if (result_formula != expected or results[0].get("unit") != eps.get("unit")):
        return unknown("预测EPS公式与已证明的申报定义不同，EPS口径未知")
    proof = {"rule_ref": RULE_REF, "model_version_ref": model["id"],
             "model_content_hash": model["content_hash"],
             "structure_ref": structure["structure_ref"],
             "structure_hash": structure["content_hash"],
             "structure_binding_hash": binding["content_hash"],
             "historical_replay_hash": content_hash(replay),
             "eps_line_ref": eps["ref"], "numerator_ref": numerator["ref"],
             "denominator_ref": denominator["ref"],
             "filed_eps_concept": formula["tie_out_concept"],
             "evidence_refs": formula["evidence_refs"],
             "tested_periods": reports[0]["tested_periods"]}
    proof["content_hash"] = content_hash(proof)
    # Company-specific proof stays separate: GAAP has a shared definition,
    # whereas adjusted/core require an independently held definition_ref.
    return {"status": "available", "basis": {"kind": "gaap"},
            "proof": proof, "reason": None}


def read_model_eps_basis(connection: Any, version_ref: str | None, *,
                         company_ref: str) -> dict[str, Any]:
    """Strong-read this exact version without initializing any write authority."""
    if connection is None or not version_ref:
        return unknown("没有持有正式模型，EPS口径未知")
    from .model_forecast_driver import ForecastModelAuthority
    try:
        authority = ForecastModelAuthority.__new__(ForecastModelAuthority)
        authority.connection = connection
        record = authority.model(version_ref)
    except Exception:
        return unknown("正式模型身份无法强读核验，EPS口径未知")
    if record["company_ref"] != company_ref:
        return unknown("正式模型不属于该公司，EPS口径未知")
    return model_eps_basis(record)
