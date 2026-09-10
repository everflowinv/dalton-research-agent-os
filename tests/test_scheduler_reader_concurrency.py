"""A stable read snapshot must not stop another scheduler from committing."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dalton_core.scheduler import Scheduler
from tests.test_scheduler import work_order


class SchedulerReaderConcurrencyTests(unittest.TestCase):
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
