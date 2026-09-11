"""Production subprocess coverage for source-neutral document research."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_research import build_document_research_policy
from dalton_core.document_research_inventory import load_document_inventory_authority
from dalton_core.document_research_strategy import STRATEGY_VERSION
from dalton_core.mission_document_model_authority import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
)
from dalton_core.mission_document_research import PURPOSE
from dalton_core.mission_document_research_admission import (
    admit_directed_inquiry, open_mission_document_admission_authority,
)
from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.store import canonical_json, content_hash
from tests.test_mission_annual_research import COMPANY, MissionAnnualFixture
from tests.test_openclaw_model_adapter import FakeBroker, seal, success_response


class MissionDocumentResearchProductionTests(unittest.TestCase):
    def _public_fixture(self):
        fixture = MissionAnnualFixture(
            self, company_in_mandate=True,
            additional_connected_source="source:public-web",
        )
        public_record = "mission-discovered-document:public-runtime-test"
        coverage = CoverageMissionAuthority(fixture.store)
        fixture.store.connection.commit()
        fixture.store.connection.execute("PRAGMA foreign_keys=OFF")
        with coverage._transaction() as cursor:
            cursor.execute(
                "INSERT INTO coverage_mission_discovered_documents"
                "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovery_ref,status,ticket_ref,failure_reason,failure_retryable,"
                "created_at,updated_at,host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (public_record, fixture.mission["id"], COMPANY, "source:public-web",
                 fixture.source.manifest["url_ref"],
                 "mission-source-discovery:public-runtime-test", "acquired",
                 fixture.source.ticket_ref, None, None,
                 fixture.mission["created_at"], fixture.mission["created_at"],
                 "www.sec.gov"),
            )
        fixture.store.connection.execute("PRAGMA foreign_keys=ON")
        fixture.harness.clock.value = datetime.now(timezone.utc)
        return fixture

    def _install_runtime_authorities(self, fixture, broker):
        ticket_dir = fixture.state / "fetches" / fixture.source.ticket_ref.split(":", 1)[1]
        ticket_dir.mkdir(parents=True)
        document_ref = fixture.source.manifest["url_ref"]
        for name, value in (
            ("ticket.json", {"id": fixture.source.ticket_ref, "status": "succeeded",
                             "document_ref": document_ref}),
            ("summary.json", {"url_ref": document_ref,
                              "canonical_url": fixture.source.url,
                              "manifest_ref": fixture.source.manifest["id"],
                              "manifest_hash": fixture.source.manifest["content_hash"],
                              "status": "succeeded"}),
            ("manifest.json", fixture.source.manifest),
        ):
            path = ticket_dir / name
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        key = fixture.state / "broker.key"
        key.write_text("a" * 64, encoding="utf-8")
        os.chmod(key, 0o600)
        annual_names = (
            "registered-annual-report-draft-model-config.json",
            "registered-annual-report-verifier-model-config.json",
        )
        generic_names = (DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME)
        for source, target in zip(annual_names, generic_names):
            value = json.loads((fixture.state / source).read_text(encoding="utf-8"))
            value.update({
                "broker_socket": str(broker.path.resolve()),
                "broker_auth_key": str(key.resolve()),
                "broker_client_id": "client:dalton-core",
                "expected_agent_id": "dalton-model-broker",
                "call_budget": {
                    "max_input_tokens": 32_000,
                    "max_output_tokens": 4_000,
                    "max_cost_usd": 1.0,
                    "timeout_seconds": 30,
                },
                "run_budget": {"max_units": 1, "max_seconds": 300},
                "transport_retry": {
                    "max_definitely_not_sent_retries": 0,
                    "queue_wait_seconds": 0,
                    "retry_backoff_seconds": 0,
                },
            })
            path = fixture.state / target
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        policy = build_document_research_policy(
            policy_ref="policy:mission-document:production-test:0.1",
            allowed_purposes=[PURPOSE],
            allowed_access_policy_refs=["policy:access:public-web"],
            max_question_chars=2_000, max_query_terms=10,
            max_query_term_chars=160, max_results=8,
            max_context_before_chars=180, max_context_after_chars=520,
            max_read_chars=10_000,
        )
        document_config = fixture.state / "document-research-config.json"
        document_config.write_text(canonical_json({
            "schema_version": "document-research-config-0.1",
            "purpose": PURPOSE,
            "spool_dir": str((fixture.source.root / "spool").resolve()),
            "enabled_sources": ["source:public-web"],
            "policy": policy,
            "source_reading_limits": {
                "alphaengine_max_document_chars": 10_000_000,
                "public_web_max_source_chars": 9_000_000,
                "public_web_max_pdf_pages": 2_000,
                "public_web_max_decompressed_bytes": 80_000_000,
            },
            "inventory_preview_chars": 600,
        }) + "\n", encoding="utf-8")
        os.chmod(document_config, 0o600)
        return document_config

    def _admit(self, fixture, document_config):
        inventory = load_document_inventory_authority(
            core=fixture.store, mission=fixture.mission, state_dir=fixture.state,
            config_path=document_config,
        )
        self.assertEqual(inventory["status"], "configured")
        rows = inventory["readable_documents_by_company"][COMPANY]
        
        self.assertEqual(len(rows), 1, inventory)
        selected = rows[0]
        inquiry = {
            "rank": 0, "company_ref": COMPANY,
            "question": "Which customer and outsourcing dependencies shape the business?",
            "wants": "Return the exact annual-report evidence.",
            "because": "The evidence supports a bounded qualitative claim.",
            "directed_document": {
                "strategy_version": STRATEGY_VERSION,
                "document_ref": selected["document_ref"],
                "document_version_hash": selected["document_version_hash"],
                "query_terms": ["customer segments", "outsourcing partners"],
                "query_rationale": "Search the exact acquired filing.",
            },
        }
        plan = {
            "schema_version": "0.1",
            "task_ref": "task:research-plan-directives:0.1",
            "created_at": fixture.mission["created_at"],
            "state_hash": "9" * 64,
            "mission_version_ref": fixture.mission["id"],
            "assessment": "Read the selected original filing.",
            "directives": [], "inquiries": [inquiry], "sufficiency": [],
        }
        plan["content_hash"] = content_hash(plan)
        from tests.test_mission_document_research import MissionDocumentResearchTests

        stored = MissionDocumentResearchTests()._record_plan(fixture, plan)
        with open_mission_document_admission_authority(
            store=fixture.store, state_dir=fixture.state, mission=fixture.mission,
            planner_scheduler_db=fixture.state / "core.sqlite",
            planner_model_config_path=(
                fixture.state / "registered-annual-report-draft-model-config.json"
            ),
            document_config_path=document_config,
        ) as (authority, registrations):
            result = admit_directed_inquiry(
                authority=authority, registrations=registrations,
                backlog=ResearchQuestionBacklog(fixture.store),
                mission=fixture.mission, plan=stored, inquiry=inquiry,
            )
            admission = authority.resolve_for_execution(result["admission_ref"])
        return admission

    def test_real_unix_broker_budget_staging_and_replay(self):
        fixture = self._public_fixture()
        statement = "The company serves varied customers and depends on outsourcing partners."
        replies = iter((
            (fixture.draft_profile, canonical_json({
                "schema_version": "0.1", "status": "answered", "answer": statement,
                "candidate": {
                    "normalized_statement": statement,
                    "metric_or_aspect": "customer and operating dependencies",
                    "period": "FY2025 annual report", "basis": "reported",
                    "cited_match_indexes": [0],
                }, "missing": [],
            })),
            (fixture.verifier_profile, canonical_json({
                "schema_version": "0.1", "verdict": "pass",
                "verified_statement": statement, "findings": [],
            })),
        ))

        def respond(request):
            semantic = dict(request)
            semantic.pop("queueWaitMs", None)
            profile, text = next(replies)
            response = success_response(semantic, text=text)
            response.pop("contentHash")
            response["provider"] = profile["provider"]
            response["model"] = profile["model"]
            response["canonicalModel"] = f"{profile['provider']}/{profile['model']}"
            return seal(response)

        broker = FakeBroker(fixture.state, respond, connections=2)
        self.addCleanup(broker.close)
        document_config = self._install_runtime_authorities(fixture, broker)
        admission = self._admit(fixture, document_config)
        self.assertEqual(fixture.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_admissions"
        ).fetchone()[0], 1)
        staging_path = fixture.state / "mission-document-staging.sqlite"
        CandidateStagingStore(staging_path).close()
        launcher = MissionDocumentResearchLauncher(
            state_dir=fixture.state, staging_path=staging_path,
            planner_scheduler_db=fixture.state / "core.sqlite",
            planner_model_config_path=(
                fixture.state / "registered-annual-report-draft-model-config.json"
            ),
            document_config_path=document_config,
            python_executable=sys.executable,
        )
        self.addCleanup(launcher.close)
        coordinator = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=launcher,
        )
        self.assertEqual(len(coordinator._admissions()), 1)
        python_path = os.pathsep.join((
            str(Path(__file__).parents[1] / "src"), str(Path(__file__).parents[1]),
        ))
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            launched = coordinator.dispatch_once()
            self.assertEqual(launched["status"], "launched", launched)
            ticket = launcher.status(launched["ticket_ref"])
            exit_code = launcher.wait(timeout=60)
            self.assertEqual(
                exit_code, 0,
                launcher._ticket_path(ticket["id"]).with_name("run.log").read_text()
                + canonical_json(launcher.status(ticket["id"])),
            )
            finished = launcher.status(ticket["id"])
        self.assertEqual(finished["summary"]["status"], "complete")
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(len(broker.requests), 2)
        with sqlite3.connect(fixture.state / "budget.sqlite") as budget:
            rows = budget.execute(
                "SELECT a.work_order_ref,s.actual_micros FROM "
                "thesis_impact_day_admissions a JOIN thesis_impact_day_settlements s "
                "ON s.admission_id=a.admission_id WHERE "
                "a.work_order_ref LIKE 'work:mission-document-research-%'"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[1] for row in rows], [10_000, 10_000])
        staging = CandidateStagingStore(staging_path)
        try:
            self.assertEqual(staging.counts()["candidate_stage_requests"], 1)
        finally:
            staging.close()
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            replay = launcher.resume(
                admission_ref=admission["id"], admission_hash=admission["content_hash"],
                prior_ticket_ref=ticket["id"], authorization="test:exact-scheduler-replay")
            self.assertEqual(launcher.wait(timeout=60), 0)
            self.assertEqual(launcher.status(replay["id"])["summary"]["status"], "complete")
        self.assertEqual(len(broker.requests), 2)
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            wrong = launcher.start(
                admission_ref=admission["id"], admission_hash="0" * 64,
            )
            self.assertEqual(launcher.wait(timeout=60), 1)
            refused = launcher.status(wrong["id"])
        self.assertEqual(refused["summary"]["status"], "failed")
        self.assertIn("admission binding drifted", refused["summary"]["error"])
        self.assertEqual(len(broker.requests), 2)

    def test_budget_refusal_is_durable_before_any_broker_send(self):
        fixture = self._public_fixture()
        consumed = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=fixture.harness.clock.value.date().isoformat(),
            work_order_ref="work:other-generic-budget-consumer",
            attempt_number=1, phase="assessment",
            route_decision_ref="route:other-generic-budget-consumer",
            reserved_micros=9_500_000,
        )
        self.assertEqual(consumed["status"], "fresh")

        def unexpected(_request):
            self.fail("budget refusal must precede broker I/O")

        broker = FakeBroker(fixture.state, unexpected, connections=1)
        self.addCleanup(broker.close)
        document_config = self._install_runtime_authorities(fixture, broker)
        admission = self._admit(fixture, document_config)
        staging_path = fixture.state / "mission-document-budget-staging.sqlite"
        CandidateStagingStore(staging_path).close()
        launcher = MissionDocumentResearchLauncher(
            state_dir=fixture.state, staging_path=staging_path,
            planner_scheduler_db=fixture.state / "core.sqlite",
            planner_model_config_path=(
                fixture.state / "registered-annual-report-draft-model-config.json"
            ),
            document_config_path=document_config, python_executable=sys.executable,
        )
        self.addCleanup(launcher.close)
        coordinator = MissionDocumentResearchCoordinator(
            store=fixture.store, launcher=launcher,
        )
        python_path = os.pathsep.join((
            str(Path(__file__).parents[1] / "src"), str(Path(__file__).parents[1]),
        ))
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            launched = coordinator.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.assertEqual(launcher.wait(timeout=60), 1)
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(
            result["last"]["recovery"]["reason"].startswith(
                "budget_or_pool_refused:"
            ), result,
        )
        self.assertEqual(broker.requests, [])


if __name__ == "__main__":
    unittest.main()
