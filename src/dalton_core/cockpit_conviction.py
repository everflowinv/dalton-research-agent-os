"""The cockpit's side of Conviction v2 (ADR-0013).

Two things, both in the owner's language:

- the **proposal card** for a v0.2 call in 待办审批: the three components the
  policy weighs -- evidence confidence (with what it rests on), the scenario
  table with the expected return it implies, and where the street stands
  relative to us -- plus the threshold the call cleared.  Accept / reject /
  defer go out exactly as they do for a v1 call (``decide_conviction_call``).
- the **policy page** (``GET /v1/cockpit/conviction-policy``,
  ``POST /v1/cockpit/conviction_policy``): the policy in force, every
  published version, the code default and frozen v1 for reference, a
  side-by-side comparison of any two, and a form to publish the next version
  through the writer as the owner's ``human:`` principal (ADR-0006: the
  cockpit holds no Core write handle).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .conviction_call import POLICY_REFUSAL_LABELS
from .terminology import label as _term
from .conviction_policy import (
    CONFIDENCE_ORDER, DEFAULT_POLICY, DIRECTIONS, FIELD_LABELS, HORIZONS, THESIS_SCOPES,
    VALUE_LABELS, compare, default_effective, display_value, effective_policy,
    published_versions, v1_view,
)

ALIGNMENT_LABELS = {
    "same_direction": "与市场同向",
    "opposite_direction": "与市场相反",
    "market_view_unavailable": "市场看法无法确认（按同向处理）",
}
THRESHOLD_KIND_LABELS = {
    "general": "一般门槛", "same_direction": "同向门槛（更高）",
    "not_applicable": "回避不设回报门槛",
}
CONFIDENCE_LABELS = {"low": "低", "medium": "中", "high": "高"}


def _cockpit() -> Any:
    from . import cockpit_plane

    return cockpit_plane


def _pct(value: Any, *, signed: bool = True) -> str:
    if value is None:
        return "—"
    number = Decimal(str(value))
    text = f"{number:.1f}%"
    return ("+" + text) if signed and number > 0 else text


def v2_card(record: Mapping[str, Any]) -> dict[str, Any]:
    """The structured block the page draws for one v0.2 proposal."""

    evidence = record.get("evidence_confidence") or {}
    alignment = record.get("market_view_alignment") or {}
    market = alignment.get("market_view") or {}
    expected = record.get("expected_return") or {}
    gate = record.get("gate") or {}
    wrong = alignment.get("where_market_is_wrong") or {}
    return {
        "evidence": {
            "level": evidence.get("level"),
            "level_label": CONFIDENCE_LABELS.get(evidence.get("level"), "—"),
            "statement": evidence.get("statement"),
            "rests_on": (f"{len(evidence.get('thesis_refs') or [])} 条投资论点、"
                         f"{len(evidence.get('claim_refs') or [])} 条其他依据"),
            "thesis_refs": list(evidence.get("thesis_refs") or []),
            "claim_refs": list(evidence.get("claim_refs") or []),
        },
        "scenarios": [{
            "name": row.get("name"),
            "probability": f"{Decimal(str(row.get('probability'))) * 100:.0f}%",
            "payoff": _pct(row.get("payoff")),
            "trigger": row.get("trigger"),
            "held": bool(row.get("source_ref")),
        } for row in record.get("scenarios") or []],
        "expected_return": {
            "stock": _pct(expected.get("stock_percent")),
            "position": _pct(expected.get("position_percent")),
            "required": None if gate.get("required_percent") is None
            else _pct(gate.get("required_percent")),
            "threshold_kind": THRESHOLD_KIND_LABELS.get(gate.get("threshold_kind"), "—"),
            "line": (f"期望回报 {_pct(expected.get('position_percent'))}"
                     + (f"，高于{THRESHOLD_KIND_LABELS.get(gate.get('threshold_kind'), '门槛')}"
                        f" {_pct(gate.get('required_percent'))}"
                        if gate.get("required_percent") is not None else "")),
        },
        "alignment": {
            "alignment": alignment.get("alignment"),
            "label": ALIGNMENT_LABELS.get(alignment.get("alignment"), "—"),
            "market_view": market.get("statement") or market.get("reason"),
            "where_market_is_wrong": wrong.get("statement"),
        },
        "invalidators": [row.get("statement") for row in record.get("invalidators") or []],
        "valuation_as_of": (record.get("valuation") or {}).get("as_of"),
        "policy": {"ref": record.get("policy_ref"),
                   "label": (_term("policy", "default") if str(record.get("policy_ref", "")).endswith(
                       "code-default") else f"已发布策略 {record.get('policy_ref')}")},
    }


def v2_approval_item(row: Mapping[str, Any], record: Mapping[str, Any],
                     who: str) -> dict[str, Any]:
    """One v0.2 proposal as a 待办审批 card; the decision path is v1's."""

    cockpit = _cockpit()
    card = v2_card(record)
    direction = cockpit.CALL_DIRECTION_LABELS.get(row["direction"], row["direction"])
    return {
        "kind": "conviction_call", "ref": row["proposal_id"], "hash": row["content_hash"],
        "at": row["created_at"],
        "title": f"是否采纳这条投资建议：{direction}（{card['alignment']['label']}）",
        "who": who,
        "summary": (record.get("our_view") or {}).get("statement") or "",
        "details": {
            "证据置信度": card["evidence"]["level_label"],
            "投资期限": cockpit.CALL_HORIZON_LABELS.get(row["time_horizon"], row["time_horizon"]),
            "期望回报": card["expected_return"]["line"],
            "与市场看法": card["alignment"]["label"],
        },
        "detail_labels": {"证据置信度": "证据置信度", "投资期限": "投资期限",
                          "期望回报": "期望回报", "与市场看法": "与市场看法"},
        "conviction_v2": card,
        "actions": [
            {"decision": "accept", "label": "采纳"},
            {"decision": "reject", "label": "驳回"},
            {"decision": "defer", "label": "暂缓"},
        ],
        "needs_rationale": True,
        "rationale_hint": ("写清楚为什么采纳、驳回或暂缓。这句话会和决定一起存进判断记录，"
                           "每周回头看时会读到。"),
    }


# ---------------------------------------------------------------------------
# the policy page
# ---------------------------------------------------------------------------

def _labelled(entry: Mapping[str, Any]) -> dict[str, Any]:
    policy = entry.get("policy") or {}
    return {
        "version_ref": entry.get("version_ref") or entry.get("id"),
        "version": entry.get("version"),
        "source": entry.get("source", "published"),
        "source_label": {"code_default": f"{_term('policy', 'default')}（尚未发布任何版本）",
                         "published": "已发布", "frozen_v1": "v1（已冻结，仅供对照）"
                         }.get(entry.get("source", "published"), "已发布"),
        "created_at": entry.get("created_at"),
        "reason": entry.get("reason"),
        "who": "你" if str(entry.get("actor_ref") or "").startswith("human:") else None,
        "policy_hash": entry.get("policy_hash"),
        "policy": dict(policy),
        "fields": [{"field": field, "label": label,
                    "value": display_value(policy.get(field))}
                   for field, label in FIELD_LABELS.items() if field in policy],
    }


def policy_view(plane: Any, query: Mapping[str, str] | None = None) -> dict[str, Any]:
    query = query or {}
    with plane._core() as core:
        effective = effective_policy(core)
        versions = published_versions(core)
    entries: dict[str, dict[str, Any]] = {
        "default": {**default_effective()}, "v1": v1_view()}
    for version in versions:
        entries[version["id"]] = {"source": "published", "version_ref": version["id"],
                                  "version": version["version"],
                                  "policy": version["policy"],
                                  "policy_hash": version["policy_hash"],
                                  "created_at": version["created_at"],
                                  "actor_ref": version["actor_ref"],
                                  "reason": version["reason"]}
    left_key = query.get("left") or ("default" if versions else "v1")
    right_key = query.get("right") or (versions[-1]["id"] if versions else "default")
    if left_key not in entries or right_key not in entries:
        raise _cockpit().CockpitError("没有这个策略版本：请从列表里选择")
    return {
        "enabled": True,
        "effective": _labelled(effective),
        "versions": [_labelled(entries[version["id"]]) for version in reversed(versions)],
        "default": _labelled(entries["default"]),
        "v1": _labelled(entries["v1"]),
        "choices": [{"key": key, "label": (
            "v1（冻结）" if key == "v1" else _term("policy", "default") if key == "default"
            else f"第 {entries[key]['version']} 版")} for key in entries],
        "compare": {"left": left_key, "right": right_key,
                    "rows": compare(entries[left_key]["policy"], entries[right_key]["policy"])},
        "form": {
            "prior_version_ref": versions[-1]["id"] if versions else None,
            "values": dict(effective["policy"]),
            "options": {
                "min_evidence_confidence": [{"value": v, "label": VALUE_LABELS[v]}
                                            for v in CONFIDENCE_ORDER],
                "allowed_horizons": [{"value": v, "label": VALUE_LABELS[v]} for v in HORIZONS],
                "allowed_directions": [{"value": v, "label": VALUE_LABELS[v]}
                                       for v in DIRECTIONS],
                "thesis_scope": [{"value": v, "label": VALUE_LABELS[v]} for v in THESIS_SCOPES],
            },
            "labels": dict(FIELD_LABELS),
        },
        "refusal_labels": dict(POLICY_REFUSAL_LABELS),
        "explainer": ("v2 把一条投资建议拆成三部分分别判断：证据置信度（依据有多扎实）、"
                      "情景表（每种情景的概率、回报和可观察的触发条件，由此算出期望回报）、"
                      "与市场看法的关系。与市场同向不再自动驳回，但期望回报必须越过更高的同向门槛。"),
    }


def publish_policy(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    cockpit = _cockpit()
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise cockpit.CockpitError("请写一句为什么要改：它会和这一版策略一起存档")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise cockpit.CockpitError("缺少请求编号，请刷新页面后重试")
    edits = value.get("policy")
    if not isinstance(edits, Mapping):
        raise cockpit.CockpitError("缺少策略内容")
    body = {**dict(DEFAULT_POLICY), **{key: edits[key] for key in DEFAULT_POLICY
                                       if key in edits}}
    body["schema_version"], body["policy_ref"], body["gate"] = (
        DEFAULT_POLICY["schema_version"], DEFAULT_POLICY["policy_ref"], "v2")
    params = {"policy": body, "reason": reason.strip()[:1000],
              "prior_version_ref": value.get("prior_version_ref") or None,
              "idempotency_key": f"cockpit-conviction-policy:{request_id.strip()}"}
    result = plane._governance(login, "publish_conviction_policy", params,
                               failure="投资建议策略没有发布")
    status = result.get("status")
    version = result.get("version") or {}
    title = {"published": f"发布了投资建议策略第 {version.get('version')} 版",
             "duplicate": "投资建议策略已经发布过（重复提交）",
             "unchanged": "投资建议策略没有变化，未生成新版本"}.get(status, "投资建议策略")
    plane.journal.record_event(kind="conviction_policy", title=title, detail=reason.strip(),
                               login=login, refs={"version_ref": version.get("id"),
                                                  "status": status})
    return {"status": status, "version_ref": version.get("id"),
            "version": version.get("version"), "message": title}


__all__ = ["ALIGNMENT_LABELS", "policy_view", "publish_policy", "v2_approval_item", "v2_card"]
