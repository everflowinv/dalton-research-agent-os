"""Reading config reaches exact source windows; render bounds are explicit."""
import tempfile
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
                      {"max_document_chars": 1000}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_reading_limits({"reading_limits": value})

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
