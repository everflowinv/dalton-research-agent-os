#!/usr/bin/env python3
"""Settle cockpit day-ledger reservations whose Scheduler attempt is over.

Dry run by default: prints the plan (what would be settled, at what, and why
anything was skipped) and writes nothing.  ``--apply`` writes one settlement
per planned admission through the ordinary ledger API.  See
``dalton_core.cockpit_reservation_recovery`` for the rules.

Typical use on the legacy environment, after the fix is deployed:

    STATE="$HOME/Library/Application Support/Dalton/state/dalton-core"
    python3 scripts/settle_orphan_cockpit_reservations.py \\
        --budget-db "$STATE/thesis-impact-budget.sqlite" \\
        --scheduler-db "$STATE/scheduler.sqlite" \\
        --broker-journal "$HOME/.openclaw/dalton-model-broker.sock.journal.json"
    # review, then the same command with --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.cockpit_reservation_recovery import (
    DEFAULT_PURPOSES,
    UNKNOWN_COST_POLICIES,
    apply_plan,
    plan_orphan_settlements,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--budget-db", type=Path, required=True)
    parser.add_argument("--scheduler-db", type=Path, required=True,
                        help="the Scheduler the cockpit lanes claim attempts from")
    parser.add_argument("--broker-journal", type=Path, default=None,
                        help="dalton-model-broker journal; metered costs come from here")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--purpose", action="append", dest="purposes",
                       help=f"repeatable; default {', '.join(DEFAULT_PURPOSES)}")
    scope.add_argument("--all-cockpit", action="store_true",
                       help="every work:cockpit-* purpose")
    parser.add_argument("--unknown-cost", choices=UNKNOWN_COST_POLICIES,
                        default="reserved",
                        help="settlement when the journal has no metered cost "
                             "(default: the reservation, the conservative choice)")
    parser.add_argument("--apply", action="store_true",
                        help="write the settlements (default is a dry run)")
    parser.add_argument("--details", action="store_true",
                        help="print every planned and skipped admission")
    args = parser.parse_args(argv)

    plan = plan_orphan_settlements(
        budget_db=args.budget_db.expanduser(),
        scheduler_db=args.scheduler_db.expanduser(),
        broker_journal=(None if args.broker_journal is None
                        else args.broker_journal.expanduser()),
        now=datetime.now(timezone.utc),
        purposes=None if args.all_cockpit else (args.purposes or DEFAULT_PURPOSES),
        unknown_cost=args.unknown_cost,
    )
    by_purpose: dict[str, dict[str, int]] = {}
    for item in plan["settle"]:
        row = by_purpose.setdefault(item["purpose"] or "?", {
            "count": 0, "reserved_micros": 0, "settled_micros": 0,
            "from_broker_journal": 0})
        row["count"] += 1
        row["reserved_micros"] += item["reserved_micros"]
        row["settled_micros"] += item["actual_micros"]
        row["from_broker_journal"] += item["source"] == "broker_journal"
    skipped: dict[str, int] = {}
    for item in plan["skipped"]:
        skipped[item["reason"]] = skipped.get(item["reason"], 0) + 1
    report = {
        "mode": "apply" if args.apply else "dry_run",
        "totals": plan["totals"],
        "by_purpose": by_purpose,
        "skipped_by_reason": skipped,
    }
    if args.details:
        report["plan"] = plan
    if args.apply:
        results = apply_plan(plan)
        report["applied"] = {
            status: sum(r["status"] == status for r in results)
            for status in ("fresh", "duplicate", "refused")
        }
        report["refused"] = [r for r in results if r["status"] == "refused"]
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 1 if args.apply and report.get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
