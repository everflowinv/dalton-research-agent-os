"""P11b: the consensus child, offline, against the recorded ACN call.

Approval first, artifact always, contract last -- and the fiscal calendar
before any of it, because a run that cannot name its periods must not write.
The fixture is the one P11a recorded from the live library; nothing here
reaches the network.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.consensus_estimate import ConsensusEstimateAuthority
from dalton_core.consensus_estimate_cli import build_parser, run
from dalton_core.store import DaltonStore, canonical_json
from dalton_core.yfinance_core import build_yfinance_governance_record

ACN = "company:sec-cik:0001467373"
FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "market" / "acn-analyst-estimates.json"
)


def approved_record(path: Path) -> Path:
    """The packaged governance record, approved, so the child may run."""

    wire = build_yfinance_governance_record(
        operation="analyst_estimates", approved_by="human:lumos", status="approved"
    )
    path.write_text(canonical_json(wire), encoding="utf-8")
    return path


class ChildTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.governance = approved_record(self.state / "governance.json")

    def args(self, **overrides):
        value = {
            "state_dir": str(self.state),
            "governance": str(self.governance),
            "company_ref": ACN,
            "ticker": "ACN",
            "fiscal_year_end": "08-31",
            "last_reported_period_end": "2026-05-31",
            "summary_dir": str(self.state),
            "actor_ref": "core:consensus-estimate-worker",
            "allow_network": False,
            "fixture_file": str(FIXTURE),
            "no_publish": False,
            "quiet": True,
        }
        value.update(overrides)
        return argparse.Namespace(**value)

    def store(self):
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        return store


class RunTests(ChildTestCase):
    def test_the_recorded_call_publishes_a_first_version(self):
        summary = run(self.args())
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["consensus_status"], "fresh")
        self.assertEqual(summary["as_of"], "2026-09-09")
        self.assertEqual(summary["target_price_mean"], "184.1884")
        self.assertEqual(
            summary["mapped_periods"], ["FY2026", "FY2026Q4", "FY2027", "FY2027Q1"]
        )

    def test_the_raw_call_is_hashed_and_spooled_before_a_number_is_read(self):
        summary = run(self.args())
        self.assertEqual(len(summary["artifact"]["content_hash"]), 64)
        self.assertTrue(summary["invocation_ref"].startswith("connector-invocation:"))
        held = ConsensusEstimateAuthority(self.store()).latest_consensus(ACN)
        self.assertEqual(held["artifact_hash"], summary["artifact"]["content_hash"])
        self.assertEqual(held["invocation_ref"], summary["invocation_ref"])

    def test_the_same_call_again_is_a_duplicate(self):
        run(self.args())
        again = run(self.args())
        self.assertEqual(again["consensus_status"], "duplicate")
        self.assertEqual(again["changed_fields"], [])
        self.assertEqual(len(ConsensusEstimateAuthority(self.store()).versions(ACN)), 1)

    def test_a_summary_is_always_written(self):
        run(self.args())
        summary = json.loads((self.state / "summary.json").read_text())
        self.assertEqual(summary["operation"], "analyst_estimates")
        self.assertEqual(summary["transport"], "fixture")

    def test_no_publish_validates_without_writing(self):
        summary = run(self.args(no_publish=True))
        self.assertEqual(summary["consensus_status"], "not_published")
        self.assertIsNone(
            ConsensusEstimateAuthority(self.store()).latest_version(ACN)
        )


class RefusalTests(ChildTestCase):
    def test_an_unapproved_record_is_refused_before_anything_is_read(self):
        wire = build_yfinance_governance_record(
            operation="analyst_estimates", approved_by="human:lumos"
        )
        self.assertEqual(wire["status"], "proposed")
        self.governance.write_text(canonical_json(wire), encoding="utf-8")
        summary = run(self.args())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_record_for_the_other_operation_does_not_cover_this_one(self):
        approved = build_yfinance_governance_record(
            operation="daily_prices", approved_by="human:lumos", status="approved"
        )
        self.governance.write_text(canonical_json(approved), encoding="utf-8")
        summary = run(self.args())
        self.assertIn("different capability", summary["failure_reason"])

    def test_an_unknown_fiscal_calendar_writes_nothing(self):
        summary = run(self.args(fiscal_year_end="unknown"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("fiscal calendar", summary["failure_reason"])
        # The artifact is still kept: the call happened and its bytes are the
        # evidence of what the vendor said, whatever we could do with it.
        self.assertIsNotNone(summary["artifact"])

    def test_both_modes_at_once_is_refused_rather_than_resolved(self):
        summary = run(self.args(allow_network=True))
        self.assertIn("exactly one of", summary["failure_reason"])

    def test_neither_mode_is_refused_rather_than_reaching_the_network(self):
        summary = run(self.args(fixture_file=None))
        self.assertIn("exactly one of", summary["failure_reason"])

    def test_the_parser_refuses_a_run_with_no_mode(self):
        parser = build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args([])


if __name__ == "__main__":
    unittest.main()
