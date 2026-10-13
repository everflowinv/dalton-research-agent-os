"""Printing equivalents preserve exact sources and do not supply absent content."""
import copy
import unittest

from dalton_core.initial_screen import parse_document_output
from dalton_core.mission_deliverable import (
    MissionDeliverableError, pack_passage_resolver, unsourced_numbers,
    validate_derived_number, validate_section, value_tokens,
)
from dalton_core.product_contract import normalise_pack, passage_quote_matches
from tests.synthetic_company import material_pack, strl_document


class InitialScreenPrintEquivalenceTests(unittest.TestCase):
    def test_single_hyphen_line_wrap_in_two_distinct_passages(self):
        for original, quoted in (("Growth over 2019-\n2024 reached 24%.",
                                  "Growth over 2019-2024 reached 24%."),
                                 ("Issuer B expects a 3-\r\n  pt improvement.",
                                  "Issuer B expects a 3-pt improvement.")):
            with self.subTest(original=original):
                self.assertTrue(passage_quote_matches(quoted, original))
                self.assertTrue(passage_quote_matches(original, original))
                pack = normalise_pack(material_pack())
                pack['tags']['P99'] = {'ref': 'doc-passage:synthetic', 'text': original}
                doc = strl_document()
                doc['sections'][0]['numbers'] = [{'tag': 'P99', 'quote': quoted}]
                before = copy.deepcopy((pack, doc))
                sections, findings = parse_document_output(doc, pack=pack)
                self.assertEqual(findings, [])
                self.assertEqual((pack, doc), before)
                self.assertEqual(sections[0]['numbers'][0]['text'], quoted)

    def test_changed_digit_hyphen_minus_and_paragraph_join_are_not_equivalent(self):
        for quoted, original in (("2019-2025", "2019-\n2024"),
                                 ("2019–2024", "2019-\n2024"),
                                 ("3pt", "3-\npt"),
                                 ("-123", "-\n123"),
                                 ("3-pt", "3-\n\npt"),
                                 ("revenue fell", "revenue grew")):
            with self.subTest(quoted=quoted, original=original):
                self.assertFalse(passage_quote_matches(quoted, original))

    def test_quote_equivalence_does_not_change_raw_passage_hash_authority(self):
        raw = "2019-\n2024 grew 24%."
        from dalton_core.evidence_index import make_passage_ref
        ref = make_passage_ref('doc:synthetic', 0, len(raw), raw)
        self.assertEqual(pack_passage_resolver(None, {ref: raw})(ref), raw)
        self.assertIsNone(pack_passage_resolver(None, {ref: raw.replace('-\n', '-')})(ref))

    def test_explicit_amount_years_are_values_but_periods_stay_periods(self):
        for text in ('收入2006亿美元', '$2006', '2006 million USD', '2006%',
                     '收入1999欧元', '2019人民币'):
            with self.subTest(text=text):
                self.assertTrue(value_tokens(text, include_small=True))
        for text in ('2006', 'FY2006', '2006年', '2006-01-01', '2006Q1', 'FY2006 收入'):
            with self.subTest(text=text):
                self.assertEqual(value_tokens(text, include_small=True), [])
        self.assertEqual(unsourced_numbers('收入2006亿美元', []), ['2006'])
        self.assertEqual(value_tokens('收入2006.5亿美元'), ['2006.5'])
        self.assertEqual(value_tokens('收入20060美元'), ['20060'])

    def test_amount_result_requires_bound_exact_arithmetic(self):
        source = {'text': '$200.6 billion', 'number_ref': 'pack-json:/source'}
        result = {'text': '收入2006亿美元',
                  'derivation': {'expression': '200.6*10', 'result': '2006'}}
        validate_derived_number(result, [source])
        validate_section({'title': '换算', 'body': '收入2006亿美元',
                          'numbers': [source, result]}, resolve_number=lambda _: '$200.6 billion')
        for bad in ({**result, 'derivation': {'expression': '200.7*10', 'result': '2006'}},
                    {**result, 'text': '收入2007亿美元'},
                    {**result, 'derivation': {'expression': '200.6*11', 'result': '2006'}}):
            with self.subTest(bad=bad), self.assertRaises(MissionDeliverableError):
                validate_derived_number(bad, [source])

    def test_absent_expression_and_unlabelled_estimate_still_refuse(self):
        sources = [{'text': '981.67', 'number_ref': 'pack-json:/a'},
                   {'text': '948.77', 'number_ref': 'pack-json:/b'}]
        with self.assertRaises(MissionDeliverableError):
            validate_section({'title': '比较', 'body': '(981.67/948.77-1)*100',
                              'numbers': sources}, resolve_number={'pack-json:/a': '981.67',
                                                                  'pack-json:/b': '948.77'}.get)
        with self.assertRaises(MissionDeliverableError):
            validate_section({'title': '估计', 'body': '估值为28倍',
                              'numbers': [{'text': '28倍', 'estimate': {'assumption_ref': 'assumption:synthetic'}}]})
