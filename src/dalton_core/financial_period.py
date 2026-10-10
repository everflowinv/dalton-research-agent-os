"""Batch B: the period calendar every number in the numbers pack is filed against.

The bug this module exists for (diagnosis 2026-10-09): Accenture reported its
FY2026 fourth quarter in an 8-K on 2026-10-01, but the consensus lane read its
"last reported period" from 10-Q/10-K filings alone, so on 2026-10-09 it still
believed 2026-05-31 was the newest reported quarter.  Yahoo's ``0q`` was
therefore labelled FY2026Q4 -- and that row's ``year_ago`` revenue,
18,742,125,000, is exactly the FY2026 *Q1* revenue the 10-Q for 2025-11-30
filed.  The row was FY2027Q1, mislabelled.

Two fixes, both deterministic:

1. **The calendar advances on every evidence of a reported period**, not only
   on periodic filings: 10-Q/10-K filings, ADR-0015 earnings-release reads
   (the 8-K's Exhibit 99.1, preliminary actuals), and the XBRL statement
   lines' own newest period end.  Each piece of evidence carries the day it
   became public, so a historical consensus version is relabelled with only
   what was known on its ``as_of`` -- never with hindsight.
2. **The vendor's labels are checked against the actuals we hold.**  Each
   vendor row carries ``year_ago``; if that equals the actual of the period
   one year before the label, the label is confirmed; if it equals the actual
   one period earlier, the vendor block has not rolled yet (Yahoo rolls EPS
   and revenue, quarters and years, independently) and the block is shifted.
   Votes are counted per (metric, granularity); a tie borrows the other
   metric's decision; no evidence leaves the calendar label, marked
   ``calendar_unverified``.

Nothing here rewrites a stored consensus version.  A re-derivation is a new,
append-only ``consensus_period_relabels`` record bound to the version it
relabels and to the calendar evidence used (``ConsensusPeriodRelabelStore``).
"""

from __future__ import annotations

import calendar as _calendar
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
RELABEL_VERSION = "consensus-period-relabel:batch-b:0.1"
_SCHEMA_PATH = Path(__file__).with_name("financial_period_schema.sql")

REVENUE_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
)
EPS_CONCEPTS = ("us-gaap:EarningsPerShareDiluted",)
#: Relative tolerance for a revenue year-ago match (press releases round to
#: tens of millions; a vendor copies the filed figure).
REVENUE_TOLERANCE = Decimal("0.003")
#: Absolute tolerance for a per-share year-ago match: to the cent.
EPS_TOLERANCE = Decimal("0.006")

LABEL_ANCHORED = "street_median_proximity"
LABEL_CONFIRMED = "year_ago_confirmed"
LABEL_SHIFTED = "year_ago_shifted"
LABEL_BORROWED = "shift_borrowed_from_other_metric"
LABEL_UNVERIFIED = "calendar_unverified"
LABEL_BASES = (LABEL_ANCHORED, LABEL_CONFIRMED, LABEL_SHIFTED, LABEL_BORROWED, LABEL_UNVERIFIED)
#: Street-median proximity decides a label only when it is decisive: the
#: nearest candidate within this gap, and the runner-up at least this many
#: times further away.
ANCHOR_MAX_GAP = Decimal("0.05")
ANCHOR_MIN_RATIO = Decimal("2")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def query(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Rows as dicts whatever the connection's row factory is."""

    cursor = connection.execute(sql, tuple(params))
    names = [item[0] for item in cursor.description or ()]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _refs(company_ref: str) -> list[str]:
    try:
        from .mission_company_cik import company_ref_variants

        return company_ref_variants(company_ref)
    except Exception:  # noqa: BLE001 - a ref this helper cannot parse is itself
        return [company_ref]


def _in(refs: Sequence[str]) -> str:
    return ",".join("?" * len(refs))


def month_end(year: int, month: int) -> date:
    return date(year, month, _calendar.monthrange(year, month)[1])


def shift_months(moment: date, months: int) -> date:
    """The month end ``months`` away (quarter ends here are month ends)."""

    index = moment.year * 12 + moment.month - 1 + months
    return month_end(index // 12, index % 12 + 1)


def fiscal_label(period_end: date, fye_month: int, kind: str) -> dict[str, Any]:
    """``FY2027Q1`` / ``FY2027`` for a period ending on ``period_end``.

    FY2027 is the fiscal year that *ends* in calendar 2027 (Accenture's and
    every filer's own convention, and ``consensus_estimate``'s label basis).
    """

    year = period_end.year + (1 if period_end.month > fye_month else 0)
    if kind == "fiscal_year":
        return {"period_kind": "fiscal_year", "fiscal_year": year, "fiscal_quarter": None,
                "period_end": period_end.isoformat(), "label": f"FY{year}"}
    quarter = ((period_end.month - fye_month - 1) % 12) // 3 + 1
    return {"period_kind": "quarter", "fiscal_year": year, "fiscal_quarter": quarter,
            "period_end": period_end.isoformat(), "label": f"FY{year}Q{quarter}"}


# ---------------------------------------------------------------------------
# the fiscal year end and the reported-period evidence
# ---------------------------------------------------------------------------


def _filings(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    if not _table_exists(connection, "coverage_mission_statement_filings"):
        return []
    refs = _refs(company_ref)
    return query(
        connection,
        f"SELECT ingest_id, accession, form, filed, report_date FROM coverage_mission_statement_filings "
        f"WHERE company_ref IN ({_in(refs)}) ORDER BY report_date", refs)


def fiscal_year_end(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    """``{"fiscal_year_end": "08-31", "month": 8, "basis": ...}`` or None.

    The newest 10-K's period end first; failing that the quarter a filer never
    files a 10-Q for (``consensus_estimate.fiscal_year_end_from_quarters``) --
    the same two routes the consensus lane uses, in the same order.
    """

    filings = _filings(connection, company_ref)
    annual = [row["report_date"] for row in filings if str(row["form"]).startswith("10-K")]
    if annual:
        end = _date(max(annual))
        if end is not None:
            return {"fiscal_year_end": f"{end.month:02d}-{end.day:02d}", "month": end.month,
                    "basis": "annual_report"}
    from .consensus_estimate import fiscal_year_end_from_quarters

    stated = fiscal_year_end_from_quarters(
        [row["report_date"] for row in filings if str(row["form"]).startswith("10-Q")])
    if stated:
        return {"fiscal_year_end": stated, "month": int(stated[:2]), "basis": "quarterly_grid"}
    return None


def reported_period_evidence(connection: sqlite3.Connection, company_ref: str,
                             fye_month: int | None = None) -> list[dict[str, Any]]:
    """Every piece of evidence that a period has been reported, oldest first.

    Each item: ``{period_end, public_on, basis, ref}``.  ``public_on`` is the day
    the world (and this system) could know it -- a filing's filed date, a
    release's report date -- so a reader can ask "as of then".
    """

    out: list[dict[str, Any]] = []
    for row in _filings(connection, company_ref):
        out.append({"period_end": str(row["report_date"]), "public_on": str(row["filed"])[:10],
                    "basis": f"sec_{row['form'].lower()}", "ref": f"sec:filing:{row['accession']}"})
    # XBRL: the statement lines' own newest income-statement period, which for
    # a 10-K can be later than its cover date only by mistake -- read anyway,
    # because the filed lines are what the numbers are.
    if _table_exists(connection, "coverage_mission_statement_lines"):
        refs = _refs(company_ref)
        for row in query(
            connection,
            "SELECT f.accession AS accession, f.filed AS filed, MAX(l.period_end) AS end "
            "FROM coverage_mission_statement_lines l JOIN coverage_mission_statement_filings f "
            f"ON f.ingest_id=l.ingest_id WHERE f.company_ref IN ({_in(refs)}) AND l.statement='income' "
            "AND l.period_start IS NOT NULL GROUP BY f.ingest_id", refs):
            if row["end"]:
                out.append({"period_end": str(row["end"]), "public_on": str(row["filed"])[:10],
                            "basis": "xbrl_statement_lines", "ref": f"sec:filing:{row['accession']}"})
    # ADR-0015: the 8-K earnings release.  Read through the intake's own public
    # records (``earnings_release_reads``); the intake module is not imported.
    if _table_exists(connection, "earnings_release_reads"):
        refs = _refs(company_ref)
        for row in query(
            connection,
            f"SELECT accession, status, period, record_json, created_at FROM earnings_release_reads "
            f"WHERE company_ref IN ({_in(refs)})", refs):
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError):
                record = {}
            end = _date(record.get("period_end"))
            if end is None:
                end = _period_end_from_label(str(row["period"] or ""), fye_month)
            if end is None:
                continue
            public = str(record.get("report_date") or row["created_at"])[:10]
            out.append({"period_end": end.isoformat(), "public_on": public,
                        "basis": "earnings_release_8k", "ref": f"sec:filing:{row['accession']}",
                        "read_status": row["status"]})
    out.sort(key=lambda item: (item["period_end"], item["public_on"], item["basis"]))
    return out


def _period_end_from_label(label: str, fye_month: int | None) -> date | None:
    """``FY2026-Q4`` -> 2026-08-31 for an August year end."""

    if fye_month is None or not label.startswith("FY"):
        return None
    try:
        year_part, _, quarter_part = label[2:].partition("-Q")
        year, quarter = int(year_part), int(quarter_part)
    except ValueError:
        return None
    if not 1 <= quarter <= 4:
        return None
    return shift_months(month_end(year, fye_month), -3 * (4 - quarter))


def last_reported_period(connection: sqlite3.Connection, company_ref: str,
                         as_of: str | None = None,
                         evidence: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    """The newest period this company had reported by ``as_of`` (inclusive)."""

    items = list(evidence) if evidence is not None else reported_period_evidence(connection, company_ref)
    if as_of:
        items = [item for item in items if str(item["public_on"]) <= str(as_of)[:10]]
    if not items:
        return None
    newest = max(items, key=lambda item: (item["period_end"], item["public_on"]))
    same = [item for item in items if item["period_end"] == newest["period_end"]]
    return {"period_end": newest["period_end"],
            "bases": sorted({item["basis"] for item in same}),
            "refs": sorted({item["ref"] for item in same}),
            "first_public_on": min(item["public_on"] for item in same)}


def company_calendar(connection: sqlite3.Connection, company_ref: str,
                     as_of: str | None = None) -> dict[str, Any] | None:
    """The corrected fiscal calendar: year end, last reported period, evidence."""

    fye = fiscal_year_end(connection, company_ref)
    if fye is None:
        return None
    evidence = reported_period_evidence(connection, company_ref, fye["month"])
    last = last_reported_period(connection, company_ref, as_of, evidence)
    if last is None:
        return None
    filed_only = [item for item in evidence if item["basis"].startswith("sec_10")
                  and (not as_of or item["public_on"] <= str(as_of)[:10])]
    filed_last = max((item["period_end"] for item in filed_only), default=None)
    return {
        "fiscal_year_end": fye["fiscal_year_end"], "fiscal_year_end_month": fye["month"],
        "fiscal_year_end_basis": fye["basis"],
        "last_reported_period_end": last["period_end"],
        "last_reported_bases": last["bases"], "last_reported_refs": last["refs"],
        "last_filed_period_end": filed_last,
        # True when the 8-K (or XBRL) moved the calendar past the periodic
        # filings -- exactly the ACN 2026-10-01..10-09 window.
        "advanced_by_release": bool(filed_last and last["period_end"] > filed_last),
        "as_of": as_of,
    }


# ---------------------------------------------------------------------------
# the actuals a year-ago figure can be checked against
# ---------------------------------------------------------------------------


def _kind_of(start: date | None, end: date | None) -> str | None:
    if start is None or end is None:
        return None
    days = (end - start).days
    if 80 <= days <= 100:
        return "quarter"
    if 350 <= days <= 380:
        return "fiscal_year"
    return None


def held_actuals(connection: sqlite3.Connection, company_ref: str,
                 extra: Sequence[Mapping[str, Any]] = ()) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """``{(metric, kind, period_end): [{value, basis, ref}]}`` for revenue and EPS.

    Revenue in currency units; EPS per share.  Several values may sit under one
    key (GAAP and adjusted EPS, a filed and a preliminary revenue) -- a vendor's
    year-ago figure matches if it equals any of them.  ``extra`` admits rows
    from callers (the outlook parser's reported headline figures).
    """

    out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def add(metric: str, kind: str | None, end: Any, value: Any, basis: str, ref: str) -> None:
        amount = _dec(value)
        if kind is None or amount is None or not end:
            return
        bucket = out.setdefault((metric, kind, str(end)[:10]), [])
        if not any(item["value"] == amount for item in bucket):
            bucket.append({"value": amount, "basis": basis, "ref": ref})

    if _table_exists(connection, "coverage_mission_statement_lines"):
        refs = _refs(company_ref)
        concepts = REVENUE_CONCEPTS + EPS_CONCEPTS
        for row in query(
            connection,
            "SELECT l.concept AS concept, l.period_start AS start, l.period_end AS end, l.value AS value, "
            "f.accession AS accession FROM coverage_mission_statement_lines l JOIN "
            "coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
            f"WHERE f.company_ref IN ({_in(refs)}) AND l.statement='income' AND l.dimension_axis IS NULL "
            f"AND l.concept IN ({_in(concepts)})", (*refs, *concepts)):
            metric = "revenue" if row["concept"] in REVENUE_CONCEPTS else "eps"
            add(metric, _kind_of(_date(row["start"]), _date(row["end"])), row["end"], row["value"],
                "xbrl_filed", f"sec:filing:{row['accession']}")
    if _table_exists(connection, "preliminary_actuals"):
        refs = _refs(company_ref)
        scale = {"one": Decimal(1), "thousand": Decimal(1000), "million": Decimal(1_000_000),
                 "billion": Decimal(1_000_000_000)}
        for row in query(
            connection,
            f"SELECT metric, value, scale, accession, record_json FROM preliminary_actuals "
            f"WHERE company_ref IN ({_in(refs)}) AND metric IN ('revenue','eps_diluted')", refs):
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError):
                record = {}
            value = _dec(row["value"])
            if value is None:
                continue
            if row["metric"] == "revenue":
                value = value * scale.get(str(row["scale"]), Decimal(1))
            kind = _kind_of(_date(record.get("period_start")), _date(record.get("period_end"))) or "quarter"
            add("revenue" if row["metric"] == "revenue" else "eps", kind, record.get("period_end"),
                value, "press_release_preliminary", f"sec:filing:{row['accession']}")
    for item in extra:
        add(str(item.get("metric")), item.get("kind"), item.get("period_end"), item.get("value"),
            str(item.get("basis") or "caller"), str(item.get("ref") or ""))
    return out


def _matches(metric: str, figure: Decimal, held: Sequence[Mapping[str, Any]]) -> bool:
    for item in held:
        actual = item["value"]
        if metric == "eps":
            if abs(actual - figure) <= EPS_TOLERANCE:
                return True
        elif actual and abs(actual - figure) / abs(actual) <= REVENUE_TOLERANCE:
            return True
    return False


# ---------------------------------------------------------------------------
# relabelling one vendor version
# ---------------------------------------------------------------------------


def _base_label(vendor_period: str, cal: Mapping[str, Any]) -> dict[str, Any]:
    from .consensus_estimate import fiscal_calendar, map_estimate_period

    mapped = fiscal_calendar(fiscal_year_end=cal["fiscal_year_end"],
                             last_reported_period_end=cal["last_reported_period_end"])
    return map_estimate_period(vendor_period, mapped)


def _shifted(row: Mapping[str, Any], steps: int, fye_month: int) -> dict[str, Any]:
    end = _date(row["period_end"])
    months = 3 if row["period_kind"] == "quarter" else 12
    return fiscal_label(shift_months(end, steps * months), fye_month, row["period_kind"])


def _vote(row: Mapping[str, Any], metric: str, fye_month: int,
          actuals: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]]) -> list[int]:
    """Which shifts of this row's label its year-ago figure agrees with."""

    figure = _dec(row.get("year_ago"))
    if figure is None:
        return []
    votes = []
    for steps in (0, -1, 1):
        candidate = _shifted(row, steps, fye_month)
        prior_end = shift_months(_date(candidate["period_end"]), -12).isoformat()
        held = actuals.get((metric, candidate["period_kind"], prior_end))
        if held and _matches(metric, figure, held):
            votes.append(steps)
    return votes


def _decide(votes: Sequence[int]) -> int | None:
    if not votes:
        return None
    counts = {step: votes.count(step) for step in set(votes)}
    best = max(counts.values())
    winners = [step for step, count in counts.items() if count == best]
    return winners[0] if len(winners) == 1 else None


def _anchored(row: Mapping[str, Any], metric: str, fye_month: int,
              anchors: Mapping[tuple[str, str], Any] | None) -> tuple[int, dict[str, Any]] | None:
    """The shift whose label the brokers' median for that period is decisively nearest.

    ``anchors``: ``{(metric, label): median}`` from broker estimate tables.  A
    vendor block that has not rolled (or has rolled half-way: Yahoo on
    2026-10-03 held a cached FY2026 ``0y`` beside an FY2028 ``+1y``) is placed
    by its *values*, where its year-ago figure would mislead.
    """

    mean = _dec(row.get("avg"))
    if not anchors or mean is None:
        return None
    gaps: dict[int, Decimal] = {}
    for steps in (0, -1, 1):
        label = _shifted(row, steps, fye_month)["label"]
        median = _dec(anchors.get((metric, label)))
        if median:
            gaps[steps] = abs(mean / median - 1)
    if not gaps:
        return None
    ordered = sorted(gaps.items(), key=lambda item: item[1])
    best, gap = ordered[0]
    # One anchored candidate decides nothing: adjacent quarters differ by a
    # few percent, so "near the only median we hold" is not evidence.
    if len(ordered) < 2 or gap > ANCHOR_MAX_GAP or ordered[1][1] < ANCHOR_MIN_RATIO * gap:
        return None
    return best, {"gap": format(gap.quantize(Decimal("0.0001")), "f"),
                  "candidates": {_shifted(row, k, fye_month)["label"]: format(v.quantize(Decimal("0.0001")), "f")
                                 for k, v in ordered}}


def relabel_rows(record: Mapping[str, Any], cal: Mapping[str, Any],
                 actuals: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
                 anchors: Mapping[tuple[str, str], Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """``{"eps": [...], "revenue": [...]}`` relabelled rows for one vendor version.

    Order of evidence per row: (1) broker-median proximity when decisive
    (``anchors``); (2) the year-ago votes of the row's (metric, granularity)
    block; (3) the other metric's block; (4) the corrected calendar.  Two rows
    landing on one label keep the first by evidence order; the other is
    marked ``duplicate`` and readers skip it.

    Every output row keeps the vendor's figures untouched and adds: the
    corrected ``label``/``fiscal_year``/``fiscal_quarter``/``period_end``,
    ``original_label`` (what the stored version says), ``label_basis``,
    ``year_ago_check`` and ``reported`` (the period was already reported by
    ``as_of`` -- the vendor's number is then a pre-report consensus).
    """

    fye_month = int(cal["fiscal_year_end_month"])
    last = str(cal["last_reported_period_end"])
    blocks = {"eps": list(record.get("eps_estimates") or ()),
              "revenue": list(record.get("revenue_estimates") or ())}
    based: dict[str, list[dict[str, Any]]] = {}
    for metric, rows in blocks.items():
        based[metric] = []
        for row in rows:
            key = row.get("vendor_period")
            if key not in ("0q", "+1q", "0y", "+1y"):
                continue
            based[metric].append({**dict(row), "original_label": row.get("label"),
                                  **_base_label(key, cal)})
    decision: dict[tuple[str, str], tuple[int | None, list[int]]] = {}
    for metric, rows in based.items():
        for kind in ("quarter", "fiscal_year"):
            votes: list[int] = []
            for row in rows:
                if row["period_kind"] == kind:
                    votes.extend(_vote(row, metric, fye_month, actuals))
            decision[(metric, kind)] = (_decide(votes), votes)
    out: dict[str, list[dict[str, Any]]] = {}
    for metric, rows in based.items():
        other = "revenue" if metric == "eps" else "eps"
        out[metric] = []
        for row in rows:
            kind = row["period_kind"]
            steps, votes = decision[(metric, kind)]
            basis = LABEL_CONFIRMED if steps == 0 else LABEL_SHIFTED
            anchor = _anchored(row, metric, fye_month, anchors)
            if anchor is not None:
                steps, basis = anchor[0], LABEL_ANCHORED
            elif steps is None:
                borrowed = decision[(other, kind)][0]
                if borrowed is not None:
                    steps, basis = borrowed, LABEL_BORROWED
                else:
                    steps, basis = 0, LABEL_UNVERIFIED
            label = _shifted(row, steps, fye_month)
            own = _vote(row, metric, fye_month, actuals)
            if _dec(row.get("year_ago")) is None:
                check = "no_year_ago"
            elif not own:
                prior_end = shift_months(_date(label["period_end"]), -12).isoformat()
                check = ("mismatch" if actuals.get((metric, kind, prior_end)) else "no_actual_held")
            else:
                check = "match" if steps in own else "mismatch"
            out[metric].append({
                **row, **label, "label_basis": basis, "label_shift": steps,
                "votes": votes, "year_ago_check": check,
                "anchor": None if anchor is None else anchor[1],
                "reported": label["period_end"] <= last,
            })
        rank = {basis: index for index, basis in enumerate(LABEL_BASES)}
        taken: set[str] = set()
        for item in sorted(out[metric], key=lambda item: rank[item["label_basis"]]):
            item["duplicate"] = item["label"] in taken
            taken.add(item["label"])
        out[metric].sort(key=lambda item: (item["period_end"], item["period_kind"]))
    return out


def relabel_consensus(connection: sqlite3.Connection, record: Mapping[str, Any],
                      extra_actuals: Sequence[Mapping[str, Any]] = (),
                      anchors: Mapping[tuple[str, str], Any] | None = None) -> dict[str, Any] | None:
    """The full re-derivation for one stored vendor version, or None.

    Uses only evidence public by the version's ``as_of``.  Report-consensus
    versions carry no period rows and are returned with empty blocks.
    """

    company_ref = str(record.get("company_ref"))
    as_of = str(record.get("as_of") or "")[:10] or None
    cal = company_calendar(connection, company_ref, as_of)
    stored = record.get("fiscal_calendar") or {}
    if cal is None:
        if not stored.get("fiscal_year_end") or not stored.get("last_reported_period_end"):
            return None
        month = int(str(stored["fiscal_year_end"])[:2])
        cal = {"fiscal_year_end": stored["fiscal_year_end"], "fiscal_year_end_month": month,
               "fiscal_year_end_basis": "stored_version",
               "last_reported_period_end": stored["last_reported_period_end"],
               "last_reported_bases": ["stored_version"], "last_reported_refs": [],
               "last_filed_period_end": None, "advanced_by_release": False, "as_of": as_of}
    actuals = held_actuals(connection, company_ref, extra_actuals)
    rows = relabel_rows(record, cal, actuals, anchors)
    changed = sorted({f"{item['original_label']}->{item['label']}"
                      for block in rows.values() for item in block
                      if item["original_label"] != item["label"]})
    return {
        "schema_version": SCHEMA_VERSION, "relabel_version": RELABEL_VERSION,
        "company_ref": company_ref, "consensus_version_ref": record.get("id"),
        "consensus_version_hash": record.get("content_hash"), "as_of": as_of,
        "stored_calendar": {key: stored.get(key) for key in
                            ("fiscal_year_end", "last_reported_period_end", "mapping_version")},
        "calendar": cal, "eps_estimates": rows["eps"], "revenue_estimates": rows["revenue"],
        "changed_labels": changed,
        "anchors_used": bool(anchors),
    }


# ---------------------------------------------------------------------------
# NTM aligned to fiscal periods
# ---------------------------------------------------------------------------


def ntm_aligned(last_reported_period_end: str, fye_month: int, *,
                quarterly: Mapping[str, Any], annual: Mapping[str, Any]) -> dict[str, Any]:
    """Next-twelve-months value as the next four *fiscal quarters* after the
    last reported one.

    Per fiscal year in the window: a year wholly inside it uses the annual
    figure (or its four quarters); a year partly inside uses the quarterly
    figures held and, for its other window quarters, a quarter-share of the
    annual figure (labelled ``pro_rata``).  Missing inputs make it unavailable
    rather than approximated further.
    """

    end = _date(last_reported_period_end)
    if end is None:
        return {"status": "unavailable", "reason": "不知道最近已报告的财期"}
    window = [fiscal_label(shift_months(end, 3 * step), fye_month, "quarter") for step in range(1, 5)]
    groups: dict[int, list[dict[str, Any]]] = {}
    for quarter in window:
        groups.setdefault(quarter["fiscal_year"], []).append(quarter)
    total = Decimal(0)
    components = []
    for year, quarters in sorted(groups.items()):
        label = f"FY{year}"
        year_value = _dec(annual.get(label))
        if len(quarters) == 4 and year_value is not None:
            total += year_value
            components.append({"part": label, "value": format(year_value, "f"), "source": "annual"})
            continue
        for quarter in quarters:
            value = _dec(quarterly.get(quarter["label"]))
            if value is not None:
                total += value
                components.append({"part": quarter["label"], "value": format(value, "f"), "source": "quarterly"})
            elif year_value is not None:
                share = year_value / 4
                total += share
                components.append({"part": quarter["label"], "value": format(share, "f"),
                                   "source": f"pro_rata_of_{label}"})
            else:
                return {"status": "unavailable", "window": [q["label"] for q in window],
                        "reason": f"{quarter['label']} 既没有季度值也没有 {label} 全年值"}
    return {"status": "available", "value": format(total, "f"), "window": [q["label"] for q in window],
            "components": components, "method": "next_four_fiscal_quarters"}


# ---------------------------------------------------------------------------
# the append-only re-derivation ledger
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class ConsensusPeriodRelabelStore:
    """``consensus_period_relabels``: one row per (version, relabel version, calendar).

    Old consensus rows are never touched.  Re-running with the same evidence is
    a no-op (the binding hash is unique); new evidence (a 10-K landing) appends
    a new relabel record for the same version.
    """

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ConsensusPeriodRelabelStore requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def rederive(self, company_ref: str, *, extra_actuals: Sequence[Mapping[str, Any]] = (),
                 anchors_for: Any = None, now: str | None = None) -> dict[str, Any]:
        """``anchors_for(as_of) -> {(metric, label): median}`` supplies broker
        medians known by each version's date (optional)."""

        if not _table_exists(self.connection, "consensus_estimate_versions"):
            return {"status": "no_consensus", "appended": 0, "unchanged": 0}
        refs = _refs(company_ref)
        rows = query(
            self.connection,
            f"SELECT record_json FROM consensus_estimate_versions WHERE company_ref IN ({_in(refs)}) "
            "AND source_kind='vendor_observation' ORDER BY version_number", refs)
        appended = unchanged = 0
        for row in rows:
            record = json.loads(row["record_json"])
            anchors = anchors_for(record.get("as_of")) if anchors_for is not None else None
            derived = relabel_consensus(self.connection, record, extra_actuals, anchors)
            if derived is None:
                continue
            body = _jsonable(derived)
            binding = content_hash({"version": body["consensus_version_ref"],
                                    "relabel_version": RELABEL_VERSION,
                                    "calendar": body["calendar"],
                                    "eps": body["eps_estimates"], "revenue": body["revenue_estimates"],
                                    "anchors": body.get("anchors_used")})
            exists = self.connection.execute(
                "SELECT 1 FROM consensus_period_relabels WHERE binding_hash=?", (binding,)).fetchone()
            if exists:
                unchanged += 1
                continue
            body["binding_hash"] = binding
            body["created_at"] = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
            body["id"] = f"consensus-period-relabel:{binding[:32]}"
            body["content_hash"] = content_hash(body)
            with self.store._transaction() as cur:
                cur.execute(
                    "INSERT INTO consensus_period_relabels (relabel_id, company_ref, consensus_version_ref, "
                    "relabel_version, binding_hash, last_reported_period_end, changed_count, record_json, "
                    "content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (body["id"], company_ref, body["consensus_version_ref"], RELABEL_VERSION, binding,
                     body["calendar"]["last_reported_period_end"], len(body["changed_labels"]),
                     canonical_json(body), body["content_hash"], body["created_at"]))
            appended += 1
        return {"status": "ok", "appended": appended, "unchanged": unchanged}


def stored_relabel(connection: sqlite3.Connection, version_ref: str) -> dict[str, Any] | None:
    """The newest stored re-derivation of one consensus version, if any."""

    if not _table_exists(connection, "consensus_period_relabels"):
        return None
    rows = query(
        connection,
        "SELECT record_json FROM consensus_period_relabels WHERE consensus_version_ref=? "
        "ORDER BY created_at DESC LIMIT 1", (version_ref,))
    return json.loads(rows[0]["record_json"]) if rows else None


__all__ = [
    "ntm_aligned",
    "ConsensusPeriodRelabelStore", "LABEL_BASES", "RELABEL_VERSION", "company_calendar",
    "fiscal_label", "fiscal_year_end", "held_actuals", "last_reported_period",
    "relabel_consensus", "relabel_rows", "reported_period_evidence", "shift_months",
    "stored_relabel",
]
