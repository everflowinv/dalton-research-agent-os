"""ADR-0015: the company's own earnings release, read the day it is furnished.

Covers the fiscal-period advance (incl. non-December fiscal years), the
deterministic Exhibit 99.1 parse, supersession by the filed 10-Q/10-K, the
code-owned SEC spec set and its parity row, the exhibit fetch's URL
authority, and the consumers (events, cockpit, needs-human, brief).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core import earnings_release_intake as intake
from dalton_core.earnings_release_intake import (
    PRELIMINARY_LABEL,
    PreliminaryActualsAuthority,
    earnings_release_event_candidates,
    exhibit_url,
    ingest_company,
    latest_preliminary,
    parse_press_release,
    preliminary_actuals,
    preliminary_claim_rows,
    press_release_exhibit_name,
    release_fiscal_quarter,
    reported_release,
)
from dalton_core.store import DaltonStore, content_hash

FIXTURES = Path(__file__).parent / "fixtures" / "sec-earnings-release"
ACN = "company:sec-cik:0001467373"
ACN_RELEASE = "0001467373-26-000045"


def acn_submissions(extra_rows=()):
    rows = (
        (ACN_RELEASE, "8-K", "2026-10-01", "2026-10-01", "2.02,9.01", "acn-20261001.htm"),
        ("0001467373-26-000044", "8-K", "2026-09-24", "2026-09-24", "8.01,9.01", "acn-20260924.htm"),
        ("0001467373-26-000032", "10-Q", "2026-06-26", "2026-05-31", "", "acn-20260531.htm"),
        ("0001467373-26-000031", "8-K", "2026-06-18", "2026-06-18", "2.02,9.01", "acn-20260618.htm"),
        *extra_rows,
    )
    recent = {
        "accessionNumber": [r[0] for r in rows], "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows], "reportDate": [r[3] for r in rows],
        "items": [r[4] for r in rows], "primaryDocument": [r[5] for r in rows],
        "acceptanceDateTime": [f"{r[2]}T10:40:00.000Z" for r in rows],
    }
    return {"cik": "1467373", "name": "Accenture plc", "fiscalYearEnd": "0831",
            "filings": {"recent": recent}}


class FiscalPeriodTests(unittest.TestCase):
    def test_accenture_august_year_end_reports_fy2026_q4_on_october_1(self):
        self.assertEqual(release_fiscal_quarter("2026-10-01", 8),
                         {"period": "FY2026-Q4", "period_end": "2026-08-31",
                          "period_start": "2026-06-01"})
        # The June release is the May quarter, FY2026-Q3.
        self.assertEqual(release_fiscal_quarter("2026-06-18", 8)["period"], "FY2026-Q3")

    def test_microsoft_june_year_end(self):
        self.assertEqual(release_fiscal_quarter("2026-07-29", 6)["period"], "FY2026-Q4")
        self.assertEqual(release_fiscal_quarter("2026-10-28", 6)["period"], "FY2027-Q1")

    def test_december_and_march_year_ends(self):
        self.assertEqual(release_fiscal_quarter("2026-07-22", 12)["period"], "FY2026-Q2")
        self.assertEqual(release_fiscal_quarter("2027-01-28", 12)["period"], "FY2026-Q4")
        # DXC (March year end): the June quarter is FY2027-Q1.
        self.assertEqual(release_fiscal_quarter("2026-07-30", 3)["period"], "FY2027-Q1")

    def test_a_release_long_after_any_quarter_end_is_not_mapped(self):
        # Never more than ~3 months back; a lag beyond the bound is not a release of it.
        self.assertIsNotNone(release_fiscal_quarter("2026-11-30", 8))
        self.assertIsNone(release_fiscal_quarter("2026-10-01", 0))


class SpoolHarness(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state = Path(self._dir.name)
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.store.close)
        intake._INDEX_CACHE.clear()
        intake._ELIGIBLE_CACHE.clear()
        self.store.connection.executescript(
            "CREATE TABLE IF NOT EXISTS connector_call_specs("
            "call_spec_id TEXT PRIMARY KEY, operation TEXT, record_json TEXT);"
            "CREATE TABLE IF NOT EXISTS connector_invocations("
            "connector_invocation_id TEXT PRIMARY KEY, call_spec_ref TEXT);"
            "CREATE TABLE IF NOT EXISTS connector_source_envelopes("
            "source_envelope_id TEXT PRIMARY KEY, connector_invocation_ref TEXT,"
            "raw_response_hash TEXT, status TEXT, record_json TEXT);"
            "CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings("
            "ingest_id TEXT PRIMARY KEY, company_ref TEXT, cik TEXT, accession TEXT, form TEXT,"
            "filed TEXT, report_date TEXT);"
            "CREATE TABLE IF NOT EXISTS coverage_mission_statement_lines("
            "line_id TEXT PRIMARY KEY, ingest_id TEXT, concept TEXT, period_start TEXT,"
            "period_end TEXT, value TEXT, unit TEXT, dimension_axis TEXT);"
        )
        self._seq = 0

    def spool(self, body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()
        objects = self.state / "connector-spool" / "objects" / digest[:2]
        objects.mkdir(parents=True, exist_ok=True)
        (objects / digest).write_bytes(body)
        return digest

    def seed_index(self, payload=None, *, envelope="envelope:1", form="8-K"):
        digest = self.spool(json.dumps(payload or acn_submissions()).encode())
        self._seq += 1
        connection = self.store.connection
        connection.execute("INSERT INTO connector_call_specs VALUES(?,?,?)", (
            f"spec:{self._seq}", "list_filings",
            json.dumps({"parameters": {"issuer": "0001467373", "form": form}})))
        connection.execute("INSERT INTO connector_invocations VALUES(?,?)",
                           (f"inv:{self._seq}", f"spec:{self._seq}"))
        connection.execute("INSERT INTO connector_source_envelopes VALUES(?,?,?,?,?)", (
            envelope, f"inv:{self._seq}", digest, "complete",
            json.dumps({"raw_response_hash": digest})))
        return digest

    def filing(self, form, report_date, accession=None, ingest=None):
        self._seq += 1
        ingest = ingest or f"ingest:{self._seq}"
        self.store.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?)",
            (ingest, ACN, "0001467373", accession or f"0001467373-26-{self._seq:06d}", form,
             report_date, report_date))
        return ingest


class ReportedPeriodTests(SpoolHarness):
    def test_a_2_02_8k_after_the_latest_filed_quarter_is_reported_not_filed(self):
        self.seed_index()
        self.filing("10-K", "2025-08-31")
        self.filing("10-Q", "2026-05-31")
        found = reported_release(self.store.connection, ACN)
        self.assertEqual(found["period"], "FY2026-Q4")
        self.assertEqual(found["status"], "reported")
        self.assertEqual(found["accession"], ACN_RELEASE)
        self.assertEqual(found["latest_filed_period"], "FY2026-Q3")

    def test_the_10k_landing_makes_it_filed(self):
        self.seed_index()
        self.filing("10-K", "2025-08-31")
        self.filing("10-Q", "2026-05-31")
        self.filing("10-K", "2026-08-31")
        self.assertIsNone(reported_release(self.store.connection, ACN))

    def test_a_release_for_an_already_filed_quarter_advances_nothing(self):
        # IBM-like: the July release is the June quarter, already filed.
        payload = acn_submissions()
        payload["filings"]["recent"]["items"][0] = "8.01"  # no Q4 release yet
        self.seed_index(payload)
        self.filing("10-K", "2025-08-31")
        self.filing("10-Q", "2026-05-31")
        self.assertIsNone(reported_release(self.store.connection, ACN))

    def test_without_a_10k_the_fiscal_year_end_comes_from_sec(self):
        self.seed_index()
        self.filing("10-Q", "2026-05-31")
        self.assertEqual(reported_release(self.store.connection, ACN)["period"], "FY2026-Q4")

    def test_an_in_memory_core_has_no_advance(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(intake.reported_fiscal_quarters(store.connection, [ACN]), {})


class StageAdvanceTests(unittest.TestCase):
    """The reported quarter becomes a required transcript period immediately."""

    def setUp(self):
        from tests.test_mission_stage import StageHarness

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = str(Path(self._dir.name) / "core.sqlite")
        harness = StageHarness("setUp")
        with mock.patch("tests.test_mission_stage.DaltonStore", lambda _: DaltonStore(path)):
            harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.harness = harness
        intake._INDEX_CACHE.clear()
        intake._ELIGIBLE_CACHE.clear()

    def test_accenture_q4_is_required_the_day_the_release_is_on_disk(self):
        from tests.test_mission_stage import TranscriptBootstrapTests

        harness = self.harness
        filing = TranscriptBootstrapTests.filing.__get__(harness)
        filing(ACN, "10-K", "2025-08-31")
        filing(ACN, "10-Q", "2026-05-31")
        calls = harness.item(harness.evaluate(), ACN, "earnings_calls")
        self.assertEqual(calls["required_periods"][-1], "FY2026-Q3")
        release = {"period": "FY2026-Q4", "period_end": "2026-08-31", "report_date": "2026-10-01",
                   "accession": ACN_RELEASE, "items": ["2.02", "9.01"],
                   "latest_filed_period": "FY2026-Q3", "status": "reported"}
        with mock.patch.object(intake, "reported_fiscal_quarters", return_value={ACN: release}):
            companies = harness.evaluate()
        calls = harness.item(companies, ACN, "earnings_calls")
        self.assertEqual(calls["required_periods"],
                         ["FY2026-Q1", "FY2026-Q2", "FY2026-Q3", "FY2026-Q4"])
        self.assertIn("FY2026-Q4", calls["missing_periods"])
        self.assertEqual(calls["period_basis"], "earnings_release")
        company = next(c for c in companies if c["company_ref"] == ACN)
        self.assertEqual(company["reported_release"]["period"], "FY2026-Q4")
        # The search form AlphaEngine titles the call with.
        from dalton_core.mission_source_discovery import quarter_title_label

        self.assertEqual(quarter_title_label(calls["missing_periods"][-1]), "Q4 2026")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.raw = (FIXTURES / "acn-ex991-fy26q4.htm").read_bytes()

    def test_the_income_statement_current_quarter_is_read_row_by_row(self):
        parsed = parse_press_release(self.raw, expected_period_end="2026-08-31")
        self.assertEqual(parsed["status"], "parsed")
        rows = {row["metric"]: row for row in parsed["rows"]}
        self.assertEqual(rows["revenue"]["value"], "17612345")
        self.assertEqual(rows["revenue"]["scale"], "thousand")
        self.assertEqual(rows["revenue"]["unit"], "USD")
        self.assertEqual(rows["operating_income"]["value"], "2399363")
        self.assertEqual(rows["net_income"]["value"], "1808509")
        self.assertEqual(rows["net_income_attributable"]["value"], "1775790")
        self.assertEqual(rows["eps_diluted"]["value"], "2.88")
        self.assertEqual(rows["eps_diluted"]["unit"], "USD/share")
        # "except share and per share amounts": share counts are not in thousands.
        self.assertEqual(rows["shares_diluted"]["scale"], "one")
        self.assertEqual(parsed["table"]["period_end"], "2026-08-31")

    def test_every_row_points_at_the_bytes_it_came_from(self):
        parsed = parse_press_release(self.raw, expected_period_end="2026-08-31")
        text = self.raw.decode("utf-8")
        for row in parsed["rows"]:
            start, end = row["span"]
            fragment = text[start:end]
            self.assertTrue(fragment.startswith("<tr"))
            self.assertTrue(fragment.endswith("</tr>"))
            self.assertIn(row["label_as_filed"], fragment)
            self.assertEqual(row["excerpt_hash"],
                             hashlib.sha256(row["excerpt"].encode()).hexdigest()
                             if len(row["excerpt"]) < 300 else row["excerpt_hash"])

    def test_a_quarter_that_is_not_the_8ks_is_refused(self):
        parsed = parse_press_release(self.raw, expected_period_end="2026-05-31")
        self.assertEqual(parsed["status"], "unparseable")
        self.assertIn("不符", parsed["reason"])

    def test_a_table_without_a_unit_is_refused_not_guessed(self):
        raw = self.raw.decode().replace("(In thousands of U.S. dollars, except share and per share amounts)", "")
        parsed = parse_press_release(raw, expected_period_end="2026-08-31")
        self.assertEqual(parsed["status"], "unparseable")
        self.assertIn("单位", parsed["reason"])

    def test_a_microsoft_style_table_reads_too(self):
        html = """<html><body><p>MICROSOFT CORPORATION INCOME STATEMENTS</p>
        <p>(In millions, except per share amounts) (Unaudited)</p><table>
        <tr><td></td><td>Three Months Ended June 30,</td><td>Twelve Months Ended June 30,</td></tr>
        <tr><td></td><td>2026</td><td>2025</td><td>2026</td><td>2025</td></tr>
        <tr><td>Revenue:</td></tr>
        <tr><td>Product</td><td>$</td><td>16,402</td><td>$</td><td>16,320</td><td>$</td><td>63,946</td><td>$</td><td>63,946</td></tr>
        <tr><td>Total revenue</td><td>$</td><td>79,412</td><td>$</td><td>76,441</td><td>$</td><td>300,100</td><td>$</td><td>281,724</td></tr>
        <tr><td>Operating income</td><td></td><td>35,010</td><td></td><td>34,323</td><td></td><td>140,002</td><td></td><td>128,528</td></tr>
        <tr><td>Net income</td><td>$</td><td>28,104</td><td>$</td><td>27,233</td><td>$</td><td>110,555</td><td>$</td><td>101,832</td></tr>
        <tr><td>Earnings per share:</td></tr>
        <tr><td>Basic</td><td>$</td><td>3.78</td><td>$</td><td>3.66</td><td>$</td><td>14.88</td><td>$</td><td>13.70</td></tr>
        <tr><td>Diluted</td><td>$</td><td>3.76</td><td>$</td><td>3.65</td><td>$</td><td>14.82</td><td>$</td><td>13.64</td></tr>
        <tr><td>Weighted-average shares outstanding:</td></tr>
        <tr><td>Basic</td><td></td><td>7,431</td><td></td><td>7,433</td><td></td><td>7,431</td><td></td><td>7,433</td></tr>
        <tr><td>Diluted</td><td></td><td>7,470</td><td></td><td>7,466</td><td></td><td>7,470</td><td></td><td>7,466</td></tr>
        </table></body></html>"""
        parsed = parse_press_release(html, expected_period_end="2026-06-30")
        self.assertEqual(parsed["status"], "parsed", parsed.get("reason"))
        rows = {row["metric"]: row for row in parsed["rows"]}
        self.assertEqual((rows["revenue"]["value"], rows["revenue"]["scale"]), ("79412", "million"))
        self.assertEqual(rows["eps_diluted"]["value"], "3.76")
        self.assertEqual(rows["shares_diluted"]["scale"], "million")

    def test_the_exhibit_is_named_by_the_8ks_own_exhibit_index(self):
        primary = (FIXTURES / "acn-8k-primary.htm").read_text()
        self.assertEqual(press_release_exhibit_name(primary), "acn-ex991_fy26q4.htm")
        self.assertEqual(press_release_exhibit_name('<a href="msft-ex99_1.htm">x</a>'),
                         "msft-ex99_1.htm")
        # A link elsewhere is not this filing's exhibit.
        self.assertIsNone(press_release_exhibit_name(
            '<tr><td><a href="https://example.com/x.htm">99.1</a></td></tr>'))
        self.assertIsNone(press_release_exhibit_name(
            '<tr><td><a href="../other/x.htm">99.1</a></td></tr>'))

    def test_the_exhibit_url_stays_in_the_accession_directory(self):
        self.assertEqual(exhibit_url("0001467373", ACN_RELEASE, "acn-ex991_fy26q4.htm"),
                         "https://www.sec.gov/Archives/edgar/data/1467373/"
                         "000146737326000045/acn-ex991_fy26q4.htm")
        with self.assertRaises(intake.EarningsReleaseError):
            exhibit_url("0001467373", ACN_RELEASE, "../x.htm")


class ExhibitAuthorityTests(unittest.TestCase):
    def test_the_exhibit_authority_is_the_primary_documents_with_one_name_changed(self):
        from dalton_core.public_web_connector import (
            public_web_url_ref, validate_public_web_url_authority,
        )
        from dalton_core.sec_filings_index import (
            SecFilingsIndexError, build_sec_exhibit_url_authority,
        )

        url = "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000045/acn-20261001.htm"
        url_ref = public_web_url_ref(url)
        base = {"schema_version": "0.1",
                "id": "public-web-url-authority:" + content_hash(
                    {"url_ref": url_ref, "source_envelope_hash": "b" * 64}),
                "created_at": "2026-10-02T00:00:00.000000+00:00", "url_ref": url_ref,
                "canonical_url": url, "url_hash": url_ref.removeprefix("public-web-url:sha256:"),
                "host": "www.sec.gov", "discovery_source_envelope_ref": "source-envelope:x",
                "discovery_source_envelope_hash": "b" * 64,
                "discovery_raw_artifact_version_ref": "artifact-version:x", "search_record_ref": url_ref}
        primary = validate_public_web_url_authority({**base, "content_hash": content_hash(base)})
        exhibit = build_sec_exhibit_url_authority(primary, "acn-ex991_fy26q4.htm")
        self.assertTrue(exhibit["canonical_url"].endswith("/000146737326000045/acn-ex991_fy26q4.htm"))
        self.assertEqual(exhibit["discovery_source_envelope_hash"], "b" * 64)
        for bad in ("../x.htm", "x.exe", "a/b.htm", "x.htm?y=1"):
            with self.assertRaises(SecFilingsIndexError):
                build_sec_exhibit_url_authority(primary, bad)


class LedgerHarness(SpoolHarness):
    """A fetched 8-K with its exhibit, read into the preliminary ledger."""

    def setUp(self):
        super().setUp()
        connection = self.store.connection
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS coverage_mission_discovered_documents("
            "record_id TEXT PRIMARY KEY, company_ref TEXT, source_ref TEXT, document_ref TEXT,"
            "discovery_ref TEXT, status TEXT, ticket_ref TEXT, updated_at TEXT);"
            "CREATE TABLE IF NOT EXISTS coverage_mission_source_discoveries("
            "record_id TEXT PRIMARY KEY, spec_ref TEXT, source_envelope_ref TEXT);")
        self.seed_index()
        self.filing("10-K", "2025-08-31")
        self.filing("10-Q", "2026-05-31")

    def fetched(self, *, exhibit_body=None, exhibit_status="succeeded", ticket="aaaabbbbccccddddeeeeffff"):
        from dalton_core.public_web_connector import public_web_url_ref

        document_ref = f"sec:filing:{ACN_RELEASE}"
        connection = self.store.connection
        connection.execute("INSERT INTO coverage_mission_source_discoveries VALUES(?,?,?)",
                           ("discovery:8k", "current-report-8k", "envelope:1"))
        connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?,?,?,?)",
            ("record:1", ACN, "source:sec-edgar", document_ref, "discovery:8k", "acquired",
             f"public-web-fetch:{ticket}", "2026-10-02T00:00:00+00:00"))
        directory = self.state / "fetches" / ticket
        directory.mkdir(parents=True)
        exhibit: dict = {"status": exhibit_status, "failure_reason": None if exhibit_status == "succeeded"
                         else "fetch outcome failed; HTTP 404"}
        if exhibit_body is not None and exhibit_status == "succeeded":
            digest = self.spool(exhibit_body)
            url = ("https://www.sec.gov/Archives/edgar/data/1467373/000146737326000045/"
                   "acn-ex991_fy26q4.htm")
            url_ref = public_web_url_ref(url)
            doc = f"public-web-document:url-sha256:{url_ref.rsplit(':', 1)[-1]}:body-sha256:{digest}"
            base = {"schema_version": "0.1",
                    "id": "public-web-fetch-manifest:" + content_hash(
                        {"document_ref": doc, "source_envelope_hash": "3" * 64}),
                    "created_at": "2026-10-02T00:00:01.000000+00:00", "document_ref": doc,
                    "url_ref": url_ref, "canonical_url": url, "host": "www.sec.gov", "final_url": url,
                    "source_record_ref": doc, "body_sha256": digest, "body_bytes": len(exhibit_body),
                    "raw_media_type": "text/html", "url_authority_ref": "public-web-url-authority:" + "d" * 64,
                    "url_authority_hash": "e" * 64, "discovery_source_envelope_ref": "source-envelope:x",
                    "discovery_source_envelope_hash": "f" * 64,
                    "connector_profile_ref": "connector-profile:web-fetch:x", "connector_profile_hash": "1" * 64,
                    "connector_invocation_ref": "connector-invocation:web-fetch:x",
                    "connector_invocation_hash": "2" * 64, "source_envelope_ref": "source-envelope:y",
                    "source_envelope_hash": "3" * 64, "raw_artifact_version_ref": "artifact-version:y",
                    "raw_response_hash": digest}
            manifest = {**base, "content_hash": content_hash(base)}
            (directory / "exhibit-manifest.json").write_text(json.dumps(manifest))
            exhibit.update(canonical_url=url, manifest_ref=manifest["id"],
                           manifest_hash=manifest["content_hash"], body_sha256=digest)
        summary = {"status": "succeeded", "url_ref": document_ref, "exhibit": exhibit}
        (directory / "summary.json").write_text(json.dumps(summary))
        return document_ref


class IngestAndSupersessionTests(LedgerHarness):
    def test_the_exhibit_is_read_once_into_preliminary_rows_with_span_and_accession(self):
        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        first = ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(first["reads"][0]["status"], "parsed")
        again = ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(again["reads"][0]["write"], "duplicate")
        rows = preliminary_actuals(self.store.connection, ACN)
        revenue = next(row for row in rows if row["metric"] == "revenue")
        self.assertEqual(revenue["status"], "preliminary")
        self.assertEqual(revenue["period"], "FY2026-Q4")
        self.assertEqual(revenue["accession"], ACN_RELEASE)
        self.assertEqual(revenue["label_note"], PRELIMINARY_LABEL)
        self.assertEqual(len(revenue["span"]), 2)
        claims = preliminary_claim_rows(self.store.connection, ACN)
        self.assertTrue(all(PRELIMINARY_LABEL in row["normalized_statement"] for row in claims))
        self.assertTrue(all(row["basis"] == intake.PRELIMINARY_BASIS for row in claims))
        # Never in the filed Ledger.
        count = self.store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_filed_10k_supersedes_every_preliminary_row(self):
        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(len(earnings_release_event_candidates(self.store.connection, company_ref=ACN)), 1)
        ingest = self.filing("10-K", "2026-08-31", accession="0001467373-26-000060")
        self.store.connection.execute(
            "INSERT INTO coverage_mission_statement_lines VALUES(?,?,?,?,?,?,?,?)",
            ("line:1", ingest, "us-gaap:Revenues", "2026-06-01", "2026-08-31", "17612000000", "USD", None))
        rows = preliminary_actuals(self.store.connection, ACN)
        self.assertTrue(rows and all(row["status"] == "superseded" for row in rows))
        revenue = next(row for row in rows if row["metric"] == "revenue")
        self.assertEqual(revenue["superseded_by"]["accession"], "0001467373-26-000060")
        self.assertEqual(revenue["filed_line"]["value"], "17612000000")
        # A preliminary number never outranks a filed one: once filed, nothing
        # preliminary is offered as a claim or an event.
        self.assertEqual(preliminary_claim_rows(self.store.connection, ACN), [])
        self.assertEqual(earnings_release_event_candidates(self.store.connection, company_ref=ACN), [])
        self.assertEqual(latest_preliminary(self.store.connection, ACN)["status"], "superseded")

    def test_an_unreadable_exhibit_is_one_needs_human_card_naming_the_filing(self):
        from dalton_core.needs_human import earnings_release_failures

        self.fetched(exhibit_body=b"<html><body><p>no table here</p></body></html>")
        report = ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(report["reads"][0]["status"], "unparseable")
        cards = earnings_release_failures(self.store.connection)
        self.assertEqual(len(cards), 1)
        self.assertIn(ACN_RELEASE, cards[0]["title"])
        self.assertEqual(cards[0]["kind"], "earnings_release_unread")
        # Filed: the card goes away.
        self.filing("10-K", "2026-08-31")
        self.assertEqual(earnings_release_failures(self.store.connection), [])

    def test_a_parsed_release_needs_nothing_from_a_human(self):
        from dalton_core.needs_human import earnings_release_failures

        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(earnings_release_failures(self.store.connection), [])

    def test_a_missing_exhibit_is_recorded_with_its_reason(self):
        self.fetched(exhibit_status="failed")
        report = ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.assertEqual(report["reads"][0]["status"], "exhibit_unavailable")
        self.assertIn("404", report["reads"][0]["reason"])

    def test_the_ledger_is_append_only(self):
        import sqlite3

        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE preliminary_actuals SET value='1'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM earnings_release_reads")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO preliminary_actuals(row_id,read_ref,company_ref,accession,metric,value,unit,"
                "scale,span_start,span_end,record_json,content_hash,created_at) "
                "VALUES('x','y',?,?,'revenue','1','USD','one',0,1,'{}','h','t')", (ACN, ACN_RELEASE))

    def test_the_event_payload_validates_and_carries_the_accession(self):
        from dalton_core.research_event import DEFAULT_TIER_BY_KIND, validate_payload

        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        event = earnings_release_event_candidates(self.store.connection, company_ref=ACN)[0]
        payload = validate_payload("earnings_release", event["payload"])
        self.assertEqual(payload["accession"], ACN_RELEASE)
        self.assertEqual(payload["revenue"], "17612345")
        self.assertEqual(payload["status"], "preliminary")
        self.assertEqual(event["evidence_tier"], "management_direct")
        self.assertEqual(DEFAULT_TIER_BY_KIND["earnings_release"], "management_direct")
        from dalton_core.event_triage import BYPASS_KINDS

        self.assertIn("earnings_release", BYPASS_KINDS)
        lines = intake.judgement_lines({"payload": payload, "company_ref": ACN}, self.store.connection)
        self.assertTrue(any("PRELIMINARY" in line for line in lines))

    def test_the_cockpit_card_shows_the_preliminary_rows_and_the_replacement_note(self):
        from dalton_core.cockpit_plane import CockpitPlane

        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        card = CockpitPlane._preliminary_results(self.store.connection, [ACN])[ACN]
        self.assertEqual(card["title"], "最新业绩（初步）")
        self.assertIn("自动取代", card["note"])
        self.assertEqual(card["rows"][0]["label"], "营业收入")
        self.assertIn("亿美元", card["rows"][0]["text"])
        self.assertIn(ACN_RELEASE, card["rows"][0]["source"])
        html = (Path(intake.__file__).with_name("cockpit_control.html")).read_text()
        self.assertIn("preliminaryBlock(c.preliminary_results)", html)

    def test_the_weekly_brief_lines_and_replay(self):
        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        entries = intake.brief_entries(self.store.connection, [ACN], since="2026-09-28")
        self.assertEqual(len(entries), 1)
        lines = intake.brief_lines(entries, {ACN: "ACN"})
        self.assertIn(PRELIMINARY_LABEL, lines[0])
        intake.replay_brief_entries(self.store.connection, entries)
        entries[0]["rows"][0]["value"] = "1"
        with self.assertRaises(intake.EarningsReleaseError):
            intake.replay_brief_entries(self.store.connection, entries)


class ProvisionalReconciliationTests(LedgerHarness):
    def test_preliminary_revenue_is_reconciled_against_the_forecast_and_not_written(self):
        self.fetched(exhibit_body=(FIXTURES / "acn-ex991-fy26q4.htm").read_bytes())
        ingest_company(self.store, company_ref=ACN, state_dir=self.state)
        self.store.connection.executescript(
            "CREATE TABLE model_forecast_line_versions(version_id TEXT, line_ref TEXT, version_number INT,"
            "subject_ref TEXT, record_json TEXT, content_hash TEXT);")
        line = {"id": "forecast-line-version:1", "metric_or_aspect": "metric:revenue-usd",
                "period": {"start": "2026-06-01", "end": "2026-08-31"}, "value": "17000",
                "unit": "million", "currency": "USD"}
        self.store.connection.execute(
            "INSERT INTO model_forecast_line_versions VALUES(?,?,?,?,?,?)",
            ("forecast-line-version:1", "line:1", 1, ACN, json.dumps(line), "h"))
        with mock.patch("dalton_core.forecast_reconciliation.validate_forecast_line", lambda value: value):
            found = intake.provisional_reconciliations(self.store.connection, ACN)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["actual_value"], "17612.345")
        self.assertEqual(found[0]["band"], "overturn_candidate")
        self.assertEqual(found[0]["status"], "provisional")
        self.assertTrue(found[0]["provisional"])


class AcquisitionFilterTests(LedgerHarness):
    def test_only_the_item_2_02_8k_is_fetched(self):
        connection = self.store.connection
        connection.execute("INSERT INTO coverage_mission_source_discoveries VALUES(?,?,?)",
                           ("discovery:8k", "current-report-8k", "envelope:1"))
        for index, accession in enumerate((ACN_RELEASE, "0001467373-26-000044")):
            connection.execute(
                "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?,?,?,?)",
                (f"record:{index}", ACN, "source:sec-edgar", f"sec:filing:{accession}",
                 "discovery:8k", "discovered", None, "2026-10-02T00:00:00+00:00"))
        plan = {"specs": [{"spec_ref": "annual-report-10k"}, {"spec_ref": "current-report-8k"}]}
        self.assertEqual(intake.ineligible_filing_refs(connection, plan),
                         ("sec:filing:0001467373-26-000044",))
        # Once the 10-K covers the quarter, the release is not worth two fetches.
        self.filing("10-K", "2026-08-31")
        self.assertEqual(set(intake.ineligible_filing_refs(connection, plan)),
                         {"sec:filing:0001467373-26-000044", f"sec:filing:{ACN_RELEASE}"})


class SecPlanSpecTests(unittest.TestCase):
    def legacy_plan(self):
        from dalton_core.mission_source_discovery import validate_discovery_plan

        base = {"budget": {"max_calls_24h": 50},
                "companies": {ACN: {"cik": "0001467373"}},
                "created_at": "2026-09-08T00:00:00.000000+00:00",
                "id": "discovery-plan:us-it-services:sec-filings:1",
                "mission_ref": "coverage-mission:us-it-services", "schema_version": "0.4",
                "source_ref": "source:sec-edgar",
                "specs": [{"form": "10-K", "lookback_days": 400, "rediscovery_interval_days": 30,
                           "retry_interval_days": 2, "spec_ref": "annual-report-10k"}]}
        return validate_discovery_plan({**base, "content_hash": content_hash(base)})

    def test_legacy_runs_the_8k_spec_with_its_own_10k_intervals_kept(self):
        from dalton_core.alphaengine_query_forms import effective_discovery_plan

        plan = self.legacy_plan()
        rendered = effective_discovery_plan(plan)
        self.assertEqual([spec["spec_ref"] for spec in rendered["specs"]],
                         ["annual-report-10k", "current-report-8k"])
        self.assertEqual(rendered["specs"][0]["lookback_days"], 400)  # owner-governed, kept
        self.assertEqual(rendered["specs"][1]["form"], "8-K")
        self.assertEqual(rendered["budget"], plan["budget"])
        self.assertNotEqual(rendered["content_hash"], plan["content_hash"])
        self.assertEqual(effective_discovery_plan(rendered), rendered)  # idempotent

    def test_a_generated_plan_is_already_the_code_set(self):
        from dalton_core.alphaengine_query_forms import effective_discovery_plan
        from dalton_core.mission_source_discovery import validate_discovery_plan
        from dalton_core.sec_plan_specs import generator_specs

        base = {**{k: v for k, v in self.legacy_plan().items() if k != "content_hash"},
                "specs": generator_specs()}
        plan = validate_discovery_plan({**base, "content_hash": content_hash(base)})
        self.assertEqual(effective_discovery_plan(plan)["content_hash"], plan["content_hash"])

    def test_the_parity_row_shows_the_drift(self):
        from dalton_core.sec_plan_specs import sec_plan_report

        report = sec_plan_report(self.legacy_plan())
        rows = {row["spec_ref"]: row for row in report["specs"]}
        self.assertEqual(rows["annual-report-10k"]["drift"], "none")
        self.assertEqual(rows["current-report-8k"]["drift"], "added_by_code")
        self.assertEqual(rows["current-report-8k"]["item_filter"], ["2.02"])
        self.assertEqual(rows["current-report-8k"]["intervals_from"], "code")
        self.assertEqual(report["rendered_from"], "code")

    def test_the_parity_rows_render_for_an_environment(self):
        from dalton_core import workspace_health_parity as parity

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sec.json"
            path.write_text(json.dumps(self.legacy_plan()))
            env = mock.Mock(state=Path(tmp), mission=object())
            with mock.patch("dalton_core.macos_launchagent._sec_discovery_plan", return_value=path):
                rows = parity.sec_plan_spec_rows(env)
        by_check = {row["check"]: row for row in rows}
        self.assertEqual(by_check["sec.plan_specs.annual-report-10k"]["status"], parity.OK)
        self.assertEqual(by_check["sec.plan_specs.current-report-8k"]["status"], parity.INFO)
        self.assertIn("added by code", by_check["sec.plan_specs.current-report-8k"]["detail"])
        self.assertIn("items=2.02", by_check["sec.plan_specs.current-report-8k"]["detail"])


class FetchChildExhibitTests(unittest.TestCase):
    """The fetch child's second governed call: Exhibit 99.1 beside the 8-K."""

    def primary_authority(self):
        from dalton_core.public_web_connector import (
            public_web_url_ref, validate_public_web_url_authority,
        )

        url = "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000045/acn-20261001.htm"
        url_ref = public_web_url_ref(url)
        base = {"schema_version": "0.1",
                "id": "public-web-url-authority:" + content_hash(
                    {"url_ref": url_ref, "source_envelope_hash": "b" * 64}),
                "created_at": "2026-10-02T00:00:00.000000+00:00", "url_ref": url_ref,
                "canonical_url": url, "url_hash": url_ref.removeprefix("public-web-url:sha256:"),
                "host": "www.sec.gov", "discovery_source_envelope_ref": "source-envelope:x",
                "discovery_source_envelope_hash": "b" * 64,
                "discovery_raw_artifact_version_ref": "artifact-version:x", "search_record_ref": url_ref}
        return validate_public_web_url_authority({**base, "content_hash": content_hash(base)})

    def test_the_exhibit_named_by_the_8k_is_fetched_and_its_manifest_written(self):
        from dalton_core.public_web_fetch_cli import fetch_press_release_exhibit

        primary = (FIXTURES / "acn-8k-primary.htm").read_bytes()
        requested = []

        class Spool:
            def read_object(self, digest):
                return primary

        class Fetch:
            def build_request(self, authority):
                requested.append(authority["canonical_url"])
                return authority

            def fetch(self, request):
                return {"outcome": "succeeded", "document_ref": "doc", "provider_calls": 1}

            def manifest(self, receipt):
                return {"id": "public-web-fetch-manifest:m", "content_hash": "h" * 64,
                        "body_sha256": "a" * 64, "body_bytes": 12}

        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_press_release_exhibit(
                Fetch(), Spool(), primary_authority=self.primary_authority(),
                primary_receipt={"raw_response_hash": "x"}, out=Path(tmp))
            self.assertTrue((Path(tmp) / "exhibit-manifest.json").is_file())
        self.assertEqual(result["status"], "succeeded")
        # 2026-10-09: the filing's own document table is asked first (one call;
        # this fake serves the 8-K for it, which names no EX-99 type), then the
        # 8-K's exhibit index decides and the exhibit is fetched.
        self.assertEqual(result["provider_calls"], 2)
        self.assertEqual(result["detected_by"], "primary_links")
        self.assertEqual(requested, ["https://www.sec.gov/Archives/edgar/data/1467373/"
                                     "000146737326000045/0001467373-26-000045-index.html",
                                     "https://www.sec.gov/Archives/edgar/data/1467373/"
                                     "000146737326000045/acn-ex991_fy26q4.htm"])

    def test_an_8k_without_an_exhibit_costs_only_the_index_call(self):
        from dalton_core.public_web_fetch_cli import fetch_press_release_exhibit

        class Spool:
            def read_object(self, digest):
                return b"<html><p>Item 8.01 Other Events</p></html>"

        fetch = mock.Mock()
        fetch.build_request.side_effect = lambda authority: authority
        fetch.fetch.return_value = {"outcome": "succeeded", "document_ref": "doc",
                                    "provider_calls": 1, "raw_response_hash": "i"}
        with tempfile.TemporaryDirectory() as tmp:
            result = fetch_press_release_exhibit(
                fetch, Spool(), primary_authority=self.primary_authority(),
                primary_receipt={"raw_response_hash": "x"}, out=Path(tmp))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(fetch.fetch.call_count, 1)

    def test_only_an_item_2_02_8k_of_the_release_spec_gets_an_exhibit_fetch(self):
        from dalton_core.public_web_fetch_cli import press_release_due

        payload = json.dumps(acn_submissions()).encode()

        class Spool:
            def read_object(self, digest):
                return payload

        import sqlite3

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE connector_source_envelopes(source_envelope_id TEXT, record_json TEXT)")
        connection.execute("INSERT INTO connector_source_envelopes VALUES('e', ?)",
                           (json.dumps({"raw_response_hash": "x"}),))
        discovery = {"spec_ref": "current-report-8k", "source_ref": "source:sec-edgar",
                     "source_envelope_ref": "e"}
        self.assertTrue(press_release_due(connection, Spool(), discovery, f"sec:filing:{ACN_RELEASE}"))
        self.assertFalse(press_release_due(connection, Spool(), discovery,
                                           "sec:filing:0001467373-26-000044"))
        self.assertFalse(press_release_due(connection, Spool(), {**discovery, "spec_ref": "annual-report-10k"},
                                           f"sec:filing:{ACN_RELEASE}"))


class LateCalibrationTests(unittest.TestCase):
    def test_a_confirmed_release_learned_late_still_opens_its_calibration(self):
        from dalton_core.catalyst_calendar import emit_calendar_events

        entry = {"entry_ref": "catalyst-entry:x", "event_kind": "earnings",
                 "expected_date": "2026-10-01", "confidence": "confirmed", "disagreement": False,
                 "sources": [{"ref": f"sec:filing:{ACN_RELEASE}", "source_ref": "source:sec-edgar"}]}
        seen: list = []
        emit_calendar_events(company_ref=ACN, entries=[entry], record_event=lambda **k: seen.append(k),
                             now="2026-10-08")
        self.assertEqual(seen, [])  # the two-day window closed on 10-03
        emit_calendar_events(company_ref=ACN, entries=[entry], record_event=lambda **k: seen.append(k),
                             now="2026-10-08", late_calibration_entry_refs=["catalyst-entry:x"])
        self.assertEqual([call["payload"]["window"] for call in seen], ["calibration"])
        # An estimated date never opens one, late or not.
        seen.clear()
        emit_calendar_events(company_ref=ACN, entries=[{**entry, "confidence": "estimated"}],
                             record_event=lambda **k: seen.append(k), now="2026-10-08",
                             late_calibration_entry_refs=["catalyst-entry:x"])
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
