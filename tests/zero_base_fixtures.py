"""W4: the smallest Core the zero-base slice can be tested against.

Two decisions worth stating.

**The price series are plain dictionaries, not published versions.**  Every
function in ``judgement_outcome`` that touches a price takes
``series_by_company`` as a mapping and reads ``bars`` off it; publishing them
through ``MarketPriceSeriesAuthority`` would exercise the price authority's
governance bindings and tell us nothing about the formula.  The shape here is
the shape ``MarketPriceSeriesAuthority.series()`` returns.

**The judgement rows are written through the store's own transaction.**  They
are inputs to this slice, not its subject: driving them through the judgement
lane would need a model, a verifier of a different family and a mission
version, and would test P14a.  The insert still goes through the real schema
and its triggers, so a renamed column fails here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from dalton_core.store import canonical_json, content_hash

#: Far enough back that no bar in a fixture is ever "provisional": a bar
#: captured after 21:00 UTC on its own day has settled.
CAPTURE_HOUR = "T23:00:00+00:00"

MISSION: dict[str, Any] = {
    "id": "coverage-mission-version:w4test",
    "content_hash": "0" * 64,
    "mission_ref": "coverage-mission:w4test",
    "universe": [
        {"company_ref": "company:ACN", "ticker": "ACN"},
        {"company_ref": "company:CTSH", "ticker": "CTSH"},
        {"company_ref": "company:INFY", "ticker": "INFY"},
        {"company_ref": "company:IBM", "ticker": "IBM"},
    ],
    "autonomy": {
        "automation_principal": "automation:coverage-mission",
        "may_write": ["deliverable", "thesis_revision_candidate"],
        "human_checkpoints": ["thesis_revision_candidate"],
    },
}

THRESHOLDS: dict[str, Any] = {
    "divergence_vs_basket_percent": "6.0",
    "window_trading_days": 5,
    "min_basket_members": 2,
}

STANCES: dict[str, dict[str, str]] = {
    ref["company_ref"]: {"thesis_ref": f"thesis:{ref['company_ref']}", "stance": "long"}
    for ref in MISSION["universe"]
}


def trading_days(start: str, count: int) -> list[str]:
    """``count`` consecutive calendar days from ``start``, oldest first.

    Calendar days rather than a market calendar on purpose: the formula never
    asks what day of the week it is, only which days everyone has a settled
    bar for, and inventing a holiday calendar here would test the calendar.
    """

    first = datetime.fromisoformat(start).date()
    return [(first + timedelta(days=index)).isoformat() for index in range(count)]


def series(company_ref: str, dates: Sequence[str], closes: Sequence[float]) -> dict[str, Any]:
    if len(dates) != len(closes):
        raise ValueError("one close per date")
    return {
        "company_ref": company_ref,
        "version_ref": f"market-price-series-version:{company_ref}",
        "bars": [
            {"date": day, "close": f"{close:.4f}", "captured_at": day + CAPTURE_HOUR}
            for day, close in zip(dates, closes)
        ],
        "observations": [],
    }


def flat_universe(
    dates: Sequence[str], *, subject: str, subject_closes: Sequence[float],
    peer_close: float = 100.0,
) -> dict[str, Any]:
    """One company that moves and three peers that do not."""

    built = {subject: series(subject, dates, subject_closes)}
    for member in MISSION["universe"]:
        ref = member["company_ref"]
        if ref == subject:
            continue
        built[ref] = series(ref, dates, [peer_close] * len(dates))
    return built


def add_judgement(
    store: Any, *, judgement_id: str, company_ref: str, decision: str, action: str,
    created_at: str, event_ref: str | None = None, effect: dict | None = None,
) -> dict[str, Any]:
    """One row in the real ``event_judgements`` table, through its triggers."""

    event_ref = event_ref or f"research-event:{judgement_id}"
    record = {
        "schema_version": "0.1", "id": judgement_id, "event_ref": event_ref,
        "company_ref": company_ref, "decision": decision, "action": action,
        "verdict": "pass", "created_at": created_at,
    }
    if effect is not None:
        record["effect"] = effect
    record["content_hash"] = content_hash(
        {key: value for key, value in record.items() if key != "content_hash"}
    )
    with store._transaction() as cur:
        cur.execute(
            "INSERT INTO event_judgements(judgement_id,event_ref,event_hash,company_ref,"
            "decision,action,verdict,cost_micros,mission_version_ref,record_json,"
            "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (judgement_id, event_ref, "e" * 64, company_ref, decision, action, "pass",
             0, MISSION["id"], canonical_json(record), record["content_hash"],
             "automation:coverage-mission", created_at),
        )
    return record


def add_reconciliation(
    store: Any, *, reconciliation_id: str, company_ref: str, forecast: str, actual: str,
    created_at: str, band: str = "notable", period_end: str = "2026-03-31",
) -> None:
    """One forecast-versus-actual row, through the real schema.

    ``forecast_reconciliations`` guards its insert on its own authorisation
    function rather than on ``dalton_authorized``, so the fixture opens that
    one for the length of the insert -- the same thing
    ``ForecastReconciliationAuthority`` does, without needing a forecast line
    and a Claim to hang it off.
    """

    record = {
        "id": reconciliation_id, "subject_ref": company_ref, "metric_ref": "metric:revenue",
        "period_end": period_end, "forecast_value": forecast, "actual_value": actual,
        "band": band, "created_at": created_at,
    }
    connection = store.connection
    connection.create_function("dalton_forecast_reconciliation_authorized", 0, lambda: 1)
    try:
        with store._transaction() as cur:
            cur.execute(
                "INSERT INTO forecast_reconciliations(reconciliation_id,subject_ref,"
                "metric_ref,period_start,period_end,forecast_line_ref,"
                "forecast_line_version_ref,forecast_line_version_hash,claim_version_ref,"
                "claim_version_hash,band,human_checkpoint,mission_version_ref,requested_by,"
                "actor_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reconciliation_id, company_ref, "metric:revenue", "2026-01-01", period_end,
                 f"forecast-line:{reconciliation_id}", f"forecast-line-version:{reconciliation_id}",
                 "f" * 64, f"claim-version:{reconciliation_id}", "c" * 64, band, None,
                 MISSION["id"], "automation:coverage-mission", "automation:coverage-mission",
                 canonical_json(record), content_hash(record), created_at),
            )
    finally:
        connection.create_function(
            "dalton_forecast_reconciliation_authorized", 0, lambda: 0
        )


def at(day: str, hour: int = 12) -> str:
    return datetime(
        int(day[0:4]), int(day[5:7]), int(day[8:10]), hour, tzinfo=timezone.utc
    ).isoformat(timespec="microseconds")


__all__ = [
    "CAPTURE_HOUR",
    "MISSION",
    "STANCES",
    "THRESHOLDS",
    "add_judgement",
    "add_reconciliation",
    "at",
    "flat_universe",
    "series",
    "trading_days",
]
