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
from dalton_core.research_auto_commit import (
    COMPANY_FACTS_ANNUAL_RULE_REF,
    COMPANY_FACTS_RULE_REF,
)
from dalton_core.research_plan import (
    PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
)
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.coverage_mission import CoverageMissionAuthority
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
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_forecast_reconciliation import ForecastReconciliationFixture

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


def _sec_company_facts_annual_body() -> bytes:
    """A 10-K that reports the fourth-quarter pair inside the annual filing (P9b)."""
    concept = {
        "label": "Revenues",
        "description": "Synthetic annual filing with quarterly disclosures.",
        "units": {"USD": [
            {
                "start": "2024-09-01", "end": "2025-08-31",
                "val": 69672977000, "accn": "0000320193-25-000217",
                "fy": 2025, "fp": "FY", "form": "10-K",
                "filed": "2025-10-10", "frame": "CY2025",
            },
            {
                "start": "2024-06-01", "end": "2024-08-31",
                "val": 16405819000, "accn": "0000320193-25-000217",
                "fy": 2025, "fp": "FY", "form": "10-K",
                "filed": "2025-10-10", "frame": "CY2024Q3",
            },
            {
                "start": "2025-06-01", "end": "2025-08-31",
                "val": 17596260000, "accn": "0000320193-25-000217",
                "fy": 2025, "fp": "FY", "form": "10-K",
                "filed": "2025-10-10", "frame": "CY2025Q3",
            },
        ]},
    }
    payload = {
        "cik": 320193,
        "entityName": "APPLE INC.",
        "facts": {"us-gaap": {"Revenues": concept}},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def install_lane_rules(state: Path, *, annual: bool = False) -> str:
    """Install the company-facts auto-start / auto-commit rules on a fresh Core.

    ``annual=True`` also lists the P9b 10-K rules next to the 10-Q rules.
    """
    core = DaltonStore(state / "core.sqlite")
    try:
        active = core.active_policy()
        policy = dict(active["policy"])
        start_rules = [PLAN_COMPANY_FACTS_AUTO_START_RULE_REF]
        commit_rules = [COMPANY_FACTS_RULE_REF]
        if annual:
            start_rules.append(PLAN_COMPANY_FACTS_ANNUAL_AUTO_START_RULE_REF)
            commit_rules.append(COMPANY_FACTS_ANNUAL_RULE_REF)
        policy["research_plan_auto_start"] = {
            "enabled": True, "rules": start_rules,
        }
        policy["research_candidate_auto_commit"] = {
            "enabled": True, "rules": commit_rules, "max_records": 20,
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
            issuers=kw.pop("issuers", (ISSUER,)),
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
            self.assertIs(lane.scheduler.connection, lane.core.connection)

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
                for t in ("agenda_policy_versions", "research_plan_versions", "agenda_cycles")
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
        foreign = Issuer("MSFT", "789019", "company:sec-cik:0000789019", "Microsoft")
        foreign_facts = json.loads(_sec_company_facts_body())
        foreign_facts["cik"] = 789019
        foreign_facts["entityName"] = "MICROSOFT CORP"
        foreign_body = json.dumps(foreign_facts, sort_keys=True, separators=(",", ":")).encode()
        with self._lane(issuers=(ISSUER, foreign), adapter=fake_adapter(self.clock, foreign_body)) as lane:
            seed = lane.run_issuer(foreign, actor_ref="human:tester", run_key="seed", **WINDOW)
            self.assertEqual(seed["status"], "committed", seed)
            self.assertEqual(len(lane.review.list_candidates()), 1)
        with self._lane(issuers=(ISSUER, foreign)) as lane:
            # Same rehearsal clock as the seed run: the second issuer must move
            # into the next 60-second SEC quota window or the connector rejects it.
            lane.advance_past_quota_window()
            summary = self._run(lane)
            self.assertEqual(len(lane.review.list_candidates()), 2)
            self.assertEqual(summary["status"], "committed", summary)
            self.assertEqual(summary["candidate"]["subject_ref"].endswith("320193"), True,
                             summary["candidate"])
            self.assertEqual(summary["formal_ledger_counts"]["claim_versions"], 2)

    def test_annual_form_commits_fourth_quarter_claim_when_policy_lists_annual_rules(self) -> None:
        """P9b: a 10-K run produces its own plan/Claim under the annual rules."""
        install_lane_rules(self.state, annual=True)
        window = {"filed_from": "2025-01-01", "filed_to": "2025-12-31"}
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            summary = lane.run_issuer(
                ISSUER, actor_ref="human:tester", run_key="annual-1", form="10-K", **window
            )
            self.assertEqual(summary["status"], "committed", summary)
            self.assertEqual(summary["form"], "10-K")
            self.assertEqual(summary["plan"]["parameters"]["form"], "10-K")
            self.assertEqual(summary["facts"]["growth_percent"], "7.26")
            self.assertEqual(summary["facts"]["latest_accession"], "0000320193-25-000217")
            self.assertEqual(summary["candidate"]["period"], "2025-06-01..2025-08-31")
            self.assertEqual(
                summary["policy_version_ref"], "policy:sec-lane-test:v2"
            )
            self.assertEqual([v["verdict"] for v in summary["verifications"]], ["pass", "pass"])
            self.assertEqual(summary["formal_ledger_counts"]["claim_versions"], 1)
            self.assertEqual(summary["integrity"]["core"], "ok")
            # A quarterly run over the same window is a distinct plan (the
            # annual form is part of the lane identity), not a duplicate.
            with self.assertRaises(LanePreconditionError):
                lane.run_issuer(
                    ISSUER, actor_ref="human:tester", run_key="annual-1", form="8-K", **window
                )
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            replay = lane.run_issuer(
                ISSUER, actor_ref="human:tester", run_key="annual-1", form="10-K", **window
            )
            self.assertEqual(replay["status"], "duplicate", replay)
            self.assertEqual(replay["plan"]["ref"], summary["plan"]["ref"])

    def test_mission_automation_commits_exact_accession_and_stage_claim(self) -> None:
        install_lane_rules(self.state, annual=True)
        window = {"filed_from": "2025-01-01", "filed_to": "2025-12-31"}
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            method = bootstrap_method_authorities(lane.core)
            params = mission_params(method)
            params["universe"] = [{
                "company_ref": ISSUER.company_ref, "ticker": ISSUER.ticker,
                "coverage_tier": "A", "bootstrap_priority": "P0",
            }]
            mission = CoverageMissionAuthority(lane.core).create_mission(
                params.pop("mission_ref"), **params
            )
            summary = lane.run_issuer(
                ISSUER, actor_ref="automation:coverage-mission", run_key="mission-annual-1",
                form="10-K", expected_accession="0000320193-25-000217",
                mission_context={
                    "mission_version_ref": mission["id"],
                    "mission_version_hash": mission["content_hash"],
                    "company_ref": ISSUER.company_ref,
                },
                **window,
            )
            self.assertEqual(summary["status"], "committed", summary)
            stage_claim = summary["mission_stage_claim"]
            self.assertEqual(stage_claim["status"], "fresh")
            self.assertEqual(stage_claim["stage_ref"], "initial_screen")
            self.assertEqual(stage_claim["actor_ref"], "automation:coverage-mission")
            authority = CoverageMissionAuthority(lane.core)
            self.assertEqual(len(authority.stage_claims(mission["id"], ISSUER.company_ref)), 1)
            progress = authority.mission_progress(mission["mission_ref"])
            self.assertEqual(progress["companies"][0]["claim_count"], 1)
            self.assertEqual(progress["companies"][0]["current_stage"], "initial_screen")

    ANNUAL_PERIOD = {
        "start": "2025-06-01", "end": "2025-08-31",
        "calendar": "company:fiscal", "kind": "quarter",
    }

    def _publish_forecast_line(self, store: DaltonStore, value: str) -> dict:
        fixture = ForecastReconciliationFixture(store)
        return fixture.line(
            value, period=self.ANNUAL_PERIOD, subject=ISSUER.company_ref,
            line_ref="forecast-line:aapl:revenue:q4fy25",
        )

    def test_human_lane_run_reconciles_matching_forecast_line(self) -> None:
        """P9c: the committed actual is reconciled against the latest forecast line."""
        install_lane_rules(self.state, annual=True)
        window = {"filed_from": "2025-01-01", "filed_to": "2025-12-31"}
        core = DaltonStore(self.state / "core.sqlite")
        try:
            line = self._publish_forecast_line(core, "17300")
        finally:
            core.close()
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            summary = lane.run_issuer(
                ISSUER, actor_ref="human:tester", run_key="annual-recon", form="10-K", **window
            )
            self.assertEqual(summary["status"], "committed", summary)
            outcome = summary["forecast_reconciliation"]
            self.assertEqual(outcome["status"], "reconciled", outcome)
            self.assertEqual(outcome["skipped"], [])
            record = outcome["created"][0]
            self.assertEqual(record["forecast_line_version_ref"], line["id"])
            self.assertEqual(record["actual_value"], "17596.26000000")
            self.assertEqual(record["forecast_value"], "17300.00000000")
            self.assertEqual(record["direction"], "above_forecast")
            self.assertEqual(record["band"], "notable")
            self.assertIsNone(record["mission_binding"])
            self.assertEqual(summary["formal_ledger_counts"]["claim_versions"], 1)

    def test_mission_automation_reconciles_only_with_the_scope(self) -> None:
        """P9c: mission v1 (no scope) skips truthfully; a mission granting the scope binds it."""
        install_lane_rules(self.state, annual=True)
        window = {"filed_from": "2025-01-01", "filed_to": "2025-12-31"}
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            method = bootstrap_method_authorities(lane.core)
            params = mission_params(method)
            params["universe"] = [{
                "company_ref": ISSUER.company_ref, "ticker": ISSUER.ticker,
                "coverage_tier": "A", "bootstrap_priority": "P0",
            }]
            self.assertNotIn("forecast_reconciliation", params["autonomy"]["may_write"])
            mission = CoverageMissionAuthority(lane.core).create_mission(
                params.pop("mission_ref"), **params
            )
            self._publish_forecast_line(lane.core, "16800")
            summary = lane.run_issuer(
                ISSUER, actor_ref="automation:coverage-mission", run_key="mission-recon-1",
                form="10-K", expected_accession="0000320193-25-000217",
                mission_context={
                    "mission_version_ref": mission["id"],
                    "mission_version_hash": mission["content_hash"],
                    "company_ref": ISSUER.company_ref,
                },
                **window,
            )
            self.assertEqual(summary["status"], "committed", summary)
            outcome = summary["forecast_reconciliation"]
            self.assertEqual(outcome["status"], "skipped", outcome)
            self.assertEqual(outcome["created"], [])
            self.assertIn("forecast_reconciliation", outcome["skipped"][0]["reason"])
            self.assertEqual(
                lane.core.connection.execute(
                    "SELECT COUNT(*) FROM forecast_reconciliations"
                ).fetchone()[0], 0,
            )
            # Owner publishes mission v2 with the scope; the pending pair is now
            # reconcilable under the exact mission binding (the controller tick
            # path), without re-running the lane.
            v2 = dict(params)
            v2["autonomy"] = {
                **params["autonomy"],
                "may_write": [*params["autonomy"]["may_write"], "forecast_reconciliation"],
            }
            v2["version_id"] = "coverage-mission-version:us-it-services:2"
            v2["prior_version_ref"] = mission["id"]
            v2["idempotency_key"] = "coverage-mission:us-it-services:2"
            # The lane run opened its own CoverageMissionAuthority on this
            # connection; open a fresh one for the post-run steps.
            authority = CoverageMissionAuthority(lane.core)
            mission_v2 = authority.create_mission(mission["mission_ref"], **v2)
            from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority
            reconciler = ForecastReconciliationAuthority(lane.core)
            result = reconciler.reconcile_pending(
                requested_by="automation:coverage-mission",
                mission_resolver=lambda company_ref: authority.authorize_forecast_reconciliation(
                    company_ref=company_ref, actor_ref="automation:coverage-mission",
                ),
            )
            self.assertEqual(result["status"], "reconciled", result)
            record = result["created"][0]
            self.assertEqual(record["mission_binding"]["mission_version_ref"], mission_v2["id"])
            self.assertEqual(record["mission_binding"]["mission_version_hash"], mission_v2["content_hash"])
            self.assertEqual(record["band"], "overturn_candidate")
            self.assertEqual(record["human_checkpoint"], "forecast_overturn")
            self.assertEqual(record["requested_by"], "automation:coverage-mission")

    def test_later_window_for_the_same_issuer_asks_a_new_question(self) -> None:
        """P9b: a second window must not die on the issuer's answered first question."""
        install_lane_rules(self.state)
        with self._lane() as lane:
            first = self._run(lane)
            self.assertEqual(first["status"], "committed", first)
        later = {"filed_from": "2025-09-01", "filed_to": "2026-08-20"}
        with self._lane() as lane:
            lane.advance_past_quota_window()
            second = lane.run_issuer(
                ISSUER, actor_ref="human:tester", run_key="run-2", **later
            )
        self.assertIn(second["status"], {"committed", "duplicate"}, second)
        self.assertNotEqual(second["plan"]["ref"], first["plan"]["ref"])
        core = DaltonStore(self.state / "core.sqlite")
        try:
            questions = core.connection.execute(
                "SELECT json_extract(identity_json,'$.question') FROM backlog_questions ORDER BY created_at"
            ).fetchall()
        finally:
            core.close()
        self.assertEqual(len(questions), 2)
        self.assertIn("10-Q filed 2025-08-20..2026-08-20", questions[0][0])
        self.assertIn("10-Q filed 2025-09-01..2026-08-20", questions[1][0])

    def test_annual_form_is_refused_when_policy_lists_only_quarterly_rules(self) -> None:
        """P9b: the historical single-rule policy keeps rejecting 10-K plans."""
        install_lane_rules(self.state)
        window = {"filed_from": "2025-01-01", "filed_to": "2025-12-31"}
        with self._lane(adapter=fake_adapter(self.clock, _sec_company_facts_annual_body())) as lane:
            result = lane.run_lane(
                actor_ref="human:tester", run_key="annual-2", form="10-K", **window
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["form"], "10-K")
        self.assertEqual(result["issuers"][0]["status"], "failed")
        self.assertIn("does not authorize", result["issuers"][0]["error"])
        core = DaltonStore(self.state / "core.sqlite")
        try:
            self.assertEqual(
                core.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0], 0
            )
        finally:
            core.close()

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

    def test_agenda_binding_with_different_issuer_set_is_refused(self) -> None:
        install_lane_rules(self.state)
        with self._lane() as lane:
            lane.ensure_agenda_bindings(actor_ref="human:tester")
        other = Issuer("MSFT", "789019", "company:sec-cik:0000789019", "Microsoft")
        with self._lane(issuers=(other,)) as lane:
            with self.assertRaises(LanePreconditionError):
                lane.ensure_agenda_bindings(actor_ref="human:tester")

    def test_partial_issuer_runs_within_the_universe_share_one_binding(self) -> None:
        # v2+ binding: a later run for a different in-universe ticker must
        # replay the same idempotent binding instead of conflicting (live
        # 2026-08-26: ACN+EPAM first, CTSH later).  v3 adds DXC to the
        # universe, which is exactly why the binding version was bumped.
        install_lane_rules(self.state)
        first, second = US_IT_SERVICES_ISSUERS[0], US_IT_SERVICES_ISSUERS[1]
        with self._lane(issuers=(first,)) as lane:
            binding_a = lane.ensure_agenda_bindings(actor_ref="human:tester")
        with self._lane(issuers=(second,)) as lane:
            binding_b = lane.ensure_agenda_bindings(actor_ref="human:tester")
        self.assertEqual(binding_a, binding_b)
        self.assertTrue(binding_a["agenda_policy_version_ref"].endswith(":v3"))

    def test_default_issuers_are_the_five_us_it_services_names(self) -> None:
        self.assertEqual(
            [i.ticker for i in US_IT_SERVICES_ISSUERS],
            ["ACN", "CTSH", "EPAM", "IBM", "DXC"],
        )
        self.assertEqual(US_IT_SERVICES_ISSUERS[3].company_ref, "company:sec-cik:0000051143")
        self.assertEqual(US_IT_SERVICES_ISSUERS[4].company_ref, "company:sec-cik:001688568")


if __name__ == "__main__":
    unittest.main()
