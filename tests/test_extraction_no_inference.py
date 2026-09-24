"""2026-09-24b: an extracted statement restates its excerpt and infers nothing."""

from __future__ import annotations

import unittest

from dalton_core.document_extraction import NO_INFERENCE_INSTRUCTION, build_prompt


class NoInferencePromptTests(unittest.TestCase):
    def test_the_extraction_prompt_forbids_inference_beyond_the_excerpt(self) -> None:
        prompt = build_prompt({"company_ref": "company:ticker:epam", "company_label": "EPAM",
                               "document_ref": "d", "offset": 0, "end": 1,
                               "quotes": [{"quote_id": "q", "raw_text": "t"}]})
        self.assertIn(NO_INFERENCE_INSTRUCTION, prompt)
        self.assertIn("no inference", prompt)
        self.assertIn("never a reading the excerpt contradicts", prompt)

    def test_the_audited_sources_are_inside_the_support_check(self) -> None:
        # The three statements that said more than their span were AlphaEngine
        # documents on the legacy Core; the support check covers that source.
        from dalton_core.claim_support_verification import VERIFIED_SOURCE_REFS

        self.assertIn("source:alphaengine", VERIFIED_SOURCE_REFS)
        self.assertIn("source:sales-notes", VERIFIED_SOURCE_REFS)


if __name__ == "__main__":
    unittest.main()
