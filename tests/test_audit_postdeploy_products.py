import importlib.util
import json
import unittest
from pathlib import Path

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore, canonical_json, content_hash
from tests.test_research_planner import NOW, directive, plan_from_response, response, state


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_postdeploy_products.py"
SPEC = importlib.util.spec_from_file_location("audit_postdeploy_products", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ResearchPlanAuditTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore()
        self.authority = CoverageMissionAuthority(self.store)
        self.work_ref = "work:cockpit-plan:test"
        self.plan = plan_from_response(
            state(), response(directive()), created_at=NOW)
        self.authority.record_research_plan(
            self.plan, decided_by="actor:test", work_order_ref=self.work_ref)
        self.row = self.store.connection.execute(
            "SELECT * FROM coverage_mission_research_plans WHERE work_order_ref=?",
            (self.work_ref,),
        ).fetchone()

    def tearDown(self):
        self.store.close()

    def test_real_coverage_mission_plan_replays_without_embedded_id(self):
        wire = audit.exact_research_plan(self.row)
        self.assertNotIn("id", wire)
        self.assertEqual(wire, self.plan)
        row, resolved = audit.stored_plan_for_work(
            self.store.connection, self.work_ref)
        self.assertEqual(row["plan_id"], self.row["plan_id"])
        self.assertEqual(resolved, self.plan)

    def _mutated_row(self, *, column=None, value=None, wire_change=None):
        row = dict(self.row)
        if wire_change is not None:
            wire = json.loads(row["plan_json"])
            wire_change(wire)
            body = dict(wire)
            body.pop("content_hash", None)
            wire["content_hash"] = content_hash(body)
            row["plan_json"] = canonical_json(wire)
            row["content_hash"] = wire["content_hash"]
        if column is not None:
            row[column] = value
        return row

    def test_identity_columns_hash_and_task_drift_are_rejected(self):
        mutations = {
            "plan id": self._mutated_row(column="plan_id", value="mission-research-plan:foreign"),
            "mission": self._mutated_row(
                column="mission_version_ref", value="coverage-mission-version:foreign:1"),
            "assessment column": self._mutated_row(column="assessment", value="foreign"),
            "hash column": self._mutated_row(column="content_hash", value="f" * 64),
            "task": self._mutated_row(
                wire_change=lambda wire: wire.__setitem__("task_ref", "task:foreign:0.1")),
        }
        for label, row in mutations.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                    RuntimeError, "formal research plan authority differs"):
                audit.exact_research_plan(row)

    def test_null_sufficiency_cannot_alias_an_empty_list(self):
        row = self._mutated_row(
            wire_change=lambda wire: wire.__setitem__("sufficiency", None))
        self.assertEqual(row["sufficiency_json"], "[]")
        with self.assertRaisesRegex(RuntimeError, "formal research plan authority differs"):
            audit.exact_research_plan(row)

    def test_one_work_order_cannot_claim_multiple_stored_plans(self):
        moved = state(figures_by_company={})
        second = plan_from_response(moved, response(), created_at=NOW)
        self.authority.record_research_plan(
            second, decided_by="actor:test", work_order_ref=self.work_ref)
        with self.assertRaisesRegex(RuntimeError, "multiple stored plans"):
            audit.stored_plan_for_work(self.store.connection, self.work_ref)

    def test_one_work_order_cannot_exist_in_both_schedulers(self):
        seen = set()
        audit.claim_planner_work(seen, self.work_ref)
        with self.assertRaisesRegex(RuntimeError, "both schedulers"):
            audit.claim_planner_work(seen, self.work_ref)

    def test_stored_plan_is_distinct_from_scheduler_formal_authority(self):
        plan = {"plan_ref": self.row["plan_id"]}
        self.assertEqual(
            audit.planner_classification(plan, []),
            "stored_plan_without_succeeded_formal")
        self.assertEqual(
            audit.planner_classification(plan, [{"terminal_state": "succeeded"}]),
            "stored_plan_with_succeeded_formal")
        self.assertEqual(
            audit.planner_classification(None, [{"terminal_state": "failed"}]),
            "planner_terminal_failed")


if __name__ == "__main__":
    unittest.main()
