"""C1: the calendar child -- approval first, artifact always, contract last.

Every run here replays a captured call. The suite is offline and this file is
where that has to stay true: ``--allow-network`` is never passed and ``run``
refuses a Namespace that names neither mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.catalyst_calendar import CatalystCalendarAuthority
from dalton_core.catalyst_calendar_cli import build_parser, run
from dalton_core.connector_governance import build_governance_record
from dalton_core.store import DaltonStore, canonical_json

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "market" / "acn-calendar.json"
COMPANY = "company:sec-cik:0001467373"
OWNER = "human:coverage-owner"
ROWS = (
    ("0001467373-26-000031", "8-K", "2026-06-18", "2026-06-18", "2.02,9.01"),
    ("0001467373-26-000035", "8-K", "2026-06-23", "2026-06-23", "7.01,9.01"),
)


class ChildTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        self.governance = self.root / "yfinance-calendar-v1.json"
        self.write_governance()
        self.fixture = self.root / "acn-calendar.json"
        self.fixture.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    def write_governance(self, *, status="approved", **overrides):
        record = build_governance_record(
            "yfinance-calendar", approved_by=OWNER, status=status)
        record.update(overrides)
        if overrides:
            body = {k: v for k, v in record.items() if k != "content_hash"}
            from dalton_core.store import content_hash

            record["content_hash"] = content_hash(body)
        self.governance.write_text(canonical_json(record) + "\n", encoding="utf-8")

    def args(self, **overrides):
        values = {
            "state_dir": str(self.state), "governance": str(self.governance),
            "company_ref": COMPANY, "ticker": "ACN", "issuer": None,
            "summary_dir": str(self.out), "actor_ref": "core:test",
            "as_of": "2026-09-09", "allow_network": False,
            "fixture_file": str(self.fixture), "no_publish": False, "quiet": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def summary_on_disk(self):
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))

    def seed_filings(self, *, issuer="0001467373", rows=ROWS):
        body = json.dumps({"cik": issuer, "filings": {"recent": {
            "accessionNumber": [row[0] for row in rows],
            "form": [row[1] for row in rows],
            "filingDate": [row[2] for row in rows],
            "reportDate": [row[3] for row in rows],
            "items": [row[4] for row in rows],
            "acceptanceDateTime": [f"{row[2]}T10:00:00.000Z" for row in rows],
        }}})
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        objects = self.state / "connector-spool" / "connector-spool" / "objects"
        (objects / digest[:2]).mkdir(parents=True, exist_ok=True)
        (objects / digest[:2] / digest).write_text(body, encoding="utf-8")
        store = DaltonStore(str(self.state / "core.sqlite"))
        try:
            store.connection.executescript(
                "CREATE TABLE IF NOT EXISTS connector_call_specs("
                "call_spec_id TEXT PRIMARY KEY, operation TEXT, record_json TEXT);"
                "CREATE TABLE IF NOT EXISTS connector_invocations("
                "connector_invocation_id TEXT PRIMARY KEY, call_spec_ref TEXT);"
                "CREATE TABLE IF NOT EXISTS connector_source_envelopes("
                "source_envelope_id TEXT PRIMARY KEY, connector_invocation_ref TEXT,"
                "raw_response_hash TEXT, status TEXT);"
            )
            store.connection.execute(
                "INSERT INTO connector_call_specs VALUES(?,?,?)",
                ("spec:1", "list_filings",
                 json.dumps({"parameters": {"issuer": issuer, "form": "10-K"}})))
            store.connection.execute(
                "INSERT INTO connector_invocations VALUES(?,?)", ("inv:1", "spec:1"))
            store.connection.execute(
                "INSERT INTO connector_source_envelopes VALUES(?,?,?,?)",
                ("envelope:1", "inv:1", digest, "complete"))
        finally:
            store.close()


class ApprovalTests(ChildTestCase):
    def test_an_unapproved_record_stops_the_run_before_anything_is_read(self):
        self.write_governance(status="proposed")
        summary = run(self.args())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_record_whose_contract_moved_does_not_cover_this_output(self):
        self.write_governance(expected_schema_hash="0" * 64)
        summary = run(self.args())
        self.assertEqual(summary["status"], "failed")
        self.assertIn("does not cover this output contract",
                      summary["failure_reason"])

    def test_a_run_that_names_neither_mode_refuses_rather_than_reaching_yahoo(self):
        summary = run(self.args(fixture_file=None, allow_network=False))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("exactly one of", summary["failure_reason"])

    def test_a_run_that_names_both_modes_refuses_too(self):
        summary = run(self.args(allow_network=True))
        self.assertEqual(summary["status"], "failed")


class ArtifactTests(ChildTestCase):
    def test_the_whole_call_is_spooled_before_a_date_is_read_out_of_it(self):
        summary = run(self.args())
        self.assertEqual(summary["status"], "succeeded")
        digest = summary["artifact"]["content_hash"]
        stored = (self.state / "connector-spool" / "connector-spool" / "objects"
                  / digest[:2] / digest)
        self.assertTrue(stored.is_file())
        # The artifact is the call: what the normaliser dropped -- Yahoo's
        # consensus figures -- is still recoverable from it.
        self.assertIn("Earnings Average", stored.read_text(encoding="utf-8"))

    def test_the_invocation_names_the_approval_and_the_bytes(self):
        summary = run(self.args())
        self.assertTrue(
            summary["invocation_ref"].startswith("connector-invocation:yfinance:"))
        again = run(self.args())
        self.assertEqual(summary["invocation_ref"], again["invocation_ref"])

    def test_a_summary_is_always_written(self):
        self.write_governance(status="proposed")
        run(self.args())
        self.assertEqual(self.summary_on_disk()["status"], "failed")


class PublishingTests(ChildTestCase):
    def test_the_vendor_half_alone_publishes_an_estimated_calendar(self):
        summary = run(self.args())
        self.assertEqual(summary["calendar_status"], "fresh")
        self.assertEqual(summary["earnings_dates"], ["2026-10-01"])
        self.assertEqual(summary["next_catalyst_date"], "2026-10-01")
        self.assertEqual(summary["sec_status"], "not_requested")
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        entry = CatalystCalendarAuthority(store).next_catalyst(COMPANY, "2026-09-09")
        self.assertEqual(entry["confidence"], "estimated")

    def test_the_company_s_own_filing_confirms_the_quarter_it_reported(self):
        self.seed_filings()
        summary = run(self.args(issuer="0001467373"))
        self.assertEqual(summary["sec_status"], "read")
        self.assertEqual(summary["sec_release_count"], 1)
        self.assertEqual(summary["sec_entry_count"], 1)
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        entries = CatalystCalendarAuthority(store).entries(COMPANY)
        confirmed = [item for item in entries if item["confidence"] == "confirmed"]
        self.assertEqual([item["expected_date"] for item in confirmed],
                         ["2026-06-18"])

    def test_a_company_whose_index_has_not_been_read_still_gets_the_vendor_half(self):
        summary = run(self.args(issuer="0000051143"))
        self.assertEqual(summary["sec_status"], "unavailable")
        self.assertEqual(summary["calendar_status"], "fresh")
        self.assertEqual(summary["earnings_dates"], ["2026-10-01"])

    def test_the_same_reading_twice_is_a_duplicate(self):
        run(self.args())
        summary = run(self.args(as_of="2026-09-10"))
        self.assertEqual(summary["calendar_status"], "duplicate")
        self.assertIsNone(summary["change_reason"])
        self.assertEqual(summary["moved_entry_refs"], [])

    def test_a_moved_date_publishes_a_driver_event_and_names_what_moved(self):
        run(self.args())
        moved = json.loads(self.fixture.read_text(encoding="utf-8"))
        moved["calendar"]["Earnings Date"] = ["2026-10-02"]
        moved["captured_at"] = "2026-09-10T15:00:00+00:00"
        moved["observed_on"] = "2026-09-10"
        path = self.root / "moved.json"
        path.write_text(json.dumps(moved), encoding="utf-8")
        summary = run(self.args(fixture_file=str(path), as_of="2026-09-10"))
        self.assertEqual(summary["calendar_status"], "fresh")
        self.assertEqual(summary["change_reason"], "driver_event")
        self.assertEqual(len(summary["moved_entry_refs"]), 1)
        self.assertEqual(summary["next_catalyst_date"], "2026-10-02")

    def test_no_publish_validates_without_writing(self):
        summary = run(self.args(no_publish=True))
        self.assertEqual(summary["calendar_status"], "not_published")
        store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(store.close)
        self.assertEqual(CatalystCalendarAuthority(store).entries(COMPANY), [])


class ParserTests(unittest.TestCase):
    def test_a_mode_has_to_be_chosen(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_the_issuer_is_optional_because_not_every_company_files(self):
        args = build_parser().parse_args([
            "--state-dir", "/tmp/s", "--governance", "/tmp/g",
            "--company-ref", COMPANY, "--ticker", "ACN", "--fixture-file", "/tmp/f",
        ])
        self.assertIsNone(args.issuer)
        self.assertFalse(args.allow_network)


if __name__ == "__main__":
    unittest.main()
