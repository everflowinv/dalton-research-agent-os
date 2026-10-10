"""Synthetic disclosure vintages, weighted shares, fiscal gaps and paid cash."""
import unittest
from decimal import Decimal
from dalton_core.company_model_annual_projection import fiscal_groups
from dalton_core.return_attribution import FiscalCalendar, _ttm_points, _latest_point, decompose, dividend_cash_window
from dalton_core.valuation_history import eps_points, ttm_eps, pe_history
from tests.test_valuation_snapshot import ValuationTestCase, flow, roles


class PerShareEvidenceTests(unittest.TestCase):
    def row(self, start, end, semantics, income, shares, eps="999", filed="2026-08-01"):
        return {"start": start, "end": end, "semantics": semantics, "fiscal_label": "SYNTH",
                "available_on": filed, "net_income_to_common": {"value": str(income), "basis": "filed", "ref": "common"},
                "diluted_shares": {"value": str(shares), "ref": "shares"}, "basic_shares": {"value": str(shares), "ref": "basic"},
                "diluted_eps": {"value": eps, "ref": "eps"}, "basic_eps": {"value": eps, "ref": "eps"}}

    def test_ttm_rebuilds_income_over_integral_weighted_shares_not_eps_sum(self):
        rows = [self.row("2025-01-01", "2025-12-31", "annual", 400, 100),
                self.row("2025-01-01", "2025-06-30", "year_to_date", 150, 120),
                self.row("2026-01-01", "2026-06-30", "year_to_date", 250, 80),
                self.row("2026-04-01", "2026-06-30", "quarterly", 120, 70)]
        points = _ttm_points(rows, FiscalCalendar(["2025-12-31"]))
        point = next(p for p in points if p["end"] == "2026-06-30")
        weighted = (Decimal(100)*365 + Decimal(80)*181 - Decimal(120)*181)/365
        self.assertAlmostEqual(float(Decimal(point["diluted_shares"]["value"])), float(weighted), places=6)
        self.assertAlmostEqual(float(Decimal(point["diluted_eps"]["value"])), float(Decimal(500)/weighted), places=4)
        self.assertEqual(point["diluted_eps"]["basis"], "ttm_common_income_over_day_weighted_shares")
        self.assertIsNone(point["dps"])

    def test_annual_eps_preserves_filed_adjustments(self):
        row = self.row("2025-01-01", "2025-12-31", "annual", 400, 100, eps="3.97")
        point = _ttm_points([row], FiscalCalendar(["2025-12-31"]))[0]
        self.assertEqual(point["diluted_eps"]["value"], "3.97")
        self.assertEqual(point["diluted_eps"]["basis"], "filed_annual")

    def test_disclosure_cutoff_rejects_later_restatement_and_selects_old_vintage(self):
        series = {"ttm_vintages": [
            {"end": "2025-12-31", "available_on": "2026-02-10", "diluted_eps": {"value": "4"}},
            {"end": "2025-12-31", "available_on": "2026-08-01", "diluted_eps": {"value": "7"}}]}
        self.assertIsNone(_latest_point(series, on_or_before="2026-02-01"))
        self.assertEqual(_latest_point(series, on_or_before="2026-03-01")["diluted_eps"]["value"], "4")
        self.assertEqual(_latest_point(series, on_or_before="2026-08-01")["diluted_eps"]["value"], "7")

    def test_missing_dividend_is_distinct_from_confirmed_zero(self):
        self.assertIsNone(decompose({"price": Decimal("1.2")}, None)["total_return_percent"])
        self.assertEqual(decompose({"price": Decimal("1.2")}, Decimal(0))["total_return_percent"], "20.00")
        self.assertEqual(dividend_cash_window(None, "2025-01-01", "2026-01-01")["status"], "not_collected")
        evidence = {"company_ref": "company:ticker:synthetic", "coverage_start": "2024-01-01", "coverage_end": "2026-12-31",
                    "share_class_ref": "synthetic:CommonMember", "currency": "USD", "source_ref": "raw-sink:"+"a"*64,
                    "source_hash": "a"*64, "payments": []}
        self.assertEqual(dividend_cash_window(evidence, "2025-01-01", "2026-01-01")["status"], "confirmed_zero")
        evidence["payments"] = [{"payment_date": "2025-05-01", "cash_per_share": "0.5", "evidence_ref": "paid:1"},
                                {"payment_date": "2026-05-01", "cash_per_share": "0.8", "evidence_ref": "paid:2"}]
        self.assertEqual(dividend_cash_window(evidence, "2025-01-01", "2026-01-01")["cash_per_share"], "0.5")

    def test_complete_fiscal_grid_exposes_an_entire_missing_historical_year(self):
        groups = fiscal_groups(["2024-12-31", "2026-03-31", "2026-06-30"],
                               {"fiscal_year_end_month": 12}, {"2024-12-31", "2026-03-31"})
        label, periods = next(item for item in groups if item[0].startswith("FY2025"))
        self.assertEqual(label, "FY2025A (partial)")
        self.assertEqual(periods, ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
        legacy = fiscal_groups(["2024-12-31", "2026-03-31"], {"fiscal_year_end_month": 12},
                               {"2024-12-31"}, complete_grid=False)
        self.assertFalse(any(label.startswith("FY2025") for label, _ in legacy))

    def test_payment_authority_requires_exact_held_raw_and_is_append_only(self):
        import json, hashlib, tempfile, sqlite3
        from pathlib import Path
        from dalton_core.store import DaltonStore, canonical_json
        from dalton_core.raw_spool import RawSpool
        from dalton_core.return_attribution import ReturnAttributionAuthority, ReturnAttributionConflict, read_inputs
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = DaltonStore(str(root / "core.sqlite"))
            self.addCleanup(store.close)
            body = {"company_ref": "company:ticker:synthetic", "coverage_start": "2024-01-01", "coverage_end": "2026-12-31",
                    "share_class_ref": "synthetic:CommonMember", "currency": "USD", "payments": [
                        {"payment_date": "2025-05-01", "cash_per_share": "0.5", "evidence_ref": "paid:1"}]}
            raw = canonical_json(body).encode()
            spool = RawSpool(str(root / "raw-spool"), max_total_bytes=1000000)
            sink = spool.open_sink("raw-sink:" + hashlib.sha256(raw).hexdigest(), max_response_bytes=100000)
            sink.write(raw)
            artifact = sink.finalize()
            evidence = {**body, "source_ref": "raw-sink:" + artifact.content_hash, "source_hash": artifact.content_hash}
            authority = ReturnAttributionAuthority(store)
            record = authority.record_dividend_evidence(evidence, mission=None, actor_ref="human:synthetic", state_dir=root)
            self.assertEqual(authority.record_dividend_evidence(evidence, mission=None, actor_ref="human:synthetic", state_dir=root), record)
            self.assertEqual(read_inputs(store.connection, body["company_ref"])["dividend_payments"], evidence)
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute("DELETE FROM dividend_payment_evidence_versions")
            wrong = {**evidence, "payments": [{"payment_date": "2025-05-02", "cash_per_share": "0.5", "evidence_ref": "paid:1"}]}
            with self.assertRaises(ReturnAttributionConflict):
                authority.record_dividend_evidence(wrong, mission=None, actor_ref="human:synthetic", state_dir=root)

    def test_companyfacts_restated_eps_keeps_its_actual_availability_date(self):
        facts = {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 4, "filed": "2026-02-10"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 8, "filed": "2026-08-01"}]}}}}
        series = ttm_eps(eps_points(facts))
        old = pe_history([{"date": "2026-03-01", "close": "40"}], series)
        new = pe_history([{"date": "2026-09-01", "close": "40"}], series)
        self.assertEqual(old["current"], "10")
        self.assertEqual(new["current"], "5")
        self.assertEqual(len(series), 2)


class CoreValuationTests(ValuationTestCase):
    def test_core_pe_requires_disclosed_core_tax_and_keeps_gaap_separate(self):
        missing = self.metrics(self.publish())
        self.assertEqual(missing["trailing_pe"]["earnings_basis"], "gaap")
        self.assertEqual(missing["core_pe"]["status"], "unavailable")
        available = self.metrics(self.publish(windows=[{"as_of": "2025-12-31", "roles": roles(
            core_tax_expense=flow("1000000", concept="synthetic:CoreOperatingTaxExpense"))}]))
        self.assertEqual(available["core_pe"]["status"], "available")
        self.assertNotEqual(available["core_pe"]["value"], available["trailing_pe"]["value"])
        self.assertEqual(available["core_pe"]["earnings_basis"], "core_operating_after_tax")

    def test_historical_bars_before_first_share_disclosure_are_excluded(self):
        published = self.publish(share_history=[{"as_of": "2026-08-20", "shares_outstanding": "1000000"}])
        metric = self.metrics(published)["trailing_pe"]
        self.assertEqual(metric["sample_window"]["start"], "2026-08-20")
        self.assertLess(metric["sample_window"]["sample_size"], 40)
