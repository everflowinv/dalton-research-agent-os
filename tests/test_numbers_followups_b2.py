"""Batch B / al follow-ups (2026-10-09): one-off items read from the release,
the forecast and return legs on the relabelled consensus, the valuation
modules' reasons in the registry's words, and the segment bridge
「分部合计 + 公司层未分摊（CorporateNonSegment）+ AllOther = 集团」.

Fixtures: the real Accenture Q4 FY26 Exhibit 99.1 (no one-off in the
quarter) and a synthetic META-shaped exhibit (legal-related charges of
$2.40 billion, severance of $1.18 billion; invented figures).
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from dalton_core import economic_invariants as ei
from dalton_core import forecast_assumption_sources as fas
from dalton_core.earnings_release_outlook import (
    ONE_OFF_PARSER_VERSION, EarningsReleaseOneOffStore, one_off_items, parse_one_offs, plain_text,
)
from dalton_core.forecast_coverage_assumptions import normalise_base
from dalton_core.return_attribution import build_return_bridge, read_inputs
from dalton_core.store import DaltonStore
from dalton_core.terminology import banned_hits, label
from tests.test_economic_invariants import _COSTS, _GEOGRAPHY, _REVENUE, _SEGMENTS, _googl_q1_2024, _row
from tests.test_numbers_wiring import ACN, FIXTURE, _filing, _plain_tables, build_acn_core

META_FIXTURE = Path(__file__).parent / "fixtures" / "numbers" / "meta-shaped-q4-synthetic-ex991.htm"
META_SHA = hashlib.sha256(META_FIXTURE.read_bytes()).hexdigest()
SYNTH = "company:sec-cik:0009999901"
Q4_2025 = {"kind": "quarter", "fiscal_year": 2025, "fiscal_quarter": 4}


def build_meta_core(state: Path) -> DaltonStore:
    """A Core holding one META-shaped release read and its exhibit in the spool."""

    state.mkdir(parents=True, exist_ok=True)
    store = DaltonStore(str(state / "core.sqlite"))
    c = store.connection
    _plain_tables(c)
    _filing(c, "ing-k24", "10-K", "2025-01-30", "2024-12-31", company=SYNTH,
            accession="0009999901-25-000001")
    record = {"accession": "0009999901-26-000002", "body_sha256": META_SHA, "company_ref": SYNTH,
              "period": "FY2025-Q4", "report_date": "2026-01-28", "status": "parsed",
              "id": "earnings-release-read:synth", "exhibit_url": "https://example.invalid/ex991.htm"}
    c.execute("INSERT INTO earnings_release_reads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              ("earnings-release-read:synth", SYNTH, record["accession"], "parsed", None, "FY2025-Q4",
               record["exhibit_url"], META_SHA, 4, json.dumps(record), "0" * 64,
               "2026-01-28T22:00:00+00:00"))
    objects = state / "transcript-spool" / "connector-spool" / "objects" / META_SHA[:2]
    objects.mkdir(parents=True, exist_ok=True)
    (objects / META_SHA).write_bytes(META_FIXTURE.read_bytes())
    return store


class OneOffParserTests(unittest.TestCase):
    def test_meta_shaped_release_names_legal_and_severance(self):
        text = plain_text(META_FIXTURE.read_bytes())
        items = parse_one_offs(text, fye_month=12, release_period=Q4_2025)
        by_kind = {item["kind"]: item for item in items}
        self.assertEqual(sorted(by_kind), ["legal", "restructuring"])
        legal, severance = by_kind["legal"], by_kind["restructuring"]
        self.assertEqual((legal["amount"], legal["scale"], legal["unit"]), ("2.40", "billion", "currency"))
        self.assertEqual(legal["concept"], "us-gaap:GeneralAndAdministrativeExpense")
        self.assertEqual(legal["concept_basis"], "stated_line")
        self.assertEqual((severance["amount"], severance["scale"]), ("1.18", "billion"))
        for item in items:
            self.assertEqual((item["period"], item["period_end"]), ("FY2025Q4", "2025-12-31"))
            start, end = item["span"]
            self.assertEqual(text[start:end], item["quote"])
            self.assertEqual(item["sentence"], item["quote"])
            self.assertEqual(item["quote_sha256"], hashlib.sha256(item["quote"].encode()).hexdigest())
            self.assertIn(item["amount"], item["quote"])
        # The table row (1,180 in millions) is the same severance: kept once.
        self.assertEqual(len([i for i in items if i["kind"] == "restructuring"]), 1)
        # The outlook's "we expect restructuring charges of $0.3 billion" is not a one-off held.
        self.assertFalse(any("expect" in item["quote"] for item in items))

    def test_acn_q4_fy26_release_has_no_one_off_in_the_quarter(self):
        text = plain_text(FIXTURE.read_bytes())
        items = parse_one_offs(text, fye_month=8,
                               release_period={"kind": "quarter", "fiscal_year": 2026, "fiscal_quarter": 4})
        # "$0.78 for business optimization costs" is per share; the table's quarter column is "—".
        self.assertEqual(items, [])


class OneOffHookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_meta_shaped_items_reach_the_forecast_sources_by_name(self):
        store = build_meta_core(self.state)
        self.addCleanup(store.close)
        raw = one_off_items(store.connection, SYNTH)
        self.assertEqual(len(raw), 2)
        self.assertTrue(all(item["sha256"] == META_SHA for item in raw))
        self.assertEqual(one_off_items(store.connection, SYNTH, "FY2025Q4"), raw)
        self.assertEqual(one_off_items(store.connection, SYNTH, "FY2025Q3"), [])
        items, notes = fas.one_off_items(store.connection, SYNTH)
        self.assertIn("dalton_core.earnings_release_outlook.one_off_items", notes[0])
        by_kind = {item["kind"]: item for item in items}
        self.assertEqual(Decimal(by_kind["legal"]["amount"]), Decimal("2400000000"))
        self.assertEqual(Decimal(by_kind["restructuring"]["amount"]), Decimal("1180000000"))
        self.assertEqual(by_kind["legal"]["scale"], "one")
        self.assertEqual(by_kind["legal"]["ref"]["kind"], "figure")
        self.assertEqual(by_kind["legal"]["ref"]["accession"], "0009999901-26-000002")
        self.assertIn("legal-related charges of $2.40 billion", by_kind["legal"]["because"])
        # normalise_base takes the legal charge out of its quarter.
        out, records = normalise_base(
            {"2025-09-30": Decimal("1200000000"), "2025-12-31": Decimal("3650000000")},
            flagged=False, explicit=[by_kind["legal"]])
        self.assertEqual(out["2025-12-31"], Decimal("1250000000"))
        self.assertEqual(records[0]["basis"], "explicit_one_off")

    def test_the_item_shape_normalises_the_synthetic_company_g_and_a(self):
        from tests.synthetic_company import company
        from tests.test_p0_error_chains import assumptions_for, build, stored

        store = build_meta_core(self.state)
        self.addCleanup(store.close)
        items, _notes = fas.one_off_items(store.connection, SYNTH)
        legal = next(item for item in items if item["kind"] == "legal")
        # Synthetic Widgets' latest quarter carries the same 2.4bn accrual.
        legal = {**legal, "period_end": "2026-06-30",
                 "ref": {**legal["ref"], "period_end": "2026-06-30"}}
        body = build(company(gna_spike="2400000000"),
                     {"one_offs": {"concepts": [], "items": [legal]}})
        gna = assumptions_for(body, "us-gaap:GeneralAndAdministrativeExpense")
        self.assertIn("explicit_one_off", gna[0]["because"])
        self.assertIn("legal-related charges", gna[0]["because"])
        self.assertLess(Decimal(gna[0]["value"]), Decimal("0.02"))
        stored(body)

    def test_acn_hookup_is_empty(self):
        store = build_acn_core(self.state)
        self.addCleanup(store.close)
        self.assertEqual(one_off_items(store.connection, ACN), [])
        items, notes = fas.one_off_items(store.connection, ACN)
        self.assertEqual(items, [])
        self.assertIn("没有可读的一次性项目", notes[0])

    def test_store_is_append_only_and_read_back(self):
        store = build_meta_core(self.state)
        self.addCleanup(store.close)
        lane = EarningsReleaseOneOffStore(store)
        first = lane.ingest_company(self.state, SYNTH)
        second = lane.ingest_company(self.state, SYNTH)
        self.assertEqual([r["status"] for r in first], ["recorded"])
        self.assertEqual([r["status"] for r in second], ["duplicate"])
        row = store.connection.execute(
            "SELECT parser_version, item_count FROM earnings_release_one_offs").fetchone()
        self.assertEqual(tuple(row), (ONE_OFF_PARSER_VERSION, 2))
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("UPDATE earnings_release_one_offs SET item_count=0")
        with self.assertRaises(sqlite3.DatabaseError):
            store.connection.execute("DELETE FROM earnings_release_one_offs")
        items = one_off_items(store.connection, SYNTH, state_dir=self.state / "elsewhere")
        self.assertEqual(len(items), 2)  # from the stored record, no spool needed


class RelabelledConsensusHookupTests(unittest.TestCase):
    FILED = {"2025-08-31": Decimal("17596260000"), "2025-11-30": Decimal("18742125000"),
             "2026-02-28": Decimal("18044066000"), "2026-05-31": Decimal("18718144000")}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_acn_core(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.c = self.store.connection

    def test_forecast_targets_use_fy27q1_not_the_stored_fy26q4(self):
        targets, notes = fas.held_consensus_targets(self.c, ACN, self.FILED)
        self.assertIn("2026-11-30", targets)  # FY2027Q1
        self.assertNotIn("2026-08-31", targets)
        first = targets["2026-11-30"]
        self.assertEqual(first["value"], "19411241500")
        self.assertEqual((first["label"], first["original_label"]), ("FY2027Q1", "FY2026Q4"))
        self.assertIn("relabelled from FY2026Q4", first["because"])
        self.assertEqual(targets["2027-02-28"]["label"], "FY2027Q2")
        self.assertTrue(any("FY2026Q4->FY2027Q1" in note for note in notes))

    def test_without_the_relabel_the_stored_labels_lose_the_quarter(self):
        with patch.object(fas, "relabelled_consensus", return_value=None):
            targets, notes = fas.held_consensus_targets(self.c, ACN, self.FILED)
        self.assertNotIn("2026-11-30", targets)
        self.assertTrue(any("沿用存储的财期标签" in note for note in notes))

    def _snapshot(self):
        return {"id": "valuation-snapshot-version:acn:1", "metrics": [],
                "price": {"close": "207.28", "bar_date": "2026-10-08", "version_ref": "price:acn"}}

    def _series(self):
        return {"ttm": [{"end": "2026-05-31", "rule": "annual", "diluted_eps": {"value": "12.2"},
                         "dps": {"value": "5.92"}}]}

    def test_target_leg_uses_the_next_four_relabelled_quarters(self):
        inputs = read_inputs(self.c, ACN)
        self.assertIsNotNone(inputs["consensus_relabel"])
        bridge = build_return_bridge(ACN, series=self._series(), series_ref=None,
                                     snapshot=self._snapshot(), price=None, model=None,
                                     consensus=inputs["consensus"],
                                     consensus_relabel=inputs["consensus_relabel"])
        leg = next(item for item in bridge["legs"] if item["leg"] == "expected_consensus")
        self.assertEqual(leg["status"], "available")
        self.assertEqual(leg["eps_window_basis"], "next_four_fiscal_quarters")
        self.assertEqual(leg["eps_end"]["window"], ["FY2027Q1", "FY2027Q2", "FY2027Q3", "FY2027Q4"])
        self.assertEqual(Decimal(leg["eps_end"]["value"]), Decimal("15.90406"))
        self.assertEqual(Decimal(leg["eps_start"]["value"]), Decimal("13.97"))
        self.assertEqual(leg["window"], {"start": "2026-10-08", "end": "2027-10-08"})
        self.assertEqual(leg["inputs"]["consensus_periods"], "relabelled")
        self.assertIn("12 个月窗口一致", leg["notes"][1])
        growth = Decimal(leg["factors"]["earnings_growth"])
        self.assertAlmostEqual(float(growth), float(Decimal("15.90406") / Decimal("13.97")), places=5)

    def test_stored_labels_put_the_window_a_quarter_early(self):
        inputs = read_inputs(self.c, ACN)
        bridge = build_return_bridge(ACN, series=self._series(), series_ref=None,
                                     snapshot=self._snapshot(), price=None, model=None,
                                     consensus=inputs["consensus"])
        leg = next(item for item in bridge["legs"] if item["leg"] == "expected_consensus")
        # Without batch B's calendar the last reported quarter is still
        # 2026-05-31: the window would open on the already-reported FY2026Q4.
        self.assertEqual(leg["eps_end"]["window"][0], "FY2026Q4")
        self.assertEqual(leg["inputs"]["consensus_periods"], "stored")


class ValuationWordingTests(unittest.TestCase):
    def test_unconfigured_lane_says_it_in_the_registry_words(self):
        from dalton_core.valuation_snapshot_lane import dispatch

        class Server:
            def lane_launcher(self, _kwarg):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertEqual(result["reason"], label("valuation_reason", "service_unconfigured"))
        self.assertNotIn("writer", result["reason"])
        self.assertEqual(banned_hits(result["reason"]), [])

    def test_missing_line_reasons_name_the_line_in_chinese(self):
        from dalton_core.valuation_snapshot import compute_metrics

        totals = {"revenue": Decimal("100"), "net_income": Decimal("-5")}
        metrics, basis = compute_metrics(
            close=Decimal("10"), shares=Decimal("10"), as_of="2026-10-08",
            windows=[{"as_of": "2026-06-30", "_totals": totals}], bars=[])
        reasons = {item["metric"]: item["reason"] for item in metrics}
        self.assertIn("「营业利润」", reasons["ev_to_ebitda"])
        self.assertIn("「经营活动现金流净额」", reasons["fcf_yield"])
        self.assertIn("净利润不为正", reasons["trailing_pe"])
        self.assertIn("「有息债务合计」", basis["enterprise_value_reason"])
        for text in [*reasons.values(), basis["enterprise_value_reason"]]:
            if text:
                self.assertNotRegex(text.replace("EBITDA", ""), r"[A-Za-z]{4,}")


def _segment_income(*values, start="2024-01-01", end="2024-03-31", members=None):
    rows = [_row("us-gaap:OperatingIncomeLoss", value, "srt:ConsolidationItemsAxis",
                 "us-gaap:OperatingSegmentsMember", start=start, end=end, dimension_count=2)
            for value in values]
    for row, member in zip(rows, members or ()):
        row["dimensions"] = {_SEGMENTS: member, "srt:ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}
    return rows


def _widgets(*, a_cost="300", corporate="-20"):
    """Synthetic Widgets Co.: two segments, AllOther revenue, corporate costs."""

    revenue = "us-gaap:Revenues"
    # A 500-300=200, B 250-150=100, AllOther 40 revenue / 70 costs = -30,
    # corporate revenue 5 / costs 25 = -20; group: revenue 795, costs 545, OI 250.
    return [
        _row(revenue, "795"),
        _row(revenue, "500", _SEGMENTS, "x:AMember"),
        _row(revenue, "250", _SEGMENTS, "x:BMember"),
        _row(revenue, "40", _SEGMENTS, "us-gaap:AllOtherSegmentsMember"),
        _row(_COSTS, "545"),
        _row(_COSTS, a_cost, _SEGMENTS, "x:AMember"),
        _row(_COSTS, "150", _SEGMENTS, "x:BMember"),
        _row("us-gaap:OperatingIncomeLoss", "250"),
        _row("us-gaap:OperatingIncomeLoss", corporate, "srt:ConsolidationItemsAxis",
             "us-gaap:CorporateNonSegmentMember"),
        *_segment_income("200", "100", "-30", members=("x:AMember", "x:BMember", "us-gaap:AllOtherSegmentsMember")),
    ]


class SegmentBridgeTests(unittest.TestCase):
    def verdict(self, rows):
        return ei._segment_sum(ei.segment_groups(rows))

    def test_anonymous_legacy_income_cannot_be_assigned_by_matching_amounts(self):
        rows = _widgets()
        for row in rows:
            row.pop("dimensions", None)
        verdict = self.verdict(rows)
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("lack segment identity", verdict.findings[0])
        self.assertIn("missing operating income members", verdict.findings[0])

    def test_without_segment_income_the_missing_members_fail(self):
        rows = [row for row in _widgets() if row.get("dimension_count") != 2]
        verdict = self.verdict(rows)
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("missing operating income members", verdict.findings[0])

    def test_synthetic_company_bridge_closes(self):
        groups = ei.segment_groups(_widgets())
        verdict = ei._segment_sum(groups)
        self.assertEqual((verdict.status, verdict.checked), (ei.PASS, 3))
        bridge = ei.segment_bridge(groups[0])
        self.assertEqual(bridge["derived_all_other_costs"], {"us-gaap:AllOtherSegmentsMember": "70"})
        self.assertEqual(bridge["segment_operating_income_matched"], {"x:AMember": "200", "x:BMember": "100"})

    def test_a_segment_cost_that_does_not_match_its_income_fails(self):
        verdict = self.verdict(_widgets(a_cost="310"))
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("differs from its operating income by -10", verdict.findings[0])

    def test_a_corporate_amount_that_does_not_close_fails(self):
        # Pair detection still works (845-595 = 250) but the segment income +
        # corporate (-25) no longer adds to the group's 250.
        verdict = self.verdict(_widgets(corporate="-25"))
        self.assertEqual(verdict.status, ei.FAIL)

    def test_swapped_member_income_fails_even_though_values_still_match_some_margin(self):
        rows = _widgets()
        incomes = [r for r in rows if r.get("dimensions")]
        incomes[0]["value"], incomes[1]["value"] = incomes[1]["value"], incomes[0]["value"]
        verdict = self.verdict(rows)
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("member x:AMember", verdict.findings[0])
        self.assertIn("by 100", verdict.findings[0])

    def test_equal_income_amounts_use_identity_without_ranking(self):
        rows = _widgets()
        for row in rows:
            if row.get("concept") == _COSTS and row.get("dimension_member") == "x:AMember":
                row["value"] = "400"
            if row.get("dimensions") and row["dimensions"][_SEGMENTS] == "x:AMember":
                row["value"] = "100"
            if row.get("concept") == _COSTS and not row.get("dimension_axis"):
                row["value"] = "645"
            if row.get("concept") == "us-gaap:OperatingIncomeLoss" and not row.get("dimension_axis"):
                row["value"] = "150"
        self.assertEqual(self.verdict(rows).status, ei.PASS)

    def test_extra_dimension_slice_is_not_added_to_segment_income(self):
        rows = _widgets()
        sliced = dict(next(row for row in rows if row.get("dimensions")))
        sliced["dimensions"] = {**sliced["dimensions"], "srt:StatementGeographicalAxis": "x:NorthMember"}
        sliced["dimension_count"] = 3
        rows.append(sliced)
        self.assertEqual(self.verdict(rows).status, ei.PASS)

    def test_single_missing_cost_member_is_named_and_fails(self):
        rows = [row for row in _widgets() if not (row["concept"] == _COSTS and row["dimension_member"] == "x:BMember")]
        verdict = self.verdict(rows)
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertIn("missing cost member: x:BMember", verdict.findings[0])

    def test_filed_rounding_inside_relative_tolerance_passes_and_outside_fails(self):
        for delta, status in (("0.0001", ei.PASS), ("0.02", ei.FAIL)):
            rows = _widgets()
            for row in rows:
                row["value"] = str(Decimal(row["value"]) * Decimal("1000000000"))
                if row.get("dimensions") and row["dimensions"][_SEGMENTS] == "x:AMember":
                    row["value"] = str(Decimal(row["value"]) + Decimal(delta) * Decimal("1000000000"))
            self.assertEqual(self.verdict(rows).status, status)

    def test_a_different_axis_with_same_partial_sum_is_not_accepted_by_value(self):
        rows = _widgets() + [_row("us-gaap:Revenues", "790", _GEOGRAPHY, "x:NorthMember")]
        verdict = self.verdict(rows)
        self.assertEqual(verdict.status, ei.FAIL)
        self.assertTrue(any(_GEOGRAPHY in finding for finding in verdict.findings))

    def test_a_breakdown_that_closes_alone_is_untouched(self):
        rows = [_row(_REVENUE, "100"), _row(_REVENUE, "60", _GEOGRAPHY, "x:N"),
                _row(_REVENUE, "40", _GEOGRAPHY, "x:S")]
        self.assertEqual(self.verdict(rows).status, ei.PASS)
        self.assertNotIn("bridge", ei.segment_groups(rows)[0])

    def test_the_contract_names_the_bridge(self):
        contract = ei.FORECAST_INVARIANT_CONTRACT["segment_sum"]
        self.assertTrue(contract["segment_bridge"].startswith("segments+all-other+corporate-non-segment"))
        self.assertEqual(contract["all_other_members"], ["us-gaap:AllOtherSegmentsMember"])
        self.assertEqual(ei.FORECAST_INVARIANT_CONTRACT_REF, "forecast-economic-invariants:11")


if __name__ == "__main__":
    unittest.main()
