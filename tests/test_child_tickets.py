"""P9d-14: a finished child's own summary settles its ticket; nothing else does."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.child_tickets import adopt_finished_child


class AdoptFinishedChildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.summary = Path(self.temp.name) / "summary.json"

    def record(self) -> dict:
        return {"id": "public-web-fetch:" + "a" * 24, "status": "running", "pid": 2**22 - 1,
                "exit_code": None, "completed_at": None}

    def test_terminal_summary_is_adopted_and_says_so(self) -> None:
        for status, code in (("succeeded", 0), ("failed", 1)):
            self.summary.write_text(json.dumps({"status": status, "failure_reason": None}), encoding="utf-8")
            record = self.record()
            self.assertTrue(adopt_finished_child(record, self.summary, now="T"))
            self.assertEqual((record["status"], record["exit_code"], record["completed_at"], record["adopted_from_summary"]),
                             (status, code, "T", True))

    def test_missing_unreadable_or_non_terminal_summary_is_not_adopted(self) -> None:
        cases = [None, "{not json", json.dumps([1, 2]), json.dumps({"status": "running"}),
                 json.dumps({"status": "orphaned"}), json.dumps({"failure_reason": "x"})]
        for case in cases:
            if case is None:
                if self.summary.exists():
                    self.summary.unlink()
            else:
                self.summary.write_text(case, encoding="utf-8")
            record = self.record()
            self.assertFalse(adopt_finished_child(record, self.summary, now="T"), case)
            self.assertEqual(record, self.record())


if __name__ == "__main__":
    unittest.main()
