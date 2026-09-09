#!/usr/bin/env python3
"""P14a read-only smoke: what the last few days would have produced.

Copies nothing back and calls no model.  Point it at a *copy* of a Core --
never the live file -- and it prints, per company, the events the tracking
lane would have recorded, the abnormal moves the price detector would have
found over the last N settled trading days, what each source is due for, and
the source capability table the judgement prompt would carry.

    python3 scripts/run_p14a_tracking_smoke.py --core /tmp/p14a-core.sqlite
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.market_event import (  # noqa: E402
    detect_abnormal_moves,
    detect_price_divergences,
    recent_settled_dates,
)
from dalton_core.research_event import (  # noqa: E402
    claim_event_candidates,
    document_event_candidates,
    reconciliation_event_candidates,
)
from dalton_core.source_capability_map import build_map, prompt_table  # noqa: E402
from dalton_core.tracking_cadence import load_policy  # noqa: E402


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def active_mission(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        "SELECT v.record_json AS record_json FROM coverage_mission_pointer p "
        "JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id "
        "ORDER BY p.mission_ref LIMIT 1"
    ).fetchone()
    if row is None:
        raise SystemExit("this Core has no active mission")
    return json.loads(row["record_json"])


def screened(connection: sqlite3.Connection, mission: dict) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT company_ref FROM coverage_mission_stage_records "
        "WHERE mission_version_ref=? AND stage_ref='initial_screen' AND status='gate_passed'",
        (mission["id"],),
    ).fetchall()
    passed = {row["company_ref"] for row in rows}
    return [member["company_ref"] for member in mission["universe"]
            if member["company_ref"] in passed]


def price_series(connection: sqlite3.Connection, company_ref: str) -> dict | None:
    try:
        row = connection.execute(
            "SELECT record_json FROM market_price_series_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (company_ref,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    record = json.loads(row["record_json"])
    return {"version_ref": record["id"], "bars": record["bars"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--core", type=Path, required=True,
                        help="a COPY of core.sqlite; never the live file")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--policy", type=Path, default=None)
    args = parser.parse_args(argv)

    connection = read_only(args.core)
    policy = load_policy(args.policy)
    mission = active_mission(connection)
    tracked = screened(connection, mission)
    now = datetime.now(timezone.utc)

    print(f"mission     : {mission['id']}")
    print(f"policy      : {policy['policy_ref']} ({policy['content_hash'][:12]})")
    print(f"universe    : {len(mission['universe'])} companies")
    print(f"tracked     : {len(tracked)} with a passed Initial Screen")
    print(f"grants      : may_write = {sorted(mission['autonomy']['may_write'])}")
    for word in ("market_event", "observation", "deliverable"):
        held = word in mission["autonomy"]["may_write"]
        print(f"  {word:<16} {'granted' if held else 'NOT GRANTED — the lane would be idle'}")
    print()

    series = {member["company_ref"]: price_series(connection, member["company_ref"])
              for member in mission["universe"]}
    window = int(policy["abnormal_move"].get("window_trading_days") or 10)
    window_dates = recent_settled_dates(series, limit=window)
    dates = recent_settled_dates(series, limit=args.days)
    if dates:
        print(f"price days  : {dates[0]} .. {dates[-1]}")
        moves = []
        for as_of in dates:
            moves.extend(detect_abnormal_moves(
                series_by_company=series, as_of=as_of,
                thresholds=policy["abnormal_move"],
            ))
        print(f"abnormal    : {len(moves)}")
        for move in moves:
            payload = move["payload"]
            print(f"  {payload['as_of']} {move['company_ref']} "
                  f"{payload['return_percent']}% vs basket "
                  f"{payload['excess_vs_basket_percent']} [{payload['trigger']}]")
        stances = {ref: {"thesis_ref": "thesis:unknown",
                         "stance": policy["thesis_stances"]["default"]}
                   for ref in tracked}
        divergences = detect_price_divergences(
            series_by_company=series, dates=window_dates,
            thresholds=policy["abnormal_move"], stances=stances,
        )
        print(f"divergences : {len(divergences)} over {len(window_dates)} settled days "
              f"(stance assumed {policy['thesis_stances']['default']} for every thesis)")
        for row in divergences:
            payload = row["payload"]
            print(f"  {payload['from_date']}..{payload['as_of']} {row['company_ref']} "
                  f"{payload['divergence_percent']} against the thesis")
    else:
        print("price days  : none — this Core holds no MarketPriceSeriesVersion, so "
              "neither the abnormal-move nor the divergence emitter would record "
              "anything (P11a's lane has not run here)")
    print()

    total = 0
    for company_ref in tracked:
        candidates = document_event_candidates(
            connection, company_ref=company_ref, mission_ref=mission["mission_ref"],
            now=now, lookback_days=args.lookback_days,
        )
        candidates += claim_event_candidates(
            connection, company_ref=company_ref, now=now,
            lookback_days=args.lookback_days,
        )
        candidates += reconciliation_event_candidates(
            connection, company_ref=company_ref, now=now,
            lookback_days=args.lookback_days,
        )
        by_kind: dict[str, int] = {}
        for candidate in candidates:
            by_kind[candidate["kind"]] = by_kind.get(candidate["kind"], 0) + 1
        total += len(candidates)
        ticker = next((m.get("ticker") for m in mission["universe"]
                       if m["company_ref"] == company_ref), "")
        print(f"{ticker:<5} {company_ref}: {len(candidates)} events {by_kind}")
    print()
    print(f"total       : {total} events over the last {args.lookback_days} days")
    print(f"judgement   : {total} bounded calls at $0.10 a pair would cost "
          f"${total * 0.1:.2f}; the event_response pool is "
          f"${float(mission['budget']['max_daily_cost_usd']) * 0.15:.2f} a day")
    print()
    print("source capability map (what the judgement prompt would carry):")
    print(prompt_table(build_map(mission=mission, cadences=policy["cadences"]),
                       connected_only=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
