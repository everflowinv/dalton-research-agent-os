from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
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
    ResearchVerificationConflict,
    build_authority_source_material,
    build_candidate_claim,
    build_candidate_evidence,
    verify_authority_source_material,
    verify_numeric_spec,
)
from dalton_core.sec_authority_harness import SecAuthorityHarness, WIRE_WHEN
from dalton_core.store import canonical_json, content_hash
from dalton_core.transcript_correction import (
    TRANSCRIPT_EVIDENCE_SOURCE_TYPE,
    TranscriptCorrectionAuthority,
    TranscriptCorrectionConflict,
    bind_candidate_evidence_to_transcript_citation,
)
from tests.test_alphaengine_document_acquisition import (
    FakeAuthorityReader,
    FakePagePort,
)
from tests.test_connector_runner import assert_wire_schema
from tests.test_live_mcp_connector import (
    LiveGateHarness,
    document_parameters,
    tool_result,
)


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

    def _stage_candidate(
        self,
        *,
        suffix: str = "1",
        evidence_transform: Callable[[dict], dict] | None = None,
    ) -> dict:
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
            "id": f"numeric-spec:sec:review-count:{suffix}",
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
            candidate_evidence_ref=f"candidate-evidence:sec:review:{suffix}",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
            verification_mode="connector_authority",
        )
        if evidence_transform is not None:
            evidence = evidence_transform(evidence)
        claim = build_candidate_claim(
            evidence,
            source_bundle,
            spec,
            numeric_bundle,
            candidate_claim_ref=f"candidate-claim:sec:review:{suffix}",
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
                idempotency_key=f"stage:sec:review:{suffix}",
                verification_mode="connector_authority",
                authority_resolver=resolver,
            )
        finally:
            staging.close()
        return {"evidence": evidence, "claim": claim}

    def _transcript_citation(self, suffix: str, *, unresolved: bool = False) -> dict:
        artifact_ref = self.candidate["evidence"]["artifact_refs"][0]["ref"]
        artifact = self.harness.core.connection.execute(
            "SELECT artifact_content_hash FROM observability_artifact_versions_v2 "
            "WHERE version_id=?",
            (artifact_ref,),
        ).fetchone()
        raw = self.harness.spool.read_object(artifact["artifact_content_hash"])
        original = raw.decode("utf-8")
        plan = build_alphaengine_document_acquisition_plan(
            document_ref=f"alphaengine-doc:formal-transcript-{suffix}",
            created_at=WIRE_WHEN,
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=max(1, len(original)),
        )
        manifest_authority = FakeAuthorityReader()
        manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=FakePagePort(
                plan=plan,
                pages=[original],
                authority=manifest_authority,
                spool=self.harness.spool,
            ),
            authority_reader=manifest_authority,
            spool=self.harness.spool,
        ).execute()
        self.assertEqual(
            manifest["assembled_object"]["content_hash"],
            artifact["artifact_content_hash"],
        )
        support_body = {
            "id": f"authority:transcript-terminology:{suffix}",
            "kind": "primary-reference",
        }
        support = {**support_body, "content_hash": content_hash(support_body)}
        corrections = TranscriptCorrectionAuthority(
            self.harness.core,
            spool=self.harness.spool,
            manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
            evidence_resolver=lambda ref: support if ref == support["id"] else None,
        )
        source_text = "10-Q"
        start = original.index(source_text)
        correction_set = corrections.publish(
            f"transcript-correction-set:formal-{suffix}",
            source_manifest_ref=manifest["id"],
            source_manifest_hash=manifest["content_hash"],
            source_content_hash=manifest["assembled_object"]["content_hash"],
            review_scope="targeted_flags",
            corrections=[{
                "source_start": start,
                "source_end": start + len(source_text),
                "source_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved" if unresolved else "accepted",
                "replacement_text": None if unresolved else "10-Q filing",
                "rationale": "Exact transcript citation admission fixture.",
                "evidence_bindings": [] if unresolved else [{
                    "authority_ref": support["id"],
                    "authority_hash": support["content_hash"],
                    "evidence_kind": "primary_reference",
                    "location": "official-term:10-Q",
                }],
            }],
            actor_ref="human:owner",
        )
        return corrections.bind_claim_citation(
            correction_set["id"],
            correction_set["content_hash"],
            source_start=0,
            source_end=len(original),
        )

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

    def test_authority_can_be_read_from_threaded_http_worker(self) -> None:
        results: list[list[dict]] = []
        errors: list[BaseException] = []

        def read_candidates() -> None:
            try:
                results.append(self.review.list_candidates())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        worker = threading.Thread(target=read_candidates)
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0][0]["claim"]["id"],
            self.candidate["claim"]["id"],
        )

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

    def test_transcript_citation_binding_survives_staging_and_formal_promotion(self) -> None:
        binding = self._transcript_citation("accepted")
        self.candidate = self._stage_candidate(
            suffix="transcript-accepted",
            evidence_transform=lambda evidence: (
                bind_candidate_evidence_to_transcript_citation(evidence, binding)
            ),
        )
        assert_wire_schema(
            self,
            "candidate-evidence.schema.json",
            self.candidate["evidence"],
        )
        self._decide(
            idempotency_key="review:transcript:accepted",
            source_event_ref="research-review:transcript-accepted",
        )
        bundle = self.review.pending_commits()[0]
        result = self.harness.core.commit_reviewed_candidate(
            **bundle,
            idempotency_key="reviewed-ledger:transcript-accepted",
        )
        evidence = json.loads(
            self.harness.core.connection.execute(
                "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?",
                (result["evidence_version_ref"],),
            ).fetchone()[0]
        )
        assert_wire_schema(
            self,
            "evidence-version-v0.2.schema.json",
            evidence,
        )
        self.assertEqual(evidence["source_type"], TRANSCRIPT_EVIDENCE_SOURCE_TYPE)
        self.assertEqual(
            evidence["artifact_refs"][1],
            {"ref": binding["id"], "hash": binding["content_hash"]},
        )
        self.assertEqual(evidence["source_lineage"][-1], binding["id"])

    def test_real_alphaengine_envelope_promotes_with_exact_document_digest(self) -> None:
        original = "Revenue grew 3% in local currency. r ight"
        document_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        live = LiveGateHarness(
            "get_document",
            document_parameters(),
            tool_result({
                "metadata": {"doc_id": "320000610033807", "title": "ACN"},
                "content_chars": len(original),
                "content_sha256": document_digest,
                "offset": 0,
                "returned_chars": len(original),
                "text": original,
                "next_offset": None,
                "complete": True,
            }),
        )
        self.addCleanup(live.close)
        self.assertEqual(live.execute()["outcome"], "succeeded")
        source = json.loads(live.core.connection.execute(
            "SELECT record_json FROM connector_source_envelopes"
        ).fetchone()[0])
        artifact = live.observability.get_artifact_version_v2(
            source["raw_artifact_version_ref"]
        )
        self.assertNotEqual(artifact["artifact_content_hash"], document_digest)

        plan = build_alphaengine_document_acquisition_plan(
            document_ref="alphaengine-doc:320000610033807",
            created_at=WIRE_WHEN,
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=len(original),
        )
        manifest_authority = FakeAuthorityReader()
        manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=FakePagePort(
                plan=plan,
                pages=[original],
                authority=manifest_authority,
                spool=live.spool,
            ),
            authority_reader=manifest_authority,
            spool=live.spool,
        ).execute()
        corrections = TranscriptCorrectionAuthority(
            live.core,
            spool=live.spool,
            manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
            evidence_resolver=lambda _ref: None,
        )
        flag_start = original.index("r ight")
        correction_set = corrections.publish(
            "transcript-correction-set:alphaengine-envelope",
            source_manifest_ref=manifest["id"],
            source_manifest_hash=manifest["content_hash"],
            source_content_hash=document_digest,
            review_scope="targeted_flags",
            corrections=[{
                "source_start": flag_start,
                "source_end": len(original),
                "source_sha256": hashlib.sha256(
                    original[flag_start:].encode("utf-8")
                ).hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved",
                "replacement_text": None,
                "rationale": "Flag outside the exact cited span.",
                "evidence_bindings": [],
            }],
            actor_ref="human:owner",
        )
        citation = corrections.bind_claim_citation(
            correction_set["id"],
            correction_set["content_hash"],
            source_start=0,
            source_end=original.index(". ") + 1,
        )
        source_verification_ref = "verification-bundle:source:alphaengine-envelope"
        source_verification_hash = "1" * 64
        evidence_body = {
            "schema_version": "0.1",
            "id": "candidate-evidence-version:alphaengine-envelope",
            "created_at": WIRE_WHEN,
            "candidate_evidence_ref": "candidate-evidence:alphaengine-envelope",
            "version": 1,
            "source_type": "alphaengine_document",
            "source_ref": "source:alphaengine",
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "artifact_refs": [{"ref": artifact["id"], "hash": artifact["content_hash"]}],
            "retrieved_at": WIRE_WHEN,
            "valid_until": None,
            "source_lineage": [source["id"]],
            "independence_group": "issuer:acn:q3fy26",
            "source_verification_ref": source_verification_ref,
            "source_verification_hash": source_verification_hash,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        evidence = bind_candidate_evidence_to_transcript_citation(
            {**evidence_body, "content_hash": content_hash(evidence_body)}, citation
        )
        period = {
            "kind": "fiscal_quarter", "label": "FY2026Q3",
            "start": "2026-03-01T00:00:00.000000+00:00",
            "end": "2026-05-31T23:59:59.000000+00:00",
        }
        claim_body = {
            "schema_version": "0.1",
            "id": "candidate-claim-version:alphaengine-envelope",
            "created_at": WIRE_WHEN,
            "candidate_claim_ref": "candidate-claim:alphaengine-envelope",
            "version": 1,
            "subject_ref": "company:sec-cik:0001467373",
            "metric_or_aspect": "metric:revenue-growth-local-currency",
            "period": period,
            "basis": "management-reported",
            "normalized_statement": "Q3 FY2026 revenue grew 3% in local currency.",
            "semantic_verification_status": "unverified",
            "claim_kind": "quantitative", "value": "3", "unit": "percent",
            "currency": None, "scale": "one",
            "candidate_evidence_refs": [{"ref": evidence["id"], "hash": evidence["content_hash"]}],
            "source_verification_ref": source_verification_ref,
            "source_verification_hash": source_verification_hash,
            "numeric_spec_ref": "numeric-spec:alphaengine-envelope",
            "numeric_spec_hash": "2" * 64,
            "numeric_verification_ref": "verification-bundle:numeric:alphaengine-envelope",
            "numeric_verification_hash": "3" * 64,
            "actor_ref": "system:offline-verifier",
            "prior_version_ref": None,
        }
        claim = {**claim_body, "content_hash": content_hash(claim_body)}
        semantics = {
            key: claim[key] for key in (
                "subject_ref", "metric_or_aspect", "period", "basis",
                "normalized_statement",
            )
        }
        decision_body = {
            "schema_version": "0.1", "id": "human-review-decision:alphaengine-envelope",
            "created_at": WIRE_WHEN,
            "candidate_claim_ref": claim["id"],
            "candidate_claim_hash": claim["content_hash"],
            "candidate_evidence_ref": evidence["id"],
            "candidate_evidence_hash": evidence["content_hash"],
            "verdict": "accept", "reviewed_semantics": semantics,
            "proposed_revisions": None, "relation": "supports",
            "rationale": "Hermetic authenticated-review fixture for exact lineage.",
            "findings": ["raw document span and numeric meaning agree"],
            "reviewer_ref": REVIEWER,
            "authorization": "explicit_human_review", "source": "tailscale_review",
            "source_event_ref": "research-review:alphaengine-envelope",
        }
        decision = {**decision_body, "content_hash": content_hash(decision_body)}
        result = live.core.commit_reviewed_candidate(
            decision=decision, evidence=evidence, claim=claim,
            idempotency_key="reviewed-ledger:alphaengine-envelope",
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(live.count("evidence_versions"), 1)
        self.assertEqual(live.count("claim_versions"), 1)

    def test_unresolved_transcript_citation_cannot_reach_formal_claim(self) -> None:
        binding = self._transcript_citation("unresolved", unresolved=True)

        def hostile_transform(evidence: dict) -> dict:
            with self.assertRaises(TranscriptCorrectionConflict):
                bind_candidate_evidence_to_transcript_citation(evidence, binding)
            base = {
                key: value for key, value in evidence.items()
                if key != "content_hash"
            }
            base["source_type"] = TRANSCRIPT_EVIDENCE_SOURCE_TYPE
            base["artifact_refs"] = [
                *evidence["artifact_refs"],
                {"ref": binding["id"], "hash": binding["content_hash"]},
            ]
            base["source_lineage"] = [*evidence["source_lineage"], binding["id"]]
            return {**base, "content_hash": content_hash(base)}

        self.candidate = self._stage_candidate(
            suffix="transcript-unresolved",
            evidence_transform=hostile_transform,
        )
        self._decide(
            idempotency_key="review:transcript:unresolved",
            source_event_ref="research-review:transcript-unresolved",
        )
        with self.assertRaisesRegex(GateRejected, "citation authority"):
            self.harness.core.commit_reviewed_candidate(
                **self.review.pending_commits()[0],
                idempotency_key="reviewed-ledger:transcript-unresolved",
            )
        self.assertEqual(
            self.harness.core.connection.execute(
                "SELECT COUNT(*) FROM claim_versions"
            ).fetchone()[0],
            0,
        )

    def test_transcript_binding_shape_and_hash_drift_fail_closed(self) -> None:
        binding = self._transcript_citation("drift")

        def missing_binding(evidence: dict) -> dict:
            base = {
                key: value for key, value in evidence.items()
                if key != "content_hash"
            }
            base["source_type"] = TRANSCRIPT_EVIDENCE_SOURCE_TYPE
            return {**base, "content_hash": content_hash(base)}

        with self.assertRaisesRegex(
            ResearchVerificationConflict, "candidate evidence drifted"
        ):
            self._stage_candidate(
                suffix="transcript-missing",
                evidence_transform=missing_binding,
            )

        def drifted_binding(evidence: dict) -> dict:
            admitted = bind_candidate_evidence_to_transcript_citation(
                evidence, binding
            )
            base = {
                key: value for key, value in admitted.items()
                if key != "content_hash"
            }
            base["artifact_refs"] = [
                admitted["artifact_refs"][0],
                {"ref": binding["id"], "hash": "0" * 64},
            ]
            return {**base, "content_hash": content_hash(base)}

        self.candidate = self._stage_candidate(
            suffix="transcript-drifted",
            evidence_transform=drifted_binding,
        )
        self._decide(
            idempotency_key="review:transcript:drifted",
            source_event_ref="research-review:transcript-drifted",
        )
        with self.assertRaisesRegex(GateRejected, "citation authority"):
            self.harness.core.commit_reviewed_candidate(
                **self.review.pending_commits()[0],
                idempotency_key="reviewed-ledger:transcript-drifted",
            )


if __name__ == "__main__":
    unittest.main()
