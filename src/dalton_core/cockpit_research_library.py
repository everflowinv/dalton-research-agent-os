"""Read published research without initializing or writing its authorities."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping

from .company_dossier import (CompanyDossierAuthority, CompanyDossierError,
                              dossier_completeness, section_body)
from .coverage_mission import (CoverageMissionAuthority, CoverageMissionError,
                               validate_mission_stage_record)
from .debate_map import DebateMapAuthority, DebateMapError
from .industry_framework import (IndustryFrameworkAuthority, IndustryFrameworkError,
                                 deliverable_sections)
from .store import content_hash


def _comparison_table_display(title: Any, body: Any,
                              numbers: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Render every cell of the closed governed comparison TSV for the UI."""
    from .numeric_display import format_typed_value
    from .research_html_export import _structured_comparison_display
    from .cockpit_plane import claim_period_display_label

    checked = _structured_comparison_display(str(title or ""), body, numbers)
    if checked is None:
        return None
    raw = str(body or "")
    lines = raw.splitlines()
    boundary = next((index for index, line in enumerate(lines[1:], 1)
                     if not line or line.startswith("# ")), len(lines))
    cells = [line.split("\t") for line in lines[:boundary]]
    if not cells:
        return None
    metric_keys = {
        "revenue": ("营业收入", "amount"),
        "revenue_yoy_growth": ("营业收入同比增速", "percent"),
        "gross_margin": ("毛利率", "percent"),
        "operating_margin": ("营业利润率", "percent"),
        "营业收入": ("营业收入", "amount"),
        "营业收入同比增速": ("营业收入同比增速", "percent"),
        "毛利率": ("毛利率", "percent"),
        "营业利润率": ("营业利润率", "percent"),
    }
    headers = ["公司", "指标"] + [claim_period_display_label(value) or value
                                  for value in cells[0][2:]]
    rows = []
    for row in cells[1:]:
        label_kind = metric_keys.get(row[1])
        if label_kind is None:
            return None
        label, kind = label_kind
        shown = []
        for value in row[2:]:
            if value == "-":
                shown.append("—")
            elif kind == "amount" and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                shown.append(format_typed_value(
                    value, unit="usd", scale="one", currency="USD", metric="revenue"))
            elif kind == "percent" and re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", value):
                shown.append(format_typed_value(
                    value[:-1], unit="percent", scale="one", metric=row[1]))
            else:
                return None
        rows.append([row[0], label, *shown])
    return {"headers": headers, "rows": rows, "note": checked[0], "original": checked[1]}


def _reader(connection: sqlite3.Connection, authority: type) -> Any:
    instance = authority.__new__(authority)
    instance.connection = connection
    return instance


def _sections(kind: str, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    if kind == "dossier":
        blocks = [(s["aspect"], s) for s in record.get("sections", [])]
        blocks += [(key, record[key]) for key in ("industry_classification", "variant_view")
                   if record.get(key)]
        return [{"title": title, "body": section_body(block),
                 "sources": list(block.get("sources") or []),
                 "gaps": list(block.get("gaps") or []) +
                         ([block["reason"]] if block.get("reason") else [])}
                for title, block in blocks]
    if kind == "debate_map":
        sections = []
        for debate in record.get("debates", []):
            for key, label in (("bull_position", "多方"), ("bear_position", "空方"),
                               ("market_position", "市场看法"), ("our_position", "我们的判断")):
                position = debate.get(key) or {}
                sections.append({"title": f"{debate['question']} · {label}",
                                 "body": position.get("statement") or "尚未形成判断",
                                 "sources": list(position.get("claim_refs") or position.get("refs") or []),
                                 "position": ({key: position.get(key) for key in
                                               ("available", "lean", "state", "side")
                                               if key in position}),
                                 "gaps": [], "debate_status": debate.get("status"),
                                 "last_shift_reason": debate.get("last_shift_reason")})
        return sections
    sections = deliverable_sections(record) if kind == "industry_framework" else record.get("sections", [])
    return [{"title": s["title"], "body": s.get("body") or "",
             "sources": list(s.get("claim_refs") or []),
             "numbers": list(s.get("numbers") or []),
             "gaps": list(s.get("gaps") or [])} for s in sections]


def research_library(connection: sqlite3.Connection, mission: Mapping[str, Any],
                     company_ref: str, *, localize: bool = True) -> dict[str, Any]:
    if company_ref not in {m["company_ref"] for m in mission["universe"]}:
        raise ValueError("company is outside the current research mission")
    products = []
    for kind, label, table in (
        ("dossier", "公司档案", "company_dossier_versions"),
        ("debate_map", "核心争议", "debate_map_versions"),
        ("industry_framework", "行业框架", "industry_framework_versions"),
        ("initial_screen", "初步筛选", "mission_deliverable_versions"),
        ("investment_memo", "投资备忘录", "mission_deliverable_versions"),
    ):
        subject_ref = mission["industry_ref"] if kind == "industry_framework" else company_ref
        item: dict[str, Any] = {"kind": kind, "label": label, "status": "missing",
                                "subject_ref": subject_ref,
                                "sections": [], "gaps": []}
        products.append(item)
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)).fetchone():
            item["reason"] = "尚未发布此类研究产物"
            continue
        try:
            if kind == "dossier":
                record = _reader(connection, CompanyDossierAuthority).latest(company_ref)
            elif kind == "debate_map":
                record = _reader(connection, DebateMapAuthority).current(company_ref)
            elif kind == "industry_framework":
                record = _reader(connection, IndustryFrameworkAuthority).latest(mission["industry_ref"])
            else:
                row = connection.execute(
                    "SELECT v.record_json,v.content_hash FROM mission_deliverable_pointer p "
                    "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
                    "WHERE v.kind=? AND v.subject_ref=? ORDER BY v.created_at DESC LIMIT 1",
                    (kind, company_ref),
                ).fetchone()
                record = None if row is None else json.loads(row["record_json"])
                if record is not None and not (
                    record.get("content_hash") == row["content_hash"] ==
                    content_hash({k: v for k, v in record.items() if k != "content_hash"})
                ):
                    raise ValueError("published document integrity check failed")
            if record is None:
                item["reason"] = "尚未发布此类研究产物"
                continue
            bound = record.get("mission_version_ref") or (record.get("bindings") or {}).get("mission_version_ref")
            item.update(status="available", version_ref=record["id"],
                        content_hash=record["content_hash"], created_at=record["created_at"],
                        mission_version_ref=bound,
                        mission_binding="current" if bound == mission["id"] else "historical",
                        sections=_sections(kind, record), gaps=list(record.get("gaps") or []))
            if kind == "dossier":
                item["completeness"] = dossier_completeness(record)
            if kind == "investment_memo":
                item["approval"] = _memo_approval(
                    connection, mission, company_ref, record)
            else:
                item["approval"] = {"status": "not_applicable"}
        except (ValueError, KeyError, TypeError, sqlite3.Error,
                CompanyDossierError, DebateMapError, IndustryFrameworkError,
                CoverageMissionError) as exc:
            item.update(status="invalid", reason=str(exc), sections=[], gaps=[])
    result = {"company_ref": company_ref, "industry_ref": mission["industry_ref"],
              "mission_version_ref": mission["id"], "products": products}
    if localize:
        from .research_localization_store import localize_library
        from .research_gap_display import display_metadata_text, gap_display_text
        # Display-only fields follow the exact-source receipt lookup. Raw
        # snapshots and the source hashes used by paid reviews stay stable.
        result = localize_library(connection, result)
        for product in result["products"]:
            product["display_gaps"] = [gap_display_text(gap) for gap in product.get("gaps", [])]
            for section in product.get("sections", []):
                comparison = _comparison_table_display(
                    section.get("title"), section.get("body"),
                    [row for row in section.get("numbers", []) if isinstance(row, Mapping)])
                section["display_body"] = display_metadata_text(
                    comparison["note"] if comparison is not None else section.get("body") or "")
                if comparison is not None:
                    section["display_comparison"] = {
                        "headers": comparison["headers"], "rows": comparison["rows"]}
                    section["display_body_technical"] = comparison["original"]
                section["display_gaps"] = [gap_display_text(gap) for gap in section.get("gaps", [])]
        return result
    return result


def _memo_approval(connection: sqlite3.Connection, mission: Mapping[str, Any],
                   company_ref: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Human decision for these exact memo bytes, never publication status."""
    if record.get("mission_version_ref") != mission.get("id"):
        return {"status": "historical", "decision_record_ref": None}
    state = _reader(connection, CoverageMissionAuthority).current_stage_state(
        mission["mission_ref"], company_ref)
    stage = state.get("stages", {}).get("investment_memo") or {}
    record_ref = stage.get("record_ref")
    if stage.get("status") not in {"gate_passed", "gate_failed"} or not record_ref:
        return {"status": "pending_human_decision", "decision_record_ref": None}
    row = connection.execute(
        "SELECT * FROM coverage_mission_stage_records WHERE record_id=?",
        (record_ref,),
    ).fetchone()
    if row is None:
        raise ValueError("memo stage decision record is missing")
    decision = validate_mission_stage_record(json.loads(row["record_json"]))
    columns = {
        "id": "record_id", "mission_version_ref": "mission_version_ref",
        "company_ref": "company_ref", "stage_ref": "stage_ref", "status": "status",
        "actor_ref": "actor_ref", "created_at": "created_at", "content_hash": "content_hash",
    }
    if (any(decision[key] != row[column] for key, column in columns.items())
            or decision["id"] != record_ref
            or decision["mission_version_ref"] != mission["id"]
            or decision["mission_version_hash"] != mission["content_hash"]
            or decision["company_ref"] != company_ref
            or decision["stage_ref"] != "investment_memo"
            or decision["status"] != stage["status"]
            or not decision["actor_ref"].startswith("human:")):
        raise ValueError("memo stage decision authority binding drifted")
    if record["id"] not in (decision.get("evidence_refs") or []):
        return {"status": "pending_human_decision", "decision_record_ref": None,
                "reason": "current stage decision belongs to another memo version"}
    return {
        "status": ("approved" if stage["status"] == "gate_passed" else "rejected"),
        "decision_record_ref": record_ref,
        "actor_ref": decision.get("actor_ref"),
        "decided_at": decision.get("created_at"),
    }
