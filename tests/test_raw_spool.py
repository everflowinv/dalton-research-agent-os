from __future__ import annotations

import hashlib
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path

from dalton_core.raw_spool import (
    RawSpool,
    RawSpoolCapacityError,
    RawSpoolReader,
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

    def test_capacity_pressure_losslessly_archives_old_objects(self) -> None:
        payload = b'{"repeated":"' + b"value," * 100 + b'"}'
        spool = RawSpool(
            self.temp.name, max_total_bytes=len(payload) + 30,
            archive_after_seconds=0,
        )
        sink = spool.open_sink(self.sink_ref("a"), max_response_bytes=len(payload))
        sink.write(payload)
        original = sink.finalize()
        second = spool.open_sink(self.sink_ref("b"), max_response_bytes=64)
        archived = list((Path(self.temp.name) / "connector-spool" / "objects").glob("*/*.gz"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(spool.read_object(original.content_hash), payload)
        self.assertEqual(
            RawSpoolReader(self.temp.name).read_object(original.content_hash), payload
        )
        second.abort()
        restored = spool.restore_archived_objects()
        self.assertEqual(restored["restored"], 1)
        self.assertFalse(archived[0].exists())
        self.assertEqual(spool.read_object(original.content_hash), payload)

    def test_capacity_error_reports_actionable_numbers(self) -> None:
        spool = RawSpool(self.temp.name, max_total_bytes=8, archive_after_seconds=0)
        sink = spool.open_sink(self.sink_ref("c"), max_response_bytes=4)
        sink.write(b"1234")
        sink.finalize()
        with self.assertRaisesRegex(
            RawSpoolCapacityError,
            r"used_bytes=4 .*requested_bytes=5 .*max_total_bytes=8",
        ):
            spool.open_sink(self.sink_ref("d"), max_response_bytes=5)

    def test_named_connector_spool_is_not_doubled_for_new_install(self) -> None:
        named = Path(self.temp.name) / "connector-spool"
        named.mkdir()
        RawSpool(named, max_total_bytes=64)
        self.assertTrue((named / "objects").is_dir())
        self.assertFalse((named / "connector-spool").exists())

    def test_existing_historical_nested_spool_remains_readable(self) -> None:
        named = Path(self.temp.name) / "connector-spool"
        historical = named / "connector-spool"
        (historical / "objects").mkdir(parents=True)
        (historical / "tmp").mkdir()
        spool = RawSpool(named, max_total_bytes=64)
        sink = spool.open_sink(self.sink_ref("e"), max_response_bytes=4)
        sink.write(b"live")
        obj = sink.finalize()
        self.assertTrue((historical / "objects" / obj.content_hash[:2] / obj.content_hash).is_file())

    def test_runtime_capacity_override_replaces_hard_coded_default(self) -> None:
        with patch.dict("os.environ", {"DALTON_RAW_SPOOL_MAX_TOTAL_BYTES": "4"}):
            spool = RawSpool(self.temp.name, max_total_bytes=1_000_000_000)
        with self.assertRaises(RawSpoolCapacityError):
            spool.open_sink(self.sink_ref("f"), max_response_bytes=5)

    def test_other_instance_gc_preserves_inflight_download_and_completed_original(self):
        first = RawSpool(self.temp.name, max_total_bytes=4096)
        second = RawSpool(self.temp.name, max_total_bytes=4096)
        sink = first.open_sink(self.sink_ref("7"), max_response_bytes=1024)
        sink.write(b"original paragraph not represented by any Claim")
        self.assertEqual(second.gc_orphans(), 0)
        original = sink.finalize()
        self.assertEqual(second.gc_orphans(), 0)
        self.assertEqual(second.read_object(original.content_hash), b"original paragraph not represented by any Claim")

    def test_activity_lock_survives_flush_until_object_publication(self):
        writer = RawSpool(self.temp.name, max_total_bytes=4096)
        collector = RawSpool(self.temp.name, max_total_bytes=4096)
        sink = writer.open_sink(self.sink_ref("9"), max_response_bytes=1024)
        sink.write(b"publish without an unlocked gap")
        publish = writer._finalize

        def interleaved(*args):
            self.assertEqual(collector.gc_orphans(), 0)
            return publish(*args)

        with patch.object(writer, "_finalize", side_effect=interleaved):
            original = sink.finalize()
        self.assertEqual(collector.read_object(original.content_hash), b"publish without an unlocked gap")

    def test_another_process_download_is_protected_until_that_process_exits(self):
        script = (
            "import os,sys; sys.path.insert(0,sys.argv[2]); "
            "from dalton_core.raw_spool import RawSpool; "
            "s=RawSpool(sys.argv[1],max_total_bytes=4096); "
            "sink=s.open_sink('raw-sink:'+'8'*64,max_response_bytes=1024); "
            "sink.write(b'active original'); print('ready',flush=True); "
            "sys.stdin.readline(); os._exit(0)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, self.temp.name, str(Path(__file__).resolve().parents[1] / "src")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            collector = RawSpool(self.temp.name, max_total_bytes=4096)
            self.assertEqual(collector.gc_orphans(), 0)
            process.communicate("exit\n", timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(collector.gc_orphans(), 1)
            self.assertEqual(collector.gc_orphans(), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
