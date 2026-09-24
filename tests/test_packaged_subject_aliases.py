"""2026-09-24b: the packaged aliases for GOOGL and IBM, and which are brands."""

from __future__ import annotations

import unittest

from dalton_core.claim_subject import name_needles
from dalton_core.document_subject import (
    BRAND_NAMES,
    COMPANY_NAMES,
    document_names_subject,
    earnings_call_names_issuer,
)


class PackagedAliasTests(unittest.TestCase):
    def test_googl_and_ibm_carry_their_products_and_acquisitions(self) -> None:
        for name in ("GOOG", "Gemini", "YouTube"):
            self.assertIn(name, COMPANY_NAMES["GOOGL"])
        for name in ("Red Hat", "HashiCorp", "Confluent", "watsonx"):
            self.assertIn(name, COMPANY_NAMES["IBM"])

    def test_products_are_brands_but_a_share_class_is_the_issuer(self) -> None:
        for name in ("Gemini", "YouTube", "Red Hat", "HashiCorp", "Confluent", "watsonx"):
            self.assertIn(name, BRAND_NAMES)
        self.assertNotIn("GOOG", BRAND_NAMES)

    def test_no_executive_is_a_subject_alias(self) -> None:
        executives = {"jassy", "pichai", "zuckerberg", "nadella", "krishna", "sweet", "cook"}
        for names in COMPANY_NAMES.values():
            self.assertFalse({name.casefold() for name in names} & executives, names)

    def test_a_product_in_another_issuers_call_title_is_not_a_second_issuer(self) -> None:
        verdict = earnings_call_names_issuer(
            "IBM Q2 2026 Earnings Call: watsonx and Red Hat vs YouTube ads", "IBM")
        self.assertTrue(verdict["names_issuer"], verdict)

    def test_an_acquired_companys_own_call_is_not_its_acquirers(self) -> None:
        verdict = earnings_call_names_issuer("Confluent Q4 2025 Earnings Call", "IBM")
        self.assertFalse(verdict["names_issuer"], verdict)

    def test_the_new_aliases_name_the_subject_in_a_document(self) -> None:
        self.assertTrue(document_names_subject("Red Hat OpenShift grew again.", "IBM")["names_subject"])
        self.assertTrue(document_names_subject("YouTube ad revenue rose.", "GOOGL")["names_subject"])
        self.assertFalse(document_names_subject("Revenue was reduced; that hat.", "IBM")["names_subject"])

    def test_a_two_word_alias_is_one_needle_not_two_common_words(self) -> None:
        self.assertEqual(name_needles(["Red Hat"]), ["red hat"])
        self.assertNotIn("web", name_needles(["Amazon Web Services"]))
        self.assertIn("amazon", name_needles(["Amazon.com"]))
        self.assertIn("grid dynamics", name_needles(["Grid Dynamics Holdings"]))
        self.assertIn("meta", name_needles(["Meta Platforms"]))


if __name__ == "__main__":
    unittest.main()
