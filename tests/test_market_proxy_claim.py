import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_index_authority import ClaimIndexAuthority
from dalton_core.company_model_state import build_company_model_state
from dalton_core.company_dossier_cli import guidance_material
from dalton_core.company_model_spec import build_prompt
from dalton_core.market_price import MarketPriceSeriesAuthority
from dalton_core.market_proxy_claim import (
    MarketProxyClaimAuthority, MarketProxyClaimError, load_mappings,
)
from dalton_core.mission_market_price_lane import MissionMarketPriceLaneCoordinator
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.test_claim_index_entries import LedgerFixture, entry_args

SOURCE = "market-proxy-series:yfinance:XLE"
TARGET = "company:sec-cik:0001467373"
MAPPING = {
    "mapping_ref": "proxy-map:energy-etf",
    "source_series_company_ref": SOURCE,
    "source_ticker": "XLE",
    "target_subject_ref": TARGET,
    "metric_or_aspect": "industry energy price",
    "aspect": "demand_drivers",
    "proxy_gap": "ETF price includes diversified producers and is not this company's realised price.",
}


def bar(day="2026-09-01", close="100"):
    return {"date": day, "open": close, "high": close, "low": close,
            "close": close, "adj_close": close, "volume": "1000"}


class MarketProxyClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = DaltonStore(str(Path(self.tmp.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.prices = MarketPriceSeriesAuthority(self.store)

    def publish(self, *, captured="2026-09-01T23:30:00+00:00",
                day="2026-09-01", close="100", artifact="1" * 64):
        return self.prices.publish_series(
            company_ref=SOURCE, ticker="XLE", currency="USD", bars=[bar(day, close)],
            observations=[], invocation_ref="connector:yfinance:xle",
            artifact_hash=artifact, governance_ref="governance:yfinance",
            governance_hash="2" * 64, requested_start="2026-09-01",
            requested_end="2026-09-02", captured_at=captured)

    def test_empty_config_is_quiet_and_mapping_is_closed(self):
        path = Path(self.tmp.name) / "proxy.json"
        path.write_text('{"schema_version":"0.1","mappings":[]}', encoding="utf-8")
        self.assertEqual(load_mappings(path), [])
        with self.assertRaises(MarketProxyClaimError):
            load_mappings(path.with_name("missing.json"))

    def test_governed_series_derives_qualitative_proxy_and_index_entry(self):
        source = self.publish()
        result = MarketProxyClaimAuthority(self.store).refresh(MAPPING)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["source_series_version_ref"], source["id"])
        self.assertEqual(result["source_series_version_hash"], source["content_hash"])
        self.assertEqual(result["proxy_gap"], MAPPING["proxy_gap"])
        claim = self.store.get_claim(result["claim_version_ref"])
        self.assertEqual(claim["claim"]["claim_kind"], "qualitative")
        self.assertIsNone(claim["claim"]["value"])
        entry = ClaimIndexAuthority(self.store).current_entry(result["claim_version_ref"])
        self.assertEqual(entry["evidence_kind"], "market_proxy")
        _, actuals = guidance_material(self.store, TARGET)
        self.assertNotIn(result["claim_version_ref"],
                         {item["ref"] for item in actuals})
        self.assertEqual(self.store.list_claim_challenges(), [])
        self.assertEqual(MarketProxyClaimAuthority(self.store).refresh(MAPPING)["status"],
                         "duplicate")

    def test_provisional_bar_is_refused_until_settled(self):
        self.publish(captured="2026-09-01T18:00:00+00:00")
        result = MarketProxyClaimAuthority(self.store).refresh(MAPPING)
        self.assertEqual(result["status"], "waiting_for_settlement")
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM claim_versions").fetchone()[0], 0)

    def test_model_state_reads_proxy_separately_from_statements(self):
        self.publish()
        MarketProxyClaimAuthority(self.store).refresh(MAPPING)
        class Missions:
            connection = self.store.connection
            def statement_filings(inner, company_ref):
                return [{"company_ref": company_ref, "report_date": "2026-06-30",
                         "accession": "fixture", "entity_name": "Fixture",
                         "cik": "1467373", "form": "10-Q", "line_count": 1,
                         "ingest_id": "filing:1"}]
            def statement_lines(inner, ingest_id):
                return [{"statement": "income", "concept": "Revenue",
                         "label": "Revenue", "level": 0, "parent_concept": None,
                         "is_breakdown": False, "dimension_axis": None,
                         "dimension_member": None}]
        state = build_company_model_state(Missions(), TARGET)
        self.assertEqual(state["market_proxies"][0]["mapping_ref"], MAPPING["mapping_ref"])
        self.assertNotIn("market_proxy", json.dumps(state["statements"]))
        prompt = build_prompt(state)
        self.assertIn(MAPPING["proxy_gap"], prompt)
        self.assertIn(state["market_proxies"][0]["source_series_version_hash"], prompt)
        self.assertIn("never company actuals", prompt)

        before = state["state_hash"]
        self.publish(captured="2026-09-02T23:30:00+00:00",
                     day="2026-09-02", close="105", artifact="3" * 64)
        refreshed = MarketProxyClaimAuthority(self.store).refresh(MAPPING)
        after = build_company_model_state(Missions(), TARGET)
        self.assertNotEqual(after["state_hash"], before)
        self.assertIn(refreshed["source_series_version_hash"], build_prompt(after))


class LegacyClaimIndexMigrationTests(unittest.TestCase):
    def test_legacy_record_json_and_hash_remain_unchanged(self):
        # Exercise through the repository's pre-column schema fixture by
        # creating a current record, removing only the physical column, then
        # reopening the authority. SQLite preserves record_json bytes.
        fixture = LedgerFixture(); self.addCleanup(fixture.store.close)
        authority = ClaimIndexAuthority(fixture.store)
        claim = fixture.add_claim("legacy-proxy-kind")
        current = authority.record_entry(**entry_args(claim))
        legacy = {k: v for k, v in current.items()
                  if k not in {"status", "recanonicalised", "evidence_kind", "content_hash"}}
        legacy["content_hash"] = content_hash(legacy)
        encoded = canonical_json(legacy)
        fixture.store.connection.execute("DROP TRIGGER claim_index_entry_no_update")
        fixture.store.connection.execute(
            "UPDATE claim_index_entry_versions SET record_json=?,content_hash=? WHERE version_id=?",
            (encoded, legacy["content_hash"], current["id"]))
        reopened = ClaimIndexAuthority(fixture.store)
        decoded = reopened.entry(current["id"])
        row = fixture.store.connection.execute(
            "SELECT record_json,content_hash,evidence_kind FROM claim_index_entry_versions "
            "WHERE version_id=?", (current["id"],)).fetchone()
        self.assertEqual(row["record_json"], encoded)
        self.assertEqual(row["content_hash"], legacy["content_hash"])
        self.assertEqual(row["evidence_kind"], "statement")
        self.assertEqual(decoded["evidence_kind"], "statement")


class ProxyAcquisitionWiringTests(unittest.TestCase):
    def test_explicit_source_mapping_enters_price_acquisition_without_mutating_mission(self):
        class Prices:
            def latest_version(self, company_ref): return None
        class Launcher:
            def __init__(self): self.started = []
            def start(self, **kwargs):
                self.started.append(kwargs); return {"id": "ticket:proxy"}
        class Proxies:
            def refresh_all(self, mappings):
                return {"status": "settled", "results": [{"status": "waiting_for_series"}]}
        mission = {"autonomy": {"may_write": ["market_price"]},
                   "universe": [{"company_ref": TARGET}]}
        launcher = Launcher()
        lane = MissionMarketPriceLaneCoordinator(
            authority=Prices(), launcher=launcher, mission=lambda: mission,
            proxy_mappings=[MAPPING], proxy_authority=Proxies())
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], SOURCE)
        self.assertEqual(result["ticker"], "XLE")
        self.assertEqual(mission["universe"], [{"company_ref": TARGET}])

    def test_mapping_cannot_expand_research_universe(self):
        class Launcher: pass
        mission = {"autonomy": {"may_write": ["market_price"]}, "universe": []}
        lane = MissionMarketPriceLaneCoordinator(
            authority=object(), launcher=Launcher(), mission=lambda: mission,
            proxy_mappings=[MAPPING], proxy_authority=None)
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "misconfigured")
        self.assertIn(TARGET, result["reason"])


if __name__ == "__main__":
    unittest.main()
