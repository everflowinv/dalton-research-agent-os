"""Prepare or apply one reviewed controlled-failure recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .controlled_failure_redrive import apply, prepare
from .store import canonical_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler-db", required=True)
    parser.add_argument("--budget-db", required=True)
    parser.add_argument("--old-work-order-ref")
    parser.add_argument("--candidate")
    parser.add_argument("--expected-candidate-hash")
    args = parser.parse_args(argv)
    if bool(args.old_work_order_ref) == bool(args.candidate):
        parser.error("choose exactly one of --old-work-order-ref or --candidate")
    if args.old_work_order_ref:
        result = prepare(
            scheduler_db=args.scheduler_db, budget_db=args.budget_db,
            old_work_order_ref=args.old_work_order_ref,
        )
    else:
        if not args.expected_candidate_hash:
            parser.error("--candidate requires --expected-candidate-hash")
        result = apply(
            scheduler_db=args.scheduler_db, budget_db=args.budget_db,
            candidate=json.loads(Path(args.candidate).read_text("utf-8")),
            expected_candidate_hash=args.expected_candidate_hash,
        )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
