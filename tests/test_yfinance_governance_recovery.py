from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.catalyst_calendar_launcher import CatalystCalendarLauncher
from dalton_core.connector_governance import build_governance_record
from dalton_core.consensus_estimate_launcher import ConsensusEstimateLauncher
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.market_price_launcher import MarketPriceLauncher
from dalton_core.mission_market_price_lane import MissionMarketPriceLaneCoordinator
from dalton_core.mission_consensus_lane import MissionConsensusLaneCoordinator
from dalton_core.mission_catalyst_lane import MissionCatalystLaneCoordinator


class _CaptureSpawn:
    def spawn(self, *, digest, record, **command):
        return {"id": digest, **record, "command": command}


class Price(_CaptureSpawn, MarketPriceLauncher):
    pass


class Consensus(_CaptureSpawn, ConsensusEstimateLauncher):
    pass


class Calendar(_CaptureSpawn, CatalystCalendarLauncher):
    pass


class YfinanceGovernanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def governance(self, kind, status, version):
        path = self.root / f"{kind}.json"
        path.write_text(
            json.dumps(
                build_governance_record(
                    kind,
                    approved_by="human:owner",
                    status=status,
                    effective_from="2026-09-10T00:00:00+00:00",
                    version=version,
                )
            )
        )
        return path

    def test_all_three_refuse_proposed_before_spawn_and_recover_on_approval(self):
        cases = (
            (
                Price,
                "yfinance-daily-prices",
                dict(company_ref="company:acn", ticker="ACN", start="2026-09-01", end="2026-09-10"),
            ),
            (
                Consensus,
                "yfinance-analyst-estimates",
                dict(
                    company_ref="company:acn",
                    ticker="ACN",
                    fiscal_year_end="08-31",
                    last_reported_period_end="2026-05-31",
                    day="2026-09-10",
                ),
            ),
            (
                Calendar,
                "yfinance-calendar",
                dict(company_ref="company:acn", ticker="ACN", issuer="Accenture", as_of="2026-09-10"),
            ),
        )
        for launcher_type, kind, arguments in cases:
            with self.subTest(kind=kind):
                path = self.governance(kind, "proposed", 1)
                launcher = launcher_type(state_dir=self.state, governance_path=path)
                proposed_hash = launcher.governance_identity()
                with self.assertRaisesRegex(LaneChildRejected, "not approved"):
                    launcher.start(**arguments)
                path = self.governance(kind, "approved", 2)
                approved_hash = launcher.governance_identity()
                ticket = launcher.start(**arguments)
                self.assertNotEqual(proposed_hash, approved_hash)
                self.assertEqual(ticket["governance_hash"], approved_hash)

    def test_consensus_identity_binds_both_fiscal_inputs(self):
        path = self.governance("yfinance-analyst-estimates", "approved", 1)
        launcher = Consensus(state_dir=self.state, governance_path=path)
        common = dict(
            company_ref="company:acn", ticker="ACN", day="2026-09-10",
            fiscal_year_end="08-31", last_reported_period_end="2026-05-31",
        )
        first = launcher.start(**common)
        second = launcher.start(**{**common, "last_reported_period_end": "2026-08-31"})
        third = launcher.start(**{**common, "fiscal_year_end": "12-31"})
        self.assertEqual(3, len({first["id"], second["id"], third["id"]}))

    def test_three_coordinators_retire_only_persisted_legacy_permission_refusals(self):
        now = lambda: datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        company = "company:acn"
        cases = (
            (
                "price",
                Price(state_dir=self.state, governance_path=self.governance("yfinance-daily-prices", "approved", 2)),
                "MarketPriceRunError: yfinance daily-prices governance record is not approved",
            ),
            (
                "consensus",
                Consensus(state_dir=self.state, governance_path=self.governance("yfinance-analyst-estimates", "approved", 2)),
                "ConsensusEstimateRunError: yfinance analyst-estimates governance record is not approved",
            ),
            (
                "calendar",
                Calendar(state_dir=self.state, governance_path=self.governance("yfinance-calendar", "approved", 2)),
                "CatalystCalendarRunError: yfinance calendar governance record is not approved",
            ),
        )
        for name, launcher, legacy_reason in cases:
            with self.subTest(name=name):
                ledger = self.root / f"ledger-{name}"

                def coordinator():
                    if name == "price":
                        return MissionMarketPriceLaneCoordinator(
                            authority=object(), launcher=launcher, mission=lambda: None,
                            clock=now, failure_ledger_dir=ledger,
                        )
                    if name == "consensus":
                        return MissionConsensusLaneCoordinator(
                            authority=object(), street_store=object(), launcher=launcher,
                            mission=lambda: None, fiscal_calendar_for=lambda _: None,
                            next_context=lambda *_: None, clock=now,
                            failure_ledger_dir=ledger,
                        )
                    return MissionCatalystLaneCoordinator(
                        authority=object(), launcher=launcher, mission=lambda: None,
                        clock=now, failure_ledger_dir=ledger,
                    )

                first = coordinator()
                first.budget.record(company, reason="gated:governance " + legacy_reason)
                restarted = coordinator()
                self.assertIsNotNone(restarted.budget.blocked(company))
                restarted._retire_legacy_permission(company)
                self.assertIsNone(restarted.budget.blocked(company))

                restarted.budget.record(
                    company, reason="MarketDataAdapterError: transport_unavailable"
                )
                again = coordinator()
                again._retire_legacy_permission(company)
                self.assertIsNotNone(again.budget.blocked(company))


if __name__ == "__main__":
    unittest.main()
