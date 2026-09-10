from __future__ import annotations

from types import SimpleNamespace

from dalton_core.cockpit_model import _failed_work_trace, build_work
from dalton_core.controlled_failure_redrive import apply, prepare
from dalton_core.controlled_lane_reentry import eligible_controlled_reentries
from dalton_core.scheduler import ResultEnvelope
from tests.test_controlled_failure_redrive import ControlledFailureRedriveTests, NOW


class ControlledLaneReentryTests(ControlledFailureRedriveTests):
    def cockpit_failure(self):
        work = build_work(
            purpose="plan", request_id="lane-request", prompt="draft",
            mission_version_ref=self.mission["id"], mission_version_hash=None,
            max_input_tokens=100, max_output_tokens=10,
            max_cost_usd=0.0001, max_seconds=10, created_at=NOW.isoformat(),
        )
        self.scheduler.enqueue(work)
        lease = self.scheduler.claim("worker:lane", work_order_id=work.id)
        result = ResultEnvelope(
            schema_version="0.1", id="result:lane-host-failed",
            created_at=NOW.isoformat(), work_order_ref=work.id,
            invocation_ref="invocation:lane", status="failed", outputs={},
            actual_side_effects=(), usage_refs=(), artifact_refs=(),
            error={"code": "HOST_COMPLETION_FAILED"}, metadata={},
        )
        self.scheduler.complete(
            work.id, 1, "worker:lane", lease["lease_token"], result,
            idempotency_key="complete:lane",
        )
        admission = self.budget.admit(
            policy_version_id="budget:test:1", day="2026-09-10",
            work_order_ref=work.id, attempt_number=1, phase="assessment",
            route_decision_ref="route:lane", reserved_micros=100,
            mission_binding={
                "mission_ref": "coverage-mission:test",
                "mission_version_ref": self.mission["id"],
                "mission_version_hash": self.mission["content_hash"],
                "max_daily_paid_calls": 10, "max_daily_cost_micros": 1000,
            },
        )
        self.budget.settle(admission["admission_id"], actual_micros=1)
        candidate = prepare(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            old_work_order_ref=work.id, openclaw_root=self.openclaw_root,
        )
        saved = apply(
            scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            candidate=candidate, expected_candidate_hash=candidate["candidate_hash"],
        )
        view = SimpleNamespace(connection=self.scheduler.connection)
        view.work_order_authority = lambda work_id: self.scheduler.work_order_authority(work_id)
        trace = _failed_work_trace(
            view, work, purpose="plan", request_id="lane-request")
        return work, trace, ":operator-recovery:" + saved["content_hash"][:16]

    def test_exact_approved_unconsumed_trace_is_eligible_then_enqueue_consumes_it(self):
        _work, trace, suffix = self.cockpit_failure()
        summary = {"failed_model_traces": [trace, trace]}
        eligible = eligible_controlled_reentries(
            summary, scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            mission=self.mission,
        )
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["authorization_suffix"], suffix)

        recovery = build_work(
            purpose="plan", request_id="lane-request" + suffix, prompt="draft",
            mission_version_ref=self.mission["id"], mission_version_hash=None,
            max_input_tokens=100, max_output_tokens=10,
            max_cost_usd=0.0001, max_seconds=10, created_at=NOW.isoformat(),
        )
        self.scheduler.enqueue(recovery)
        self.assertEqual(eligible_controlled_reentries(
            summary, scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            mission=self.mission,
        ), [])

    def test_malformed_or_mission_mismatched_trace_is_not_eligible(self):
        _work, trace, _suffix = self.cockpit_failure()
        broken = dict(trace)
        broken["formal_result_envelope_hash"] = "0" * 64
        self.assertEqual(eligible_controlled_reentries(
            {"failed_model_traces": [broken]}, scheduler_db=self.scheduler_db,
            budget_db=self.budget_db, mission=self.mission,
        ), [])
        changed = {**self.mission, "id": "coverage-mission-version:test:2"}
        self.assertEqual(eligible_controlled_reentries(
            {"failed_model_traces": [trace]}, scheduler_db=self.scheduler_db,
            budget_db=self.budget_db, mission=changed,
        ), [])

    def test_missing_or_oversized_trace_list_is_quiet_and_read_only(self):
        before_scheduler = self.scheduler_db.read_bytes()
        before_budget = self.budget_db.read_bytes()
        self.assertEqual(eligible_controlled_reentries(
            {}, scheduler_db=self.scheduler_db, budget_db=self.budget_db,
            mission=self.mission,
        ), [])
        self.assertEqual(eligible_controlled_reentries(
            {"failed_model_traces": [{}] * 17}, scheduler_db=self.scheduler_db,
            budget_db=self.budget_db, mission=self.mission,
        ), [])
        self.assertEqual(self.scheduler_db.read_bytes(), before_scheduler)
        self.assertEqual(self.budget_db.read_bytes(), before_budget)
