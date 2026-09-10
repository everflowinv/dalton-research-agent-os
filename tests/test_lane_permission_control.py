from __future__ import annotations

import sqlite3
import unittest

from dalton_core.lane_failure_class import LaneFailureBudget
from dalton_core.lane_permission_control import (
    current_permission, record_controlled_failure,
)


class Launcher:
    pass


class PermissionControlTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE coverage_mission_pointer "
            "(mission_ref TEXT, mission_version_id TEXT)")
        self.connection.execute(
            "INSERT INTO coverage_mission_pointer VALUES ('m', 'mv1')")
        self.connection.execute(
            "CREATE TABLE governance_policy_pointer "
            "(pointer_id TEXT, policy_version_id TEXT)")
        self.connection.execute(
            "INSERT INTO governance_policy_pointer VALUES ('p', 'pv1')")
        self.addCleanup(self.connection.close)
        self.budget = LaneFailureBudget("test")
        self.mission = {"id": "mv1", "autonomy": {"may_write": []}}

    def record_permission(self, company: str):
        return record_controlled_failure(
            self.budget, f"{company}|same-input", self.mission, Launcher(),
            connection=self.connection, status="gated",
            reason="mission does not grant model_line",
        )

    def test_policy_pointer_change_retires_same_input_permission(self):
        self.record_permission("company:a")
        self.assertEqual(len(self.budget.permission_items()), 1)
        self.connection.execute(
            "UPDATE governance_policy_pointer SET policy_version_id='pv2'")
        current_permission(
            self.budget, "company:a|same-input", self.mission, Launcher(),
            connection=self.connection,
        )
        self.assertEqual(self.budget.permission_items(), [])

    def test_refreshing_a_does_not_release_b(self):
        self.record_permission("company:a")
        self.record_permission("company:b")
        changed = {**self.mission, "id": "mv2"}
        current_permission(
            self.budget, "company:a|same-input", changed, Launcher(),
            connection=self.connection,
        )
        self.assertEqual(
            [row["item_key"].split("|", 1)[0]
             for row in self.budget.permission_items()],
            ["company:b"],
        )

    def test_gated_dependency_reason_remains_a_dependency(self):
        decision = record_controlled_failure(
            self.budget, "company:a|same-input", self.mission, Launcher(),
            status="gated", reason="model_unavailable",
        )
        self.assertEqual(decision.classification.failure_class,
                         "dependency_unavailable")


if __name__ == "__main__":
    unittest.main()
