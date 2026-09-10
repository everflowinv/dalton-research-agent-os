"""P14a: the child that turns a day's ledger movement into events.

It makes no model call and reaches no network.  Everything it does is a read
of ledgers this Core already holds -- price versions, discovered documents,
Claims, reconciliation rows -- and an append to the event ledger, which is
idempotent on what the event says.  So a run that finds nothing new costs a
handful of queries, and running it twice in one tick is indistinguishable from
running it once.

It is deliberately not selective.  Every company whose Initial Screen has
passed is scanned on every run, because the owner's instruction is that
tracking is resident: a lane that picked one company per tick would have five
covered names looked at once every five ticks, which is not daily tracking, it
is a queue with a nicer name.

Exit 0 when the run completed, including when it recorded nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from .coverage_mission import CoverageMissionAuthority
from .market_event import (
    DEFAULT_THESIS_STANCE,
    detect_abnormal_moves,
    detect_price_divergences,
    recent_settled_dates,
)
from .market_price import MarketPriceSeriesAuthority
from .event_judgement import company_theses
from .research_event import (
    DEFAULT_LOOKBACK_DAYS,
    ResearchEventAuthority,
    ResearchEventError,
    claim_event_candidates,
    document_event_candidates,
    reconciliation_event_candidates,
    record_event,
)
from .buyback_disclosure import buyback_event_candidates
from .insider_trading_plan import trading_plan_event_candidates
from .store import DaltonStore
from .tracking_cadence import (
    TrackingCadenceAuthority,
    TrackingCadenceError,
    due_sources,
    load_policy,
    screen_passed_companies,
)

SUMMARY_SCHEMA_VERSION = "0.1"
MAX_EVENTS_PER_RUN = 120
# How many settled trading days back the price detector looks. Short: an
# abnormal move six days old is history, and the reason it happened has
# already been written or never will be.
PRICE_LOOKBACK_DAYS = 5


def _write_owner_only(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def missing_write_scopes(mission: Any) -> list[str]:
    """The words this lane needs and the mission may not have granted.

    ``market_event`` is the event ledger (P11d's MarketEvent was folded into
    ResearchEvent); ``observation`` is the cadence chain.  Both are checked
    before anything is read, so a mission that grants neither costs one query
    per tick rather than a scan whose writes all fail at the end.
    """

    granted = set(mission["autonomy"]["may_write"])
    return [word for word in ("market_event", "observation") if word not in granted]


def price_events(
    prices: MarketPriceSeriesAuthority,
    mission: Any,
    policy: Any,
    *,
    tracked: list[str],
    lookback: int = PRICE_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Abnormal moves for the tracked companies over the last few settled days.

    The basket is built from the whole mission universe rather than from the
    tracked subset: a company that has not passed its screen yet is still a
    peer, and dropping it would make the comparator narrower exactly when
    coverage is being widened.
    """

    universe = [member["company_ref"] for member in mission["universe"]]
    series = {ref: prices.series(ref) for ref in universe}
    thresholds = policy["abnormal_move"]
    benchmark_ref = next(iter(thresholds.get("benchmark_refs") or ()), None)
    benchmark = prices.series(benchmark_ref) if benchmark_ref else None
    dates = recent_settled_dates(series, limit=lookback)
    events: list[dict[str, Any]] = []
    for as_of in dates:
        for event in detect_abnormal_moves(
            series_by_company=series, as_of=as_of, thresholds=thresholds,
            benchmark_series=benchmark, benchmark_ref=benchmark_ref,
        ):
            if event["company_ref"] in tracked:
                events.append(event)
    return events


def thesis_stances(
    store: DaltonStore, policy: Any, *, tracked: list[str]
) -> dict[str, dict[str, str]]:
    """company -> the one thesis the divergence detector reads it against.

    The newest thesis bound to the company, or -- when the company has none of
    its own -- the industry thesis that covers it, because "US IT services
    demand is bottoming" is the thing an ACN price divergence most often bears
    on and a company with no thesis of its own would otherwise never diverge
    from anything.
    """

    overrides = policy["thesis_stances"]["overrides"]
    default = policy["thesis_stances"]["default"]
    stances: dict[str, dict[str, str]] = {}
    for company_ref in tracked:
        theses = company_theses(store.connection, company_ref)
        if not theses:
            continue
        # The newest, by the time it was admitted rather than by version id:
        # company_theses returns oldest first for exactly this reason, because
        # a version id is a hash and hash order is not time order.
        own = [row for row in theses if row.get("subject_ref") == company_ref]
        chosen = max(
            (own or theses),
            key=lambda row: (str(row.get("created_at") or ""), row["ref"]),
        )
        stances[company_ref] = {
            "thesis_ref": chosen["ref"],
            "stance": overrides.get(chosen.get("thesis_ref") or "", default)
            or DEFAULT_THESIS_STANCE,
        }
    return stances


def divergence_events(
    store: DaltonStore,
    prices: MarketPriceSeriesAuthority,
    mission: Any,
    policy: Any,
    events: ResearchEventAuthority,
    *,
    tracked: list[str],
    now: datetime,
) -> list[dict[str, Any]]:
    """Windows where the price ran against what our thesis implies.

    Suppressed per company for the length of the window: a divergence that
    persists for a fortnight would otherwise produce a fresh event every day,
    each with a different end date and therefore a different hash, and the
    idempotency rule would catch none of them.
    """

    thresholds = policy["abnormal_move"]
    window = int(thresholds.get("window_trading_days") or 10)
    universe = [member["company_ref"] for member in mission["universe"]]
    series = {ref: prices.series(ref) for ref in universe}
    dates = recent_settled_dates(series, limit=window)
    if len(dates) < 2:
        return []
    cutoff = (now - timedelta(days=window * 2)).isoformat(timespec="microseconds")
    suppress = {
        ref: (events.latest_occurred_at(ref, "price_divergence") or "") >= cutoff
        for ref in tracked
    }
    return detect_price_divergences(
        series_by_company=series, dates=dates, thresholds=thresholds,
        stances=thesis_stances(store, policy, tracked=tracked), suppress=suppress,
    )


def company_events(
    store: DaltonStore,
    mission: Any,
    *,
    company_ref: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """The three ledger scans for one company, newest first."""

    connection = store.connection
    candidates = document_event_candidates(
        connection, company_ref=company_ref, mission_ref=mission["mission_ref"],
        now=now, lookback_days=lookback_days,
    )
    candidates += claim_event_candidates(
        connection, company_ref=company_ref, now=now, lookback_days=lookback_days,
    )
    candidates += reconciliation_event_candidates(
        connection, company_ref=company_ref, now=now, lookback_days=lookback_days,
    )
    # W4: what the company did with its own shares. Not windowed by
    # ``lookback_days`` like the three above, and deliberately: a US issuer
    # discloses repurchases once a quarter in a 10-Q, so a seven-day window
    # would see the table on exactly the days the filing landed and never
    # again. The scan is bounded by the number of filings held instead, and
    # re-reading one costs a lookup because the ledger is idempotent on what
    # the event says.
    buybacks = buyback_event_candidates(connection, company_ref=company_ref)
    candidates += buybacks["events"]
    plans = trading_plan_event_candidates(connection, company_ref=company_ref)
    candidates += plans["events"]
    for candidate in candidates:
        candidate.setdefault("company_ref", company_ref)
    return candidates


def round_robin(
    by_company: Mapping[str, Sequence[Mapping[str, Any]]], *, order: Sequence[str]
) -> list[dict[str, Any]]:
    """One candidate from each company in turn, until they are all spent.

    The interleave is the whole of the fairness guarantee. Concatenating the
    per-company lists puts the first company's hundred candidates in front of
    the fifth company's two, and any cap at all then starves the tail.
    """

    queues = {ref: list(by_company.get(ref, ())) for ref in order}
    result: list[dict[str, Any]] = []
    index = 0
    while any(queues.values()):
        for ref in order:
            queue = queues[ref]
            if index < len(queue):
                result.append(dict(queue[index]))
        if not any(index < len(queue) for queue in queues.values()):
            break
        index += 1
    return result


def run_tracking(
    *,
    state_dir: Path,
    summary_dir: Path,
    policy_path: Path | None = None,
    company_ref: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_events: int = MAX_EVENTS_PER_RUN,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": moment.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "record",
        "tracking_status": None,
        "policy_ref": None,
        "tracked_companies": [],
        "events_recorded": 0,
        "events_duplicate": 0,
        "events_by_kind": {},
        "due": {},
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        policy = load_policy(policy_path)
        summary["policy_ref"] = policy["policy_ref"]
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "tracking_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        missing = missing_write_scopes(mission)
        if missing:
            summary.update({
                "status": "idle", "tracking_status": "ungranted",
                "failure_reason": "the mission does not grant " + ", ".join(missing),
            })
            return summary
        tracked = screen_passed_companies(missions, mission)
        if company_ref is not None:
            if company_ref not in tracked:
                summary.update({
                    "status": "idle", "tracking_status": "not_tracked",
                    "failure_reason": f"{company_ref} has no passed Initial Screen",
                })
                return summary
            tracked = [company_ref]
        summary["tracked_companies"] = list(tracked)
        if not tracked:
            summary.update({"status": "idle", "tracking_status": "nothing_tracked"})
            return summary

        events = ResearchEventAuthority(store)
        cadences = TrackingCadenceAuthority(store)
        prices = MarketPriceSeriesAuthority(store)
        actor = mission["autonomy"]["automation_principal"]

        by_company: dict[str, list[dict[str, Any]]] = {ref: [] for ref in tracked}
        for candidate in price_events(prices, mission, policy, tracked=tracked):
            by_company[candidate["company_ref"]].append(candidate)
        for candidate in divergence_events(
            store, prices, mission, policy, events, tracked=tracked, now=moment,
        ):
            by_company[candidate["company_ref"]].append(candidate)
        for ref in tracked:
            by_company[ref].extend(
                company_events(store, mission, company_ref=ref, now=moment,
                               lookback_days=lookback_days)
            )
        candidates = round_robin(by_company, order=tracked)

        recorded = duplicated = previewed = 0
        by_kind: dict[str, int] = {}
        for candidate in candidates:
            # The cap counts what was *written*, not what was looked at, and
            # the queue is interleaved by company. Both halves matter and the
            # bug they prevent is the same one: ACN alone produces a hundred
            # candidates a week, so a cap applied to candidates in universe
            # order spends the whole run re-recognising ACN's duplicates and
            # never reaches IBM -- while the lane reports a healthy run,
            # because a duplicate is a success.
            if recorded + previewed >= max_events:
                break
            if dry_run:
                by_kind[candidate["kind"]] = by_kind.get(candidate["kind"], 0) + 1
                previewed += 1
                continue
            result = record_event(
                events,
                company_ref=candidate["company_ref"],
                kind=candidate["kind"],
                occurred_at=candidate["occurred_at"],
                source_refs=candidate["source_refs"],
                payload=candidate["payload"],
                evidence_tier=candidate.get("evidence_tier"),
                mission=mission,
                actor_ref=actor,
            )
            if result["status"] == "fresh":
                recorded += 1
                by_kind[candidate["kind"]] = by_kind.get(candidate["kind"], 0) + 1
            else:
                duplicated += 1
        summary["events_recorded"] = recorded
        summary["events_duplicate"] = duplicated
        summary["events_by_kind"] = by_kind
        summary["formal_authority_writes"] = recorded

        # What the discovery lanes should consult. Reported rather than acted
        # on: this lane does not fetch anything, and a lane that both decided
        # a cadence and obeyed it would be marking its own homework.
        for ref in tracked:
            open_events = events.events(company_ref=ref, limit=20)
            summary["due"][ref] = {
                key: {"due": row["due"], "due_at": row["due_at"],
                      "interval_seconds": row["interval_seconds"], "origin": row["origin"]}
                for key, row in due_sources(
                    cadences, company_ref=ref, policy=policy, now=moment,
                    last_pulls={}, events=open_events,
                ).items()
            }
        summary["status"] = "succeeded"
        summary["tracking_status"] = "recorded" if recorded else "no_new_events"
        return summary
    except (ResearchEventError, TrackingCadenceError) as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--tracking-policy", type=Path,
                        help="the baselines and thresholds; defaults to the packaged file")
    parser.add_argument("--company-ref", help="track this company rather than all of them")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS_PER_RUN)
    parser.add_argument("--dry-run", action="store_true", help="count and stop; no writes")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_tracking(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        policy_path=args.tracking_policy,
        company_ref=args.company_ref,
        lookback_days=args.lookback_days,
        max_events=args.max_events,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "MAX_EVENTS_PER_RUN",
    "PRICE_LOOKBACK_DAYS",
    "build_parser",
    "company_events",
    "divergence_events",
    "main",
    "missing_write_scopes",
    "price_events",
    "round_robin",
    "run_tracking",
    "thesis_stances",
]
