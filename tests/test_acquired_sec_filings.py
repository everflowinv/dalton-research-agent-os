"""D2 / F13-US: the disclosure parsers read the SEC filings the discovery lane fetched.

Live, both Cores held each covered company's 10-K bytes in the spool while the
tracking lane's disclosure scan read ``document_index_documents`` from
``core.sqlite`` -- a table that lives in another file -- and reported zero
filings every run.  These tests stand a Core directory up the way the SEC
discovery lane leaves it (discovered-document row, completed fetch ticket,
spooled body, spooled submissions index) and read it through the same calls
the tracking lane makes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.acquired_sec_filings import acquired_filing_documents
from dalton_core.buyback_disclosure import (
    buyback_event_candidates, issuer_purchase_section,
)
from dalton_core.insider_trading_plan import (
    inline_xbrl_trading_arrangements, trading_arrangement_events,
    trading_plan_event_candidates,
)
from dalton_core.public_web_connector import public_web_url_ref
from dalton_core.raw_spool import RawSpool
from dalton_core.research_event import validate_payload
from dalton_core.store import content_hash

FIXTURE = Path(__file__).parent / "fixtures/sec-10k-item9b/synthetic-10k.htm"
COMPANY = "company:sec-cik:0009999999"
ACCESSION = "0009999999-26-000001"
URL = "https://www.sec.gov/Archives/edgar/data/9999999/000999999926000001/fix-20251231.htm"
TICKET = "public-web-fetch:" + "c" * 24


def spool(state: Path, data: bytes) -> str:
    sink = RawSpool(state / "transcript-spool", max_total_bytes=50_000_000).open_sink(
        "raw-sink:" + hashlib.sha256(data).hexdigest(), max_response_bytes=len(data) + 1)
    sink.write(data)
    return sink.finalize().content_hash


def manifest_for(body: bytes, url: str = URL) -> dict:
    digest = hashlib.sha256(body).hexdigest()
    document_ref = f"public-web-document:url-sha256:{'b' * 64}:body-sha256:{digest}"
    base = {
        "schema_version": "0.1", "created_at": "2026-09-08T11:30:24.036259+00:00",
        "document_ref": document_ref, "source_record_ref": document_ref,
        "url_ref": public_web_url_ref(url), "canonical_url": url, "final_url": url,
        "host": "www.sec.gov", "body_sha256": digest, "body_bytes": len(body),
        "raw_media_type": "text/html", "raw_response_hash": digest,
        "url_authority_ref": "url-authority:fixture", "url_authority_hash": "1" * 64,
        "discovery_source_envelope_ref": "source-envelope:" + "2" * 64,
        "discovery_source_envelope_hash": "3" * 64,
        "connector_profile_ref": "connector-profile:web-fetch:fixture",
        "connector_profile_hash": "4" * 64,
        "connector_invocation_ref": "connector-invocation:web-fetch:fixture",
        "connector_invocation_hash": "5" * 64,
        "source_envelope_ref": "source-envelope:" + "6" * 64,
        "source_envelope_hash": "7" * 64,
        "raw_artifact_version_ref": "artifact-version:" + "8" * 64,
    }
    base["id"] = "public-web-fetch-manifest:" + content_hash(
        {"document_ref": base["document_ref"], "source_envelope_hash": base["source_envelope_hash"]})
    return {**base, "content_hash": content_hash(base)}


class AcquiredCore:
    """A Core directory shaped like the SEC discovery lane leaves it."""

    def __init__(self, case: unittest.TestCase, *, body: bytes | None = None,
                 url: str = URL, submissions: bool = True, form: str = "10-K") -> None:
        temp = tempfile.TemporaryDirectory()
        case.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.body = FIXTURE.read_bytes() if body is None else body
        self.connection = sqlite3.connect(str(self.state / "core.sqlite"))
        self.connection.row_factory = sqlite3.Row
        case.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE coverage_mission_discovered_documents(record_id TEXT, company_ref TEXT,"
            " source_ref TEXT, document_ref TEXT, ticket_ref TEXT, discovery_ref TEXT,"
            " status TEXT, created_at TEXT);"
            "CREATE TABLE coverage_mission_source_discoveries(record_id TEXT, source_envelope_ref TEXT);"
            "CREATE TABLE connector_source_envelopes(source_envelope_id TEXT, raw_response_hash TEXT);"
            "CREATE TABLE coverage_mission_statement_filings(accession TEXT, form TEXT, filed TEXT,"
            " entity_name TEXT, recorded_at TEXT);")
        spool(self.state, self.body)
        if submissions:
            index = json.dumps({"name": "Fixture Holdings, Inc.", "filings": {"recent": {
                "accessionNumber": ["0009999999-26-000009", ACCESSION],
                "form": ["8-K", form], "filingDate": ["2026-03-01", "2026-02-06"],
                "primaryDocument": ["x8k.htm", url.rsplit("/", 1)[-1]],
            }}}).encode()
            self.connection.execute("INSERT INTO coverage_mission_source_discoveries VALUES(?,?)",
                                    ("mission-source-discovery:fixture", "source-envelope:index"))
            self.connection.execute("INSERT INTO connector_source_envelopes VALUES(?,?)",
                                    ("source-envelope:index", spool(self.state, index)))
        self.connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?,?,?,?)",
            ("mission-discovered-document:fixture", COMPANY, "source:sec-edgar",
             f"sec:filing:{ACCESSION}", TICKET, "mission-source-discovery:fixture",
             "acquired", "2026-09-08T08:53:19+00:00"))
        self.connection.commit()
        ticket = self.state / "fetches" / TICKET.split(":", 1)[1]
        ticket.mkdir(parents=True, mode=0o700)
        manifest = manifest_for(self.body, url)
        for name, value in (
            ("ticket.json", {"id": TICKET, "status": "succeeded",
                             "document_ref": f"sec:filing:{ACCESSION}"}),
            ("summary.json", {"url_ref": f"sec:filing:{ACCESSION}", "canonical_url": url,
                              "manifest_ref": manifest["id"],
                              "manifest_hash": manifest["content_hash"], "status": "succeeded"}),
            ("manifest.json", manifest),
        ):
            path = ticket / name
            path.write_text(json.dumps(value), encoding="utf-8")
            os.chmod(path, 0o600)


class AcquiredFilingReaderTests(unittest.TestCase):
    def test_spooled_filing_is_read_with_form_and_date_from_the_submissions_index(self):
        core = AcquiredCore(self)
        found = acquired_filing_documents(core.connection, company_ref=COMPANY, state_dir=core.state)
        self.assertEqual(len(found["documents"]), 1, found["reports"])
        document = found["documents"][0]
        self.assertEqual((document["form"], document["document_date"], document["accession"]),
                         ("10-K", "2026-02-06", ACCESSION))
        self.assertEqual(document["issuer_name"], "Fixture Holdings, Inc.")
        self.assertEqual(document["artifact_hash"], hashlib.sha256(core.body).hexdigest())
        self.assertIn("Issuer Purchases of Equity Securities", document["rendered_text"])

    def test_statement_ledger_names_the_filing_when_the_index_is_pruned(self):
        core = AcquiredCore(self, submissions=False)
        self.assertEqual(acquired_filing_documents(
            core.connection, company_ref=COMPANY, state_dir=core.state)["documents"], [])
        core.connection.execute("INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?)",
                                (ACCESSION, "10-K", "2026-02-06", "FIXTURE HOLDINGS", "2026-02-07"))
        document = acquired_filing_documents(
            core.connection, company_ref=COMPANY, state_dir=core.state)["documents"][0]
        self.assertEqual((document["form"], document["identity_basis"]),
                         ("10-K", "statement_filing_ledger"))

    def test_a_fetch_outside_the_accession_directory_is_refused(self):
        core = AcquiredCore(self, url="https://www.sec.gov/Archives/edgar/data/1/000000000126000002/x.htm")
        found = acquired_filing_documents(core.connection, company_ref=COMPANY, state_dir=core.state)
        self.assertEqual(found["documents"], [])
        self.assertIn("EDGAR 目录", found["reports"][0]["reason"])

    def test_changed_spool_bytes_are_refused_not_read(self):
        core = AcquiredCore(self)
        digest = hashlib.sha256(core.body).hexdigest()
        target = next((core.state / "transcript-spool").rglob(digest))
        os.chmod(target, 0o600)
        target.write_bytes(core.body.replace(b"Jane Q. Public", b"Someone Else!!"))
        found = acquired_filing_documents(core.connection, company_ref=COMPANY, state_dir=core.state)
        self.assertEqual(found["documents"], [])
        self.assertEqual(found["reports"][0]["status"], "refused")

    def test_a_core_without_fetch_tickets_reads_nothing_and_says_why(self):
        core = AcquiredCore(self)
        import shutil
        shutil.rmtree(core.state / "fetches")
        found = acquired_filing_documents(core.connection, company_ref=COMPANY, state_dir=core.state)
        self.assertEqual(found["documents"], [])
        self.assertEqual(found["reports"][0]["status"], "unavailable")


class DisclosureFromAcquiredFilingTests(unittest.TestCase):
    def test_10k_item9b_tagged_plans_become_events(self):
        core = AcquiredCore(self)
        plans = trading_plan_event_candidates(core.connection, company_ref=COMPANY,
                                              state_dir=core.state)
        rows = {event["payload"]["person_name"]: event["payload"] for event in plans["events"]}
        self.assertEqual(set(rows), {"Jane Q. Public", "John R. Doe"})
        jane, john = rows["Jane Q. Public"], rows["John R. Doe"]
        self.assertEqual((jane["form"], jane["filing_date"], jane["action"], jane["action_date"]),
                         ("10-K", "2026-02-06", "adopted", "2025-11-03"))
        self.assertEqual((jane["aggregate_shares"], jane["expiration_date"], jane["person_title"]),
                         ("64059", "2026-11-30", "Chief Financial Officer"))
        self.assertIn("Jane Q. Public", jane["material_terms_excerpt"])
        # Two share classes are two filed figures; their sum is not one.
        self.assertEqual((john["action"], john["action_date"], john["aggregate_shares"]),
                         ("terminated", "2025-12-01", None))
        for event in plans["events"]:
            validate_payload("insider_trading_plan", event["payload"])
            self.assertIn(f"sec:filing:{ACCESSION}", event["source_refs"])

    def test_10k_item5_table_becomes_buyback_events_with_amounts(self):
        core = AcquiredCore(self)
        produced = buyback_event_candidates(core.connection, company_ref=COMPANY,
                                            state_dir=core.state)
        rows = [event["payload"] for event in produced["events"]]
        self.assertEqual([(row["shares_purchased"], row["average_price_paid"],
                           row["remaining_authorisation"]) for row in rows],
                         [("100000", "50.00", "900"), ("200000", "55.00", "889"),
                          ("0", "0", "889")])
        self.assertEqual(produced["tie_outs"][0]["status"], "matched")
        self.assertTrue(all(row["form"] == "10-K" and row["market"] == "US" for row in rows))

    def test_without_a_state_directory_the_scan_is_unchanged(self):
        core = AcquiredCore(self)
        self.assertEqual(trading_plan_event_candidates(core.connection, company_ref=COMPANY)["events"], [])

    def test_tracking_company_events_reads_the_core_directory(self):
        from dalton_core.tracking_lane_cli import _core_directory

        core = AcquiredCore(self)
        self.assertEqual(_core_directory(core.connection), core.state.resolve())


class SectionAndInlineTests(unittest.TestCase):
    def parse(self, text: str, form: str = "10-K") -> dict:
        return trading_arrangement_events(
            text, company_ref=COMPANY, accession=ACCESSION, filing_date="2026-02-06",
            issuer_name="Issuer", document_ref="d", artifact_hash="a" * 64, form=form)

    def test_untagged_10k_prose_with_name_title_order_is_read(self):
        text = (
            "Item 9B. Other Information 13\nItem 9C. Disclosure 13\n\n"
            "Item 9B. Other Information\n\nOn November 3, 2025 , David A. Zapolsky , Senior Vice "
            "President, Chief Global Affairs and Legal Officer , adopted a trading plan intended "
            "to satisfy Rule 10b5-1(c) to sell up to 64,059 shares of common stock over a period "
            "ending on November 30, 2026 , subject to certain conditions. On November 10, 2025 , "
            "Douglas J. Herrington , CEO Worldwide Stores , adopted a trading plan intended to "
            "satisfy Rule 10b5-1(c) to sell up to 132,937 shares.\n\n"
            "Item 9C. Disclosure Regarding Foreign Jurisdictions\n\nNot applicable.")
        result = self.parse(text)
        self.assertEqual(result["status"], "read", result["reason"])
        people = [(row["payload"]["person_name"], row["payload"]["action_date"],
                   row["payload"]["aggregate_shares"]) for row in result["events"]]
        self.assertEqual(people, [("David A. Zapolsky", "2025-11-03", "64059"),
                                  ("Douglas J. Herrington", "2025-11-10", "132937")])

    def test_untagged_10k_negative_disclosure_is_empty(self):
        text = ("Item 9B. Other Information\n\nDuring the three months ended December 31, 2025, "
                "no director or officer of the Company adopted or terminated a Rule 10b5-1 "
                "trading arrangement.\n\nItem 9C. Disclosure\n\nNot applicable.")
        self.assertEqual(self.parse(text)["status"], "empty")

    def test_entity_level_false_flags_alone_are_empty(self):
        html = ('<html><body><ix:nonNumeric name="ecd:Rule10b51ArrAdoptedFlag" contextRef="c-1" '
                'format="ixt:fixed-false">false</ix:nonNumeric></body></html>')
        result = inline_xbrl_trading_arrangements(
            html, company_ref=COMPANY, accession=ACCESSION, form="10-Q",
            filing_date="2026-02-06", issuer_name="Issuer", document_ref="d",
            artifact_hash="a" * 64)
        self.assertEqual(result["status"], "empty")

    def test_markup_is_admitted_beyond_the_plain_text_bound(self):
        html = "<html><body>" + "<div>x</div>" * 100_000 + "</body></html>"
        self.assertGreater(len(html), 1_000_000)
        self.assertEqual(self.parse(html)["status"], "absent")

    def test_one_filings_plan_rows_share_one_judgement_slot(self):
        from dalton_core.event_judgement_cli import event_group_key

        def event(event_id, accession):
            return {"id": event_id, "kind": "insider_trading_plan", "company_ref": COMPANY,
                    "payload": {"accession": accession}}

        self.assertEqual(event_group_key(event("e1", ACCESSION)),
                         event_group_key(event("e2", ACCESSION)))
        self.assertNotEqual(event_group_key(event("e1", ACCESSION)),
                            event_group_key(event("e3", "0009999999-26-000002")))

    def test_issuer_purchase_section_skips_the_table_of_contents(self):
        text = ("Issuer Purchases of Equity Securities\n\n37\n\nItem 6.\n\nReserved\n\n"
                "Item 5. Market and Issuer Purchases of Equity Securities\n\n"
                "Period\n\nOctober 1, 2025 to October 31, 2025\n\n10\n\n$\n\n5.00\n\nItem 6.")
        section = issuer_purchase_section(text)
        self.assertIn("October 1, 2025 to October 31, 2025", section["blocks"])


if __name__ == "__main__":
    unittest.main()
