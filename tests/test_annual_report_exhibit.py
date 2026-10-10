"""2026-10-09: a 10-K's Exhibit 13 is fetched beside it and read with it.

IBM's FY2025 10-K incorporates "Portions of IBM's Annual Report to
Stockholders" by reference and links the report from its exhibit index row
"(13) Annual Report to Security Holders"; the segment table a directed read
asked for twice lives there.  Fixtures are cut-down, anonymised versions of
that 10-K's cover and exhibit index, and of EPAM's, which incorporates
nothing.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_research import (
    ANNUAL_REPORT_EXHIBIT_READER,
    PublicWebDocumentSourceAdapter,
)
from dalton_core.public_web_fetch_cli import fetch_annual_report_exhibit
from dalton_core.raw_spool import RawSpool
from dalton_core.sec_annual_report_exhibit import (
    DETECTOR_VERSION,
    MANIFEST_FILE,
    RECORD_FILE,
    annual_report_exhibit_name,
    detect,
    exhibit_due,
    exhibit_for_ticket,
)
from dalton_core.store import content_hash

ACCESSION = "0000000001-26-000010"
DIRECTORY = "https://www.sec.gov/Archives/edgar/data/1/000000000126000010"

INCORPORATING_10K = (
    "<html><body><p>FORM 10-K</p><p>Documents incorporated by reference:</p>"
    "<p>Portions of the Issuer&#8217;s Annual Report to Stockholders for the year ended "
    "December 31, 2025 are incorporated by reference into Parts I, II and IV of this Form 10-K.</p>"
    "<table><tr><td>(10)</td><td>Material contracts</td>"
    "<td><a href=\"https://www.sec.gov/Archives/edgar/data/1/000000000124000012/x10kex101.htm\">"
    "x</a></td></tr>"
    "<tr><td>(13)</td><td>Annual Report to Security Holders</td><td>(2)</td>"
    "<td><a href=\"issuer-20251231_d2.htm\">13</a></td></tr>"
    "<tr><td>(21)</td><td>Subsidiaries</td><td><a href=\"ex21.htm\">21</a></td></tr>"
    "</table></body></html>"
).encode("utf-8")
SELF_CONTAINED_10K = (
    "<html><body><p>FORM 10-K</p><p>Item 7. Management's Discussion and Analysis</p>"
    "<table><tr><td>21.1</td><td>Subsidiaries</td><td><a href=\"ex21.htm\">x</a></td></tr>"
    "</table></body></html>"
).encode("utf-8")
EXHIBIT_13 = (
    "<html><body><p>Segment Details</p><table>"
    "<tr><td>Consulting revenue</td><td>$</td><td>20,996</td></tr>"
    "<tr><td>Consulting pre-tax income</td><td>$</td><td>2,465</td></tr>"
    "</table></body></html>"
).encode("utf-8")


class DetectionTests(unittest.TestCase):
    def test_the_incorporated_annual_report_and_its_file_are_found(self) -> None:
        found = detect(INCORPORATING_10K, accession=ACCESSION)
        self.assertEqual((found["incorporated"], found["name"]),
                         (True, "issuer-20251231_d2.htm"))

    def test_a_self_contained_10k_costs_nothing(self) -> None:
        self.assertEqual(detect(SELF_CONTAINED_10K, accession=ACCESSION)["incorporated"], False)

    def test_ambiguous_or_foreign_links_name_nothing(self) -> None:
        two = INCORPORATING_10K.replace(
            b'<a href="issuer-20251231_d2.htm">13</a>',
            b'<a href="a.htm">13</a><a href="b.htm">13</a>')
        self.assertIsNone(annual_report_exhibit_name(two, accession=ACCESSION))
        foreign = INCORPORATING_10K.replace(
            b'href="issuer-20251231_d2.htm"',
            b'href="https://www.sec.gov/Archives/edgar/data/1/000000000125000015/old_d2.htm"')
        self.assertIsNone(annual_report_exhibit_name(foreign, accession=ACCESSION))
        own = INCORPORATING_10K.replace(
            b'href="issuer-20251231_d2.htm"', f'href="{DIRECTORY}/issuer-20251231_d2.htm"'.encode())
        self.assertEqual(annual_report_exhibit_name(own, accession=ACCESSION),
                         "issuer-20251231_d2.htm")


class _Spool:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def read_object(self, digest: str) -> bytes:
        return self.objects[digest]


class _Fetch:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def build_request(self, authority):
        return {"authority": authority}

    def fetch(self, request):
        self.requests.append(request)
        return {"outcome": "succeeded", "document_ref": "public-web-document:x",
                "provider_calls": 1}

    def manifest(self, receipt):
        return {"id": "public-web-fetch-manifest:ex13", "content_hash": "c" * 64,
                "body_sha256": "d" * 64, "body_bytes": len(EXHIBIT_13)}


def _primary_authority() -> dict:
    return {"canonical_url": f"{DIRECTORY}/issuer-20251231.htm"}


class FetchTests(unittest.TestCase):
    def test_a_self_contained_10k_records_why_and_makes_no_call(self) -> None:
        fetch = _Fetch()
        with tempfile.TemporaryDirectory() as temp:
            result = fetch_annual_report_exhibit(
                fetch, _Spool({"p": SELF_CONTAINED_10K}),
                primary_authority=_primary_authority(),
                primary_receipt={"raw_response_hash": "p"}, out=Path(temp))
        self.assertEqual((result["status"], result["provider_calls"]), ("not_incorporated", 0))
        self.assertEqual(fetch.requests, [])

    def test_the_named_exhibit_is_fetched_in_the_filings_own_directory(self) -> None:
        fetch = _Fetch()
        seen = {}

        def authority(primary, name):
            seen["name"] = name
            return {"canonical_url": f"{DIRECTORY}/{name}"}

        import dalton_core.sec_filings_index as index

        original = index.build_sec_exhibit_url_authority
        index.build_sec_exhibit_url_authority = authority
        try:
            with tempfile.TemporaryDirectory() as temp:
                result = fetch_annual_report_exhibit(
                    fetch, _Spool({"p": INCORPORATING_10K}),
                    primary_authority=_primary_authority(),
                    primary_receipt={"raw_response_hash": "p"}, out=Path(temp))
                written = json.loads((Path(temp) / MANIFEST_FILE).read_text())
        finally:
            index.build_sec_exhibit_url_authority = original
        self.assertEqual(seen["name"], "issuer-20251231_d2.htm")
        self.assertEqual((result["status"], result["provider_calls"],
                          result["detector_version"]), ("succeeded", 1, DETECTOR_VERSION))
        self.assertEqual(written["id"], "public-web-fetch-manifest:ex13")


def _manifest(body: bytes) -> dict:
    digest = hashlib.sha256(body).hexdigest()
    url = f"{DIRECTORY}/issuer-20251231_d2.htm"
    url_hash = hashlib.sha256(url.encode()).hexdigest()
    document_ref = f"public-web-document:url-sha256:{url_hash}:body-sha256:{digest}"
    wire = {
        "schema_version": "0.1", "created_at": "2026-10-09T00:00:00+00:00",
        "document_ref": document_ref, "url_ref": f"public-web-url:sha256:{url_hash}",
        "canonical_url": url, "host": "www.sec.gov", "final_url": url,
        "source_record_ref": document_ref, "body_sha256": digest, "body_bytes": len(body),
        "raw_media_type": "text/html", "url_authority_ref": "public-web-url-authority:x",
        "url_authority_hash": "1" * 64, "discovery_source_envelope_ref": "source-envelope:d",
        "discovery_source_envelope_hash": "2" * 64, "connector_profile_ref": "profile:x",
        "connector_profile_hash": "3" * 64, "connector_invocation_ref": "invocation:x",
        "connector_invocation_hash": "4" * 64, "source_envelope_ref": "source-envelope:e",
        "source_envelope_hash": "5" * 64, "raw_artifact_version_ref": "artifact-version:ex13",
        "raw_response_hash": digest,
    }
    wire["id"] = "public-web-fetch-manifest:" + content_hash(
        {"document_ref": document_ref, "source_envelope_hash": wire["source_envelope_hash"]})
    wire["content_hash"] = content_hash(wire)
    return wire


class ReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.ticket = "public-web-fetch:" + "a" * 24
        self.directory = self.state / "fetches" / ("a" * 24)
        self.directory.mkdir(parents=True)
        (self.directory / "summary.json").write_text(json.dumps(
            {"status": "succeeded", "url_ref": f"sec:filing:{ACCESSION}"}))

    def _record_exhibit(self) -> dict:
        manifest = _manifest(EXHIBIT_13)
        sink = RawSpool(str(self.state / "connector-spool"), max_total_bytes=10_000_000).open_sink(
            "raw-sink:" + manifest["body_sha256"], max_response_bytes=10_000_000)
        sink.write(EXHIBIT_13)
        sink.finalize()
        (self.directory / MANIFEST_FILE).write_text(json.dumps(manifest))
        (self.directory / RECORD_FILE).write_text(json.dumps({"exhibit": {
            "status": "succeeded", "manifest_ref": manifest["id"],
            "manifest_hash": manifest["content_hash"], "detector_version": DETECTOR_VERSION}}))
        return manifest

    def test_a_10k_fetched_before_this_is_due_once(self) -> None:
        self.assertTrue(exhibit_due(self.state, self.ticket))
        (self.directory / RECORD_FILE).write_text(json.dumps({"exhibit": {
            "status": "not_incorporated", "detector_version": DETECTOR_VERSION}}))
        self.assertFalse(exhibit_due(self.state, self.ticket))

    def test_the_fetched_exhibit_is_read_back_re_hashed(self) -> None:
        self._record_exhibit()
        found = exhibit_for_ticket(self.state, self.ticket, f"sec:filing:{ACCESSION}")
        self.assertEqual(found["status"], "fetched")
        self.assertEqual(found["bytes"], EXHIBIT_13)
        self.assertEqual(exhibit_for_ticket(self.state, self.ticket, "sec:filing:other")["status"],
                         "no_ticket")

    def test_the_directed_read_adapter_appends_the_exhibit(self) -> None:
        self._record_exhibit()
        adapter = PublicWebDocumentSourceAdapter.__new__(PublicWebDocumentSourceAdapter)
        adapter.launcher = type("Launcher", (), {"state_dir": self.state})()
        adapter.max_source_chars, adapter.max_pdf_pages = 1_000_000, 10
        adapter.max_decompressed_bytes = 10_000_000
        exhibit = adapter._annual_report_exhibit(f"sec:filing:{ACCESSION}", self.ticket)
        self.assertIn("Consulting revenue", exhibit["text"])
        self.assertIn("20,996", exhibit["text"])
        self.assertEqual(exhibit["artifact_ref"], "artifact-version:ex13")
        self.assertIsNone(adapter._annual_report_exhibit("public-web-url:sha256:" + "0" * 64,
                                                         self.ticket))
        self.assertTrue(ANNUAL_REPORT_EXHIBIT_READER.startswith("annual-report-exhibit"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
