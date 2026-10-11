"""Qualitative sources retain their provenance without pretending to be numbers."""
import copy
import unittest
from dalton_core.initial_screen import parse_document_output
from dalton_core.product_contract import normalise_pack, tag_entry
from dalton_core.mission_deliverable import validate_section, MissionDeliverableError
from tests.synthetic_company import material_pack, strl_document


class QualitativeCitationTests(unittest.TestCase):
    def quote(self):
        pack=normalise_pack(material_pack());entry=tag_entry(pack,'P1')
        # A literal nonnumeric phrase from the same held synthetic passage.
        import re
        phrase=next(m.group().strip() for m in re.finditer(r'[A-Za-z][A-Za-z ,;-]{8,}',entry['text'])
                    if m.group().strip())
        return pack,entry,phrase

    def test_exact_qualitative_quote_preserves_body_source_and_raw_input(self):
        pack,entry,phrase=self.quote();doc=strl_document()
        doc['sections'][0].update(body=phrase,numbers=[{'tag':'P1','quote':phrase}],sources=[])
        before=copy.deepcopy((pack,doc));sections,findings=parse_document_output(doc,pack=pack)
        self.assertEqual(findings,[]);self.assertEqual((pack,doc),before)
        section=sections[0];self.assertEqual(section['body'],phrase);self.assertEqual(section['numbers'],[])
        self.assertIn(entry['ref'],[s['ref']for s in section['source_refs']])
        validate_section(section,resolve_passage=lambda ref:entry['text'])

    def test_qualitative_source_cannot_supply_unstated_body_amount(self):
        pack,entry,phrase=self.quote();doc=strl_document()
        doc['sections'][0].update(body='收入 987654 美元',numbers=[{'tag':'P1','quote':phrase}])
        sections,findings=parse_document_output(doc,pack=pack);self.assertEqual(findings,[])
        with self.assertRaises(MissionDeliverableError):validate_section(sections[0],resolve_passage=lambda ref:entry['text'])

    def test_made_up_qualitative_quote_remains_a_failure(self):
        pack,entry,phrase=self.quote();doc=strl_document()
        doc['sections'][0]['numbers']=[{'tag':'P1','quote':'This invented sentence is absent.'}]
        _,findings=parse_document_output(doc,pack=pack)
        self.assertIn('quote_not_in_passage',[f['code']for f in findings])

    def test_declared_passage_figure_must_be_in_its_selected_quote(self):
        pack,entry,phrase=self.quote();doc=strl_document()
        doc['sections'][0]['numbers']=[{'tag':'P1','quote':phrase,'figure':'987654'}]
        _,findings=parse_document_output(doc,pack=pack)
        self.assertIn('passage_figure_invalid',[f['code']for f in findings])

    def test_explicit_figure_is_checked_and_preserved_instead_of_ignored(self):
        pack,entry,phrase=self.quote();doc=strl_document()
        from dalton_core.mission_deliverable import value_tokens
        figure=value_tokens(entry['text'],include_small=True)[0]
        doc['sections'][0].update(body=figure,numbers=[{'tag':'P1','quote':entry['text'],'figure':figure}])
        sections,findings=parse_document_output(doc,pack=pack)
        self.assertEqual(findings,[]);self.assertEqual(sections[0]['numbers'][0]['text'],figure)
        validate_section(sections[0],resolve_passage=lambda ref:entry['text'])
