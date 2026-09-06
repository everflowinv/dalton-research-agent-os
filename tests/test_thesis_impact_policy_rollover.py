"""Governance policy rollover parks a passed verification instead of looping.

Offline fixtures only: no broker, no provider call, no live state.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dalton_core.thesis_impact import (
    ThesisImpactIneligible,
    ThesisImpactVerificationPolicySuperseded,
)
from dalton_core.thesis_impact_control import POLICY_SUPERSEDED_STATUS
from dalton_core.thesis_impact_production import (
    BLOCKED_STATUSES,
    SETTLED_STATUSES,
    ThesisImpactProductionError,
    ThesisImpactProductionRunner,
    _failure_report,
    main,
)
from dalton_core.writer_protocol import RemoteError
from tests import test_thesis_impact as thesis_fixtures


OWNER = "human:policy-rollover-test"


class PolicyRolloverAuthorityTests(unittest.TestCase):
    """Reuse the existing authority fixture without re-running its tests."""

    def setUp(self):
        fixture = thesis_fixtures.ThesisImpactTests("run")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store = fixture.store
        self.authority = fixture.authority
        self.claim_ref = fixture.claim_ref
        self.thesis_ref = fixture.thesis_ref
        self.complete_model = fixture.complete_model
        self.record_assessment = fixture.record_assessment

    def passed_assessment(self):
        recorded, _ = self.record_assessment()
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
            "impact-b",
        )
        verified = self.authority.verify_assessment(
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
            verifier_result_envelope_ref=result_ref,
        )
        return assessment, verified["verification"]

    def roll_policy(self, version_id="policy:test-rollover:2"):
        return self.store.create_policy(
            {**self.store.active_policy_version().policy},
            policy_version_id=version_id,
            actor_ref=OWNER,
            change_reason="Test-only governance policy version rollover.",
        )

    def counts(self):
        return {
            table: self.store.connection.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]
            for table in (
                "thesis_impact_assessments",
                "thesis_impact_verifications",
                "thesis_versions",
                "claim_versions",
            )
        }

    def test_rollover_is_reported_exactly_and_stays_ineligible(self):
        assessment, verification = self.passed_assessment()
        self.assertIsNone(
            self.authority.superseded_verification(
                claim_version_ref=self.claim_ref, thesis_version_ref=self.thesis_ref
            )
        )
        self.assertEqual(
            self.authority.eligible_assessment(assessment["id"])["assessment"]["id"],
            assessment["id"],
        )
        before = self.counts()
        self.roll_policy()

        with self.assertRaises(ThesisImpactVerificationPolicySuperseded) as caught:
            self.authority.eligible_assessment(assessment["id"])
        detail = caught.exception.detail
        self.assertIsInstance(caught.exception, ThesisImpactIneligible)
        self.assertEqual(detail["verification_ref"], verification["id"])
        self.assertEqual(detail["assessment_ref"], assessment["id"])
        self.assertEqual(
            detail["verification_policy_version_ref"],
            verification["policy_version_ref"],
        )
        self.assertEqual(
            detail["active_policy_version_ref"],
            self.store.active_policy_version().id,
        )
        self.assertNotEqual(
            detail["verification_policy_version_ref"],
            detail["active_policy_version_ref"],
        )
        self.assertEqual(
            self.authority.superseded_verification(
                claim_version_ref=self.claim_ref, thesis_version_ref=self.thesis_ref
            ),
            detail,
        )
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            self.store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

    def test_reject_and_missing_verification_keep_their_own_ineligibility(self):
        recorded, _ = self.record_assessment()
        assessment = recorded["assessment"]
        # No verification at all: unchanged generic ineligibility.
        with self.assertRaises(ThesisImpactIneligible) as missing:
            self.authority.eligible_assessment(assessment["id"])
        self.assertNotIsInstance(
            missing.exception, ThesisImpactVerificationPolicySuperseded
        )
        verifier, result_ref = self.complete_model(
            "impact-verifier",
            [assessment["id"], self.claim_ref, self.thesis_ref],
            {
                "schema_version": "0.1",
                "assessment_ref": assessment["id"],
                "assessment_hash": assessment["content_hash"],
                "verdict": "reject",
                "findings": [
                    {"code": "OVERCLAIM", "message": "Rationale is not supported."}
                ],
            },
            "impact-b",
        )
        self.authority.verify_assessment(
            assessment_ref=assessment["id"],
            verifier_invocation=verifier,
            verifier_result_envelope_ref=result_ref,
        )
        self.roll_policy(version_id="policy:test-rollover:reject")
        # A reject is never parked as "waiting for a policy decision".
        with self.assertRaises(ThesisImpactIneligible) as rejected:
            self.authority.eligible_assessment(assessment["id"])
        self.assertNotIsInstance(
            rejected.exception, ThesisImpactVerificationPolicySuperseded
        )
        self.assertIsNone(
            self.authority.superseded_verification(
                claim_version_ref=self.claim_ref, thesis_version_ref=self.thesis_ref
            )
        )

    def test_unrelated_binding_is_not_parked(self):
        self.passed_assessment()
        self.roll_policy(version_id="policy:test-rollover:other")
        self.assertIsNone(
            self.authority.superseded_verification(
                claim_version_ref=self.claim_ref,
                thesis_version_ref="thesis-version:not-this-one",
            )
        )


class RunnerOutcomeTests(unittest.TestCase):
    """Classification and exit codes; no writer, scheduler or model involved."""

    @staticmethod
    def classify(statuses):
        results = [{"status": status} for status in statuses]
        runner = ThesisImpactProductionRunner.__new__(ThesisImpactProductionRunner)
        with patch.object(
            ThesisImpactProductionRunner, "_client"
        ) as client, patch.object(
            ThesisImpactProductionRunner, "_worker"
        ), patch.object(
            ThesisImpactProductionRunner, "_run_target", side_effect=results
        ), patch(
            "dalton_core.thesis_impact_production.Scheduler"
        ), patch(
            "dalton_core.thesis_impact_production.ModelRouter"
        ), patch(
            "dalton_core.thesis_impact_production.ThesisImpactBudgetStore"
        ):
            client.return_value.thesis_impact_targets.return_value = [
                {"plan_version_ref": "plan:" + str(i), "thesis_ref": "thesis:x"}
                for i, _ in enumerate(statuses)
            ]
            runner.config = type("C", (), {
                "budget_policy_version_id": "budget:test:1",
                "day_cap_micros": 1,
                "scheduler_db": ":memory:",
                "model_router_db": ":memory:",
                "budget_db": ":memory:",
                "company_thesis_refs": {},
                "max_targets": 10,
            })()
            return runner.run_once()

    def test_parked_targets_are_reported_not_treated_as_success_or_fault(self):
        self.assertEqual(self.classify(["eligible"])["status"], "completed")
        self.assertEqual(
            self.classify([POLICY_SUPERSEDED_STATUS])["status"],
            "blocked_pending_human",
        )
        self.assertEqual(
            self.classify(["eligible", POLICY_SUPERSEDED_STATUS])["status"],
            "blocked_pending_human",
        )
        # A real fault alongside a parked target still reports incomplete.
        self.assertEqual(
            self.classify([POLICY_SUPERSEDED_STATUS, "assessment_failed"])["status"],
            "incomplete",
        )
        self.assertFalse(SETTLED_STATUSES & BLOCKED_STATUSES)

    def test_exit_codes_separate_blocked_from_fault_and_success(self):
        cases = [
            ({"status": "completed", "results": []}, 0),
            ({"status": "idle"}, 0),
            ({"status": "blocked_pending_human", "results": []}, 3),
            ({"status": "incomplete", "results": []}, 2),
        ]
        for result, expected in cases:
            with self.subTest(result=result["status"]), patch.object(
                ThesisImpactProductionRunner, "run_once", return_value=result
            ), patch(
                "dalton_core.thesis_impact_production.load_config"
            ):
                self.assertEqual(main(["--config", "unused.json"]), expected)

    def test_failure_report_names_the_blocker_without_leaking_content(self):
        remote = RemoteError("conflict", "request conflicts with existing immutable data")
        report = _failure_report(remote)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_type"], "RemoteError")
        self.assertEqual(report["error_code"], "conflict")
        self.assertIn("immutable data", report["error_message"])
        secret = _failure_report(RuntimeError("token=" + "s" * 500))
        self.assertEqual(set(secret), {"status", "error_type"})
        local = _failure_report(ThesisImpactProductionError("x" * 500))
        self.assertEqual(len(local["error_message"]), 200)
        self.assertEqual(json.loads(json.dumps(report)), report)


if __name__ == "__main__":
    unittest.main()
