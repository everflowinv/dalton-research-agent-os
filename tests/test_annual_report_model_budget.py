from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from dalton_core.annual_report_qualitative import RegisteredAnnualReportDraftWorker
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


class AnnualReportModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.budget = ThesisImpactBudgetStore(clock=lambda: NOW)
        self.budget.register_policy(
            policy_version_id="budget:annual:test", day_cap_micros=5_000_000
        )
        self.worker = object.__new__(RegisteredAnnualReportDraftWorker)
        self.worker.budget_store = self.budget
        self.worker.budget_policy_ref = "budget:annual:test"
        self.worker.mission_resolver = lambda ref: {
            "id": ref, "content_hash": "a" * 64, "mission_ref": "mission:test",
            "budget": {"max_daily_paid_calls": 4, "max_daily_cost_usd": 4.0},
        }
        self.worker.clock = lambda: NOW
        self.worker.admission = None
        self.worker._admission_identity = None
        self.work = SimpleNamespace(
            id="work:annual:draft:test",
            budget={"max_cost_usd": 1.0},
            metadata={"budget_db": ":memory:", "budget_policy_ref": "budget:annual:test",
                      "retrieval_proof": {"registration": {
                "mission_version_ref": "mission-version:test",
            }}},
        )
        self.route = {"id": "route:test", "attempt_number": 1}

    def tearDown(self):
        self.budget.close()

    def _row(self):
        return self.budget.connection.execute(
            "SELECT a.reserved_micros,s.actual_micros FROM thesis_impact_day_admissions a "
            "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
            "WHERE a.work_order_ref=?", (self.work.id,),
        ).fetchone()

    def test_unknown_cost_keeps_full_reservation(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        self.assertIsNone(self.worker._after_accounting(
            self.work, self.route,
            {"cost": {"amount_micros": 1234, "cost_status": "estimated", "id": "cost:x"},
             "usage": {"id": "usage:x"}},
        ))
        row = self._row()
        self.assertEqual(row["reserved_micros"], 1_000_000)
        self.assertIsNone(row["actual_micros"])

    def test_actual_cost_settles_and_capacity_releases(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        self.worker._after_accounting(
            self.work, self.route,
            {"cost": {"amount_micros": 4321, "cost_status": "actual", "id": "cost:x"},
             "usage": {"id": "usage:x"}},
        )
        self.assertEqual(self._row()["actual_micros"], 4321)

        other = SimpleNamespace(
            id="work:annual:draft:capacity", budget={"max_cost_usd": 1.0},
            metadata=self.work.metadata,
        )
        self.worker._before_model_call(other, {"id": "route:capacity", "attempt_number": 1}, {}, False)
        self.worker._after_capacity_deferred(other, {}, None)
        row = self.budget.connection.execute(
            "SELECT s.actual_micros FROM thesis_impact_day_admissions a JOIN "
            "thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
            "WHERE a.work_order_ref=?", (other.id,),
        ).fetchone()
        self.assertEqual(row["actual_micros"], 0)

    def test_each_attempt_has_an_independent_admission(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        self.worker._before_model_call(
            self.work, {"id": "route:test:2", "attempt_number": 2}, {}, False
        )
        rows = self.budget.connection.execute(
            "SELECT attempt_number,reserved_micros FROM thesis_impact_day_admissions "
            "WHERE work_order_ref=? ORDER BY attempt_number", (self.work.id,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 1_000_000), (2, 1_000_000)])


if __name__ == "__main__":
    unittest.main()
