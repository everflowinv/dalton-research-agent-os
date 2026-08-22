"""Closed tests for the thesis-impact day-budget and alert authority."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.thesis_impact_budget import (
    ALERT_MAX_DELIVERY_ATTEMPTS,
    ThesisImpactBudgetConflict,
    ThesisImpactBudgetStore,
    ThesisImpactBudgetValidationError,
    ThesisImpactDayBudgetExceeded,
)


FIXED = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def store(path: str | Path = ":memory:") -> ThesisImpactBudgetStore:
    return ThesisImpactBudgetStore(path, clock=lambda: FIXED)


def admit(
    authority: ThesisImpactBudgetStore,
    *,
    work: str,
    attempt: int = 1,
    phase: str = "assessment",
    reserved: int = 100_000,
    policy: str = "budget-policy:day:1",
    day: str = "2026-08-22",
):
    return authority.admit(
        policy_version_id=policy,
        day=day,
        work_order_ref=work,
        attempt_number=attempt,
        phase=phase,
        route_decision_ref=f"route-decision:{work}:{attempt}",
        reserved_micros=reserved,
    )


class BudgetPolicyTests(unittest.TestCase):
    def test_policy_registration_is_immutable_and_chained(self) -> None:
        authority = store()
        self.addCleanup(authority.close)
        first = authority.register_policy(
            policy_version_id="budget-policy:day:1", day_cap_micros=1_000_000
        )
        self.assertEqual(first["status"], "fresh")
        duplicate = authority.register_policy(
            policy_version_id="budget-policy:day:1", day_cap_micros=1_000_000
        )
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaises(ThesisImpactBudgetConflict):
            authority.register_policy(
                policy_version_id="budget-policy:day:1", day_cap_micros=2_000_000
            )
        with self.assertRaises(ThesisImpactBudgetConflict):
            authority.register_policy(
                policy_version_id="budget-policy:day:2",
                day_cap_micros=1_000_000,
                prior_version_id="budget-policy:missing",
            )
        second = authority.register_policy(
            policy_version_id="budget-policy:day:2",
            day_cap_micros=500_000,
            prior_version_id="budget-policy:day:1",
        )
        self.assertEqual(second["status"], "fresh")
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "already advanced"):
            authority.register_policy(
                policy_version_id="budget-policy:day:2-sibling",
                day_cap_micros=500_000,
                prior_version_id="budget-policy:day:1",
            )
        with self.assertRaises(ThesisImpactBudgetValidationError):
            authority.register_policy(
                policy_version_id="budget-policy:day:3", day_cap_micros=0
            )

    def test_duplicate_semantics_do_not_depend_on_retry_time(self) -> None:
        current = [FIXED]
        authority = ThesisImpactBudgetStore(clock=lambda: current[0])
        self.addCleanup(authority.close)
        first_policy = authority.register_policy(
            policy_version_id="budget-policy:day:1", day_cap_micros=1_000_000
        )
        current[0] += timedelta(minutes=1)
        duplicate_policy = authority.register_policy(
            policy_version_id="budget-policy:day:1", day_cap_micros=1_000_000
        )
        self.assertEqual(duplicate_policy["status"], "duplicate")
        self.assertEqual(duplicate_policy["created_at"], first_policy["created_at"])
        first = admit(authority, work="work:a", reserved=100_000)
        current[0] += timedelta(minutes=1)
        duplicate = admit(authority, work="work:a", reserved=100_000)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["created_at"], first["created_at"])
        settled = authority.settle(first["admission_id"], actual_micros=50_000)
        current[0] += timedelta(minutes=1)
        duplicate_settlement = authority.settle(
            first["admission_id"], actual_micros=50_000
        )
        self.assertEqual(duplicate_settlement["status"], "duplicate")
        self.assertEqual(
            duplicate_settlement["created_at"], settled["created_at"]
        )


class DayAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = store()
        self.addCleanup(self.authority.close)
        self.authority.register_policy(
            policy_version_id="budget-policy:day:1", day_cap_micros=1_000_000
        )

    def test_admit_settle_and_day_summary_arithmetic(self) -> None:
        first = admit(self.authority, work="work:a", reserved=400_000)
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(
            self.authority.day_summary(
                policy_version_id="budget-policy:day:1", day="2026-08-22"
            ),
            {
                "schema_version": "0.1",
                "policy_version_id": "budget-policy:day:1",
                "day": "2026-08-22",
                "day_cap_micros": 1_000_000,
                "committed_micros": 400_000,
                "remaining_micros": 600_000,
            },
        )
        settlement = self.authority.settle(
            first["admission_id"], actual_micros=250_000, usage_entry_ref="usage:1"
        )
        self.assertEqual(settlement["status"], "fresh")
        self.assertEqual(settlement["status"], "fresh")
        duplicate = self.authority.settle(
            first["admission_id"], actual_micros=250_000, usage_entry_ref="usage:1"
        )
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.authority.settle(first["admission_id"], actual_micros=999_999)
        summary = self.authority.day_summary(
            policy_version_id="budget-policy:day:1", day="2026-08-22"
        )
        self.assertEqual(summary["committed_micros"], 250_000)
        second = admit(self.authority, work="work:b", reserved=750_000)
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(
            self.authority.day_summary(
                policy_version_id="budget-policy:day:1", day="2026-08-22"
            )["committed_micros"],
            1_000_000,
        )

    def test_settlement_cannot_exceed_admitted_reservation(self) -> None:
        opened = admit(self.authority, work="work:over-settle", reserved=10_000)
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "reservation"):
            self.authority.settle(opened["admission_id"], actual_micros=10_001)

    def test_policy_advance_keeps_same_day_spend_and_closes_old_version(self) -> None:
        first = admit(self.authority, work="work:v1", reserved=400_000)
        self.authority.register_policy(
            policy_version_id="budget-policy:day:2",
            day_cap_micros=500_000,
            prior_version_id="budget-policy:day:1",
        )
        summary = self.authority.day_summary(
            policy_version_id="budget-policy:day:2", day="2026-08-22"
        )
        self.assertEqual(summary["committed_micros"], 400_000)
        duplicate = admit(self.authority, work="work:v1", reserved=400_000)
        self.assertEqual(duplicate["admission_id"], first["admission_id"])
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "superseded"):
            admit(self.authority, work="work:old-new", reserved=1)
        admitted = admit(
            self.authority,
            work="work:v2",
            reserved=100_000,
            policy="budget-policy:day:2",
        )
        self.assertEqual(admitted["status"], "fresh")
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            admit(
                self.authority,
                work="work:v2-over",
                reserved=1,
                policy="budget-policy:day:2",
            )

    def test_file_backed_authority_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite"
            authority = ThesisImpactBudgetStore(path, clock=lambda: FIXED)
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                authority.close()

    def test_over_cap_rejection_is_durable_and_final(self) -> None:
        admit(self.authority, work="work:a", reserved=400_000)
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as caught:
            admit(self.authority, work="work:b", reserved=700_000)
        rejection = caught.exception.rejection
        self.assertEqual(rejection["day_committed_micros"], 400_000)
        self.assertEqual(rejection["reserved_micros"], 700_000)
        self.assertEqual(rejection["day_cap_micros"], 1_000_000)
        rows = self.authority.connection.execute(
            "SELECT COUNT(*) FROM thesis_impact_day_rejections"
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        # Freeing budget via settlements must not revive a rejected identity.
        first = self.authority.connection.execute(
            "SELECT admission_id FROM thesis_impact_day_admissions "
            "WHERE work_order_ref='work:a'"
        ).fetchone()
        self.authority.settle(first["admission_id"], actual_micros=0)
        with self.assertRaises(ThesisImpactBudgetConflict):
            admit(self.authority, work="work:b", reserved=700_000)
        with self.assertRaises(ThesisImpactBudgetConflict):
            admit(self.authority, work="work:b", reserved=100_000)

    def test_admission_idempotency_and_open_reservation_conservatism(self) -> None:
        first = admit(self.authority, work="work:a", reserved=300_000)
        duplicate = admit(self.authority, work="work:a", reserved=300_000)
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaises(ThesisImpactBudgetConflict):
            admit(self.authority, work="work:a", reserved=301_000)
        # A crash between the paid call and settlement keeps the full
        # reservation counted against the day cap.
        self.assertEqual(
            self.authority.day_summary(
                policy_version_id="budget-policy:day:1", day="2026-08-22"
            )["committed_micros"],
            300_000,
        )
        with self.assertRaises(ThesisImpactDayBudgetExceeded):
            admit(self.authority, work="work:b", reserved=800_000)

    def test_closed_field_validation(self) -> None:
        for kwargs, pattern in (
            ({"day": "2026-8-22"}, "YYYY-MM-DD"),
            ({"day": "2026-02-30"}, "real date"),
            ({"phase": "calibration"}, "phase"),
            ({"attempt": 0}, "attempt_number"),
            ({"reserved": 0}, "reserved_micros"),
            ({"policy": "budget-policy:missing"}, "not registered"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(
                    (ThesisImpactBudgetValidationError, ThesisImpactBudgetConflict),
                    pattern,
                ):
                    admit(self.authority, work="work:x", **kwargs)
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.authority.settle("thesis-impact-admission:missing", actual_micros=1)

    def test_records_are_append_only(self) -> None:
        admit(self.authority, work="work:a", reserved=100_000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.authority.connection.execute(
                "UPDATE thesis_impact_day_admissions SET reserved_micros=1"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.authority.connection.execute(
                "DELETE FROM thesis_impact_day_admissions"
            )


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.authority = store(Path(self.temp.name) / "budget.sqlite")
        self.addCleanup(self.authority.close)

    def test_alert_lifecycle_is_idempotent_and_bounded(self) -> None:
        recorded = self.authority.record_alert(
            alert_id="thesis-impact-alert:abc",
            kind="day_budget_exceeded",
            severity="high",
            work_order_ref="work:a",
            phase="verification",
            detail={"day": "2026-08-22"},
        )
        self.assertEqual(recorded["status"], "fresh")
        duplicate = self.authority.record_alert(
            alert_id="thesis-impact-alert:abc",
            kind="day_budget_exceeded",
            severity="high",
            work_order_ref="work:a",
            phase="verification",
            detail={"day": "2026-08-22"},
        )
        self.assertEqual(duplicate["status"], "duplicate")
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.authority.record_alert(
                alert_id="thesis-impact-alert:abc",
                kind="work_order_failed",
                severity="medium",
                detail={},
            )
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.authority.record_alert(
                alert_id="thesis-impact-alert:abc",
                kind="day_budget_exceeded",
                severity="medium",
                work_order_ref="work:a",
                phase="verification",
                detail={"day": "2026-08-22"},
            )
        pending = self.authority.pending_alerts()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "day_budget_exceeded")
        self.assertEqual(pending[0]["state"], "pending")
        self.assertEqual(pending[0]["detail"], {"day": "2026-08-22"})

        claims = self.authority.claim_alerts(
            endpoint_ref="discord:owner", actor_ref="controller:dalton"
        )
        self.assertEqual(len(claims), 1)
        self.assertEqual(self.authority.pending_alerts()[0]["state"], "claimed")
        self.authority.record_alert_delivery(
            claims[0]["alert_id"], state="delivered"
        )
        self.assertEqual(self.authority.pending_alerts(), [])

        self.authority.record_alert(
            alert_id="thesis-impact-alert:def",
            kind="work_order_failed",
            severity="medium",
            detail={"code": "MODEL_ROUTE_REJECTED"},
        )
        for _ in range(ALERT_MAX_DELIVERY_ATTEMPTS):
            claim = self.authority.claim_alerts(
                endpoint_ref="discord:owner", actor_ref="controller:dalton"
            )
            self.assertEqual(len(claim), 1)
            self.authority.record_alert_delivery(
                claim[0]["alert_id"], state="failed", error_code="ENDPOINT_DOWN"
            )
        exhausted = self.authority.claim_alerts(
            endpoint_ref="discord:owner", actor_ref="controller:dalton"
        )
        self.assertEqual(exhausted, [])
        self.assertEqual(len(self.authority.pending_alerts()), 1)

        with self.assertRaises(ThesisImpactBudgetValidationError):
            self.authority.record_alert(
                alert_id="thesis-impact-alert:bad",
                kind="not_a_kind",
                severity="high",
                detail={},
            )
        with self.assertRaises(ThesisImpactBudgetConflict):
            self.authority.record_alert_delivery("thesis-impact-alert:missing", state="delivered")
        with self.assertRaisesRegex(ThesisImpactBudgetConflict, "claimed"):
            self.authority.record_alert_delivery(
                "thesis-impact-alert:def", state="failed", error_code="ENDPOINT_DOWN"
            )


if __name__ == "__main__":
    unittest.main()
