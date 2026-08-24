from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.research_review_control import (
    ResearchReviewControlConfig,
    ResearchReviewControlError,
    ResearchReviewControlPlane,
    _subject_for_login,
)
from dalton_core.store import content_hash
from dalton_core.transcript_review_inbox import stage_transcript_review_bundle
from tests.test_alphaengine_document_acquisition import (
    FakeAuthorityReader,
    FakePagePort,
)


HASH = "a" * 64
LOGIN = "reviewer@example.com"
WHEN = "2026-08-24T12:00:00+00:00"


class FakeAuthority:
    def __init__(self):
        self.claim = {
            "id": "candidate-claim:version:1", "content_hash": HASH,
            "subject_ref": "company:x", "metric_or_aspect": "revenue",
            "period": {"kind": "quarter", "label": "2026Q2", "start": "2026-04-01T00:00:00+00:00", "end": "2026-06-30T00:00:00+00:00"},
            "basis": "reported", "normalized_statement": "Revenue was 3.",
            "value": "3", "unit": "USD", "currency": "USD", "scale": "million",
        }
        self.evidence = {
            "id": "candidate-evidence:version:1", "content_hash": "b" * 64,
            "source_type": "official_filing", "source_ref": "source:sec-edgar",
            "source_envelope_ref": "source-envelope:1",
            "artifact_refs": [{"ref": "artifact:1", "hash": "c" * 64}],
        }
        self.decision = None
        self.decide_params = None
        self.commit_results = []

    def list_candidates(self, *, limit=100):
        state = None
        if self.decision is not None and self.decision["verdict"] == "accept":
            state = "committed" if self.commit_results else "queued"
        return [{
            "claim": self.claim, "evidence": self.evidence,
            "decision": self.decision, "commit_state": state,
        }]

    def decide(self, **params):
        self.decide_params = params
        self.decision = {
            "id": "human-review:1", "content_hash": "d" * 64,
            "reviewer_ref": params["reviewer_ref"],
            "verdict": params["verdict"],
        }
        return {
            "write_status": "fresh", "decision_ref": "human-review:1",
            "decision_hash": "d" * 64, "verdict": params["verdict"],
            "commit_state": (
                "queued" if params["verdict"] == "accept" else "not_applicable"
            ),
        }

    def pending_commits(self, *, limit=100):
        if (
            self.decision is None or self.decision["verdict"] != "accept"
            or self.commit_results
        ):
            return []
        return [{
            "decision": self.decision, "evidence": self.evidence,
            "claim": self.claim,
        }]

    def record_commit_result(self, decision_ref, **params):
        self.commit_results.append((decision_ref, params))
        return {"state": "committed" if params.get("ledger_result") else "failed"}

    def commit_event(self, decision_ref):
        if self.decision is None or self.decision["id"] != decision_ref:
            raise RuntimeError("decision unavailable")
        if self.decision["verdict"] != "accept":
            return None
        ledger_result = (
            None if not self.commit_results
            else self.commit_results[-1][1].get("ledger_result")
        )
        state = "queued" if ledger_result is None else "committed"
        event = {
            "id": f"human-review-commit-event:{state}",
            "content_hash": "9" * 64,
            "state": state,
            "ledger_result": ledger_result,
        }
        return event

    def close(self):
        return None


class FakeWriter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []
        self.transcript_state = {
            "status": "pending_human_review", "correction_set": None,
            "citation_binding": None, "claim_eligible": False,
        }

    def commit_reviewed_candidate(self, **params):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("writer failed")
        return {"status": "fresh", "claim_version_ref": "claim-version:1"}

    def transcript_correction_review_state(self, **params):
        self.calls.append({"transcript_state": params})
        return dict(self.transcript_state)


class FakeGovernance:
    def __init__(self):
        self.calls = []

    def __call__(self, token_config, writer_socket, **params):
        self.calls.append((token_config, writer_socket, params))
        if params["operation"] == "publish_transcript_correction_set":
            body = {
                "id": "transcript-correction-set-version:1",
                "content_hash": "e" * 64,
            }
            return {"status": "fresh", **body}
        return {
            "id": "transcript-claim-citation-binding:1",
            "content_hash": "f" * 64,
            "claim_eligible": True,
        }


class ResearchReviewControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review_dir = self.root / "review-inbox"
        self.review_dir.mkdir()
        self.config = ResearchReviewControlConfig.from_mapping({
            "candidate_staging_path": str(self.root / "candidate.sqlite"),
            "transcript_review_directory": str(self.review_dir),
            "reconcile_interval_seconds": 60,
        })

    def plane(self, **kwargs):
        return ResearchReviewControlPlane(
            self.config,
            writer_socket=self.root / "writer.sock",
            token_config=self.root / "tokens.json",
            authority=kwargs.pop("authority", FakeAuthority()),
            writer=kwargs.pop("writer", FakeWriter()),
            governance_call=kwargs.pop("governance_call", FakeGovernance()),
            **kwargs,
        )

    def write_transcript_packet(self):
        original = "New bookings decreased 3% in local currency. r ight"
        acquisition = self.root / "acquisition"
        spool = RawSpool(acquisition, max_total_bytes=1_000_000)
        plan = build_alphaengine_document_acquisition_plan(
            document_ref="alphaengine-doc:130000095976806",
            created_at=WHEN,
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=len(original),
        )
        reader = FakeAuthorityReader()
        manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=FakePagePort(
                plan=plan, pages=[original], authority=reader, spool=spool
            ),
            authority_reader=reader,
            spool=spool,
        ).execute()
        citation_text = original.split(".")[0] + "."
        flag_text = "r ight"
        flag_start = original.index(flag_text)
        case = self.review_dir / "acn-q3fy26"
        case.mkdir()
        packet = {
            "schema_version": "0.1",
            "id": "transcript-review-packet:acn:q3fy26:1",
            "created_at": WHEN,
            "source": {
                "document_ref": manifest["document_ref"],
                "manifest_ref": manifest["id"],
                "manifest_hash": manifest["content_hash"],
                "content_chars": manifest["content_chars"],
                "content_sha256": manifest["assembled_object"]["content_hash"],
                "page_count": len(manifest["pages"]),
                "physical_calls": manifest["physical_calls"],
                "title": "Accenture Q3 2026",
                "lineage_path": "source-manifest.json",
                "summary_fields_allowed": False,
            },
            "proposed_correction_set": {
                "correction_set_ref": "transcript-correction-set:acn:q3fy26:1",
                "review_scope": "targeted_flags",
                "corrections": [{
                    "source_start": flag_start,
                    "source_end": flag_start + len(flag_text),
                    "source_sha256": hashlib.sha256(
                        flag_text.encode("utf-8")
                    ).hexdigest(),
                    "source_text": flag_text,
                    "correction_kind": "terminology",
                    "disposition": "unresolved",
                    "replacement_text": None,
                    "rationale": "Flag outside the formal citation.",
                    "evidence_bindings": [],
                }],
                "actor_ref": None,
                "human_review_required": True,
                "unresolved_overlap_with_formal_citation": 0,
            },
            "candidate_claim": {
                "candidate_claim_ref": "candidate-claim:acn:q3fy26:new-bookings",
                "subject_ref": "company:sec-cik:0001467373",
                "metric_or_aspect": "metric:new-bookings-growth-local-currency",
                "period": "FY2026Q3", "basis": "management-reported",
                "normalized_statement": "New bookings decreased 3% in local currency.",
                "claim_kind": "quantitative", "value": "-3",
                "unit": "percent", "currency": None, "scale": "one",
                "formal_status": "blocked_pending_authenticated_human_review",
                "citation": {
                    "source_start": 0, "source_end": len(citation_text),
                    "source_sha256": hashlib.sha256(
                        citation_text.encode("utf-8")
                    ).hexdigest(),
                    "raw_span": citation_text,
                },
                "required_secondary_numeric_authority": {
                    "claim_ref": "claim:acn:q3fy26:new-bookings",
                    "source_ref": "sec:acn-q3fy26-exhibit",
                    "source_type": "sec-filing-exhibit",
                },
            },
            "research_targets": [],
            "review_contract": {
                "acceptance_scope": ["raw span and sign are correct"],
                "allowed_verdicts": ["accept", "revise", "reject"],
                "authorization": "explicit_human_review",
                "source": "tailscale_review",
            },
            "formal_authority_counts": {
                "claim_versions": 0, "evidence_versions": 0,
                "thesis_versions": 0,
            },
            "production_activated": False,
            "forbidden_inputs": [
                "metadata.main_point", "metadata.question_answer"
            ],
        }
        packet["content_hash"] = content_hash(packet)
        (case / "review-packet.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )
        (case / "source-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return packet, manifest

    def test_plane_injects_identity_semantics_and_commits_accept(self):
        authority = FakeAuthority()
        writer = FakeWriter()
        plane = self.plane(authority=authority, writer=writer)
        result = plane.record(LOGIN, {
            "request_id": "request-12345678",
            "candidate_claim_ref": authority.claim["id"],
            "candidate_claim_hash": authority.claim["content_hash"],
            "verdict": "accept", "rationale": "Checked the source.",
            "findings": [], "proposed_revisions": None,
        })
        self.assertEqual(result["commit_state"], "committed")
        self.assertEqual(
            authority.decide_params["reviewer_ref"], _subject_for_login(LOGIN)
        )
        self.assertEqual(
            authority.decide_params["reviewed_semantics"]["normalized_statement"],
            authority.claim["normalized_statement"],
        )
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(authority.commit_results), 1)

    def test_writer_failure_leaves_durable_pending_result(self):
        authority = FakeAuthority()
        plane = self.plane(authority=authority, writer=FakeWriter(fail=True))
        result = plane.record(LOGIN, {
            "request_id": "request-abcdefgh",
            "candidate_claim_ref": authority.claim["id"],
            "candidate_claim_hash": authority.claim["content_hash"],
            "verdict": "accept", "rationale": "Checked the source.",
            "findings": [], "proposed_revisions": None,
        })
        self.assertEqual(result["commit_state"], "pending")
        self.assertEqual(
            authority.commit_results[0][1]["error_code"], "writer_rejected"
        )

    def test_transcript_packet_is_exact_and_uses_ephemeral_human_governance(self):
        packet, _ = self.write_transcript_packet()
        governance = FakeGovernance()
        plane = self.plane(governance_call=governance)
        view = plane.transcript_view(LOGIN)
        self.assertEqual(view["items"][0]["packet_hash"], packet["content_hash"])
        result = plane.record_transcript(LOGIN, {
            "request_id": "request-transcript-1",
            "packet_ref": packet["id"],
            "packet_hash": packet["content_hash"],
            "action": "publish_and_bind",
        })
        self.assertTrue(result["claim_eligible"])
        self.assertEqual(len(governance.calls), 2)
        self.assertEqual(
            governance.calls[0][2]["actor_ref"], _subject_for_login(LOGIN)
        )
        self.assertEqual(
            governance.calls[0][2]["operation"],
            "publish_transcript_correction_set",
        )
        correction = governance.calls[0][2]["params"]["corrections"][0]
        self.assertNotIn("source_text", correction)
        self.assertEqual(
            governance.calls[1][2]["operation"],
            "bind_transcript_claim_citation",
        )

    def test_trajectory_is_deterministic_read_only_and_exposes_gaps(self):
        packet, manifest = self.write_transcript_packet()
        plane = self.plane()
        first = plane.trajectory_view(LOGIN)
        second = plane.trajectory_view(LOGIN)
        self.assertTrue(first["projection_only"])
        self.assertEqual(len(first["items"]), 1)
        trajectory = first["items"][0]
        self.assertEqual(trajectory["content_hash"], second["items"][0]["content_hash"])
        self.assertEqual(trajectory["source_packet_ref"], packet["id"])
        self.assertFalse(trajectory["admission_effect"])
        self.assertEqual(
            [node["stage"] for node in trajectory["nodes"]],
            [
                "agenda", "research_question", "planning", "connector",
                "raw_artifact", "transcript_correction", "citation_binding",
                "candidate", "human_review", "formal_ledger", "brief",
            ],
        )
        by_stage = {node["stage"]: node for node in trajectory["nodes"]}
        self.assertEqual(by_stage["agenda"]["status"], "unrecorded")
        self.assertEqual(by_stage["planning"]["status"], "unrecorded")
        self.assertEqual(by_stage["connector"]["status"], "complete")
        self.assertEqual(by_stage["transcript_correction"]["status"], "pending")
        self.assertEqual(by_stage["citation_binding"]["status"], "blocked")
        self.assertEqual(by_stage["formal_ledger"]["status"], "blocked")
        raw_ref = by_stage["raw_artifact"]["exact_refs"][0]
        self.assertEqual(raw_ref["ref"], manifest["document_ref"])
        self.assertEqual(
            raw_ref["hash"], manifest["assembled_object"]["content_hash"]
        )
        for event in trajectory["nodes"]:
            self.assertTrue(event["exact_refs"])
            for exact in event["exact_refs"]:
                self.assertEqual(len(exact["hash"]), 64)

        # Mutating a returned projection changes neither the next rebuild nor
        # the exact packet that the write path independently revalidates.
        trajectory["nodes"][0]["status"] = "complete"
        rebuilt = plane.trajectory_view(LOGIN)["items"][0]
        self.assertEqual(rebuilt["nodes"][0]["status"], "unrecorded")
        with self.assertRaises(ResearchReviewControlError):
            plane.record_transcript(LOGIN, {
                "request_id": "request-projection-tamper",
                "packet_ref": packet["id"],
                "packet_hash": rebuilt["content_hash"],
                "action": "publish_and_bind",
            })

    def test_trajectory_advances_only_after_exact_correction_and_candidate_state(self):
        packet, manifest = self.write_transcript_packet()
        writer = FakeWriter()
        correction = packet["proposed_correction_set"]
        citation = packet["candidate_claim"]["citation"]
        published = {
            "id": "transcript-correction-set-version:1",
            "content_hash": "e" * 64,
            "correction_set_ref": correction["correction_set_ref"],
            "source_manifest_ref": manifest["id"],
            "source_manifest_hash": manifest["content_hash"],
            "source_content_hash": manifest["assembled_object"]["content_hash"],
            "review_scope": correction["review_scope"],
            "corrections": [
                {key: item[key] for key in item if key != "source_text"}
                for item in correction["corrections"]
            ],
        }
        binding = {
            "id": "transcript-claim-citation-binding:1",
            "content_hash": "f" * 64,
            "correction_set_version_ref": published["id"],
            "correction_set_version_hash": published["content_hash"],
            "source_manifest_ref": manifest["id"],
            "source_manifest_hash": manifest["content_hash"],
            "source_content_hash": manifest["assembled_object"]["content_hash"],
            "source_start": citation["source_start"],
            "source_end": citation["source_end"],
            "claim_eligible": True,
        }
        writer.transcript_state = {
            "status": "claim_eligible",
            "correction_set": published,
            "citation_binding": binding,
            "claim_eligible": True,
        }
        authority = FakeAuthority()
        authority.claim["candidate_claim_ref"] = packet["candidate_claim"][
            "candidate_claim_ref"
        ]
        authority.claim["prior_version_ref"] = None
        trajectory = self.plane(
            writer=writer, authority=authority
        ).trajectory_view(LOGIN)["items"][0]
        by_stage = {node["stage"]: node for node in trajectory["nodes"]}
        self.assertEqual(trajectory["state"], "awaiting_candidate_review")
        self.assertEqual(by_stage["transcript_correction"]["status"], "complete")
        self.assertEqual(by_stage["citation_binding"]["status"], "complete")
        self.assertEqual(by_stage["candidate"]["status"], "complete")
        self.assertEqual(by_stage["human_review"]["status"], "pending")
        self.assertEqual(by_stage["formal_ledger"]["status"], "blocked")

    def test_transcript_governance_failure_is_sanitized(self):
        packet, _ = self.write_transcript_packet()

        def fail_governance(*_args, **_kwargs):
            raise RuntimeError("sensitive writer detail")

        plane = self.plane(governance_call=fail_governance)
        with self.assertRaisesRegex(
            ResearchReviewControlError,
            "transcript review writer rejected the admission",
        ):
            plane.record_transcript(LOGIN, {
                "request_id": "request-transcript-failure",
                "packet_ref": packet["id"],
                "packet_hash": packet["content_hash"],
                "action": "publish_and_bind",
            })

    def test_published_transcript_state_must_match_packet_exactly(self):
        self.write_transcript_packet()
        writer = FakeWriter()
        writer.transcript_state = {
            "status": "correction_published",
            "correction_set": {
                "id": "transcript-correction-set-version:wrong",
                "content_hash": "e" * 64,
                "correction_set_ref": "transcript-correction-set:acn:q3fy26:1",
                "source_manifest_ref": "wrong-manifest",
                "source_manifest_hash": "0" * 64,
                "source_content_hash": "0" * 64,
                "review_scope": "targeted_flags",
                "corrections": [],
            },
            "citation_binding": None,
            "claim_eligible": False,
        }
        with self.assertRaisesRegex(
            ResearchReviewControlError,
            "published correction state disagrees",
        ):
            self.plane(writer=writer).transcript_view(LOGIN)

    def test_contradictory_transcript_state_cannot_advance_trajectory(self):
        self.write_transcript_packet()
        writer = FakeWriter()
        writer.transcript_state["status"] = "claim_eligible"
        with self.assertRaisesRegex(
            ResearchReviewControlError,
            "contradictory state",
        ):
            self.plane(writer=writer).trajectory_view(LOGIN)

    def test_packet_hash_drift_fails_closed_before_governance(self):
        packet, _ = self.write_transcript_packet()
        governance = FakeGovernance()
        plane = self.plane(governance_call=governance)
        with self.assertRaises(ResearchReviewControlError):
            plane.record_transcript(LOGIN, {
                "request_id": "request-transcript-2",
                "packet_ref": packet["id"],
                "packet_hash": "0" * 64,
                "action": "publish_and_bind",
            })
        self.assertEqual(governance.calls, [])

    def test_review_bundle_staging_is_immutable_and_idempotent(self):
        packet, manifest = self.write_transcript_packet()
        acquisition = self.root / "acquisition"
        (acquisition / "review-packet.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )
        (acquisition / "source-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        destination = self.root / "runtime-review-inbox"
        spool_root = self.root / "runtime-transcript-spool"
        first = stage_transcript_review_bundle(
            acquisition, destination, spool_root
        )
        second = stage_transcript_review_bundle(
            acquisition, destination, spool_root
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["packet_hash"], packet["content_hash"])
        self.assertEqual(first["formal_authority_writes"], 0)
        target = RawSpool(spool_root, max_total_bytes=1_000_000)
        self.assertTrue(
            target.object_exists(manifest["assembled_object"]["content_hash"])
        )
        case = Path(first["review_case_path"])
        self.assertEqual(
            json.loads((case / "review-packet.json").read_text())["id"],
            packet["id"],
        )

    def test_config_rejects_core_path_and_standalone_server_fields(self):
        value = {
            "candidate_staging_path": str(self.root / "candidate.sqlite"),
            "transcript_review_directory": str(self.review_dir),
            "reconcile_interval_seconds": 60,
            "core_db": str(self.root / "core.sqlite"),
        }
        with self.assertRaises(ResearchReviewControlError):
            ResearchReviewControlConfig.from_mapping(value)
        value.pop("core_db")
        value["host"] = "127.0.0.1"
        with self.assertRaises(ResearchReviewControlError):
            ResearchReviewControlConfig.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
