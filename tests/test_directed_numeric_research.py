import unittest
from dalton_core.directed_numeric_research import normalize_targets,coverage,DirectedNumericError

class TypedContractTests(unittest.TestCase):
    def setUp(self):
        self.target=normalize_targets([{'target_ref':'target:revenue','metric_ref':'metric:revenue',
            'metric_definition':{'label':'Total revenue','meaning':'Consolidated issuer total revenue excluding segment subtotals'},
            'period':'FY2025','unit':'currency','currency':'USD','basis':'gaap-reported'}])[0]
    def checked(self,targets,figures):return coverage(targets,figures,company_ref='company:syn',document_ref='doc:syn',source_manifest_hash='c'*64)
    def figure(self):return {**self.target,'figure_id':'figure:syn','content_hash':'a'*64,'company_ref':'company:syn','document_ref':'doc:syn','source_manifest_hash':'c'*64}
    def test_question_scoped_definition_needs_no_global_discovery(self):
        self.assertEqual(normalize_targets([self.target]),[self.target])
        self.assertEqual(self.checked([self.target],[self.figure()])['status'],'completed')
    def test_definition_hash_and_meaning_cannot_drift(self):
        for changes in ({'metric_definition_hash':'b'*64},{'metric_definition':{'label':'Segment','meaning':'Segment revenue'}}):
            with self.subTest(changes=changes),self.assertRaises(DirectedNumericError):normalize_targets([{**self.target,**changes}])
    def test_one_metric_cannot_have_incompatible_definition(self):
        second={**self.target,'target_ref':'target:second','period':'FY2026','metric_definition':{'label':'Revenue','meaning':'Segment revenue'}}
        second.pop('metric_definition_hash')
        with self.assertRaises(DirectedNumericError):normalize_targets([self.target,second])
    def test_duplicate_identity_refused(self):
        with self.assertRaises(DirectedNumericError):normalize_targets([self.target,self.target])
    def test_wrong_source_company_definition_missing_refs_or_dimensions_cannot_complete(self):
        for changes in ({'company_ref':'company:other'},{'document_ref':'doc:other'},{'source_manifest_hash':'d'*64},
            {'figure_id':None},{'content_hash':None},{'metric_definition_hash':'b'*64},{'period':'FY2024'},
            {'unit':'count'},{'currency':'EUR'},{'basis':'management-reported'}):
            with self.subTest(changes=changes):self.assertEqual(self.checked([self.target],[{**self.figure(),**changes}])['status'],'waiting_evidence')
    def test_all_targets_required(self):
        second={**self.target,'target_ref':'target:fy2026','period':'FY2026'}
        result=self.checked([self.target,second],[self.figure()]);self.assertEqual(result['status'],'waiting_evidence')
        self.assertEqual(result['pending_targets'][0]['target_ref'],'target:fy2026')
