from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timezone

from dalton_core.host_tool_runner import HostToolRunner


class HostToolRunnerFailureTests(unittest.TestCase):
    def test_nonzero_child_failure_reason_from_stdout_is_preserved(self) -> None:
        runner = object.__new__(HostToolRunner)
        runner.timeout_seconds = 10.0
        runner.max_response_bytes = 4096
        runner.clock = lambda: datetime.now(timezone.utc)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import json,sys; print(json.dumps({'status':'failed',"
                "'failure_reason':'RawSpoolCapacityError: raw spool high-water mark reached'}));"
                "sys.exit(1)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        raw, exit_code, note = runner._drain(process)

        self.assertIn(b'"status": "failed"', raw)
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            note,
            "exit 1: RawSpoolCapacityError: raw spool high-water mark reached",
        )


if __name__ == "__main__":
    unittest.main()
