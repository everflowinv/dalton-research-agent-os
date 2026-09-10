from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.catalyst_calendar_launcher import CatalystCalendarLauncher
from dalton_core.connector_governance import build_governance_record
from dalton_core.consensus_estimate_launcher import ConsensusEstimateLauncher
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.market_price_launcher import MarketPriceLauncher


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


if __name__ == "__main__":
    unittest.main()
