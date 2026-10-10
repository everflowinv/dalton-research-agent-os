"""Cockpit v2: full-text evidence search over every acquired document (batch A).

``GET /v2/data/evidence/search?q=<words>[&company=<ref|ticker>][&kinds=a,b][&from=YYYY-MM-DD][&to=YYYY-MM-DD][&limit=n]``
``GET /v2/data/evidence/passages?document=<document ref>``
``GET /v2/data/evidence/passage?ref=<doc-passage:...>``

Read-only over ``document-index.sqlite`` (:mod:`evidence_index`), scoped to the
active mission the way :mod:`evidence_api` is: a company outside the mission is
an honest empty state, never another mission's documents.  A blank workspace
(no Core, no mission, or no index built yet) is a 200 ``status: "empty"`` that
names the state, what it affects, who handles it and what the analyst can do;
only an unusable request is a 400.  Nothing here writes, calls a model or
reaches the network, and no path answers 5xx.
"""

from __future__ import annotations

from ..terminology import label as _term

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

PREFIX = "/v2/data/evidence/"
SEARCH_PATH = PREFIX + "search"
PASSAGES_PATH = PREFIX + "passages"
PASSAGE_PATH = PREFIX + "passage"
MAX_LIMIT = 50

NO_INDEX = _empty_parts("这个研究环境还没有建立全文证据索引", "暂时不能按关键词检索原文段落",
                        "系统每个调度周期会自动为已获取的资料建立索引", "稍后再来检索，或先在公司页查看已有结论")
NO_MATCH = _empty_parts("索引里没有与这些词匹配的段落", "这次检索没有结果",
                        "无需处理", "换一个说法、去掉公司或日期筛选再试")
OUTSIDE = _empty_parts("这家公司不在当前研究项目中", "不显示它的资料",
                       "无需处理：可能是旧链接", f"从公司页的「{_term('company_section', 'evidence')}」重新进入")
NOT_INDEXED = _empty_parts("这段引用在本环境的索引里找不到", "无法显示被引用的原文",
                           "索引正在补建时会自动出现；一直如此请联系管理员", "稍后刷新再试")
TAMPERED = _empty_parts("这条引用与索引中的原文指纹不一致", "为避免误导，不显示这段文字",
                        "管理员核对引用来源", "回到引用它的结论，查看其他出处")


def _company(ctx: _Context, value: str | None) -> tuple[str | None, bool]:
    """``(company_ref, in_scope)`` for a ref or a ticker; ``(None, True)`` when absent."""

    if not value:
        return None, True
    if value in ctx.members:
        return value, True
    upper = value.strip().upper()
    for ref, member in ctx.members.items():
        if str(member.get("ticker") or "").strip().upper() == upper:
            return ref, True
    return value, False


def _public(item: Mapping[str, Any], ctx: _Context) -> dict[str, Any]:
    return {
        "passage_ref": item["passage_ref"],
        "kind": item["kind"], "kind_label": item["kind_label"],
        "title": item.get("title"), "house": item.get("house"), "speakers": item.get("speakers") or [],
        "date": item.get("date"), "period": item.get("period"), "heading": item.get("heading"),
        "url": item.get("url"),
        "companies": [ctx.company(ref) for ref in item.get("company_refs") or () if ctx.in_scope(ref)],
        "text": item.get("text"), "snippet": item.get("snippet"), "highlights": item.get("highlights") or [],
        "score": item.get("score"),
        "technical": {"document_ref": item["document_ref"], "start": item["start"], "end": item["end"],
                      "content_sha256": item.get("content_sha256"), "score_parts": item.get("score_parts"),
                      "company_refs": item.get("company_refs") or []},
    }


def _has_index(state_dir: Any) -> bool:
    from .. import evidence_index

    connection = evidence_index.open_index_readonly(state_dir)
    if connection is None:
        return False
    connection.close()
    return True


def _limit(query: Mapping[str, Any]) -> int:
    raw = _param(query, "limit", "最多显示多少条？", required=False) or "20"
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_LIMIT:
        raise EvidenceRequestError(f"参数 limit 应该是 1 到 {MAX_LIMIT} 之间的整数")
    return int(raw)


def search_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    from .. import evidence_index

    words = _param(query, "q", "要检索什么？例如「bookings FY27 guidance」")
    if len(words) > evidence_index.MAX_QUERY_CHARS:
        raise EvidenceRequestError("检索词太长")
    raw_kinds = _param(query, "kinds", "资料类型", required=False)
    kinds = [kind.strip() for kind in (raw_kinds or "").split(",") if kind.strip()]
    unknown = [kind for kind in kinds if kind not in evidence_index.KINDS]
    if unknown:
        raise EvidenceRequestError("不认识的资料类型：" + "、".join(unknown))
    date_from = _param(query, "from", "开始日期", required=False)
    date_to = _param(query, "to", "结束日期", required=False)
    limit = _limit(query)
    payload = _envelope("search")
    payload.update({"query": words, "results": [],
                    "kinds_available": [{"kind": kind, "label": evidence_index.KIND_LABELS[kind]}
                                        for kind in evidence_index.KINDS]})
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
    company_ref, in_scope = _company(ctx, _param(query, "company", "哪家公司？", required=False))
    if not in_scope:
        return _empty(payload, OUTSIDE)
    try:
        found = evidence_index.evidence_search(ctx.state_dir, words, company_ref=company_ref, kinds=kinds,
                                               date_from=date_from, date_to=date_to, limit=limit)
    except evidence_index.EvidenceIndexError as exc:
        raise EvidenceRequestError(f"检索条件不对：{exc}") from exc
    if found["status"] == "no_index":
        return _empty(payload, NO_INDEX)
    payload["company"] = ctx.company(company_ref) if company_ref else None
    payload["terms"] = found["terms"]
    if found["status"] != "ok":
        return _empty(payload, NO_MATCH)
    payload["results"] = [_public(item, ctx) for item in found["results"]]
    payload["technical"] = {"company_ref": company_ref, "kinds": kinds, "total_candidates": found["total_candidates"]}
    return payload


def passages_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    from .. import evidence_index

    document_ref = _param(query, "document", "要读哪一份资料？")
    payload = _envelope("passages")
    payload["passages"] = []
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
    if not _has_index(ctx.state_dir):
        return _empty(payload, NO_INDEX)
    items = evidence_index.passages_for_document(ctx.state_dir, document_ref)
    if not items:
        return _empty(payload, NOT_INDEXED)
    if not any(ctx.in_scope(ref) for item in items for ref in item.get("company_refs") or ()):
        return _empty(payload, OUTSIDE)
    payload["document"] = {key: items[0].get(key) for key in
                           ("kind", "kind_label", "title", "house", "date", "period", "url")}
    payload["passages"] = [_public(item, ctx) for item in items]
    payload["technical"] = {"document_ref": document_ref}
    return payload


def passage_payload(query: Mapping[str, Any], context: Any) -> dict[str, Any]:
    from .. import evidence_index

    ref = _param(query, "ref", "要看哪一段引用？")
    try:
        evidence_index.parse_passage_ref(ref)
    except evidence_index.PassageRefInvalid as exc:
        raise EvidenceRequestError("参数 ref 不是段落引用（doc-passage:…）") from exc
    payload = _envelope("passage")
    ctx = _Context(context)
    with ctx.reader.core() as core:
        early = _with_scope(payload, ctx, core)
        if early is not None:
            return early
    if not _has_index(ctx.state_dir):
        return _empty(payload, NO_INDEX)
    try:
        found = evidence_index.passage(ctx.state_dir, ref)
    except evidence_index.PassageTampered:
        return _empty(payload, TAMPERED)
    if found is None:
        return _empty(payload, NOT_INDEXED)
    if not any(ctx.in_scope(company) for company in found.get("company_refs") or ()):
        return _empty(payload, OUTSIDE)
    payload["passage"] = _public(found, ctx)
    return payload


Builder = Callable[[Mapping[str, Any], Any], dict[str, Any]]


def _handler(kind: str, builder: Builder) -> Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]:
    def handle(query: Mapping[str, Any], context: Any) -> tuple[int, str, bytes]:
        from .charts_api import ChartRequestError

        try:
            return _json(200, builder(query or {}, context))
        except EvidenceRequestError as exc:
            return _json(400, {"error": "evidence_request", "message": str(exc), "display_message": str(exc)})
        except ChartRequestError:
            return _json(200, _empty(_envelope(kind), NO_CORE))
        except Exception as exc:  # noqa: BLE001 - never a 5xx: an honest empty state
            payload = _empty(_envelope(kind), READ_FAILED)
            payload["technical_reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return _json(200, payload)
    handle.__name__ = f"evidence_{kind}"
    return handle


def evidence_search_routes() -> dict[str, Callable[[Mapping[str, Any], Any], tuple[int, str, bytes]]]:
    return {SEARCH_PATH: _handler("search", search_payload),
            PASSAGES_PATH: _handler("passages", passages_payload),
            PASSAGE_PATH: _handler("passage", passage_payload)}


def register(router: Any) -> None:
    for path, handler in evidence_search_routes().items():
        router.add_get(path, handler)


__all__ = ["PASSAGES_PATH", "PASSAGE_PATH", "SEARCH_PATH", "evidence_search_routes", "passage_payload",
           "passages_payload", "register", "search_payload"]
