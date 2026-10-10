"""Read-only coverage gaps shared by the company page and task list.

These are missing prerequisites, not inferred failures or new approval gates.
Only companies in a current mission are considered; old models remain visible.
"""
from __future__ import annotations

import json
from typing import Any


def model_coverage_status(core: Any) -> dict[str, dict[str, Any]]:
    if core is None:
        return {}
    tables = {row[0] for row in core.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"coverage_mission_pointer", "coverage_mission_versions"} <= tables:
        return {}
    models: dict[str, dict[str, Any]] = {}
    if "forecast_model_versions" in tables:
        for row in core.execute("SELECT record_json FROM forecast_model_versions ORDER BY created_at,version_number"):
            record = json.loads(row[0])
            models[str(record["company_ref"])] = record
    specs = ({row[0] for row in core.execute("SELECT DISTINCT company_ref FROM coverage_mission_company_model_specs")}
             if "coverage_mission_company_model_specs" in tables else set())
    filings = ({row[0] for row in core.execute("SELECT DISTINCT company_ref FROM coverage_mission_statement_filings")}
               if "coverage_mission_statement_filings" in tables else set())
    out: dict[str, dict[str, Any]] = {}
    missions = core.execute(
        "SELECT v.record_json,p.updated_at FROM coverage_mission_versions v "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=v.mission_version_id "
        "ORDER BY p.updated_at DESC,p.mission_ref")
    for row in missions:
        mission = json.loads(row[0])
        for member in mission.get("universe") or []:
            ref = member.get("company_ref")
            if not ref or ref in out:
                continue
            model = models.get(ref)
            reasons = []
            if model is None:
                if ref not in filings:
                    reasons.append({"code": "missing_filed_statements", "text": "已申报财务报表尚未入库，缺少可核验的历史数字"})
                if ref not in specs:
                    reasons.append({"code": "missing_model_spec", "text": "尚未完成模型设计，需要明确收入、成本和现金流的预测方法"})
                if "forecast_line" not in (mission.get("autonomy") or {}).get("may_write", []):
                    reasons.append({"code": "forecast_scope_not_granted", "text": "当前研究范围尚未包含财务预测"})
                if not reasons:
                    reasons.append({"code": "forecast_not_published", "text": "模型设计和申报资料已有记录，但还没有发布可读取的预测结果；需检查计算与校验进度"})
            out[ref] = {
                "status": "available" if model is not None else "blocked",
                "company_ref": ref, "model_version_ref": model.get("id") if model else None,
                "as_of": model.get("created_at") if model else row[1],
                "reasons": reasons,
                "label": (None if model else "该公司的预测模型还没有生成：" + "；".join(r["text"] for r in reasons) + "。"),
            }
    return out
