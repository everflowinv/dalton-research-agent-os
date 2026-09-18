"""The owner's door for a document-research admission the lane has given up on.

    # read-only: what is actually held, and which of it is waiting on a person
    .venv/bin/python -m dalton_core.document_recovery_cli holds \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core"

    # allow one more controlled re-entry for one named admission
    .venv/bin/python -m dalton_core.document_recovery_cli authorize-reentry \\
        --state-dir "..." --admission-ref mission-document-research-admission:… \\
        --apply --actor human:lumos

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


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("holds", help="read-only: what is held and why")
    listing.add_argument("--state-dir", required=True)
    grant = sub.add_parser(
        "authorize-reentry",
        help="allow one more controlled re-entry for one admission")
    grant.add_argument("--state-dir", required=True)
    grant.add_argument("--admission-ref", required=True)
    grant.add_argument("--actor", default=None,
                       help="human:<owner>; required with --apply")
    grant.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    state = Path(args.state_dir).expanduser()
    if args.command == "holds":
        print(json.dumps(holds(state), ensure_ascii=False, indent=1))
        return 0
    held = holds(state).get("mission_document_research", {})
    matching = [item for item in held.get("escalated", [])
                if item["admission_ref"] == args.admission_ref]
    print(json.dumps({
        "admission_ref": args.admission_ref,
        "escalated": bool(matching),
        "reason": matching[0]["reason"] if matching else None,
        "operation": OPERATION,
    }, ensure_ascii=False, indent=1))
    if not matching:
        # Not a refusal: a hold the lane has not escalated is one it is still
        # working on by itself, and granting a re-entry there would be the
        # owner doing the lane's job.
        print("this admission is not waiting on a person; nothing to authorize",
              file=sys.stderr)
        return 1
    if not args.apply:
        print("dry run; add --apply --actor human:<owner> to grant one re-entry",
              file=sys.stderr)
        return 0
    if not args.actor or not args.actor.startswith("human:"):
        parser.error("--apply needs --actor human:<owner>")
    from .governance_cli import ephemeral_call

    result = ephemeral_call(
        state / "writer-tokens.json", state / "run" / "writer.sock",
        actor_ref=args.actor, operation=OPERATION,
        params={"admission_ref": args.admission_ref, "actor_ref": args.actor},
    )
    print(json.dumps(result if isinstance(result, (dict, list)) else {"result": result},
                     ensure_ascii=False, indent=1))
    return 0


__all__ = ["OPERATION", "holds", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
