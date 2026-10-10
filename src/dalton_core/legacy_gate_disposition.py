"""Historical gate copies point to the formal authority without rewriting hashes."""
from __future__ import annotations

import json
from typing import Any

from .store import content_hash


def legacy_gate_dispositions(core: Any, company_ref: str) -> list[dict[str, Any]]:
    tables = {row[0] for row in core.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"mission_deliverable_versions", "deep_insight_gate_versions"} <= tables:
        return []
    gates = {}
    for row in core.execute(
        "SELECT record_json,content_hash FROM deep_insight_gate_versions WHERE company_ref=? "
        "ORDER BY version_number DESC", (company_ref,)):
        gate = json.loads(row[0])
        if gate.get("content_hash") != row[1] or content_hash({k:v for k,v in gate.items() if k != "content_hash"}) != row[1]:
            continue
        mission = (gate.get("bindings") or {}).get("mission_version_ref")
        if mission:
            gates.setdefault(mission, gate)
    result = []
    from .deep_insight_gate import gate_sections
    for row in core.execute(
        "SELECT version_id,mission_version_ref,content_hash,created_at FROM mission_deliverable_versions "
        "WHERE kind='deep_insight_gate' AND subject_ref=? ORDER BY created_at DESC,version_id", (company_ref,)):
        gate = gates.get(row[1])
        result.append({
            "version_ref": row[0], "content_hash": row[2], "created_at": row[3],
            "status": "superseded" if gate else "historical_copy",
            "label": "旧版复核副本已由正式复核记录替代" if gate else "旧版复核副本，尚未找到同一任务书的正式记录",
            "superseded_by": gate["id"] if gate else None,
            "superseded_by_hash": gate["content_hash"] if gate else None,
            "replacement": ({"kind": "deep_insight_gate", "label": "正式深度研究复核记录",
                             "version_ref": gate["id"], "version": gate["version"],
                             "created_at": gate["created_at"], "sections": gate_sections(gate),
                             "gaps": [], "status": "available"} if gate else None),
        })
    return result
