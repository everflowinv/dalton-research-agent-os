from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.contracts import ResultEnvelope, WorkOrder
from dalton_core.controlled_failure_redrive import (
    ControlledFailureRedriveError,
    apply,
    approved_request,
    prepare,
)
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from integrations.openclaw_host_patches.patch_controlled_completion_transport import PATCHED, PATCHED_BIND


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class ControlledFailureRedriveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.openclaw_root = root / "openclaw"
        (self.openclaw_root / "dist").mkdir(parents=True)
        (self.openclaw_root / "package.json").write_text(
            '{"version":"2026.9.3"}', encoding="utf-8")
        (self.openclaw_root / "dist" / "simple-completion-execution-test.mjs").write_text(
            PATCHED + "\n" + PATCHED_BIND, encoding="utf-8")
        self.scheduler_db = root / "scheduler.sqlite"
        self.budget_db = root / "budget.sqlite"
        self.scheduler = Scheduler(self.scheduler_db, clock=lambda: NOW)
        self.addCleanup(self.scheduler.close)
        self.budget = ThesisImpactBudgetStore(self.budget_db, clock=lambda: NOW)
        self.addCleanup(self.budget.close)
        self.budget.register_policy(
            policy_version_id="budget:test:1", day_cap_micros=1000
        )
        self.mission = {
            "id": "coverage-mission-version:test:1",
            "content_hash": "a" * 64,
        }
        self.work = WorkOrder(
            schema_version="0.1", id="work:cockpit-test-old",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
            question="test", requested_capabilities=("research",),
            runtime_profile_ref="runtime:test", budget={"max_attempts": 1},
            idempotency_key="test", declared_side_effects=(), status="ready",
            input_refs=(), metadata={
                "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
            },
        )
        self.scheduler.enqueue(self.work)
        lease = self.scheduler.claim("worker:test", work_order_id=self.work.id)
        result = ResultEnvelope(
            schema_version="0.1", id="result:test-host-failed",
            created_at=NOW.isoformat(), work_order_ref=self.work.id,
            invocation_ref="invocation:test", status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "HOST_COMPLETION_FAILED"}, metadata={},
        )
        self.scheduler.complete(
            self.work.id, 1, "worker:test", lease["lease_token"], result,
            idempotency_key="complete:test",
        )
        self.admission = self.budget.admit(
            policy_version_id="budget:test:1", day="2026-09-10",
            work_order_ref=self.work.id, attempt_number=1, phase="assessment",
            route_decision_ref="route:test", reserved_micros=100,
            mission_binding={
                "mission_ref": "coverage-mission:test",
                "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
                "max_daily_paid_calls": 10,
                "max_daily_cost_micros": 1000,
            },
        )
        self.settlement = self.budget.settle(
            self.admission["admission_id"], actual_micros=1
        )

    def test_prepare_is_read_only_and_apply_is_single_hash_bound_record(self):
        before_scheduler = self.scheduler_db.read_bytes()
        before_budget = self.budget_db.read_bytes()
        before_sidecars = sorted(path.name for path in self.budget_db.parent.iterdir())
        before_work = self.scheduler.work_order_authority(self.work.id)
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        self.assertEqual(self.scheduler_db.read_bytes(), before_scheduler)
        self.assertEqual(self.budget_db.read_bytes(), before_budget)
        self.assertEqual(
            sorted(path.name for path in self.budget_db.parent.iterdir()),
            before_sidecars,
        )
        saved = apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )
        self.assertEqual(saved["status"], "fresh")
        self.assertEqual(apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )["status"], "duplicate")
        self.assertEqual(self.scheduler.work_order_authority(self.work.id), before_work)
        formal = self.scheduler.formal_result(self.work.id)
        self.assertEqual(
            approved_request(self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
                             formal=formal, mission=self.mission),
            ":operator-recovery:" + saved["content_hash"][:16],
        )

    def test_tampered_review_or_changed_mission_is_refused(self):
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=self.work.id, openclaw_root=self.openclaw_root,
        )
        changed = copy.deepcopy(candidate)
        changed["mission_version_hash"] = "b" * 64
        with self.assertRaises(ControlledFailureRedriveError):
            apply(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                  candidate=changed,
                  expected_candidate_hash=candidate["candidate_hash"])
        self.assertIsNone(approved_request(
            self.scheduler_db, self.budget_db, old_work_order_ref=self.work.id,
            formal=self.scheduler.formal_result(self.work.id),
            mission={**self.mission, "content_hash": "b" * 64},
        ))

    def test_success_and_unaccounted_failures_are_ineligible(self):
        with self.assertRaises(ControlledFailureRedriveError):
            prepare(scheduler_db=self.scheduler_db, budget_db=self.budget_db,
                    old_work_order_ref="work:missing", openclaw_root=self.openclaw_root)


if __name__ == "__main__":
    unittest.main()
