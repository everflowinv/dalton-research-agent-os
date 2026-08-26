"""ADR-0003 option B: qualitative transcript candidates.

End-to-end on the Core-hosted AlphaEngine harness: ACN Q3 FY2026 transcript
-> correction set + eligible citation -> ``stage_transcript_qualitative_candidate``
-> ``HumanReviewAuthority.decide(accept)`` -> ``commit_reviewed_candidate``.
Plus adversarial shapes that must fail closed.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_auto_commit import (
    ResearchAutoCommitRejected,
    authorize_policy_candidate,
)
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_review_control import ResearchReviewControlPlane
from dalton_core.research_verification import (
    TRANSCRIPT_CORE_AUTHORITY_MODE,
    CandidateStagingStore,
    ResearchVerificationConflict,
    ResearchVerificationError,
    VerificationRejected,
    validate_candidate_claim,
)
from dalton_core.store import GateRejected, content_hash
from dalton_core.transcript_candidate_staging import (
    TranscriptCoreAuthorityError,
    TranscriptCoreAuthorityResolver,
    build_transcript_qualitative_candidate,
    stage_transcript_qualitative_candidate,
)
from dalton_core.transcript_correction import (
    TRANSCRIPT_EVIDENCE_SOURCE_TYPE,
    TranscriptCorrectionAuthority,
    TranscriptCorrectionConflict,
)
from tests.test_alphaengine_core_acquisition import (
    DIGEST,
    DOCUMENT,
    DOCUMENT_REF,
    PAGE_ONE,
    REVIEWER,
    CoreHarness,
    FakeDocumentHandle,
    approved_governance,
)
from tests.test_connector import assert_wire_schema


SUBJECT = "company:sec-cik:0001467373"
ASPECT = "aspect:new-bookings-direction-local-currency"
PERIOD = {
    "kind": "fiscal_quarter", "label": "FY2026Q3",
    "start": "2026-03-01T00:00:00.000000+00:00",
    "end": "2026-05-31T23:59:59.000000+00:00",
}
BASIS = "management-reported"
STATEMENT = (
    "Accenture management said Q3 FY2026 new bookings declined year over year "
    "in local currency; the numeric growth rate is not asserted by this claim."
)
PRODUCER = "system:transcript-candidate-stager"


class QualitativeTranscriptHarness:
    """Core-held AlphaEngine document plus one eligible ACN citation."""

    def __init__(self, *, unresolved_overlap: bool = False) -> None:
        self.handle = FakeDocumentHandle(DOCUMENT, page_chars=len(PAGE_ONE))
        self.core_harness = CoreHarness(approved_governance(), self.handle)
        self.temp = tempfile.TemporaryDirectory()
        self.staging_path = Path(self.temp.name) / "candidate-staging.sqlite"
        h = self.core_harness
        plan = h.acquisition.build_plan(DOCUMENT_REF)
        self.manifest = h.acquisition.acquire(plan)["manifest"]
        corrections = TranscriptCorrectionAuthority(
            h.core,
            spool=h.spool,
            manifest_resolver=lambda ref: self.manifest if ref == self.manifest["id"] else None,
            evidence_resolver=lambda _ref: None,
        )
        span = "New bookings were $19.3 billion"
        self.span_start = DOCUMENT.index(span)
        self.span_end = DOCUMENT.index("book-to-bill of 1.0.") + len("book-to-bill of 1.0.")
        if unresolved_overlap:
            flag_text = "3% in local currency"
        else:
            flag_text = "r ight"
        flag_start = DOCUMENT.index(flag_text)
        self.correction_set = corrections.publish(
            "transcript-correction-set:acn:q3fy26:qualitative",
            source_manifest_ref=self.manifest["id"],
            source_manifest_hash=self.manifest["content_hash"],
            source_content_hash=DIGEST,
            review_scope="targeted_flags",
            corrections=[{
                "source_start": flag_start,
                "source_end": flag_start + len(flag_text),
                "source_sha256": hashlib.sha256(flag_text.encode("utf-8")).hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved",
                "replacement_text": None,
                "rationale": "ASR flag fixture.",
                "evidence_bindings": [],
            }],
            actor_ref=REVIEWER,
        )
        self.citation = corrections.bind_claim_citation(
            self.correction_set["id"], self.correction_set["content_hash"],
            source_start=self.span_start, source_end=self.span_end,
        )

    @property
    def core(self):
        return self.core_harness.core

    def count(self, table: str) -> int:
        return self.core_harness.count(table)

    def artifact_reader(self, artifact: dict) -> bytes:
        return self.core_harness.spool.read_object(artifact["artifact_content_hash"])

    def stage(self, staging: CandidateStagingStore, *, idempotency_key: str = "stage:acn:qualitative:1", **overrides) -> dict:
        params = {
            "correction_set_ref": self.correction_set["id"],
            "citation_ref": self.citation["id"],
            "subject_ref": SUBJECT,
            "metric_or_aspect": ASPECT,
            "period": PERIOD,
            "basis": BASIS,
            "normalized_statement": STATEMENT,
            "actor_ref": PRODUCER,
            "idempotency_key": idempotency_key,
            "artifact_reader": self.artifact_reader,
        }
        params.update(overrides)
        return stage_transcript_qualitative_candidate(self.core, staging, **params)

    def close(self) -> None:
        self.core_harness.close()
        self.temp.cleanup()


class QualitativeTranscriptEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = QualitativeTranscriptHarness()
        self.addCleanup(self.h.close)
        self.assertTrue(self.h.citation["claim_eligible"])

    def test_acn_semantic_candidate_stages_reviews_and_commits(self) -> None:
        h = self.h
        staging = CandidateStagingStore(h.staging_path)
        try:
            first = h.stage(staging)
            self.assertEqual(first["write_status"], "fresh")
            self.assertIsNone(first["staging"]["numeric_verification_ref"])
            again = h.stage(staging)
            self.assertEqual(again["write_status"], "duplicate")
            counts = staging.counts()
        finally:
            staging.close()
        self.assertEqual(counts["candidate_claim_versions"], 1)
        self.assertEqual(counts["candidate_evidence_versions"], 1)
        self.assertEqual(counts["candidate_numeric_specs"], 0)
        self.assertEqual(counts["candidate_verifications"], 1)

        claim = first["claim"]
        evidence = first["evidence"]
        assert_wire_schema(self, "candidate-claim.schema.json", claim)
        assert_wire_schema(self, "candidate-evidence.schema.json", evidence)
        assert_wire_schema(
            self, "authority-source-verification-material.schema.json", first["material"]
        )
        assert_wire_schema(self, "verification-bundle.schema.json", first["source_verification"])
        self.assertEqual(claim["claim_kind"], "qualitative")
        for field in ("value", "unit", "currency", "scale", "numeric_spec_ref",
                      "numeric_spec_hash", "numeric_verification_ref", "numeric_verification_hash"):
            self.assertIsNone(claim[field], field)
        self.assertEqual(evidence["source_type"], TRANSCRIPT_EVIDENCE_SOURCE_TYPE)
        self.assertEqual(evidence["artifact_refs"][1]["ref"], h.citation["id"])
        self.assertEqual(first["material"]["provenance_mode"], TRANSCRIPT_CORE_AUTHORITY_MODE)
        self.assertEqual(first["source_verification"]["verdict"], "pass")
        self.assertIn(
            "raw_artifact_bytes",
            {item["code"] for item in first["source_verification"]["findings"]},
        )

        review = HumanReviewAuthority(h.staging_path)
        self.addCleanup(review.close)
        # Cockpit projection: the authority bundle has no numeric authority.
        bundle = review.candidate_authority_bundle(claim["id"])
        self.assertIsNone(bundle["numeric_spec"])
        self.assertIsNone(bundle["numeric_verification"])
        self.assertEqual(bundle["material"]["id"], first["material"]["id"])
        listed = review.list_candidates()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["claim"]["claim_kind"], "qualitative")

        decision = review.decide(
            candidate_claim_ref=claim["id"],
            candidate_claim_hash=claim["content_hash"],
            verdict="accept",
            reviewed_semantics={
                key: claim[key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement",
                )
            },
            rationale="Raw span says bookings fell 3% in local currency; semantic statement only.",
            findings=["raw span supports the direction statement"],
            reviewer_ref=REVIEWER,
            source_event_ref="research-review:acn:qualitative",
            idempotency_key="review:acn:qualitative",
            created_at=claim["created_at"],
        )
        self.assertEqual(decision["verdict"], "accept")
        pending = review.pending_commits()
        self.assertEqual(len(pending), 1)
        result = h.core.commit_reviewed_candidate(
            **pending[0], idempotency_key="reviewed-ledger:acn:qualitative",
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(h.count("evidence_versions"), 1)
        self.assertEqual(h.count("claim_versions"), 1)
        stored = json.loads(h.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
            (result["claim_version_ref"],),
        ).fetchone()[0])
        assert_wire_schema(self, "claim-version-v0.2.schema.json", stored)
        self.assertEqual(stored["claim_kind"], "qualitative")
        for field in ("value", "unit", "currency", "scale"):
            self.assertIsNone(stored[field], field)
        self.assertEqual(stored["normalized_statement"], STATEMENT)
        stored_evidence = json.loads(h.core.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
            (result["evidence_version_ref"],),
        ).fetchone()[0])
        self.assertEqual(stored_evidence["source_type"], TRANSCRIPT_EVIDENCE_SOURCE_TYPE)
        self.assertEqual(stored_evidence["artifact_refs"][1]["ref"], h.citation["id"])

        duplicate = h.core.commit_reviewed_candidate(
            **pending[0], idempotency_key="reviewed-ledger:acn:qualitative",
        )
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(h.count("claim_versions"), 1)

    def test_cockpit_view_marks_semantic_candidate_without_numbers(self) -> None:
        h = self.h
        staging = CandidateStagingStore(h.staging_path)
        try:
            h.stage(staging)
        finally:
            staging.close()
        review = HumanReviewAuthority(h.staging_path)
        self.addCleanup(review.close)
        item = review.list_candidates()[0]
        projection = ResearchReviewControlPlane.project_candidate(item)
        self.assertEqual(projection["claim_kind"], "qualitative")
        self.assertIsNone(projection["value"])
        self.assertIsNone(projection["unit"])
        cockpit = (
            Path(__file__).parents[1] / "src" / "dalton_core" / "cockpit_control.html"
        ).read_text(encoding="utf-8")
        self.assertIn('item.claim_kind==="qualitative"', cockpit)
        self.assertIn("语义候选（无数值）", cockpit)


class QualitativeTranscriptAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = QualitativeTranscriptHarness()
        self.addCleanup(self.h.close)
        self.staging = CandidateStagingStore(":memory:")
        self.addCleanup(self.staging.close)
        self.staged = self.h.stage(self.staging)

    def _reclaim(self, **changes) -> dict:
        base = {k: v for k, v in self.staged["claim"].items() if k != "content_hash"}
        base.update(changes)
        return {**base, "content_hash": content_hash(base)}

    def test_qualitative_claim_with_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchVerificationError, "must not carry numeric fields: value"):
            validate_candidate_claim(self._reclaim(value="-3"))
        with self.assertRaisesRegex(ResearchVerificationError, "unit, scale"):
            validate_candidate_claim(self._reclaim(unit="percent", scale="one"))

    def test_qualitative_claim_with_numeric_spec_ref_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchVerificationError, "numeric_spec_ref"):
            validate_candidate_claim(self._reclaim(
                numeric_spec_ref="numeric-spec:smuggled", numeric_spec_hash="2" * 64,
            ))
        with self.assertRaisesRegex(ResearchVerificationError, "numeric_verification_ref"):
            validate_candidate_claim(self._reclaim(
                numeric_verification_ref="verification-bundle:numeric:smuggled",
                numeric_verification_hash="3" * 64,
            ))

    def test_unknown_claim_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchVerificationError, "claim_kind"):
            validate_candidate_claim(self._reclaim(claim_kind="narrative"))

    def test_stage_rejects_qualitative_without_transcript_evidence(self) -> None:
        material = self.staged["material"]
        bundle = self.staged["source_verification"]
        evidence = self.staged["evidence"]
        # Strip the citation binding: plain authenticated_library evidence.
        base = {k: v for k, v in evidence.items() if k != "content_hash"}
        base["source_type"] = material["source_type"]
        base["artifact_refs"] = evidence["artifact_refs"][:1]
        base["source_lineage"] = evidence["source_lineage"][:-1]
        plain_evidence = {**base, "content_hash": content_hash(base)}
        claim_base = {k: v for k, v in self.staged["claim"].items() if k != "content_hash"}
        claim_base["candidate_evidence_refs"] = [
            {"ref": plain_evidence["id"], "hash": plain_evidence["content_hash"]}
        ]
        plain_claim = {**claim_base, "content_hash": content_hash(claim_base)}
        resolver = TranscriptCoreAuthorityResolver(self.h.core)
        with self.assertRaisesRegex(VerificationRejected, "authenticated transcript evidence"):
            self.staging.stage(
                material=material, source_verification=bundle,
                evidence=plain_evidence, claim=plain_claim,
                idempotency_key="stage:plain", verification_mode=TRANSCRIPT_CORE_AUTHORITY_MODE,
                authority_resolver=resolver,
            )
        with self.assertRaises(VerificationRejected):
            build_transcript_qualitative_candidate(
                plain_evidence, bundle, candidate_claim_ref="candidate-claim:plain",
                subject_ref=SUBJECT, metric_or_aspect=ASPECT, period=PERIOD, basis=BASIS,
                normalized_statement=STATEMENT, actor_ref=PRODUCER,
                created_at=plain_evidence["created_at"],
            )

    def test_stage_rejects_transcript_label_without_citation_binding(self) -> None:
        material = self.staged["material"]
        bundle = self.staged["source_verification"]
        evidence = self.staged["evidence"]
        base = {k: v for k, v in evidence.items() if k != "content_hash"}
        base["artifact_refs"] = evidence["artifact_refs"][:1]
        base["source_lineage"] = evidence["source_lineage"][:-1]
        mislabeled = {**base, "content_hash": content_hash(base)}
        claim_base = {k: v for k, v in self.staged["claim"].items() if k != "content_hash"}
        claim_base["candidate_evidence_refs"] = [
            {"ref": mislabeled["id"], "hash": mislabeled["content_hash"]}
        ]
        claim = {**claim_base, "content_hash": content_hash(claim_base)}
        with self.assertRaisesRegex(ResearchVerificationConflict, "candidate evidence drifted"):
            self.staging.stage(
                material=material, source_verification=bundle,
                evidence=mislabeled, claim=claim,
                idempotency_key="stage:mislabeled",
                verification_mode=TRANSCRIPT_CORE_AUTHORITY_MODE,
                # Same reader as the staged bundle so the source verification
                # recomputes byte-identical and the evidence shape is what fails.
                authority_resolver=TranscriptCoreAuthorityResolver(
                    self.h.core, artifact_reader=self.h.artifact_reader
                ),
            )

    def test_stage_rejects_qualitative_with_numeric_inputs_or_wrong_mode(self) -> None:
        staged = self.staged
        resolver = TranscriptCoreAuthorityResolver(self.h.core)
        with self.assertRaisesRegex(VerificationRejected, "cannot carry numeric"):
            self.staging.stage(
                material=staged["material"], source_verification=staged["source_verification"],
                evidence=staged["evidence"], claim=staged["claim"],
                numeric_spec={"schema_version": "0.1"}, numeric_verification={"schema_version": "0.1"},
                idempotency_key="stage:numeric", verification_mode=TRANSCRIPT_CORE_AUTHORITY_MODE,
                authority_resolver=resolver,
            )
        with self.assertRaisesRegex(VerificationRejected, "requires checkpoint"):
            self.staging.stage(
                material=staged["material"], source_verification=staged["source_verification"],
                evidence=staged["evidence"], claim=staged["claim"],
                idempotency_key="stage:wrong-mode", verification_mode="connector_authority",
                authority_resolver=resolver,
            )
        with self.assertRaisesRegex(VerificationRejected, "resolver"):
            self.staging.stage(
                material=staged["material"], source_verification=staged["source_verification"],
                evidence=staged["evidence"], claim=staged["claim"],
                idempotency_key="stage:no-resolver", verification_mode=TRANSCRIPT_CORE_AUTHORITY_MODE,
            )

    def test_tampered_source_verification_is_not_the_deterministic_verifier(self) -> None:
        staged = self.staged
        bundle = {k: v for k, v in staged["source_verification"].items() if k != "content_hash"}
        bundle["created_at"] = "2026-08-26T15:00:00.000000+00:00"
        tampered = {**bundle, "content_hash": content_hash(bundle)}
        evidence_base = {k: v for k, v in staged["evidence"].items() if k != "content_hash"}
        evidence_base["source_verification_hash"] = tampered["content_hash"]
        evidence = {**evidence_base, "content_hash": content_hash(evidence_base)}
        claim_base = {k: v for k, v in staged["claim"].items() if k != "content_hash"}
        claim_base["source_verification_hash"] = tampered["content_hash"]
        claim_base["candidate_evidence_refs"] = [{"ref": evidence["id"], "hash": evidence["content_hash"]}]
        claim = {**claim_base, "content_hash": content_hash(claim_base)}
        with self.assertRaisesRegex(ResearchVerificationConflict, "deterministic verifier"):
            self.staging.stage(
                material=staged["material"], source_verification=tampered,
                evidence=evidence, claim=claim, idempotency_key="stage:tampered",
                verification_mode=TRANSCRIPT_CORE_AUTHORITY_MODE,
                authority_resolver=TranscriptCoreAuthorityResolver(self.h.core),
            )

    def test_wrong_correction_set_and_missing_document_fail_closed(self) -> None:
        with self.assertRaisesRegex(TranscriptCoreAuthorityError, "does not belong"):
            self.h.stage(
                self.staging, idempotency_key="stage:wrong-set",
                correction_set_ref="transcript-correction-set:other",
            )
        with self.assertRaises(TranscriptCoreAuthorityError):
            TranscriptCoreAuthorityResolver(self.h.core).locate_source_envelope(
                "alphaengine-doc:999", "0" * 64
            )

    def test_policy_paths_reject_qualitative_candidates(self) -> None:
        staged = self.staged
        with self.assertRaisesRegex(ResearchAutoCommitRejected, "explicit human review"):
            authorize_policy_candidate(
                connection=self.h.core.connection, policy_version={},
                evidence=staged["evidence"], claim=staged["claim"],
            )
        with self.assertRaisesRegex(GateRejected, "explicit human review"):
            self.h.core.commit_policy_candidate(
                evidence=staged["evidence"], claim=staged["claim"],
                idempotency_key="policy:qualitative",
            )
        # Defense in depth inside the shared Ledger writer: a policy binding
        # or a non-human authorization can never carry a qualitative claim.
        decision_body = {
            "schema_version": "0.1", "id": "human-review-decision:forged",
            "created_at": staged["claim"]["created_at"],
            "candidate_claim_ref": staged["claim"]["id"],
            "candidate_claim_hash": staged["claim"]["content_hash"],
            "candidate_evidence_ref": staged["evidence"]["id"],
            "candidate_evidence_hash": staged["evidence"]["content_hash"],
            "verdict": "accept",
            "reviewed_semantics": {
                key: staged["claim"][key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement",
                )
            },
            "proposed_revisions": None, "relation": "supports",
            "rationale": "forged", "findings": [], "reviewer_ref": REVIEWER,
            "authorization": "explicit_human_review", "source": "tailscale_review",
            "source_event_ref": "research-review:forged",
        }
        decision = {**decision_body, "content_hash": content_hash(decision_body)}
        with self.assertRaisesRegex(GateRejected, "explicit human review"):
            self.h.core._commit_authorized_candidate(
                decision_wire=decision, evidence=staged["evidence"], claim=staged["claim"],
                idempotency_key="policy:forged", fault_at=None,
                active_policy_binding=("policy-version:x", "0" * 64),
            )
        self.assertEqual(self.h.count("claim_versions"), 0)
        self.assertEqual(self.h.count("reviewed_candidate_commits"), 0)


class UnresolvedOverlapTests(unittest.TestCase):
    def test_citation_overlapping_unresolved_flag_cannot_stage(self) -> None:
        h = QualitativeTranscriptHarness(unresolved_overlap=True)
        self.addCleanup(h.close)
        self.assertFalse(h.citation["claim_eligible"])
        staging = CandidateStagingStore(":memory:")
        self.addCleanup(staging.close)
        with self.assertRaisesRegex(TranscriptCoreAuthorityError, "ineligible"):
            h.stage(staging)
        self.assertEqual(staging.counts()["candidate_claim_versions"], 0)
        # The existing binder rejects the same citation independently.
        with self.assertRaises(TranscriptCorrectionConflict):
            from dalton_core.transcript_correction import (
                bind_candidate_evidence_to_transcript_citation,
            )
            resolver = TranscriptCoreAuthorityResolver(h.core)
            source_ref = resolver.locate_source_envelope(DOCUMENT_REF, DIGEST)
            authority = resolver._authority(source_ref)
            when = authority["source"]["retrieved_at"]
            body = {
                "schema_version": "0.1", "id": "candidate-evidence-version:overlap",
                "created_at": when, "candidate_evidence_ref": "candidate-evidence:overlap",
                "version": 1, "source_type": "authenticated_library",
                "source_ref": "source:alphaengine",
                "source_envelope_ref": authority["source"]["id"],
                "source_envelope_hash": authority["source"]["content_hash"],
                "artifact_refs": [{"ref": authority["artifact"]["id"], "hash": authority["artifact"]["content_hash"]}],
                "retrieved_at": when, "valid_until": None,
                "source_lineage": [authority["source"]["id"]],
                "independence_group": "independence:source:alphaengine",
                "source_verification_ref": "verification-bundle:source:overlap",
                "source_verification_hash": "1" * 64, "actor_ref": PRODUCER,
                "prior_version_ref": None,
            }
            bind_candidate_evidence_to_transcript_citation(
                {**body, "content_hash": content_hash(body)}, h.citation
            )


if __name__ == "__main__":
    unittest.main()
