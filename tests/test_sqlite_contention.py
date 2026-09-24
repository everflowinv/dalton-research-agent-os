"""2026-09-24: shared SQLite writers wait out a burst instead of failing it.

After a deploy, six legacy cockpit results and four publication-worker
products failed with ``OperationalError: database is locked`` at the model
router's ``BEGIN IMMEDIATE``: the router and scheduler waited 5 s for the write
lock and the day ledger the implicit 5 s of ``sqlite3.connect``, while route()
held that lock across an unindexed scan of every decision ever made.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.scheduler import Scheduler


class SqliteContentionTests(unittest.TestCase):
    def test_every_shared_writer_waits_thirty_seconds(self) -> None:
        from dalton_core.model_router import ModelRouter
        from dalton_core.sqlite_contention import SQLITE_BUSY_TIMEOUT_MS
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        self.assertEqual(SQLITE_BUSY_TIMEOUT_MS, 30_000)
        with tempfile.TemporaryDirectory() as root:
            for store in (ModelRouter(Path(root) / "r.sqlite"),
                          Scheduler(Path(root) / "s.sqlite"),
                          ThesisImpactBudgetStore(Path(root) / "b.sqlite")):
                with store:
                    self.assertEqual(store.connection.execute(
                        "PRAGMA busy_timeout").fetchone()[0], 30_000)

    def test_the_retry_is_bounded_and_only_for_lock_errors(self) -> None:
        from dalton_core.sqlite_contention import retry_on_sqlite_lock

        clock = [0.0]
        waits: list[float] = []

        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        def locked():
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            retry_on_sqlite_lock(locked, deadline_seconds=60, sleep=sleep,
                                 monotonic=lambda: clock[0])
        self.assertLessEqual(sum(waits), 60)
        self.assertGreater(sum(waits), 55)
        self.assertLessEqual(max(waits), 5.0)

        calls = []

        def broken():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: x")

        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            retry_on_sqlite_lock(broken, sleep=sleep, monotonic=lambda: clock[0])
        self.assertEqual(len(calls), 1)

    def test_the_route_lookup_of_a_works_latest_decision_uses_an_index(self) -> None:
        from dalton_core.model_router import ModelRouter

        with tempfile.TemporaryDirectory() as root, \
                ModelRouter(Path(root) / "r.sqlite") as router:
            plan = " ".join(row[-1] for row in router.connection.execute(
                "EXPLAIN QUERY PLAN SELECT decision_id, decision_json FROM "
                "model_route_decisions WHERE work_order_id=? AND capability=? "
                "ORDER BY decision_sequence DESC LIMIT 1", ("w", "c")))
        self.assertIn("model_route_decisions_by_work", plan)
        self.assertNotIn("SCAN model_route_decisions", plan)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
