"""Reading config reaches exact source windows; render bounds are explicit."""
import tempfile
import gzip
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_reading_limits import resolve_reading_limits
from dalton_core.public_web_extraction_source import render_public_web_text
from dalton_core.store import content_hash
from tests.test_document_extraction import ExtractionHarness


class ReadingLimitsTests(unittest.TestCase):
    def test_closed_positive_geometry(self):
        for value in ([], {"bad": 1}, {"window_chars": True},
                      {"max_pdf_pages": 0}, {"quote_chars": 13000},
                      {"max_decompressed_bytes": False}, {"max_decompressed_bytes": 0},
                      {"max_document_chars": 1000}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_reading_limits({"reading_limits": value})

    def test_gzip_limit_flows_through_real_public_web_context_and_reread(self):
        from tests.test_document_extraction_automation import WebAdmissionTests, AUTOMATION
        from tests.test_public_web_fetch_lane import BODY
        from dalton_core.document_extraction import DocumentExtractionService
        from dalton_core.document_extraction_cli import ExtractionHost
        from dalton_core.public_web_extraction_source import PublicWebSourceError

        fixture = WebAdmissionTests()
        self.addCleanup(fixture.doCleanups)
        with patch("tests.test_public_web_fetch_lane.BODY", gzip.compress(BODY, mtime=0)):
            fixture.setUp()  # Real fetch child/receipt/mission; no external call.
        host = ExtractionHost(
            state_dir=fixture.state, spool_dir=fixture.state / "spool",
            scheduler_db=fixture.state / "scheduler.sqlite", connector_governance=None,
            web_fetch_governance=fixture.governance_path, model_config=None)
        self.addCleanup(host.close)
        service = DocumentExtractionService(host)
        params = {"review_id": fixture.review["review_id"],
                  "expected_review_hash": content_hash(fixture.review),
                  "offset": 0, "actor_ref": AUTOMATION}
        host._document_extraction_model_config = {
            "reading_limits": {"max_decompressed_bytes": len(BODY) - 1}}
        with self.assertRaisesRegex(PublicWebSourceError, "decompressed byte limit"):
            service._source_context(**params)
        host._document_extraction_model_config = {
            "reading_limits": {"max_decompressed_bytes": len(BODY)}}
        context = service._source_context(**params)
        self.assertEqual(context["reading_limits"]["max_decompressed_bytes"], len(BODY))
        self.assertEqual(context["source_renderer"], "gzip:0.1|html-visible-blocks:0.1")
        original = service._document_text(context)
        self.assertTrue(original)
        # Later owner configuration cannot alter the bound reread geometry.
        host._document_extraction_model_config["reading_limits"]["max_decompressed_bytes"] = 1
        self.assertEqual(service._document_text(context), original)
        narrowed = {**context, "reading_limits": {
            **context["reading_limits"], "max_decompressed_bytes": len(BODY) - 1}}
        with self.assertRaisesRegex(PublicWebSourceError, "decompressed byte limit"):
            service._document_text(narrowed)

    def test_complete_large_render_after_configured_limit_increase(self):
        raw = b"a" * 610000 + b" revenue recognition policy at the end"
        legacy = render_public_web_text(raw, raw_media_type="text/plain")
        expanded = render_public_web_text(raw, raw_media_type="text/plain",
                                          max_source_chars=1000000)
        self.assertTrue(legacy["truncated"])
        self.assertFalse(expanded["truncated"])
        self.assertEqual(expanded["text"].encode(), raw)

    def test_service_windows_and_quotes_consume_config_and_rekey_context(self):
        with tempfile.TemporaryDirectory() as temp:
            h = ExtractionHarness(Path(temp))
            self.addCleanup(h.close)
            before = h.service._source_context(**h.params)
            h.writer._document_extraction_model_config = {"reading_limits": {
                "window_chars": 6000, "quote_chars": 600,
                "max_document_chars": 1000000, "max_pdf_pages": 1000}}
            first = h.service._source_context(**h.params)
            second = h.service._source_context(**{**h.params, "offset": 6000})
            self.assertEqual(first["end"], 6000)
            self.assertEqual(first["quotes"][0]["source_end"], 600)
            self.assertEqual(first["next_offset"], second["offset"])
            self.assertNotEqual(content_hash(before), content_hash(first))
            self.assertEqual(h.service._document_text(first)[:600], first["quotes"][0]["raw_text"])
            with self.assertRaisesRegex(Exception, "differs from the bound source context"):
                h.service._document_text({**first, "source_content_hash": "0" * 64})
            # Config must also reach receipt verification, not just slicing.
            with patch("dalton_core.document_extraction.verified_source", wraps=__import__(
                    "dalton_core.document_extraction", fromlist=["verified_source"]).verified_source) as verify:
                h.service._source_context(**h.params)
                self.assertEqual(verify.call_args.kwargs["max_document_chars"], 1000000)


if __name__ == "__main__":
    unittest.main()
