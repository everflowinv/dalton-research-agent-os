"""S2: acquiring a Guidepoint excerpt spends no call and proves its own bytes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_authority_port import ConnectorCompletionReceiptReader
from dalton_core.guidepoint_acquisition import (
    GUIDEPOINT_SOURCE_REF,
    GuidepointAcquisitionConflict,
    GuidepointAcquisitionError,
    acquire_guidepoint_excerpt,
    validate_guidepoint_excerpt_acquisition_manifest,
    verified_guidepoint_source,
)
from dalton_core.guidepoint_search import (
    QUOTE_POLICY,
    FakeGuidepointHandle,
    guidepoint_excerpt_records,
    verify_guidepoint_quote,
)
from dalton_core.store import content_hash

from tests.test_guidepoint_search_lane import ROWS, SPEC, Harness

ROOT = Path(__file__).resolve().parents[1]


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.harness = Harness(self.root, FakeGuidepointHandle(ROWS))
        self.addCleanup(self.harness.close)
        self.receipt = self.harness.search.search(self.harness.search.build_request(SPEC))
        self.assertEqual(self.receipt["outcome"], "succeeded")
        self.reader = ConnectorCompletionReceiptReader(
            connectors=self.harness.connectors, observability=self.harness.observability
        )

    def acquire(self, ordinal: int = 1):
        return acquire_guidepoint_excerpt(
            core=self.harness.core,
            spool=self.harness.spool,
            receipt_reader=self.reader,
            source_envelope_ref=self.receipt["source_envelope_ref"],
            document_ref=self.receipt["document_refs"][ordinal - 1],
        )

    def test_the_manifest_binds_the_search_and_spends_no_provider_call(self) -> None:
        manifest = self.acquire()
        self.assertEqual(manifest["provider_calls"], 0)
        self.assertEqual(manifest["source_ref"], GUIDEPOINT_SOURCE_REF)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["excerpt_ordinal"], 1)
        self.assertEqual(
            manifest["connector_invocation_ref"], self.receipt["connector_invocation_ref"]
        )
        self.assertEqual(manifest["source_envelope_ref"], self.receipt["source_envelope_ref"])
        self.assertEqual(manifest["quote_policy"], dict(QUOTE_POLICY))
        self.assertEqual(manifest["transcript_name"], ROWS[0]["transcript_name"])
        self.assertEqual(manifest["reference_url"], ROWS[0]["reference_url"])
        # Acquiring did not spend a second Guidepoint call.
        self.assertEqual(len(self.harness.handle.calls), 1)
        # The excerpt is in the spool under the hash the manifest declares.
        stored = self.harness.spool.read_object(manifest["declared_content_sha256"])
        expected = guidepoint_excerpt_records({"data": ROWS})[0]["excerpt_text"]
        self.assertEqual(stored.decode("utf-8"), expected)
        self.assertEqual(manifest["content_chars"], len(expected))

    def test_the_same_excerpt_acquired_twice_is_the_same_manifest(self) -> None:
        self.assertEqual(self.acquire(), self.acquire())

    def test_verification_re_derives_the_text_from_the_raw_bytes(self) -> None:
        manifest = self.acquire(2)
        checked, text = verified_guidepoint_source(
            self.harness.core, self.harness.spool, manifest, self.reader
        )
        self.assertEqual(checked, manifest)
        self.assertIn(ROWS[1]["answer"], text)
        # And the licence gate reads the same excerpt the verification returned.
        excerpt = guidepoint_excerpt_records({"data": ROWS})[1]
        self.assertEqual(excerpt["excerpt_text"], text)
        self.assertEqual(
            verify_guidepoint_quote("the hours came down first", excerpt=excerpt)["word_count"], 5
        )

    def test_an_excerpt_the_search_did_not_return_cannot_be_acquired(self) -> None:
        with self.assertRaises(GuidepointAcquisitionError):
            acquire_guidepoint_excerpt(
                core=self.harness.core, spool=self.harness.spool, receipt_reader=self.reader,
                source_envelope_ref=self.receipt["source_envelope_ref"],
                document_ref="guidepoint-excerpt:sha256:" + "0" * 64,
            )
        with self.assertRaises(GuidepointAcquisitionError):
            acquire_guidepoint_excerpt(
                core=self.harness.core, spool=self.harness.spool, receipt_reader=self.reader,
                source_envelope_ref=self.receipt["source_envelope_ref"],
                document_ref="alphaengine-doc:1234",
            )

    def test_a_manifest_that_claims_a_provider_call_is_refused(self) -> None:
        manifest = dict(self.acquire())
        manifest.pop("content_hash")
        manifest["provider_calls"] = 1
        manifest["content_hash"] = content_hash(manifest)
        with self.assertRaises(GuidepointAcquisitionError):
            validate_guidepoint_excerpt_acquisition_manifest(manifest)

    def test_a_manifest_cannot_loosen_the_verbatim_licence(self) -> None:
        manifest = dict(self.acquire())
        manifest.pop("content_hash")
        manifest["quote_policy"] = {"max_verbatim_words": 500}
        manifest["content_hash"] = content_hash(manifest)
        with self.assertRaises(GuidepointAcquisitionError):
            validate_guidepoint_excerpt_acquisition_manifest(manifest)

    def test_a_manifest_pointing_at_another_excerpt_s_text_is_refused(self) -> None:
        first = self.acquire(1)
        second = self.acquire(2)
        forged = dict(first)
        forged.pop("content_hash")
        forged["excerpt_object"] = second["excerpt_object"]
        forged["declared_content_sha256"] = second["declared_content_sha256"]
        forged["content_hash"] = content_hash(forged)
        with self.assertRaises(GuidepointAcquisitionConflict):
            verified_guidepoint_source(
                self.harness.core, self.harness.spool, forged, self.reader
            )

    def test_a_manifest_whose_ordinal_moved_is_refused(self) -> None:
        manifest = dict(self.acquire(1))
        manifest.pop("content_hash")
        manifest["excerpt_ordinal"] = 2
        manifest["content_hash"] = content_hash(manifest)
        with self.assertRaises(GuidepointAcquisitionConflict):
            verified_guidepoint_source(
                self.harness.core, self.harness.spool, manifest, self.reader
            )

    def test_a_manifest_whose_receipt_hash_drifted_is_refused(self) -> None:
        manifest = dict(self.acquire())
        manifest.pop("content_hash")
        manifest["physical_attempt_hash"] = "0" * 64
        manifest["content_hash"] = content_hash(manifest)
        with self.assertRaises(GuidepointAcquisitionConflict):
            verified_guidepoint_source(
                self.harness.core, self.harness.spool, manifest, self.reader
            )

    def test_the_manifest_shape_is_closed(self) -> None:
        manifest = dict(self.acquire())
        with self.assertRaises(GuidepointAcquisitionError):
            validate_guidepoint_excerpt_acquisition_manifest({**manifest, "extra": 1})
        stripped = {k: v for k, v in manifest.items() if k != "respondent"}
        with self.assertRaises(GuidepointAcquisitionError):
            validate_guidepoint_excerpt_acquisition_manifest(stripped)


class LiveFramingTests(unittest.TestCase):
    """The whole path over the framing the live proxy actually uses."""

    def test_an_sse_framed_search_acquires_and_verifies(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        harness = Harness(root, FakeGuidepointHandle(ROWS, sse=True))
        self.addCleanup(harness.close)
        receipt = harness.search.search(harness.search.build_request(SPEC))
        self.assertEqual(receipt["outcome"], "succeeded")
        reader = ConnectorCompletionReceiptReader(
            connectors=harness.connectors, observability=harness.observability
        )
        envelope = harness.search.receipts.get_source_envelope(
            receipt["source_envelope_ref"]
        )
        self.assertTrue(
            harness.spool.read_object(envelope["raw_response_hash"]).startswith(
                b"event: message"
            )
        )
        manifest = acquire_guidepoint_excerpt(
            core=harness.core, spool=harness.spool, receipt_reader=reader,
            source_envelope_ref=receipt["source_envelope_ref"],
            document_ref=receipt["document_refs"][0],
        )
        _, text = verified_guidepoint_source(
            harness.core, harness.spool, manifest, reader
        )
        self.assertIn(ROWS[0]["answer"], text)


class ExtractionDispatchTests(unittest.TestCase):
    def test_the_alphaengine_validator_cannot_stand_in_for_this_manifest(self) -> None:
        # Named here because it is the reason a second verified_* exists: a
        # Guidepoint excerpt has no pages, so verified_source would refuse it.
        from dalton_core.alphaengine_document_acquisition import (
            AlphaEngineDocumentAcquisitionError,
            validate_alphaengine_document_acquisition_manifest,
        )

        sample = json.loads(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "document_ref": "guidepoint-excerpt:sha256:" + "0" * 64,
                }
            )
        )
        with self.assertRaises(AlphaEngineDocumentAcquisitionError):
            validate_alphaengine_document_acquisition_manifest(sample)


if __name__ == "__main__":
    unittest.main()
