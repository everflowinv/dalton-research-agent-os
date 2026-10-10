"""Cockpit v2: the scenario probability group, saved in one write.

``POST /v2/data/scenarios/revise-group`` (batch ah, 2026-10-09; UX review
C23/C24, MUSE P0 #6).  The company page's 「调整情景概率」 form used to send one
``/v1/cockpit/scenario_revise`` per changed scenario, so a stale row could
leave half a group saved.  This route sends the whole group to the writer's
``revise_company_scenario_group`` -- append-only, all or nothing -- as the
owner's own principal.  The reason is optional: the writer records a
generated one when it is left empty.  ``/v1/cockpit/scenario_revise`` stays
for compatibility.

Body::

    {"company_ref": "company:…", "request_id": "…", "reason": "可选",
     "changes": [{"scenario_ref", "version_ref", "version_hash",
                  "probability": "0.45" | "45", "retire": false}, …]}

Every scenario in force is listed once (unchanged ones too).  The router has
already checked the session's CSRF token.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

REVISE_GROUP_PATH = "/v2/data/scenarios/revise-group"
REASON_MAX = 600
MAX_CHANGES = 4
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_LABELS = {"bull": "乐观", "base": "基准", "bear": "悲观"}


def _error(message: str) -> Exception:
    from ..cockpit_plane import CockpitError

    return CockpitError(message)


def _text(value: Any, maximum: int, message: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise _error(message)
    return value.strip()


def _probability(raw: Any) -> str:
    if isinstance(raw, bool) or raw is None:
        raise _error("概率请写成 1 到 95 之间的百分比")
    try:
        number = Decimal(str(raw).strip().rstrip("%"))
    except (InvalidOperation, ValueError) as exc:
        raise _error("概率请写成 1 到 95 之间的百分比") from exc
    if not number.is_finite():
        raise _error("概率请写成 1 到 95 之间的百分比")
    if number > 1:
        number = number / 100
    return str(number)


def group_params(value: Any) -> dict[str, Any]:
    """The writer params for one request body (pure; Chinese refusals)."""

    if not isinstance(value, Mapping):
        raise _error("请求内容不对，请刷新页面后重试")
    company_ref = _text(value.get("company_ref"), 512, "请从公司页面发起调整")
    request_id = _text(value.get("request_id"), 128, "缺少请求编号，请刷新页面后重试")
    reason = value.get("reason")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise _error("理由请写成一段文字")
    reason = reason.strip()
    if len(reason) > REASON_MAX:
        raise _error(f"理由请控制在 {REASON_MAX} 字以内")
    raw_changes = value.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes or len(raw_changes) > MAX_CHANGES:
        raise _error("请把这家公司正在使用的每个情景都列出来")
    changes = []
    for item in raw_changes:
        if not isinstance(item, Mapping):
            raise _error("请求内容不对，请刷新页面后重试")
        retire = item.get("retire", False)
        if not isinstance(retire, bool):
            raise _error("请求内容不对，请刷新页面后重试")
        digest = item.get("version_hash")
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            raise _error("情景版本信息不完整，请刷新页面后重试")
        changes.append({
            "scenario_ref": _text(item.get("scenario_ref"), 240, "请从公司页面发起调整"),
            "version_ref": _text(item.get("version_ref"), 240, "请从公司页面发起调整"),
            "version_hash": digest, "retire": retire,
            "probability": None if retire else _probability(item.get("probability"))})
    return {"company_ref": company_ref, "changes": changes, "reason": reason,
            "idempotency_key": f"cockpit-scenario-group:{request_id}"}


def _percent(text: Any) -> str:
    try:
        return f"{int((Decimal(str(text)) * 100).to_integral_value())}%"
    except (InvalidOperation, ValueError):
        return "—"


def receipt(result: Mapping[str, Any]) -> str:
    status = result.get("status")
    if status == "duplicate":
        return "概率和现在一样，没有需要保存的修改。"
    versions = list(result.get("versions") or ())
    if status == "replayed":
        return "这组修改之前已经保存过，没有重复写入。"
    parts = []
    for item in versions:
        label = KEY_LABELS.get(item.get("key"), str(item.get("name") or "情景"))
        parts.append(f"{label}已停用" if item.get("state") == "retired"
                     else f"{label} {_percent(item.get('probability'))}")
    return f"已整组保存（{len(versions)} 项修改）：{'；'.join(parts)}。"


def handle_revise_group(application: Any, login: str, session: Any, csrf: Any,
                        body: bytes, path: str = REVISE_GROUP_PATH) -> dict[str, Any]:
    plane = getattr(application, "cockpit_plane", None) or application
    try:
        value = json.loads((body or b"").decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("请求内容不对，请刷新页面后重试") from exc
    params = group_params(value)
    result = plane._governance(login, "revise_company_scenario_group", params,
                               failure="情景概率没有保存")
    try:
        plane.journal.record_event(
            kind="approval", title="调整了一组情景的概率",
            detail=str(result.get("reason") or ""), login=login,
            refs={"company_ref": params["company_ref"], "group_ref": result.get("group_ref")})
    except Exception:  # noqa: BLE001 - the write stands; the journal line is a courtesy
        pass
    return {
        "status": result.get("status"), "group_ref": result.get("group_ref"),
        "reason": result.get("reason"), "reason_auto": bool(result.get("reason_auto")),
        "versions": [{"scenario_ref": item.get("scenario_ref"), "version_ref": item.get("id"),
                      "key": item.get("key"), "probability": item.get("probability"),
                      "state": item.get("state")} for item in result.get("versions") or ()],
        "message": receipt(result),
    }


def register(router: Any) -> None:
    router.add_post(REVISE_GROUP_PATH, handle_revise_group)


__all__ = ["REVISE_GROUP_PATH", "group_params", "handle_revise_group", "receipt", "register"]
