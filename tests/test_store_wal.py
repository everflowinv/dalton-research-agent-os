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


if __name__ == "__main__":
    unittest.main()
