from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_review_control import (
    ResearchReviewControlApplication,
    ResearchReviewControlConfig,
    ResearchReviewControlError,
    ResearchReviewControlPlane,
    _subject_for_login,
)


HASH = "a" * 64
LOGIN = "reviewer@example.com"


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
            "claim": self.claim, "evidence": self.evidence, "decision": self.decision,
            "commit_state": state,
        }]

    def decide(self, **params):
        self.decide_params = params
        self.decision = {
            "id": "human-review:1", "content_hash": "d" * 64,
            "reviewer_ref": params["reviewer_ref"], "verdict": params["verdict"],
        }
        return {
            "write_status": "fresh", "decision_ref": "human-review:1",
            "decision_hash": "d" * 64, "verdict": params["verdict"],
            "commit_state": "queued" if params["verdict"] == "accept" else "not_applicable",
        }

    def pending_commits(self, *, limit=100):
        if self.decision is None or self.decision["verdict"] != "accept" or self.commit_results:
            return []
        return [{"decision": self.decision, "evidence": self.evidence, "claim": self.claim}]

    def record_commit_result(self, decision_ref, **params):
        self.commit_results.append((decision_ref, params))
        return {"state": "committed" if params.get("ledger_result") else "failed"}

    def close(self):
        return None


class FakeWriter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def commit_reviewed_candidate(self, **params):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("writer failed")
        return {"status": "fresh", "claim_version_ref": "claim-version:1"}


class ResearchReviewControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = ResearchReviewControlConfig.from_mapping({
            "host": "127.0.0.1", "port": 8794,
            "tailscale_host": "dalton.example.ts.net",
            "allowed_tailscale_logins": [LOGIN],
            "candidate_staging_path": str(root / "candidate.sqlite"),
            "writer_socket": str(root / "writer.sock"),
            "token_config": str(root / "tokens.json"),
            "reconcile_interval_seconds": 60,
        })

    def test_plane_injects_identity_semantics_and_commits_accept(self):
        authority = FakeAuthority()
        writer = FakeWriter()
        plane = ResearchReviewControlPlane(self.config, authority=authority, writer=writer)
        result = plane.record(LOGIN, {
            "request_id": "request-12345678",
            "candidate_claim_ref": authority.claim["id"],
            "candidate_claim_hash": authority.claim["content_hash"],
            "verdict": "accept", "rationale": "Checked the source.",
            "findings": [], "proposed_revisions": None,
        })
        self.assertEqual(result["commit_state"], "committed")
        self.assertEqual(authority.decide_params["reviewer_ref"], _subject_for_login(LOGIN))
        self.assertEqual(
            authority.decide_params["reviewed_semantics"]["normalized_statement"],
            authority.claim["normalized_statement"],
        )
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(authority.commit_results), 1)

    def test_writer_failure_leaves_durable_pending_result(self):
        authority = FakeAuthority()
        writer = FakeWriter(fail=True)
        plane = ResearchReviewControlPlane(self.config, authority=authority, writer=writer)
        result = plane.record(LOGIN, {
            "request_id": "request-abcdefgh",
            "candidate_claim_ref": authority.claim["id"],
            "candidate_claim_hash": authority.claim["content_hash"],
            "verdict": "accept", "rationale": "Checked the source.",
            "findings": [], "proposed_revisions": None,
        })
        self.assertEqual(result["commit_state"], "pending")
        self.assertEqual(authority.commit_results[0][1]["error_code"], "writer_rejected")

    def test_application_csrf_and_closed_body(self):
        authority = FakeAuthority()
        plane = ResearchReviewControlPlane(
            self.config, authority=authority, writer=FakeWriter()
        )
        app = ResearchReviewControlApplication(self.config, plane)
        _, session, _ = app.session(LOGIN, None)
        body = json.dumps({
            "request_id": "request-12345678",
            "candidate_claim_ref": authority.claim["id"],
            "candidate_claim_hash": authority.claim["content_hash"],
            "verdict": "reject", "rationale": "Statement is too broad.",
            "findings": [], "proposed_revisions": None,
        }).encode()
        with self.assertRaises(PermissionError):
            app.post(LOGIN, session, "wrong", body)
        result = app.post(LOGIN, session, session.csrf, body)
        self.assertEqual(result["verdict"], "reject")
        malformed = json.dumps({"request_id": "request-12345678"}).encode()
        with self.assertRaises(ResearchReviewControlError):
            app.post(LOGIN, session, session.csrf, malformed)

    def test_config_rejects_non_loopback(self):
        value = {
            "host": "0.0.0.0", "port": 8794,
            "tailscale_host": "dalton.example.ts.net",
            "allowed_tailscale_logins": [LOGIN],
            "candidate_staging_path": "/tmp/candidate.sqlite",
            "writer_socket": "/tmp/writer.sock", "token_config": "/tmp/tokens.json",
            "reconcile_interval_seconds": 60,
        }
        with self.assertRaises(ResearchReviewControlError):
            ResearchReviewControlConfig.from_mapping(value)


if __name__ == "__main__":
    unittest.main()
