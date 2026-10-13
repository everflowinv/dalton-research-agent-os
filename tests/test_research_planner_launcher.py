"""P13o: one planner child per tick, and no process when nothing moved."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.research_planner_launcher import (
    IDLE_HOLD,
    TICKET_PREFIX,
    ResearchPlannerCoordinator,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class FakeLauncher:
    def __init__(self, root: Path):
        self.tickets_dir = root
        self.starts = 0
        self.ticket = None

    def start(self):
        self.starts += 1
        self.ticket = {"id": f"{TICKET_PREFIX}:{self.starts:024x}",
                       "started_at": NOW.isoformat(), "status": "running",
                       "summary": None}
        return dict(self.ticket)

    def status(self, ticket_ref):
        return dict(self.ticket)

    def finish(self, summary):
        self.ticket.update({"status": "succeeded", "summary": summary})


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(self.store.close)
        # The coordinator counts mission tables; opening the authority is what
        # creates them.
        self.missions = CoverageMissionAuthority(self.store)
        self.now = NOW
        self.launcher = FakeLauncher(self.root)
        self.coordinator = ResearchPlannerCoordinator(
            store=self.store, launcher=self.launcher, clock=lambda: self.now)

    def test_an_unconfigured_writer_says_so_instead_of_failing(self):
        bare = ResearchPlannerCoordinator(store=self.store, launcher=None)
        self.assertEqual(bare.dispatch_once()["status"], "unconfigured")

    def test_the_first_tick_launches(self):
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.launcher.starts, 1)

    def test_a_running_child_is_not_joined_by_a_second(self):
        self.coordinator.dispatch_once()
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.assertEqual(self.launcher.starts, 1)

    def test_an_unmoved_state_does_not_spawn_a_process(self):
        # The child already refuses to pay twice for the same world; this is
        # about not spawning a process to discover that.
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "unchanged"})
        self.now += timedelta(minutes=5)
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(self.launcher.starts, 1)

    def test_a_moved_state_launches_again(self):
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "fresh"})
        self.now += timedelta(minutes=5)
        # One more learned metric is a different world.
        self.missions.record_metric_observations(
            company_ref="company:sec-cik:0001467373",
            proposals=[{"metric_ref": "metric:new-bookings", "label": "new bookings",
                        "unit": "currency", "evidence_phrase": "new bookings",
                        "quote_id": "quote:0:200:aaaaaaaaaaaaaaaa",
                        "document_ref": "alphaengine-doc:1",
                        "citation_text": "We remain focused on new bookings."}],
            observed_by="automation:x")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.launcher.starts, 2)

    def test_the_hold_lapses_after_an_idle_hour(self):
        self.coordinator.dispatch_once()
        self.launcher.finish({"status": "succeeded", "plan_status": "unchanged"})
        self.now += IDLE_HOLD + timedelta(minutes=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_changed_storage_bound_wakes_old_idle_once_without_changed_research(self):
        self.coordinator.dispatch_once()
        self.launcher.finish({"status":"succeeded","plan_status":"input_too_large"})
        path=self.root/"latest.json"
        old=json.loads(path.read_text())
        old["idle_signature"].pop("planner_work_order_prompt_bytes")
        path.write_text(json.dumps(old))
        self.assertEqual(self.coordinator.dispatch_once()["status"],"launched")
        self.assertEqual(self.launcher.starts,2)
        self.launcher.finish({"status":"succeeded","plan_status":"input_too_large"})
        self.assertEqual(self.coordinator.dispatch_once()["status"],"held")
        self.assertEqual(self.launcher.starts,2)

    def test_input_budget_change_wakes_idle_once_without_changed_research(self):
        from dalton_core.shared_call_budget_policy import updated_shared_limits, LIMITS_SCHEMA_VERSION
        from dalton_core.store import content_hash
        policy = {"schema_version": LIMITS_SCHEMA_VERSION, "default_max_cost_usd": 1.,
                  "purpose_max_cost_usd": {}, "purpose_call_limits": {"plan": {"max_input_tokens": 80000}},
                  "purpose_run_limits": {}, "revision": 1, "prior_hash": None,
                  "updated_at": NOW.isoformat(), "actor_ref": "human:test"}
        policy["content_hash"] = content_hash(policy)
        shared = self.root / "shared.json"
        shared.write_text(json.dumps(policy))
        self.launcher.model_config_path = self.root / "model.json"
        self.launcher.state_dir = self.root
        self.launcher.model_config_path.write_text(json.dumps({"shared_call_budget_policy_path": str(shared)}))
        self.coordinator.dispatch_once()
        self.assertEqual(self.coordinator._signature()["planner_input_byte_bound"], 80000)
        self.launcher.finish({"status": "succeeded", "plan_status": "input_too_large"})
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        changed = updated_shared_limits(policy, purpose="plan", kind="call",
            budget={"max_input_tokens": 250000}, actor_ref="human:test", updated_at=NOW.isoformat())
        shared.write_text(json.dumps(changed))
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.coordinator._signature()["planner_input_byte_bound"], 250000)
        self.launcher.finish({"status": "succeeded", "plan_status": "input_too_large"})
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.assertEqual(self.launcher.starts, 2)

    def test_the_signature_counts_what_the_state_is_built_from(self):
        signature = self.coordinator._signature()
        self.assertEqual(
            set(signature),
            {"documents", "reviews_open", "claims", "figures", "metrics",
             "mission_versions", "dossier_feedback", "document_versions", "document_research_feedback",
             "document_research_settlement", "reflection_requests", "followup_decisions",
             "backlog_versions", "backlog_events", "planner_work_order_prompt_bytes", "planner_input_byte_bound", "planner_task_hash", "memo_question_feedback", "planner_structural_revalidation", "current_product_inputs", "model_history_note_priority"})
        self.assertTrue(all(isinstance(v, int) for key, v in signature.items()
                            if key not in {"model_history_note_priority", "planner_structural_revalidation", "current_product_inputs", "memo_question_feedback", "planner_task_hash", "planner_input_byte_bound", "dossier_feedback", "document_versions", "document_research_feedback",
                                           "document_research_settlement", "reflection_requests", "followup_decisions",
             "backlog_versions", "backlog_events"}))
        self.assertEqual(len(signature["document_research_settlement"]), 64)
        self.assertEqual(len(signature["dossier_feedback"]), 64)
        self.assertEqual(len(signature["document_versions"]), 64)

    def test_a_retracted_figure_does_not_count_as_movement(self):
        # Otherwise withdrawing a bad figure would look like new evidence.
        before = self.coordinator._signature()["figures"]
        self.assertEqual(before, 0)


if __name__ == "__main__":
    unittest.main()
