from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.errors import GateRejected
from dalton_core.research_context import build_claim_index
from dalton_core.research_review import (
    HumanReviewAuthority,
    ResearchReviewConflict,
    ResearchReviewError,
    ResearchReviewRejected,
)
from dalton_core.research_verification import (
    CandidateStagingStore,
    build_authority_source_material,
    build_candidate_claim,
    build_candidate_evidence,
    verify_authority_source_material,
    verify_numeric_spec,
)
from dalton_core.sec_authority_harness import SecAuthorityHarness, WIRE_WHEN
from dalton_core.store import canonical_json, content_hash
from tests.test_connector_runner import assert_wire_schema


REVIEWER = "human:tailscale-0123456789abcdef0123456789abcdef"


class ResearchReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.harness = SecAuthorityHarness()
        self.addCleanup(self.harness.close)
        self.staging_path = Path(self.temp.name) / "candidate-staging.sqlite"
        self.candidate = self._stage_candidate()
        self.review = HumanReviewAuthority(self.staging_path)
        self.addCleanup(self.review.close)

    def _receipt(self) -> dict:
        row = self.harness.coordinator_store.connection.execute(
            "SELECT receipt_json FROM research_completion_receipts"
        ).fetchone()
        return json.loads(row[0])

    def _stage_candidate(self) -> dict:
        resolver = self.harness.resolver()
        source_ref = self.harness.checkpoint["source_envelopes"][0]["ref"]
        resolved = resolver.resolve(
            source_ref, checkpoint_ref=self.harness.checkpoint["id"]
        )
        material = build_authority_source_material(resolved)
        source_bundle = verify_authority_source_material(
            material,
            resolver=resolver,
            checkpoint=self.harness.checkpoint,
            plan=self.harness.plan,
            context_pack=self.harness.context,
            step=self.harness.step,
            runner_request=self.harness.coordinator_request,
            receipt=self._receipt(),
        )
        period = {
            "kind": "fiscal_year",
            "label": "FY2025",
            "start": "2025-01-01T00:00:00.000000+00:00",
            "end": "2025-12-31T23:59:59.000000+00:00",
        }
        spec = {
            "schema_version": "0.1",
            "id": "numeric-spec:sec:review-count:1",
            "created_at": WIRE_WHEN,
            "operator": "identity",
            "inputs": [{
                "name": "filing_count", "value": "3", "unit": "records",
                "currency": None, "scale": "one", "period": period,
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": "/records", "extractor": "count",
            }],
            "output_value": "3", "output_unit": "records",
            "output_currency": None, "output_scale": "one",
            "output_period": period,
            "rounding": {"mode": "down", "digits": 0},
        }
        spec["content_hash"] = content_hash(spec)
        numeric_bundle = verify_numeric_spec(
            spec,
            checkpoint_ref=self.harness.checkpoint["id"],
            checkpoint_hash=self.harness.checkpoint["content_hash"],
            source_material=material,
            source_bundle=source_bundle,
        )
        evidence = build_candidate_evidence(
            material,
            source_bundle,
            candidate_evidence_ref="candidate-evidence:sec:review:1",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
            verification_mode="connector_authority",
        )
        claim = build_candidate_claim(
            evidence,
            source_bundle,
            spec,
            numeric_bundle,
            candidate_claim_ref="candidate-claim:sec:review:1",
            subject_ref="company:issuer-0000789019",
            metric_or_aspect="filing_count",
            basis="official-filing",
            normalized_statement="The bounded SEC result contains three 2025 10-Q filings.",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
        )
        staging = CandidateStagingStore(self.staging_path)
        try:
            staging.stage(
                checkpoint=self.harness.checkpoint,
                plan=self.harness.plan,
                context_pack=self.harness.context,
                step=self.harness.step,
                runner_request=self.harness.coordinator_request,
                receipt=self._receipt(),
                material=material,
                numeric_spec=spec,
                source_verification=source_bundle,
                numeric_verification=numeric_bundle,
                evidence=evidence,
                claim=claim,
                idempotency_key="stage:sec:review:1",
                verification_mode="connector_authority",
                authority_resolver=resolver,
            )
        finally:
            staging.close()
        return {"evidence": evidence, "claim": claim}

    @staticmethod
    def _semantics(claim: dict) -> dict:
        return {
            field: claim[field]
            for field in (
                "subject_ref", "metric_or_aspect", "period", "basis",
                "normalized_statement",
            )
        }

    def _decide(self, verdict: str = "accept", **overrides) -> dict:
        claim = self.candidate["claim"]
        params = {
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "verdict": verdict,
            "reviewed_semantics": self._semantics(claim),
            "rationale": "Checked the exact filing count and claim semantics.",
            "findings": ["source and statement refer to the same bounded result"],
            "reviewer_ref": REVIEWER,
            "source_event_ref": "research-review:test-event-1",
            "idempotency_key": "review:sec:1",
            "created_at": WIRE_WHEN,
            "proposed_revisions": None,
        }
        params.update(overrides)
        return self.review.decide(**params)

    def test_explicit_accept_promotes_losslessly_and_atomically(self) -> None:
        decision_result = self._decide()
        self.assertEqual(decision_result["commit_state"], "queued")
        self.assertEqual(len(self.review.pending_commits()), 1)
        bundle = self.review.pending_commits()[0]
        result = self.harness.core.commit_reviewed_candidate(
            **bundle, idempotency_key="reviewed-ledger:sec:1"
        )
        self.review.record_commit_result(
            bundle["decision"]["id"], created_at=WIRE_WHEN, ledger_result=result
        )
        self.assertEqual(result["status"], "fresh")
        duplicate = self.harness.core.commit_reviewed_candidate(
            **bundle, idempotency_key="reviewed-ledger:sec:1"
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.review.pending_commits(), [])

        evidence_row = self.harness.core.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
            (result["evidence_version_ref"],),
        ).fetchone()
        claim_projection = self.harness.core.get_claim(result["claim_version_ref"])
        evidence = json.loads(evidence_row[0])
        claim = claim_projection["claim"]
        assert_wire_schema(self, "human-review-decision.schema.json", bundle["decision"])
        assert_wire_schema(self, "evidence-version-v0.2.schema.json", evidence)
        assert_wire_schema(self, "claim-version-v0.2.schema.json", claim)
        self.assertEqual(evidence["schema_version"], "0.2")
        self.assertEqual(
            evidence["source_envelope_hash"],
            self.candidate["evidence"]["source_envelope_hash"],
        )
        self.assertEqual(
            evidence["source_verification_hash"],
            self.candidate["evidence"]["source_verification_hash"],
        )
        self.assertEqual(claim["schema_version"], "0.2")
        self.assertEqual(claim["value"], "3")
        self.assertIsNone(claim["currency"])
        self.assertEqual(claim["scale"], "one")
        self.assertEqual(claim["period"]["label"], "FY2025")
        self.assertEqual(claim_projection["status"], "proposed")
        self.assertEqual(len(claim_projection["evidence_relations"]), 1)
        self.assertEqual(
            claim_projection["evidence_relations"][0]["relation"], "supports"
        )
        index = build_claim_index(ledger=self.harness.core, created_at=WIRE_WHEN)
        self.assertEqual(len(index["entries"]), 1)
        self.assertEqual(index["entries"][0]["status"], "proposed")
        self.assertEqual(
            index["entries"][0]["period"], canonical_json(claim["period"])
        )

    def test_review_rejects_implicit_identity_and_semantic_rebinding(self) -> None:
        with self.assertRaises(ResearchReviewRejected):
            self._decide(reviewer_ref="automation:timeout")
        rebound = self._semantics(self.candidate["claim"])
        rebound["normalized_statement"] = "A different statement."
        with self.assertRaises(ResearchReviewConflict):
            self._decide(reviewed_semantics=rebound)
        self.assertEqual(self.review.counts()["human_review_decisions"], 0)

    def test_revise_stages_one_candidate_v2_and_replays(self) -> None:
        revised = self._decide(
            "revise",
            proposed_revisions={"normalized_statement": "Use a narrower statement."},
        )
        self.assertEqual(revised["commit_state"], "not_applicable")
        self.assertEqual(self.review.pending_commits(), [])
        first = self.review.consume_revision(revised["decision_ref"])
        replay = self.review.consume_revision(revised["decision_ref"])
        self.assertEqual(first["write_status"], "fresh")
        self.assertEqual(replay["write_status"], "duplicate")
        self.assertEqual(first["candidate_claim_ref"], replay["candidate_claim_ref"])
        candidates = self.review.list_candidates(limit=10)
        self.assertEqual(len(candidates), 2)
        v1, v2 = sorted(
            (item["claim"] for item in candidates),
            key=lambda item: item["version"],
        )
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["prior_version_ref"], v1["id"])
        self.assertEqual(v2["normalized_statement"], "Use a narrower statement.")
        self.assertEqual(v2["actor_ref"], "system:research-review-revision")
        for field in (
            "candidate_claim_ref", "subject_ref", "metric_or_aspect", "period",
            "basis", "claim_kind", "value", "unit", "currency", "scale",
            "candidate_evidence_refs", "source_verification_ref",
            "source_verification_hash", "numeric_spec_ref", "numeric_spec_hash",
            "numeric_verification_ref", "numeric_verification_hash",
        ):
            self.assertEqual(v2[field], v1[field], field)
        lineage = self.review.revision_lineage(v2["id"])
        self.assertEqual([item["id"] for item in lineage["claims"]], [v1["id"], v2["id"]])
        self.assertEqual(
            [item["id"] for item in lineage["revision_decisions"]],
            [revised["decision_ref"]],
        )
        self.assertIsNone(self.review.commit_event(revised["decision_ref"]))
        with self.assertRaises(ResearchReviewConflict):
            self._decide(
                "reject", idempotency_key="review:sec:2",
                source_event_ref="research-review:test-event-2",
            )

    def test_in_place_revision_rejects_source_numeric_or_period_rebinding(self) -> None:
        changed_period = dict(self.candidate["claim"]["period"])
        changed_period["label"] = "FY2024"
        with self.assertRaises(ResearchReviewRejected):
            self._decide(
                "revise",
                proposed_revisions={"period": changed_period},
            )
        self.assertEqual(self.review.counts()["human_review_decisions"], 0)
        self.assertEqual(len(self.review.list_candidates(limit=10)), 1)

    def test_formal_commit_failure_seams_leave_no_partial_ledger(self) -> None:
        self._decide()
        bundle = self.review.pending_commits()[0]
        for seam in ("after_evidence", "after_claim", "after_relation", "after_receipt"):
            with self.subTest(seam=seam), self.assertRaises(RuntimeError):
                self.harness.core.commit_reviewed_candidate(
                    **bundle, idempotency_key=f"reviewed-ledger:{seam}", fault_at=seam
                )
            counts = {
                table: self.harness.core.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "evidence_versions", "claim_versions", "evidence_relations",
                    "reviewed_candidate_commits",
                )
            }
            self.assertEqual(counts, {key: 0 for key in counts})

    def test_nonaccepted_decision_cannot_be_promoted(self) -> None:
        self._decide(
            "reject", rationale="The semantic statement overstates the source."
        )
        bundle = self.review.decision_bundle(
            self.review.list_candidates()[0]["decision"]["id"]
        )
        with self.assertRaises(GateRejected):
            self.harness.core.commit_reviewed_candidate(
                **bundle, idempotency_key="reviewed-ledger:rejected"
            )


if __name__ == "__main__":
    unittest.main()
