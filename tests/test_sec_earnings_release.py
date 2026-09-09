"""C1: the company's own earnings announcements, out of filings already held."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.sec_earnings_release import (
    EARNINGS_RELEASE_ITEM,
    SecEarningsReleaseError,
    announced_next_date_entry,
    calendar_entries,
    earnings_release_filings,
    releases_for_issuer,
    submissions_artifacts,
)
from dalton_core.store import DaltonStore
from dalton_core.yfinance_calendar_adapter import quarter_subject

# Accenture's own recent block, in SEC's column-oriented shape and with the two
# columns the existing normaliser drops -- ``items`` and ``reportDate`` -- which
# are the whole reason this module exists. The 8-K rows and their item strings
# are taken verbatim from the artifact the live filings-index lane already
# spooled for CIK 0001467373.
ROWS = (
    ("0001467373-26-000041", "10-K", "2026-10-15", "2026-08-31", ""),
    ("0001193125-26-300813", "8-K", "2026-07-10", "2026-07-10", "8.01,9.01"),
    ("0001467373-26-000035", "8-K", "2026-06-23", "2026-06-23", "7.01,9.01"),
    ("0001467373-26-000031", "8-K", "2026-06-18", "2026-06-18", "2.02,9.01"),
    ("0001467373-26-000019", "8-K", "2026-04-24", "2026-04-22", "1.01,1.02,2.03,9.01"),
    ("0001467373-26-000013", "8-K", "2026-03-19", "2026-03-19", "2.02,9.01"),
    ("0001467373-25-000221", "8-K", "2025-12-18", "2025-12-18", "2.02,9.01"),
)


def submissions(rows=ROWS, *, drop=()):
    recent = {
        "accessionNumber": [row[0] for row in rows],
        "form": [row[1] for row in rows],
        "filingDate": [row[2] for row in rows],
        "reportDate": [row[3] for row in rows],
        "items": [row[4] for row in rows],
        "acceptanceDateTime": [f"{row[2]}T10:41:29.000Z" for row in rows],
        "primaryDocument": ["d1.htm" for _ in rows],
    }
    for name in drop:
        recent.pop(name)
    return {"cik": "1467373", "filings": {"recent": recent}}


class DetectorTests(unittest.TestCase):
    def test_only_item_2_02_filings_are_earnings_announcements(self):
        found = earnings_release_filings(submissions())
        self.assertEqual(
            [row["accession"] for row in found],
            ["0001467373-26-000031", "0001467373-26-000013",
             "0001467373-25-000221"],
        )
        self.assertEqual(EARNINGS_RELEASE_ITEM, "2.02")

    def test_an_item_number_is_matched_whole_and_not_as_a_substring(self):
        rows = ROWS + (("0001467373-26-000099", "8-K", "2026-08-01",
                        "2026-08-01", "12.02,9.01"),)
        found = earnings_release_filings(submissions(rows))
        self.assertNotIn("0001467373-26-000099",
                         [row["accession"] for row in found])

    def test_a_10_k_is_never_an_earnings_announcement(self):
        found = earnings_release_filings(submissions())
        self.assertEqual({row["form"] for row in found}, {"8-K"})

    def test_the_date_is_the_day_it_was_reported(self):
        rows = (("0001467373-26-000031", "8-K", "2026-06-20", "2026-06-18",
                 "2.02,9.01"),)
        found = earnings_release_filings(submissions(rows))
        self.assertEqual(found[0]["report_date"], "2026-06-18")
        self.assertEqual(found[0]["filing_date"], "2026-06-20")

    def test_a_missing_report_date_falls_back_to_the_filing_date(self):
        found = earnings_release_filings(submissions(drop=("reportDate",)))
        self.assertEqual(found[0]["report_date"], "2026-06-18")

    def test_an_artifact_without_the_items_column_yields_nothing_rather_than_failing(self):
        # The frozen normaliser never read this column, so an older artifact
        # may not carry it. That is a thinner answer, not a broken lane.
        self.assertEqual(earnings_release_filings(submissions(drop=("items",))), [])

    def test_the_lookback_bounds_what_is_proposed(self):
        found = earnings_release_filings(submissions(), since="2026-04-01")
        self.assertEqual([row["accession"] for row in found],
                         ["0001467373-26-000031"])

    def test_something_that_is_not_a_submissions_index_is_refused(self):
        with self.assertRaises(SecEarningsReleaseError):
            earnings_release_filings({"facts": {}})
        with self.assertRaises(SecEarningsReleaseError):
            earnings_release_filings({"filings": {"recent": {"form": []}}})


class EntryTests(unittest.TestCase):
    def test_a_filed_announcement_is_a_confirmed_earnings_entry(self):
        releases = earnings_release_filings(submissions(), since="2026-04-01")
        entries = calendar_entries(
            releases, invocation_ref="connector-invocation:sec-filings-index:x",
            artifact_hash="a" * 64, subject_for=quarter_subject,
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["event_kind"], "earnings")
        self.assertEqual(entry["subject"], "2026q2")
        source = entry["sources"][0]
        self.assertEqual(source["kind"], "filing")
        self.assertEqual(source["confidence"], "confirmed")
        self.assertEqual(source["ref"], "sec:filing:0001467373-26-000031")
        self.assertEqual(source["source_ref"], "source:sec-edgar")
        self.assertEqual(source["observed_date"], "2026-06-18")
        self.assertIn("2.02", source["note"])

    def test_the_announced_future_date_seam_takes_the_date_from_its_caller(self):
        # Nothing in this module reads a filing's text, so nothing in it can
        # produce a future date on its own. This is where a reader that can
        # would attach one, and it is confirmed because a company said it.
        entry = announced_next_date_entry(
            accession="0001467373-26-000035", announced_date="2026-10-01",
            subject="2026q4", filing_date="2026-06-23",
            note="press release: will report Q4 on October 1",
        )
        self.assertEqual(entry["event_kind"], "earnings")
        self.assertEqual(entry["sources"][0]["observed_date"], "2026-10-01")
        self.assertEqual(entry["sources"][0]["confidence"], "confirmed")

    def test_a_date_that_is_not_a_date_is_refused_at_the_seam(self):
        with self.assertRaises(ValueError):
            announced_next_date_entry(
                accession="x", announced_date="early October", subject="2026q4",
                filing_date="2026-06-23")


class SpoolTests(unittest.TestCase):
    """Reading a governed call that already happened, with no call of its own."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        # The three columns of the connector tables this reader joins over.
        # The real schema carries a dozen more and a chain of foreign keys;
        # what is being tested is the join and the spool read, and building the
        # whole connector plane here would test that instead.
        self.store.connection.executescript(
            "CREATE TABLE connector_call_specs("
            "call_spec_id TEXT PRIMARY KEY, operation TEXT, record_json TEXT);"
            "CREATE TABLE connector_invocations("
            "connector_invocation_id TEXT PRIMARY KEY, call_spec_ref TEXT);"
            "CREATE TABLE connector_source_envelopes("
            "source_envelope_id TEXT PRIMARY KEY, connector_invocation_ref TEXT,"
            "raw_response_hash TEXT, status TEXT);"
        )

    def seed(self, *, issuer="0001467373", payload=None):
        body = json.dumps(payload if payload is not None else submissions())
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        objects = self.state / "connector-spool" / "connector-spool" / "objects"
        (objects / digest[:2]).mkdir(parents=True, exist_ok=True)
        (objects / digest[:2] / digest).write_text(body, encoding="utf-8")
        connection = self.store.connection
        connection.execute(
            "INSERT INTO connector_call_specs VALUES(?,?,?)",
            ("spec:1", "list_filings",
             json.dumps({"parameters": {"issuer": issuer, "form": "10-K"}})),
        )
        connection.execute(
            "INSERT INTO connector_invocations VALUES(?,?)", ("inv:1", "spec:1"))
        connection.execute(
            "INSERT INTO connector_source_envelopes VALUES(?,?,?,?)",
            ("envelope:1", "inv:1", digest, "complete"))
        return digest

    def test_the_governed_call_is_found_by_its_issuer(self):
        self.seed()
        found = submissions_artifacts(self.store.connection, issuer="1467373")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["invocation_ref"], "inv:1")
        # Leading zeros are a formatting choice SEC and this codebase disagree
        # about; DXC's own company ref is nine digits.
        self.assertEqual(
            len(submissions_artifacts(self.store.connection, issuer="0001467373")), 1)
        self.assertEqual(
            submissions_artifacts(self.store.connection, issuer="51143"), [])

    def test_the_spooled_bytes_are_read_and_nothing_is_fetched(self):
        digest = self.seed()
        found = releases_for_issuer(
            self.store.connection, self.state, issuer="0001467373",
            today="2026-09-09", lookback_days=400,
        )
        self.assertEqual(found["status"], "read")
        self.assertEqual(len(found["releases"]), 3)
        self.assertEqual(found["artifact_hash"], digest)

    def test_a_company_whose_index_has_not_been_read_says_so(self):
        found = releases_for_issuer(
            self.store.connection, self.state, issuer="0000051143",
            today="2026-09-09",
        )
        self.assertEqual(found["status"], "unavailable")
        self.assertEqual(found["releases"], [])
        self.assertIn("discovery lane has not run", found["reason"])


if __name__ == "__main__":
    unittest.main()
