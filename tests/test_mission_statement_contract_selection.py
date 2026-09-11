from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core.mission_statement_lane import argv_fragment
from dalton_core.sec_financials_core import build_sec_financials_governance_record


class StatementContractSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.governance = self.state / "connector-governance"
        self.governance.mkdir()
        self.context = SimpleNamespace(state=self.state)

    def write(self, version: int, status: str) -> Path:
        path = self.governance / f"sec-financial-statements-v{version}.json"
        path.write_text(json.dumps(build_sec_financials_governance_record(
            approved_by="human:test", status=status, version=version)), encoding="utf-8")
        return path

    def selected(self) -> str | None:
        argv = argv_fragment(self.context)
        return None if not argv else argv[1]

    def test_existing_approved_v2_survives_a_proposed_v3_install(self):
        v2 = self.write(2, "approved")
        self.write(3, "proposed")
        self.assertEqual(self.selected(), str(v2))

    def test_approved_v3_is_an_explicit_upgrade(self):
        self.write(2, "approved")
        v3 = self.write(3, "approved")
        self.assertEqual(self.selected(), str(v3))

    def test_installed_lane_requests_annual_and_quarterly_coverage(self):
        self.write(2, "approved")
        argv = argv_fragment(self.context)
        self.assertEqual(
            [argv[index + 1] for index, item in enumerate(argv)
             if item == "--statement-lane-form"],
            ["10-K", "10-Q"],
        )
        limit = argv.index("--statement-lane-filing-limit")
        self.assertEqual(argv[limit + 1], "10-K=1")

    def test_fresh_proposed_records_do_not_advertise_a_connected_lane(self):
        self.write(2, "proposed")
        self.write(3, "proposed")
        self.assertIsNone(self.selected())


if __name__ == "__main__":
    unittest.main()
