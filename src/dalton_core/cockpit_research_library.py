"""Read published research without initializing or writing its authorities."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .company_dossier import CompanyDossierAuthority, section_body
from .debate_map import DebateMapAuthority
from .industry_framework import IndustryFrameworkAuthority, deliverable_sections
from .store import content_hash


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
                                 "gaps": [], "debate_status": debate.get("status"),
                                 "last_shift_reason": debate.get("last_shift_reason")})
        return sections
    sections = deliverable_sections(record) if kind == "industry_framework" else record.get("sections", [])
    return [{"title": s["title"], "body": s.get("body") or "",
             "sources": list(s.get("claim_refs") or []),
             "numbers": list(s.get("numbers") or []),
             "gaps": list(s.get("gaps") or [])} for s in sections]


def research_library(connection: sqlite3.Connection, mission: Mapping[str, Any],
                     company_ref: str) -> dict[str, Any]:
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
        item: dict[str, Any] = {"kind": kind, "label": label, "status": "missing",
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
            # Publication is not a human stage decision. The reader makes no
            # claim that an available Memo has been approved.
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            item.update(status="invalid", reason=str(exc), sections=[], gaps=[])
    return {"company_ref": company_ref, "industry_ref": mission["industry_ref"],
            "mission_version_ref": mission["id"], "products": products}
