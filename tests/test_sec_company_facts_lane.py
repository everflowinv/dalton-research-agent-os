"""Core-hosted SEC company-facts lane tests (no network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.research_auto_commit import COMPANY_FACTS_RULE_REF
from dalton_core.research_plan import PLAN_COMPANY_FACTS_AUTO_START_RULE_REF
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.sec_authority_harness import MutableClock, _Response
from dalton_core.sec_company_facts_lane import (
    Issuer,
    LanePreconditionError,
    RehearsalGovernance,
    SecCompanyFactsLane,
    US_IT_SERVICES_ISSUERS,
)
from dalton_core.sec_public_adapter import SecPublicRouterAdapter
from dalton_core.store import DaltonStore
from tests.test_research_plan_executor import _sec_company_facts_body

REPO = Path(__file__).resolve().parents[1]
ISSUER = Issuer("AAPL", "320193", "company:sec-cik:0000320193", "Apple Inc")
WINDOW = {"filed_from": "2025-08-20", "filed_to": "2026-08-20"}


def fake_adapter(clock: MutableClock, body: bytes | None = None) -> SecPublicRouterAdapter:
    payload = body if body is not None else _sec_company_facts_body()
    return SecPublicRouterAdapter(
        transport=PublicHttpTransport(
            resolver=lambda _host, _port: ("93.184.216.34",),
            exchange=lambda _t, _m, _h, _b, _timeout: _Response(payload),
        ),
        clock=clock,
    )


def install_lane_rules(state: Path) -> str:
    """Install the company-facts auto-start / auto-commit rules on a fresh Core."""
    core = DaltonStore(state / "core.sqlite")
    try:
        active = core.active_policy()
        policy = dict(active["policy"])
        policy["research_plan_auto_start"] = {
            "enabled": True, "rules": [PLAN_COMPANY_FACTS_AUTO_START_RULE_REF],
        }
        policy["research_candidate_auto_commit"] = {
            "enabled": True, "rules": [COMPANY_FACTS_RULE_REF], "max_records": 20,
        }
        installed = core.create_policy(
            policy,
            policy_version_id="policy:sec-lane-test:v2",
            version_number=2,
            prior_version_ref=active["policy_version_id"],
            actor_ref="human:test-owner",
            change_reason="authorize company-facts lane in test",
            activate=True,
        )
        return installed["policy_version_id"]
    finally:
        core.close()


class LaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir(mode=0o700)
        self.staging = self.state / "candidate-staging.sqlite"
        self.clock = MutableClock()

    def _lane(self, **kw) -> SecCompanyFactsLane:
        return SecCompanyFactsLane(
            state_dir=self.state,
            staging_path=self.staging,
            governance=kw.pop("governance", RehearsalGovernance(approved_by="human:tester")),
            issuers=(ISSUER,),
            adapter=kw.pop("adapter", fake_adapter(self.clock)),
            clock=self.clock,
            **kw,
        )

    def _run(self, lane: SecCompanyFactsLane, run_key: str = "run-1") -> dict:
        return lane.run_issuer(ISSUER, actor_ref="human:tester", run_key=run_key, **WINDOW)

    def test_single_issuer_commits_one_evidence_and_claim_without_gates(self) -> None:
        install_lane_rules(self.state)
        with self._lane() as lane:
            summary = self._run(lane)
            self.assertEqual(summary["status"], "committed", summary)
            self.assertEqual(summary["formal_ledger_counts"]["evidence_versions"], 1)
            self.assertEqual(summary["formal_ledger_counts"]["claim_versions"], 1)
            self.assertEqual(summary["human_gate_counts"], {"plan_approvals": 0, "claim_reviews": 0})
            self.assertEqual(set(summary["model_accounting_counts"].values()), {0})
            self.assertEqual(summary["closure"]["replay_status"], "duplicate")
            self.assertEqual(summary["facts"]["growth_percent"] is not None, True)
            self.assertEqual([v["verdict"] for v in summary["verifications"]], ["pass", "pass"])
            self.assertEqual(summary["integrity"]["core"], "ok")
            # WorkOrders live in core.sqlite, not a separate scheduler db.
            self.assertFalse((self.state / "scheduler.sqlite").exists())
            self.assertGreater(
                lane.core.connection.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0], 0
            )

    def test_same_parameters_rerun_is_duplicate_and_counts_unchanged(self) -> None:
        install_lane_rules(self.state)
        with self._lane() as lane:
            first = self._run(lane)
        with self._lane() as lane:
            second = self._run(lane)
        self.assertEqual(second["status"], "duplicate", second)
        self.assertEqual(second["plan"]["ref"], first["plan"]["ref"])
        self.assertEqual(second["formal_ledger_counts"], first["formal_ledger_counts"])
        self.assertEqual(second["promotion"], first["promotion"])

    def test_unapproved_governance_is_refused_before_opening_core(self) -> None:
        install_lane_rules(self.state)
        with self.assertRaises(LanePreconditionError):
            self._lane(governance=RehearsalGovernance(approved_by="human:tester", approved=False))

    def test_missing_core_rules_raise_precondition_without_writes(self) -> None:
        with self._lane() as lane:
            before = {
                t: lane.core.connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("agenda_policy_versions", "research_plan_versions", "work_orders")
            }
            with self.assertRaises(LanePreconditionError) as ctx:
                self._run(lane)
            self.assertIn("research_plan_auto_start", str(ctx.exception))
            after = {
                t: lane.core.connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in before
            }
        self.assertEqual(before, after)

    def test_locates_own_candidate_when_staging_has_other_candidates(self) -> None:
        install_lane_rules(self.state)
        # Seed the shared staging file with a foreign plan's candidate first.
        with self._lane() as lane:
            foreign = Issuer("MSFT", "789019", "company:sec-cik:0000789019", "Microsoft")
            lane.run_issuer(foreign, actor_ref="human:tester", run_key="seed", **WINDOW)
        with self._lane() as lane:
            summary = self._run(lane)
            self.assertEqual(summary["status"], "committed", summary)
            self.assertEqual(summary["candidate"]["subject_ref"].endswith("320193"), True,
                             summary["candidate"])
            self.assertEqual(summary["formal_ledger_counts"]["claim_versions"], 2)

    def test_cli_fixture_path_end_to_end(self) -> None:
        install_lane_rules(self.state)
        fixture = self.state / "facts.json"
        fixture.write_bytes(_sec_company_facts_body())
        summary_dir = self.state / "summary"
        proc = subprocess.run(
            [
                sys.executable, "-m", "dalton_core.sec_lane_cli",
                "--state-dir", str(self.state), "--staging", str(self.staging),
                "--rehearsal-approved-by", "human:tester",
                "--issuer", "AAPL", "--issuer-cik", "AAPL=320193",
                "--filed-from", WINDOW["filed_from"], "--filed-to", WINDOW["filed_to"],
                "--actor", "human:tester", "--fixture-company-facts", str(fixture),
                "--summary-dir", str(summary_dir), "--quiet",
            ],
            cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((summary_dir / "summary.json").read_text())
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["issuers"][0]["status"], "committed")
        self.assertEqual(oct(os.stat(summary_dir / "summary.json").st_mode & 0o777), "0o600")

    def test_default_issuers_are_the_four_us_it_services_names(self) -> None:
        self.assertEqual([i.ticker for i in US_IT_SERVICES_ISSUERS], ["ACN", "CTSH", "EPAM", "IBM"])
        self.assertEqual(US_IT_SERVICES_ISSUERS[3].company_ref, "company:sec-cik:0000051143")


if __name__ == "__main__":
    unittest.main()
