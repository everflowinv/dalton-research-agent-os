"""The owner's door for a document-research admission the lane has given up on.

    # read-only: what is actually held, and which of it is waiting on a person
    .venv/bin/python -m dalton_core.document_recovery_cli holds \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # allow one more controlled re-entry for one named admission
    .venv/bin/python -m dalton_core.document_recovery_cli authorize-reentry \\
        --state-dir "..." --admission-ref mission-document-research-admission:… \\
        --apply --actor human:lumos

    # buy one more model call for a failure the lane already retried once
    .venv/bin/python -m dalton_core.document_recovery_cli authorize-paid \\
        --state-dir "..." --admission-ref … --max-cost-usd 0.50 \\
        --apply --actor human:lumos
    .venv/bin/python -m dalton_core.document_recovery_cli authorize-unproved \\
        --state-dir "..." --admission-ref … --apply --actor human:lumos

    # or clear the whole escalated pile, oldest first, under one total cap
    .venv/bin/python -m dalton_core.document_recovery_cli authorize-all-escalated \\
        --state-dir "..." --max-total-cost-usd 2.00 --apply --actor human:lumos

    # close one escalated or terminal admission for good (2026-09-30); the
    # planner may ask the same inquiry again later, under the current budget
    .venv/bin/python -m dalton_core.document_recovery_cli withdraw \\
        --state-dir "..." --admission-ref … --reason "…" --apply --actor human:lumos

    # list (default) or withdraw every hold whose retry is known to be pointless
    .venv/bin/python -m dalton_core.document_recovery_cli withdraw-all-unrecoverable \\
        --state-dir "..." [--apply --actor human:lumos]

The lane retries a moved ticket identity once by itself and then stops, which
is the right rule and was, until this existed, a dead end: the escalation said
"a person has to decide" and there was nothing for a person to press.  This is
that thing.  It grants exactly what the lane grants itself -- one re-entry --
and it goes through the live writer's ephemeral human principal, because the
lane's ticket directory is writer state and a second process must not write it.

``holds`` is a read-only view of the lane's own ledger; it takes no writer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

OPERATION = "authorize_mission_document_reentry"
PAID_OPERATION = "authorize_mission_document_paid_recovery"
UNPROVED_OPERATION = "authorize_mission_document_unproved_recovery"
WITHDRAW_OPERATION = "withdraw_mission_document_admission"
# Which door each escalated hold reason leads to.  The lane writes the reason;
# the owner should never have to translate it into an operation name.
DOORS: dict[str, str] = {
    "contract_failed_after_automatic_retry": PAID_OPERATION,
    "unproved_send_failed_after_automatic_retry": UNPROVED_OPERATION,
    # 2026-10-06: no door for a provider-budget refusal.  The unproved door
    # buys the same call at the same ceiling, which the provider's count of
    # the same prompt breaks again.  The lane corrects the ceiling once by
    # itself; what it cannot correct is classified unrecoverable (withdraw).
    "reentry_failed_after_automatic_rebind": OPERATION,
}
COMMANDS: dict[str, str] = {
    OPERATION: "authorize-reentry",
    PAID_OPERATION: "authorize-paid",
    UNPROVED_OPERATION: "authorize-unproved",
}


def _ledger(state_dir: Path) -> dict[str, Any]:
    from .needs_human import lane_hold_ledgers

    return lane_hold_ledgers(state_dir)


# What ``holds`` says about a hold the lane has not escalated: it is still the
# lane's own business, so there is deliberately no command to run.
LANE_OWNS_IT = "车道还在自己处理（每轮自动重试或等待到期），不需要 owner 操作。"


def owner_command(state_dir: Path | str, item: dict[str, Any]) -> str | None:
    """The exact line that opens this escalated hold's door, if it has one."""

    import shlex

    from .needs_human import recovery_command

    operation = door_for(str(item.get("reason") or ""))
    if operation is None:
        return None
    return recovery_command(
        COMMANDS[operation], state_dir=shlex.quote(str(state_dir)),
        admission_ref=shlex.quote(str(item.get("admission_ref") or "<ref>")),
        cap=None if operation == OPERATION else "<上限美元>")


def owner_doors_used(state_dir: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Read-only: every owner (paid/unproved) authorization already written.

    The executor grants each of those doors once per stage and refuses the
    second with "... was already owner-recovered".  Live on ws-7d, three
    admissions (bec19d08, c737cc83, 4225dbd9) were owner-authorized on
    2026-09-26, failed again, and went on being listed as waiting on the owner
    with a command that could only be refused.
    """

    import sqlite3

    database = Path(state_dir) / "core.sqlite"
    if not database.exists():
        return {}
    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return {}
    try:
        rows = connection.execute(
            "SELECT admission_ref, authorization_id, stage_ordinal, created_at "
            "FROM mission_document_research_controlled_recovery_authorizations "
            "ORDER BY created_at").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    used: dict[str, list[dict[str, Any]]] = {}
    for admission_ref, authorization_id, stage_ordinal, created_at in rows:
        for operation, prefix in OWNER_AUTHORIZATION_PREFIXES.items():
            if str(authorization_id).startswith(prefix):
                used.setdefault(str(admission_ref), []).append({
                    "operation": operation, "authorization_ref": authorization_id,
                    "stage_ordinal": stage_ordinal, "authorized_at": created_at,
                })
    return used


def holds(state_dir: Path) -> dict[str, Any]:
    """Every held admission in this state directory, who is waiting, and on what.

    An escalated hold carries the one command that opens its door, already
    filled in with this state directory and admission (only the cap and the
    owner's name are left); a hold the lane is still working on says so and
    carries no command, because running one there would be doing the lane's
    job for it.
    """

    ledgers = _ledger(state_dir)
    used = owner_doors_used(state_dir) if ledgers else {}
    out: dict[str, Any] = {}
    for lane, value in sorted(ledgers.items()):
        escalated = []
        for item in value["escalated"]:
            operation = door_for(item["reason"])
            spent = [entry for entry in used.get(item["admission_ref"], [])
                     if entry["operation"] == operation]
            command = None if spent else owner_command(state_dir, item)
            entry: dict[str, Any] = {**item, "operation": operation,
                                     "command": command}
            waiting = (_pending_owner_recovery_of(state_dir, item["admission_ref"])
                       if spent else None)
            if waiting is not None:
                # 2026-09-30: authorized, but the recovery it bought has not
                # run yet; the hold is the failure it was bought to retry.
                entry["owner_door_used"] = spent[-1]
                entry["pending_owner_recovery"] = waiting["pending"]
                entry["note"] = (
                    f"owner 已在 {spent[-1]['authorized_at']} 授权恢复，但车道还没把它跑完"
                    "（恢复 WorkOrder 尚未派发或尚无结果）。不需要再按，也不要撤回；"
                    "等车道下一轮的结果。")
            elif spent:
                entry["owner_door_used"] = spent[-1]
                entry["note"] = (
                    f"owner 已在 {spent[-1]['authorized_at']} 用过这道门，执行器只放一次，"
                    "再按会被拒绝（already owner-recovered）。这条 admission 在这份文档上"
                    "已经没有可以再买的一步：用 withdraw 把它关掉（之后规划器可以按当前预算"
                    "重新立项同一个问题）。")
            elif command is None:
                entry["note"] = "没有对应的 owner 门；把这条原因原文发给维护者。"
            escalated.append(entry)
        other = [{**item, "command": None, "note": LANE_OWNS_IT}
                 for item in value["waiting"] if item not in value["escalated"]]
        out[lane] = {
            "holds_path": value["path"],
            "held": len(value["waiting"]),
            "waiting_on_owner": len(value["escalated"]),
            "actionable": sum(1 for item in escalated if item["command"]),
            "escalated": escalated,
            "other": other,
        }
        if lane == "mission_document_research":
            # 2026-09-30: say which of these no door can fix, and the one
            # command that lists them (a dry run; it writes nothing).
            try:
                hopeless = unrecoverable_holds(state_dir)
            except Exception:  # noqa: BLE001 - a suggestion never fails the view
                hopeless = []
            by_ref = {item["admission_ref"]: item for item in hopeless}
            for entry in escalated:
                found = by_ref.get(entry["admission_ref"])
                if found is not None:
                    entry["unrecoverable"] = found["class"]
                    # The buy door refuses these (2026-10-03); this is the
                    # one that helps.
                    entry["withdraw_instead"] = _withdraw_command(
                        Path(state_dir), entry["admission_ref"],
                        "unrecoverable:" + found["class"] + ": " + found["why"])
            if hopeless:
                import shlex

                out[lane]["unrecoverable"] = len(hopeless)
                out[lane]["suggest"] = (
                    f"{len(hopeless)} 条 hold 再重试也不会有结果（门已用过、提示词超过准入输入预算、"
                    "在低于当前的输出上限里被截断、或核验仍按已退役的 0.1 契约）。先预览："
                    "python -m dalton_core.document_recovery_cli withdraw-all-unrecoverable "
                    f"--state-dir {shlex.quote(str(state_dir))}"
                    "；确认后加 --apply --actor human:<owner> 撤回。撤回后规划器可以按当前预算重新立项。")
    return out


def escalated_holds(state_dir: Path) -> list[dict[str, Any]]:
    """The document lane's escalated holds, oldest run first.

    The ledger itself carries no instant -- it is keyed by admission -- so the
    order comes from the run each hold names.  An owner clearing a backlog
    under a cap wants the one that has been stuck longest, not the one whose
    hash sorts first.
    """

    from .mission_document_research_launcher import TICKETS_DIRNAME

    items = list(holds(state_dir).get("mission_document_research", {}).get(
        "escalated", []))
    for item in items:
        ticket_ref = item.get("ticket_ref")
        started = ""
        if isinstance(ticket_ref, str) and ticket_ref:
            path = (Path(state_dir) / TICKETS_DIRNAME
                    / ticket_ref.split(":")[-1] / "ticket.json")
            try:
                started = str(json.loads(path.read_text(encoding="utf-8")).get(
                    "started_at") or "")
            except (OSError, ValueError, TypeError, AttributeError):
                started = ""
        item["started_at"] = started
        item["operation"] = door_for(item.get("reason") or "")
    # An unreadable instant sorts last rather than first: it is the one thing
    # here that is not evidence of age.
    items.sort(key=lambda item: (item["started_at"] == "", item["started_at"],
                                 item["admission_ref"]))
    return items


def door_for(reason: str) -> str | None:
    """The writer operation this hold reason leads to, if any.

    2026-10-09: asked of the executor's policy (``available_doors``), the one
    place that says what each reason permits -- a fresh-work cap that is a
    policy bound leads to one more round, through the re-entry door.  The
    table above is the fallback when the policy cannot be imported.
    """

    try:
        from .mission_document_research_executor import available_doors
    except Exception:  # noqa: BLE001 - the historical table
        for name, operation in DOORS.items():
            if name in reason:
                return operation
        return None
    for door in available_doors({"reason": reason}):
        if door["door"] not in {"withdraw", "technical_help"} and door["operation"]:
            return door["operation"]
    return None


def _ephemeral_call(state: Path, *, operation: str, actor: str,
                    params: dict[str, Any]) -> Any:
    from .governance_cli import ephemeral_call

    return ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=actor, operation=operation, params=params,
    )


# 2026-09-26.  A door the client gave up on may still have been opened: the
# writer finishes a request it has started even after the caller has gone
# (live: seven BrokenPipeErrors, every one of them a door that *had* been
# opened).  So a timeout is never answered by blindly calling again.  The
# ledger is asked first, read-only, whether this call's door is already open;
# only when it is not is the call repeated -- which is safe as well, because
# every door replays an authorization it already wrote rather than minting a
# second one.
RETRYABLE_CODES = frozenset({"transport_error", "store_timeout", "queued_timeout"})
CALL_ATTEMPTS = 3
CONFIRM_WAIT_SECONDS = 30.0
# 2026-10-03: ``store_timeout`` means the writer *started* the request and is
# still running it on its one store thread (live legacy 10-02: one
# authorize-unproved held it for minutes).  Calling again at once only queues a
# duplicate behind the original -- which then answers ``queued_timeout`` while
# every lane tick waits as well.  So after a started request the ledger is
# watched for longer before anything is sent again.
STARTED_CONFIRM_WAIT_SECONDS = 180.0
CONFIRM_POLL_SECONDS = 3.0
# Recorded instants are the writer's clock and ours is the terminal's: the
# same machine, but allow a little for the stamp being taken before the call.
CLOCK_SLACK = timedelta(seconds=5)
OWNER_AUTHORIZATION_PREFIXES: dict[str, str] = {
    PAID_OPERATION: "mission-document-paid-recovery-authorization:",
    UNPROVED_OPERATION: "mission-document-unproved-send-recovery-authorization:",
}


def _instant(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def door_opened_since(state: Path, *, operation: str, admission_ref: str,
                      actor: str, since: datetime) -> dict[str, Any] | None:
    """Read-only: whether this admission's door was opened at or after ``since``.

    Paid and unproved doors write an owner authorization row in the Core
    ledger; the re-entry door writes a grant file in the lane's ticket
    directory, which the next re-entry renames to a ``-used-`` record.  Both
    are read without a writer and without write access: SQLite is opened
    ``mode=ro``, and the files are only read.
    """

    floor = since - CLOCK_SLACK
    if operation == WITHDRAW_OPERATION:
        record = _read_only_withdrawals(state).get(admission_ref)
        moment = None if record is None else _instant(record.get("created_at"))
        if (record is not None and moment is not None and moment >= floor
                and record.get("actor_ref") == actor):
            return {"status": "withdrawn", **record}
        return None
    if operation == OPERATION:
        from types import SimpleNamespace

        from .lane_reentry_claim import grant_path
        from .mission_document_research_launcher import TICKETS_DIRNAME

        pending = grant_path(SimpleNamespace(
            tickets_dir=Path(state) / TICKETS_DIRNAME), admission_ref)
        candidates = [pending, *sorted(pending.parent.glob(
            pending.name[:-5] + "-used-*.json"))]
        for path in candidates:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            moment = _instant(record.get("granted_at")) if isinstance(record, dict) else None
            if (moment is not None and moment >= floor
                    and record.get("admission_ref") == admission_ref
                    and record.get("actor_ref") == actor):
                return {"status": "granted", "grant_path": str(path),
                        "granted_at": record.get("granted_at")}
        return None
    prefix = OWNER_AUTHORIZATION_PREFIXES.get(operation)
    database = Path(state) / "core.sqlite"
    if prefix is None or not database.exists():
        return None
    import sqlite3

    try:
        connection = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT authorization_id, stage_ordinal, created_at, record_json "
            "FROM mission_document_research_controlled_recovery_authorizations "
            "WHERE admission_ref=? ORDER BY created_at", (admission_ref,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    for authorization_id, stage_ordinal, created_at, record_json in rows:
        try:
            record = json.loads(record_json)
        except (TypeError, ValueError):
            continue
        moment = _instant(created_at)
        if (isinstance(record, dict)
                and str(record.get("id") or authorization_id).startswith(prefix)
                and moment is not None and moment >= floor):
            return {"status": "admitted", "admission_ref": admission_ref,
                    "stage_ordinal": stage_ordinal,
                    "authorization_ref": record.get("id") or authorization_id,
                    "authorized_at": created_at,
                    "max_cost_usd": record.get("max_cost_usd")}
    return None


# 2026-10-07: the paid and unproved doors are governed jobs.  The writer
# answers the request at once with a ``job_ref``; the runtime build and the
# door run on its owner-door thread.  The CLI polls the job (one indexed read
# on the writer) until it has a result.
JOB_OPERATIONS = frozenset({PAID_OPERATION, UNPROVED_OPERATION})
JOB_WAIT_SECONDS = 1800.0
JOB_POLL_SECONDS = 5.0


def _await_job(state: Path, *, operation: str, actor: str, params: dict[str, Any],
               answer: Any, sleep: Any, clock: Any) -> Any:
    """Poll a door's job until it finishes; return the door's own result."""

    if (operation not in JOB_OPERATIONS or not isinstance(answer, dict)
            or not isinstance(answer.get("job_ref"), str)
            or answer.get("status") not in {"requested", "in_progress"}):
        return answer
    job_ref = answer["job_ref"]
    print(f"writer accepted the request as {job_ref}; waiting for the result "
          "(the writer keeps serving lanes meanwhile)", file=sys.stderr)
    deadline = clock() + JOB_WAIT_SECONDS
    view = answer
    while clock() < deadline:
        sleep(JOB_POLL_SECONDS)
        try:
            view = _ephemeral_call(state, operation=operation, actor=actor,
                                   params={**params, "job_ref": job_ref})
        except Exception as exc:  # noqa: BLE001 - a missed poll is not the answer
            code = getattr(exc, "code", None)
            if code not in RETRYABLE_CODES:
                raise
            continue
        if not isinstance(view, dict):
            return view
        if view.get("status") == "finished":
            result = view.get("result")
            if view.get("state") == "failed" or not isinstance(result, dict):
                return {"status": "error", "job_ref": job_ref,
                        "code": "owner_door_job_failed",
                        "message": str((view.get("error") or {}).get("message"))[:500],
                        "error": view.get("error")}
            return {**result, "job_ref": job_ref}
        if view.get("status") in {"interrupted", "unknown_job"}:
            return {**view, "note": view.get("note") or
                    "这个请求没有完成（写入服务重启或记录不存在）；可以再执行一次同一条命令。"}
    return {"status": "still_running", "job_ref": job_ref,
            "last_state": view.get("state") if isinstance(view, dict) else None,
            "note": ("后台仍在处理；没有重复提交。稍后用同一条命令再查即可"
                     "（同一道门同一条 admission 不会重复受理）。")}


def _call(state: Path, *, operation: str, actor: str,
          params: dict[str, Any], sleep: Any = None, clock: Any = None) -> Any:
    """Call one door; after a timeout, look before calling it again."""

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    answer = _call_once(state, operation=operation, actor=actor, params=params,
                        sleep=sleep, clock=clock)
    return _await_job(state, operation=operation, actor=actor, params=params,
                      answer=answer, sleep=sleep, clock=clock)


def _call_once(state: Path, *, operation: str, actor: str,
               params: dict[str, Any], sleep: Any = None, clock: Any = None) -> Any:
    """One door request; after a timeout, look before calling it again."""

    from .writer_protocol import RemoteError

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    started = datetime.now(timezone.utc)
    admission_ref = str(params.get("admission_ref") or "")
    failure: RemoteError | None = None
    for attempt in range(1, CALL_ATTEMPTS + 1):
        try:
            return _ephemeral_call(
                state, operation=operation, actor=actor, params=params)
        except RemoteError as exc:
            if exc.code not in RETRYABLE_CODES:
                raise
            failure = exc
        # ``queued_timeout`` is the writer saying the request never ran, so
        # there is nothing to wait for; anything else may still be finishing.
        deadline = clock() + (
            0.0 if failure.code == "queued_timeout"
            else STARTED_CONFIRM_WAIT_SECONDS if failure.code == "store_timeout"
            else CONFIRM_WAIT_SECONDS)
        while True:
            opened = door_opened_since(
                state, operation=operation, admission_ref=admission_ref,
                actor=actor, since=started)
            if opened is not None:
                print(f"writer answered {failure.code}, but the door is open: "
                      "confirmed from the ledger, not called again",
                      file=sys.stderr)
                return {**opened, "confirmed_after": failure.code}
            if clock() >= deadline:
                break
            sleep(CONFIRM_POLL_SECONDS)
        if attempt < CALL_ATTEMPTS:
            print(f"writer answered {failure.code} and the door is not open; "
                  f"calling again ({attempt + 1}/{CALL_ATTEMPTS})",
                  file=sys.stderr)
    assert failure is not None
    raise failure


def _print(value: Any) -> None:
    print(json.dumps(value if isinstance(value, (dict, list)) else {"result": value},
                     ensure_ascii=False, indent=1))


def _cost(result: Any) -> float:
    if not isinstance(result, dict) or result.get("status") != "admitted":
        return 0.0
    try:
        return float(result.get("max_cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hopeless_by_ref(state: Path) -> dict[str, dict[str, Any]]:
    """Read-only: the unrecoverable class of each held admission, if any."""

    try:
        return {item["admission_ref"]: item for item in unrecoverable_holds(state)}
    except Exception:  # noqa: BLE001 - no evidence is no refusal
        return {}


def _withdraw_command(state: Path, admission_ref: str, why: str) -> str:
    import shlex

    return (f"python -m dalton_core.document_recovery_cli withdraw "
            f"--state-dir {shlex.quote(str(state))} "
            f"--admission-ref {shlex.quote(admission_ref)} "
            f"--reason {shlex.quote(why)} --apply --actor human:<owner>")


def _with_state(result: Any, state: Path) -> Any:
    """Fill the writer's ``<state>`` placeholder with this state directory."""

    if isinstance(result, dict) and isinstance(result.get("next_command"), str):
        import shlex

        return {**result, "next_command": result["next_command"].replace(
            "<state>", shlex.quote(str(state)))}
    return result


def authorize_all_escalated(
    state: Path, *, actor: str | None, apply: bool,
    max_total_cost_usd: float, include_unrecoverable: bool = False,
) -> int:
    """Walk the escalated pile oldest-first and stop at the total cap.

    Each admission is authorized with the *remaining* budget as its own cap,
    so a stage that costs more than what is left is refused by the writer
    rather than silently overspending it; the re-entry door costs nothing and
    is taken whenever it is the door this reason leads to.

    2026-10-03: one admission's failure is that admission's result, not the
    end of the walk -- every escalated hold gets its own line, and the exit
    code says whether any of them errored.  A hold already classified as
    unrecoverable is not bought again (its withdraw command is printed
    instead) unless ``include_unrecoverable`` says so.
    """

    items = escalated_holds(state)
    used = owner_doors_used(state)
    hopeless = {} if include_unrecoverable else _hopeless_by_ref(state)
    remaining, spent, results = float(max_total_cost_usd), 0.0, []
    for item in items:
        operation = item["operation"]
        entry = {"admission_ref": item["admission_ref"],
                 "reason": item["reason"], "operation": operation,
                 "started_at": item["started_at"]}
        door_used = [record for record in used.get(item["admission_ref"], [])
                     if record["operation"] == operation]
        found = hopeless.get(item["admission_ref"])
        if operation is None:
            entry["result"] = {"status": "no_door_for_this_reason"}
        elif found is not None and operation != OPERATION:
            entry["result"] = {
                "status": "unrecoverable_withdraw_instead",
                "class": found["class"], "why": found["why"],
                "withdraw": _withdraw_command(
                    state, item["admission_ref"],
                    "unrecoverable:" + found["class"] + ": " + found["why"]),
            }
        elif door_used:
            # The executor would refuse it; asking costs a writer round trip
            # and, before this, aborted the whole walk on the first refusal.
            entry["result"] = {"status": "owner_door_already_used",
                               "authorized_at": door_used[-1]["authorized_at"]}
        elif remaining <= 0 and operation != OPERATION:
            entry["result"] = {"status": "stopped_at_total_cap",
                               "remaining_usd": round(remaining, 6)}
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False))
            break
        elif not apply:
            entry["result"] = {"status": "dry_run",
                               "max_cost_usd_cap": (
                                   None if operation == OPERATION
                                   else round(remaining, 6))}
        else:
            params: dict[str, Any] = {"admission_ref": item["admission_ref"],
                                      "actor_ref": actor}
            if operation != OPERATION:
                params["max_cost_usd"] = round(remaining, 6)
            try:
                entry["result"] = _with_state(_call(
                    state, operation=operation, actor=str(actor), params=params),
                    state)
            except Exception as exc:  # noqa: BLE001 - this admission's result
                entry["result"] = {
                    "status": "error",
                    "code": getattr(exc, "code", None) or type(exc).__name__,
                    "message": str(exc)[:500],
                    "note": "这一条没有完成；其余的照常继续。可以单独用 holds 查看，"
                            "或稍后单独再按这一条的门。",
                }
            cost = _cost(entry["result"])
            spent += cost
            remaining -= cost
            if (entry["result"] or {}).get("status") == "deferred":
                # The writer is still cold: every later door would be
                # deferred for the same reason.  Stop and say when to retry.
                entry["running_total_usd"] = round(spent, 6)
                results.append(entry)
                print(json.dumps(entry, ensure_ascii=False))
                break
        entry["running_total_usd"] = round(spent, 6)
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    by_status: dict[str, int] = {}
    for entry in results:
        status = str((entry.get("result") or {}).get("status"))
        by_status[status] = by_status.get(status, 0) + 1
    _print({"escalated": len(items), "handled": len(results),
            "results": by_status,
            "total_authorized_usd": round(spent, 6),
            "max_total_cost_usd": float(max_total_cost_usd),
            "applied": bool(apply)})
    return 1 if by_status.get("error") else 0


# -- withdrawal (2026-09-30) -----------------------------------------------------

UNRECOVERABLE_CLASSES: dict[str, str] = {
    "owner_door_already_used":
        "owner 已经用过这条原因对应的门，而且它买的恢复已经跑完并失败；执行器每阶段只放一次，再按只会被拒绝。",
    "input_over_admitted_budget":
        "提示词本身超过准入时的输入/总量预算（PROVIDER_BUDGET_EXCEEDED）；系统已按供应商计数"
        "加 25% 自动修正上限重试过一次仍超出，或者超出无法靠调高输入上限解决"
        "（输出/费用超限、所需输入超过准入值 2 倍）。再按同样上限买一次同样超出、同样计费；"
        "撤回后规划器会重新立项，重新立项的起草若再超出会自动修正一次。",
    "output_cap_exhausted_below_current_budget":
        "起草回复在准入的输出上限里被用尽（空回复或截断），而当前配置的上限更高；"
        "恢复按准入值封顶，只能重新立项才用得上新上限。",
    "verifier_restatement_under_retired_contract":
        "核验按已退役的 0.1 契约被拒（通过时改写了被核验的句子）；这条 admission 的核验阶段"
        "永远按 0.1 重放，再买一次还是同样被拒。重新立项会走 0.2 契约。",
    "verifier_refused_before_send":
        "核验请求至少两次、而且最近一次都在发送前被宿主的 provider-control 预检拒绝"
        "（HOST_CONTROL_PROOF_MISSING：没有发出、没有计费）。预检按准入冻结的输入/总量/费用上限"
        "计算，同一份输入每次结果相同——最常见是核验提示词超过了准入的 max_input_tokens。"
        "再买一次没有用；先调高 mission_directed_document_verifier 的输入上限，再撤回重新立项。",
}
# The host refuses these before the provider is called (2026-10-03).
_PREFLIGHT_REFUSED_CODES = frozenset({"HOST_CONTROL_PROOF_MISSING"})
_RESTATEMENT_ERROR = "passing verifier must preserve the exact candidate statement"
_OUTPUT_LIMIT_CODES = frozenset({"HOST_OUTPUT_LIMIT_REACHED"})
# What an empty reply was recorded as before HOST_OUTPUT_LIMIT_REACHED existed
# (a reasoning model spending the whole cap on thinking).  Generic on its own,
# so it counts only when it is the *only* way this stage ever failed.
_EMPTY_REPLY_CODES = frozenset({"INVALID_HOST_RESULT", "HOST_EMPTY_COMPLETION"})


def _budget_numbers(reason: str) -> dict[str, Any]:
    """The ``input=<used>><admitted>`` / ``need=<tokens>`` the lane appends."""

    numbers: dict[str, Any] = {}
    for part in reason.split(":"):
        if part.startswith("input=") and ">" in part:
            used, admitted = part[len("input="):].split(">", 1)
            if used.isdigit() and admitted.isdigit():
                numbers["provider_input_tokens"] = int(used)
                numbers["admitted_max_input_tokens"] = int(admitted)
        elif part.startswith("need=") and part[len("need="):].isdigit():
            numbers["needed_max_input_tokens"] = int(part[len("need="):])
    return numbers


def _read_only_core(state: Path) -> Any:
    import sqlite3

    database = Path(state) / "core.sqlite"
    if not database.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    return connection


def _read_only_withdrawals(state: Path) -> dict[str, dict[str, Any]]:
    from .mission_document_research_withdrawal import read_withdrawals

    connection = _read_only_core(state)
    if connection is None:
        return {}
    try:
        return read_withdrawals(connection)
    except Exception:  # noqa: BLE001 - unreadable is "none seen", the writer decides
        return {}
    finally:
        connection.close()


def _lane_holds(state: Path) -> dict[str, dict[str, Any]]:
    from .mission_document_research_lane import HOLDS_FILE, _read_holds
    from .mission_document_research_launcher import TICKETS_DIRNAME

    return _read_holds(Path(state) / TICKETS_DIRNAME / HOLDS_FILE)


def _run_summaries(state: Path) -> dict[str, list[dict[str, Any]]]:
    """Every run summary of this lane, by admission, read-only."""

    from .mission_document_research_launcher import TICKETS_DIRNAME

    found: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((Path(state) / TICKETS_DIRNAME).glob("*/summary.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(summary, dict) and isinstance(summary.get("admission_ref"), str):
            found.setdefault(summary["admission_ref"], []).append(summary)
    # Oldest run first: "the last failure" below means the latest one, and a
    # ticket directory's name is a hash, not an instant.  Stable for equal or
    # missing stamps, so the directory order still breaks ties.
    for summaries in found.values():
        summaries.sort(key=lambda item: str(item.get("created_at") or ""))
    return found


def _stage_evidence(summaries: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    """Each model attempt of one stage the lane's runs recorded, oldest first."""

    attempts = []
    for summary in summaries:
        for outcome in summary.get("outcomes") or []:
            if not isinstance(outcome, dict) or outcome.get("stage") != stage:
                continue
            result = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
            usage = ((outcome.get("accounting") or {}).get("usage") or {}) \
                if isinstance(outcome.get("accounting"), dict) else {}
            rejected = (result.get("metadata") or {}).get("rejected_model_output") \
                if isinstance(result.get("metadata"), dict) else None
            attempts.append({
                "status": outcome.get("status"),
                "error_code": (result.get("error") or {}).get("code")
                if isinstance(result.get("error"), dict) else None,
                "output_tokens": usage.get("output_tokens"),
                "model": usage.get("model"),
                "rejection": (rejected or {}).get("error_message")
                if isinstance(rejected, dict) else None,
            })
    return attempts


def _admission_record(connection: Any, admission_ref: str) -> dict[str, Any] | None:
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT record_json FROM mission_document_research_admissions "
            "WHERE admission_id=?", (admission_ref,)).fetchone()
        return None if row is None else json.loads(row[0])
    except Exception:  # noqa: BLE001 - evidence, not authority
        return None


def _escalated_stage(connection: Any, admission_ref: str) -> str | None:
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT record_json FROM mission_document_research_observations "
            "WHERE admission_ref=? AND outcome='recovery_required' "
            "ORDER BY created_at DESC, observation_id DESC LIMIT 1",
            (admission_ref,)).fetchone()
        return None if row is None else json.loads(row[0]).get("stage")
    except Exception:  # noqa: BLE001
        return None


def _current_draft_output_cap(state: Path) -> int | None:
    try:
        from .annual_report_runtime import plan_model_execution
        from .mission_document_model_authority import (
            DRAFT_PURPOSE, load_mission_document_model_configs,
        )

        draft, _verifier = load_mission_document_model_configs(state)
        return int(plan_model_execution(draft, DRAFT_PURPOSE)["max_output_tokens"])
    except Exception:  # noqa: BLE001 - no config, no claim about it
        return None


def _used_reentry_grant(state: Path, admission_ref: str) -> dict[str, Any] | None:
    from types import SimpleNamespace

    from .lane_reentry_claim import grant_path
    from .mission_document_research_launcher import TICKETS_DIRNAME

    pending = grant_path(SimpleNamespace(tickets_dir=Path(state) / TICKETS_DIRNAME),
                         admission_ref)
    for path in sorted(pending.parent.glob(pending.name[:-5] + "-used-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(record, dict) and record.get("admission_ref") == admission_ref
                and str(record.get("actor_ref") or "").startswith("human:")):
            return {"granted_at": record.get("granted_at"), "grant_path": str(path)}
    return None


PENDING_OWNER_RECOVERY = "pending owner recovery"
OWNER_RETRY_SPENT_REASON = "contract_failed_after_owner_authorized_retry"
# The ordinals the executor's authorization rows use (2 = draft, 3 = verifier).
_STAGE_ORDINALS: dict[str, int] = {
    "qualitative_model_draft": 2,
    "independent_qualitative_verifier": 3,
}


def _pending_owner_recovery(state: Path, connection: Any,
                            admission_ref: str) -> dict[str, Any] | None:
    """What the owner has granted this admission that the lane has not yet run.

    2026-09-30, live legacy 3013db85: the owner bought a paid recovery at
    13:35:04Z; minutes later its recovery WorkOrder was still ``ready`` and
    the hold ledger still showed the contract failure it was bought to retry,
    so ``withdraw-all-unrecoverable`` listed it as ``owner_door_already_used``.
    Whatever class the evidence would otherwise suggest, an admission with an
    owner recovery or re-entry still waiting to run is not unrecoverable.
    """

    from .mission_document_research_launcher import TICKETS_DIRNAME
    from .mission_document_research_withdrawal import (
        pending_owner_recoveries, pending_reentry_grant,
    )

    pending: list[dict[str, Any]] = []
    if connection is not None:
        pending.extend(pending_owner_recoveries(connection, admission_ref))
    grant = pending_reentry_grant(Path(state) / TICKETS_DIRNAME, admission_ref)
    if grant is not None:
        pending.append({"operation": OPERATION, "state": "not_dispatched", **grant})
    if not pending:
        return None
    return {"admission_ref": admission_ref, "status": "skipped",
            "reason": PENDING_OWNER_RECOVERY, "pending": pending}


def _pending_owner_recovery_of(state_dir: Path | str,
                               admission_ref: str) -> dict[str, Any] | None:
    connection = _read_only_core(Path(state_dir))
    try:
        return _pending_owner_recovery(Path(state_dir), connection, admission_ref)
    except Exception:  # noqa: BLE001 - a view never fails on a hint
        return None
    finally:
        if connection is not None:
            connection.close()


def unrecoverable_holds(state_dir: Path | str, *,
                        skipped: list[dict[str, Any]] | None = None,
                        ) -> list[dict[str, Any]]:
    """Read-only: the held admissions whose retry is known to be pointless.

    Only holds the owner may close (escalated or terminal) are considered,
    and each is named with the one class of evidence that makes its retry
    pointless.  Anything the evidence does not settle is left out: listing a
    hold here is a recommendation to close it, so a doubt keeps it off.

    An owner door counts as already used only once the recovery it granted
    has run and failed.  An admission with an owner recovery or re-entry the
    lane has not yet run to a result is never listed, whatever its class;
    when ``skipped`` is given, each such admission is appended to it with the
    reason ``pending owner recovery``.
    """

    from .mission_document_research_withdrawal import (
        owner_recoveries, withdrawable_hold,
    )

    state = Path(state_dir)
    try:
        holds = _lane_holds(state)
    except Exception:  # noqa: BLE001 - no readable ledger, nothing to recommend
        return []
    candidates = {ref: held for ref, held in holds.items() if withdrawable_hold(held)[0]}
    if not candidates:
        return []
    summaries = _run_summaries(state)
    connection = _read_only_core(state)
    current_cap = _current_draft_output_cap(state)
    out: list[dict[str, Any]] = []
    try:
        for admission_ref, held in sorted(candidates.items()):
            waiting = _pending_owner_recovery(state, connection, admission_ref)
            if waiting is not None:
                if skipped is not None:
                    skipped.append({**waiting, "hold_reason": held["reason"],
                                    "disposition": held["disposition"]})
                continue
            reason = held["reason"]
            operation = door_for(reason)
            evidence: dict[str, Any] | None = None
            kind = None
            stage = _escalated_stage(connection, admission_ref)
            # Once the owner's paid retry has failed too, the executor names
            # the hold after that (no door is left, so ``DOORS`` has no entry);
            # it is the same spent paid door.
            door = (PAID_OPERATION if OWNER_RETRY_SPENT_REASON in reason
                    else operation)
            prefix = OWNER_AUTHORIZATION_PREFIXES.get(door or "")
            ordinal = _STAGE_ORDINALS.get(str(stage))
            # The executor grants each door once per (admission, stage): a door
            # used on the draft says nothing about the verifier's.
            spent = [] if connection is None or prefix is None else [
                item for item in owner_recoveries(connection, admission_ref)
                if str(item["authorization_ref"]).startswith(prefix)
                and item["stage_ordinal"] == ordinal]
            # Used means the recovery it bought ran and failed; one that
            # succeeded leaves this hold stale, not hopeless.
            if spent and spent[-1]["state"] == "failed":
                last = spent[-1]
                kind, evidence = "owner_door_already_used", {
                    "operation": door,
                    "authorization_ref": last["authorization_ref"],
                    "stage_ordinal": last["stage_ordinal"],
                    "authorized_at": last["authorized_at"],
                    "recovery_state": last["state"],
                    "recovery_work_order_refs": last["work_order_refs"],
                }
            elif operation == OPERATION:
                grant = _used_reentry_grant(state, admission_ref)
                if grant is not None:
                    kind, evidence = "owner_door_already_used", grant
            # 2026-10-06: the bare ``provider_budget_exceeded_not_retried`` of
            # releases before the automatic budget correction is not listed:
            # the lane re-enters it and retries it once under the corrected
            # ceiling.  Only what that correction could not settle is
            # hopeless, and its hold reason carries the numbers.
            if kind is None and any(name in reason for name in (
                    "provider_budget_exceeded_after_corrected_retry",
                    "provider_budget_exceeded_correction_refused")):
                kind, evidence = "input_over_admitted_budget", {
                    "reason": reason, **_budget_numbers(reason)}
            attempts_of = summaries.get(admission_ref, [])
            if kind is None and stage == "qualitative_model_draft" and current_cap:
                record = _admission_record(connection, admission_ref) or {}
                admitted = ((record.get("model_execution") or {}).get("draft") or {}).get(
                    "max_output_tokens")
                attempts = _stage_evidence(attempts_of, "qualitative_model_draft")
                # At the cap, or within 5% of it: a reply cut off mid-JSON
                # reports a few tokens short of the limit (live d7a12e77,
                # 4023 of 4096, "not strict JSON").
                def at_cap(item: dict[str, Any]) -> bool:
                    return (item["error_code"] in _OUTPUT_LIMIT_CODES
                            or (isinstance(admitted, int) and isinstance(
                                item["output_tokens"], int)
                                and item["output_tokens"] * 100 >= admitted * 95))

                exhausted = [item for item in attempts if at_cap(item)]
                failures = [item for item in attempts
                            if item["status"] in {"failed", "retryable"}]
                if failures and all(at_cap(item) or item["error_code"] in _EMPTY_REPLY_CODES
                                    for item in failures):
                    exhausted = [item for item in failures]
                # 2026-10-03: the *latest* failure is what a retry repeats.
                # Live legacy f3cdba70/ec1e759a/d0e9fb10: early contract
                # rejections (since fixed) kept them off this list, the owner
                # bought a retry, and each came back empty at exactly 4096
                # output tokens, charged.  An empty reply as the last word,
                # under a cap lower than today's, is the same evidence.
                elif failures and (at_cap(failures[-1])
                                   or failures[-1]["error_code"] in _EMPTY_REPLY_CODES):
                    exhausted = [item for item in failures
                                 if at_cap(item) or item["error_code"] in _EMPTY_REPLY_CODES]
                if isinstance(admitted, int) and admitted < current_cap and exhausted:
                    kind, evidence = "output_cap_exhausted_below_current_budget", {
                        "admitted_max_output_tokens": admitted,
                        "current_max_output_tokens": current_cap,
                        "exhausted_attempts": len(exhausted),
                        "models": sorted({str(item["model"]) for item in exhausted}),
                    }
            # Whatever the last failure was called (a contract refusal or an
            # unproved send), a verifier stage that stays on the retired 0.1
            # contract and whose every refused reply was a restated pass will
            # be refused the same way by any retry.
            if kind is None and stage == "independent_qualitative_verifier":
                attempts = _stage_evidence(attempts_of, "independent_qualitative_verifier")
                restated = [item for item in attempts
                            if _RESTATEMENT_ERROR in str(item["rejection"] or "")]
                if restated and len(restated) == len(
                        [item for item in attempts if item["rejection"]]):
                    kind, evidence = "verifier_restatement_under_retired_contract", {
                        "refused_passes": len(restated)}
                failures = [item for item in attempts
                            if item["status"] in {"failed", "retryable"}]
                refused = [item for item in failures
                           if item["error_code"] in _PREFLIGHT_REFUSED_CODES]
                if (kind is None and len(refused) >= 2
                        and failures[-1]["error_code"] in _PREFLIGHT_REFUSED_CODES):
                    kind, evidence = "verifier_refused_before_send", {
                        "refused_before_send": len(refused),
                        "error_code": failures[-1]["error_code"],
                        "models": sorted({str(item["model"]) for item in refused}),
                    }
            if kind is None:
                continue
            out.append({"admission_ref": admission_ref, "hold_reason": reason,
                        "disposition": held["disposition"], "stage": stage,
                        "class": kind, "why": UNRECOVERABLE_CLASSES[kind],
                        "evidence": evidence})
    finally:
        if connection is not None:
            connection.close()
    return out


def withdrawal_preview(state: Path, admission_ref: str) -> dict[str, Any]:
    """Read-only: whether the writer would accept this withdrawal now."""

    from datetime import datetime as _datetime

    from .mission_document_research_launcher import TICKETS_DIRNAME
    from .mission_document_research_withdrawal import (
        running_work_orders, withdrawable_hold,
    )

    try:
        held = _lane_holds(state).get(admission_ref)
    except Exception as exc:  # noqa: BLE001
        return {"admission_ref": admission_ref, "blockers": [f"hold_ledger:{exc}"]}
    blockers = []
    allowed, why = withdrawable_hold(held)
    if not allowed:
        blockers.append("hold:" + why)
    if isinstance(held, dict) and isinstance(held.get("ticket_ref"), str):
        ticket_path = (Path(state) / TICKETS_DIRNAME
                       / held["ticket_ref"].split(":")[-1] / "ticket.json")
        try:
            if json.loads(ticket_path.read_text(encoding="utf-8")).get("status") == "running":
                blockers.append("child:running:" + held["ticket_ref"])
        except (OSError, ValueError, AttributeError):
            pass
    connection = _read_only_core(state)
    try:
        if connection is not None:
            blockers.extend("work:running:" + ref for ref in running_work_orders(
                connection, admission_ref, now=_datetime.now(timezone.utc)))
        # The same refusal the writer makes (``withdrawal_blockers``): a
        # recovery or re-entry the owner granted that the lane has not yet run.
        waiting = _pending_owner_recovery(state, connection, admission_ref)
    finally:
        if connection is not None:
            connection.close()
    for item in (waiting or {}).get("pending", []):
        if item.get("operation") == OPERATION:
            blockers.append("owner_reentry:pending:" + str(item["grant_path"]))
        else:
            blockers.append("owner_recovery:pending:" + str(item["authorization_ref"]))
    withdrawn = _read_only_withdrawals(state).get(admission_ref)
    return {"admission_ref": admission_ref,
            "hold": None if held is None else {
                "disposition": held["disposition"], "reason": held["reason"]},
            "already_withdrawn": withdrawn,
            "blockers": blockers}


def withdraw_all_unrecoverable(state: Path, *, actor: str | None, apply: bool,
                               only: Iterable[str] | None = None) -> int:
    skipped: list[dict[str, Any]] = []
    items = unrecoverable_holds(state, skipped=skipped)
    if only is not None:
        wanted = set(only)

        def chosen(item: dict[str, Any]) -> bool:
            return (item["admission_ref"] in wanted
                    or item["admission_ref"].rsplit(":", 1)[-1] in wanted)

        items = [item for item in items if chosen(item)]
        skipped = [item for item in skipped if chosen(item)]
    for item in skipped:
        # Said out loud so the owner sees why a hold they expected is absent.
        print(json.dumps(item, ensure_ascii=False))
    results = []
    for item in items:
        entry = dict(item)
        preview = withdrawal_preview(state, item["admission_ref"])
        if preview["already_withdrawn"] is not None:
            entry["result"] = {"status": "already_withdrawn"}
        elif preview["blockers"]:
            entry["result"] = {"status": "blocked", "blockers": preview["blockers"]}
        elif not apply:
            entry["result"] = {"status": "dry_run"}
        else:
            entry["result"] = _call(state, operation=WITHDRAW_OPERATION, actor=str(actor),
                                    params={"admission_ref": item["admission_ref"],
                                            "actor_ref": actor,
                                            "reason": "unrecoverable:" + item["class"]
                                            + ": " + item["why"]})
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    counts: dict[str, int] = {}
    for entry in results:
        status = str(entry["result"].get("status"))
        counts[status] = counts.get(status, 0) + 1
    classes: dict[str, int] = {}
    for entry in results:
        classes[entry["class"]] = classes.get(entry["class"], 0) + 1
    _print({"unrecoverable": len(items), "by_class": classes, "results": counts,
            "skipped_pending_owner_recovery": len(skipped),
            "applied": bool(apply)})
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("holds", help="read-only: what is held and why")
    listing.add_argument("--state-dir", required=True)
    for name, note in (
        ("authorize-reentry",
         "allow one more controlled re-entry for one admission"),
        ("authorize-paid",
         "buy one more reply for a contract failure already retried once"),
        ("authorize-unproved",
         "buy one more call for an unproved send already retried once"),
    ):
        grant = sub.add_parser(name, help=note)
        grant.add_argument("--state-dir", required=True)
        grant.add_argument("--admission-ref", required=True)
        grant.add_argument("--actor", default=None,
                           help="human:<owner>; required with --apply")
        grant.add_argument("--apply", action="store_true")
        if name != "authorize-reentry":
            grant.add_argument(
                "--max-cost-usd", type=float, default=None,
                help="refuse if the failed stage's own budget exceeds this")
            grant.add_argument(
                "--even-if-unrecoverable", action="store_true",
                help="buy the call even though the evidence says a retry "
                     "fails the same way")
    every = sub.add_parser(
        "authorize-all-escalated",
        help="authorize every escalated hold, oldest first, under one cap")
    every.add_argument("--state-dir", required=True)
    every.add_argument("--max-total-cost-usd", type=float, required=True)
    every.add_argument("--actor", default=None,
                       help="human:<owner>; required with --apply")
    every.add_argument("--apply", action="store_true")
    every.add_argument("--include-unrecoverable", action="store_true",
                       help="also buy calls for holds classified as unrecoverable")
    withdraw = sub.add_parser(
        "withdraw", help="close one escalated or terminal admission for good")
    withdraw.add_argument("--state-dir", required=True)
    withdraw.add_argument("--admission-ref", required=True)
    withdraw.add_argument("--reason", required=True)
    withdraw.add_argument("--actor", default=None,
                          help="human:<owner>; required with --apply")
    withdraw.add_argument("--apply", action="store_true")
    hopeless = sub.add_parser(
        "withdraw-all-unrecoverable",
        help="list (default) or withdraw every hold whose retry is known to be pointless")
    hopeless.add_argument("--state-dir", required=True)
    hopeless.add_argument("--only", action="append", default=None,
                          help="restrict to these admission refs (or their hex); repeatable")
    hopeless.add_argument("--actor", default=None,
                          help="human:<owner>; required with --apply")
    hopeless.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    state = Path(args.state_dir).expanduser()
    if args.command == "holds":
        _print(holds(state))
        return 0
    if getattr(args, "apply", False) and (
            not args.actor or not args.actor.startswith("human:")):
        parser.error("--apply needs --actor human:<owner>")
    if args.command == "authorize-all-escalated":
        return authorize_all_escalated(
            state, actor=args.actor, apply=args.apply,
            max_total_cost_usd=args.max_total_cost_usd,
            include_unrecoverable=args.include_unrecoverable)
    if args.command == "withdraw-all-unrecoverable":
        return withdraw_all_unrecoverable(
            state, actor=args.actor, apply=args.apply, only=args.only)
    if args.command == "withdraw":
        if not args.reason.strip():
            parser.error("--reason must say why")
        preview = withdrawal_preview(state, args.admission_ref)
        _print({**preview, "operation": WITHDRAW_OPERATION, "reason": args.reason})
        if preview["already_withdrawn"] is not None:
            print("already withdrawn; nothing to do", file=sys.stderr)
            return 0
        if preview["blockers"]:
            print("refused: " + ", ".join(preview["blockers"])
                  + " (a withdrawal needs an escalated or terminal hold and nothing of it "
                  "running)", file=sys.stderr)
            return 1
        if not args.apply:
            print("dry run; add --apply --actor human:<owner> to withdraw it",
                  file=sys.stderr)
            return 0
        _print(_call(state, operation=WITHDRAW_OPERATION, actor=args.actor,
                     params={"admission_ref": args.admission_ref,
                             "actor_ref": args.actor, "reason": args.reason}))
        return 0
    operation = {
        "authorize-reentry": OPERATION,
        "authorize-paid": PAID_OPERATION,
        "authorize-unproved": UNPROVED_OPERATION,
    }[args.command]
    matching = [item for item in escalated_holds(state)
                if item["admission_ref"] == args.admission_ref]
    _print({
        "admission_ref": args.admission_ref,
        "escalated": bool(matching),
        "reason": matching[0]["reason"] if matching else None,
        "operation": operation,
        "lane_door_for_this_reason": (
            matching[0]["operation"] if matching else None),
        "max_cost_usd": getattr(args, "max_cost_usd", None),
    })
    if not matching:
        # Not a refusal: a hold the lane has not escalated is one it is still
        # working on by itself, and granting a re-entry there would be the
        # owner doing the lane's job.
        print("this admission is not waiting on a person; nothing to authorize",
              file=sys.stderr)
        return 1
    if matching[0]["operation"] not in (None, operation):
        # The reason names a different door.  Say which, rather than spending
        # a call proving that this one does not fit.
        print("this admission's reason leads to "
              + COMMANDS[matching[0]["operation"]] + ", not " + args.command,
              file=sys.stderr)
        return 1
    door_used = [record for record in owner_doors_used(state).get(
        args.admission_ref, []) if record["operation"] == operation]
    if door_used:
        print("the owner already opened this door at "
              f"{door_used[-1]['authorized_at']}; the executor grants it once "
              "and would refuse this call", file=sys.stderr)
        return 1
    found = (None if getattr(args, "even_if_unrecoverable", False)
             else _hopeless_by_ref(state).get(args.admission_ref))
    if found is not None and operation != OPERATION:
        # 2026-10-03: live legacy, four owner-bought retries came back empty
        # at the same frozen 4096-token cap within the hour.  Say so before
        # spending, and name the door that does help.
        _print({"refused": "unrecoverable", "class": found["class"],
                "why": found["why"], "evidence": found.get("evidence"),
                "withdraw": _withdraw_command(
                    state, args.admission_ref,
                    "unrecoverable:" + found["class"] + ": " + found["why"])})
        print("refused: the evidence says a retry fails the same way ("
              + found["class"] + "); withdraw it instead, or add "
              "--even-if-unrecoverable to buy the call anyway", file=sys.stderr)
        return 1
    if not args.apply:
        print("dry run; add --apply --actor human:<owner> to authorize it",
              file=sys.stderr)
        return 0
    params: dict[str, Any] = {"admission_ref": args.admission_ref,
                              "actor_ref": args.actor}
    if getattr(args, "max_cost_usd", None) is not None:
        params["max_cost_usd"] = args.max_cost_usd
    result = _with_state(
        _call(state, operation=operation, actor=args.actor, params=params), state)
    _print(result)
    if isinstance(result, dict) and result.get("status") == "refused":
        print("refused by the executor: " + str(result.get("reason"))
              + (("; next: " + str(result["next_command"]))
                 if result.get("next_command") else ""), file=sys.stderr)
        return 1
    if isinstance(result, dict) and result.get("status") in {
            "deferred", "error", "still_running", "interrupted", "unknown_job"}:
        print(str(result.get("explanation") or result.get("note")
                  or result.get("message") or result.get("status")), file=sys.stderr)
        return 1
    return 0


__all__ = [
    "DOORS", "LANE_OWNS_IT", "OPERATION", "PAID_OPERATION", "UNPROVED_OPERATION",
    "UNRECOVERABLE_CLASSES", "WITHDRAW_OPERATION",
    "authorize_all_escalated", "door_for", "door_opened_since",
    "escalated_holds", "holds", "main", "owner_command", "owner_doors_used",
    "unrecoverable_holds", "withdraw_all_unrecoverable", "withdrawal_preview",
]


if __name__ == "__main__":
    raise SystemExit(main())
