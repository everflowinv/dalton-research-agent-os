"""Batch B 数字接线: period calendar, consensus table, street tables, release
outlook, forward valuation, market block and the numbers pack.

The regression at the heart of it is Accenture on 2026-10-09: the 8-K for
FY2026 Q4 had been furnished on 2026-10-01, the 10-K had not, and the
consensus lane still believed 2026-05-31 was the last reported quarter -- so
the vendor's ``0q`` was stored as FY2026Q4 although its year-ago revenue,
18,742,125,000, is FY2026 Q1's filed revenue.  It was FY2027Q1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dalton_core import financial_period as fp
from dalton_core.consensus_estimate import ConsensusEstimateAuthority
from dalton_core.consensus_table import consensus_delta, consensus_table, consensus_table_from
from dalton_core.earnings_release_outlook import (
    EarningsReleaseOutlookStore, actuals_for_calendar, latest_outlook, parse_release, plain_text,
)
from dalton_core.market_data_block import market_block_from
from dalton_core.numbers_pack import display_block, numbers_pack, render_text
from dalton_core.store import DaltonStore, content_hash
from dalton_core.street_estimate_tables import (
    StreetEstimateFigureStore, model_fallback, parse_tables, rating_target_changes, scan_company,
    street_table_from,
)
from dalton_core.valuation_forward import ebitda_ttm, forward_block, ntm, our_eps, total_debt

ACN = "company:sec-cik:0001467373"
PEER = "company:sec-cik:0001058290"
FIXTURE = Path(__file__).parent / "fixtures" / "numbers" / "acn-q4fy26earnings8-kexhibit.htm"
EXHIBIT_SHA = "99ae2d87332b734b7b700007691106502874a7e6f4ce2f9247d9f5b11bfbc98d"
ACCESSION_8K = "0001467373-26-000037"


def row(period, avg, low=None, high=None, year_ago=None, growth=None, n=20):
    return {"period": period, "avg": avg, "low": low, "high": high, "year_ago": year_ago,
            "growth": growth, "number_of_analysts": n, "currency": "USD"}


# The vendor block stored as version 61 on 2026-10-09 (values as observed).
ACN_WIRE = {
    "ticker": "ACN", "as_of": "2026-10-09",
    "price_target": {"current": "207.28", "high": "275", "low": "163", "mean": "223.65459",
                     "median": "223", "number_of_analysts": 24},
    "recommendations": [{"period": "0m", "strong_buy": 2, "buy": 9, "hold": 16, "sell": 0, "strong_sell": 0}],
    "eps_estimates": [
        row("0q", "3.94689", "3.82", "4.04", "3.03", "0.3026", 21),
        row("+1q", "3.15056", "3.04", "3.49", "2.93", "0.0753", 21),
        row("0y", None, n=None),
        row("+1y", "15.90406", "15.03", "16.61", "13.97", "0.1384", 28),
    ],
    "revenue_estimates": [
        row("0q", "19411241500", "19217000000", "19632000000", "18742125000", "0.0357", 19),
        row("+1q", "18828149490", "18522700000", "19068900000", "18044066000", "0.0435", 18),
        row("0y", "73566672090", "73320000000", "73996000000", "69672977000", "0.0559", 26),
        row("+1y", "81240639950", "78471000000", "83677000000", "77450807130", "0.048899997", 25),
    ],
}


def _plain_tables(connection: sqlite3.Connection) -> None:
    """Just the columns the readers use; no triggers (fixture only)."""

    connection.executescript("""
    CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings (
        ingest_id TEXT PRIMARY KEY, company_ref TEXT, accession TEXT, form TEXT, filed TEXT, report_date TEXT);
    CREATE TABLE IF NOT EXISTS coverage_mission_statement_lines (
        line_id TEXT PRIMARY KEY, ingest_id TEXT, statement TEXT, ordinal INTEGER, concept TEXT, label TEXT,
        level INTEGER, parent_concept TEXT, is_breakdown INTEGER, dimension_axis TEXT, dimension_member TEXT,
        period_start TEXT, period_end TEXT, value TEXT, unit TEXT, balance TEXT, dimension_count INTEGER);
    CREATE TABLE IF NOT EXISTS earnings_release_reads (
        read_id TEXT PRIMARY KEY, company_ref TEXT, accession TEXT, status TEXT, reason TEXT, period TEXT,
        exhibit_url TEXT, body_sha256 TEXT, row_count INTEGER, record_json TEXT, content_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS coverage_mission_versions (mission_version_id TEXT PRIMARY KEY, record_json TEXT);
    CREATE TABLE IF NOT EXISTS coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS market_price_series_versions (
        version_id TEXT PRIMARY KEY, company_ref TEXT, version_number INTEGER, record_json TEXT, ticker TEXT);
    CREATE TABLE IF NOT EXISTS street_estimates (
        estimate_id TEXT PRIMARY KEY, company_ref TEXT, document_ref TEXT, broker TEXT, published_on TEXT,
        rating TEXT, target_value TEXT, source_manifest_hash TEXT);
    """)


def _filing(connection, ingest, form, filed, report_date, company=ACN, accession=None):
    connection.execute("INSERT INTO coverage_mission_statement_filings VALUES (?,?,?,?,?,?)",
                       (ingest, company, accession or f"0001467373-{ingest[-6:]}", form, filed, report_date))


_LINE = [0]


def _line(connection, ingest, statement, concept, start, end, value, unit="usd"):
    _LINE[0] += 1
    connection.execute(
        "INSERT INTO coverage_mission_statement_lines (line_id, ingest_id, statement, ordinal, concept, label, level, "
        "is_breakdown, period_start, period_end, value, unit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"line-{_LINE[0]}", ingest, statement, _LINE[0], concept, concept, 0, 0, start, end, value, unit))


def build_acn_core(state: Path, *, release: bool = True, with_spool: bool = True) -> DaltonStore:
    """A Core holding ACN as it stood on 2026-10-09 (the parts the readers use)."""

    state.mkdir(parents=True, exist_ok=True)
    store = DaltonStore(str(state / "core.sqlite"))
    c = store.connection
    _plain_tables(c)
    _filing(c, "ing-k25", "10-K", "2025-10-10", "2025-08-31")
    _filing(c, "ing-q26a", "10-Q", "2025-12-18", "2025-11-30")
    _filing(c, "ing-q26b", "10-Q", "2026-03-19", "2026-02-28")
    _filing(c, "ing-q26c", "10-Q", "2026-06-18", "2026-05-31")
    rev, eps = "us-gaap:Revenues", "us-gaap:EarningsPerShareDiluted"
    _line(c, "ing-k25", "income", rev, "2024-09-01", "2025-08-31", "69672977000")
    _line(c, "ing-k25", "income", rev, "2025-06-01", "2025-08-31", "17596260000")
    _line(c, "ing-k25", "income", eps, "2025-06-01", "2025-08-31", "2.25", "usdPerShare")
    _line(c, "ing-q26a", "income", rev, "2025-09-01", "2025-11-30", "18742125000")
    _line(c, "ing-q26a", "income", eps, "2025-09-01", "2025-11-30", "3.54", "usdPerShare")
    _line(c, "ing-q26b", "income", rev, "2025-12-01", "2026-02-28", "18044066000")
    _line(c, "ing-q26b", "income", eps, "2025-12-01", "2026-02-28", "2.93", "usdPerShare")
    _line(c, "ing-q26c", "income", rev, "2026-03-01", "2026-05-31", "18718144000")
    # balance sheet and flows for EV / EBITDA (quarterly so a TTM can be formed)
    _line(c, "ing-q26c", "balance", "us-gaap:DebtCurrent", None, "2026-05-31", "112816000")
    _line(c, "ing-q26c", "balance", "us-gaap:LongTermDebtAndCapitalLeaseObligations", None, "2026-05-31", "5029449000")
    _line(c, "ing-q26c", "balance", "us-gaap:CashAndCashEquivalentsAtCarryingValue", None, "2026-05-31", "10165245000")
    for ingest, start, end, op, da in (
            ("ing-k25", "2025-06-01", "2025-08-31", "2050000000", "400000000"),
            ("ing-q26a", "2025-09-01", "2025-11-30", "2873698000", "410000000"),
            ("ing-q26b", "2025-12-01", "2026-02-28", "2493547000", "420000000"),
            ("ing-q26c", "2026-03-01", "2026-05-31", "3175298000", "430000000")):
        _line(c, ingest, "income", "us-gaap:OperatingIncomeLoss", start, end, op)
        _line(c, ingest, "cash", "acn:DepreciationAmortizationAndOther", start, end, da)
    if release:
        record = {"accession": ACCESSION_8K, "body_sha256": EXHIBIT_SHA, "company_ref": ACN,
                  "document_ref": f"sec:filing:{ACCESSION_8K}", "period": "FY2026-Q4",
                  "period_start": "2026-06-01", "period_end": "2026-08-31", "report_date": "2026-10-01",
                  "status": "parsed", "id": "earnings-release-read:test",
                  "exhibit_url": "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000037/q4fy26earnings8-kexhibit.htm"}
        c.execute("INSERT INTO earnings_release_reads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("earnings-release-read:test", ACN, ACCESSION_8K, "parsed", None, "FY2026-Q4",
                   record["exhibit_url"], EXHIBIT_SHA, 11, json.dumps(record), "0" * 64,
                   "2026-10-09T11:03:29+00:00"))
    if with_spool:
        objects = state / "transcript-spool" / "connector-spool" / "objects" / EXHIBIT_SHA[:2]
        objects.mkdir(parents=True, exist_ok=True)
        (objects / EXHIBIT_SHA).write_bytes(FIXTURE.read_bytes())
    mission = {"mission_ref": "mission:test", "universe": [
        {"company_ref": ACN, "ticker": "ACN"}, {"company_ref": PEER, "ticker": "CTSH"}]}
    c.execute("INSERT INTO coverage_mission_versions VALUES (?,?)", ("mv-1", json.dumps(mission)))
    c.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)", ("mission:test", "mv-1", "2026-10-01"))
    for ref, base in ((ACN, Decimal("200")), (PEER, Decimal("70"))):
        c.execute("INSERT INTO market_price_series_versions VALUES (?,?,?,?,?)",
                  (f"mps-{ref}", ref, 1, json.dumps(price_record(ref, base)), "X"))
    authority = ConsensusEstimateAuthority(store)
    authority.publish_consensus(
        company_ref=ACN, wire=ACN_WIRE, fiscal_year_end="08-31", last_reported_period_end="2026-05-31",
        invocation_ref="connector-invocation:yfinance:" + "a" * 32, artifact_hash="1" * 64,
        governance_ref="connector-governance:yfinance-analyst-estimates:v1", governance_hash="2" * 64,
        captured_at="2026-10-09T13:51:43+00:00")
    return store


def price_record(ref, base: Decimal, *, days: int = 400, volume: bool = True) -> dict:
    bars = []
    day = date(2026, 10, 8) - timedelta(days=days)
    price = base
    step = 0
    while day <= date(2026, 10, 8):
        if day.weekday() < 5:
            step += 1
            price = base * (Decimal(1) + Decimal(step % 50 - 25) / Decimal(500)) + Decimal(step) / Decimal(10)
            bars.append({"date": day.isoformat(), "open": str(price), "high": str(price + 2),
                         "low": str(price - 2), "close": str(price.quantize(Decimal("0.01"))),
                         "adj_close": str(price.quantize(Decimal("0.01"))),
                         "volume": "1000000" if volume else None,
                         "captured_at": "2026-10-09T01:00:00+00:00"})
        day += timedelta(days=1)
    return {"id": f"mps:{ref}", "company_ref": ref, "ticker": "X", "currency": "USD", "bars": bars,
            "source_ref": "source:yahoo-finance",
            "observations": [{"observation": "shares_outstanding", "as_of": "2026-10-08", "value": "600000000"}]}


class PeriodCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = build_acn_core(self.state)
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def test_the_8k_release_advances_the_last_reported_period(self):
        cal = fp.company_calendar(self.c, ACN, "2026-10-09")
        self.assertEqual(cal["fiscal_year_end"], "08-31")
        self.assertEqual(cal["last_reported_period_end"], "2026-08-31")
        self.assertEqual(cal["last_filed_period_end"], "2026-05-31")
        self.assertTrue(cal["advanced_by_release"])
        self.assertIn("earnings_release_8k", cal["last_reported_bases"])

    def test_evidence_is_read_as_of_a_date_without_hindsight(self):
        cal = fp.company_calendar(self.c, ACN, "2026-09-20")
        self.assertEqual(cal["last_reported_period_end"], "2026-05-31")
        self.assertFalse(cal["advanced_by_release"])

    def test_acn_regression_year_ago_proves_fy2027q1(self):
        record = json.loads(self.c.execute(
            "SELECT record_json FROM consensus_estimate_versions").fetchone()[0])
        # The stored label is the bug.
        stored = {r["vendor_period"]: r["label"] for r in record["revenue_estimates"]}
        self.assertEqual(stored["0q"], "FY2026Q4")
        relabelled = fp.relabel_consensus(self.c, record)
        revenue = {r["vendor_period"]: r for r in relabelled["revenue_estimates"]}
        self.assertEqual(revenue["0q"]["label"], "FY2027Q1")
        self.assertEqual(revenue["0q"]["label_basis"], fp.LABEL_CONFIRMED)
        self.assertEqual(revenue["0q"]["year_ago_check"], "match")
        self.assertEqual(revenue["+1q"]["label"], "FY2027Q2")
        # The vendor's annual block had not rolled: its year-ago is FY2025.
        self.assertEqual(revenue["0y"]["label"], "FY2026")
        self.assertEqual(revenue["0y"]["label_basis"], fp.LABEL_SHIFTED)
        self.assertTrue(revenue["0y"]["reported"])
        self.assertIn("FY2026Q4->FY2027Q1", relabelled["changed_labels"])

    def test_release_actuals_confirm_the_eps_annual_shift(self):
        record = json.loads(self.c.execute(
            "SELECT record_json FROM consensus_estimate_versions").fetchone()[0])
        items = parse_release(plain_text(FIXTURE.read_bytes()), fye_month=8,
                              release_period={"kind": "quarter", "fiscal_year": 2026, "fiscal_quarter": 4})
        extra = actuals_for_calendar(items, "sec:filing:x")
        relabelled = fp.relabel_consensus(self.c, record, extra)
        eps = {r["vendor_period"]: r for r in relabelled["eps_estimates"]}
        self.assertEqual(eps["+1y"]["label"], "FY2027")
        self.assertEqual(eps["+1y"]["year_ago_check"], "match")  # 13.97 = FY2026 adjusted EPS

    def test_broker_medians_place_a_half_rolled_vendor_block(self):
        # Yahoo from 2026-10-03: a cached FY2026 0y beside an FY2028 +1y whose
        # year-ago (13.97, FY2026's actual) would put it in FY2027.
        record = json.loads(self.c.execute(
            "SELECT record_json FROM consensus_estimate_versions").fetchone()[0])
        anchors = {("eps", "FY2026"): "13.86", ("eps", "FY2027"): "14.75", ("eps", "FY2028"): "16.30",
                   ("eps", "FY2029"): "18.07", ("revenue", "FY2026"): "73500000000",
                   ("revenue", "FY2027"): "76020000000", ("revenue", "FY2028"): "79770000000",
                   ("eps", "FY2026Q4"): "3.19", ("eps", "FY2027Q1"): "3.99", ("eps", "FY2027Q2"): "3.10",
                   ("revenue", "FY2027Q1"): "19372000000"}
        relabelled = fp.relabel_consensus(self.c, record, anchors=anchors)
        eps = {r["vendor_period"]: r for r in relabelled["eps_estimates"]}
        revenue = {r["vendor_period"]: r for r in relabelled["revenue_estimates"]}
        self.assertEqual((eps["+1y"]["label"], eps["+1y"]["label_basis"]), ("FY2028", fp.LABEL_ANCHORED))
        self.assertEqual(revenue["+1y"]["label"], "FY2028")
        self.assertEqual(revenue["0y"]["label"], "FY2026")
        self.assertEqual(eps["0q"]["label"], "FY2027Q1")
        # One anchored candidate is not decisive: +1q revenue stays on its year-ago label.
        self.assertEqual((revenue["+1q"]["label"], revenue["+1q"]["label_basis"]),
                         ("FY2027Q2", fp.LABEL_CONFIRMED))
        self.assertFalse(any(r["duplicate"] for r in relabelled["revenue_estimates"]))

    def test_rederivation_is_append_only_and_idempotent(self):
        before = self.c.execute("SELECT record_json, content_hash FROM consensus_estimate_versions").fetchall()
        relabels = fp.ConsensusPeriodRelabelStore(self.store)
        first = relabels.rederive(ACN, now="2026-10-09T15:00:00+00:00")
        second = relabels.rederive(ACN, now="2026-10-09T16:00:00+00:00")
        self.assertEqual(first["appended"], 1)
        self.assertEqual(second, {"status": "ok", "appended": 0, "unchanged": 1})
        after = self.c.execute("SELECT record_json, content_hash FROM consensus_estimate_versions").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        stored = fp.stored_relabel(self.c, json.loads(before[0][0])["id"])
        self.assertEqual(stored["calendar"]["last_reported_period_end"], "2026-08-31")
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE consensus_period_relabels SET changed_count=0")
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("DELETE FROM consensus_period_relabels")

    def test_the_lane_calendar_reader_uses_the_release(self):
        from dalton_core.mission_consensus_lane import _fiscal_calendar_reader

        class Server:
            class store:
                connection = None

        Server.store.connection = self.c
        self.c.row_factory = sqlite3.Row
        calendar = _fiscal_calendar_reader(Server)(ACN)
        self.assertEqual(calendar["last_reported_period_end"], "2026-08-31")
        self.assertEqual(calendar["fiscal_year_end"], "08-31")


class ConsensusTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        build_acn_core(self.state).close()

    def test_table_renders_corrected_periods_targets_ratings_and_staleness(self):
        table = consensus_table(self.state, ACN, "2026-10-09")
        self.assertEqual(table["status"], "ok")
        self.assertEqual([p["label"] for p in table["periods"]], ["FY2027", "FY2028", "FY2029"])
        fy = table["periods"][0]
        self.assertEqual(fy["eps"]["mean"], "15.90406")
        self.assertEqual(fy["eps"]["count"], 28)
        self.assertEqual(fy["revenue"]["low"], "78471000000")
        self.assertEqual(table["periods"][1]["status"], "unavailable")
        self.assertEqual([q["label"] for q in table["quarters"]], ["FY2027Q1", "FY2027Q2"])
        self.assertEqual(table["quarters"][0]["revenue"]["stored_label"], "FY2026Q4")
        self.assertEqual(table["target"]["mean"], "223.65459")
        self.assertEqual(table["ratings"]["hold"], 16)
        self.assertEqual(table["ratings"]["total"], 27)
        self.assertEqual(table["staleness"]["vendor_days_old"], 0)
        self.assertTrue(table["calendar"]["advanced_by_release"])
        self.assertTrue(table["relabel"]["used_release_actuals"])
        self.assertEqual(table["pre_report"][0]["label"], "FY2026")

    def test_delta_flags_material_moves_only(self):
        a = consensus_table(self.state, ACN, "2026-10-09")
        b = json.loads(json.dumps(a))
        b["periods"][0]["eps"]["mean"] = str(Decimal("15.90406") * Decimal("1.02"))
        moved = consensus_delta(a, b)
        self.assertTrue(moved["material"])
        self.assertEqual(moved["changes"][0]["period"], "FY2027")
        c = json.loads(json.dumps(a))
        c["periods"][0]["eps"]["mean"] = "15.91"
        self.assertFalse(consensus_delta(a, c)["material"])
        self.assertTrue(consensus_delta(a, c, {"eps": "0.0001"})["material"])
        self.assertEqual(consensus_delta(a, a)["change_count"], 0)

    def test_missing_core_is_an_empty_state(self):
        empty = consensus_table(Path(self.tmp.name) / "nowhere", ACN)
        self.assertEqual(empty["status"], "empty")


class OutlookTests(unittest.TestCase):
    def setUp(self):
        self.text = plain_text(FIXTURE.read_bytes())
        self.items = parse_release(self.text, fye_month=8, release_period={
            "kind": "quarter", "fiscal_year": 2026, "fiscal_quarter": 4})

    def find(self, kind, metric, period):
        return next(i for i in self.items if i["kind"] == kind and i["metric"] == metric and i["period"] == period)

    def test_bookings_book_to_bill_and_growth(self):
        bookings = self.find("bookings", "bookings", "FY2026Q4")
        self.assertEqual((bookings["value"], bookings["scale"]), ("22.17", "billion"))
        self.assertEqual(self.find("book_to_bill", "book_to_bill", "FY2026Q4")["value"], "1.2")
        self.assertEqual(self.find("growth", "revenue_growth_lc", "FY2026Q4")["value"], "7")
        self.assertEqual(self.find("book_to_bill", "book_to_bill:managed_services", "FY2026Q4")["value"], "1.4")
        self.assertEqual(self.find("bookings", "bookings", "FY2026")["value"], "84.54")

    def test_guidance_ranges_and_versus_prior_guidance(self):
        q1 = self.find("guidance", "revenue", "FY2027Q1")
        self.assertEqual((q1["low"], q1["high"], q1["scale"]), ("18.95", "19.60", "billion"))
        eps = self.find("guidance", "eps", "FY2027")
        self.assertEqual((eps["low"], eps["high"]), ("14.39", "14.81"))
        lc = self.find("guidance", "revenue_growth_lc", "FY2027")
        self.assertEqual((lc["low"], lc["high"]), ("3", "6"))
        margin = self.find("guidance", "operating_margin", "FY2027")
        self.assertEqual((margin["low"], margin["high"]), ("15.9", "16.1"))
        above = self.find("guidance_vs_actual", "revenue_growth_lc_prior_guidance", "FY2026Q4")
        self.assertEqual((above["low"], above["high"], above["position"]), ("1", "5", "above"))
        self.assertEqual(self.find("guidance", "capital_return", "FY2027")["qualifier"], "at_least")

    def test_reported_adjusted_eps_for_the_year_ago_check(self):
        self.assertEqual(self.find("reported", "eps_adjusted", "FY2026")["value"], "13.97")
        self.assertEqual(self.find("reported", "eps_adjusted", "FY2025Q4")["value"], "3.03")

    def test_every_item_quotes_its_sentence(self):
        for item in self.items:
            start, end = item["span"]
            self.assertEqual(self.text[start:end], item["quote"])
            self.assertEqual(item["quote_sha256"], hashlib.sha256(item["quote"].encode()).hexdigest())
            for key in ("value", "low", "high"):
                if item.get(key) not in (None, "0"):
                    digits = item[key].lstrip("-")
                    self.assertTrue(digits.split(".")[0] in item["quote"], (key, item))

    def test_store_is_append_only_and_read_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = build_acn_core(Path(tmp.name))
        self.addCleanup(store.close)
        outlooks = EarningsReleaseOutlookStore(store)
        first = outlooks.ingest_company(tmp.name, ACN)
        second = outlooks.ingest_company(tmp.name, ACN)
        self.assertEqual([r["status"] for r in first], ["recorded"])
        self.assertEqual([r["status"] for r in second], ["duplicate"])
        held = latest_outlook(store.connection, ACN)
        self.assertEqual(held["accession"], ACCESSION_8K)
        self.assertGreater(held["item_count"], 30)
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("DELETE FROM earnings_release_outlooks")

    def test_a_tampered_exhibit_is_refused(self):
        from dalton_core.earnings_release_outlook import EarningsReleaseOutlookError, outlook_record

        with self.assertRaises(EarningsReleaseOutlookError):
            outlook_record({"body_sha256": "0" * 64}, b"<p>other</p>", fye_month=8)


GS_LIKE = """Exhibit 1: Summary of House vs. Street estimates
Financials ($ mn)
GS
Street
Δ
GS
Street
Δ
Total Revenue
18,127
18,064
+0.3%
76,167
76,401
-0.3%
EPS (non-GAAP diluted)
$3.12
$3.18
-1.9%
$14.75
$14.59
+1.1%
4Q26 (E)
FY27 (E)
Source: House research, Visible Alpha Consensus Data
"""
TD_LIKE = """Price Target: $173.00 (Prior $151.00)
FY
(Aug)
2026E
2027E
2028E
EPS
Q1
$3.94A
$3.83
-
Year
$13.87
$14.16
$15.12
Prior Year
$13.88
$14.12
$15.06
Consensus EPS
$13.86
$14.68
$15.75
Revenue (MM)
Year
$73,524.0
$75,562.0
$78,601.0
EBITDA
Year
$13,935.0
$14,517.0
$15,199.0
Prior Year
-
$14,479.0
$15,140.0
"""
WF_LIKE = """$ (Aug)
Q1
Q2
Q3
Q4
FY
EPS
2026E
3.94 A
2.93 A
3.80 A
3.19 E 13.86 E
2027E
4.00 E
3.10 E
4.07 E
3.60 E 14.77 E
"""


class StreetTableTests(unittest.TestCase):
    def figures(self, text):
        return {(f["metric"], f["estimate_kind"], f["period"]): f["value"]
                for f in parse_tables(text, fye_month=8)["figures"]}

    def test_house_vs_street_triple_with_trailing_header(self):
        got = self.figures(GS_LIKE)
        self.assertEqual(got[("revenue", "house", "FY2026Q4")], "18127000000")
        self.assertEqual(got[("revenue", "street_per_broker", "FY2027")], "76401000000")
        self.assertEqual(got[("eps", "house", "FY2027")], "14.75")
        self.assertEqual(got[("eps", "street_per_broker", "FY2026Q4")], "3.18")

    def test_triple_whose_delta_does_not_reconcile_is_refused(self):
        bad = GS_LIKE.replace("-1.9%", "+9.0%")
        parsed = parse_tables(bad, fye_month=8)
        self.assertNotIn(("eps", "house", "FY2026Q4"),
                         {(f["metric"], f["estimate_kind"], f["period"]) for f in parsed["figures"]})
        self.assertIn("delta_does_not_reconcile", {s["reason"] for s in parsed["skipped"]})

    def test_sub_labelled_rows_prior_consensus_and_section_end(self):
        got = self.figures(TD_LIKE)
        self.assertEqual(got[("eps", "house", "FY2027Q1")], "3.83")
        self.assertNotIn(("eps", "house", "FY2026Q1"), got)  # "A" is an actual
        self.assertEqual(got[("eps", "house", "FY2028")], "15.12")
        self.assertEqual(got[("eps", "house_prior", "FY2027")], "14.12")
        self.assertEqual(got[("eps", "street_per_broker", "FY2027")], "14.68")
        self.assertEqual(got[("revenue", "house", "FY2027")], "75562000000")
        # EBITDA's "Prior Year" must not become a revenue figure.
        self.assertNotIn(("revenue", "house_prior", "FY2027"), got)

    def test_transposed_year_by_quarter_grid(self):
        got = self.figures(WF_LIKE)
        self.assertEqual(got[("eps", "house", "FY2026")], "13.86")
        self.assertEqual(got[("eps", "house", "FY2027Q4")], "3.6")
        self.assertNotIn(("eps", "house", "FY2026Q1"), got)

    def test_prose_revision_and_target_change(self):
        text = "Estimate adjustments: we adjust our FY27/28 EPS estimates to $14.75/$16.30 from\n$14.55/$15.95."
        got = self.figures(text)
        self.assertEqual(got[("eps", "house", "FY2028")], "16.3")
        self.assertEqual(got[("eps", "house_prior", "FY2027")], "14.55")
        changes = rating_target_changes(TD_LIKE)
        self.assertEqual((changes[0]["from"], changes[0]["to"], changes[0]["direction"]),
                         ("151.00", "173.00", "raised"))

    def test_revenue_without_a_printed_scale_is_refused(self):
        text = "2026E\n2027E\nRevenues\n73,524\n75,562\n"
        parsed = parse_tables(text, fye_month=8)
        self.assertEqual(parsed["figures"], [])
        self.assertEqual(parsed["skipped"][0]["reason"], "revenue_scale_not_printed")

    def test_store_scan_and_street_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        store = build_acn_core(state, release=False, with_spool=False)
        self.addCleanup(store.close)
        notes = {"doc:td": ("td", "2026-08-25", TD_LIKE), "doc:gs": ("goldman", "2026-09-11", GS_LIKE),
                 "doc:x": ("ubs", "2026-08-25", "nothing here but EPS words\nEPS\nsee text")}
        for ref, (house, day, text) in notes.items():
            digest = hashlib.sha256(text.encode()).hexdigest()
            folder = state / "acquisitions" / ref.replace(":", "-")
            folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps(
                {"document_ref": ref, "assembled_object": {"content_hash": digest}}))
            objects = state / "transcript-spool" / "connector-spool" / "objects" / digest[:2]
            objects.mkdir(parents=True, exist_ok=True)
            (objects / digest).write_bytes(text.encode())
            store.connection.execute("INSERT INTO street_estimates VALUES (?,?,?,?,?,?,?,?)",
                                     (f"se-{ref}", ACN, ref, house, day, "hold", "173", digest))
        figures = StreetEstimateFigureStore(store)
        results = scan_company(figures, state, ACN, fye_month=8, limit=5)
        outcomes = {r["document_ref"]: r["status"] for r in results}
        self.assertEqual(outcomes["doc:td"], "recorded")
        self.assertEqual(outcomes["doc:gs"], "recorded")
        self.assertEqual(outcomes["doc:x"], "needs_model")
        self.assertEqual(scan_company(figures, state, ACN, fye_month=8, limit=5), [])
        table = street_table_from(store.connection, ACN, as_of="2026-10-09")
        fy27 = next(p for p in table["periods"] if p["period"] == "FY2027" and p["metric"] == "eps")
        self.assertEqual(fy27["count"], 2)
        self.assertEqual(fy27["median"], "14.455")
        self.assertEqual(sorted(i["house"] for i in fy27["street_quoted"]), ["goldman", "td"])
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("UPDATE street_estimate_figures SET value='1'")

    def test_model_fallback_respects_the_budget_gate(self):
        calls = []
        result = model_fallback("EPS 1 2 3", company_ref=ACN, document_ref="doc", house="x",
                                subject_as_named="Accenture", model_call=lambda *a: calls.append(a),
                                budget_ok=lambda purpose: False)
        self.assertEqual(result["status"], "budget_refused")
        self.assertEqual(calls, [])


class ValuationAndMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = build_acn_core(self.state)
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def test_total_debt_is_current_plus_noncurrent_with_components(self):
        debt = total_debt(self.c, ACN, as_of="2026-10-09")
        self.assertEqual(debt["status"], "available")
        self.assertEqual(debt["value"], "5142265000")
        self.assertEqual(debt["basis"], "current_plus_noncurrent")
        self.assertEqual({c["concept"] for c in debt["components"]},
                         {"us-gaap:DebtCurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations"})

    def test_ebitda_admits_a_labelled_extension_da_concept(self):
        ebitda = ebitda_ttm(self.c, ACN, as_of="2026-10-09")
        self.assertEqual(ebitda["status"], "available")
        self.assertEqual(ebitda["value"], str(2050000000 + 2873698000 + 2493547000 + 3175298000
                                              + 400000000 + 410000000 + 420000000 + 430000000))
        self.assertIn("acn:DepreciationAmortizationAndOther", ebitda["note"])

    def test_ntm_weights_fy1_and_fy2(self):
        got = ntm(("FY2027", "14", "2027-08-31"), ("FY2028", "16", "2028-08-31"), "2026-10-09")
        self.assertEqual(got["method"], "time_weighted_fy1_fy2")
        weight = Decimal((date(2027, 8, 31) - date(2026, 10, 9)).days) / 365
        self.assertEqual(Decimal(got["value"]), (weight * 14 + (1 - weight) * 16).quantize(Decimal("0.01")))
        alone = ntm(("FY2027", "14", "2027-08-31"), None, "2026-10-09")
        self.assertEqual((alone["method"], alone["value"]), ("fy1_only", "14"))

    def test_our_eps_needs_an_eps_row(self):
        payload = {"status": "ok", "period": {"forecast": ["2026-11-30", "2027-02-28", "2027-05-31", "2027-08-31"]},
                   "rows": [{"key": "revenue", "label": "收入", "cells": []}]}
        self.assertEqual(our_eps(payload, ACN)["status"], "unavailable")
        payload["rows"].append({"key": "eps_diluted", "label": "每股收益", "unit_kind": "per_share",
                                "cells": [{"end": end, "value": "3.5"} for end in payload["period"]["forecast"]]})
        got = our_eps(payload, ACN)
        self.assertEqual(got["status"], "unavailable")
        self.assertIn("季度EPS不能相加", got["reason"])

    def test_market_block_adv_range_returns_relative(self):
        market = market_block_from(self.c, ACN, as_of="2026-10-09", basket=[ACN, PEER])
        self.assertEqual(market["status"], "ok")
        self.assertEqual(market["adv_30d"]["status"], "available")
        self.assertEqual(market["adv_30d"]["shares"], "1000000")
        self.assertIn("high", market["range_52w"])
        for key in ("ytd", "1m", "3m", "6m"):
            self.assertEqual(market["returns"][key]["status"], "available")
            self.assertEqual(market["relative_to_basket"]["windows"][key]["status"], "available")
        self.assertIn("short_interest", market["unavailable"])

    def test_missing_volume_is_said_not_zeroed(self):
        self.c.execute("UPDATE market_price_series_versions SET record_json=? WHERE company_ref=?",
                       (json.dumps(price_record(ACN, Decimal(200), volume=False)), ACN))
        market = market_block_from(self.c, ACN, as_of="2026-10-09")
        self.assertEqual(market["adv_30d"]["status"], "unavailable")
        self.assertIn("没有成交量", market["adv_30d"]["reason"])

    def test_forward_block_three_sources(self):
        consensus = consensus_table_from(self.c, ACN, "2026-10-09", state_dir=self.state, today="2026-10-09")
        market = market_block_from(self.c, ACN, as_of="2026-10-09")
        block = forward_block(self.c, ACN, as_of="2026-10-09", consensus=consensus, street={"periods": []},
                              market=market, ours={"status": "unavailable", "reason": "no EPS row"})
        rows = {r["source"]: r for r in block["forward_pe"]}
        self.assertEqual(rows["consensus_vendor"]["method"], "next_four_fiscal_quarters")
        self.assertEqual(rows["consensus_vendor"]["window"], ["FY2027Q1", "FY2027Q2", "FY2027Q3", "FY2027Q4"])
        self.assertIsNotNone(rows["consensus_vendor"]["forward_pe"])
        self.assertEqual(rows["ours"]["status"], "unavailable")
        self.assertIsNotNone(block["enterprise_value"]["value"])
        self.assertIsNotNone(block["ev_to_ebitda"])


class NumbersPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        build_acn_core(self.state).close()

    def test_pack_assembles_every_block_and_text_is_bounded(self):
        pack = numbers_pack(self.state, ACN, "2026-10-09")
        body = pack["json"]
        for key in ("consensus", "street", "outlook", "valuation", "peers", "market", "cross_checks"):
            self.assertIn(key, body)
        self.assertEqual(body["consensus"]["periods"][0]["label"], "FY2027")
        self.assertTrue(body["outlook"]["items"])
        self.assertLessEqual(len(pack["text"]), 6000)
        self.assertIn("FY2027", pack["text"])
        self.assertIn("由8-K推进", pack["text"])
        # The vendor's FY2027 EPS sits above the company's guided range.
        self.assertIn("consensus_outside_guidance", {f["kind"] for f in body["cross_checks"]})
        json.dumps(body, default=str)

    def test_render_truncates_to_the_limit(self):
        pack = numbers_pack(self.state, ACN, "2026-10-09")["json"]
        self.assertLessEqual(len(render_text(pack, limit=400)), 400)

    def test_display_block_for_the_cockpit(self):
        c = sqlite3.connect(f"file:{self.state / 'core.sqlite'}?mode=ro", uri=True)
        self.addCleanup(c.close)
        c.row_factory = sqlite3.Row
        block = display_block(c, ACN, as_of="2026-10-09", basket=[ACN, PEER])
        self.assertEqual(block["consensus"]["fiscal_year"], "FY2027")
        self.assertEqual(block["consensus"]["label_note"], "财期已按 8-K 业绩稿更正")
        self.assertIsNotNone(block["market"]["adv_30d_value"])

    def test_charts_numbers_payload(self):
        from dalton_core.cockpit_v2.charts_api import KINDS, chart_payload

        self.assertIn("numbers", KINDS)
        payload = chart_payload("numbers", {"company": ACN, "as_of": "2026-10-09", "peers": "0"},
                                {"core_db": self.state / "core.sqlite"})
        self.assertEqual(payload["status"], "ok", payload.get("empty_reason"))
        self.assertEqual(payload["numbers"]["consensus"]["periods"][0]["label"], "FY2027")
        self.assertTrue(payload["text"].startswith("【数字包】"))


class BlankWorkspaceCanaryTests(unittest.TestCase):
    """Every batch B reader on a freshly created workspace: empty, never an error."""

    def setUp(self):
        from dalton_core.workspace_creation import create_blank_workspace

        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        release = root / "release"
        release.mkdir()
        catalog = {"schema_version": "dalton-shared-connection-catalog-0.1", "models": [], "sources": []}
        catalog["content_hash"] = content_hash(catalog)
        catalog_path = root / "connections.json"
        catalog_path.write_text(json.dumps(catalog))
        receipt = create_blank_workspace(
            root, "new", 18953, "release:sha256:" + "c" * 64, release,
            request_id="canary-numbers", display_name="新研究", shared_readonly_paths=[catalog_path],
            connection_catalog=dict(path=str(catalog_path), content_hash=catalog["content_hash"]))
        env = patch.dict(os.environ, DALTON_WORKSPACE_MANIFEST=receipt["manifest_path"])
        env.start()
        self.addCleanup(env.stop)
        self.state = root / "workspaces/new/state/dalton-core"

    def test_blank_workspace_reads_are_empty_states(self):
        from dalton_core.cockpit_v2.charts_api import chart_payload
        from dalton_core.street_estimate_tables import street_table

        self.assertEqual(consensus_table(self.state, ACN)["status"], "empty")
        self.assertEqual(street_table(self.state, ACN, parse_unstored=True)["status"], "empty")
        pack = numbers_pack(self.state, ACN, "2026-10-09")
        self.assertEqual(pack["json"]["consensus"]["status"], "empty")
        self.assertEqual(pack["json"]["market"]["status"], "empty")
        self.assertLessEqual(len(pack["text"]), 6000)
        payload = chart_payload("numbers", {"company": ACN}, {"core_db": self.state / "core.sqlite"})
        self.assertEqual(payload["status"], "empty")
        c = sqlite3.connect(f"file:{self.state / 'core.sqlite'}?mode=ro", uri=True)
        self.addCleanup(c.close)
        self.assertIsNone(fp.company_calendar(c, ACN))
        self.assertIsNone(display_block(c, ACN, as_of="2026-10-09"))


if __name__ == "__main__":
    unittest.main()


class LaneTableScanHookTests(unittest.TestCase):
    """The consensus lane reads one estimates table a tick once page one is done."""

    def test_scan_falls_through_to_the_table_pass(self):
        from dalton_core.mission_consensus_lane import MissionConsensusLaneCoordinator

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Street:
            def scanned_document_refs(self, company_ref):
                return set()

        seen = []
        coordinator = MissionConsensusLaneCoordinator(
            authority=None, street_store=Street(), launcher=None, mission=lambda: None,
            fiscal_calendar_for=lambda ref: None, next_context=lambda ref, scanned: None,
            failure_ledger_dir=tmp.name,
            table_scan=lambda universe: seen.append(universe) or {"outcome": "table_scanned"})
        result = coordinator._scan([{"company_ref": ACN, "ticker": "ACN"}])
        self.assertEqual(result, {"outcome": "table_scanned"})
        self.assertEqual(len(seen), 1)

        def broken(universe):
            raise RuntimeError("spool gone")

        coordinator.table_scan = broken
        coordinator._scanned_out.clear()
        self.assertEqual(coordinator._scan([{"company_ref": ACN, "ticker": "ACN"}])["outcome"],
                         "table_scan_failed")


DXC_LIKE_Q1 = """First Quarter Fiscal 2027 Results
Free cash flow for the quarter included a $214 million cash receipt from a litigation settlement.
First Quarter Fiscal 2027
(in millions) | First Quarter Fiscal 2026
(in millions)
Net cash provided by operating activities
$420 | $250
Purchases of property and equipment
$(30) | $(40)
Payments for transition and transformation contract costs
$(36) | $(50)
Software purchased and developed
$(40) | $(63)
Free cash flow
$314 | $97
"""
DXC_LIKE_FY = """Fiscal 2026 Results
Fiscal 2026
(in millions) | Fiscal 2025
(in millions)
Net cash provided by operating activities
$1,100 | $1,200
Purchases of property and equipment
$(120) | $(130)
Payments for transition and transformation contract costs
$(127) | $(150)
Software purchased and developed
$(140) | $(160)
Free cash flow
$713 | $760
"""


class OwnerReviewScopeTests(unittest.TestCase):
    """Owner review 2026-10-09 §P1: FCF definitions, dated shares, debt, preview periods."""

    def test_acn_company_fcf_reconciles_and_sits_beside_the_system_figure(self):
        from dalton_core.valuation_fcf_bridge import company_fcf_tables, fcf_bridge

        text = plain_text(FIXTURE.read_bytes())
        tables = company_fcf_tables(text, fye_month=8, release_period={
            "kind": "quarter", "fiscal_year": 2026, "fiscal_quarter": 4})
        fy = next(t for t in tables if t["period"] == "FY2026")
        self.assertEqual(fy["fcf"], "11620000000")
        self.assertTrue(fy["reconciles"])
        self.assertEqual(fy["components"][1]["value"], "-740000000")
        bridge = fcf_bridge(system={"value": "12581688000", "through": "2026-05-31"},
                            releases=[{"text": text, "accession": ACCESSION_8K, "report_date": "2026-10-01",
                                       "release_period": {"kind": "quarter", "fiscal_year": 2026,
                                                          "fiscal_quarter": 4}}],
                            market_cap="127500000000", fye_month=8)
        system, company, adjusted = bridge["columns"]
        self.assertEqual(system["yield_percent"], "9.87")
        self.assertEqual((company["value"], company["method"]), ("11620000000", "fiscal_year_column"))
        self.assertEqual(adjusted["value"], company["value"])

    def test_q1_rolls_fy_plus_q1_minus_prior_q1_and_strips_a_named_one_off(self):
        from dalton_core.valuation_fcf_bridge import fcf_bridge

        releases = [
            {"text": DXC_LIKE_FY, "release_period": {"kind": "quarter", "fiscal_year": 2026, "fiscal_quarter": 4}},
            {"text": DXC_LIKE_Q1, "release_period": {"kind": "quarter", "fiscal_year": 2027, "fiscal_quarter": 1}},
        ]
        bridge = fcf_bridge(system={"value": "1252000000", "through": "2026-06-30"}, releases=releases,
                            market_cap="1976000000", fye_month=3)
        system, company, adjusted = bridge["columns"]
        # 713 - 97 + 314 = 930 (the review's DXC arithmetic)
        self.assertEqual(company["value"], "930000000")
        self.assertEqual(company["yield_percent"], "47.06")
        self.assertEqual(adjusted["value"], "716000000")
        self.assertEqual(adjusted["yield_percent"], "36.23")
        self.assertIn("litigation", adjusted["adjustments"][0]["quote"])
        self.assertEqual(system["yield_percent"], "63.36")

    def test_snapshot_inputs_carry_composite_debt_extension_da_and_dated_shares(self):
        from dalton_core import valuation_snapshot_cli as cli

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = build_acn_core(Path(tmp.name), release=False, with_spool=False)
        self.addCleanup(store.close)
        c = store.connection
        _line(c, "ing-q26b", "income", "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
              "2025-12-01", "2026-02-28", "620000000", "shares")
        _line(c, "ing-q26c", "income", "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
              "2026-03-01", "2026-05-31", "615000000", "shares")

        class Missions:
            connection = c

            def statement_filings(self, company_ref):
                return fp.query(c, "SELECT * FROM coverage_mission_statement_filings WHERE company_ref=?",
                                (company_ref,))

            def statement_lines(self, ingest_id):
                rows = fp.query(c, "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? ORDER BY ordinal",
                                (ingest_id,))
                for row in rows:
                    row["is_breakdown"] = False
                return rows

            def statement_series_lines(self, company_ref, concept, *, statement=None):
                rows = fp.query(c, "SELECT l.*, f.accession AS accession, f.filed AS filed, "
                                   "f.report_date AS report_date FROM coverage_mission_statement_lines l "
                                   "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
                                   "WHERE f.company_ref=? AND l.concept=? AND l.statement=?",
                                (company_ref, concept, statement))
                for row in rows:
                    row["is_breakdown"] = False
                return rows

        history = cli.share_history_from_filings(Missions(), ACN)
        self.assertEqual(history, [{"as_of": "2026-03-19", "shares_outstanding": "620000000"},
                                   {"as_of": "2026-06-18", "shares_outstanding": "615000000"}])
        lines = lambda statement, concept: Missions().statement_series_lines(ACN, concept, statement=statement)  # noqa: E731
        debt = cli._fallback_role(Missions(), ACN, "total_debt", as_of="2026-10-09", currency="usd",
                                  lines=lines)
        self.assertEqual(debt[0]["concept"], "us-gaap:DebtCurrent + us-gaap:LongTermDebtAndCapitalLeaseObligations")
        self.assertEqual(debt[1], Decimal("5142265000"))
        da = cli._fallback_role(Missions(), ACN, "depreciation_amortisation", as_of="2026-10-09",
                                currency="usd", lines=lines)
        self.assertEqual(da[0]["concept"], "acn:DepreciationAmortizationAndOther")
        self.assertEqual(da[1], Decimal("1660000000"))

    def test_the_preview_finds_the_consensus_for_its_own_fiscal_period(self):
        from dalton_core.consensus_estimate import period_consensus
        from dalton_core.earnings_season import consensus_block
        from dalton_core.earnings_season_cli import consensus_reader_for

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = build_acn_core(Path(tmp.name))
        self.addCleanup(store.close)
        rows = period_consensus(store, ACN)
        q1 = [row for row in rows if row["period_end"] == "2026-11-30"]
        self.assertEqual({row["measure"] for row in q1}, {"eps", "revenue"})
        self.assertIn("调整后", next(r for r in q1 if r["measure"] == "eps")["metric"])
        block = consensus_block(ACN, "2026-11-30", reader=consensus_reader_for(store))
        self.assertTrue(block["available"], block.get("reason"))
        self.assertEqual({row["value"] for row in block["rows"]}, {"3.94689", "19411241500"})

    def test_model_targets_provider_gives_guidance_and_relabelled_consensus(self):
        from dalton_core.model_targets import forecast_model_targets

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = build_acn_core(Path(tmp.name))
        self.addCleanup(store.close)
        with patch("dalton_core.earnings_release_intake.core_state_dir", return_value=Path(tmp.name)):
            targets = forecast_model_targets(store.connection, ACN)
        guidance = [t for t in targets if t["source"] == "guidance"]
        self.assertEqual((guidance[0]["period_end"], guidance[0]["value"]), ("2026-11-30", "19275000000"))
        consensus = {t["period_end"]: t["value"] for t in targets if t["source"] == "consensus"}
        self.assertEqual(consensus["2026-11-30"], "19411241500")
        self.assertNotIn("2026-08-31", consensus)  # the stale FY2026Q4 label is gone

    def test_our_eps_reads_the_coverage_models_diluted_eps_row(self):
        ends = ["2026-11-30", "2027-02-28", "2027-05-31", "2027-08-31"]
        starts = ["2026-09-01", "2026-12-01", "2027-03-01", "2027-06-01"]
        payload = {"status": "ok", "period": {"forecast": ends}, "columns": [{"start": s, "end": e} for s,e in zip(starts,ends)], "rows": [
            {"key": "result:diluted_eps_numerator", "role": "diluted_eps_numerator", "label": "稀释每股收益分子",
             "unit_kind": "amount", "cells": [{"end": e, "value": "370"} for e in ends]},
            {"key": "result:shares", "role": "diluted_weighted_average_shares", "unit_kind": "shares", "cells": [{"end": e, "value": "100"} for e in ends]},
            {"key": "result:diluted_eps", "role": "diluted_eps", "label": "稀释每股收益",
             "unit_kind": "per_share", "cells": [{"end": e, "value": "3.7"} for e in ends]}]}
        got = our_eps(payload, ACN)
        self.assertEqual((got["status"], got["value"], got["row"]), ("available", "14.8", "稀释每股收益"))


class LongHistoryAndNtmTests(unittest.TestCase):
    """Owner review follow-up: NTM by fiscal quarters, honest percentile words,
    company-facts EPS history, broad-market benchmark."""

    def test_ntm_is_the_next_four_fiscal_quarters(self):
        whole = fp.ntm_aligned("2026-08-31", 8, quarterly={}, annual={"FY2027": "14.75"})
        self.assertEqual((whole["value"], whole["window"][0]), ("14.75", "FY2027Q1"))
        split = fp.ntm_aligned("2026-05-31", 8, quarterly={"FY2026Q4": "3.2"},
                               annual={"FY2027": "14.8"})
        # FY2026Q4 + three quarter-shares of FY2027.
        self.assertEqual(Decimal(split["value"]), Decimal("3.2") + Decimal("14.8") * 3 / 4)
        self.assertEqual(split["components"][1]["source"], "pro_rata_of_FY2027")
        missing = fp.ntm_aligned("2026-08-31", 8, quarterly={"FY2027Q1": "3.9"}, annual={})
        self.assertEqual(missing["status"], "unavailable")

    def test_percentile_words_need_three_years_and_two_windows(self):
        from dalton_core.valuation_history import percentile_reading

        self.assertEqual(percentile_reading("15", sample_size=250, windows=8)["label"], "样本不足")
        self.assertEqual(percentile_reading("15", sample_size=800, windows=1)["label"], "样本不足")
        self.assertEqual(percentile_reading("15", sample_size=800, windows=4)["label"], "历史低位")
        self.assertEqual(percentile_reading("85", sample_size=800, windows=4)["label"], "历史高位")
        self.assertEqual(percentile_reading("50", sample_size=800, windows=4)["label"], "历史中间区间")

    def test_company_facts_eps_history_preserves_disclosure_vintages(self):
        from dalton_core.valuation_history import company_facts, eps_points, long_pe, ttm_eps

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        store = build_acn_core(state, release=False, with_spool=False)
        self.addCleanup(store.close)
        facts = {"cik": 1467373, "entityName": "Accenture plc", "facts": {"us-gaap": {"EarningsPerShareDiluted": {
            "units": {"USD/shares": [
                {"start": "2023-09-01", "end": "2024-08-31", "val": 11.44, "filed": "2024-10-15", "form": "10-K"},
                {"start": "2023-09-01", "end": "2024-08-31", "val": 11.44, "filed": "2025-10-10", "form": "10-K"},
                {"start": "2024-09-01", "end": "2024-11-30", "val": 3.59, "filed": "2024-12-19", "form": "10-Q"},
                {"start": "2024-12-01", "end": "2025-02-28", "val": 2.82, "filed": "2025-03-20", "form": "10-Q"},
                {"start": "2025-03-01", "end": "2025-05-31", "val": 3.49, "filed": "2025-06-20", "form": "10-Q"},
                {"start": "2024-09-01", "end": "2025-05-31", "val": 9.90, "filed": "2025-06-20", "form": "10-Q"},
                {"start": "2024-09-01", "end": "2025-08-31", "val": 12.15, "filed": "2025-10-10", "form": "10-K"},
            ]}}}}}
        raw = json.dumps(facts).encode()
        digest = hashlib.sha256(raw).hexdigest()
        objects = state / "transcript-spool" / "connector-spool" / "objects" / digest[:2]
        objects.mkdir(parents=True)
        (objects / digest).write_bytes(raw)
        store.connection.execute(
            "CREATE TABLE connector_source_envelopes (source_envelope_id TEXT, connector_invocation_ref TEXT, "
            "connector_profile_ref TEXT, raw_artifact_version_ref TEXT, raw_response_hash TEXT, completeness TEXT, "
            "status TEXT, record_json TEXT, content_hash TEXT, created_at TEXT)")
        store.connection.execute("INSERT INTO connector_source_envelopes VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 ("e1", "i1", "connector-profile-template:sec:0.1", "a", digest, "c", "ok", "{}",
                                  "h", "2026-09-29T00:00:00+00:00"))
        held = company_facts(store.connection, state, ACN)
        self.assertEqual(held["raw_hash"], digest)
        points = eps_points(held["facts"])
        vintages = [p for p in points["eps_vintages"] if p["end"] == "2024-08-31"]
        self.assertEqual([p["filed"] for p in vintages], ["2024-10-15", "2025-10-10"])
        # Annual EPS and a nine-month weighted EPS do not supply standalone Q4 EPS.
        self.assertNotIn("2025-08-31", points["quarters"])
        ttm = {p["period_end"]: p for p in ttm_eps(points)}
        self.assertEqual(ttm["2025-08-31"]["value"], Decimal("12.15"))
        bars = [{"date": "2025-01-02", "close": "350"}, {"date": "2025-11-03", "close": "243"}]
        history = long_pe(store.connection, state, ACN, bars=bars)
        self.assertEqual(history["current"], "20")
        self.assertEqual(history["reading"]["label"], "样本不足")

    def test_benchmark_is_used_when_held_and_named_missing_when_not(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = build_acn_core(Path(tmp.name), release=False, with_spool=False)
        self.addCleanup(store.close)
        c = store.connection
        missing = market_block_from(c, ACN, as_of="2026-10-09")["relative_to_benchmark"]
        self.assertEqual(missing["status"], "unavailable")
        self.assertIn("SPY", missing["reason"])
        spy = price_record("market-proxy-series:yfinance:SPY", Decimal(500))
        spy["ticker"] = "SPY"
        c.execute("INSERT INTO market_price_series_versions VALUES (?,?,?,?,?)",
                  ("mps-spy", "market-proxy-series:yfinance:SPY", 1, json.dumps(spy), "SPY"))
        found = market_block_from(c, ACN, as_of="2026-10-09")["relative_to_benchmark"]
        self.assertEqual((found["status"], found["ticker"]), ("available", "SPY"))
        self.assertEqual(found["windows"]["ytd"]["status"], "available")
