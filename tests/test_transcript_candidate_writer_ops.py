"""S7c-2: writer human-governance op ``stage_transcript_candidate``.

Isolated Core with the 8/24 ACN fixture (Core-held AlphaEngine authority,
correction set, eligible citation) -> in-process writer -> human principal
stages a qualitative candidate into the shared CandidateStaging file ->
``transcript_candidate_status`` reads it back.  No network, no live Core,
no Ledger write.
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from pathlib import Path

from dalton_core.research_auto_commit import (
    ResearchAutoCommitRejected,
    authorize_policy_candidate,
)
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import TRANSCRIPT_CORE_AUTHORITY_MODE
from dalton_core.store import GateRejected
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    DASHBOARD_CONTROL_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)

from tests.test_transcript_qualitative_candidate import (
    ASPECT,
    BASIS,
    PERIOD,
    STATEMENT,
    SUBJECT,
    QualitativeTranscriptHarness,
)

OWNER = "human:lumos"
GOVERNANCE_TOKEN = "governance-token-s7c2"
DASHBOARD_TOKEN = "dashboard-token-s7c2"
AUTOMATION_TOKEN = "automation-token-s7c2"
CORE_TOKEN = "core-token-s7c2"


class WriterHarness:
    """In-process writer over the harness Core, sharing its spool and staging file."""

    def __init__(self, *, with_staging: bool = True) -> None:
        self.h = QualitativeTranscriptHarness()
        root = Path(self.h.core_harness.temp.name)
        self.socket = root / "run" / "w.sock"
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref=OWNER,
            ),
            "dashboard-control": Principal(
                "dashboard-control", DASHBOARD_TOKEN, DASHBOARD_CONTROL_OPERATIONS,
                actor_ref="bridge:tailscale-dashboard",
            ),
            # Holds the governance operation set but is not a human principal.
            "automation-stager": Principal(
                "automation-stager", AUTOMATION_TOKEN, HUMAN_GOVERNANCE_OPERATIONS,
                actor_ref="system:transcript-candidate-stager",
            ),
        }
        self.server = WriterServer(
            root / "core.sqlite", self.socket, principals,
            transcript_spool_dir=root / "spool",
            candidate_staging_path=self.h.staging_path if with_staging else None,
        )
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def client(self, token: str) -> WriterClient:
        return WriterClient(str(self.socket), token, timeout=30)

    def params(self, **overrides) -> dict:
        base = {
            "correction_set_ref": self.h.correction_set["id"],
            "citation_ref": self.h.citation["id"],
            "subject_ref": SUBJECT,
            "metric_or_aspect": ASPECT,
            "period": PERIOD,
            "basis": BASIS,
            "normalized_statement": STATEMENT,
            "idempotency_key": "stage:acn:qualitative:writer:1",
        }
        base.update(overrides)
        return base

    def staging_counts(self) -> dict:
        # Own connection: the writer's staging handle lives on its store thread.
        conn = sqlite3.connect(self.h.staging_path)
        try:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("candidate_claim_versions", "candidate_stage_requests")
            }
        finally:
            conn.close()

    def close(self) -> None:
        self.server.stop()
        self.thread.join(timeout=10)
        self.h.close()


class StageTranscriptCandidateOpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = WriterHarness()
        self.addCleanup(self.w.close)

    def test_human_principal_stages_qualitative_candidate_and_reads_status(self) -> None:
        governance = self.w.client(GOVERNANCE_TOKEN)
        result = governance.call("stage_transcript_candidate", self.w.params())
        self.assertEqual(result["write_status"], "fresh")
        self.assertEqual(result["staging"]["write_status"], "fresh")
        self.assertEqual(result["claim"]["claim_kind"], "qualitative")
        self.assertIsNone(result["claim"]["value"])
        self.assertIsNone(result["claim"]["numeric_spec_ref"])
        self.assertEqual(result["claim"]["actor_ref"], OWNER)
        self.assertEqual(result["evidence"]["actor_ref"], OWNER)
        self.assertEqual(result["material"]["provenance_mode"], TRANSCRIPT_CORE_AUTHORITY_MODE)
        self.assertEqual(result["source_verification"]["verdict"], "pass")
        self.assertFalse(
            any(item["status"] == "fail" for item in result["source_verification"]["findings"])
        )
        self.assertEqual(result["citation"]["id"], self.w.h.citation["id"])

        # The Cockpit-side reader of the same file sees exactly one staged pair.
        review = HumanReviewAuthority(self.w.h.staging_path)
        try:
            listed = review.list_candidates()
        finally:
            review.close()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["claim"]["id"], result["staging"]["candidate_claim_ref"])
        self.assertIsNone(listed[0]["decision"])

        # Status by exact version ref and by stable candidate ref.
        status = governance.call(
            "transcript_candidate_status",
            {"candidate_claim_ref": result["staging"]["candidate_claim_ref"]},
        )
        self.assertEqual(status["review_state"], "staged")
        self.assertEqual(status["claim_kind"], "qualitative")
        self.assertIsNone(status["decision"])
        self.assertIsNone(status["commit_state"])
        self.assertEqual(status["candidate_claim_hash"], result["staging"]["candidate_claim_hash"])
        by_stable = governance.call(
            "transcript_candidate_status",
            {"candidate_claim_ref": result["claim"]["candidate_claim_ref"]},
        )
        self.assertEqual(by_stable["candidate_claim_ref"], status["candidate_claim_ref"])

        # Core untouched: staging only, no Ledger Evidence / Claim.
        for table in ("evidence_versions", "claim_versions", "reviewed_candidate_commits"):
            self.assertEqual(self.w.h.count(table), 0)

    def test_idempotent_replay_does_not_stage_twice(self) -> None:
        governance = self.w.client(GOVERNANCE_TOKEN)
        first = governance.call("stage_transcript_candidate", self.w.params())
        self.assertEqual(first["write_status"], "fresh")
        counts_after_first = self.w.staging_counts()
        again = governance.call("stage_transcript_candidate", self.w.params())
        self.assertEqual(again["write_status"], "duplicate")
        self.assertEqual(
            again["staging"]["candidate_claim_ref"], first["staging"]["candidate_claim_ref"]
        )
        self.assertEqual(self.w.staging_counts(), counts_after_first)
        self.assertEqual(counts_after_first["candidate_claim_versions"], 1)
        self.assertEqual(counts_after_first["candidate_stage_requests"], 1)
        # Same key with a different statement is a conflict, not a second stage.
        with self.assertRaises(RemoteError) as conflict:
            governance.call(
                "stage_transcript_candidate",
                self.w.params(normalized_statement=STATEMENT + " (edited)"),
            )
        self.assertEqual(conflict.exception.code, "conflict")
        self.assertEqual(self.w.staging_counts(), counts_after_first)

    def test_non_human_principals_are_refused(self) -> None:
        # Not in the dashboard operation set at all.
        with self.assertRaises(RemoteAuthorizationError):
            self.w.client(DASHBOARD_TOKEN).call("stage_transcript_candidate", self.w.params())
        with self.assertRaises(RemoteAuthorizationError):
            self.w.client(DASHBOARD_TOKEN).call(
                "transcript_candidate_status", {"candidate_claim_ref": "x"}
            )
        # Has the operation set but resolves to a non-human actor.
        with self.assertRaises(RemoteAuthorizationError):
            self.w.client(AUTOMATION_TOKEN).call("stage_transcript_candidate", self.w.params())
        # A human principal cannot smuggle a different actor.
        with self.assertRaises(RemoteAuthorizationError):
            self.w.client(GOVERNANCE_TOKEN).call(
                "stage_transcript_candidate", self.w.params(actor_ref="human:other")
            )
        self.assertEqual(self.w.staging_counts()["candidate_claim_versions"], 0)

    def test_missing_or_foreign_citation_is_rejected(self) -> None:
        governance = self.w.client(GOVERNANCE_TOKEN)
        with self.assertRaises(RemoteError) as missing:
            governance.call(
                "stage_transcript_candidate",
                self.w.params(citation_ref="transcript-claim-citation:" + "0" * 64),
            )
        self.assertEqual(missing.exception.code, "rejected")
        with self.assertRaises(RemoteError) as foreign:
            governance.call(
                "stage_transcript_candidate",
                self.w.params(correction_set_ref="transcript-correction-set:other"),
            )
        self.assertEqual(foreign.exception.code, "rejected")
        with self.assertRaises(RemoteError) as unknown_field:
            governance.call(
                "stage_transcript_candidate",
                self.w.params(verification_mode="connector_authority"),
            )
        self.assertEqual(unknown_field.exception.code, "protocol_error")
        self.assertEqual(self.w.staging_counts()["candidate_claim_versions"], 0)
        with self.assertRaises(RemoteError) as not_found:
            governance.call(
                "transcript_candidate_status", {"candidate_claim_ref": "candidate-claim:missing"}
            )
        self.assertEqual(not_found.exception.code, "not_found")

    def test_policy_path_rejects_writer_staged_qualitative_candidate(self) -> None:
        governance = self.w.client(GOVERNANCE_TOKEN)
        result = governance.call("stage_transcript_candidate", self.w.params())
        review = HumanReviewAuthority(self.w.h.staging_path)
        try:
            bundle = review.candidate_bundle(result["staging"]["candidate_claim_ref"])
        finally:
            review.close()
        with self.assertRaisesRegex(ResearchAutoCommitRejected, "explicit human review"):
            authorize_policy_candidate(
                connection=self.w.h.core.connection, policy_version={},
                evidence=bundle["evidence"], claim=bundle["claim"],
            )
        with self.assertRaisesRegex(GateRejected, "explicit human review"):
            self.w.h.core.commit_policy_candidate(
                evidence=bundle["evidence"], claim=bundle["claim"],
                idempotency_key="policy:qualitative:writer",
            )
        self.assertEqual(self.w.h.count("claim_versions"), 0)


class WriterWithoutStagingTests(unittest.TestCase):
    def test_stage_is_rejected_when_staging_is_not_configured(self) -> None:
        w = WriterHarness(with_staging=False)
        self.addCleanup(w.close)
        with self.assertRaises(RemoteError) as rejected:
            w.client(GOVERNANCE_TOKEN).call("stage_transcript_candidate", w.params())
        self.assertEqual(rejected.exception.code, "rejected")


if __name__ == "__main__":
    unittest.main()
