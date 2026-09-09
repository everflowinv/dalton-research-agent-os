"""P12a: Core is WAL, so a reader cannot block the writer."""
import sqlite3, tempfile, unittest
from pathlib import Path
from dalton_core.store import DaltonStore


class WalTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.path = Path(self.d.name) / "core.sqlite"

    def test_a_fresh_core_is_wal(self):
        s = DaltonStore(str(self.path)); self.addCleanup(s.close)
        self.assertEqual(s.connection.execute("pragma journal_mode").fetchone()[0], "wal")

    def test_an_existing_rollback_journal_core_is_converted_on_open(self):
        raw = sqlite3.connect(self.path)
        raw.execute("pragma journal_mode=delete")
        self.assertEqual(raw.execute("pragma journal_mode").fetchone()[0], "delete")
        raw.close()
        s = DaltonStore(str(self.path)); self.addCleanup(s.close)
        self.assertEqual(s.connection.execute("pragma journal_mode").fetchone()[0], "wal")

    def test_an_open_reader_does_not_block_a_write(self):
        # This is the failure that took two lanes dark: a child holding a read
        # transaction while the writer tried to record anything.
        s = DaltonStore(str(self.path)); self.addCleanup(s.close)
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        reader.execute("begin")
        reader.execute("select count(*) from claim_versions").fetchone()
        s.connection.execute("create table if not exists probe(x)")
        s.connection.execute("insert into probe values (1)")
        self.assertEqual(s.connection.execute("select count(*) from probe").fetchone()[0], 1)

    def test_the_busy_timeout_stays_inside_the_writer_request_timeout(self):
        from dalton_core.writer_server import STORE_REQUEST_TIMEOUT
        s = DaltonStore(str(self.path)); self.addCleanup(s.close)
        busy = s.connection.execute("pragma busy_timeout").fetchone()[0]
        self.assertGreater(busy, 5000)
        self.assertLess(busy / 1000.0, STORE_REQUEST_TIMEOUT)

    def test_an_in_memory_core_still_opens(self):
        s = DaltonStore(":memory:"); self.addCleanup(s.close)
        self.assertIsNotNone(s.connection.execute("select 1").fetchone())

    def test_opening_while_another_connection_writes_does_not_fail(self):
        """P13ab: busy_timeout does not cover PRAGMA journal_mode.

        SQLite refuses the mode change immediately when another connection
        holds a lock rather than calling the busy handler, so the timeout this
        used to rely on never applied. Opening then failed outright with
        "database is locked" -- before any of the work it was opened for. The
        writer holds core.sqlite continuously and every lane subprocess opens
        it, so losing that race is ordinary, not exceptional.
        """

        first = DaltonStore(str(self.path)); self.addCleanup(first.close)
        first.connection.execute("create table if not exists probe(x)")
        # A held write transaction is the lock a second opener runs into.
        first.connection.execute("begin immediate")
        first.connection.execute("insert into probe values (1)")
        try:
            second = DaltonStore(str(self.path))
        finally:
            first.connection.execute("commit")
        self.addCleanup(second.close)
        self.assertEqual(
            second.connection.execute("pragma journal_mode").fetchone()[0], "wal")

    def test_a_database_that_cannot_convert_still_reports_it(self):
        # Tolerating the race must not become tolerating anything: a database
        # that never reaches WAL is worth failing on rather than running
        # against a rollback journal in silence.
        import dalton_core.store as store_module

        raw = sqlite3.connect(self.path)
        raw.execute("pragma journal_mode=delete")
        raw.close()
        original = store_module._WAL_CONVERSION_SECONDS
        store_module._WAL_CONVERSION_SECONDS = 0.05
        self.addCleanup(setattr, store_module, "_WAL_CONVERSION_SECONDS", original)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("begin immediate")
        with self.assertRaises(sqlite3.OperationalError):
            DaltonStore(str(self.path))
        blocker.execute("rollback")


if __name__ == "__main__":
    unittest.main()
