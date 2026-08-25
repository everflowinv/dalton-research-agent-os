"""End-to-end replay for the isolated S5 connector-to-staging canary."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AnswerRefreshConnectorCanaryTests(unittest.TestCase):
    def test_observed_refresh_rechecks_real_connector_and_staging_authorities(
        self,
    ) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/run_isolated_answer_refresh_connector_canary.py",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["mode"],
            "isolated-synthetic-transport-real-connector-authorities",
        )
        self.assertEqual(result["route"], "answer_after_refresh")
        self.assertEqual(
            result["connector_execution_statuses"],
            ["admitted", "admitted", "admitted", "complete"],
        )
        self.assertEqual(result["connector_physical_attempt_count"], 1)
        self.assertEqual(result["candidate_stage_request_count"], 1)
        self.assertTrue(result["untrusted_source_rejected"])
        self.assertEqual(result["outcome_kind"], "observed")
        self.assertEqual(
            result["terminal_state"], "evidence_observed_for_review"
        )
        self.assertEqual(result["replay_status"], "duplicate")
        self.assertTrue(result["formal_answer_authority_counts_unchanged"])
        self.assertEqual(
            result["formal_connector_authority_counts"],
            {
                "claim_versions": 0,
                "evidence_versions": 0,
                "thesis_versions": 0,
            },
        )
        self.assertEqual(
            result["candidate_semantic_verification_status"], "unverified"
        )
        self.assertEqual(result["scheduler_completion_status"], "succeeded")
        self.assertEqual(result["external_network_calls"], 0)
        self.assertEqual(result["paid_model_calls"], 0)
        self.assertEqual(result["live_database_writes"], 0)
        self.assertEqual(
            result["integrity_check"],
            {
                "answer_core": "ok",
                "candidate_staging": "ok",
                "connector_core": "ok",
            },
        )
        for field in (
            "dispatch_hash",
            "connector_plan_version_hash",
            "source_envelope_hash",
            "raw_artifact_version_hash",
            "candidate_evidence_hash",
            "candidate_claim_hash",
            "outcome_receipt_hash",
        ):
            self.assertRegex(result[field], r"^[0-9a-f]{64}$", field)


if __name__ == "__main__":
    unittest.main()
