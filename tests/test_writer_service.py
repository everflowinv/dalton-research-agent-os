import json
import hashlib
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError, decode_frame
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    FEEDBACK_BRIDGE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    MAX_CONNECTIONS,
    RESEARCH_REVIEW_CONTROL_OPERATIONS,
    THESIS_IMPACT_OPERATIONS,
    VERIFIER_OPERATIONS,
    WORKER_OPERATIONS,
    Principal,
    WriterServerError,
    load_principals,
    write_token_config,
)
from tests.test_alphaengine_document_acquisition import (
    FakeAuthorityReader,
    FakePagePort,
)


def invocation(i, family="family", work_order="wo"):
    return {
        "schema_version": "0.1", "id": i, "created_at": "2026-01-01T00:00:00+00:00",
        "work_order_ref": work_order, "profile_ref": "profile-" + i, "granularity": "task",
        "capability": "research", "provider": "provider-" + i, "model": "model-" + i,
        "model_family": family, "runtime_ref": "runtime", "actor_ref": "actor",
        "usage": {"tokens": 1}, "input_refs": [], "output_refs": [],
        "started_at": "2026-01-01T00:00:00+00:00", "completed_at": None,
        "side_effects": [], "parent_ref": None,
    }


def thesis_payload(statement="s"):
    return {"statement": statement, "mechanism": "m", "confidence": "medium",
            "implied_expectation": "e", "claim_refs": [], "catalyst_refs": [],
            "falsifier_refs": [], "change_reason": "test"}


class WriterServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "private" / "coverage.db"
        self.sock = root / "run" / "writer.sock"
        self.tokens = root / "private" / "writer-tokens.json"
        self.transcript_spool_dir = root / "transcript-spool"
        self.worker_token = "worker-token-9f0c"
        self.verifier_token = "verifier-token-9f0c"
        self.core_token = "core-token-9f0c"
        self.review_token = "review-token-9f0c"
        self.thesis_impact_token = "thesis-impact-token-9f0c"
        self.governance_token = "governance-token-9f0c"
        write_token_config(self.tokens, [
            Principal("worker", self.worker_token, WORKER_OPERATIONS, frozenset({"producer"}), frozenset({"wo"})),
            Principal("verifier", self.verifier_token, VERIFIER_OPERATIONS, frozenset({"verifier"}), frozenset({"wo"})),
            Principal("core", self.core_token, CORE_OPERATIONS, unrestricted=True),
            Principal(
                "research-review-control", self.review_token,
                RESEARCH_REVIEW_CONTROL_OPERATIONS,
                actor_ref="bridge:tailscale-review",
            ),
            Principal(
                "thesis-impact",
                self.thesis_impact_token,
                THESIS_IMPACT_OPERATIONS,
                actor_ref="system:thesis-impact-model-worker",
            ),
            Principal(
                "coverage-governance", self.governance_token,
                HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref="human:coverage-owner",
            ),
        ])
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        self.proc = None
        self.start_server()
        self.addCleanup(self.stop_server)

    def start_server(self):
        self.proc = subprocess.Popen([
            sys.executable, "-m", "dalton_core.writer_server", "--db", str(self.db),
            "--scheduler", str(Path(self.tmp.name) / "scheduler.sqlite"),
            "--socket", str(self.sock), "--token-config", str(self.tokens),
            "--transcript-spool-dir", str(self.transcript_spool_dir),
        ], cwd=str(Path(__file__).parents[1]), env=self.env,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Hosted runners can take more than five seconds to import sqlite-heavy
        # authority modules under concurrent matrix load.  Keep the early-exit
        # check, but allow enough time for a healthy server to publish its UDS.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.sock.exists():
                return
            if self.proc.poll() is not None:
                self.fail(f"writer server exited with {self.proc.returncode}")
            time.sleep(0.02)
        self.fail("writer server did not create socket")

    def stop_server(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None
        self.tmp.cleanup()

    def stop_process_only(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        self.proc = None

    @property
    def worker(self):
        return WriterClient(str(self.sock), self.worker_token)

    @property
    def verifier(self):
        return WriterClient(str(self.sock), self.verifier_token)

    @property
    def core(self):
        return WriterClient(str(self.sock), self.core_token)

    @property
    def review(self):
        return WriterClient(str(self.sock), self.review_token)

    @property
    def thesis_impact(self):
        return WriterClient(str(self.sock), self.thesis_impact_token)

    @property
    def governance(self):
        return WriterClient(str(self.sock), self.governance_token)

    def test_permission_matrix_and_unknown_fields(self):
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.register_invocation(invocation("worker-1", "one"))
        self.core.register_invocation(invocation("producer", "one"))
        self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.stage_change(change_id="c2", thesis_id="t2", content=thesis_payload(), producer_invocation=invocation("producer-2", "one"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.create_policy(policy={"allowed_verdicts": ["pass"]})
        with self.assertRaises(RemoteError) as ctx:
            self.worker.call("stage_change", {"unknown": True})
        self.assertEqual(ctx.exception.code, "protocol_error")

    def test_thesis_impact_principal_is_scoped_and_empty_discovery_is_safe(self):
        self.assertEqual(
            self.thesis_impact.thesis_impact_targets({}, limit=10), []
        )
        with self.assertRaises(RemoteAuthorizationError):
            self.thesis_impact.stage_change(
                change_id="forbidden",
                thesis_id="thesis:forbidden",
                content=thesis_payload(),
                producer_invocation_id="missing",
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.thesis_impact_targets({}, limit=10)

    def test_coverage_admission_writes_require_authenticated_human(self):
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.register_driver_pack(
                "driver-pack:forbidden", industry_ref="industry:us-it-services"
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.core.register_driver_pack(
                "driver-pack:forbidden", industry_ref="industry:us-it-services"
            )

        mandate = self.governance.create_mandate(
            mandate_ref="mandate:us-it-services-acn",
            objective="Admit initial ACN coverage.",
            scope_refs=[
                "company:sec-cik:0001467373", "industry:us-it-services"
            ],
            constraints={"human_thesis_admission_required": True},
            success_criteria={"initial_thesis_count": 1},
            effective_from="2020-01-01T00:00:00+00:00",
            effective_until=None,
            activate=True,
            version_id="mandate-version:us-it-services-acn:1",
            idempotency_key="mandate:us-it-services-acn:1",
        )
        pack = self.governance.register_driver_pack(
            "driver-pack:us-it-services",
            industry_ref="industry:us-it-services",
            title="US IT Services Driver Pack",
            drivers=[{
                "driver_ref": "driver:bookings-conversion",
                "label": "Bookings conversion",
                "mechanism": "Bookings convert into revenue with a lag.",
                "metric_refs": ["metric:new-bookings"],
            }],
            metric_specs=[{
                "metric_ref": "metric:new-bookings",
                "label": "New bookings",
                "definition": "Contract value recorded as new bookings.",
                "unit": "USD",
                "periodicity": "quarterly",
                "preferred_source_refs": ["source:earnings-release"],
                "verification_kind": "numeric_and_semantic",
                "caveats": [],
            }],
            thesis_templates=[{
                "template_ref": "template:ai-reinvention-growth",
                "statement": "AI-led reinvention work can support growth.",
                "mechanism": "Bookings convert while productivity protects margin.",
                "driver_refs": ["driver:bookings-conversion"],
                "implied_expectation": "Bookings support later revenue growth.",
                "falsifier_refs": ["falsifier:bookings-conversion-breaks"],
            }],
            version_id="driver-pack-version:us-it-services:1",
            prior_version_ref=None,
            idempotency_key="driver-pack:us-it-services:1",
        )
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.register_industry_evidence_pack(
                "industry-evidence-pack:forbidden"
            )
        self.assertEqual(
            self.core.get_driver_pack(pack["id"])["content_hash"],
            pack["content_hash"],
        )
        self.core.register_invocation(invocation("industry-evidence-producer"))
        evidence = self.core.register_evidence(evidence={
            "evidence_ref": "evidence:acn:new-bookings",
            "source_type": "sec-filing-exhibit",
            "source_ref": "sec:acn:q3fy26",
            "retrieved_at": "2026-08-23T12:00:00+00:00",
            "source_lineage": ["sec:acn:q3fy26"],
            "independence_group": "issuer:acn:q3fy26",
        })
        claim = self.core.register_claim(
            claim={
                "claim_ref": "claim:acn:new-bookings", "subject_ref": "company:sec-cik:0001467373",
                "metric_or_aspect": "metric:new-bookings", "period": "FY2026Q3",
                "basis": "issuer-reported", "normalized_statement": "New bookings were reported.",
                "claim_kind": "quantitative", "value": 19.32, "unit": "USD_billion",
            },
            producer_invocation_refs=["industry-evidence-producer"],
        )
        relation = self.core.relate_evidence(relation={
            "id": "relation:acn:new-bookings",
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        industry_pack = self.governance.register_industry_evidence_pack(
            "industry-evidence-pack:us-it-services",
            industry_ref="industry:us-it-services", title="US IT Services Evidence Pack",
            as_of="2026-08-23T12:00:00+00:00",
            boundary={
                "definition": "Consulting and managed IT services providers.",
                "inclusion_rules": ["Material IT services revenue."],
                "exclusion_rules": ["Pure-play software vendors."],
            },
            coverage_universe=[{
                "company_ref": "company:sec-cik:0001467373", "ticker": "ACN",
                "role": "scaled global pure-play", "comparability_tier": "core",
            }],
            driver_pack_version_ref=pack["id"], driver_pack_version_hash=pack["content_hash"],
            evidence_bindings=[{
                "binding_ref": "binding:acn:new-bookings", "driver_ref": "driver:bookings-conversion",
                "metric_ref": "metric:new-bookings", "claim_version_ref": claim["claim_version_id"],
                "claim_version_hash": claim["content_hash"],
                "relation_refs": [{"ref": relation["relation_id"], "hash": relation["content_hash"]}],
            }],
            debates=[{
                "debate_ref": "debate:conversion", "question": "Will bookings convert?", "status": "open",
                "positions": [
                    {"label": "bookings support conversion", "stance": "supports", "claim_version_refs": [claim["claim_version_id"]]},
                    {"label": "conversion lag remains", "stance": "qualifies", "claim_version_refs": [claim["claim_version_id"]]},
                ],
            }],
            source_plan=[{"source_ref": "source:sec-edgar", "purpose": "Issuer filings", "priority": 1, "required": True}],
            report_contract={
                "industry_brief_sections": [
                    "boundary and universe", "driver scoreboard", "KPI evidence",
                    "KPI coverage gaps", "debates", "falsifiers", "open questions",
                ],
                "company_difference_fields": ["role", "watchpoints"],
            },
            version_id="industry-evidence-pack-version:us-it-services:1", prior_version_ref=None,
            idempotency_key="industry-evidence-pack:us-it-services:1",
        )
        overlay = self.governance.register_company_overlay(
            "company-overlay:acn", company_ref="company:sec-cik:0001467373",
            industry_ref="industry:us-it-services", title="Accenture overlay",
            as_of="2026-08-23T12:00:00+00:00", role="scaled global pure-play",
            evidence_pack_version_ref=industry_pack["id"],
            evidence_pack_version_hash=industry_pack["content_hash"],
            driver_views=[{
                "driver_ref": "driver:bookings-conversion", "stance": "neutral",
                "claim_version_refs": [{"ref": claim["claim_version_id"], "hash": claim["content_hash"]}],
                "model_input_version_refs": [], "differentiators": [],
                "metric_coverage": [{
                    "metric_ref": "metric:new-bookings", "status": "observed",
                    "claim_version_refs": [claim["claim_version_id"]],
                    "rationale": "The exact SEC filing evidence supports the bookings claim.",
                }],
                "watchpoints": ["Bookings conversion."],
            }],
            key_differences=["Global scale."], open_questions=["Conversion timing?"],
            falsifier_refs=["falsifier:bookings-conversion-breaks"], thesis_candidate_refs=[],
            version_id="company-overlay-version:acn:1", prior_version_ref=None,
            idempotency_key="company-overlay:acn:1",
        )
        self.assertEqual(
            industry_pack["content_hash"],
            self.core.get_industry_evidence_pack(industry_pack["id"])["content_hash"],
        )
        self.assertEqual(
            overlay["content_hash"], self.core.get_company_overlay(overlay["id"])["content_hash"]
        )
        snapshot = self.core.industry_brief_snapshot(industry_pack["id"], [overlay["id"]])
        rendered = self.core.render_industry_brief_markdown(
            industry_pack["id"], [overlay["id"]]
        )
        self.assertEqual(snapshot["content_hash"], rendered["snapshot_hash"])
        self.assertIn("## KPI evidence", rendered["body"])
        self.assertIn("New bookings were reported.", rendered["body"])
        self.assertTrue(self.core.industry_research_integrity_report()["ok"])
        candidate = self.governance.propose_thesis_admission(
            candidate_id="thesis-admission-candidate:acn:1",
            thesis_ref="thesis:acn:ai-reinvention-growth",
            company_ref="company:sec-cik:0001467373",
            industry_ref="industry:us-it-services",
            template_ref="template:ai-reinvention-growth",
            driver_refs=["driver:bookings-conversion"],
            mandate_version_ref=mandate["id"],
            mandate_version_hash=mandate["content_hash"],
            driver_pack_version_ref=pack["id"],
            driver_pack_version_hash=pack["content_hash"],
            content={
                "statement": "AI demand can sustain Accenture growth.",
                "mechanism": "Bookings convert while productivity protects margin.",
                "confidence": "medium",
                "implied_expectation": "Bookings support later revenue growth.",
                "claim_refs": [],
                "catalyst_refs": ["catalyst:quarterly-results"],
                "falsifier_refs": ["falsifier:bookings-conversion-breaks"],
                "change_reason": "Initial human-reviewed ACN admission.",
            },
            idempotency_key="thesis-admission-candidate:acn:1",
        )
        admitted = self.governance.decide_thesis_admission(
            candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"],
            verdict="admit",
            rationale="Drivers and falsifiers are explicit.",
            decision_id="thesis-admission-decision:acn:1",
            idempotency_key="thesis-admission-decision:acn:1",
        )
        self.assertEqual(admitted["thesis_version"]["authority_kind"], "human_admission")
        self.assertEqual(
            self.core.get_thesis_admission_candidate(candidate["id"])["id"],
            candidate["id"],
        )
        self.assertEqual(
            self.core.get_thesis_admission_decision(
                "thesis-admission-decision:acn:1"
            )["verdict"],
            "admit",
        )

    def test_transcript_correction_rpc_requires_human_governance(self):
        original = "Revenue grew 3% in local currency. r ight"
        spool = RawSpool(self.transcript_spool_dir, max_total_bytes=1_000_000_000)
        plan = build_alphaengine_document_acquisition_plan(
            document_ref="alphaengine-doc:130000095976806",
            created_at="2026-08-24T12:00:00+00:00",
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=len(original),
        )
        manifest_authority = FakeAuthorityReader()
        manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=FakePagePort(
                plan=plan, pages=[original], authority=manifest_authority,
                spool=spool,
            ),
            authority_reader=manifest_authority,
            spool=spool,
        ).execute()
        flag_start = original.index("r ight")
        params = {
            "correction_set_ref": "transcript-correction-set:acn-q3fy26",
            "source_manifest": manifest,
            "review_scope": "targeted_flags",
            "corrections": [{
                "source_start": flag_start,
                "source_end": len(original),
                "source_sha256": hashlib.sha256(
                    original[flag_start:].encode("utf-8")
                ).hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved",
                "replacement_text": None,
                "rationale": "Flag outside the exact citation span.",
                "evidence_bindings": [],
            }],
            "prior_version_ref": None,
        }
        pending = self.review.transcript_correction_review_state(
            source_manifest=manifest,
            correction_set_ref=params["correction_set_ref"],
            source_start=0,
            source_end=original.index(". ") + 1,
        )
        self.assertEqual(pending["status"], "pending_human_review")
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.transcript_correction_review_state(
                source_manifest=manifest,
                correction_set_ref=params["correction_set_ref"],
                source_start=0,
                source_end=original.index(". ") + 1,
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.publish_transcript_correction_set(**params)
        with self.assertRaises(RemoteAuthorizationError):
            self.core.publish_transcript_correction_set(**params)
        correction_set = self.governance.publish_transcript_correction_set(**params)
        self.assertEqual(correction_set["actor_ref"], "human:coverage-owner")
        citation = self.governance.bind_transcript_claim_citation(
            correction_set_version_ref=correction_set["id"],
            correction_set_version_hash=correction_set["content_hash"],
            source_manifest=manifest,
            source_start=0,
            source_end=original.index(". ") + 1,
        )
        self.assertTrue(citation["claim_eligible"])
        self.assertEqual(citation["unresolved_correction_indexes"], [])
        state = self.review.transcript_correction_review_state(
            source_manifest=manifest,
            correction_set_ref=params["correction_set_ref"],
            source_start=0,
            source_end=original.index(". ") + 1,
        )
        self.assertEqual(state["status"], "claim_eligible")
        self.assertEqual(state["correction_set"]["id"], correction_set["id"])
        self.assertEqual(state["citation_binding"]["id"], citation["id"])

    def test_model_input_candidate_requires_worker_and_admission_requires_human(self):
        evidence = self.core.register_evidence(
            evidence={
                "evidence_ref": "evidence:acn:revenue:2026q1",
                "source_type": "filing",
                "source_ref": "sec:acn:10q:2026q1",
                "retrieved_at": "2026-08-23T12:00:00+00:00",
                "source_lineage": ["sec:acn:10q:2026q1"],
                "independence_group": "sec:acn:10q:2026q1",
            }
        )
        payload = {
            "schema_version": "0.1",
            "metric_ref": "metric:revenue",
            "subject_ref": "company:acn",
            "business_line_ref": None,
            "period": {
                "start": "2026-01-01", "end": "2026-03-31",
                "calendar": "company:fiscal", "kind": "quarter",
            },
            "unit": "million",
            "currency": "USD",
            "value": "100",
            "source_authorities": [{
                "authority_kind": "evidence_version",
                "version_ref": evidence["evidence_version_id"],
                "content_hash": evidence["content_hash"],
            }],
        }
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.propose_model_input(
                candidate_id="candidate:forbidden", input_kind="actual",
                model_input_ref="input:forbidden", prior_version_ref=None,
                payload=payload, idempotency_key="candidate:forbidden",
            )
        candidate = self.worker.propose_model_input(
            candidate_id="candidate:acn:revenue:2026q1",
            input_kind="actual",
            model_input_ref="input:acn:revenue:2026q1",
            prior_version_ref=None,
            payload=payload,
            idempotency_key="candidate:acn:revenue:2026q1",
        )["candidate"]
        self.assertEqual("worker", candidate["proposed_by"])
        with self.assertRaises(RemoteAuthorizationError):
            self.core.decide_model_input(
                decision_id="decision:forbidden", candidate_id=candidate["id"],
                candidate_hash=candidate["content_hash"], verdict="admit",
                rationale="not human", findings=[], version_id="version:forbidden",
                idempotency_key="decision:forbidden",
            )
        admitted = self.governance.decide_model_input(
            decision_id="decision:acn:revenue:2026q1",
            candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"],
            verdict="admit",
            rationale="Filing authority checked.",
            findings=[],
            version_id="input-version:acn:revenue:2026q1:1",
            idempotency_key="decision:acn:revenue:2026q1",
        )
        self.assertEqual("human:coverage-owner", admitted["decision"]["reviewer_ref"])
        self.assertEqual(
            admitted["version"],
            self.core.current_model_input("input:acn:revenue:2026q1"),
        )
        self.assertEqual(
            candidate,
            self.core.get_model_input_candidate(candidate["id"]),
        )
        self.assertTrue(self.core.model_input_integrity_report()["ok"])

    def test_core_records_closed_model_run_and_reconciliation_over_rpc(self):
        candidate = self.worker.propose_model_input(
            candidate_id="candidate:scenario:base",
            input_kind="scenario",
            model_input_ref="scenario:base",
            prior_version_ref=None,
            payload={
                "schema_version": "0.1", "scenario_ref": "scenario:base",
                "label": "Base", "description": "Reviewed base case",
                "base_scenario_version_ref": None,
                "base_scenario_version_hash": None,
                "owner_ref": "human:coverage-owner",
            },
            idempotency_key="candidate:scenario:base",
        )["candidate"]
        scenario = self.governance.decide_model_input(
            decision_id="decision:scenario:base", candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"], verdict="admit",
            rationale="Base scenario approved.", findings=[],
            version_id="input-version:scenario:base:1",
            idempotency_key="decision:scenario:base",
        )["version"]
        run = self.core.record_model_run(
            version_id="model-run-version:rpc:1", model_run_ref="model-run:rpc",
            prior_version_ref=None, scenario_version_ref=scenario["id"],
            scenario_version_hash=scenario["content_hash"],
            input_bindings=[{
                "binding_ref": "scenario", "role": "scenario",
                "version_ref": scenario["id"],
                "version_hash": scenario["content_hash"],
            }],
            formula_version_ref="formula:rpc:1",
            formula_version_hash="0" * 64,
            status="completed",
            outputs=[{
                "output_ref": "output:rpc", "output_kind": "metric",
                "metric_ref": "metric:test",
                "period": {
                    "start": "2026-01-01", "end": "2026-03-31",
                    "calendar": "company:fiscal", "kind": "quarter",
                },
                "unit": "count", "currency": None, "value": "1",
                "authority_bindings": [],
            }],
            errors=[], started_at="2026-08-23T12:00:00+00:00",
            completed_at="2026-08-23T12:00:01+00:00",
            idempotency_key="model-run:rpc:1",
        )["model_run"]
        checks = [{
            "check_kind": kind, "status": "pass", "details": "checked",
            "authority_bindings": [{
                "authority_kind": "model_run_version", "version_ref": run["id"],
                "content_hash": run["content_hash"],
            }],
        } for kind in (
            "financial_statement", "unit_currency", "period_calendar",
            "share_count", "actual_override", "source_revision",
        )]
        reconciliation = self.core.record_model_reconciliation(
            reconciliation_id="reconciliation:rpc:1",
            model_run_version_ref=run["id"],
            model_run_version_hash=run["content_hash"],
            checks=checks, idempotency_key="reconciliation:rpc:1",
        )["reconciliation"]
        self.assertEqual("core", run["actor_ref"])
        self.assertEqual("pass", reconciliation["verdict"])
        self.assertEqual(
            [reconciliation], self.core.get_model_reconciliations(run["id"])
        )

    def test_agenda_context_operations_are_core_only_and_closed(self):
        # Materializing an Agenda context reads mandates and perception
        # snapshots.  Only the core principal may ask for it, and neither
        # operation may accept an out-of-contract field.
        for client in (self.worker, self.verifier, self.review):
            with self.assertRaises(RemoteAuthorizationError):
                client.materialize_agenda_context(cycle_id="agenda-cycle:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.register_perception_snapshot(snapshot={}, actor_ref="core")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_agenda_mandate_version(version_id="mandate-version:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_agenda_policy_version(version_id="agenda-policy-version:1")
            with self.assertRaises(RemoteAuthorizationError):
                client.get_perception_snapshot(snapshot_id="perception:1")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call("materialize_agenda_context", {"snapshot": {}})
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "materialize_agenda_context",
                {
                    "cycle_id": "agenda-cycle:absent",
                    "max_tokens": 100,
                    "max_bytes": 1000,
                    "created_at": "2099-01-01T00:00:00Z",
                },
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "get_perception_snapshot",
                {"snapshot_id": "perception:1", "snapshot": {}},
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        with self.assertRaises(RemoteError) as ctx:
            self.core.call(
                "register_perception_snapshot",
                {"snapshot": {}, "actor_ref": "core", "database_path": "/tmp/x"},
            )
        self.assertEqual(ctx.exception.code, "protocol_error")
        # A caller may name a cycle and a budget; it may not smuggle a body.
        with self.assertRaises(RemoteError) as ctx:
            self.core.materialize_agenda_context(
                cycle_id="agenda-cycle:absent", max_tokens=100, max_bytes=1000
            )
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(RemoteError) as ctx:
            self.core.register_perception_snapshot(
                snapshot={"schema_version": "0.1"}, actor_ref="core"
            )
        self.assertEqual(ctx.exception.code, "rejected")

    def test_stage_verify_commit_uses_separate_scopes(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("verifier", "two"))
        staged = self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        self.assertEqual(staged["status"], "staged")
        verified = self.verifier.verify_change(change_id="c1", verification_id="v1", verifier_invocation_id="verifier", verdict="pass", findings=[])
        self.assertEqual(verified["status"], "verified")
        committed = self.core.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        self.assertEqual(committed["status"], "fresh")
        self.assertEqual(self.core.current_pointer("t1")["version_id"], committed["version_id"])

    def test_scoped_actor_is_injected_and_spoof_is_rejected(self):
        self.core.register_invocation(invocation("producer", "one"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(
                change_id="spoof", thesis_id="t1", content=thesis_payload(),
                producer_invocation_id="producer", actor_id="human:owner",
            )
        self.worker.stage_change(
            change_id="c1", thesis_id="t1", content=thesis_payload(),
            producer_invocation_id="producer",
        )
        staged_event = next(event for event in self.core.list_events("t1") if event["event_type"] == "staged")
        self.assertEqual(staged_event["actor_id"], "worker")

        self.core.register_invocation(invocation("verifier", "two"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(
                change_id="c1", verification_id="spoof-v", verifier_invocation_id="verifier",
                verdict="pass", findings=[], actor_id="human:owner",
            )

    def test_core_reads_and_socket_permissions(self):
        self.assertEqual(stat.S_IMODE(self.sock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o600)
        self.assertEqual(self.core.active_policy()["policy_version_id"], "policy-1")
        client_vars = vars(self.worker)
        self.assertNotIn("db_path", client_vars)
        self.assertNotIn("database", client_vars)
        self.assertNotIn("coverage.db", repr(self.worker))
        self.assertNotIn(self.worker_token, repr(self.worker))

    def test_invalid_token_and_malformed_frame_are_sanitized(self):
        bad = WriterClient(str(self.sock), "wrong-token")
        with self.assertRaises(RemoteAuthorizationError) as ctx:
            bad.active_policy()
        self.assertEqual(ctx.exception.code, "forbidden")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(str(self.sock))
            raw.sendall(b"not-json\n")
            response = decode_frame(raw.makefile("rb").readline())
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "protocol_error")
        self.assertNotIn(str(self.db), json.dumps(response))
        self.assertNotIn("Traceback", json.dumps(response))

    def test_restart_preserves_database_but_not_client_db_access(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("verifier", "two"))
        self.worker.stage_change(change_id="c1", thesis_id="t1", content=thesis_payload(), producer_invocation_id="producer")
        self.verifier.verify_change(change_id="c1", verification_id="v1", verifier_invocation_id="verifier", verdict="pass", findings=[])
        self.core.commit(change_id="c1", verification_id="v1", idempotency_key="k1")
        self.stop_process_only()
        self.start_server()
        self.assertEqual(self.core.current_pointer("t1")["version_number"], 1)

    def test_inline_foreign_and_wrong_work_order_subjects_are_rejected(self):
        self.core.register_invocation(invocation("producer", "one"))
        self.core.register_invocation(invocation("other", "one", work_order="not-assigned"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="inline", thesis_id="t", content=thesis_payload(), producer_invocation_id="producer", producer_invocation=invocation("producer", "attacker"))
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="foreign", thesis_id="t", content=thesis_payload(), producer_invocation_id="other")
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.stage_change(change_id="missing", thesis_id="t", content=thesis_payload(), producer_invocation_id="not-registered")

        self.core.register_invocation(invocation("verifier", "two"))
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(change_id="missing", verification_id="v-inline", verifier_invocation_id="verifier", verifier_invocation=invocation("verifier", "attacker"), verdict="pass", findings=[])
        with self.assertRaises(RemoteAuthorizationError):
            self.verifier.verify_change(change_id="missing", verification_id="v-foreign", verifier_invocation_id="producer", verdict="pass", findings=[])

    def test_token_config_rejects_non_owner_permissions(self):
        os.chmod(self.tokens, 0o644)
        # This test uses the loader directly and does not restart the running
        # server, so it cannot accidentally alter the live writer authority.
        with self.assertRaises(WriterServerError):
            load_principals(self.tokens)

    def test_human_actor_requires_nonempty_normalized_subject(self):
        with self.assertRaises(WriterServerError):
            Principal("governance", "bad", frozenset({"active_policy"}), actor_ref="human:")
        bad_config = Path(self.tmp.name) / "private" / "bad-human.json"
        bad_config.write_text(json.dumps({
            "schema_version": "0.1",
            "principals": [{
                "principal_id": "governance", "token": "bad-human-token",
                "operations": ["active_policy"], "allowed_invocation_refs": [],
                "work_order_refs": [], "unrestricted": False, "actor_ref": "human:",
            }],
        }), encoding="utf-8")
        os.chmod(bad_config, 0o600)
        with self.assertRaises(WriterServerError):
            load_principals(bad_config)

    def test_scoped_feedback_principals_require_exact_actor_and_operations(self):
        for principal_id, actor_ref in (
            ("dashboard-control", "bridge:tailscale-dashboard"),
            ("agenda-timeout", "automation:agenda-timeout"),
        ):
            valid = Path(self.tmp.name) / "private" / f"{principal_id}.json"
            write_token_config(valid, [
                Principal(
                    principal_id, f"{principal_id}-token", FEEDBACK_BRIDGE_OPERATIONS,
                    actor_ref=actor_ref,
                )
            ])
            self.assertIn(principal_id, load_principals(valid))
            invalid = Path(self.tmp.name) / "private" / f"{principal_id}-invalid.json"
            write_token_config(invalid, [
                Principal(
                    principal_id, f"{principal_id}-bad-token", FEEDBACK_BRIDGE_OPERATIONS,
                    actor_ref="bridge:openclaw-discord",
                )
            ])
            with self.assertRaises(WriterServerError):
                load_principals(invalid)

    def test_research_review_principal_is_exact_and_rejects_automation(self):
        with self.assertRaises(RemoteAuthorizationError):
            self.worker.commit_reviewed_candidate(
                decision={}, evidence={}, claim={}, idempotency_key="forbidden"
            )
        with self.assertRaises(RemoteAuthorizationError):
            self.review.commit_reviewed_candidate(
                decision={
                    "reviewer_ref": "automation:timeout", "verdict": "accept",
                    "authorization": "explicit_human_review", "source": "tailscale_review",
                    "source_event_ref": "research-review:bad",
                },
                evidence={}, claim={}, idempotency_key="automation",
            )
        invalid = Path(self.tmp.name) / "private" / "research-review-invalid.json"
        write_token_config(invalid, [
            Principal(
                "research-review-control", "bad-review-token",
                RESEARCH_REVIEW_CONTROL_OPERATIONS,
                actor_ref="bridge:tailscale-dashboard",
            )
        ])
        with self.assertRaises(WriterServerError):
            load_principals(invalid)

    def test_partial_frame_does_not_block_valid_client_and_connection_limit(self):
        partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(partial.close)
        partial.connect(str(self.sock))
        partial.sendall(b'{"protocol_version":"0.1"')
        started = time.monotonic()
        self.assertEqual(self.core.active_policy()["policy_version_id"], "policy-1")
        self.assertLess(time.monotonic() - started, 0.75)
        partial.close()

        blockers = []
        try:
            for _ in range(MAX_CONNECTIONS):
                blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                blocker.settimeout(0.5)
                blocker.connect(str(self.sock))
                blocker.sendall(b"{")
                blockers.append(blocker)
                time.sleep(0.02)
            extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(extra.close)
            extra.settimeout(0.5)
            extra.connect(str(self.sock))
            extra.sendall(b"{")
            try:
                closed = extra.recv(1) == b""
            except (ConnectionResetError, BrokenPipeError):
                closed = True
            self.assertTrue(closed, "connection beyond MAX_CONNECTIONS was not rejected")
        finally:
            for blocker in blockers:
                blocker.close()


if __name__ == "__main__":
    unittest.main()
