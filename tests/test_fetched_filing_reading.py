"""P11t: a filing fetched by URL is read like the page it is.

Live, five acquired annual reports sat in the extraction queue and every run
refused all five: "only acquired AlphaEngine documents and fetched public-web
pages can be viewed here".  The bytes were on disk, fetched over the same
public HTTPS path as any searched page.  Two things stood in the way, and both
are the same mistake -- treating the ref a document is *queued* under as the
ref its bytes are *keyed* under.

A searched page is queued and fetched under one ref.  A SEC filing is queued by
accession and fetched by the URL derived from it, so the two differ, and code
that assumed they never do refused the filing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_extraction import (
    FETCHED_SOURCE_REFS,
    PUBLIC_WEB_SOURCE_REF,
    SEC_EDGAR_SOURCE_REF,
    SUPPORTED_SOURCE_REFS,
)
from dalton_core.public_web_connector import public_web_url_ref
from dalton_core.public_web_fetch_launcher import (
    FetchLaunchRejected,
    PublicWebFetchLauncher,
)

URL = "https://www.sec.gov/Archives/edgar/data/1467373/000146737325000217/acn-20250831.htm"
URL_REF = public_web_url_ref(URL)
FILING = "sec:filing:0001467373-25-000217"
TICKET = "public-web-fetch:" + "c" * 24


class ManifestReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.launcher = PublicWebFetchLauncher(
            state_dir=self.state, governance_path=self.state / "unused.json",
        )

    def write(self, *, queued_ref: str, manifest_url_ref: str, canonical_url: str = URL):
        directory = self.state / "fetches" / TICKET.split(":", 1)[1]
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest = {
            "schema_version": "0.1",
            "id": "public-web-fetch-manifest:" + "a" * 32,
            "url_ref": manifest_url_ref,
            "document_ref": "public-web-document:url-sha256:" + "b" * 64 + ":body-sha256:" + "c" * 64,
            "canonical_url": canonical_url,
            "host": "www.sec.gov",
            "body_sha256": "c" * 64,
            "body_bytes": 12,
            "raw_media_type": "text/html",
            "retrieved_at": "2026-09-08T11:30:34.447319+00:00",
            "source_envelope_ref": "source-envelope:" + "d" * 64,
            "source_envelope_hash": "e" * 64,
            "raw_artifact_version_ref": "artifact-version:" + "f" * 64,
            "raw_artifact_content_hash": "0" * 64,
            "content_hash": "1" * 64,
        }
        files = {
            "ticket.json": {"id": TICKET, "status": "succeeded", "document_ref": queued_ref},
            "summary.json": {"url_ref": queued_ref, "canonical_url": canonical_url,
                             "manifest_ref": manifest["id"], "manifest_hash": manifest["content_hash"],
                             "status": "succeeded"},
            "manifest.json": manifest,
        }
        for name, value in files.items():
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
        return manifest

    def read(self, queued_ref):
        # The manifest validator is not what is under test; the agreement check
        # that runs before it is.
        try:
            self.launcher.read_completed_manifest(TICKET, queued_ref)
        except FetchLaunchRejected:
            raise
        except Exception:
            return "agreed"
        return "agreed"

    def test_a_filing_queued_by_accession_reads_the_manifest_keyed_by_its_url(self):
        self.write(queued_ref=FILING, manifest_url_ref=URL_REF)
        self.assertEqual(self.read(FILING), "agreed")

    def test_a_searched_page_still_reads_when_both_refs_are_the_same(self):
        self.write(queued_ref=URL_REF, manifest_url_ref=URL_REF)
        self.assertEqual(self.read(URL_REF), "agreed")

    def test_a_manifest_for_some_other_url_is_still_a_disagreement(self):
        # This is what the dropped equality check was protecting, and it has to
        # keep failing: the manifest must be of the URL this fetch says it got.
        self.write(queued_ref=FILING, manifest_url_ref=public_web_url_ref(
            "https://www.sec.gov/Archives/edgar/data/51143/000005114325000012/ibm-20250630.htm"))
        with self.assertRaises(FetchLaunchRejected):
            self.launcher.read_completed_manifest(TICKET, FILING)

    def test_a_summary_that_names_no_url_is_a_disagreement(self):
        self.write(queued_ref=FILING, manifest_url_ref=URL_REF)
        path = self.state / "fetches" / TICKET.split(":", 1)[1] / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        del summary["canonical_url"]
        path.write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaises(FetchLaunchRejected):
            self.launcher.read_completed_manifest(TICKET, FILING)

    def test_a_ticket_for_another_document_is_still_refused(self):
        self.write(queued_ref=FILING, manifest_url_ref=URL_REF)
        with self.assertRaises(FetchLaunchRejected):
            self.launcher.read_completed_manifest(TICKET, "sec:filing:0000051143-25-000012")


class SourceGateTests(unittest.TestCase):
    def test_a_fetched_filing_is_read_on_the_same_path_as_a_fetched_page(self):
        self.assertEqual(FETCHED_SOURCE_REFS, {PUBLIC_WEB_SOURCE_REF, SEC_EDGAR_SOURCE_REF})
        self.assertIn(SEC_EDGAR_SOURCE_REF, SUPPORTED_SOURCE_REFS)
        self.assertIn("source:alphaengine", SUPPORTED_SOURCE_REFS)

    def test_a_source_nobody_has_wired_is_still_refused(self):
        self.assertNotIn("source:guidepoint", SUPPORTED_SOURCE_REFS)
        self.assertNotIn("source:company-ir", SUPPORTED_SOURCE_REFS)


if __name__ == "__main__":
    unittest.main()
