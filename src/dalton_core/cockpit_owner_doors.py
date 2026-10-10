"""The owner's recovery doors for a held research task, from the cockpit (2026-10-09).

Owner, 10-08: 「我根本不知道该怎么处理，没办法消除掉这个通知」.  A
``controlled_recovery`` card used to say "a technical administrator must run a
command on the machine": the four doors that settle a held document-research
admission -- (CLI names) authorize-reentry / authorize-paid / authorize-unproved /
withdraw -- existed only as ``document_recovery_cli`` subcommands.  This module
sends the same four writer operations from the cockpit, as the owner's own
``human:`` principal (ADR-0006), with the owner's reason recorded beside the
receipt.  Nothing here decides anything the CLI does not: the writer and the
lane refuse what they would refuse from the terminal, and the CLI commands
stay in the card's 技术详情.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .terminology import label as _term

#: action -> (writer operation, needs a cost ceiling, title for the journal)
DOORS: Mapping[str, tuple[str, bool, str]] = {
    "reentry": ("authorize_mission_document_reentry", False, _term("recovery_door", "reentry_done")),
    "paid": ("authorize_mission_document_paid_recovery", True,
             _term("recovery_door", "paid_done") + "（上次回复不符合输出要求）"),
    "unproved": ("authorize_mission_document_unproved_recovery", True,
                 _term("recovery_door", "paid_done") + "（上次送达未确认）"),
    "withdraw": ("withdraw_mission_document_admission", False, _term("recovery_door", "withdraw_done")),
    # 2026-10-09: one more round of automatic recovery for a task stopped on
    # its retry bound.  The same writer door as a re-entry; on such a hold the
    # lane records the round (append-only) instead of a launcher grant.
    "retry_round": ("authorize_mission_document_reentry", False,
                    _term("recovery_door", "retry_round_done")),
}
#: 2026-10-09 (financial review 痛点3; batch am: text evaluation 7.3): the
#: business words, from the terminology registry -- never 放行 / 再买一次 /
#: 付费重试 / 不产生费用, which read as trading or as petty billing.
DOOR_LABELS: Mapping[str, str] = {
    name: _term("recovery_door", name)
    for name in ("reentry", "paid", "unproved", "withdraw", "retry_round")
}
#: The CLI subcommand for each door, for 技术详情.
CLI_SUBCOMMANDS: Mapping[str, str] = {
    "reentry": "authorize-reentry", "paid": "authorize-paid",
    "unproved": "authorize-unproved", "withdraw": "withdraw",
    "retry_round": "authorize-reentry",
}
MAX_COST_USD = Decimal("20")
_ADMISSION = re.compile(r"mission-document-research-admission:[0-9a-f]{16,64}\Z")
_JOB = re.compile(r"owner-door-job:[0-9a-zA-Z._:-]{1,128}\Z")


def door_for_operation(operation: str | None) -> str | None:
    """The cockpit door name of a writer operation (``None`` if not a door)."""

    for name, (writer_operation, _cap, _title) in DOORS.items():
        if writer_operation == operation:
            return name   # the first door of a shared operation (reentry)
    return None


def _cockpit() -> Any:
    from . import cockpit_plane

    return cockpit_plane


def owner_door(plane: Any, login: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Open one recovery door for one held admission, or poll the job it started."""

    cockpit = _cockpit()
    door = value.get("door")
    if door not in DOORS:
        raise cockpit.CockpitError("请选择要执行的操作：" + "、".join(
            DOOR_LABELS[name] for name in ("reentry", "paid", "withdraw")))
    operation, needs_cap, title = DOORS[door]
    admission_ref = value.get("admission_ref")
    if not isinstance(admission_ref, str) or _ADMISSION.match(admission_ref) is None:
        raise cockpit.CockpitError("缺少要处理的研究任务编号")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
        raise cockpit.CockpitError("请写一句理由（600 字以内），会和操作一起存档")
    reason = reason.strip()
    params: dict[str, Any] = {"admission_ref": admission_ref}
    if needs_cap:
        try:
            cap = Decimal(str(value.get("max_cost_usd")))
        except (InvalidOperation, ValueError):
            raise cockpit.CockpitError(f"请填写{_term('recovery_door', 'cap_label')}") from None
        if not cap.is_finite() or cap <= 0 or cap > MAX_COST_USD:
            raise cockpit.CockpitError(f"检索预算上限需在 0 到 {MAX_COST_USD} 美元之间")
        params["max_cost_usd"] = float(cap)
        job_ref = value.get("job_ref")
        if job_ref is not None:
            if not isinstance(job_ref, str) or _JOB.match(job_ref) is None:
                raise cockpit.CockpitError("后台任务编号无效")
            params["job_ref"] = job_ref
    if door == "withdraw":
        params["reason"] = reason
    result = plane._governance(login, operation, params, failure=f"「{DOOR_LABELS[door]}」没有执行")
    if not (needs_cap and params.get("job_ref")):
        plane.journal.record_event(
            kind="lever", title=title, detail=reason, login=login,
            refs={"admission_ref": admission_ref, "operation": operation, "door": door,
                  "status": result.get("status") if isinstance(result, Mapping) else None,
                  "job_ref": result.get("job_ref") if isinstance(result, Mapping) else None})
    return {"door": door, "operation": operation, "admission_ref": admission_ref,
            "result": result if isinstance(result, (dict, list)) else None,
            "message": _message(door, result)}


def _message(door: str, result: Any) -> str:
    status = result.get("status") if isinstance(result, Mapping) else None
    state = result.get("state") if isinstance(result, Mapping) else None
    if door == "withdraw":
        return (_term("recovery_door", "withdraw_done") + "；需要时可以重新发起同一课题。"
                if status in {"withdrawn", "replayed", None} else "没有暂缓：当前状态不允许暂缓这项专项课题。")
    if door == "reentry":
        return (_term("recovery_door", "reentry_done") + "：系统下一轮（约 5 分钟内）会按当前设置继续这项研究；"
                "处理完后这张卡片会自动消失。")
    if door == "retry_round":
        if status == "refused":
            return "没有安排：这项研究会在有模型恢复可用后由系统自动再试，不需要你操作。"
        return (_term("recovery_door", "retry_round_done") + "：系统下一轮（约 5 分钟内）为这个研究步骤"
                "再安排一轮重试，费用在立项时已批准的上限之内；成功后这张卡片会自动消失。")
    if state in {"queued", "running"} or status in {"queued", "running", "accepted"}:
        return "已受理，正在后台处理；可在「管理员设置 → 运行控制」查看进度。"
    return "已受理：系统下一轮会在你给的检索预算上限内扩大范围重新获取资料。"


__all__ = ["CLI_SUBCOMMANDS", "DOORS", "DOOR_LABELS", "door_for_operation", "owner_door"]
