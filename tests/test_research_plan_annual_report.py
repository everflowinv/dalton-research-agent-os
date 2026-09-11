"""Qualitative ResearchPlan contract for one registered SEC annual report."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_read_completion import DocumentReadCompletionAuthority
from dalton_core.observability import ObservabilityStore
from dalton_core.model_router import ModelRouter
from dalton_core.model_fallback_chain import tier_for
from dalton_core.model_selection import PURPOSE_LABELS, PURPOSE_MODEL_CONFIGS
from dalton_core.annual_report_qualitative import (
    RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
)
from dalton_core.public_web_core_fetch import (
    PublicWebCoreFetch, WebFetchConnectorGovernance,
    build_web_fetch_governance_record, url_authority_from_discovery,
)
from dalton_core.public_web_core_search import (
    FakeWebSearchHandle, PublicWebCoreSearch, WebSearchConnectorGovernance,
    build_web_search_governance_record,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.registered_annual_report import (
    RegisteredAnnualReportError,
    RegisteredAnnualReportRegistry,
    normalize_request,
    source_bytes_hash,
)
from dalton_core.runner_journal import RunnerJournal
from dalton_core.sec_lane_launcher import SecLaneLauncher
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.research_plan import (
    REGISTERED_ANNUAL_REPORT_OPERATION,
    ResearchPlanValidationError,
    _plan_work_orders,
    _resolved_plan_work_orders,
)
from dalton_core.research_plan_executor import ResearchPlanExecutor
from tests import test_research_plan as planner_test_support
from tests.test_research_plan_executor import PlanExecutorHarness
from dalton_core.store import canonical_json, content_hash
from dalton_core.annual_report_runtime import (
    DRAFT_MODEL_CONFIG_NAME, VERIFIER_MODEL_CONFIG_NAME,
    load_annual_report_model_configs, plan_model_execution,
)
from tests.test_public_web_fetch_lane import _Response
from tests.test_transcript_polish_model_worker import (
    FakeAdapter,
    ReturnedFailureSequenceAdapter,
    policy,
    profile,
)


CIK = "0000320193"
ACCESSION = "0000320193-25-000079"
TEXT = (
    "Apple serves consumers, small and mid-sized businesses, education, "
    "enterprise and government customers. Geographic and channel mix can "
    "affect net sales and margins. The Company depends on manufacturing and "
    "logistics services performed by outsourcing partners."
)
MISSION = "coverage-mission-version:annual-test"
COMPANY = "wanhua"
REVIEW = "mission-document-review:annual-test"
SEC_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019325000079/apple-20250927.htm"
)


class AnnualSourceHarness:
    """Actual connector receipts, manifest, spool bytes and deterministic renderer."""

    def __init__(self, fixture, *, accession: str = ACCESSION):
        self.fixture = fixture
        original_clock = fixture.scheduler.clock
        clock = original_clock
        if clock() < datetime(2026, 8, 26, tzinfo=timezone.utc):
            clock = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
        fixture.scheduler.clock = clock
        self.root = Path(fixture.temp.name) / ("annual-source-" + accession)
        self.root.mkdir()
        self.connectors = ConnectorStore(fixture.store, clock=clock)
        self.observability = ObservabilityStore(fixture.store)
        self.journal = RunnerJournal(fixture.store, clock=clock)
        self.spool = RawSpool(self.root / "spool", max_total_bytes=10_000_000)
        search_governance = WebSearchConnectorGovernance(
            build_web_search_governance_record(
                approved_by="human:test-owner", status="approved"
            )
        )
        fetch_governance = WebFetchConnectorGovernance(
            build_web_fetch_governance_record(
                approved_by="human:test-owner", status="approved"
            )
        )
        self.search_catalog = CapabilityCatalog(
            self.root / "search-catalog.sqlite",
            approval_resolver=search_governance.approval,
            policy_resolver=search_governance.policy,
            clock=clock,
        )
        self.fetch_catalog = CapabilityCatalog(
            self.root / "fetch-catalog.sqlite",
            approval_resolver=fetch_governance.approval,
            policy_resolver=fetch_governance.policy,
            clock=clock,
        )
        url = SEC_URL.replace(ACCESSION.replace("-", ""), accession.replace("-", ""))
        self.url = url
        search = PublicWebCoreSearch(
            store=fixture.store, connectors=self.connectors,
            observability=self.observability, journal=self.journal,
            scheduler=fixture.scheduler, catalog=self.search_catalog,
            spool=self.spool, governance=search_governance,
            host_handle=FakeWebSearchHandle([{"url": url, "title": "Annual report"}]),
            clock=clock,
        )
        receipt = search.search(search.build_request({
            "query": "annual report", "date_after": "2025-01-01",
            "date_before": "2026-01-01",
        }))
        authority = url_authority_from_discovery(
            fixture.store.connection, self.spool, url_ref=receipt["document_refs"][0],
            source_envelope_ref=receipt["source_envelope_ref"],
        )
        body = f"<html><body><p>{TEXT}</p></body></html>".encode()
        fetch = PublicWebCoreFetch(
            store=fixture.store, connectors=self.connectors,
            observability=self.observability, journal=self.journal,
            scheduler=fixture.scheduler, catalog=self.fetch_catalog,
            spool=self.spool, governance=fetch_governance,
            transport=PublicHttpTransport(
                resolver=lambda _host, _port: ("23.33.29.153",),
                exchange=lambda *_args: _Response(body),
            ),
            clock=clock,
        )
        fetched = fetch.fetch(fetch.build_request(authority))
        fixture.scheduler.clock = original_clock
        self.manifest = fetch.manifest(fetched)
        self.ticket_ref = "public-web-fetch:annual-test"
        self.reader = ConnectorCompletionReceiptReader(
            connectors=self.connectors, observability=self.observability
        )
        self.registry = RegisteredAnnualReportRegistry(
            core=fixture.store, spool=self.spool,
            manifest_reader=self.read_manifest, receipt_reader=self.reader,
        )

    def read_manifest(self, ticket_ref, document_ref):
        if ticket_ref != self.ticket_ref or document_ref != f"sec:filing:{self.accession}":
            raise RegisteredAnnualReportError("test manifest pointer drifted")
        return self.manifest

    @property
    def accession(self):
        compact = self.url.rsplit("/", 2)[-2]
        return f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"

    def close(self):
        self.fetch_catalog.close()
        self.search_catalog.close()


def seed_core_registration(fixture, source, *, accession=ACCESSION, cik=CIK):
    """Install a minimal acquired SEC/statement authority in the test Core."""

    authority = CoverageMissionAuthority(fixture.store)
    connection = fixture.store.connection
    connection.execute("PRAGMA foreign_keys=OFF")
    now = "2026-09-11T10:00:00.000000+00:00"
    discovered_ref = "mission-discovered-document:annual-test:" + accession
    review_ref = REVIEW + ":" + accession
    mission_record = {
        "autonomy": {"automation_principal": "automation:test"},
        "budget": {"max_daily_paid_calls": 100, "max_daily_cost_usd": 1000.0,
                   "max_alphaengine_calls_24h": 0},
    }
    with authority._transaction() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO coverage_mission_versions("
            "mission_version_id,mission_ref,version_number,prior_version_id,industry_ref,"
            "playbook_version_ref,constitution_version_ref,mandate_version_ref,record_json,"
            "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MISSION, "coverage-mission:annual-test", 1, None, "industry:test", "playbook:test",
             "constitution:test", "mandate:test", canonical_json(mission_record),
             content_hash(mission_record), "human:test", now),
        )
        cur.execute(
            "INSERT OR IGNORE INTO coverage_mission_pointer("
            "mission_ref,mission_version_id,version_number,content_hash,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("coverage-mission:annual-test", MISSION, 1,
             content_hash(mission_record), now),
        )
        cur.execute(
            "INSERT INTO coverage_mission_discovered_documents("
            "record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,"
            "status,ticket_ref,failure_reason,failure_retryable,created_at,updated_at,host) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (discovered_ref, MISSION, COMPANY, "source:sec-edgar", f"sec:filing:{accession}",
             "mission-source-discovery:test", "acquired", source.ticket_ref, None, None,
             now, now, "sec.gov"),
        )
        cur.execute(
            "INSERT INTO coverage_mission_document_reviews("
            "review_id,mission_version_ref,company_ref,source_ref,document_ref,"
            "discovered_document_ref,state,candidate_claim_version_ref,rationale,registered_by,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (review_ref, MISSION, COMPANY, "source:sec-edgar", f"sec:filing:{accession}",
             discovered_ref, "awaiting_human_extraction", None, None, "automation:test", now, now),
        )
        cur.execute(
            "INSERT INTO coverage_mission_statement_dispatches("
            "dispatch_id,mission_version_ref,mission_version_hash,company_ref,ticker,actor_ref,"
            "form,filing_limit,attempt,authorization_json,request_hash,status,ticket_ref,"
            "failure_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mission-statement-dispatch:test:" + accession, MISSION, "d" * 64, COMPANY, "TEST",
             "automation:test", "10-K", 1, 0, "{}", "e" * 64, "succeeded",
             "sec-financials:test", None, now, now),
        )
        cur.execute(
            "INSERT INTO coverage_mission_statement_filings("
            "ingest_id,dispatch_id,company_ref,cik,entity_name,accession,form,filed,report_date,"
            "line_count,source_record_refs_json,governance_ref,governance_hash,recorded_at,"
            "content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("statement-ingest:test:" + accession, "mission-statement-dispatch:test:" + accession,
             COMPANY, cik, "Synthetic issuer", accession, "10-K", "2025-10-31", "2025-09-30",
             0, "[]", "governance:test", "f" * 64, now, "a" * 64),
        )
    connection.execute("PRAGMA foreign_keys=ON")
    row = connection.execute(
        "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?", (review_ref,)
    ).fetchone()
    review_wire = {key: row[key] for key in (
        "review_id", "mission_version_ref", "company_ref", "source_ref", "document_ref",
        "discovered_document_ref", "state", "candidate_claim_version_ref", "rationale",
        "registered_by", "created_at", "updated_at",
    )}
    window = {
        "offset": 0, "context_ref": "document-context:annual-test",
        "context_hash": "1" * 64, "next_offset": None,
        "source_content_hash": source_bytes_hash(TEXT.encode()),
        "work_order_ref": "work-order:annual-read",
        "result_envelope_ref": "result-envelope:annual-read",
        "result_envelope_hash": "2" * 64, "status": "succeeded",
        "source_review_hash": content_hash(review_wire),
    }
    class WindowReader:
        @staticmethod
        def read_completion_receipt(**_kwargs):
            return dict(window)
    proof = DocumentReadCompletionAuthority(connection).record(
        review_id=review_ref, source_review_hash=content_hash(review_wire),
        actor_ref="automation:test", windows=[window], receipt_reader=WindowReader(),
        created_at=now,
    )
    return {
        "mission_version_ref": MISSION, "company_ref": COMPANY,
        "review_ref": review_ref,
        "document_read_proof_ref": proof["proof_id"],
        "issuer_cik": cik, "accession": accession,
    }


class RegisteredAnnualReportPlanTests(unittest.TestCase):
    def planner(self):
        fixture = planner_test_support.ResearchPlanTests(
            methodName="test_create_plan_is_exact_closed_four_step_tree"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def create_plan(self, fixture) -> dict:
        source = AnnualSourceHarness(fixture)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        registration = seed_core_registration(fixture, source)
        decision, records = fixture._selected_questions([(
            "Which customer segments and dependencies shape the business?",
            "Return exact annual-report passages with filing provenance",
        )])
        record = records[0]
        return fixture.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            **registration,
            query_terms=["customer segments", "outsourcing partners"],
            max_results=4,
            actor_ref="core:planner",
            idempotency_key="create-plan:registered-annual-report",
        )

    def test_v02_plan_is_closed_four_node_qualitative_tree(self) -> None:
        fixture = self.planner()
        created = self.create_plan(fixture)
        wire = fixture.plans.plan_version(created["plan_version_ref"])
        self.assertEqual(wire["schema_version"], "0.2")
        self.assertEqual(
            wire["execution_scope"]["operation"],
            REGISTERED_ANNUAL_REPORT_OPERATION,
        )
        self.assertEqual(wire["execution_scope"]["declared_side_effects"], [])
        self.assertEqual(len(wire["execution_scope"]["steps"]), 4)
        self.assertEqual(
            [step["stage"] for step in wire["execution_scope"]["steps"]],
            ["registered_filing_retrieval", "qualitative_model_draft",
             "independent_qualitative_verifier", "qualitative_candidate_staging"],
        )
        work = _plan_work_orders(wire)[0]
        self.assertEqual(work["declared_side_effects"], [])
        self.assertIn(ACCESSION, work["question"])
        self.assertNotIn("numeric", json.dumps(wire, sort_keys=True))
        schema = json.loads(
            (Path(__file__).parents[1] / "contracts" /
             "research-plan-version-0.2.schema.json").read_text()
        )
        self.assertEqual(
            wire["schema_version"], schema["properties"]["schema_version"]["const"]
        )
        configurable = json.loads(json.dumps(
            wire["execution_scope"]["parameters"]
        ))
        configurable["limits"].update({
            "max_query_terms": 101, "max_results": 101,
            "max_source_bytes": 128 * 1024 * 1024,
            "context_before_chars": 20_000, "context_after_chars": 20_000,
        })
        configurable["model_execution"]["draft"].update({
            "max_cost_usd": 101.0, "max_attempts": 6,
            "provider_retry": {
                "max_same_profile_retries": 4, "retry_backoff_seconds": 0,
            },
        })
        self.assertEqual(
            normalize_request(configurable)["model_execution"]["draft"]["max_attempts"],
            6,
        )
        configurable["model_execution"]["draft"]["max_cost_usd"] = float("inf")
        with self.assertRaisesRegex(RegisteredAnnualReportError, "max_cost_usd"):
            normalize_request(configurable)

    def test_annual_model_purposes_are_tiered_and_cockpit_selectable(self) -> None:
        expected = {
            "registered_annual_report_draft": "brain",
            "registered_annual_report_verifier": "verifier",
        }
        for purpose, tier in expected.items():
            self.assertEqual(tier_for(purpose), tier)
            self.assertIn(purpose, PURPOSE_MODEL_CONFIGS)
            self.assertIn(purpose, PURPOSE_LABELS)

    def test_submission_agent_accession_prefix_is_not_issuer_authority(self) -> None:
        fixture = self.planner()
        agent_accession = "0000789019-25-000001"
        source = AnnualSourceHarness(fixture, accession=agent_accession)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        registration = seed_core_registration(
            fixture, source, accession=agent_accession, cik=CIK
        )
        decision, records = fixture._selected_questions([(
            "What operating dependencies does the annual report identify?",
            "Return exact registered filing passages",
        )])
        record = records[0]
        created = fixture.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            **registration,
            query_terms=["dependencies"],
            actor_ref="core:planner",
        )
        self.assertEqual(
            fixture.plans.plan_version(created["plan_version_ref"])
            ["execution_scope"]["parameters"]["accession"],
            agent_accession,
        )

    def test_acquired_full_source_can_be_searched_without_whole_read_proof(self) -> None:
        fixture = self.planner()
        source = AnnualSourceHarness(fixture)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        registration = seed_core_registration(fixture, source)
        registration["document_read_proof_ref"] = None
        decision, records = fixture._selected_questions([(
            "Which operating dependencies are disclosed?",
            "Return exact annual-report passages",
        )])
        record = records[0]
        created = fixture.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"], **registration,
            query_terms=["outsourcing partners"], actor_ref="core:planner",
        )
        request = fixture.plans.plan_version(created["plan_version_ref"])[
            "execution_scope"
        ]["parameters"]
        self.assertIsNone(request["document_read_proof_ref"])
        proof = source.registry.search(request)
        self.assertEqual(proof["matches"][0]["matched_term"], "outsourcing partners")
        raw_hash = request["source_raw_hash"]
        (source.spool._objects / raw_hash[:2] / raw_hash).write_bytes(b"drift")
        with self.assertRaisesRegex(RegisteredAnnualReportError, "bytes differ"):
            source.registry.search(request)

    def test_unsettled_acquisition_cannot_create_targeted_plan(self) -> None:
        fixture = self.planner()
        source = AnnualSourceHarness(fixture)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        registration = seed_core_registration(fixture, source)
        registration["document_read_proof_ref"] = None
        review = fixture.store.connection.execute(
            "SELECT discovered_document_ref FROM coverage_mission_document_reviews "
            "WHERE review_id=?", (registration["review_ref"],),
        ).fetchone()
        authority = CoverageMissionAuthority(fixture.store)
        with authority._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status=? WHERE record_id=?",
                ("acquisition_launched", review["discovered_document_ref"]),
            )
        decision, records = fixture._selected_questions([(
            "Which dependencies are disclosed?", "Use an acquired annual report",
        )])
        record = records[0]
        with self.assertRaisesRegex(ResearchPlanValidationError, "authorities"):
            fixture.plans.create_registered_annual_report_plan(
                question_ref=record["question_ref"],
                question_version_ref=record["question_version_ref"],
                decision_ref=decision["id"], **registration,
                query_terms=["dependencies"], actor_ref="core:planner",
            )

    def test_exact_mission_budget_caps_paid_model_attempts(self) -> None:
        fixture = self.planner()
        source = AnnualSourceHarness(fixture)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        registration = seed_core_registration(fixture, source)
        decision, records = fixture._selected_questions([(
            "Which operating dependencies are disclosed?",
            "Use the acquired annual report",
        )])
        record = records[0]
        model = {
            "routing_policy_ref": "routing-policy:test:1",
            "credential_slot_refs": ["credential-slot:model:test"],
            "budget_db": "/tmp/annual-budget.sqlite",
            "budget_policy_ref": "budget-policy:test:1",
            "max_input_tokens": 1000, "max_output_tokens": 100,
            "max_cost_usd": 10.0, "max_seconds": 30,
            "max_elapsed_seconds": 3600,
            "max_attempts": 60, "provider_retry": None,
            "transport_retry": None,
        }
        with self.assertRaisesRegex(ResearchPlanValidationError, "mission"):
            fixture.plans.create_registered_annual_report_plan(
                question_ref=record["question_ref"],
                question_version_ref=record["question_version_ref"],
                decision_ref=decision["id"], **registration,
                query_terms=["dependencies"], draft_model_execution=model,
                verifier_model_execution=model, actor_ref="core:planner",
            )


class RegisteredAnnualReportExecutorTests(unittest.TestCase):
    @staticmethod
    def _model_profile(
        *, stage: str, capability: str, slot: str, family: str | None = None
    ) -> dict:
        wire = profile()
        wire.update({
            "profile_version_ref": f"model-profile-version:annual-{stage}:1",
            "id": f"profile:annual-{stage}", "model": f"annual-{stage}",
            "family": family or f"annual-{stage}", "credential_slot_ref": slot,
            "capabilities": [capability],
        })
        wire["availability"] = {
            "state": "available",
            "checked_at": "2026-08-15T08:00:00+00:00",
            "valid_until": "2026-08-24T22:00:00+00:00",
        }
        return wire

    @staticmethod
    def _model_policy(
        *, stage: str, profile_ids: list[str], capability: str, tier: str
    ) -> dict:
        wire = policy()
        wire.update({
            "policy_version_ref": f"routing-policy:annual-{stage}:1",
            "id": f"model-routing-policy:annual-{stage}",
        })
        wire["filters"] = dict(wire["filters"])
        wire["filters"]["allowed_profile_ids"] = list(profile_ids)
        wire["filters"]["family_independence_capabilities"] = (
            [capability] if stage == "verifier" else []
        )
        wire["fallback_chains"] = {"tiers": {tier: list(profile_ids)}}
        return wire

    def test_accepted_started_plan_executes_manifest_bound_retrieval(self) -> None:
        harness = PlanExecutorHarness(suffix="annual-report-host")
        self.addCleanup(harness.close)
        source = AnnualSourceHarness(harness.planner)
        self.addCleanup(source.close)
        harness.planner.plans.annual_report_registry = source.registry
        registration_args = seed_core_registration(harness.planner, source)

        decision, records = harness.planner._selected_questions([(
            "Which customers and outsourced operations shape the company?",
            "Return exact annual-report passages with accession and source hash",
        )])
        record = records[0]
        draft_capability = "capability:dalton:model:qualitative-research"
        verifier_capability = "capability:dalton:model:qualitative-verifier"
        draft_preferred = self._model_profile(
            stage="zz-draft-preferred", capability=draft_capability,
            slot="credential-slot:model:draft-preferred",
        )
        draft_backup = self._model_profile(
            stage="aa-draft-backup", capability=draft_capability,
            slot="credential-slot:model:draft-backup",
        )
        verifier_same_family = self._model_profile(
            stage="verifier-same-family", capability=verifier_capability,
            slot="credential-slot:model:verifier-same-family",
            family=draft_backup["family"],
        )
        verifier_independent = self._model_profile(
            stage="verifier-independent", capability=verifier_capability,
            slot="credential-slot:model:verifier-independent",
        )
        draft_policy = self._model_policy(
            stage="draft",
            profile_ids=[draft_preferred["id"], draft_backup["id"]],
            capability=draft_capability,
            tier="brain",
        )
        verifier_policy = self._model_policy(
            stage="verifier",
            profile_ids=[verifier_same_family["id"], verifier_independent["id"]],
            capability=verifier_capability,
            tier="verifier",
        )
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        created = harness.planner.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"],
            **registration_args,
            query_terms=["customers", "outsourcing partners"],
            draft_model_execution={
                "routing_policy_ref": draft_policy["policy_version_ref"],
                "credential_slot_refs": [
                    draft_preferred["credential_slot_ref"],
                    draft_backup["credential_slot_ref"],
                ],
                "budget_db": str(Path(harness.planner.temp.name) / "budget.sqlite"),
                "budget_policy_ref": "budget-policy:test:1",
                "max_input_tokens": 32_000, "max_output_tokens": 4_000,
                "max_cost_usd": 1.0, "max_seconds": 120, "max_attempts": 3,
                "max_elapsed_seconds": 3600,
                "provider_retry": retry,
                "transport_retry": None,
            },
            verifier_model_execution={
                "routing_policy_ref": verifier_policy["policy_version_ref"],
                "credential_slot_refs": [
                    verifier_same_family["credential_slot_ref"],
                    verifier_independent["credential_slot_ref"],
                ],
                "budget_db": str(Path(harness.planner.temp.name) / "budget.sqlite"),
                "budget_policy_ref": "budget-policy:test:1",
                "max_input_tokens": 48_000, "max_output_tokens": 4_000,
                "max_cost_usd": 1.0, "max_seconds": 120, "max_attempts": 1,
                "max_elapsed_seconds": 120,
                "provider_retry": None,
                "transport_retry": None,
            },
            actor_ref="core:planner",
            idempotency_key="create-plan:annual-executor",
        )
        harness.planner._approve(created, suffix="annual-executor")
        started = harness.planner._start(created, suffix="annual-executor")
        request = harness.planner.plans.plan_version(created["plan_version_ref"])[
            "execution_scope"
        ]["parameters"]
        registration = source.registry.verify_request(request)
        self.assertEqual(started["plan_state"], "started")
        self.assertEqual(len(started["task_tree"]), 4)
        router = ModelRouter(clock=harness.clock)
        self.addCleanup(router.close)
        for wire in (
            draft_preferred, draft_backup,
            verifier_same_family, verifier_independent,
        ):
            self.assertEqual(router.register_profile(wire)["status"], "fresh")
        for wire in (draft_policy, verifier_policy):
            self.assertEqual(router.register_policy(wire)["status"], "fresh")
        statement = "The company serves varied customers and depends on outsourcing partners."
        draft_output = {
            "schema_version": "0.1",
            "answer": statement,
            "candidate": {
                "normalized_statement": statement,
                "metric_or_aspect": "customer and operating dependencies",
                "period": "FY2025 annual report", "basis": "reported",
                "cited_match_indexes": [0, 1],
            },
        }
        verifier_output = {
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        }
        draft_adapter = ReturnedFailureSequenceAdapter(
            draft_output, ["RATE_LIMITED", "PROVIDER_INTERNAL_ERROR"]
        )
        draft_worker = RegisteredAnnualReportDraftWorker(
            scheduler=harness.scheduler(), router=router, adapter=draft_adapter,
            store=harness.core, observability=harness.observability, polish_worker=None,
            routing_policy_ref=draft_policy["policy_version_ref"],
            credential_slot_refs=(
                draft_preferred["credential_slot_ref"],
                draft_backup["credential_slot_ref"],
            ),
            provider_retry=retry,
            clock=harness.clock,
        )
        verifier_worker = RegisteredAnnualReportVerifierWorker(
            scheduler=harness.scheduler(), router=router, adapter=FakeAdapter(verifier_output),
            store=harness.core, observability=harness.observability, polish_worker=None,
            routing_policy_ref=verifier_policy["policy_version_ref"],
            credential_slot_refs=(
                verifier_same_family["credential_slot_ref"],
                verifier_independent["credential_slot_ref"],
            ),
            clock=harness.clock,
        )
        harness.executor = ResearchPlanExecutor(
            plan=harness.planner.plans,
            scheduler=harness.scheduler(),
            connector_records=harness.connector_records,
            coordinator=harness.coordinator,
            connectors=harness.connectors,
            catalog=harness.catalog,
            transport=harness.transport,
            resolver=harness.resolver,
            staging=harness.staging,
            clock=harness.clock,
            permissions=harness.permissions,
            policy_resolver=harness.authorities.policy,
            principal_ref="principal:worker-1",
            runner_environment_hash=harness.runner_environment_hash,
            annual_report_registry=source.registry,
            annual_report_draft_worker=draft_worker,
            annual_report_verifier_worker=verifier_worker,
            actor_ref=harness.actor_ref,
        )
        before_network = harness.connectors.connection.execute(
            "SELECT COUNT(*) FROM connector_invocations"
        ).fetchone()[0]

        outcomes = []
        for _ in range(6):
            outcomes.append(harness.executor.run_once(
                plan_version_ref=created["plan_version_ref"]
            ))
        self.assertEqual(
            [item["status"] for item in outcomes],
            ["admitted", "retryable", "retryable", "admitted", "admitted", "complete"],
            outcomes,
        )
        self.assertEqual(draft_adapter.selected_profile_ids, [
            draft_preferred["id"], draft_preferred["id"], draft_backup["id"],
        ])
        resolved_work = _resolved_plan_work_orders(
            harness.planner.plans.plan_version(created["plan_version_ref"]),
            harness.core.connection,
        )
        self.assertEqual(resolved_work[1]["metadata"]["provider_retry"], retry)
        draft_decisions = router.list_decisions(work_order_id=resolved_work[1]["id"])
        self.assertEqual(
            [item["selected_profile_version_ref"] for item in draft_decisions],
            [
                draft_preferred["profile_version_ref"],
                draft_preferred["profile_version_ref"],
                draft_backup["profile_version_ref"],
            ],
        )
        self.assertEqual(
            [item["decision_kind"] for item in draft_decisions],
            ["initial", "retry", "retry"],
        )

        verifier_decision = router.list_decisions(
            work_order_id=resolved_work[2]["id"]
        )[-1]
        self.assertEqual(
            verifier_decision["selected_profile_version_ref"],
            verifier_independent["profile_version_ref"],
        )
        same = next(
            item for item in verifier_decision["candidate_snapshot"]
            if item["profile_version_ref"]
            == verifier_same_family["profile_version_ref"]
        )
        self.assertIn("model_family_not_independent", same["rejection_reasons"])
        self.assertEqual(
            harness.connectors.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations"
            ).fetchone()[0],
            before_network,
        )
        work_orders = _resolved_plan_work_orders(
            harness.planner.plans.plan_version(created["plan_version_ref"]),
            harness.core.connection,
        )
        work = work_orders[0]
        formal = harness.scheduler().formal_result(work["id"])
        proof = formal["result_envelope"]["outputs"]
        self.assertEqual(proof["registration"], registration)
        self.assertEqual(proof["request"]["issuer_cik"], CIK)
        self.assertEqual(proof["request"]["accession"], ACCESSION)
        self.assertEqual(
            proof["request"]["source_content_hash"], source_bytes_hash(TEXT.encode())
        )
        self.assertEqual(
            {item["matched_term"] for item in proof["matches"]},
            {"customers", "outsourcing partners"},
        )
        self.assertEqual(formal["result_envelope"]["actual_side_effects"], [])
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)
        self.assertEqual(harness.staging_counts()["candidate_evidence_versions"], 1)
        tree = harness.coordinator.tree_status(created["plan_version_ref"])
        self.assertEqual(
            [node["attempt_state"] for node in tree["nodes"]], ["succeeded"] * 4
        )
        draft_proof = harness.scheduler().formal_result(work_orders[1]["id"])[
            "result_envelope"
        ]["outputs"]
        verifier_proof = harness.scheduler().formal_result(work_orders[2]["id"])[
            "result_envelope"
        ]["outputs"]
        self.assertNotEqual(draft_proof["model_family"], verifier_proof["model_family"])
        self.assertEqual(verifier_proof["output"]["verdict"], "pass")
        for table in ("evidence_versions", "claim_versions"):
            self.assertEqual(
                harness.core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )


    def test_production_cli_runs_started_plan_from_installed_configs_and_local_manifest(self) -> None:
        harness = PlanExecutorHarness(suffix="annual-report-cli")
        self.addCleanup(harness.close)
        source = AnnualSourceHarness(harness.planner)
        self.addCleanup(source.close)
        source.ticket_ref = "public-web-fetch:" + "a" * 24
        harness.planner.plans.annual_report_registry = source.registry
        registration = seed_core_registration(harness.planner, source)
        state = Path(harness.planner.temp.name)

        draft_capability = "capability:dalton:model:qualitative-research"
        verifier_capability = "capability:dalton:model:qualitative-verifier"
        draft_profile = self._model_profile(
            stage="cli-draft", capability=draft_capability,
            slot="credential-slot:model:cli-draft",
        )
        verifier_profile = self._model_profile(
            stage="cli-verifier", capability=verifier_capability,
            slot="credential-slot:model:cli-verifier",
        )
        for item in (draft_profile, verifier_profile):
            item["availability"]["valid_until"] = "2027-09-11T00:00:00+00:00"
        draft_policy = self._model_policy(
            stage="cli-draft", profile_ids=[draft_profile["id"]],
            capability=draft_capability, tier="brain",
        )
        verifier_policy = self._model_policy(
            stage="cli-verifier", profile_ids=[verifier_profile["id"]],
            capability=verifier_capability, tier="verifier",
        )
        router_path = state / "model-router.sqlite"
        with ModelRouter(router_path) as router:
            for item in (draft_profile, verifier_profile):
                router.register_profile(item)
            for item in (draft_policy, verifier_policy):
                router.register_policy(item)
        key_path = state / "broker.key"
        key_path.write_text("fixture-secret", encoding="utf-8")
        os.chmod(key_path, 0o600)
        common = {
            "model_router_db": str(router_path.resolve()),
            "broker_socket": str((state / "unused-broker.sock").resolve()),
            "broker_auth_key": str(key_path.resolve()),
            "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem",
            "budget_db": str((state / "budget.sqlite").resolve()),
            "budget_policy_ref": "thesis-impact-budget-policy-version:test:1",
            "call_budget": {"max_input_tokens": 50000, "max_output_tokens": 4000,
                            "max_cost_usd": 2.0, "timeout_seconds": 120},
            "run_budget": {"max_units": 1},
        }
        for path, route, slot in (
            (state / DRAFT_MODEL_CONFIG_NAME, draft_policy, draft_profile["credential_slot_ref"]),
            (state / VERIFIER_MODEL_CONFIG_NAME, verifier_policy, verifier_profile["credential_slot_ref"]),
        ):
            installed = {
                **common, "routing_policy_ref": route["policy_version_ref"],
                "credential_slot_refs": [slot],
            }
            if path.name == DRAFT_MODEL_CONFIG_NAME:
                installed["run_budget"] = {"max_units": 2, "max_seconds": 300}
                installed["provider_retry"] = {
                    "max_same_profile_retries": 1,
                    "retry_backoff_seconds": 7,
                }
            path.write_text(canonical_json(installed) + "\n", encoding="utf-8")
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
            decision_ref=decision["id"], **registration,
            query_terms=["customers", "outsourcing partners"],
            draft_model_execution=plan_model_execution(
                configs[0], "registered_annual_report_draft"
            ),
            verifier_model_execution=plan_model_execution(
                configs[1], "registered_annual_report_verifier"
            ),
            actor_ref="core:planner",
        )
        harness.planner._approve(created, suffix="annual-report-cli-v02")
        harness.planner._start(created, suffix="annual-report-cli-v02")

        ticket_dir = state / "fetches" / source.ticket_ref.split(":", 1)[1]
        ticket_dir.mkdir(parents=True)
        document_ref = f"sec:filing:{source.accession}"
        ticket = {"id": source.ticket_ref, "status": "succeeded",
                  "document_ref": document_ref}
        summary = {
            "url_ref": document_ref, "canonical_url": source.url,
            "manifest_ref": source.manifest["id"],
            "manifest_hash": source.manifest["content_hash"], "status": "succeeded",
        }
        for name, value in (("ticket.json", ticket), ("summary.json", summary),
                            ("manifest.json", source.manifest)):
            path = ticket_dir / name
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        web_governance = state / "web-fetch-governance.json"
        web_governance.write_text("{}\n", encoding="utf-8")
        os.chmod(web_governance, 0o600)
        staging = state / "review" / "candidate-staging.sqlite"
        staging.parent.mkdir()
        draft_fixture = state / "annual-draft.json"
        verifier_fixture = state / "annual-verifier.json"
        statement = "The company serves varied customers and depends on outsourcing partners."
        draft_fixture.write_text(canonical_json([
            {"fixture_provider_failure_code": "RATE_LIMITED"},
            {
                "schema_version": "0.1", "answer": statement,
                "candidate": {"normalized_statement": statement,
                              "metric_or_aspect": "customer and operating dependencies",
                              "period": "FY2025 annual report", "basis": "reported",
                              "cited_match_indexes": [0, 1]},
            },
        ]), encoding="utf-8")
        verifier_fixture.write_text(canonical_json({
            "schema_version": "0.1", "verdict": "pass",
            "verified_statement": statement, "findings": [],
        }), encoding="utf-8")
        governance = type("Governance", (), {
            "id": "connector-governance:annual-test:v1",
            "content_hash": "f" * 64,
            "approved": True,
            "approved_by": "human:test-owner",
        })()
        launcher_kwargs = {
            "state_dir": state, "governance_path": web_governance,
            "staging_path": staging,
            "mode_args": ("--rehearsal-approved-by", "human:test-owner"),
            "spool_dir": source.root / "spool",
            "web_fetch_governance_path": web_governance,
            "annual_report_draft_model_config_path": state / DRAFT_MODEL_CONFIG_NAME,
            "annual_report_verifier_model_config_path": state / VERIFIER_MODEL_CONFIG_NAME,
            "annual_report_draft_fixture_path": draft_fixture,
            "annual_report_verifier_fixture_path": verifier_fixture,
            "governance_loader": lambda _path: governance,
        }
        old_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
        self.addCleanup(
            lambda: (
                os.environ.pop("PYTHONPATH", None)
                if old_pythonpath is None
                else os.environ.__setitem__("PYTHONPATH", old_pythonpath)
            )
        )
        launcher = SecLaneLauncher(**launcher_kwargs)
        ticket = launcher.start_registered_annual_report(
            plan_version_ref=created["plan_version_ref"],
            actor_ref="human:test-owner",
        )
        ticket_path = state / "sec-lane-runs" / ticket["id"].split(":", 1)[1]
        wait_until = time.monotonic() + 30
        while (time.monotonic() < wait_until
               and not (ticket_path / "backoff-wait.json").is_file()):
            time.sleep(0.05)
        if not (ticket_path / "backoff-wait.json").is_file():
            self.fail(
                "annual child did not enter its provider retry backoff: "
                + json.dumps({
                    "ticket": launcher.status(ticket["id"]),
                    "works": [dict(row) for row in harness.core.connection.execute(
                        "SELECT work_order_id FROM scheduler_work_orders"
                    ).fetchall()],
                    "log": (ticket_path / "run.log").read_text(encoding="utf-8"),
                }, sort_keys=True)
            )

        first_pid = ticket["pid"]
        launcher.close()  # Writer shutdown terminates the real child in backoff.
        self.assertFalse(SecLaneLauncher._pid_alive(first_pid))

        mission_record_v2 = {
            "autonomy": {"automation_principal": "automation:test"},
            "budget": {"max_daily_paid_calls": 100, "max_daily_cost_usd": 1000.0,
                       "max_alphaengine_calls_24h": 0},
        }
        mission_v2 = MISSION + ":2"
        mission_authority = CoverageMissionAuthority(harness.core)
        with mission_authority._transaction() as cursor:
            cursor.execute(
                "INSERT INTO coverage_mission_versions("
                "mission_version_id,mission_ref,version_number,prior_version_id,industry_ref,"
                "playbook_version_ref,constitution_version_ref,mandate_version_ref,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mission_v2, "coverage-mission:annual-test", 2, MISSION, "industry:test",
                 "playbook:test", "constitution:test", "mandate:test",
                 canonical_json(mission_record_v2), content_hash(mission_record_v2),
                 "human:test", "2026-09-11T10:01:00.000000+00:00"),
            )
            cursor.execute(
                "UPDATE coverage_mission_pointer SET mission_version_id=?,version_number=?,"
                "content_hash=?,updated_at=? WHERE mission_ref=?",
                (mission_v2, 2, content_hash(mission_record_v2),
                 "2026-09-11T10:01:00.000000+00:00", "coverage-mission:annual-test"),
            )
        superseded = SecLaneLauncher(**launcher_kwargs)
        self.assertIsNone(superseded.wait(timeout=1))
        self.assertEqual(
            json.loads((ticket_path / "ticket.json").read_text())["resume_count"], 0
        )
        superseded.close()
        with mission_authority._transaction() as cursor:
            cursor.execute(
                "UPDATE coverage_mission_pointer SET mission_version_id=?,version_number=?,"
                "content_hash=?,updated_at=? WHERE mission_ref=?",
                (MISSION, 1, content_hash({
                     "autonomy": {"automation_principal": "automation:test"},
                     "budget": {"max_daily_paid_calls": 100,
                                "max_daily_cost_usd": 1000.0,
                                "max_alphaengine_calls_24h": 0},
                 }), "2026-09-11T10:02:00.000000+00:00",
                 "coverage-mission:annual-test"),
            )
        fresh = SecLaneLauncher(**launcher_kwargs)  # Writer restart auto-consumes the orphan.
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.wait(timeout=60), 0)
        recovered = fresh.status(ticket["id"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertGreaterEqual(recovered["resume_count"], 1)
        self.assertNotEqual(recovered["pid"], first_pid)
        result = recovered["summary"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"][-1]["status"], "complete")
        draft_work = _resolved_plan_work_orders(
            harness.planner.plans.plan_version(created["plan_version_ref"]),
            harness.core.connection,
        )[1]
        self.assertEqual(draft_work["budget"]["max_elapsed_seconds"], 300)
        attempts = {
            row[0] for row in harness.core.connection.execute(
                "SELECT attempt_number FROM scheduler_attempt_events "
                "WHERE work_order_id=?", (draft_work["id"],)
            ).fetchall()
        }
        self.assertEqual(attempts, {1, 2})
        from dalton_core.research_verification import CandidateStagingStore
        staged = CandidateStagingStore(staging)
        try:
            self.assertEqual(staged.counts()["candidate_claim_versions"], 1)
            self.assertEqual(staged.counts()["candidate_evidence_versions"], 1)
        finally:
            staged.close()

        pending_decision, pending_records = harness.planner._selected_questions([(
            "Which supplier risks are disclosed?", "Use the registered annual report",
        )])
        pending = harness.planner.plans.create_registered_annual_report_plan(
            question_ref=pending_records[0]["question_ref"],
            question_version_ref=pending_records[0]["question_version_ref"],
            decision_ref=pending_decision["id"], **registration,
            query_terms=["suppliers"],
            draft_model_execution=plan_model_execution(
                configs[0], "registered_annual_report_draft"
            ),
            verifier_model_execution=plan_model_execution(
                configs[1], "registered_annual_report_verifier"
            ),
            actor_ref="core:planner",
        )
        probe = {"plan_version_ref": pending["plan_version_ref"]}
        self.assertEqual(fresh._annual_resume_state(probe), (False, False))
        harness.planner._approve(pending, suffix="annual-pending-only")
        self.assertEqual(fresh._annual_resume_state(probe), (False, False))
    def test_review_progress_does_not_invalidate_append_only_source_proof(self) -> None:
        fixture = planner_test_support.ResearchPlanTests(
            methodName="test_create_plan_is_exact_closed_four_step_tree"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = AnnualSourceHarness(fixture)
        self.addCleanup(source.close)
        fixture.plans.annual_report_registry = source.registry
        args = seed_core_registration(fixture, source)
        decision, records = fixture._selected_questions([(
            "Which customers shape the company?", "Use the annual report",
        )])
        record = records[0]
        created = fixture.plans.create_registered_annual_report_plan(
            question_ref=record["question_ref"],
            question_version_ref=record["question_version_ref"],
            decision_ref=decision["id"], **args, query_terms=["customers"],
            actor_ref="core:planner",
        )
        request = fixture.plans.plan_version(created["plan_version_ref"])[
            "execution_scope"
        ]["parameters"]
        authority = CoverageMissionAuthority(fixture.store)
        with authority._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_document_reviews SET state=?,updated_at=? WHERE review_id=?",
                ("dismissed", "2026-09-11T11:00:00.000000+00:00", args["review_ref"]),
            )
        proof = source.registry.search(request)
        self.assertEqual(proof["request"]["document_read_proof_ref"], args["document_read_proof_ref"])
        self.assertFalse(hasattr(source.registry, "register"))


if __name__ == "__main__":
    unittest.main()
