"""The feed lanes read the universe off the mission pointer, whatever the mission is called."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace

from dalton_core.coverage_mission import CoverageMissionNotFound
from dalton_core.mission_feed_lane import _mission_universe


class FeedLaneMissionPointerTests(unittest.TestCase):
    def _server(self, rows):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE coverage_mission_pointer (mission_ref TEXT, mission_version_id TEXT)")
        connection.executemany("INSERT INTO coverage_mission_pointer VALUES (?, ?)", rows)
        missions = {
            "coverage-mission-version:ws-abc:2": {
                "universe": [{"company_ref": "company:ticker:msft", "ticker": "MSFT"}]},
        }
        return SimpleNamespace(
            store=SimpleNamespace(connection=connection),
            coverage_mission=SimpleNamespace(
                mission=lambda ref: missions[ref],
                active_mission=lambda ref: (_ for _ in ()).throw(
                    AssertionError("must not look a mission up by the legacy name"))),
        )

    def test_a_workspace_mission_is_found_through_the_pointer(self):
        server = self._server([("coverage-mission:ws-abc", "coverage-mission-version:ws-abc:2")])
        self.assertEqual(_mission_universe(server),
                         [{"company_ref": "company:ticker:msft", "ticker": "MSFT"}])

    def test_no_pointer_is_not_found(self):
        with self.assertRaises(CoverageMissionNotFound):
            _mission_universe(self._server([]))


if __name__ == "__main__":
    unittest.main()
