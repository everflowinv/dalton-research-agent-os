import hashlib
import json
import sqlite3
import unittest

from dalton_core.scheduler import Scheduler
from dalton_core.agenda import AgendaStore
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.store import DaltonStore, canonical_json
from dalton_core.thesis_impact import (
    ThesisImpactAuthority,
    ThesisImpactConflict,
    ThesisImpactIneligible,
    ThesisImpactValidationError,
)


CREATED_AT = "2026-08-20T20:00:00+00:00"


def invocation(identifier, work_ref, input_refs, family):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": CREATED_AT,
        "work_order_ref": work_ref,
        "profile_ref": f"profile:{identifier}",
        "granularity": "task",
        "capability": "thesis_impact",
        "provider": f"provider-{family}",
        "model": f"model-{family}",
        "model_family": family,
        "input_refs": list(input_refs),
        "output_refs": [],
        "started_at": CREATED_AT,
        "completed_at": "2026-08-20T20:00:01+00:00",
        "usage": {"tokens": 1},
        "side_effects": [],
        "runtime_ref": "runtime:test",
        "actor_ref": "system:test",
        "parent_ref": None,
        "environment_hash": "a" * 64,
    }


def work_order(identifier, input_refs):
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "question": "Return the closed thesis impact JSON contract.",
        "requested_capabilities": ["thesis_impact"],
        "runtime_profile_ref": "runtime:test",
        "budget": {"max_seconds": 60},
        "idempotency_key": f"enqueue:{identifier}",
        "declared_side_effects": [],
        "status": "ready",
        "input_refs": list(input_refs),
        "metadata": {},
    }


class ThesisImpactTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.scheduler = Scheduler(":memory:")
        self.addCleanup(self.store.close)
        self.addCleanup(self.scheduler.close)
        self.authority = ThesisImpactAuthority(self.store, self.scheduler)

        self.seed_producer = invocation("invocation:seed-producer", "work:seed", [], "seed-a")
        self.seed_verifier = invocation("invocation:seed-verifier", "work:seed-v", [], "seed-b")
        self.store.stage_change(
            "change:thesis-v1",
            thesis_id="thesis:revenue",
            content={
                "statement": "Revenue growth should sustain earnings growth.",
                "mechanism": "Revenue growth sustains operating leverage.",
                "confidence": 0.6,
                "implied_expectation": "Quarterly revenue keeps growing year over year.",
                "claim_refs": [],
                "catalyst_refs": [],
                "falsifier_refs": [],
                "change_reason": "isolated test seed",
            },
            producer_invocation=self.seed_producer,
        )
        self.store.verify_change(
            "change:thesis-v1",
            verification_id="verification:thesis-v1",
            verifier_invocation=self.seed_verifier,
            verdict="pass",
            findings=[],
        )
        committed = self.store.commit(
            "change:thesis-v1", "verification:thesis-v1", "commit:thesis-v1"
        )
        self.thesis_ref = committed["version_id"]
        self.thesis_hash = committed["content_hash"]

        evidence = self.store.register_evidence({
            "evidence_ref": "evidence:revenue-growth",
            "source_type": "filing",
            "source_ref": "source:sec-10q",
            "artifact_refs": ["artifact:sec-10q"],
            "source_lineage": ["source:sec-10q"],
            "independence_group": "sec-public",
            "actor_ref": "system:test",
        })
        claim = self.store.register_claim({
            "claim_ref": "claim:revenue-growth",
            "subject_ref": "company:example",
            "metric_or_aspect": "quarterly_revenue_growth",
            "period": "2026-Q2",
            "basis": "same-filing year-over-year",
            "normalized_statement": "Quarterly revenue increased 18.3% year over year.",
            "claim_kind": "quantitative",
            "value": 18.3,
            "unit": "percent",
            "producer_invocation_refs": [self.seed_producer["id"]],
            "actor_ref": "system:test",
        })
        self.store.relate_evidence({
            "id": "relation:revenue-growth",
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"],
            "relation": "supports",
        })
        self.claim_ref = claim["claim_version_id"]
        self.claim_hash = claim["content_hash"]

    def complete_model(self, identifier, input_refs, output, family, metadata=None):
        work_ref = f"work:{identifier}"
        invocation_ref = f"invocation:{identifier}"
        inv = invocation(invocation_ref, work_ref, input_refs, family)
        work = work_order(work_ref, input_refs)
        if metadata is not None:
            work["metadata"] = dict(metadata)
        self.scheduler.enqueue(work)
        lease = self.scheduler.claim(f"worker:{identifier}", work_order_id=work_ref)
        text = canonical_json(output)
        result = {
            "schema_version": "0.1",
            "id": f"result:{identifier}",
            "created_at": "2026-08-20T20:00:02+00:00",
            "work_order_ref": work_ref,
            "invocation_ref": invocation_ref,
            "status": "succeeded",
            "outputs": {
                "text": text,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            "artifact_refs": [],
            "actual_side_effects": [],
            "usage_refs": [],
            "error": None,
            "metadata": {},
        }
        self.scheduler.complete(
            work_ref,
            lease["attempt"]["attempt_number"],
            f"worker:{identifier}",
            lease["lease_token"],
            result,
            idempotency_key=f"complete:{identifier}",
        )
        return inv, result["id"]

    def assessment_output(self, **overrides):
        output = {
            "schema_version": "0.1",
            "claim_version_ref": self.claim_ref,
            "claim_version_hash": self.claim_hash,
            "thesis_version_ref": self.thesis_ref,
            "thesis_version_hash": self.thesis_hash,
            "driver_statement": "Revenue growth sustains operating leverage.",
            "impact": "supports",
            "rationale": "The verified growth fact is directionally consistent with the driver.",
            "follow_up_question": None,
        }
        output.update(overrides)
        return output

    def record_assessment(self, family="impact-a", output=None):
        inv, result_ref = self.complete_model(
            "impact-producer",
            [self.claim_ref, self.thesis_ref],
            output or self.assessment_output(),
            family,
        )
        return self.authority.record_assessment(
            claim_version_ref=self.claim_ref,
            thesis_version_ref=self.thesis_ref,
            producer_invocation=inv,
            producer_result_envelope_ref=result_ref,
        ), inv

    def test_pass_is_eligible_but_never_mutates_thesis(self):
        before_pointer = dict(self.store.current_pointer("thesis:revenue"))
        recorded, _ = self.record_assessment()
        assessment = recorded["assessment"]
        verifier_output = {
            "schema_version": "0.1",
            "assessment_ref": assessment["id"],
            "assessment_hash": assessment["content_hash"],
            "verdict": "pass",
            "findings": [],
        }
        verifier, result_ref = self.complete_model(
            "impact-verifier",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            verifier_output,
            "impact-b",
        )
        verified = self.authority.verify_assessment(
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
            verifier_result_envelope_ref=result_ref,
        )
        eligible = self.authority.eligible_assessment(assessment["id"])

        self.assertEqual(recorded["status"], "fresh")
        self.assertEqual(verified["verification"]["verdict"], "pass")
        self.assertEqual(eligible["assessment"]["impact"], "supports")
        self.assertEqual(self.store.current_pointer("thesis:revenue"), before_pointer)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM thesis_impact_assessments").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self.assertEqual(
            self.scheduler.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

        duplicate = self.authority.record_assessment(
            claim_version_ref=self.claim_ref,
            thesis_version_ref=self.thesis_ref,
            producer_invocation=invocation(
                "invocation:impact-producer",
                "work:impact-producer",
                [self.claim_ref, self.thesis_ref],
                "impact-a",
            ),
            producer_result_envelope_ref="result:impact-producer",
        )
        self.assertEqual(duplicate["status"], "duplicate")

    def test_binding_mismatch_and_invalid_follow_up_fail_closed(self):
        bad = self.assessment_output(claim_version_hash="0" * 64)
        inv, result_ref = self.complete_model(
            "impact-producer", [self.claim_ref, self.thesis_ref], bad, "impact-a"
        )
        with self.assertRaises(ThesisImpactConflict):
            self.authority.record_assessment(
                claim_version_ref=self.claim_ref,
                thesis_version_ref=self.thesis_ref,
                producer_invocation=inv,
                producer_result_envelope_ref=result_ref,
            )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM thesis_impact_assessments").fetchone()[0],
            0,
        )

        invalid_follow_up = self.assessment_output(
            impact="supports",
            follow_up_question="This must be null for a supports judgment.",
        )
        invalid_inv, invalid_result_ref = self.complete_model(
            "impact-invalid-follow-up",
            [self.claim_ref, self.thesis_ref],
            invalid_follow_up,
            "impact-a",
        )
        with self.assertRaises(ThesisImpactValidationError):
            self.authority.record_assessment(
                claim_version_ref=self.claim_ref,
                thesis_version_ref=self.thesis_ref,
                producer_invocation=invalid_inv,
                producer_result_envelope_ref=invalid_result_ref,
            )

    def test_same_model_family_cannot_verify(self):
        recorded, _ = self.record_assessment(family="same-family")
        assessment = recorded["assessment"]
        verifier, result_ref = self.complete_model(
            "impact-verifier",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            {
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "pass",
                "findings": [],
            },
            "same-family",
        )
        with self.assertRaises(ThesisImpactIneligible):
            self.authority.verify_assessment(
                assessment_ref=assessment["id"],
                verifier_invocation=verifier,
                verifier_result_envelope_ref=result_ref,
            )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM thesis_impact_verifications").fetchone()[0],
            0,
        )

    def test_verifier_must_reread_exact_claim_and_thesis(self):
        recorded, _ = self.record_assessment()
        assessment = recorded["assessment"]
        verifier, result_ref = self.complete_model(
            "impact-verifier-incomplete",
            [assessment["id"]],
            {
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "pass",
                "findings": [],
            },
            "impact-b",
        )
        with self.assertRaises(ThesisImpactConflict):
            self.authority.verify_assessment(
                assessment_ref=assessment["id"],
                verifier_invocation=verifier,
                verifier_result_envelope_ref=result_ref,
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_verifications"
            ).fetchone()[0],
            0,
        )

        legacy_verifier, legacy_result_ref = self.complete_model(
            "impact-verifier-legacy-bypass",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            {
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "pass",
                "findings": [],
            },
            "impact-b",
            metadata={
                "phase": "verification",
                "verifier_output_schema_version": "0.2",
            },
        )
        with self.assertRaisesRegex(
            ThesisImpactValidationError, "WorkOrder contract"
        ):
            self.authority.verify_assessment(
                assessment_ref=assessment["id"],
                verifier_invocation=legacy_verifier,
                verifier_result_envelope_ref=legacy_result_ref,
            )

    def test_reject_is_durable_but_not_eligible(self):
        recorded, _ = self.record_assessment(
            output=self.assessment_output(
                impact="insufficient",
                rationale="One growth observation does not establish operating leverage.",
                follow_up_question="Did operating margin expand on the same revenue base?",
            )
        )
        assessment = recorded["assessment"]
        verifier, result_ref = self.complete_model(
            "impact-verifier",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            {
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "reject",
                "findings": [{"code": "OVERCLAIM", "message": "Rationale is not supported."}],
            },
            "impact-b",
        )
        verified = self.authority.verify_assessment(
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
            verifier_result_envelope_ref=result_ref,
        )
        self.assertEqual(verified["verification"]["verdict"], "reject")
        with self.assertRaises(ThesisImpactIneligible):
            self.authority.eligible_assessment(assessment["id"])

    def test_authority_rejects_v02_finding_contradicted_by_exact_binding(self):
        recorded, _ = self.record_assessment()
        assessment = recorded["assessment"]
        verifier, result_ref = self.complete_model(
            "impact-verifier-contradiction",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            {
                "schema_version": "0.2",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "reject",
                "findings": [{
                    "code": "binding_mismatch",
                    "severity": "high",
                    "detail": "The exact binding was incorrectly reported as mismatched.",
                    "expected_impact": None,
                }],
            },
            "impact-b",
            metadata={
                "phase": "verification",
                "verifier_output_schema_version": "0.2",
            },
        )
        with self.assertRaisesRegex(
            ThesisImpactValidationError, "already disproved by authority"
        ):
            self.authority.verify_assessment(
                assessment_ref=assessment["id"],
                verifier_invocation=verifier,
                verifier_result_envelope_ref=result_ref,
            )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_verifications"
            ).fetchone()[0],
            0,
        )

    def test_direct_writes_are_rejected(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO thesis_impact_assessments VALUES "
                "('a','c','h','t','h','supports','p','r','h','h','{}','h','now')"
            )

    def test_claim_without_thesis_creates_one_durable_follow_up(self):
        agenda = AgendaStore(self.store)
        backlog = ResearchQuestionBacklog(self.store)
        mandate = agenda.create_mandate(
            "mandate:coverage",
            objective="Maintain decision-useful company coverage.",
            scope_refs=["company:example"],
            constraints={"mode": "autonomous"},
            success_criteria={"formal_claims_mapped": True},
            effective_from="2026-08-20T00:00:00+00:00",
            effective_until="2026-09-20T00:00:00+00:00",
            actor_ref="human:owner",
            version_id="mandate-version:coverage-1",
            idempotency_key="mandate:coverage-1",
        )
        first = self.authority.route_claim(
            claim_version_ref=self.claim_ref,
            thesis_ref="thesis:missing-company-example",
            mandate_version_ref=mandate["id"],
            company_ref="company:example",
            backlog=backlog,
        )
        replay = self.authority.route_claim(
            claim_version_ref=self.claim_ref,
            thesis_ref="thesis:missing-company-example",
            mandate_version_ref=mandate["id"],
            company_ref="company:example",
            backlog=backlog,
        )
        self.assertEqual(first["status"], "follow_up_recorded")
        self.assertEqual(replay["backlog"]["status"], "duplicate")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM backlog_questions").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
