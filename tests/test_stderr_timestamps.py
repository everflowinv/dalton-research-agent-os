"""P3: every stderr line of the writer / controller / control carries its time."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import traceback
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.stderr_timestamps import TimestampedStream, iso_now, timestamped_stderr

ROOT = Path(__file__).resolve().parents[1]
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ")


class TimestampedStreamTests(unittest.TestCase):
    def test_every_line_is_stamped_once_including_multi_line_writes(self) -> None:
        target = io.StringIO()
        stream = TimestampedStream(target, clock=lambda: "T")
        stream.write("first\nsecond\n")
        stream.write("partial ")
        stream.write("end\n")
        stream.write("\n")
        self.assertEqual(target.getvalue(), "T first\nT second\nT partial end\nT \n")

    def test_a_traceback_is_stamped_line_by_line(self) -> None:
        target = io.StringIO()
        stream = TimestampedStream(target, clock=lambda: "T")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            traceback.print_exc(file=stream)
        lines = target.getvalue().splitlines()
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(line.startswith("T ") for line in lines), lines)
        self.assertEqual(lines[-1], "T RuntimeError: boom")

    def test_real_clock_is_iso_utc(self) -> None:
        self.assertRegex(iso_now() + " ", STAMP)

    def test_concurrent_writers_never_split_a_stamp_into_a_line(self) -> None:
        target = io.StringIO()
        stream = TimestampedStream(target, clock=lambda: "T")

        def worker(tag: str) -> None:
            for index in range(200):
                stream.write(f"{tag}-{index}\n")

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in "abcd"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = target.getvalue().splitlines()
        self.assertEqual(len(lines), 800)
        self.assertTrue(all(re.fullmatch(r"T [abcd]-\d+", line) for line in lines))

    def test_context_is_idempotent_and_restores_stderr(self) -> None:
        original = sys.stderr
        with timestamped_stderr() as outer:
            self.assertIsInstance(sys.stderr, TimestampedStream)
            with timestamped_stderr() as inner:
                self.assertIs(inner, outer)
            self.assertIs(sys.stderr, outer)
        self.assertIs(sys.stderr, original)


class DaemonEntryPointTests(unittest.TestCase):
    def test_each_daemon_main_runs_under_the_stamp(self) -> None:
        seen = {}
        for module, attribute in (("dalton_core.writer_server", "_main"),
                                  ("dalton_core.service", "_main"),
                                  ("dalton_core.agenda_control", "_main")):
            imported = __import__(module, fromlist=["main"])

            def fake(argv, _module=module):
                seen[_module] = isinstance(sys.stderr, TimestampedStream)
                return 0

            with mock.patch.object(imported, attribute, fake):
                self.assertEqual(imported.main([]), 0)
        self.assertEqual(seen, {"dalton_core.writer_server": True,
                                "dalton_core.service": True,
                                "dalton_core.agenda_control": True})
        self.assertNotIsInstance(sys.stderr, TimestampedStream)

    def test_launchd_style_invocation_stamps_argparse_errors(self) -> None:
        # ``python -m dalton_core.service`` is exactly what the LaunchAgent runs.
        completed = subprocess.run(
            [sys.executable, "-m", "dalton_core.service"],
            cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(completed.returncode, 0)
        lines = [line for line in completed.stderr.splitlines() if line]
        self.assertTrue(lines)
        self.assertTrue(all(STAMP.match(line) for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
