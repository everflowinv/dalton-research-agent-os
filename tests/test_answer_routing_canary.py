"""End-to-end replay for the isolated ACN answer-routing canary."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AnswerRoutingCanaryTests(unittest.TestCase):
    def test_recorded_acn_authorities_route_direct_and_fail_closed(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_isolated_acn_answer_canary.py"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["mode"], "isolated-in-memory-recorded-authority"
        )
        self.assertEqual(
            result["company_ref"], "company:sec-cik:0001467373"
        )
        self.assertEqual(result["direct_route"], "answer_direct")
        self.assertEqual(result["direct_reason_codes"], ["answer_direct_ready"])
        self.assertEqual(
            set(result["direct_claim_refs"]),
            {
                "claim:acn:q3fy26:new-bookings-total",
                "claim:acn:q3fy26:new-bookings-growth-local-currency",
            },
        )
        self.assertEqual(
            result["direct_evidence_refs"],
            ["evidence:acn:q3fy26:earnings-release"],
        )
        self.assertEqual(result["unmatched_route"], "recommend_agenda_item")
        self.assertIn(
            "question_not_admitted", result["unmatched_reason_codes"]
        )
        self.assertEqual(result["stale_route"], "recommend_agenda_item")
        self.assertIn("stale_evidence", result["stale_reason_codes"])

        for field in (
            "route_table_counts_unchanged",
            "route_total_changes_unchanged",
            "route_authority_fingerprint_unchanged",
            "old_subject_rejected_after_policy_rotation",
        ):
            self.assertTrue(result[field], field)
        self.assertFalse(result["route_write_performed"])
        self.assertEqual(
            result["answer_policy_version_refs"],
            [
                "answer-policy-version:isolated-acn:1",
                "answer-policy-version:isolated-acn:2",
            ],
        )
        self.assertEqual(result["answer_binding_count"], 2)
        self.assertEqual(result["thesis_version_count"], 1)
        self.assertEqual(result["context_thesis_count"], 1)
        self.assertEqual(result["context_driver_pack_count"], 1)
        self.assertEqual(result["context_company_overlay_count"], 1)
        self.assertEqual(result["context_formal_claim_count"], 2)
        self.assertEqual(result["context_formal_evidence_count"], 1)
        self.assertEqual(result["formal_evidence_version_count"], 1)
        self.assertEqual(result["formal_claim_version_count"], 6)
        self.assertEqual(result["formal_evidence_relation_count"], 6)
        self.assertEqual(result["recorded_model_invocation_count"], 1)
        self.assertEqual(result["recorded_cost_entry_count"], 0)
        self.assertEqual(result["paid_model_calls"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["live_database_writes"], 0)
        self.assertEqual(result["integrity_check"], "ok")
        for field in (
            "direct_context_pack_hash", "direct_route_decision_hash"
        ):
            self.assertRegex(result[field], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
