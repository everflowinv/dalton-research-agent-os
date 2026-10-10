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

        for source_ref in ("source:alphaengine", "source:sales-notes", "source:web-search",
                           "source:public-web", "source:sec-edgar", "source:guidepoint"):
            self.assertIn(source_ref, VERIFIED_SOURCE_REFS)


class StagedRationaleTests(unittest.TestCase):
    """2026-09-29: the review rationale says what held a statement, true for its source."""

    def test_holds_are_counted_by_what_held_them(self) -> None:
        from dalton_core.document_extraction_cli import staged_rationale

        held = [{"reason": "held for human review: the independent support check found the cited "
                           "text does not support the statement"},
                {"reason": "held for human review: the cited span never names the subject"}]
        text = staged_rationale("source:guidepoint", [{"status": "admitted"}], held, [])
        self.assertIn("1 qualitative claim(s) admitted (each admitted statement passed the "
                      "independent support check)", text)
        self.assertIn("1: the independent support check did not pass", text)
        self.assertIn("1: the cited span does not name the company", text)
        self.assertNotIn(" or the independent support check did not pass", text)

    def test_a_source_the_check_does_not_cover_says_so(self) -> None:
        from dalton_core.document_extraction_cli import staged_rationale

        held = [{"reason": "held for human review: the cited span never names the subject"}]
        text = staged_rationale("source:company-wiki", [], held, [{"status": "rejected"}])
        self.assertIn("source:company-wiki is not covered by the independent support check", text)
        self.assertNotIn("support check did not pass", text)
        self.assertIn("1 suggestion(s) refused", text)


if __name__ == "__main__":
    unittest.main()
