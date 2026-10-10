"""The cockpit's owner levers (vision-gap #11 / #12, 2026-10-06).

The page side of ``owner_levers`` and ``coverage_list_change``.  Like every
other cockpit decision these go back out through the writer as the owner's
Tailscale-derived ``human:`` principal (ADR-0006: the control process holds no
Core write handle); the reads open the Core read-only.

- ``GET /v1/cockpit/levers``: what is paused, the emergency switch, who is
  prioritised, the recent receipts, and the companies / lanes / research lines
  the levers can act on -- all in the owner's words.
- ``POST /v1/cockpit/lever``: pause / resume a company or a lane (with a
  reason and an optional number of hours), emergency stop / resume all.
- ``POST /v1/cockpit/stop_line``: stop one research line.
- ``POST /v1/cockpit/prioritize``: "优先看这家" / back to the baseline.
- ``POST /v1/cockpit/coverage_draft``: the ``scope_expansion`` review card --
  the draft is held as an open cockpit draft and confirmed from 待办审批
  exactly like a goal or a steer.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any

from .owner_levers import (
    OwnerLeverValidationError, lever_state, paused_label, receipts, safe_ref, ttl_until,
)
from .store import content_hash

LEVER_LABELS = {
    "pause": "暂停", "resume": "恢复", "stop_line": "停止研究线",
    "emergency_stop": "紧急停机", "resume_all": "恢复全部运行",
    "prioritize": "优先看这家", "coverage_change": "调整覆盖名单",
}
OUTCOME_LABELS = {
    "applied": "已生效", "was_not_paused": "本来就没有暂停", "was_not_stopped": "本来就没有停机",
    "stopped": "已停止", "already_stopped": "之前已经停止", "refused": "没有执行",
    "published:prioritize": "已提高跟踪频率", "published:normal": "已恢复常规频率",
    "unchanged:prioritize": "频率本来就是优先档", "unchanged:normal": "频率本来就是常规档",
    "published": "已发布", "published_with_plan_errors": "已发布（部分检索计划未更新）",
}


def _cockpit() -> Any:
    from . import cockpit_plane

    return cockpit_plane


def _lane_rows() -> list[dict[str, str]]:
    from .cockpit_plane import REGISTRY_LANE_LABELS
    from .lane_registry import registered_lanes

    rows = [{"lane": "dispatch_coverage_mission_sec_lane", "label": "SEC 申报与公司事实"}]
    for spec in registered_lanes():
        key = spec.driver_key or spec.operation
        rows.append({"lane": spec.operation, "label": REGISTRY_LANE_LABELS.get(key) or key})
    return rows


def _open_lines(core: sqlite3.Connection, labels: Mapping[str, str]) -> list[dict[str, Any]]:
    try:
        rows = core.execute(
            "SELECT a.admission_id, a.company_ref, a.created_at FROM mission_document_research_admissions a "
            "LEFT JOIN mission_document_research_withdrawals w ON w.admission_ref = a.admission_id "
            "WHERE w.admission_ref IS NULL ORDER BY a.created_at DESC LIMIT 20").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"line_ref": row["admission_id"], "kind": "admission",
             "label": f"{labels.get(row['company_ref'], row['company_ref'].rsplit(':', 1)[-1])} 的专项文档研究",
             "created_at": row["created_at"]} for row in rows]


def _receipt_view(receipt: Mapping[str, Any], labels: Mapping[str, str],
                  lanes: Mapping[str, str]) -> dict[str, Any]:
    scope = receipt.get("scope_ref") or ""
    kind = receipt.get("scope_kind")
    target = {"company": labels.get(scope, scope.rsplit(":", 1)[-1]),
              "lane": lanes.get(scope, scope), "global": "全部研究",
              "line": "一条研究线", "mission": "覆盖名单"}.get(kind, scope)
    return {
        "id": receipt.get("id"), "at": receipt.get("created_at"),
        "lever": receipt.get("lever"), "lever_label": LEVER_LABELS.get(receipt.get("lever"), "操作"),
        "target": target, "reason": receipt.get("reason"), "until": receipt.get("until"),
        "outcome": receipt.get("outcome"),
        "outcome_label": OUTCOME_LABELS.get(receipt.get("outcome"), receipt.get("outcome")),
        "who": "你" if str(receipt.get("actor_ref") or "").startswith("human:") else "系统",
        "technical": {"receipt_ref": receipt.get("id"), "scope_ref": scope,
                      "refs": receipt.get("refs")},
    }


def levers_view(plane: Any) -> dict[str, Any]:
    cockpit = _cockpit()
    with plane._core() as core:
        state = lever_state(core, now=plane.clock())
        recent = receipts(core, limit=40)
        try:
            mission = plane._mission(core)
        except cockpit.CockpitMissionMissing:
            mission = None
        members = {} if mission is None else plane._members(mission)
        labels = {ref: plane._label(members, ref) for ref in members}
        lines = _open_lines(core, labels)
    lanes = _lane_rows()
    lane_labels = {row["lane"]: row["label"] for row in lanes}
    prioritised: dict[str, str] = {}
    for receipt in reversed(recent):
        if receipt.get("lever") == "prioritize" and str(receipt.get("outcome", "")).startswith("published"):
            prioritised[receipt["scope_ref"]] = str(receipt["outcome"]).split(":", 1)[-1]
    companies = []
    for ref, member in members.items():
        paused = state["companies"].get(ref)
        companies.append({
            "company_ref": ref, "label": labels[ref], "ticker": member.get("ticker"),
            "coverage_tier": member.get("coverage_tier"),
            "paused": paused is not None,
            "paused_label": None if paused is None else paused_label(paused),
            "prioritized": prioritised.get(ref) == "prioritize",
        })
    lane_rows = []
    for row in lanes:
        paused = state["lanes"].get(row["lane"])
        lane_rows.append({**row, "paused": paused is not None,
                          "paused_label": None if paused is None else paused_label(paused)})
    emergency = state.get("emergency")
    return {
        "enabled": True,
        "as_of": state["as_of"],
        "emergency": None if emergency is None else {
            "label": paused_label(emergency), "since": emergency.get("created_at"),
            "until": emergency.get("until"), "reason": emergency.get("reason"),
            "receipt_ref": emergency.get("id")},
        "banner": (None if emergency is None else
                   "紧急停机中：系统不再接新任务，也不再发起新的付费模型调用；"
                   "正在运行的子任务会做完或在恢复后接着做。" + paused_label(emergency)),
        "paused_count": len(state["companies"]) + len(state["lanes"]),
        "companies": companies, "lanes": lane_rows, "lines": lines,
        "mission_ref": None if mission is None else mission["mission_ref"],
        "mission_hash": None if mission is None else mission["content_hash"],
        "receipts": [_receipt_view(r, labels, lane_labels) for r in recent],
        "auto_resumed": [_receipt_view(r, labels, lane_labels) for r in state["expired"]][-10:],
    }


def _reason(value: Mapping[str, Any]) -> str:
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise _cockpit().CockpitError("请写一句理由：它会和这次操作一起存档")
    if len(reason.strip()) > 600:
        raise _cockpit().CockpitError("理由请控制在 600 字以内")
    return reason.strip()


def _request_id(value: Mapping[str, Any]) -> str:
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise _cockpit().CockpitError("缺少请求编号，请刷新页面后重试")
    return request_id.strip()


def apply_lever(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    cockpit = _cockpit()
    lever = value.get("lever")
    if lever not in {"pause", "resume", "emergency_stop", "resume_all"}:
        raise cockpit.CockpitError("不认识的操作")
    reason = _reason(value)
    params: dict[str, Any] = {"lever": lever, "reason": reason,
                              "idempotency_key": f"cockpit:{_request_id(value)}"}
    try:
        if lever in {"pause", "resume"}:
            kind = value.get("scope_kind")
            if kind not in {"company", "lane"}:
                raise cockpit.CockpitError("请选择要暂停的公司或研究流程")
            params.update({"scope_kind": kind, "scope_ref": safe_ref(value.get("scope_ref"), "scope_ref")})
        else:
            params.update({"scope_kind": "global", "scope_ref": "global"})
        if lever in {"pause", "emergency_stop"}:
            until = ttl_until(value.get("hours"), now=plane.clock())
            if until is not None:
                params["until"] = until
    except OwnerLeverValidationError as exc:
        raise cockpit.CockpitError(str(exc)) from exc
    result = plane._governance(login, "apply_owner_lever", params,
                               failure=f"{LEVER_LABELS[lever]}没有生效")
    receipt = result.get("receipt") or {}
    title = {"pause": "暂停了", "resume": "恢复了", "emergency_stop": "紧急停机",
             "resume_all": "恢复了全部运行"}[lever]
    target = ""
    if params["scope_kind"] == "company":
        target = "：" + str(params["scope_ref"]).rsplit(":", 1)[-1].upper()
    elif params["scope_kind"] == "lane":
        target = "：" + {row["lane"]: row["label"] for row in _lane_rows()}.get(
            params["scope_ref"], "一项研究流程")
    plane.journal.record_event(
        kind="lever", title=f"{title}{target}",
        detail=reason, login=login, refs={"receipt_ref": receipt.get("id"), "until": params.get("until")})
    return result


def stop_line(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    cockpit = _cockpit()
    try:
        line_ref = safe_ref(value.get("line_ref"), "line_ref")
    except OwnerLeverValidationError as exc:
        raise cockpit.CockpitError("请选择要停止的研究线") from exc
    reason = _reason(value)
    result = plane._governance(login, "stop_research_line", {
        "line_ref": line_ref, "reason": reason,
        "idempotency_key": f"cockpit:{_request_id(value)}"}, failure="研究线没有停止")
    plane.journal.record_event(
        kind="lever", title="停止了一条研究线" if result.get("status") in {"stopped", "already_stopped"}
        else "停止研究线没有执行", detail=reason, login=login,
        refs={"receipt_ref": (result.get("receipt") or {}).get("id"), "line_ref": line_ref})
    return result


def prioritize(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    cockpit = _cockpit()
    mode = value.get("mode") or "prioritize"
    if mode not in {"prioritize", "normal"}:
        raise cockpit.CockpitError("只能选择「优先看这家」或「恢复常规频率」")
    try:
        company_ref = safe_ref(value.get("company_ref"), "company_ref")
    except OwnerLeverValidationError as exc:
        raise cockpit.CockpitError("请选择公司") from exc
    reason = _reason(value)
    result = plane._governance(login, "prioritize_company", {
        "company_ref": company_ref, "mode": mode, "reason": reason,
        "idempotency_key": f"cockpit:{_request_id(value)}"}, failure="跟踪频率没有调整")
    plane.journal.record_event(
        kind="lever", title=("优先看：" if mode == "prioritize" else "恢复常规跟踪频率：")
        + company_ref.rsplit(":", 1)[-1].upper(),
        detail=reason, login=login,
        refs={"receipt_ref": (result.get("receipt") or {}).get("id")})
    return result


# -- the coverage list (scope_expansion) ----------------------------------------


def coverage_draft(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the writer what the change would do, and hold it as an open draft."""

    cockpit = _cockpit()
    add = value.get("add") or []
    remove = value.get("remove") or []
    if isinstance(add, str):
        add = [item for item in add.replace("，", ",").replace(" ", ",").split(",") if item.strip()]
    if not isinstance(add, list) or not isinstance(remove, list):
        raise cockpit.CockpitError("新增请填股票代码，移除请勾选公司")
    reason = _reason(value)
    _request_id(value)
    with plane._core() as core:
        mission = plane._mission(core)
    tier = value.get("coverage_tier") or "B"
    draft = plane._governance(login, "draft_coverage_change", {
        "mission_ref": mission["mission_ref"],
        "add": [{"ticker": str(item).strip(), "coverage_tier": tier} for item in add],
        "remove": [str(ref) for ref in remove],
    }, failure="覆盖名单草案没有生成")
    draft = dict(draft)
    draft["reason"] = reason
    unresolved = [entry for entry in draft.get("additions") or ()
                  if entry.get("resolution") not in {"resolved", "already_covered"}]
    draft["summary"] = _coverage_summary(draft)
    draft["warnings"] = [entry.get("error") for entry in unresolved if entry.get("error")]
    draft_id = f"cockpit-draft:coverage:{secrets.token_hex(8)}"
    digest = content_hash(draft)
    now = cockpit._iso(plane.clock())
    plane.journal.write(
        "INSERT INTO cockpit_drafts(draft_id,kind,login,input_text,draft_json,content_hash,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (draft_id, "coverage", login, reason, json.dumps(draft, ensure_ascii=False, sort_keys=True),
         digest, "open", now, now))
    return {"draft_id": draft_id, "draft_hash": digest, "kind": "coverage", "draft": draft,
            "created_at": now}


def _coverage_summary(draft: Mapping[str, Any]) -> str:
    added = [f"{entry['ticker']}（{entry.get('name') or '名称待确认'}）"
             for entry in draft.get("additions") or () if entry.get("resolution") == "resolved"]
    removed = [entry.get("ticker") or entry.get("company_ref") for entry in draft.get("removals") or ()]
    parts = []
    if added:
        parts.append("新增 " + "、".join(added))
    if removed:
        parts.append("移除 " + "、".join(str(item) for item in removed))
    return "；".join(parts) or "没有可执行的名单调整"


def coverage_approval_item(row: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    """The ``scope_expansion`` review card in 待办审批."""

    details: dict[str, Any] = {}
    for index, entry in enumerate(draft.get("additions") or (), start=1):
        if entry.get("resolution") == "resolved":
            details[f"add_{index}"] = (f"{entry['ticker']} · {entry.get('name') or '名称未返回'} · "
                                       f"SEC CIK {entry.get('cik')} · 覆盖等级 {entry.get('coverage_tier')}")
        else:
            details[f"add_{index}"] = f"{entry.get('ticker')}：{entry.get('error') or '未能解析'}"
    for index, entry in enumerate(draft.get("removals") or (), start=1):
        details[f"remove_{index}"] = f"{entry.get('ticker') or entry.get('company_ref')}（移出覆盖名单）"
    for index, change in enumerate(draft.get("plans") or (), start=1):
        source = {"source:sec-edgar": "SEC 申报检索", "source:alphaengine": "AlphaEngine 研报/纪要检索",
                  "source:web-search": "网络检索"}.get(change.get("source_ref"), change.get("source_ref"))
        if change.get("empty"):
            text = "调整后没有公司可检索，这个来源会暂时不检索"
        else:
            text = f"新增 {len(change.get('added') or ())} 家，移除 {len(change.get('removed') or ())} 家"
            if change.get("skipped"):
                text += "；" + "；".join(item["why"] for item in change["skipped"])
        details[f"plan_{index}"] = f"{source}：{text}"
    budget = draft.get("budget") or {}
    share = budget.get("coverage_pool_usd_per_company") or {}
    details["budget"] = (
        f"每日上限不变（US${budget.get('max_daily_cost_usd')}，付费调用 {budget.get('max_daily_paid_calls')} 次）；"
        f"每家公司分到的研究预算约 US${share.get('before')} → US${share.get('after')}；"
        f"每天检索约 {budget.get('searches_per_day_delta'):+} 次")
    labels = {key: ("新增公司" if key.startswith("add_") else "移除公司" if key.startswith("remove_")
                    else "检索计划" if key.startswith("plan_") else "预算影响") for key in details}
    return {
        "kind": "draft:coverage", "ref": row["draft_id"], "hash": row["content_hash"],
        "at": row["created_at"],
        "title": ("待你确认的覆盖名单扩大" if draft.get("kind") == "scope_expansion"
                  else "待你确认的覆盖名单调整"),
        "who": "你", "summary": draft.get("summary") or "", "details": details,
        "detail_labels": labels,
        "change_note": "确认后发布为新一版研究目标；检索计划随之更新，下一轮起生效。"
                       + (" " + "；".join(draft.get("warnings") or ()) if draft.get("warnings") else ""),
        "actions": [{"decision": "publish", "label": "确认发布"}, {"decision": "discard", "label": "放弃"}],
        "needs_rationale": False,
    }


def publish_coverage(plane: Any, login: str, draft_id: str, draft: Mapping[str, Any]) -> dict[str, Any]:
    cockpit = _cockpit()
    with plane._core() as core:
        mission = plane._mission(core)
    based = draft.get("based_on") or {}
    if based.get("mission_version_ref") != mission["id"] or based.get("mission_version_hash") != mission["content_hash"]:
        raise cockpit.CockpitConflict("研究目标在草案之后变了，请重新生成覆盖名单草案")
    add = [{"ticker": entry["ticker"], "coverage_tier": entry.get("coverage_tier") or "B",
            "cik": entry.get("cik"), "name": entry.get("name")}
           for entry in draft.get("additions") or () if entry.get("resolution") == "resolved"]
    remove = [entry["company_ref"] for entry in draft.get("removals") or ()]
    if not add and not remove:
        raise cockpit.CockpitError("这份草案里没有可以发布的名单调整")
    result = plane._governance(login, "publish_coverage_change", {
        "mission_ref": mission["mission_ref"], "expected_mission_hash": mission["content_hash"],
        "add": add, "remove": remove, "reason": draft.get("reason") or "调整覆盖名单",
        "idempotency_key": draft_id,
    }, failure="覆盖名单没有发布")
    published = result.get("mission_version_ref")
    plane.journal.write(
        "UPDATE cockpit_drafts SET status='published', published_ref=?, updated_at=? WHERE draft_id=?",
        (published, cockpit._iso(plane.clock()), draft_id))
    plane.journal.record_event(
        kind="goal", title="调整了覆盖名单：" + str(draft.get("summary") or "")[:160],
        detail=draft.get("reason"), login=login,
        refs={"draft_id": draft_id, "mission_version_ref": published,
              "receipt_ref": (result.get("receipt") or {}).get("id")})
    return {"status": "published", "draft_id": draft_id, "mission_version_ref": published,
            "version": result.get("version"), "plan_errors": result.get("plan_errors") or []}


__all__ = [
    "apply_lever", "coverage_approval_item", "coverage_draft", "levers_view",
    "prioritize", "publish_coverage", "stop_line",
]
