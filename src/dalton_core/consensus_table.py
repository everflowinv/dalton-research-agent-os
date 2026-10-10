"""Batch B: the consensus table a PM reads, rendered from the stored versions.

``consensus_table(state_dir, company_ref, as_of)`` answers, deterministically
and read-only, "what does the street expect, for which fiscal year, how
dispersed, how fresh, and from whom":

* current FY, FY+1, FY+2 -- revenue and EPS mean / high / low / analyst count,
  each row carrying its corrected fiscal label and how that label was
  established (``financial_period.relabel_rows``): the stored labels of a
  version written while the calendar was stale are never trusted as stored;
* the next two quarters, and any vendor row for a period that had *already
  been reported* by ``as_of`` (a pre-report consensus, shown as such rather
  than as an estimate);
* price target mean / median / high / low / count, recommendation counts;
* the broker-report range (route 1) beside the vendor's (route 2);
* source mix and staleness.

``consensus_delta(a, b)`` compares two such tables for "changed since" -- the
input to later redraft thresholds.  Neither function writes anything.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "consensus-table:batch-b:0.1"
#: A vendor observation older than this many days is flagged stale.
STALE_DAYS = 7
#: Default "material change" thresholds for ``consensus_delta`` (fractions).
DEFAULT_THRESHOLDS = {"eps": Decimal("0.01"), "revenue": Decimal("0.005"),
                      "target": Decimal("0.02")}
SLOTS = ("FY", "FY+1", "FY+2")


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, quantum: str | None = None) -> str | None:
    if value is None:
        return None
    if quantum:
        value = value.quantize(Decimal(quantum))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@contextmanager
def open_core(state_dir: Path | str) -> Iterator[sqlite3.Connection | None]:
    """``<state_dir>/core.sqlite`` (or the ``.sqlite`` file named) opened ``mode=ro``, or None."""

    path = Path(state_dir)
    if path.suffix != ".sqlite":
        path = path / "core.sqlite"
    if not path.exists():
        yield None
        return
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()


def _versions(connection: sqlite3.Connection, company_ref: str, as_of: str | None,
              source_kind: str, limit: int = 1) -> list[dict[str, Any]]:
    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "consensus_estimate_versions"):
        return []
    refs = _refs(company_ref)
    sql = (f"SELECT record_json FROM consensus_estimate_versions WHERE company_ref IN "
           f"({','.join('?' * len(refs))}) AND source_kind=? ")
    params: list[Any] = [*refs, source_kind]
    if as_of:
        sql += "AND as_of<=? "
        params.append(str(as_of)[:10])
    sql += "ORDER BY as_of DESC, version_number DESC LIMIT ?"
    params.append(limit)
    return [json.loads(row["record_json"]) for row in query(connection, sql, params)]


def _version_count(connection: sqlite3.Connection, company_ref: str) -> dict[str, int]:
    from .financial_period import _refs, _table_exists, query

    if not _table_exists(connection, "consensus_estimate_versions"):
        return {}
    refs = _refs(company_ref)
    return {row["source_kind"]: row["n"] for row in query(
        connection,
        f"SELECT source_kind, COUNT(*) AS n FROM consensus_estimate_versions WHERE company_ref IN "
        f"({','.join('?' * len(refs))}) GROUP BY source_kind", refs)}


def _cell(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "mean": row.get("avg"), "high": row.get("high"), "low": row.get("low"),
        "count": row.get("number_of_analysts"), "currency": row.get("currency"),
        "year_ago": row.get("year_ago"), "growth": row.get("growth"),
        "period_end": row.get("period_end"),
        "vendor_period": row.get("vendor_period"), "stored_label": row.get("original_label"),
        "label_basis": row.get("label_basis"), "year_ago_check": row.get("year_ago_check"),
        "anchor": row.get("anchor"),
    }


def _days(a: str | None, b: str | None) -> int | None:
    try:
        return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days
    except (TypeError, ValueError):
        return None


def _outlook_actuals(connection: sqlite3.Connection, company_ref: str, state_dir: Path | str | None,
                     as_of: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        from .earnings_release_outlook import actuals_for_calendar, latest_outlook

        outlook = latest_outlook(connection, company_ref, state_dir=state_dir)
    except Exception:  # noqa: BLE001 - the outlook only sharpens the label check
        return [], None
    if not outlook or not outlook.get("items"):
        return [], outlook
    if as_of and str(outlook.get("report_date") or "") > str(as_of)[:10]:
        return [], outlook
    return actuals_for_calendar(outlook["items"], f"sec:filing:{outlook.get('accession')}"), outlook


def street_anchors(street: Mapping[str, Any] | None) -> dict[tuple[str, str], str]:
    """``{(metric, period): median}`` of broker house estimates, for relabelling."""

    return {(item["metric"], item["period"]): item["median"]
            for item in (street or {}).get("periods") or () if item.get("median")}


def consensus_table_from(connection: sqlite3.Connection | None, company_ref: str,
                         as_of: str | None = None, *, state_dir: Path | str | None = None,
                         today: str | None = None,
                         street: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The table from an open (read-only) Core connection.

    ``street`` (a ``street_table``) anchors the vendor's labels by value; when
    omitted, the stored broker figures are read.
    """

    from .financial_period import relabel_consensus

    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "company_ref": company_ref,
                           "as_of": as_of, "status": "empty", "reason": None}
    if connection is None:
        out["reason"] = "这个研究环境还没有数据库"
        return out
    vendor = _versions(connection, company_ref, as_of, "vendor_observation")
    reports = _versions(connection, company_ref, as_of, "report_consensus")
    if not vendor and not reports:
        out["reason"] = "还没有卖方一致预期（既没有数据商观察，也没有两家以上券商的目标价区间）"
        return out
    out["status"] = "ok"
    record = vendor[0] if vendor else None
    extra, outlook = _outlook_actuals(connection, company_ref, state_dir,
                                      record.get("as_of") if record else as_of)
    if street is None and record is not None:
        from .street_estimate_tables import street_table_from

        street = street_table_from(connection, company_ref, as_of=record.get("as_of"))
    anchors = street_anchors(street)
    relabelled = relabel_consensus(connection, record, extra, anchors) if record else None
    calendar = (relabelled or {}).get("calendar") or {}
    out["calendar"] = {key: calendar.get(key) for key in (
        "fiscal_year_end", "fiscal_year_end_basis", "last_reported_period_end",
        "last_reported_bases", "last_filed_period_end", "advanced_by_release")}
    out["version"] = None if record is None else {
        "ref": record.get("id"), "number": record.get("version"), "as_of": record.get("as_of"),
        "created_at": record.get("created_at"), "source_ref": record.get("source_ref"),
        "observation_basis": record.get("observation_basis"),
        "stored_last_reported_period_end": (record.get("fiscal_calendar") or {}).get(
            "last_reported_period_end"),
    }
    eps_rows = [row for row in (relabelled or {}).get("eps_estimates") or () if not row.get("duplicate")]
    revenue_rows = [row for row in (relabelled or {}).get("revenue_estimates") or () if not row.get("duplicate")]
    last = str(calendar.get("last_reported_period_end") or "")

    def find(rows: Sequence[Mapping[str, Any]], label: str) -> Mapping[str, Any] | None:
        return next((row for row in rows if row.get("label") == label), None)

    periods = []
    current = None
    fye_month = calendar.get("fiscal_year_end_month")
    if last and fye_month:
        from .financial_period import fiscal_label, month_end

        end = date.fromisoformat(last)
        year_end = month_end(end.year, int(fye_month))
        if year_end <= end:
            year_end = month_end(end.year + 1, int(fye_month))
        current = fiscal_label(year_end, int(fye_month), "fiscal_year")["fiscal_year"]
    for index, slot in enumerate(SLOTS):
        if current is None:
            break
        label = f"FY{current + index}"
        eps, revenue = find(eps_rows, label), find(revenue_rows, label)
        periods.append({
            "slot": slot, "label": label,
            "eps": _cell(eps), "revenue": _cell(revenue),
            "status": "available" if (eps and eps.get("avg")) or (revenue and revenue.get("avg"))
            else "unavailable",
            "reason": None if (eps and eps.get("avg")) or (revenue and revenue.get("avg")) else
            "数据商这一年没有一致预期（它只给 0y/+1y 两档，且年度档可能尚未滚动）",
        })
    out["periods"] = periods
    # Basis: the vendor's EPS is its normalised figure (usually adjusted /
    # non-GAAP); company guidance is usually GAAP.  Said on the table so the
    # two are never set side by side unlabelled.
    out["eps_basis"] = {"vendor": "adjusted", "note": "数据商 EPS 为标准化口径（通常是调整后、非 GAAP），"
                                                       "与公司 GAAP 指引比较前需先对齐口径"}
    quarters = []
    for label in sorted({row["label"] for row in (*eps_rows, *revenue_rows)
                         if row.get("period_kind") == "quarter" and not row.get("reported")}):
        quarters.append({"label": label, "eps": _cell(find(eps_rows, label)),
                         "revenue": _cell(find(revenue_rows, label))})
    out["quarters"] = quarters
    out["ntm"] = {}
    if last and fye_month:
        from .financial_period import ntm_aligned

        for metric, rows in (("eps", eps_rows), ("revenue", revenue_rows)):
            live = [row for row in rows if not row.get("reported") and row.get("avg") is not None]
            out["ntm"][metric] = ntm_aligned(
                last, int(fye_month),
                quarterly={row["label"]: row["avg"] for row in live if row.get("period_kind") == "quarter"},
                annual={row["label"]: row["avg"] for row in live if row.get("period_kind") == "fiscal_year"})
    out["pre_report"] = [
        {"label": row["label"], "metric": metric, "mean": row.get("avg"),
         "count": row.get("number_of_analysts"),
         "note": "这一期在观察日之前已经公布：这是公布前的一致预期，不是对未来的预测"}
        for metric, rows in (("eps", eps_rows), ("revenue", revenue_rows))
        for row in rows if row.get("reported") and row.get("avg") is not None]
    target = (record or {}).get("price_target") or {}
    out["target"] = {key: target.get(key) for key in ("mean", "median", "high", "low", "current")}
    out["target"]["count"] = target.get("number_of_analysts")
    ratings = ((record or {}).get("recommendations") or [None])[0]
    if ratings:
        counts = {key: ratings.get(key) for key in ("strong_buy", "buy", "hold", "sell", "strong_sell")}
        total = sum(value or 0 for value in counts.values())
        out["ratings"] = {"month": ratings.get("month"), **counts, "total": total,
                          "buy_share": _plain(Decimal((counts["strong_buy"] or 0) + (counts["buy"] or 0))
                                              / Decimal(total), "0.01") if total else None}
    else:
        out["ratings"] = None
    report = reports[0].get("report_consensus") if reports else (record or {}).get("report_consensus")
    out["report_consensus"] = None if not report else {
        key: report.get(key) for key in ("as_of", "broker_count", "brokers", "mean", "high", "low",
                                         "metric", "window_days", "currency")}
    counts = _version_count(connection, company_ref)
    out["source_mix"] = {
        "vendor": None if record is None else {
            "source_ref": record.get("source_ref"), "basis": record.get("observation_basis"),
            "eps_analysts_max": max((row.get("number_of_analysts") or 0 for row in eps_rows), default=0),
            "target_analysts": target.get("number_of_analysts")},
        "broker_reports": None if not report else {
            "houses": report.get("broker_count"), "brokers": report.get("brokers")},
        "versions": counts,
    }
    reference = str(today or as_of or date.today().isoformat())[:10]
    vendor_age = _days(record.get("as_of"), reference) if record else None
    report_age = _days(report.get("as_of"), reference) if report else None
    out["staleness"] = {
        "reference_date": reference, "vendor_as_of": None if record is None else record.get("as_of"),
        "vendor_days_old": vendor_age, "report_as_of": None if not report else report.get("as_of"),
        "report_days_old": report_age,
        "stale": bool(vendor_age is None or vendor_age > STALE_DAYS),
    }
    out["relabel"] = None if relabelled is None else {
        "changed_labels": relabelled.get("changed_labels"),
        "rule": relabelled.get("relabel_version"),
        "used_release_actuals": bool(extra),
        "used_street_anchors": bool(anchors),
    }
    out["outlook_accession"] = None if not outlook else outlook.get("accession")
    return out


def consensus_table(state_dir: Path | str, company_ref: str, as_of: str | None = None) -> dict[str, Any]:
    """The consensus table for one company, read-only from ``<state_dir>/core.sqlite``."""

    with open_core(state_dir) as connection:
        return consensus_table_from(connection, company_ref, as_of, state_dir=state_dir)


def _change(old: Any, new: Any) -> dict[str, Any] | None:
    a, b = _dec(old), _dec(new)
    if a is None and b is None:
        return None
    if a is None or b is None:
        return {"from": old, "to": new, "abs": None, "pct": None, "kind": "appeared" if a is None else "vanished"}
    if a == b:
        return None
    pct = None if a == 0 else (b - a) / abs(a)
    return {"from": old, "to": new, "abs": _plain(b - a), "pct": _plain(pct, "0.0001") if pct is not None else None,
            "kind": "moved"}


def consensus_delta(a: Mapping[str, Any], b: Mapping[str, Any],
                    thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What moved between two consensus tables (``a`` older, ``b`` newer).

    Periods are matched by fiscal label (never by slot: FY+1 in March and
    FY+1 in October may be different years).  ``material`` is true when any
    mean moved by at least its threshold (fractions; defaults
    ``DEFAULT_THRESHOLDS``) or a period appeared/vanished.
    """

    limits = {**DEFAULT_THRESHOLDS, **{k: Decimal(str(v)) for k, v in (thresholds or {}).items()}}
    changes: list[dict[str, Any]] = []

    def rows(table: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
        out: dict[tuple[str, str], Mapping[str, Any]] = {}
        for item in (*(table.get("periods") or ()), *(table.get("quarters") or ())):
            for metric in ("eps", "revenue"):
                cell = item.get(metric)
                if cell and cell.get("mean") is not None:
                    out[(item["label"], metric)] = cell
        return out

    old, new = rows(a), rows(b)
    material = False
    for key in sorted(set(old) | set(new)):
        label, metric = key
        for field in ("mean", "high", "low", "count"):
            moved = _change((old.get(key) or {}).get(field), (new.get(key) or {}).get(field))
            if moved is None:
                continue
            changes.append({"period": label, "metric": metric, "field": field, **moved})
            if field == "mean":
                if moved["kind"] != "moved" or abs(Decimal(moved["pct"] or "0")) >= limits[metric]:
                    material = True
    for field in ("mean", "high", "low", "count"):
        moved = _change(((a.get("target") or {}).get(field)), ((b.get("target") or {}).get(field)))
        if moved:
            changes.append({"period": None, "metric": "target", "field": field, **moved})
            if field == "mean" and moved["kind"] == "moved" and abs(Decimal(moved["pct"] or "0")) >= limits["target"]:
                material = True
    ra, rb = a.get("ratings") or {}, b.get("ratings") or {}
    for field in ("strong_buy", "buy", "hold", "sell", "strong_sell"):
        moved = _change(ra.get(field), rb.get(field))
        if moved:
            changes.append({"period": None, "metric": "ratings", "field": field, **moved})
    pcts = [abs(Decimal(item["pct"])) for item in changes
            if item.get("pct") is not None and item["field"] == "mean"]
    return {
        "from_as_of": (a.get("version") or {}).get("as_of") or a.get("as_of"),
        "to_as_of": (b.get("version") or {}).get("as_of") or b.get("as_of"),
        "changes": changes, "change_count": len(changes),
        "max_abs_mean_pct": _plain(max(pcts), "0.0001") if pcts else None,
        "material": material,
        "thresholds": {key: _plain(value) for key, value in limits.items()},
    }


__all__ = ["DEFAULT_THRESHOLDS", "SCHEMA_VERSION", "consensus_delta", "consensus_table",
           "consensus_table_from", "open_core"]
