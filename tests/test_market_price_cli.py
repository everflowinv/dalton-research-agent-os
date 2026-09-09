"""P11a: the price child -- approval first, artifact always, contract last.

The child does four things in an order that matters. It refuses unless the
governance record is approved *and* still describes the packaged contract. It
keeps the whole library call, hashed, before reading anything out of it. It
validates the wire against the frozen contract. Only then does it publish.

These run against the captured Accenture call. Reaching Yahoo is a separate
mode and not what is under test here.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_governance import build_governance_record
from dalton_core.market_price import MarketPriceSeriesAuthority
from dalton_core.market_price_cli import build_parser, run
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.yfinance_core import DAILY_PRICES_KIND

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "market"
OWNER = "human:lumos"
ACN = "company:sec-cik:0001467373"


class ChildTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()

    def governance(self, *, status="approved", **overrides):
        record = build_governance_record(
            DAILY_PRICES_KIND, approved_by=OWNER, status=status,
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        record.update(overrides)
        if overrides:
            record.pop("content_hash", None)
            record["content_hash"] = content_hash(record)
        path = self.root / f"governance-{status}-{len(overrides)}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def fixture(self, value=None):
        raw = value if value is not None else json.loads(
            (FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        path = self.root / "call.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def run_child(self, *, governance=None, fixture=None, extra=()):
        args = build_parser().parse_args([
            "--state-dir", str(self.state),
            "--governance", str(governance or self.governance()),
            "--company-ref", ACN, "--ticker", "ACN",
            "--start", "2026-08-25", "--end", "2026-09-09",
            "--fixture-file", str(fixture or self.fixture()),
            "--summary-dir", str(self.out), "--quiet",
            *extra,
        ])
        return run(args)

    # -- approval first ----------------------------------------------------

    def test_a_proposed_record_is_not_authority(self):
        summary = self.run_child(governance=self.governance(status="proposed"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_record_whose_contract_moved_is_refused_before_anything_runs(self):
        summary = self.run_child(
            governance=self.governance(expected_schema_hash="0" * 64))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("does not cover this output contract", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_record_for_another_capability_is_refused(self):
        from dalton_core.connector_governance import SEC_FINANCIALS_KIND

        record = build_governance_record(
            SEC_FINANCIALS_KIND, approved_by=OWNER, status="approved",
            effective_from="2026-09-09T00:00:00+00:00", version=1)
        path = self.root / "other.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        summary = self.run_child(governance=path)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])

    # -- artifact always ---------------------------------------------------

    def test_the_whole_call_is_hashed_into_the_spool_before_it_is_read(self):
        summary = self.run_child()
        self.assertEqual(summary["status"], "succeeded")
        raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        expected = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        self.assertEqual(summary["artifact"]["content_hash"], expected)
        self.assertEqual(summary["artifact"]["size_bytes"],
                         len(canonical_json(raw).encode("utf-8")))

    def test_every_stored_bar_names_the_invocation_and_the_artifact(self):
        summary = self.run_child()
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        version = MarketPriceSeriesAuthority(store).version(
            summary["series_version_ref"])
        self.assertTrue(version["bars"])
        for bar in version["bars"]:
            self.assertEqual(bar["invocation_ref"], summary["invocation_ref"])
            self.assertEqual(bar["artifact_hash"],
                             summary["artifact"]["content_hash"])

    def test_the_invocation_moves_when_the_bytes_move(self):
        first = self.run_child()
        raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        raw["rows"] = raw["rows"][:5]
        second = self.run_child(fixture=self.fixture(raw))
        self.assertNotEqual(first["invocation_ref"], second["invocation_ref"])

    # -- contract last -----------------------------------------------------

    def test_an_adjusted_capture_is_refused_after_the_artifact_is_kept(self):
        raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        raw["auto_adjust"] = True
        summary = self.run_child(fixture=self.fixture(raw))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("adjusted", summary["failure_reason"])
        # The artifact is kept anyway: it is the evidence of what came back.
        self.assertIsNotNone(summary["artifact"])
        self.assertIsNone(summary["series_version_ref"])

    def test_a_wire_the_contract_cannot_describe_never_reaches_the_authority(self):
        raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        raw["rows"] = [{**raw["rows"][0], "date": "not-a-date"}]
        summary = self.run_child(fixture=self.fixture(raw))
        self.assertEqual(summary["status"], "failed")
        self.assertIsNone(summary["series_version_ref"])

    # -- publishing --------------------------------------------------------

    def test_a_run_publishes_a_version_and_says_what_it_added(self):
        summary = self.run_child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["series_status"], "fresh")
        self.assertEqual(summary["bar_count"], 10)
        self.assertEqual(summary["added_bar_count"], 10)
        self.assertEqual(summary["first_bar_date"], "2026-08-25")
        self.assertEqual(summary["last_bar_date"], "2026-09-08")
        self.assertEqual(summary["observation_count"], 2)

    def test_replaying_the_same_call_adds_nothing(self):
        self.run_child()
        again = self.run_child()
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(again["series_status"], "duplicate")
        self.assertEqual(again["added_bar_count"], 0)

    def test_no_publish_validates_without_writing(self):
        summary = self.run_child(extra=("--no-publish",))
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["series_status"], "not_published")
        self.assertFalse((self.state / "core.sqlite").exists())

    def test_an_empty_window_is_a_normal_answer_not_a_failure(self):
        raw = json.loads((FIXTURES / "acn-daily-prices.json").read_text(encoding="utf-8"))
        raw["rows"] = []
        summary = self.run_child(fixture=self.fixture(raw))
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["series_status"], "empty")

    def test_a_summary_is_always_written(self):
        self.run_child(governance=self.governance(status="proposed"))
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["operation"], "daily_prices")
        self.assertEqual(summary["transport"], "fixture")


class ParserTests(unittest.TestCase):
    def test_exactly_one_mode_must_be_chosen(self):
        for extra in ([], ["--allow-network", "--fixture-file", "/tmp/f.json"]):
            with self.assertRaises(SystemExit):
                from dalton_core.market_price_cli import main

                main([
                    "--state-dir", "/tmp", "--governance", "/tmp/g.json",
                    "--company-ref", ACN, "--ticker", "ACN",
                    "--start", "2026-09-01", "--end", "2026-09-05", *extra,
                ])

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        from dalton_core.market_price_cli import main

        with self.assertRaises(SystemExit):
            main([
                "--state-dir", "/tmp", "--governance", "/tmp/g.json",
                "--company-ref", ACN, "--ticker", "ACN",
                "--start", "2026-09-05", "--end", "2026-09-01",
                "--fixture-file", "/tmp/f.json",
            ])


if __name__ == "__main__":
    unittest.main()
