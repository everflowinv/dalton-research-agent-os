"""Mission-bound admission tests for targeted registered annual-report repair."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.company_dossier_launcher import run_digest
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.dossier_repair_feedback import read_dossier_repair_feedback
from dalton_core.mission_annual_research import (
    MissionAnnualResearchAuthority, MissionAnnualResearchError,
    WORKFLOW_CONTRACT_REF,
)
from dalton_core.model_router import ModelRouter
from dalton_core.registered_annual_report import OPERATION
from dalton_core.store import canonical_json
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_research_plan_annual_report import (
    ACCESSION, CIK, AnnualSourceHarness,
    seed_core_registration,
)
from tests import test_research_plan_annual_report as annual_support
from tests.test_research_plan_executor import PlanExecutorHarness


COMPANY = "wanhua"
MISSION = "coverage-mission-version:annual-test"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class MissionAnnualFixture:
    def __init__(self, case: unittest.TestCase, *, mission_calls: int = 10,
                 same_family: bool = False, sec_connected: bool = True) -> None:
        self.case = case
        self.harness = PlanExecutorHarness(suffix="mission-annual-admission")
        case.addCleanup(self.harness.close)
        self.harness.clock.value = NOW
        self.store = self.harness.core
        self.state = Path(self.harness.planner.temp.name)

        outer = {
            "max_daily_paid_calls": 20,
            "max_daily_cost_usd": 20.0,
            "max_alphaengine_calls_24h": 30,
        }
        active = self.store.active_policy_version()
        self.store.create_policy(
            {**active.policy, "research_budget": outer},
            policy_version_id="governance-policy-version:mission-annual:2",
            version_number=2, prior_version_ref=active.id,
            actor_ref="human:test-owner", change_reason="test exact research budget",
            activate=True,
        )
        method = bootstrap_method_authorities(
            self.store, mandate_ref="mandate:mission-annual-test",
            mandate_constraints={"research_budget": outer},
        )
        params = mission_params(method)
        params.update({
            "mission_ref": "coverage-mission:annual-test",
            "version_id": MISSION,
            "idempotency_key": "coverage-mission:mission-annual:1",
            "universe": [{
                "company_ref": COMPANY, "ticker": "TEST",
                "coverage_tier": "A", "bootstrap_priority": "P0",
            }],
            "budget": {
                "max_daily_paid_calls": mission_calls,
                "max_daily_cost_usd": 10.0,
                "max_alphaengine_calls_24h": 0,
            },
        })
        params["autonomy"] = {
            **params["autonomy"],
            "automation_principal": "automation:test",
            "may_write": list(params["autonomy"]["may_write"]) + ["research_task"],
        }
        if not sec_connected:
            for source in params["source_plan"]:
                if source["source_ref"] == "source:sec-edgar":
                    source["status"] = "not_connected"
        for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
            params.pop(key, None)
        self.mission = CoverageMissionAuthority(self.store).create_mission(
            params.pop("mission_ref"), **params
        )

        self.source = AnnualSourceHarness(self.harness.planner)
        case.addCleanup(self.source.close)
        self.registration = seed_core_registration(self.harness.planner, self.source)
        self._write_feedback()

        helper = annual_support.RegisteredAnnualReportExecutorTests
        draft_family = "annual-producer"
        verifier_family = draft_family if same_family else "annual-independent"
        self.draft_profile = helper._model_profile(
            stage="mission-draft", capability="capability:dalton:model:qualitative-research",
            slot="credential-slot:model:mission-draft", family=draft_family,
        )
        self.verifier_profile = helper._model_profile(
            stage="mission-verifier", capability="capability:dalton:model:qualitative-verifier",
            slot="credential-slot:model:mission-verifier", family=verifier_family,
        )
        for profile in (self.draft_profile, self.verifier_profile):
            profile["availability"] = {
                "state": "available", "checked_at": "2026-09-01T00:00:00+00:00",
                "valid_until": "2027-09-11T00:00:00+00:00",
            }
        self.draft_policy = helper._model_policy(
            stage="mission-draft", profile_ids=[self.draft_profile["id"]],
            capability="capability:dalton:model:qualitative-research", tier="brain",
        )
        self.verifier_policy = helper._model_policy(
            stage="mission-verifier", profile_ids=[self.verifier_profile["id"]],
            capability="capability:dalton:model:qualitative-verifier", tier="verifier",
        )
        self.verifier_policy["filters"]["family_independence_capabilities"] = [
            "capability:dalton:model:qualitative-verifier"
        ]
        self.router_path = self.state / "model-router.sqlite"
        self.router = ModelRouter(self.router_path, clock=self.harness.clock)
        case.addCleanup(self.router.close)
        for value in (self.draft_profile, self.verifier_profile):
            self.router.register_profile(value)
        for value in (self.draft_policy, self.verifier_policy):
            self.router.register_policy(value)
        self._write_configs()
        self.authority = MissionAnnualResearchAuthority(
            self.store, state_dir=self.state, registry=self.source.registry,
            router=self.router, clock=self.harness.clock,
        )

    def _write_feedback(self, marker: str = "missing-kpi", *, targets=None) -> None:
        runs = self.state / "company-dossier-runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        signature = "ledger:" + marker
        digest = run_digest(COMPANY, signature)
        directory = runs / digest
        directory.mkdir(mode=0o700)
        ticket = {
            "id": "company-dossier-run:" + digest, "company_ref": COMPANY,
            "signature": signature, "run_digest": digest, "status": "succeeded",
            "exit_code": 0, "completed_at": (
                "2026-09-11T13:00:00+00:00" if marker != "missing-kpi"
                else "2026-09-11T12:00:00+00:00"
            ),
        }
        summary = {
            "status": "succeeded", "company_ref": COMPANY,
            "dossier_status": "insufficient_evidence",
            "repair_targets": ([{
                "unit": "kpi_dictionary", "code": "missing_evidence",
                "detail": "retention numerator and denominator",
            }] if targets is None else targets),
        }
        for name, value in (("ticket.json", ticket), ("summary.json", summary)):
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(path, 0o600)

    def _write_configs(self) -> None:
        key = self.state / "broker.key"
        key.write_text("fixture", encoding="utf-8")
        os.chmod(key, 0o600)
        common = {
            "model_router_db": str(self.router_path.resolve()),
            "broker_socket": str((self.state / "broker.sock").resolve()),
            "broker_auth_key": str(key.resolve()), "broker_client_id": "client:test",
            "expected_agent_id": "test", "budget_db": str((self.state / "budget.sqlite").resolve()),
            "budget_policy_ref": "budget-policy:mission-annual:1",
            "call_budget": {"max_input_tokens": 32000, "max_output_tokens": 4000,
                            "max_cost_usd": 1.0, "timeout_seconds": 120},
            "run_budget": {"max_units": 1},
        }
        self.budget = ThesisImpactBudgetStore(
            self.state / "budget.sqlite", clock=self.harness.clock
        )
        self.case.addCleanup(self.budget.close)
        self.budget.register_policy(
            policy_version_id="budget-policy:mission-annual:1",
            day_cap_micros=10_000_000,
        )
        for path, policy, profile in (
            (self.state / DRAFT_MODEL_CONFIG_NAME, self.draft_policy, self.draft_profile),
            (self.state / VERIFIER_MODEL_CONFIG_NAME, self.verifier_policy, self.verifier_profile),
        ):
            value = {
                **common, "routing_policy_ref": policy["policy_version_ref"],
                "credential_slot_refs": [profile["credential_slot_ref"]],
            }
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)

    def args(self, **overrides):
        feedback = read_dossier_repair_feedback(self.state)[COMPANY]
        target = feedback["repair_targets"][0]
        values = {
            "operation": OPERATION, "workflow_contract_ref": WORKFLOW_CONTRACT_REF,
            "mission_version_ref": self.mission["id"],
            "mission_version_hash": self.mission["content_hash"],
            "company_ref": COMPANY, "actor_ref": "automation:test",
            "repair_feedback_ref": feedback["id"],
            "repair_feedback_hash": feedback["content_hash"],
            "repair_target_ref": target["id"], "repair_target_hash": target["content_hash"],
            "review_ref": self.registration["review_ref"], "issuer_cik": CIK,
            "accession": ACCESSION, "query_terms": ["retention"],
            # Targeted search proves the complete, readable acquired rendering
            # itself. It need not pretend every broad-reading window finished.
            "document_read_proof_ref": None,
        }
        values.update(overrides)
        return values


class MissionAnnualResearchTests(unittest.TestCase):
    def test_exact_admission_is_append_only_idempotent_and_resolves_without_dispatch(self):
        fixture = MissionAnnualFixture(self)
        before_work = fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0]
        before_routes = len(fixture.router.list_decisions())
        admitted = fixture.authority.admit(**fixture.args())
        duplicate = fixture.authority.admit(**fixture.args())
        resolved = fixture.authority.resolve_for_execution(admitted["id"])
        self.assertEqual(admitted["status_marker"], "fresh")
        self.assertEqual(duplicate["status_marker"], "duplicate")
        self.assertEqual(resolved["id"], admitted["id"])
        self.assertEqual(
            fixture.store.connection.execute(
                "SELECT count(*) FROM mission_annual_research_admissions"
            ).fetchone()[0], 1,
        )
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM scheduler_work_orders"
        ).fetchone()[0], before_work)
        self.assertEqual(len(fixture.router.list_decisions()), before_routes)
        with self.assertRaises(Exception):
            fixture.store.connection.execute(
                "UPDATE mission_annual_research_admissions SET company_ref='company:other'"
            )

    def test_foreign_scope_stale_authority_unsupported_workflow_and_actor_are_refused(self):
        fixture = MissionAnnualFixture(self)
        cases = (
            {"company_ref": "company:other"},
            {"mission_version_hash": "0" * 64},
            {"repair_target_hash": "0" * 64},
            {"accession": "0000320193-25-000080"},
            {"workflow_contract_ref": "workflow:unsupported"},
            {"actor_ref": "human:owner"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(Exception):
                fixture.authority.admit(**fixture.args(**overrides))
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_admissions"
        ).fetchone()[0], 0)

    def test_unconnected_source_and_nonindependent_verifier_are_refused(self):
        with self.subTest("unconnected"):
            fixture = MissionAnnualFixture(self, sec_connected=False)
            with self.assertRaises(Exception):
                fixture.authority.admit(**fixture.args())
        with self.subTest("same-family"):
            fixture = MissionAnnualFixture(self, same_family=True)
            with self.assertRaisesRegex(MissionAnnualResearchError, "independent"):
                fixture.authority.admit(**fixture.args())

    def test_budget_refusal_and_stale_feedback_have_no_model_side_effect(self):
        fixture = MissionAnnualFixture(self, mission_calls=1)
        before = len(fixture.router.list_decisions())
        with self.assertRaises(Exception):
            fixture.authority.admit(**fixture.args())
        self.assertEqual(len(fixture.router.list_decisions()), before)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_annual_research_admissions"
        ).fetchone()[0], 0)

        healthy = MissionAnnualFixture(self)
        admitted = healthy.authority.admit(**healthy.args())
        healthy._write_feedback("cleared", targets=[])
        with self.assertRaisesRegex(MissionAnnualResearchError, "stale"):
            healthy.authority.resolve_for_execution(admitted["id"])

    def test_query_terms_must_be_grounded_in_exact_repair_target(self):
        fixture = MissionAnnualFixture(self)
        with self.assertRaisesRegex(MissionAnnualResearchError, "query term"):
            fixture.authority.admit(**fixture.args(query_terms=["unrelated acquisition rumor"]))


if __name__ == "__main__":
    unittest.main()
