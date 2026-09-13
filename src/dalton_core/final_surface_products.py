"""Read-only projections of human-facing Cockpit products outside the library."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def _has(connection: Any, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _section(title: str, *values: Any) -> dict[str, Any] | None:
    text = "\n".join(str(value).strip() for value in values
                     if isinstance(value, str) and value.strip())
    return None if not text else {"title": title, "body": text, "gaps": []}


def _product(kind: str, subject: str, version: str, binding_hash: str,
             sections: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    kept = [row for row in sections if row is not None]
    if not kept:
        return None
    value = {"kind": kind, "subject_ref": subject, "version_ref": version,
             "authority_content_hash": binding_hash, "status": "available",
             "sections": kept}
    value["content_hash"] = _hash(value)
    return value


def _record(row: Any) -> dict[str, Any]:
    value = json.loads(row["record_json"])
    if not isinstance(value, dict):
        raise ValueError("surface record is not an object")
    return value


def final_surface_products(connection: Any, mission: Mapping[str, Any],
                           company_ref: str) -> list[dict[str, Any]]:
    """Return only prose currently exposed by Cockpit for one covered company."""

    if company_ref not in {row.get("company_ref") for row in mission.get("universe") or []}:
        raise ValueError("company is outside the current research mission")
    products: list[dict[str, Any] | None] = []

    if _has(connection, "current_pointers") and _has(connection, "thesis_versions"):
        row = connection.execute(
            "SELECT v.version_id,v.content_hash,v.content_json FROM thesis_admission_candidates c "
            "JOIN thesis_admission_decisions d ON d.candidate_id=c.candidate_id "
            "JOIN thesis_versions v ON v.admission_decision_id=d.decision_id "
            "JOIN current_pointers p ON p.version_id=v.version_id "
            "WHERE c.company_ref=? AND v.version_number=1 "
            "ORDER BY p.updated_at DESC LIMIT 1", (company_ref,)
        ).fetchone()
        if row:
            body = json.loads(row["content_json"])
            products.append(_product("surface_thesis", company_ref, row["version_id"],
                                     row["content_hash"],
                                     [_section("当前投资论点", body.get("statement"))]))

    if _has(connection, "weekly_brief_issue_versions"):
        rows = connection.execute(
            "SELECT version_id,content_hash,record_json FROM weekly_brief_issue_versions "
            "ORDER BY version_number DESC"
        ).fetchall()
        for row in rows:
            body = _record(row)
            bindings = body.get("thesis_bindings") or []
            if not any(item.get("company_ref") == company_ref for item in bindings
                       if isinstance(item, Mapping)):
                continue
            statements = [item.get("statement") for item in bindings
                          if isinstance(item, Mapping) and item.get("company_ref") == company_ref]
            products.append(_product("surface_weekly_brief", company_ref, row["version_id"],
                                     row["content_hash"], [_section("本周观点", *statements)]))
            break

    for table, kind, id_col, fields, limit in (
        ("event_judgements", "surface_event_judgement", "judgement_id",
         ("because", "note"), 5),
        ("thesis_reflections", "surface_thesis_reflection", "reflection_id",
         ("what_we_expected", "what_happened", "why", "convergence_pathway"), 5),
    ):
        if not _has(connection, table):
            continue
        rows = connection.execute(
            f"SELECT {id_col},content_hash,record_json FROM {table} "
            f"WHERE company_ref=? ORDER BY created_at DESC,{id_col} DESC LIMIT ?",
            (company_ref, limit),
        ).fetchall()
        for row in rows:
            body = _record(row)
            products.append(_product(kind, company_ref, row[id_col], row["content_hash"],
                                     [_section("事件研判" if kind.endswith("judgement") else "观点复盘",
                                               *(body.get(field) for field in fields))]))

    if _has(connection, "deep_insight_gate_versions"):
        decision_join = (" LEFT JOIN deep_insight_gate_decisions d ON d.gate_version_ref=v.version_id "
                         "WHERE d.decision_id IS NULL AND v.company_ref=? "
                         if _has(connection, "deep_insight_gate_decisions")
                         else " WHERE v.company_ref=? ")
        rows = connection.execute(
            "SELECT v.version_id,v.content_hash,v.record_json FROM deep_insight_gate_versions v"
            + decision_join + "ORDER BY v.created_at DESC LIMIT 1", (company_ref,)).fetchall()
        for row in rows:
            body = _record(row)
            sections = []
            for item in body.get("answers") or []:
                if not isinstance(item, Mapping):
                    continue
                sentences = [sentence.get("text") for sentence in item.get("sentences") or []
                             if isinstance(sentence, Mapping)]
                unknown = item.get("unknown") or {}
                gaps = list(item.get("gaps") or [])
                if isinstance(unknown, Mapping):
                    gaps += [unknown.get("missing"), unknown.get("evidence_that_would_answer")]
                sections.append(_section(str(item.get("question") or item.get("question_ref")
                                                     or "深度认知"), *sentences, *gaps))
            products.append(_product("surface_deep_insight", company_ref, row["version_id"],
                                     row["content_hash"], sections))

    if _has(connection, "conviction_call_proposals"):
        join = (" LEFT JOIN conviction_call_decisions d ON d.proposal_ref=p.proposal_id "
                "WHERE d.decision_id IS NULL AND p.company_ref=? "
                if _has(connection, "conviction_call_decisions") else " WHERE p.company_ref=? ")
        rows = connection.execute(
            "SELECT p.proposal_id,p.content_hash,p.record_json FROM conviction_call_proposals p"
            + join + "ORDER BY p.created_at DESC LIMIT 1", (company_ref,)).fetchall()
        for row in rows:
            body = _record(row); variant = body.get("variant_view") or {}
            products.append(_product("surface_conviction", company_ref, row["proposal_id"],
                row["content_hash"], [_section("投资判断",
                    *((variant.get(key) or {}).get("statement") or (variant.get(key) or {}).get("reason")
                      for key in ("our_view", "market_view", "where_market_is_wrong")),
                    *(item.get("signal") for item in body.get("event_pathway") or []
                      if isinstance(item, Mapping)))]))

    if _has(connection, "research_cycle_reflection_versions"):
        row = connection.execute(
            "SELECT version_id,content_hash,record_json FROM research_cycle_reflection_versions "
            "WHERE mission_ref=? ORDER BY iso_week DESC,version_number DESC LIMIT 1",
            (mission["mission_ref"],)).fetchone()
        if row:
            body = _record(row); narrative = body.get("narrative") or {}
            products.append(_product("surface_cycle_reflection", mission["mission_ref"],
                row["version_id"], row["content_hash"],
                [_section(narrative.get("title") or "每周研究复盘", narrative.get("prose"),
                          *(body.get("policy_suggestions") or []))]))

    if _has(connection, "mission_deliverable_pointer") and _has(connection, "mission_deliverable_versions"):
        rows = connection.execute(
            "SELECT v.version_id,v.content_hash,v.record_json,v.kind FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE v.subject_ref=? AND v.kind IN ('earnings_preview','earnings_calibration')",
            (company_ref,)).fetchall()
        for row in rows:
            body = _record(row)
            products.append(_product("surface_" + row["kind"], company_ref, row["version_id"],
                row["content_hash"], [_section(item.get("title") or row["kind"], item.get("body"),
                                                 *(item.get("gaps") or []))
                                       for item in body.get("sections") or []
                                       if isinstance(item, Mapping)]))
    return [row for row in products if row is not None]


__all__ = ["final_surface_products"]
