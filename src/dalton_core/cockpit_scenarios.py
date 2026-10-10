"""ADR-0012: the cockpit's 「情景与触发」 -- panel, approval card, owner revision.

The page side of ``company_scenario``.  Reads open the Core read-only and
degrade to nothing on a Core that never had the scenario lanes; every write
goes back out through the writer as the owner's ``human:`` principal
(ADR-0006: the control process holds no Core write handle).

- company card key ``scenarios``: what is in force (probability, every
  trigger and invalidator with its latest status, value, date and source) and
  any proposal waiting for a decision;
- approvals card ``company_scenario_set``: 采纳 / 驳回 / 暂缓 one proposal,
  bound to the set's hash;
- ``POST /v1/cockpit/scenario_revise``: move a probability or retire a
  scenario that is in force.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from .company_scenario import (
    STATUS_LABELS,
    company_view,
    condition_rule,
    condition_rule_technical,
    read_set_versions,
    read_sets,
    table_exists,
)

DROP_REASON_LABELS = {
    "already_met": "按最新值已经成立，不能当触发条件",
    "owner_without_horizon": "由你判断的条件没有写观察期限，未纳入",
}
SET_STATE_LABELS = {"pending": "等你裁决", "deferred": "你暂缓了，仍可裁决"}
DECISION_TITLES = {"accept": "采纳了一组情景假设", "reject": "驳回了一组情景假设",
                   "defer": "暂缓决定一组情景假设"}


def _cockpit() -> Any:
    from . import cockpit_plane

    return cockpit_plane


def card_views(core: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """company_ref -> the panel; {} on a Core without the tables."""

    if not table_exists(core, "company_scenario_sets"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        companies = [row[0] for row in core.execute(
            "SELECT DISTINCT company_ref FROM company_scenario_sets")]
        for company_ref in companies:
            view = company_view(core, company_ref)
            if view is not None:
                out[company_ref] = view
    except sqlite3.Error:
        return {}
    return out


def approval_items(core: sqlite3.Connection,
                   label: Callable[[str], str]) -> list[dict[str, Any]]:
    """One card per undecided automation proposal."""

    if not table_exists(core, "company_scenario_sets"):
        return []
    items: list[dict[str, Any]] = []
    for item in read_sets(core):
        if item["origin"] != "automation" or item["state"] not in SET_STATE_LABELS:
            continue
        versions = read_set_versions(core, item["id"])
        details: dict[str, Any] = {}
        technical: list[str] = []
        for version in sorted(versions, key=lambda v: -Decimal(v["probability"])):
            pct = int((Decimal(version["probability"]) * 100).to_integral_value())
            rules = [f"触发：{condition_rule(c)}" for c in version["triggers"]]
            rules += [f"否定：{condition_rule(c)}" for c in version["invalidators"]]
            details[f"{version['name']}（{pct}%）"] = [version["narrative"], *rules]
            technical.extend(f"{version['name']} {c['condition_id']}：{condition_rule_technical(c)}"
                             for c in [*version["triggers"], *version["invalidators"]])
        dropped = item.get("dropped_conditions") or []
        if dropped:
            details["已剔除的条件"] = [
                f"{row.get('key')} {row.get('condition_id')}："
                + DROP_REASON_LABELS.get(str(row.get("reason")), "已剔除")
                for row in dropped]
        if technical:
            details["规则原文（技术详情）"] = technical
        summary = "；".join(
            f"{v['name']} {int((Decimal(v['probability']) * 100).to_integral_value())}%"
            for v in sorted(versions, key=lambda v: -Decimal(v["probability"])))
        items.append({
            "kind": "company_scenario_set", "ref": item["id"], "hash": item["content_hash"],
            "at": item["created_at"],
            "title": ("是否采纳这组情景假设" if item["state"] == "pending"
                      else "是否采纳这组情景假设（你之前暂缓了）"),
            "who": label(item["company_ref"]),
            "summary": summary,
            "details": details,
            "actions": [
                {"decision": "accept", "label": "采纳"},
                {"decision": "reject", "label": "驳回"},
                {"decision": "defer", "label": "暂缓"},
            ],
            "needs_rationale": False,
            "rationale_hint": ("可选：写一句为什么。采纳后系统每轮检查这些触发条件，"
                               "命中时交给事件研判并提高这家公司的跟踪频率；"
                               "未采纳之前只记录观察，不触发任何动作。"),
        })
    return items


def decide_params(ref: str, digest: str, decision: str, rationale: str,
                  request_id: str) -> tuple[str, dict[str, Any], str]:
    if decision not in DECISION_TITLES:
        raise _cockpit().CockpitError("只能采纳、驳回或暂缓")
    return ("decide_company_scenario_set", {
        "set_ref": ref, "set_hash": digest, "decision": decision, "reason": rationale,
        "idempotency_key": f"cockpit-scenario:{ref}:{request_id}"}, DECISION_TITLES[decision])


def revise(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """「调整概率」/「停用这个情景」 on the company panel."""

    cockpit = _cockpit()
    from .cockpit_owner_levers import _reason, _request_id

    for name in ("scenario_ref", "version_ref"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise cockpit.CockpitError("请从公司卡片上的情景发起调整")
    digest = cockpit._sha(value.get("version_hash"), "version_hash")
    retire = bool(value.get("retire"))
    params: dict[str, Any] = {
        "scenario_ref": value["scenario_ref"].strip(), "version_ref": value["version_ref"].strip(),
        "version_hash": digest, "retire": retire, "reason": _reason(value),
        "idempotency_key": f"cockpit-scenario-revise:{_request_id(value)}"}
    if not retire:
        raw = value.get("probability")
        try:
            number = Decimal(str(raw).strip().rstrip("%"))
        except Exception as exc:  # noqa: BLE001 - any unreadable number is the same refusal
            raise cockpit.CockpitError("概率请写成 0 到 100 之间的数字") from exc
        if number > 1:
            number = number / 100
        params["probability"] = str(number)
    result = plane._governance(login, "revise_company_scenario", params,
                               failure="情景没有调整")
    plane.journal.record_event(
        kind="approval", title="停用了一个情景" if retire else "调整了一个情景的概率",
        detail=params["reason"], login=login,
        refs={"scenario_ref": params["scenario_ref"], "version_ref": result.get("id")})
    return result


__all__ = ["SET_STATE_LABELS", "STATUS_LABELS", "approval_items", "card_views",
           "decide_params", "revise"]
