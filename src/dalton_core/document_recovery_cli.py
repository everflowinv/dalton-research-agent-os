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
from pathlib import Path
from typing import Any, Iterable

OPERATION = "authorize_mission_document_reentry"
PAID_OPERATION = "authorize_mission_document_paid_recovery"
UNPROVED_OPERATION = "authorize_mission_document_unproved_recovery"
# Which door each escalated hold reason leads to.  The lane writes the reason;
# the owner should never have to translate it into an operation name.
DOORS: dict[str, str] = {
    "contract_failed_after_automatic_retry": PAID_OPERATION,
    "unproved_send_failed_after_automatic_retry": UNPROVED_OPERATION,
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


def holds(state_dir: Path) -> dict[str, Any]:
    """Every held admission in this state directory, and who is waiting."""

    ledgers = _ledger(state_dir)
    return {
        lane: {
            "holds_path": value["path"],
            "held": len(value["waiting"]),
            "waiting_on_owner": len(value["escalated"]),
            "escalated": value["escalated"],
            "other": [item for item in value["waiting"]
                      if item not in value["escalated"]],
        }
        for lane, value in sorted(ledgers.items())
    }


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
    """The writer operation this hold reason leads to, if any."""

    for name, operation in DOORS.items():
        if name in reason:
            return operation
    return None


def _call(state: Path, *, operation: str, actor: str,
          params: dict[str, Any]) -> Any:
    from .governance_cli import ephemeral_call

    return ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=actor, operation=operation, params=params,
    )


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


def authorize_all_escalated(
    state: Path, *, actor: str | None, apply: bool,
    max_total_cost_usd: float,
) -> int:
    """Walk the escalated pile oldest-first and stop at the total cap.

    Each admission is authorized with the *remaining* budget as its own cap,
    so a stage that costs more than what is left is refused by the writer
    rather than silently overspending it; the re-entry door costs nothing and
    is taken whenever it is the door this reason leads to.
    """

    items = escalated_holds(state)
    remaining, spent, results = float(max_total_cost_usd), 0.0, []
    for item in items:
        operation = item["operation"]
        entry = {"admission_ref": item["admission_ref"],
                 "reason": item["reason"], "operation": operation,
                 "started_at": item["started_at"]}
        if operation is None:
            entry["result"] = {"status": "no_door_for_this_reason"}
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
            entry["result"] = _call(
                state, operation=operation, actor=str(actor), params=params)
            cost = _cost(entry["result"])
            spent += cost
            remaining -= cost
        entry["running_total_usd"] = round(spent, 6)
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    _print({"escalated": len(items), "handled": len(results),
            "total_authorized_usd": round(spent, 6),
            "max_total_cost_usd": float(max_total_cost_usd),
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
    every = sub.add_parser(
        "authorize-all-escalated",
        help="authorize every escalated hold, oldest first, under one cap")
    every.add_argument("--state-dir", required=True)
    every.add_argument("--max-total-cost-usd", type=float, required=True)
    every.add_argument("--actor", default=None,
                       help="human:<owner>; required with --apply")
    every.add_argument("--apply", action="store_true")
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
            max_total_cost_usd=args.max_total_cost_usd)
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
    if not args.apply:
        print("dry run; add --apply --actor human:<owner> to authorize it",
              file=sys.stderr)
        return 0
    params: dict[str, Any] = {"admission_ref": args.admission_ref,
                              "actor_ref": args.actor}
    if getattr(args, "max_cost_usd", None) is not None:
        params["max_cost_usd"] = args.max_cost_usd
    _print(_call(state, operation=operation, actor=args.actor, params=params))
    return 0


__all__ = [
    "DOORS", "OPERATION", "PAID_OPERATION", "UNPROVED_OPERATION",
    "authorize_all_escalated", "door_for", "escalated_holds",
    "holds", "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
