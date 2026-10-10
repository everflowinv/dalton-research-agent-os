"""Cockpit v2: MaterialRequest status for the question panel (batch G) and what was tried (batch E).

``GET /v2/data/research/material-requests?ids=<id,id>`` or ``?company=<ref|ticker>[&status=open,partial,exhausted]``

The question page shows a 「正在获取：…」 chip for every MaterialRequest an
answer opened, and polls this to move the chip (待获取 → 已找到 / 部分找到 /
来源已用尽 / 已超时) and to offer 「用新材料续答」 once a request has material.

Batch E adds, per request, what the material acquisition service did: every
source-ladder attempt of the current round (source, query, outcome, calls,
cost, when), 「已尝试」, the documents that answered it (``material_arrived``),
the close reason and the round; ``status=`` filters the company listing; the
payload carries today's ``material_acquisition`` pool.  Every word an analyst
reads comes from the terminology registry (``material_source``,
``material_outcome``, ``material_need``) and passes through ``scrub``.

Read-only over ``material-requests.sqlite`` (:mod:`material_requests`), scoped
to the active mission's companies; a blank workspace is an honest 200 empty
state and no path answers 5xx.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from .evidence_api import (
    NO_CORE,
    READ_FAILED,
    EvidenceRequestError,
    _Context,
    _empty,
    _envelope,
    _json,
    _param,
    _with_scope,
)
from .evidence_search_api import OUTSIDE, _company

PATH = "/v2/data/research/material-requests"
MAX_IDS = 10
_ID_RE = re.compile(r"^material-request:[0-9a-f]{8,64}$")
#: What the page may show of a request: never the requester's run refs.
_PUBLIC = ("id", "status", "status_label", "subject", "period", "deadline", "overdue", "attempt_count",
           "created_at", "last_event_at", "terminal")


def _label(namespace: str, key: Any) -> str:
    from ..terminology import label

    return label(namespace, str(key), default=str(key))


def _attempt(event: Mapping[str, Any]) -> dict[str, Any]:
    from ..terminology import scrub

    detail = event.get("detail")
    budget = None
    if event.get("outcome") == "budget_blocked" and isinstance(detail, str):
        try:
            budget = json.loads(detail)
        except ValueError:
            budget = None
        detail = None
    return {
        "source": event.get("source"), "source_label": _label("material_source", event.get("source")),
        "outcome": event.get("outcome"), "outcome_label": _label("material_outcome", event.get("outcome")),
        "query": event.get("query"), "calls": int(event.get("calls") or 0),
        "cost_usd": f"{int(event.get('cost_micros') or 0) / 1_000_000:.2f}",
        "detail": scrub(detail) if isinstance(detail, str) else None, "budget": budget,
        "items": [{key: item.get(key) for key in ("document_ref", "kind", "title", "date", "passage_ref")
                   if item.get(key) is not None} for item in event.get("items") or ()][:10],
        "at": event.get("created_at"),
    }


def _ladder(view: Mapping[str, Any], arrivals: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch E's fields for one request (``view`` read with its events)."""

    from ..material_acquisition import _round_attempts, tried_sources
    from ..terminology import scrub

    need = view.get("need") or {}
    closes = [event for event in view.get("events") or () if event.get("event_kind") == "closed"]
    return {
        "need_label": _label("material_need", need.get("kind")),
        "round": view.get("round") or 1,
        "requester_lanes": sorted({str(r.get("lane")) for r in view.get("requesters") or ()
                                   if isinstance(r, Mapping) and r.get("lane")}),
        "source_priority": [{"source": s, "label": _label("material_source", s)}
                            for s in view.get("source_priority") or ()],
        "attempts": [_attempt(event) for event in _round_attempts(view)],
        "tried": [scrub(text) for text in tried_sources(view)],
        "close_reason": scrub(closes[-1].get("reason")) if closes else None,
        "arrived": [{key: row.get(key) for key in ("document_ref", "kind", "source", "passage_ref", "arrival_id",
                                                   "created_at")} for row in arrivals],
    }


def _public(item: Mapping[str, Any], ctx: _Context, arrivals: list[dict[str, Any]] | None = None
            ) -> dict[str, Any]:
    out = {key: item.get(key) for key in _PUBLIC}
    need = item.get("need") or {}
    out["need"] = {"kind": need.get("kind"), "doc_kinds": list(need.get("doc_kinds") or [])}
    out["company"] = ctx.company(item.get("subject")) if ctx.in_scope(item.get("subject")) else None
    out["resolved"] = item.get("status") in ("found", "partial")
    out.update(_ladder(item, arrivals or []))
    return out


def _arrivals_by_request(state_dir: Any, **where: Any) -> dict[str, list[dict[str, Any]]]:
    from ..material_acquisition import material_arrived

    out: dict[str, list[dict[str, Any]]] = {}
    for row in material_arrived(state_dir, limit=2000, **where):
        out.setdefault(str(row.get("request_id")), []).append(row)
    return out


def _pool(state_dir: Any) -> dict[str, Any]:
    from ..material_acquisition import pool_status

    pool = pool_status(state_dir)
    return {"label": "今日材料获取额度", "day": pool["day"],
            "spent_calls": pool["spent_calls"], "cap_calls": pool["cap_calls"],
            "spent_usd": f"{pool['spent_usd_micros'] / 1_000_000:.2f}",
            "cap_usd": f"{pool['cap_usd_micros'] / 1_000_000:.2f}"}


def material_requests_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    from .. import material_requests as mr

    payload = _envelope("material_requests")
    payload.update({"requests": [], "counts": {}})
    raw_status = _param(query, "status", "哪些状态？", required=False)
    statuses = None
    if raw_status:
        statuses = tuple(s.strip() for s in raw_status.split(",") if s.strip())
        unknown = [s for s in statuses if s not in mr.STATUSES]
        if unknown:
            raise EvidenceRequestError("不认识的状态：" + "、".join(unknown))
        payload["statuses"] = list(statuses)
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
    raw_ids = _param(query, "ids", "哪些材料请求？", required=False)
    raw_company = _param(query, "company", "哪家公司？", required=False)
    state_dir = ctx.state_dir
    if raw_ids:
        ids = [item.strip() for item in raw_ids.split(",") if item.strip()][:MAX_IDS]
        bad = [item for item in ids if not _ID_RE.fullmatch(item)]
        if bad:
            raise EvidenceRequestError("材料请求编号格式不对")
        for request_id in ids:
            item = mr.request(state_dir, request_id, with_events=True)
            if item is None or not ctx.in_scope(item.get("subject")):
                payload["requests"].append({"id": request_id, "status": None, "status_label": "找不到这条请求",
                                            "resolved": False})
                continue
            arrivals = _arrivals_by_request(state_dir, request_id=request_id).get(request_id, [])
            payload["requests"].append(_public(item, ctx, arrivals))
        payload["pool"] = _pool(state_dir)
        return payload
    if raw_company:
        company_ref, in_scope = _company(ctx, raw_company)
        if not in_scope:
            return _empty(payload, OUTSIDE)
        payload["company"] = ctx.company(company_ref)
        arrivals = _arrivals_by_request(state_dir, company_ref=company_ref)
        for item in mr.open_requests(state_dir, subject=company_ref, statuses=None, limit=500):
            if statuses is not None and item["status"] not in statuses:
                continue
            if len(payload["requests"]) >= 50:
                break
            full = mr.request(state_dir, item["id"], with_events=True) or item
            payload["requests"].append(_public(full, ctx, arrivals.get(str(item["id"]), [])))
        payload["counts"] = mr.status_counts(state_dir, subject=company_ref)
        payload["pool"] = _pool(state_dir)
        return payload
    raise EvidenceRequestError("请给出材料请求编号（ids）或公司（company）")


def _handler(builder: Callable[[Mapping[str, Any], Any], dict[str, Any]]
             ) -> Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]:
    def handle(query: Mapping[str, Any], context: Any) -> tuple[int, str, bytes]:
        from .charts_api import ChartRequestError

        try:
            return _json(200, builder(query or {}, context))
        except EvidenceRequestError as exc:
            return _json(400, {"error": "material_requests_request", "message": str(exc), "display_message": str(exc)})
        except ChartRequestError:
            return _json(200, _empty(_envelope("material_requests"), NO_CORE))
        except Exception as exc:  # noqa: BLE001 - never a 5xx: an honest empty state
            payload = _empty(_envelope("material_requests"), READ_FAILED)
            payload["technical_reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return _json(200, payload)
    handle.__name__ = "material_requests"
    return handle


def material_requests_routes() -> dict[str, Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]]:
    return {PATH: _handler(material_requests_payload)}


def register(router: Any) -> None:
    for path, handler in material_requests_routes().items():
        router.add_get(path, handler)


__all__ = ["PATH", "material_requests_payload", "material_requests_routes", "register"]
