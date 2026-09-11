"""Real Unix-broker coverage for annual-report production budget wiring."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME,
    VERIFIER_MODEL_CONFIG_NAME,
    load_annual_report_model_configs,
    plan_model_execution,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_router import ModelRouter
from dalton_core.store import canonical_json
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_openclaw_model_adapter import (
    FakeBroker,
    failure_response,
    success_response,
)
from tests.test_research_plan_annual_report import (
    ACCESSION,
    COMPANY,
    MISSION,
    AnnualSourceHarness,
    seed_core_registration,
)
from tests.test_research_plan_executor import PlanExecutorHarness
from tests.test_transcript_polish_model_worker import policy, profile


def model_profile(*, stage, capability, slot):
    wire = profile()
    wire.update({
        "profile_version_ref": f"model-profile-version:annual-{stage}:1",
        "id": f"profile:annual-{stage}",
        "provider": "openai",
        "model": "gpt-5.6",
        "family": f"annual-{stage}",
        "credential_slot_ref": slot,
        "capabilities": [capability],
    })
    return wire


def model_policy(*, stage, profile_ids, capability, tier):
    wire = policy()
    wire.update({
        "policy_version_ref": f"routing-policy:annual-{stage}:1",
        "id": f"model-routing-policy:annual-{stage}",
    })
    wire["filters"] = dict(wire["filters"])
    wire["filters"]["allowed_profile_ids"] = list(profile_ids)
    wire["filters"]["family_independence_capabilities"] = (
        [capability] if "verifier" in stage else []
    )
    wire["fallback_chains"] = {"tiers": {tier: list(profile_ids)}}
    return wire


class AnnualReportProductionBudgetTests(unittest.TestCase):
    def test_cli_unix_broker_uses_active_outer_pooled_budget_for_both_workers(self):
        harness = PlanExecutorHarness(suffix="annual-report-production-budget")
        self.addCleanup(harness.close)
        state = Path(harness.planner.temp.name)
        outer = {
            "max_daily_paid_calls": 20,
            "max_daily_cost_usd": 20.0,
            "max_alphaengine_calls_24h": 30,
        }
        active = harness.core.active_policy_version()
        harness.core.create_policy(
            {**active.policy, "research_budget": outer},
            policy_version_id="governance-policy-version:annual-budget:2",
            version_number=2,
            prior_version_ref=active.id,
            actor_ref="human:test-owner",
            change_reason="authorize bounded annual model calls",
            activate=True,
        )
        method = bootstrap_method_authorities(
            harness.core,
            mandate_ref="mandate:annual-budget-test",
            mandate_constraints={"research_budget": outer},
        )
        params = mission_params(method)
        params.update({
            "mission_ref": "coverage-mission:annual-test",
            "version_id": MISSION,
            "idempotency_key": "coverage-mission:annual-budget:1",
            "universe": [{
                "company_ref": COMPANY,
                "ticker": "TEST",
                "coverage_tier": "A",
                "bootstrap_priority": "P0",
            }],
            "budget": {
                "max_daily_paid_calls": 10,
                "max_daily_cost_usd": 10.0,
                "max_alphaengine_calls_24h": 0,
            },
        })
        params["autonomy"] = {
            **params["autonomy"],
            "automation_principal": "automation:test",
        }
        for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
            params.pop(key, None)
        mission = CoverageMissionAuthority(harness.core).create_mission(
            params.pop("mission_ref"), **params
        )
        self.assertEqual(mission["id"], MISSION)

        source = AnnualSourceHarness(harness.planner)
        self.addCleanup(source.close)
        source.ticket_ref = "public-web-fetch:" + "a" * 24
        harness.planner.plans.annual_report_registry = source.registry
        registration = seed_core_registration(harness.planner, source)

        draft_capability = "capability:dalton:model:qualitative-research"
        verifier_capability = "capability:dalton:model:qualitative-verifier"
        draft_profile = model_profile(
            stage="budget-draft", capability=draft_capability,
            slot="credential-slot:model:budget-draft",
        )
        verifier_profile = model_profile(
            stage="budget-verifier", capability=verifier_capability,
            slot="credential-slot:model:budget-verifier",
        )
        for profile in (draft_profile, verifier_profile):
            profile["availability"] = {
                "state": "available",
                "checked_at": "2026-09-01T00:00:00+00:00",
                "valid_until": "2027-09-11T00:00:00+00:00",
            }
        draft_policy = model_policy(
            stage="budget-draft", profile_ids=[draft_profile["id"]],
            capability=draft_capability, tier="brain",
        )
        verifier_policy = model_policy(
            stage="budget-verifier", profile_ids=[verifier_profile["id"]],
            capability=verifier_capability, tier="verifier",
        )
        router_path = state / "model-router.sqlite"
        with ModelRouter(router_path) as router:
            for profile in (draft_profile, verifier_profile):
                router.register_profile(profile)
            for policy in (draft_policy, verifier_policy):
                router.register_policy(policy)

        statement = (
            "The company serves varied customers and depends on outsourcing partners."
        )
        draft = canonical_json({
            "schema_version": "0.1",
            "answer": statement,
            "candidate": {
                "normalized_statement": statement,
                "metric_or_aspect": "customer and operating dependencies",
                "period": "FY2025 annual report",
                "basis": "reported",
                "cited_match_indexes": [0, 1],
            },
        })
        verifier = canonical_json({
            "schema_version": "0.1",
            "verdict": "pass",
            "verified_statement": statement,
            "findings": [],
        })
        replies = iter(("capacity", draft, verifier))

        def respond(request):
            reply = next(replies)
            if reply == "capacity":
                return failure_response(request, dispatch_proof={
                    "version": "0.1",
                    "state": "definitely_not_sent",
                    "authority": "openclaw-model-broker",
                })
            return success_response(request, text=reply)

        broker = FakeBroker(state, respond, connections=3)
        self.addCleanup(broker.close)
        key_path = state / "broker.key"
        key_path.write_text("a" * 64, encoding="utf-8")
        os.chmod(key_path, 0o600)
        budget_path = state / "annual-budget.sqlite"
        with ThesisImpactBudgetStore(budget_path) as budget:
            budget.register_policy(
                policy_version_id="annual-budget-policy:test:1",
                day_cap_micros=20_000_000,
            )
        common = {
            "model_router_db": str(router_path.resolve()),
            "broker_socket": str(broker.path.resolve()),
            "broker_auth_key": str(key_path.resolve()),
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "dalton-model-broker",
            "budget_db": str(budget_path.resolve()),
            "budget_policy_ref": "annual-budget-policy:test:1",
            "call_budget": {
                "max_input_tokens": 50_000,
                "max_output_tokens": 4_000,
                "max_cost_usd": 2.0,
                "timeout_seconds": 30,
            },
            "run_budget": {"max_units": 1, "max_seconds": 90},
        }
        for path, policy, profile in (
            (state / DRAFT_MODEL_CONFIG_NAME, draft_policy, draft_profile),
            (state / VERIFIER_MODEL_CONFIG_NAME, verifier_policy, verifier_profile),
        ):
            config = {
                **common,
                "routing_policy_ref": policy["policy_version_ref"],
                "credential_slot_refs": [profile["credential_slot_ref"]],
            }
            if path.name == DRAFT_MODEL_CONFIG_NAME:
                config["run_budget"] = {"max_units": 2, "max_seconds": 90}
            path.write_text(canonical_json(config) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        configs = load_annual_report_model_configs(state)

        decision, records = harness.planner._selected_questions([(
            "Which customers and outsourced operations shape the company?",
            "Use only the registered annual report",
        )])
        record = records[0]
        created = harness.planner.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            **registration,
            query_terms=["customers", "outsourcing partners"],
            draft_model_execution=plan_model_execution(
                configs[0], "registered_annual_report_draft"
            ),
            verifier_model_execution=plan_model_execution(
                configs[1], "registered_annual_report_verifier"
            ),
            actor_ref="core:planner",
        )
        harness.planner._approve(created, suffix="annual-production-budget")
        harness.planner._start(created, suffix="annual-production-budget")

        ticket_dir = state / "fetches" / source.ticket_ref.split(":", 1)[1]
        ticket_dir.mkdir(parents=True)
        document_ref = f"sec:filing:{source.accession}"
        for name, value in (
            ("ticket.json", {
                "id": source.ticket_ref,
                "status": "succeeded",
                "document_ref": document_ref,
            }),
            ("summary.json", {
                "url_ref": document_ref,
                "canonical_url": source.url,
                "manifest_ref": source.manifest["id"],
                "manifest_hash": source.manifest["content_hash"],
                "status": "succeeded",
            }),
            ("manifest.json", source.manifest),
        ):
            target = ticket_dir / name
            target.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(target, 0o600)
        web_governance = state / "web-fetch-governance.json"
        web_governance.write_text("{}\n", encoding="utf-8")
        os.chmod(web_governance, 0o600)
        staging = state / "review" / "candidate-staging.sqlite"
        staging.parent.mkdir()
        output = state / "annual-cli-output"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        completed = subprocess.run([
            sys.executable, "-m", "dalton_core.sec_lane_cli",
            "--state-dir", str(state),
            "--staging", str(staging),
            "--governance", str(web_governance),
            "--rehearsal-approved-by", "human:test-owner",
            "--actor", "human:test-owner",
            "--annual-plan-ref", created["plan_version_ref"],
            "--web-fetch-governance", str(web_governance),
            "--annual-draft-model-config", str(state / DRAFT_MODEL_CONFIG_NAME),
            "--annual-verifier-model-config", str(state / VERIFIER_MODEL_CONFIG_NAME),
            "--spool-dir", str(source.root / "spool"),
            "--summary-dir", str(output),
            "--quiet",
        ], env=env, cwd=state, capture_output=True, text=True, timeout=60)
        diagnostic = completed.stderr
        if completed.returncode != 0:
            if (output / "summary.json").is_file():
                diagnostic += "\n" + (output / "summary.json").read_text(
                    encoding="utf-8"
                )
            diagnostic += "\nbroker requests=" + repr(broker.requests)
            with sqlite3.connect(budget_path) as debug_budget:
                diagnostic += "\nbudget rows=" + repr(debug_budget.execute(
                    "SELECT work_order_ref,attempt_number,phase "
                    "FROM thesis_impact_day_admissions"
                ).fetchall())
        self.assertEqual(completed.returncode, 0, diagnostic)
        result = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(broker.requests), 3)

        connection = sqlite3.connect(budget_path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        rows = connection.execute(
            "SELECT a.attempt_number,a.phase,a.pool,a.pool_lane,s.actual_micros,"
            "b.record_json FROM thesis_impact_day_admissions a "
            "JOIN model_mission_budget_bindings b ON b.admission_id=a.admission_id "
            "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
            "ORDER BY a.created_at"
        ).fetchall()
        self.assertEqual(
            [(row["attempt_number"], row["phase"], row["actual_micros"])
             for row in rows],
            [(1, "assessment", 0), (2, "assessment", 10_000),
             (1, "verification", 10_000)],
        )
        self.assertEqual({row["pool"] for row in rows}, {"coverage"})
        self.assertEqual(
            {row["pool_lane"] for row in rows},
            {"registered_annual_report_draft", "registered_annual_report_verifier"},
        )
        for row in rows:
            binding = json.loads(row["record_json"])
            self.assertEqual(binding["mission_version_ref"], MISSION)
            self.assertEqual(binding["mission_version_hash"], mission["content_hash"])
            self.assertEqual(
                binding["outer_budget"]["governance_policy_version_ref"],
                "governance-policy-version:annual-budget:2",
            )


if __name__ == "__main__":
    unittest.main()
