#!/usr/bin/env python3
"""P13z: give back the retry budget an outage spent without testing anything.

A filing is dispatched at most three times, each with a wider window, because
re-queuing an identical window replays the same failure.  That is right while
the failure is a property of the filing.

It was not.  A connector-profile conflict killed every SEC run for a day: it
would have killed any window for any company equally, and it consumed all three
attempts on every filing five companies still needed.  The lane then refused to
try again -- permanently, on evidence nobody had gathered.  Fixing the conflict
does nothing on its own, because the budget is already gone.

So this withdraws the attempts, and only the ones whose runs failed for a
reason given on the command line.  Withdrawal is recorded, never a deletion:
the dispatch happened, it simply proved nothing about the filing.

**Deliberately not automatic.**  Inferring "this failure was not the filing's
fault" is how a genuinely dead filing gets retried forever.  Someone has to
name the outage, and the name is kept beside every attempt it excuses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.coverage_mission import (  # noqa: E402
    SEC_RUN_SUCCEEDED,
    CoverageMissionAuthority,
)
from dalton_core.store import DaltonStore  # noqa: E402

PRECONDITION_PREFIX = "lane precondition failed:"


def run_error(state_dir: Path | None, ticket_ref: str | None) -> str | None:
    """What the lane run itself said went wrong, from its summary on disk.

    Settlements written before P13z have no ``failure_reason`` -- the column
    did not exist -- and backfilling an append-only ledger is not on offer. The
    evidence is still where the run left it, so it is read from there rather
    than invented, the same way the unattributed-metric audit reads the
    extraction summaries.

    A summary that reports ``ok`` is not an error, even when its ticket was
    marked orphaned: nine live runs did their work and lost their ticket to a
    restart, and calling those failures would excuse attempts that succeeded.
    """

    if state_dir is None or not isinstance(ticket_ref, str) or ":" not in ticket_ref:
        return None
    directory = state_dir / "sec-lane-runs" / ticket_ref.split(":", 1)[1]
    try:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict):
        if summary.get("ok") is True:
            return None
        for issuer in summary.get("issuers") or ():
            if isinstance(issuer, dict) and isinstance(issuer.get("error"), str):
                return issuer["error"].strip()
        return None
    # A run that failed its preconditions never wrote a summary at all -- it
    # refused before opening anything -- so its only trace is the log line.
    # Those are exactly the infrastructure failures worth telling apart from a
    # filing that genuinely cannot be fetched, and reading nothing would leave
    # them unattributable and therefore unforgivable.
    try:
        log = (directory / "run.log").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in log.splitlines():
        if line.startswith(PRECONDITION_PREFIX):
            return line.strip()
    return None


def candidates(
    store: DaltonStore, *, match: str | None, state_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Settled dispatches whose runs did not succeed, newest last.

    A run that succeeded is never a candidate: its attempt tested the filing
    and the answer was yes.
    """

    rows = store.connection.execute(
        "SELECT d.dispatch_id, d.company_ref, d.ticker, d.expected_accession, "
        "d.ticket_ref, s.detail, s.failure_reason, s.settled_at "
        "FROM coverage_mission_sec_dispatches d "
        "JOIN coverage_mission_sec_dispatch_settlements s ON s.dispatch_id=d.dispatch_id "
        "LEFT JOIN coverage_mission_sec_dispatch_attempt_voids v "
        "ON v.dispatch_id=d.dispatch_id "
        "WHERE v.dispatch_id IS NULL AND s.detail IS NOT ? "
        "ORDER BY s.settled_at, d.dispatch_id",
        (SEC_RUN_SUCCEEDED,),
    ).fetchall()
    selected = []
    for row in rows:
        item = dict(row)
        item["run_error"] = item.get("failure_reason") or run_error(
            state_dir, item.get("ticket_ref"))
        if match is not None:
            haystack = f"{item.get('detail') or ''} {item.get('run_error') or ''}"
            if match.lower() not in haystack.lower():
                continue
        selected.append(item)
    return selected


def attempts_by_accession(store: DaltonStore) -> dict[str, int]:
    """What the retry budget currently reads, after voids."""

    rows = store.connection.execute(
        "SELECT d.expected_accession AS accession, COUNT(*) AS n "
        "FROM coverage_mission_sec_dispatches d "
        "LEFT JOIN coverage_mission_sec_dispatch_attempt_voids v "
        "ON v.dispatch_id=d.dispatch_id "
        "WHERE v.dispatch_id IS NULL GROUP BY d.expected_accession"
    ).fetchall()
    return {row["accession"]: int(row["n"]) for row in rows if row["accession"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="service.json")
    parser.add_argument("--reason", required=True,
                        help="the outage these attempts are being excused for")
    parser.add_argument("--voided-by", required=True,
                        help="who is withdrawing them, e.g. agent:dalton-core")
    parser.add_argument("--match", default=None,
                        help="only attempts whose detail or failure reason contains this; "
                             "omit to void every unsuccessful attempt")
    parser.add_argument("--apply", action="store_true",
                        help="without this the withdrawal is only described")
    args = parser.parse_args(list(argv) if argv is not None else None)

    service = json.loads(args.config.expanduser().resolve().read_text(encoding="utf-8"))
    core_db = Path(service["core_db"])
    store = DaltonStore(str(core_db))
    try:
        authority = CoverageMissionAuthority(store)
        selected = candidates(store, match=args.match, state_dir=core_db.parent)
        result: dict[str, Any] = {
            "status": "applied" if args.apply else "planned",
            "reason": args.reason,
            "match": args.match,
            "attempts_to_void": len(selected),
            "by_ticker": dict(Counter(item["ticker"] for item in selected)),
            "by_detail": dict(Counter(item["detail"] for item in selected)),
            # What the runs themselves said, so the reason given on the command
            # line can be checked against the evidence rather than trusted.
            "by_run_error": dict(Counter(
                (item.get("run_error") or "<none recorded>")[:70] for item in selected)),
            "accessions_affected": len({item["expected_accession"] for item in selected}),
            "attempts_before": attempts_by_accession(store),
        }
        if args.apply:
            applied = 0
            for item in selected:
                outcome = authority.void_sec_dispatch_attempt(
                    item["dispatch_id"], reason=args.reason, voided_by=args.voided_by)
                applied += int(outcome["status_marker"] == "fresh")
            result["applied"] = applied
            result["attempts_after"] = attempts_by_accession(store)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script
    sys.exit(main())
