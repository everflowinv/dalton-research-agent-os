from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from dalton_core.annual_report_qualitative import RegisteredAnnualReportDraftWorker
from dalton_core.openclaw_model_adapter import OpenClawModelAdapterError
from dalton_core.thesis_impact_budget import (
    ThesisImpactBudgetConflict,
    ThesisImpactBudgetStore,
)


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
        self.worker.mission_resolver = lambda ref, company: {
            "id": ref, "content_hash": "a" * 64, "mission_ref": "mission:test",
            "budget": {"max_daily_paid_calls": 4, "max_daily_cost_usd": 4.0,
                       "max_alphaengine_calls_24h": 0},
            "outer_budget": {
                "mandate_ref": "mandate:test", "mandate_version_ref": "mandate-version:test",
                "mandate_version_hash": "b" * 64,
                "governance_policy_ref": "governance:test",
                "governance_policy_version_ref": "governance-version:test",
                "governance_policy_version_hash": "c" * 64,
                "max_daily_paid_calls": 8, "max_daily_cost_micros": 8_000_000,
            },
        }
        self.worker.clock = lambda: NOW
        self.worker.admission = None
        self.worker._admission_identity = None
        self.work = SimpleNamespace(
            id="work:annual:draft:test",
            budget={"max_cost_usd": 1.0},
            metadata={"budget_db": ":memory:", "budget_policy_ref": "budget:annual:test",
                      "stage": "qualitative_model_draft",
                      "retrieval_proof": {"registration": {
                "mission_version_ref": "mission-version:test",
                "company_ref": "company:test",
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

    def test_restart_replay_after_midnight_reuses_original_admission_day(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        restarted = object.__new__(RegisteredAnnualReportDraftWorker)
        for name in ("budget_store", "budget_policy_ref", "mission_resolver"):
            setattr(restarted, name, getattr(self.worker, name))
        restarted.clock = lambda: NOW + timedelta(days=1)
        restarted.admission = None
        restarted._admission_identity = None
        restarted._before_model_call(self.work, self.route, {}, True)
        self.assertEqual(restarted.admission["status"], "duplicate")
        self.assertEqual(restarted.admission["day"], NOW.date().isoformat())
        self.assertEqual(
            self.budget.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_admissions"
            ).fetchone()[0],
            1,
        )

    def test_exact_admission_reader_rejects_tampered_immutable_record(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        row = self.budget.connection.execute(
            "SELECT admission_id,record_json FROM thesis_impact_day_admissions"
        ).fetchone()
        triggers = self.budget.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='thesis_impact_day_admissions'"
        ).fetchall()
        for trigger in triggers:
            self.budget.connection.execute(
                f'DROP TRIGGER "{trigger["name"]}"'
            )
        altered = row["record_json"].replace(
            '"reserved_micros":1000000', '"reserved_micros":999999'
        )
        self.budget.connection.execute(
            "UPDATE thesis_impact_day_admissions SET record_json=? "
            "WHERE admission_id=?",
            (altered, row["admission_id"]),
        )
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "drifted"):
            self.budget.admission(
                work_order_ref=self.work.id,
                attempt_number=1,
                phase="assessment",
            )

    def test_budget_policy_reader_rejects_tampered_immutable_record(self):
        row = self.budget.connection.execute(
            "SELECT policy_version_id,record_json "
            "FROM thesis_impact_budget_policies"
        ).fetchone()
        triggers = self.budget.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='thesis_impact_budget_policies'"
        ).fetchall()
        for trigger in triggers:
            self.budget.connection.execute(
                f'DROP TRIGGER "{trigger["name"]}"'
            )
        altered = row["record_json"].replace(
            '"day_cap_micros":5000000', '"day_cap_micros":90000000'
        )
        self.budget.connection.execute(
            "UPDATE thesis_impact_budget_policies SET record_json=? "
            "WHERE policy_version_id=?",
            (altered, row["policy_version_id"]),
        )
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "drifted"):
            self.budget.policy(row["policy_version_id"])

    def test_pool_refusal_stops_before_model_transport(self):
        original = self.worker.mission_resolver

        def exhausted(ref, company):
            mission = original(ref, company)
            return {
                **mission,
                "budget": {
                    **mission["budget"],
                    "max_daily_cost_usd": 1.0,
                },
            }

        self.worker.mission_resolver = exhausted
        with self.assertRaisesRegex(
            OpenClawModelAdapterError,
            "budget/mission admission rejected",
        ):
            self.worker._before_model_call(self.work, self.route, {}, False)
        self.assertEqual(
            self.budget.connection.execute(
                "SELECT COUNT(*) FROM thesis_impact_day_admissions"
            ).fetchone()[0],
            0,
        )
        rejection = self.budget.connection.execute(
            "SELECT pool,record_json FROM model_budget_pool_rejections"
        ).fetchone()
        self.assertEqual(rejection["pool"], "coverage")
        self.assertIn('"reason":"pool_exhausted"', rejection["record_json"])

    def test_overrun_alert_retains_reservation_and_freezes_later_admission(self):
        self.worker._before_model_call(self.work, self.route, {}, False)
        result = self.worker._after_accounting(
            self.work,
            self.route,
            {
                "cost": {
                    "amount_micros": 1_000_001,
                    "cost_status": "actual",
                    "id": "cost:overrun",
                },
                "usage": {"id": "usage:overrun"},
            },
        )
        self.assertEqual(result, "MODEL_COST_EXCEEDED_RESERVATION")
        exact = self.budget.admission(
            work_order_ref=self.work.id,
            attempt_number=1,
            phase="assessment",
        )
        self.assertIsNone(exact["settlement"])
        self.assertEqual(exact["admission"]["reserved_micros"], 1_000_000)
        alert = self.budget.connection.execute(
            "SELECT detail_json FROM thesis_impact_alerts"
        ).fetchone()
        self.assertIn("model_reservation_overrun", alert["detail_json"])
        self.worker.admission = None
        self.worker._admission_identity = None
        with self.assertRaisesRegex(
            OpenClawModelAdapterError,
            "budget/mission admission rejected",
        ):
            self.worker._before_model_call(
                self.work,
                {"id": "route:after-overrun", "attempt_number": 2},
                {},
                False,
            )
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "overrun"):
            self.budget.admit(
                policy_version_id="budget:annual:test",
                day=NOW.date().isoformat(),
                work_order_ref="work:annual:after-overrun",
                attempt_number=1,
                phase="assessment",
                route_decision_ref="route:after-overrun",
                reserved_micros=1,
            )


if __name__ == "__main__":
    unittest.main()
