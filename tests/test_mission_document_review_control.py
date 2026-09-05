"""Cockpit document queue: real citation lineage, isolated authorities, no network."""

from __future__ import annotations

import copy
import unittest

from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_review_control import (
    ResearchReviewControlConfig, ResearchReviewControlError, ResearchReviewControlPlane,
    _subject_for_login,
)
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.store import content_hash
from dalton_core.writer_server import WriterServer, WriterServerError, Principal, CORE_OPERATIONS
from tests.test_transcript_qualitative_candidate import QualitativeTranscriptHarness, SUBJECT


class MissionQueue:
    def __init__(self, document_ref):
        self.row = {
            "review_id": "mission-document-review:test", "mission_version_ref": "mission:1",
            "company_ref": SUBJECT, "document_ref": document_ref,
            "source_ref": "source:alphaengine", "state": "awaiting_human_extraction",
        }
        self.resolutions = []

    def document_reviews(self, ref, **_kwargs):
        return [copy.deepcopy(self.row)]

    def mission(self, ref):
        return {"universe": [{"company_ref": SUBJECT, "ticker": "ACN"}]}

    def document_review(self, ref):
        return copy.deepcopy(self.row)

    def resolve_document_review(self, review_id, **params):
        self.resolutions.append((review_id, params))
        return {"status": "fresh", **params}


class MissionDocumentReviewControlTests(unittest.TestCase):
    def setUp(self):
        self.h = QualitativeTranscriptHarness()
        self.addCleanup(self.h.close)
        self.staging = CandidateStagingStore(self.h.staging_path)
        self.addCleanup(self.staging.close)
        self.staged = self.h.stage(self.staging)
        self.review = HumanReviewAuthority(self.h.staging_path)
        self.addCleanup(self.review.close)
        self.claim_ref = self.review.list_candidates()[0]["claim"]["id"]
        self.missions = MissionQueue(self.h.manifest["document_ref"])
        # Exercise the writer handlers on the same Core/citation/staging
        # authorities used by the socket server, without opening live state.
        self.server = WriterServer(":memory:", "/unused.sock", {
            "core": Principal("core", "test-token", CORE_OPERATIONS, unrestricted=True),
        })
        self.server._store = self.h.core
        self.server._candidate_staging = self.staging
        self.server._candidate_review = self.review
        self.server._coverage_mission = self.missions

    def resolution(self, **overrides):
        return {
            "review_id": self.missions.row["review_id"], "resolution": "extraction_staged",
            "candidate_claim_version_ref": self.claim_ref, "actor_ref": "human:owner",
            "rationale": "Checked the source and candidate.", **overrides,
        }

    def test_exact_document_candidate_is_listed_and_bound_without_accepting(self):
        counts = {table: self.h.count(table) for table in ("claim_versions", "evidence_versions")}
        result = self.server._op_mission_document_reviews({"mission_version_ref": "mission:1", "include_candidates": True})
        item = result["reviews"][0]
        self.assertEqual(item["review_hash"], content_hash(self.missions.row))
        self.assertEqual([c["ref"] for c in item["candidates"]], [self.claim_ref])
        result = self.server._op_resolve_mission_document_review(self.resolution(expected_review_hash=item["review_hash"]))
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(self.review.candidate_status(self.claim_ref)["review_state"], "staged")
        self.assertEqual(counts, {table: self.h.count(table) for table in counts})

    def test_foreign_document_company_source_and_nonexact_ref_are_rejected(self):
        for field, value in (
            ("document_ref", "alphaengine-doc:unrelated"),
            ("company_ref", "company:other"),
            ("source_ref", "source:other"),
        ):
            with self.subTest(field=field):
                original = self.missions.row[field]
                self.missions.row[field] = value
                listed = self.server._op_mission_document_reviews({"mission_version_ref": "mission:1", "include_candidates": True})
                self.assertEqual(listed["reviews"][0]["candidates"], [])
                with self.assertRaises(WriterServerError):
                    self.server._op_resolve_mission_document_review(self.resolution())
                self.missions.row[field] = original
        stable = self.review.candidate_status(self.claim_ref)["claim"]["candidate_claim_ref"]
        with self.assertRaises(WriterServerError):
            self.server._op_resolve_mission_document_review(self.resolution(candidate_claim_version_ref=stable))
        self.assertEqual(self.missions.resolutions, [])

    def test_http_plane_closes_request_shape_and_binds_identity_and_snapshot(self):
        calls = []
        def governance(*_args, **kwargs):
            calls.append(kwargs)
            if kwargs["operation"] == "mission_document_reviews":
                return self.server._op_mission_document_reviews({"mission_version_ref": "mission:1", **kwargs["params"]})
            return self.server._op_resolve_mission_document_review({"actor_ref": kwargs["actor_ref"], **kwargs["params"]})
        config = ResearchReviewControlConfig(self.h.staging_path, self.h.staging_path.parent, 60)
        plane = ResearchReviewControlPlane(
            config, writer_socket=self.h.staging_path.parent / "unused.sock",
            token_config=self.h.staging_path.parent / "unused.json",
            authority=self.review, writer=object(), governance_call=governance,
        )
        view = plane.document_review_view("owner@example.com")
        body = {
            "request_id": "document-test-1", "review_id": self.missions.row["review_id"],
            "review_hash": view["items"][0]["review_hash"], "resolution": "extraction_staged",
            "candidate_claim_version_ref": self.claim_ref, "rationale": "Checked original.",
        }
        plane.record_document_review("owner@example.com", body)
        self.assertEqual(calls[-1]["actor_ref"], _subject_for_login("owner@example.com"))
        self.assertEqual(calls[-1]["params"]["expected_review_hash"], body["review_hash"])
        count = len(calls)
        for changes in (
            {"actor_ref": "human:forged"}, {"rationale": " "}, {"review_hash": "bad"},
            {"resolution": "accept"}, {"resolution": "dismissed"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ResearchReviewControlError):
                plane.record_document_review("owner@example.com", {**body, **changes})
        self.assertEqual(len(calls), count)
        def broken(*_args, **_kwargs):
            raise RuntimeError("private backend details")
        plane._governance_call = broken
        with self.assertRaisesRegex(ResearchReviewControlError, "queue is unavailable"):
            plane.document_review_view("owner@example.com")
        with self.assertRaisesRegex(ResearchReviewControlError, "reload the queue"):
            plane.record_document_review("owner@example.com", body)


if __name__ == "__main__":
    unittest.main()
