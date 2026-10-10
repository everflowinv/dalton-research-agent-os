"""Cockpit v2: material status for one company (batch C).

``GET /v2/data/research/material-status?company=<ref|ticker>[&product=initial_screen]``

What the analyst's desk looks like for one product: the sufficiency checklist
(read-only: nothing is opened from a page load), the MaterialRequests open
for the company, the 「待消化」 count (arrivals since the last draft that have
not crossed a redraft threshold) and the redraft decision, and the last
material pack a lane drafted from (hash, time, size).

Read-only over Core, the evidence index and ``material-requests.sqlite``;
scoped to the active mission like the other evidence pages.  A blank
workspace is a 200 ``status: "empty"`` naming the state; only an unusable
request is a 400; no path answers 5xx.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .evidence_api import (
    NO_CORE,
    READ_FAILED,
    EvidenceRequestError,
    _Context,
    _empty,
    _empty_parts,
    _envelope,
    _json,
    _param,
    _with_scope,
)
from .evidence_search_api import OUTSIDE, _company

PATH = "/v2/data/research/material-status"
NO_COMPANY = _empty_parts("没有指定公司", "无法显示材料状态", "无需处理", "从公司页进入，或在地址里加上 company=代码")

#: product -> (table, version column, subject column, deliverable kind or None)
_LATEST: Mapping[str, tuple[str, str, str, str | None]] = {
    "initial_screen": ("mission_deliverable_versions", "version_id", "subject_ref", "initial_screen"),
    "investment_memo": ("mission_deliverable_versions", "version_id", "subject_ref", "investment_memo"),
    "earnings_preview": ("mission_deliverable_versions", "version_id", "subject_ref", "earnings_preview"),
    "dossier": ("company_dossier_versions", "version_id", "company_ref", None),
    "debate_map": ("debate_map_versions", "version_id", "subject_ref", None),
}


def last_draft(core: Any, company_ref: str, product: str) -> dict[str, Any] | None:
    """The product's newest version as a ``redraft_policy`` last_draft, or None."""

    from ..material_sufficiency import table_exists
    from ..redraft_policy import last_draft_from_version

    spec = _LATEST.get(product)
    if spec is None or core is None or not table_exists(core, spec[0]):
        return None
    table, version_column, subject_column, kind = spec
    sql = f"SELECT {version_column} AS version_ref, created_at, record_json FROM {table} WHERE {subject_column}=?"
    params: list[Any] = [company_ref]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    row = core.execute(sql + " ORDER BY version_number DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    gate = None
    if product == "initial_screen":
        try:
            from ..deliverable_reopen import passed_version

            passed = passed_version(core, company_ref=company_ref)
            gate = "gate_passed" if passed and passed["version_id"] == row["version_ref"] else None
        except Exception:  # noqa: BLE001 - no ladder: not passed
            gate = None
    return last_draft_from_version(json.loads(row["record_json"]), stage_status=gate,
                                   version_ref=row["version_ref"], created_at=row["created_at"])


def material_status_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    from .. import material_requests as mr
    from ..material_sufficiency import PRODUCTS, sufficiency
    from ..redraft_policy import evaluate

    product = _param(query, "product", "哪个产品？", required=False) or "initial_screen"
    if product not in PRODUCTS:
        raise EvidenceRequestError("不认识的产品：" + product)
    payload = _envelope("material_status")
    payload.update({"product": product, "sufficiency": None, "requests": [], "request_counts": {},
                    "pending_digest": None, "redraft": None, "last_pack": None})
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
        raw = _param(query, "company", "哪家公司？", required=False)
        if not raw:
            return _empty(payload, NO_COMPANY)
        company_ref, in_scope = _company(ctx, raw)
        if not in_scope:
            return _empty(payload, OUTSIDE)
        last = last_draft(core, company_ref, product)
    state_dir = ctx.state_dir
    payload["company"] = ctx.company(company_ref)
    check = sufficiency(state_dir, company_ref, product, record=False)
    payload["sufficiency"] = {
        "status": check["status"], "drafting_allowed": check["drafting_allowed"], "reason": check["reason"],
        "wait_until": check["wait_until"],
        "items": [{key: item.get(key) for key in ("key", "label", "satisfied", "state", "detail")}
                  for item in check["items"]],
        "missing": [{"key": item["key"], "label": item["label"], "detail": item["detail"],
                     "request": item.get("request")} for item in check["missing"]],
    }
    payload["requests"] = [
        {key: item.get(key) for key in ("id", "status", "status_label", "need", "period", "deadline", "overdue",
                                        "attempt_count", "created_at", "last_event_at")}
        | {"requesters": [r.get("lane") for r in item.get("requesters") or ()]}
        for item in mr.open_requests(state_dir, subject=company_ref, limit=50)]
    payload["request_counts"] = mr.status_counts(state_dir, subject=company_ref)
    decision = evaluate(state_dir, company_ref, product, last)
    fired = [reason for reason in decision["reasons"] if reason.get("fired")]
    payload["redraft"] = {"decision": decision["decision"],
                          "last_draft": None if last is None else {"version_ref": last.get("version_ref"),
                                                                   "created_at": last.get("created_at"),
                                                                   "gate_status": last.get("gate_status")},
                          "triggers": [{k: r.get(k) for k in ("code", "label", "detail")} for r in fired]}
    payload["pending_digest"] = {"count": decision.get("pending_count", 0),
                                 "arrivals_since_last_draft": decision.get("arrivals_since", 0),
                                 "by_kind": decision.get("by_kind") or {}, "label": "待消化",
                                 "note": ("新材料已达到重起草阈值，将在下一次批量重写中消化" if fired else
                                          "单条材料到达只登记为待消化；累积到阈值才重写")}
    payload["last_pack"] = mr.last_pack_build(state_dir, company_ref, product)
    payload["workspace"] = check.get("workspace")
    return payload


def _handler(builder: Callable[[Mapping[str, Any], Any], dict[str, Any]]
             ) -> Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]:
    def handle(query: Mapping[str, Any], context: Any) -> tuple[int, str, bytes]:
        from .charts_api import ChartRequestError

        try:
            return _json(200, builder(query or {}, context))
        except EvidenceRequestError as exc:
            return _json(400, {"error": "material_status_request", "message": str(exc), "display_message": str(exc)})
        except ChartRequestError:
            return _json(200, _empty(_envelope("material_status"), NO_CORE))
        except Exception as exc:  # noqa: BLE001 - never a 5xx: an honest empty state
            payload = _empty(_envelope("material_status"), READ_FAILED)
            payload["technical_reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return _json(200, payload)
    handle.__name__ = "material_status"
    return handle


def material_status_routes() -> dict[str, Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]]:
    return {PATH: _handler(material_status_payload)}


def register(router: Any) -> None:
    for path, handler in material_status_routes().items():
        router.add_get(path, handler)


__all__ = ["PATH", "last_draft", "material_status_payload", "material_status_routes", "register"]
