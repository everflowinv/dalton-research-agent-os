"""2026-09-24b: the admission span check matches names as the retirement rule does.

A substring test found Amazon in every "website" and "laws" once "AWS" and
"Amazon Web Services" were aliases, and would have found IBM (via "Red Hat")
in "reduced".  Admission now uses ``claim_subject.text_names_word``: a Latin
name is a whole word and never a weak token; a Chinese name is a substring.
"""

from __future__ import annotations

import unittest

from dalton_core.claim_subject import (
    document_is_subjects,
    name_needles,
    span_names_subject_for_admission,
    text_names_word,
)

AMAZON = name_needles(["Amazon", "Amazon.com", "AWS", "Amazon Web Services", "亚马逊", "AMZN"])
IBM = name_needles(["IBM", "Red Hat", "HashiCorp", "watsonx", "国际商业机器"])


def admitted(span, needles):
    return span_names_subject_for_admission(span=span, needles=needles, document_is_own=False)


class AdmissionWholeWordTests(unittest.TestCase):
    def test_a_name_inside_another_word_is_not_the_company(self) -> None:
        self.assertFalse(admitted("See the website for the laws that apply.", AMAZON))
        self.assertFalse(admitted("Margins were reduced; that was expected.", IBM))
        self.assertFalse(admitted("An amazonian rainforest fund.", AMAZON))

    def test_the_name_itself_still_admits_including_possessives(self) -> None:
        self.assertTrue(admitted("Amazon's AWS backlog is nearly $500 billion.", AMAZON))
        self.assertTrue(admitted("For most of 2025, AWS' growth rate was in the 20s.", AMAZON))
        self.assertTrue(admitted("Red Hat OpenShift grew again.", IBM))
        self.assertTrue(admitted("watsonx bookings doubled.", IBM))

    def test_a_chinese_name_is_still_a_substring_even_at_two_characters(self) -> None:
        self.assertTrue(admitted("亚马逊云科技的积压订单继续增长", AMAZON))
        self.assertTrue(text_names_word("谷歌发布了新模型", name_needles(["谷歌"])))

    def test_the_subjects_own_document_is_judged_by_whole_words_too(self) -> None:
        meta = name_needles(["Meta Platforms", "META"])
        self.assertFalse(document_is_subjects(title="Metadata and metals weekly", needles=meta))
        self.assertTrue(document_is_subjects(title="Meta Q2 2026 Earnings Call", needles=meta))
        self.assertFalse(document_is_subjects(text="Metals digest: " + "x" * 500, needles=meta))


if __name__ == "__main__":
    unittest.main()
