from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.raw_spool import (
    RawSpool,
    RawSpoolCapacityError,
    RawSpoolLimitExceeded,
)


class RawSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    @staticmethod
    def sink_ref(char: str) -> str:
        return "raw-sink:" + char * 64

    def test_finalize_is_content_addressed_and_deduplicated(self) -> None:
        spool = RawSpool(self.temp.name, max_total_bytes=64)
        first = spool.open_sink(self.sink_ref("1"), max_response_bytes=16)
        self.assertFalse(hasattr(first, "path"))
        first.write(b"same")
        obj1 = first.finalize()
        self.assertEqual(obj1.content_hash, hashlib.sha256(b"same").hexdigest())
        self.assertEqual(spool.read_object(obj1.content_hash), b"same")
        second = spool.open_sink(self.sink_ref("2"), max_response_bytes=16)
        second.write(b"same")
        obj2 = second.finalize()
        self.assertEqual(obj1, obj2)
        self.assertEqual(spool.total_bytes(), 4)

    def test_stream_limit_aborts_without_partial_success(self) -> None:
        spool = RawSpool(self.temp.name, max_total_bytes=64)
        sink = spool.open_sink(self.sink_ref("3"), max_response_bytes=3)
        with self.assertRaises(RawSpoolLimitExceeded):
            sink.write(b"four")
        self.assertEqual(list((Path(self.temp.name) / "connector-spool" / "tmp").iterdir()), [])
        self.assertEqual(spool.total_bytes(), 0)

    def test_high_water_and_orphan_gc_fail_closed(self) -> None:
        spool = RawSpool(self.temp.name, max_total_bytes=8)
        sink = spool.open_sink(self.sink_ref("4"), max_response_bytes=4)
        sink.write(b"1234")
        sink.finalize()
        with self.assertRaises(RawSpoolCapacityError):
            spool.open_sink(self.sink_ref("5"), max_response_bytes=5)
        orphan = spool.open_sink(self.sink_ref("6"), max_response_bytes=4)
        orphan.write(b"x")
        del orphan
        self.assertEqual(spool.gc_orphans(), 1)
        self.assertEqual(spool.gc_orphans(), 0)


if __name__ == "__main__":
    unittest.main()
