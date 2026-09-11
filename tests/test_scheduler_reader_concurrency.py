"""A stable read snapshot must not stop another scheduler from committing."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dalton_core.scheduler import Scheduler
from tests.test_scheduler import MutableClock, work_order


class SchedulerReaderConcurrencyTests(unittest.TestCase):
    def test_idle_sweep_reads_while_another_writer_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            clock = MutableClock()
            with Scheduler(path, clock=clock) as scheduler:
                scheduler.enqueue(work_order())
                scheduler.claim("worker:test")
                scheduler.connection.execute("PRAGMA busy_timeout=1")
                with closing(sqlite3.connect(path, isolation_level=None)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    try:
                        self.assertEqual(scheduler.sweep_expired(), [])
                        # A real due lease still needs the write transaction;
                        # contention must not hide expiration work as success.
                        clock.advance(61)
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            scheduler.sweep_expired()
                    finally:
                        writer.rollback()
                expired = scheduler.sweep_expired()
                self.assertEqual(len(expired), 1)
                self.assertEqual(expired[0]["next"]["attempt_number"], 2)
                self.assertEqual(scheduler.sweep_expired(), [])

    def test_sweep_rechecks_a_competing_sweep_before_expiring(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            clock = MutableClock()
            with Scheduler(path, clock=clock) as scheduler, Scheduler(path, clock=clock) as other:
                scheduler.enqueue(work_order())
                scheduler.claim("worker:test")
                clock.advance(31)
                original = scheduler._transaction

                @contextmanager
                def transaction_after_competing_sweep():
                    # Both sweepers saw a due lease; only the first may append
                    # its expiration and next attempt.
                    self.assertEqual(len(other.sweep_expired()), 1)
                    with original() as cursor:
                        yield cursor

                from unittest.mock import patch
                with patch.object(scheduler, "_transaction", transaction_after_competing_sweep):
                    self.assertEqual(scheduler.sweep_expired(), [])
                rows = scheduler.connection.execute(
                    "SELECT count(*) FROM scheduler_attempt_events WHERE state='expired'").fetchone()
                self.assertEqual(rows[0], 1)

    def test_old_delete_journal_migrates_without_changing_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            with Scheduler(path) as scheduler:
                scheduler.enqueue(work_order())
                before = tuple(scheduler.connection.execute(
                    "SELECT work_order_json,work_order_hash FROM scheduler_work_orders"
                ).fetchone())
            with closing(sqlite3.connect(path)) as legacy:
                self.assertEqual(legacy.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
            with Scheduler(path) as scheduler:
                self.assertEqual(scheduler.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(tuple(scheduler.connection.execute(
                    "SELECT work_order_json,work_order_hash FROM scheduler_work_orders"
                ).fetchone()), before)
                self.assertEqual(scheduler.enqueue(work_order())["status"], "duplicate")

    def test_writer_commits_while_an_independent_reader_holds_a_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            with Scheduler(path) as scheduler:
                scheduler.enqueue(work_order("before"))
            reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT count(*) FROM scheduler_work_orders").fetchone()[0], 1)

                def write():
                    with Scheduler(path) as other:
                        return other.enqueue(work_order("after"))

                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(write)
                    try:
                        self.assertEqual(future.result(timeout=3)["status"], "fresh")
                        # The reader still sees its coherent old snapshot.
                        self.assertEqual(reader.execute("SELECT count(*) FROM scheduler_work_orders").fetchone()[0], 1)
                    finally:
                        reader.rollback()
                self.assertEqual(reader.execute("SELECT count(*) FROM scheduler_work_orders").fetchone()[0], 2)
            finally:
                reader.close()

    def test_supplied_connection_keeps_its_parent_journal_policy(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        with Scheduler(connection=connection) as scheduler:
            self.assertEqual(scheduler.connection.execute("PRAGMA journal_mode").fetchone()[0], "memory")
