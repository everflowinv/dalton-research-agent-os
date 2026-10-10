"""F10: the first producer of market-proxy claims -- catalog rules over declared drivers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_index_authority import ClaimIndexAuthority
from dalton_core.claim_index_cli import record_tagged
from dalton_core.claim_index_tagging import evidence_kind_for
from dalton_core.market_price import MarketPriceSeriesAuthority, _TICKER_RE
from dalton_core.market_proxy_catalog import (
    AUTO_PREFIX, MAX_DERIVED_TICKERS, RULES, derive_mappings, mapping_for,
    matched_rules, merge_mappings,
)
from dalton_core.market_proxy_claim import (
    MarketProxyClaimAuthority, MarketProxyClaimError, load_mapping_config, validate_mapping,
)
from dalton_core.mission_market_price_lane import MissionMarketPriceLaneCoordinator
from dalton_core.store import DaltonStore, content_hash
from tests.test_claim_index_entries import LedgerFixture, entry_args

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"
DXC = "company:sec-cik:001688568"
GOOGL = "company:ticker:googl"

# The live shapes (2026-10-07, read-only): ref + label, never the prose.
ACN_SPEC = {"revenue_drivers": [
    {"kind": "contract_book", "ref": "managed-services-book", "label": "Managed Services contract book"},
    {"kind": "mix", "ref": "geographic-mix", "label": "Americas/EMEA/Asia Pacific mix and FX"},
], "expense_lines": [
    {"ref": "cost-of-services", "label": "Cost of services", "cost_driver_slot": "delivery_cost"},
]}
DXC_SPEC = {"revenue_drivers": [
    {"kind": "contract_book", "ref": "legacy-runoff", "label": "Legacy infrastructure contract run-off"},
    {"kind": "mix", "ref": "geo-mix", "label": "Geographic mix, mostly Europe"},
], "expense_lines": [
    {"ref": "interest-expense", "label": "Interest expense", "cost_driver_slot": None},
]}
GOOGL_SPEC = {"revenue_drivers": [
    {"kind": "contract_book", "ref": "cloud", "label": "Google Cloud"},
    {"kind": "external", "ref": "fx", "label": "Foreign exchange on non-US revenue"},
], "expense_lines": []}
COMMODITY_SPEC = {"revenue_drivers": [], "expense_lines": [
    {"ref": "cogs", "label": "Cost of goods sold", "cost_driver_slot": "raw_material_spread"},
    {"ref": "utilities", "label": "Plant utilities", "cost_driver_slot": "energy_intensity"},
]}


class CatalogRuleTests(unittest.TestCase):
    def test_every_catalog_ticker_is_one_the_approved_connector_accepts(self):
        for rule in RULES:
            self.assertIsNotNone(_TICKER_RE.fullmatch(rule["ticker"]), rule["rule_id"])
            validate_mapping(mapping_for(rule["rule_id"], ACN))

    def test_declared_drivers_select_their_proxies(self):
        self.assertEqual(matched_rules(ACN_SPEC), ["usd_broad", "eur_usd"])
        self.assertEqual(matched_rules(DXC_SPEC), ["usd_broad", "eur_usd", "us_rates"])
        self.assertEqual(matched_rules(GOOGL_SPEC), ["usd_broad", "semis_capex"])
        self.assertEqual(matched_rules(COMMODITY_SPEC), ["energy_cost", "broad_commodities"])

    def test_legacy_infrastructure_run_off_is_not_hardware_demand(self):
        self.assertNotIn("semis_capex", matched_rules(DXC_SPEC))

    def test_mapping_identity_never_reads_model_prose(self):
        renamed = {"revenue_drivers": [{"kind": "mix", "ref": "regions",
                                        "label": "EMEA and currency", "because": "anything"}]}
        self.assertEqual(mapping_for("eur_usd", ACN), mapping_for("eur_usd", ACN))
        self.assertIn("eur_usd", matched_rules(renamed))
        self.assertTrue(mapping_for("eur_usd", ACN)["mapping_ref"].startswith(AUTO_PREFIX))

    def test_explicit_owner_mapping_wins_over_a_derived_one(self):
        derived = mapping_for("usd_broad", ACN)
        explicit = {**derived, "proxy_gap": "Owner reviewed gap for the dollar proxy."}
        merged = merge_mappings([explicit], [derived, mapping_for("eur_usd", ACN)])
        self.assertEqual([item["proxy_gap"] for item in merged][0], explicit["proxy_gap"])
        self.assertEqual(len(merged), 2)

    def test_config_switch_and_closed_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.json"
            path.write_text('{"schema_version":"0.1","mappings":[]}', encoding="utf-8")
            self.assertTrue(load_mapping_config(path)["derive_from_model_drivers"])
            path.write_text('{"schema_version":"0.1","mappings":[],'
                            '"derive_from_model_drivers":false}', encoding="utf-8")
            self.assertFalse(load_mapping_config(path)["derive_from_model_drivers"])
            path.write_text('{"schema_version":"0.1","mappings":[],"extra":1}', encoding="utf-8")
            with self.assertRaises(MarketProxyClaimError):
                load_mapping_config(path)
        self.assertTrue(load_mapping_config(None)["derive_from_model_drivers"])


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = DaltonStore(str(Path(self.tmp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS coverage_mission_company_model_specs("
            "company_ref TEXT, revenue_drivers_json TEXT, expense_lines_json TEXT, created_at TEXT)")
        MarketProxyClaimAuthority(self.store)

    def spec(self, company_ref, spec, created_at="2026-09-29T00:00:00+00:00"):
        self.store.connection.execute(
            "INSERT INTO coverage_mission_company_model_specs VALUES(?,?,?,?)",
            (company_ref, json.dumps(spec["revenue_drivers"]),
             json.dumps(spec["expense_lines"]), created_at))

    def test_specs_without_drivers_or_companies_without_specs_derive_nothing(self):
        self.assertEqual(derive_mappings(self.store.connection, [ACN, IBM]), [])

    def test_latest_spec_per_covered_company_and_bounded_tickers(self):
        self.spec(ACN, {"revenue_drivers": [], "expense_lines": []}, "2026-09-01T00:00:00+00:00")
        self.spec(ACN, ACN_SPEC)
        self.spec(DXC, DXC_SPEC)
        self.spec(GOOGL, COMMODITY_SPEC)  # outside the universe below: ignored
        mappings = derive_mappings(self.store.connection, [ACN, DXC])
        self.assertEqual(sorted({m["source_ticker"] for m in mappings}), ["FXE", "IEF", "UUP"])
        self.assertEqual({m["target_subject_ref"] for m in mappings}, {ACN, DXC})
        self.assertLessEqual(len({m["source_ticker"] for m in mappings}), MAX_DERIVED_TICKERS)

    def test_a_recorded_auto_mapping_is_sticky_after_a_respecification(self):
        self.spec(ACN, ACN_SPEC)
        prices = MarketPriceSeriesAuthority(self.store)
        prices.publish_series(
            company_ref="market-proxy-series:yfinance:UUP", ticker="UUP", currency="USD",
            bars=[{"date": "2026-09-01", "open": "27", "high": "27", "low": "27",
                   "close": "27", "adj_close": "27", "volume": "1"}],
            observations=[], invocation_ref="connector:yfinance:uup", artifact_hash="1" * 64,
            governance_ref="governance:yfinance", governance_hash="2" * 64,
            requested_start="2026-09-01", requested_end="2026-09-02",
            captured_at="2026-09-01T23:30:00+00:00")
        result = MarketProxyClaimAuthority(self.store).refresh(mapping_for("usd_broad", ACN))
        self.assertEqual(result["status"], "fresh")
        self.spec(ACN, {"revenue_drivers": [], "expense_lines": []}, "2026-10-01T00:00:00+00:00")
        refs = [m["mapping_ref"] for m in derive_mappings(self.store.connection, [ACN])]
        self.assertEqual(refs, [mapping_for("usd_broad", ACN)["mapping_ref"]])


class ModelStateGateTests(unittest.TestCase):
    setUp = DerivationTests.setUp
    spec = DerivationTests.spec

    def publish(self, ticker):
        MarketPriceSeriesAuthority(self.store).publish_series(
            company_ref="market-proxy-series:yfinance:" + ticker, ticker=ticker, currency="USD",
            bars=[{"date": "2026-09-01", "open": "27", "high": "27", "low": "27",
                   "close": "27", "adj_close": "27", "volume": "1"}],
            observations=[], invocation_ref="connector:yfinance:" + ticker.lower(),
            artifact_hash="1" * 64, governance_ref="governance:yfinance",
            governance_hash="2" * 64, requested_start="2026-09-01",
            requested_end="2026-09-02", captured_at="2026-09-01T23:30:00+00:00")

    def proxies_in_state(self):
        from dalton_core.company_model_state import build_company_model_state

        connection = self.store.connection

        class Missions:
            def __init__(inner): inner.connection = connection
            def statement_filings(inner, company_ref):
                return [{"company_ref": company_ref, "report_date": "2026-06-30",
                         "accession": "fixture", "entity_name": "Fixture",
                         "cik": "1467373", "form": "10-Q", "filed": "2026-07-01",
                         "line_count": 1, "ingest_id": "filing:1",
                         "content_hash": content_hash({"filing": "fixture"}),
                         "source_record_refs": ["raw-sink:" + "a" * 64]}]
            def statement_lines(inner, ingest_id):
                return [{"line_id": "filing:1#0", "ingest_id": "filing:1",
                         "ordinal": 0, "statement": "income", "concept": "Revenue",
                         "label": "Revenue", "level": 0, "parent_concept": None,
                         "is_breakdown": False, "dimension_axis": None,
                         "dimension_member": None, "dimension_count": None,
                         "unit": "USD", "period_start": "2026-04-01",
                         "period_end": "2026-06-30", "value": "1000",
                         "balance": "credit"}]
        state = build_company_model_state(Missions(), ACN)
        return [item["mapping_ref"] for item in state.get("market_proxies") or ()]

    def test_catalog_proxies_enter_the_specification_together(self):
        self.spec(ACN, ACN_SPEC)
        authority = MarketProxyClaimAuthority(self.store)
        self.publish("UUP")
        authority.refresh(mapping_for("usd_broad", ACN))
        held = self.proxies_in_state()
        self.publish("FXE")
        authority.refresh(mapping_for("eur_usd", ACN))
        together = self.proxies_in_state()
        self.assertEqual(held, [])
        self.assertEqual(sorted(together), sorted([mapping_for("usd_broad", ACN)["mapping_ref"],
                                                   mapping_for("eur_usd", ACN)["mapping_ref"]]))


class LaneDerivationTests(unittest.TestCase):
    def test_derived_mapping_adds_its_series_to_price_acquisition(self):
        class Prices:
            def latest_version(self, company_ref): return None
        class Launcher:
            def __init__(self): self.started = []
            def start(self, **kwargs):
                self.started.append(kwargs); return {"id": "ticket:proxy"}
        class Proxies:
            def __init__(self): self.seen = None
            def refresh_all(self, mappings):
                self.seen = [m["mapping_ref"] for m in mappings]
                return {"status": "settled", "results": []}
        mission = {"autonomy": {"may_write": ["market_price", "claim", "evidence", "claim_index"]},
                   "universe": [{"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P1"}]}
        launcher, proxies = Launcher(), Proxies()
        lane = MissionMarketPriceLaneCoordinator(
            authority=Prices(), launcher=launcher, mission=lambda: mission,
            proxy_mappings=[], proxy_authority=proxies,
            derived_proxy_mappings=lambda current: [mapping_for("usd_broad", ACN)])
        first = lane.dispatch_once()
        self.assertEqual((first["status"], first["company_ref"]), ("launched", ACN))
        self.assertEqual(proxies.seen, [mapping_for("usd_broad", ACN)["mapping_ref"]])
        lane._open = None
        lane._satisfied[ACN] = lane.clock()
        second = lane.dispatch_once()
        self.assertEqual((second["status"], second["ticker"]), ("launched", "UUP"))
        self.assertEqual(mission["universe"][0]["company_ref"], ACN)
        self.assertEqual(len(mission["universe"]), 1)

    def test_a_failing_rule_source_never_stops_prices(self):
        class Prices:
            def latest_version(self, company_ref): return None
        class Launcher:
            def start(self, **kwargs): return {"id": "ticket:x"}
        def broken(current): raise RuntimeError("no specs table")
        mission = {"autonomy": {"may_write": ["market_price"]},
                   "universe": [{"company_ref": ACN, "ticker": "ACN"}]}
        lane = MissionMarketPriceLaneCoordinator(
            authority=Prices(), launcher=Launcher(), mission=lambda: mission,
            derived_proxy_mappings=broken)
        self.assertEqual(lane.dispatch_once()["status"], "launched")


class EvidenceKindRuleTests(unittest.TestCase):
    def test_market_quantities_are_market_proxy_and_company_figures_are_not(self):
        proxy = [
            {"normalized_statement": "Brent averaged $82 in the quarter."},
            {"normalized_statement": "The MDI-benzene price spread widened 12% in August."},
            {"metric_or_aspect": "spot price", "normalized_statement": "HRC at $780/t"},
            {"normalized_statement": "行业开工率回落至 68%"},
            {"basis": "market_proxy", "normalized_statement": "anything"},
        ]
        statement = [
            {"normalized_statement": "Revenue grew 7% in local currency."},
            {"normalized_statement": "We raised list prices for software by 5%."},
            {"normalized_statement": "Bookings were spread across all regions."},
            {"normalized_statement": "The contract price was fixed for three years."},
        ]
        self.assertEqual({evidence_kind_for(item) for item in proxy}, {"market_proxy"})
        self.assertEqual({evidence_kind_for(item) for item in statement}, {"statement"})

    def test_rule_tagged_entries_carry_the_evidence_kind(self):
        fixture = LedgerFixture(); self.addCleanup(fixture.store.close)
        claim = fixture.add_claim("brent-proxy")
        args = entry_args(claim)
        row = {
            "claim_version_ref": args["claim_version_ref"],
            "claim_version_hash": args["claim_version_hash"],
            "claim_ref": args["claim_ref"], "claim_created_at": args["claim_created_at"],
            "subject_ref": args["subject_ref"], "metric_or_aspect": "oil price",
            "claim_kind": args["claim_kind"],
            "normalized_statement": "Brent futures prices fell to $70.",
            "tags": {key: args[key] for key in (
                "period_key", "aspect", "aspect_source", "as_of", "as_of_basis", "importance",
                "importance_basis", "dedupe_group_key", "tagger_ref", "tagger_hash")},
        }
        authority = ClaimIndexAuthority(fixture.store)
        counts = record_tagged(authority, [row], actor_ref=args["actor_ref"],
                               created_at=args["created_at"])
        self.assertEqual(counts["fresh"], 1)
        entry = authority.current_entry(args["claim_version_ref"])
        self.assertEqual(entry["evidence_kind"], "market_proxy")


if __name__ == "__main__":
    unittest.main()
