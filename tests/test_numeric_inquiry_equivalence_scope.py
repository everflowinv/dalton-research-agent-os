"""Date tokens are not answer identities; typed requirements govern new reads."""
import copy
import unittest
from unittest.mock import patch
from dalton_core.document_numeric_inquiry_equivalence import admission_match, RULE_REF
from dalton_core.document_inquiry_equivalence import inquiry_signature_for, signatures_equivalent
from dalton_core.directed_numeric_research import normalize_targets
from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending
from dalton_core.mission_research_task_lane import ResearchTaskCoordinator
from dalton_core.document_numeric_completion import completed_numeric_admission
from tests import test_directed_numeric_workflow as workflow
from tests import test_numeric_waiting_target_revision as revision_fixture


def inquiry(company='company:synthetic', metric='metric:repurchases', question='截至 2025 年 12 月 31 日的回购金额？'):
    return {'company_ref':company,'question':question,'directed_document':{
        'strategy_version':'directed-document-numeric:0.1','document_ref':'source:filing',
        'document_version_hash':'d'*64,'numeric_targets':normalize_targets([{
            'target_ref':'target:one','metric_ref':metric,
            'metric_definition':{'label':'Repurchases','meaning':'Issuer consolidated gross share repurchases'},
            'period':'year ended December 31, 2025','unit':'currency','currency':'USD','basis':'gaap-reported'}])}}


class NumericInquiryScopeTests(unittest.TestCase):
    def test_same_dates_and_horizons_never_match_different_metrics_for_two_companies(self):
        for company in ('company:synthetic-a','company:synthetic-b'):
            a=inquiry(company);b=inquiry(company,'metric:deferred-revenue','截至 2025 年 12 月 31 日的递延收入？')
            b['directed_document']['numeric_targets'][0].pop('metric_definition_hash')
            self.assertEqual(signatures_equivalent(inquiry_signature_for(company,a),inquiry_signature_for(company,b)), 'figure:12,31')
            self.assertIsNone(admission_match(company,a,b))

    def test_equivalent_typed_scope_ignores_question_rewording_order_and_target_handles(self):
        a=inquiry();b=copy.deepcopy(a);b['question']='What were issuer repurchases in the year ended December 31, 2025?'
        b['directed_document']['numeric_targets'][0]['target_ref']='target:another-local-handle'
        self.assertEqual(admission_match(a['company_ref'],a,b),'numeric_targets')

    def test_period_metric_currency_unit_basis_and_definition_each_bound(self):
        a=inquiry()
        for key,value in (('period','year ended December 31, 2024'),('metric_ref','metric:other'),
                          ('currency','EUR'),('basis','non-gaap-reported')):
            b=copy.deepcopy(a);t=b['directed_document']['numeric_targets'][0];t[key]=value;t.pop('metric_definition_hash')
            with self.subTest(key=key):self.assertIsNone(admission_match(a['company_ref'],a,b))
        b=copy.deepcopy(a);t=b['directed_document']['numeric_targets'][0]
        t.update(unit='count',currency=None);t.pop('metric_definition_hash')
        self.assertIsNone(admission_match(a['company_ref'],a,b))
        b=copy.deepcopy(a);t=b['directed_document']['numeric_targets'][0]
        t['metric_definition']['meaning']='Issuer net share repurchases';t.pop('metric_definition_hash')
        self.assertIsNone(admission_match(a['company_ref'],a,b))

    def test_source_version_document_and_company_each_bound(self):
        a=inquiry()
        for key,value in (('document_ref','source:other'),('document_version_hash','e'*64)):
            b=copy.deepcopy(a);b['directed_document'][key]=value
            self.assertIsNone(admission_match(a['company_ref'],a,b))
        b=copy.deepcopy(a);b['company_ref']='company:other'
        self.assertIsNone(admission_match(a['company_ref'],a,b))

    def test_mixed_typed_untyped_never_matches_only_common_number(self):
        a=inquiry(question='At 24 months, how much revenue backlog is recognized?')
        b=copy.deepcopy(a);b['question']='What were cloud RPO and the 24 month recognition percentage?'
        b['directed_document'].pop('numeric_targets');b['directed_document']['strategy_version']='directed-document:0.1'
        self.assertIsNone(admission_match(a['company_ref'],a,b))
        b['question']=a['question']
        self.assertEqual(admission_match(a['company_ref'],a,b),'question')

    def test_invalid_or_forged_target_never_falls_back_to_question(self):
        a=inquiry()
        for mode in ('empty','unknown','digest','duplicate'):
            b=copy.deepcopy(a);targets=b['directed_document']['numeric_targets']
            if mode=='empty':targets.clear()
            if mode=='unknown':targets[0]['new_qualifier']='segment'
            if mode=='digest':targets[0]['metric_definition_hash']='0'*64
            if mode=='duplicate':targets.append(copy.deepcopy(targets[0]))
            with self.subTest(mode=mode),self.assertRaises(ValueError):admission_match(a['company_ref'],a,b)

    def test_untyped_date_masking_preserves_money_and_v1_byte_behavior(self):
        a=inquiry();b=inquiry(question='截至 2025 年 12 月 31 日的债务是多少？')
        for i in (a,b):i['directed_document'].pop('numeric_targets')
        old=signatures_equivalent(inquiry_signature_for(a['company_ref'],a),inquiry_signature_for(a['company_ref'],b))
        self.assertEqual(old,'figure:12,31');self.assertIsNone(admission_match(a['company_ref'],a,b))
        for date in ('December 31, 2025','31 December 2025','2025/12/31','2025年12月31日'):
            a['question']=date+' repurchase amount?';b['question']=date+' deferred revenue balance?'
            with self.subTest(date=date):self.assertIsNone(admission_match(a['company_ref'],a,b))
        a['question']='2025-12-31 disclosed $970 million?';b['question']='2024-12-31 what does 9.7 亿 mean?'
        self.assertEqual(admission_match(a['company_ref'],a,b),'figure:97')
        self.assertEqual(signatures_equivalent(inquiry_signature_for(a['company_ref'],inquiry()),inquiry_signature_for(a['company_ref'],inquiry(question='截至 2025 年 12 月 31 日债务？'))),'figure:12,31')

    def fixture(self,complete=False):
        h=workflow.NumericWorkflowTests('runTest');self.addCleanup(h.doCleanups)
        f,a,parent,ex,p,v=h.fixture()
        if complete:h.drive(ex,parent)
        return h,f,a,parent,p,v

    def check_scope(self,a,parent,inquiry):
        return a.equivalent_open_admission({'company_ref':parent['company_ref'],'mission_ref':parent['mission_ref'],'planner_inquiry':inquiry})

    def test_real_pending_duplicate_rewording_blocks_without_new_work_or_calls(self):
        h,f,a,parent,p,v=self.fixture();new=copy.deepcopy(parent['planner_inquiry']);new['question']='Reworded request with no numeric token.'
        before=f.store.connection.total_changes
        self.assertEqual(self.check_scope(a,parent,new)['state'],'in_flight')
        self.assertEqual(f.store.connection.total_changes,before);self.assertEqual((p.calls,v.calls),(0,0))

    def test_real_completed_answer_only_blocks_its_complete_typed_scope(self):
        h,f,a,parent,p,v=self.fixture(complete=True);new=copy.deepcopy(parent['planner_inquiry']);new['question']='A synonymous exact target request.'
        result=self.check_scope(a,parent,new)
        self.assertEqual(result['state'],'recently_answered');self.assertEqual(result['rule_ref'],RULE_REF)
        self.assertTrue(completed_numeric_admission(f.store.connection,parent['id']))
        new['directed_document']['numeric_targets'][0]['metric_definition']['meaning']='An unrelated metric scope'
        new['directed_document']['numeric_targets'][0].pop('metric_definition_hash')
        self.assertIsNone(self.check_scope(a,parent,new));self.assertEqual((p.calls,v.calls),(1,1))

    def test_real_complete_formal_invalid_hash_is_not_an_answer(self):
        h,f,a,parent,p,v=self.fixture(complete=True)
        f.store.connection.execute('DROP TRIGGER mission_document_numeric_results_no_update')
        f.store.connection.execute('UPDATE mission_document_numeric_results SET content_hash=? WHERE admission_ref=?',('0'*64,parent['id']))
        with self.assertRaises(ValueError):self.check_scope(a,parent,copy.deepcopy(parent['planner_inquiry']))
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_different_typed_scope_admits_normally_without_paid_work_and_legacy_replay_identical(self):
        h,f,a,parent,p,v=self.fixture();old=tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone())
        new=copy.deepcopy(parent['planner_inquiry']);t=new['directed_document']['numeric_targets'][0]
        t['metric_ref']='metric:other-financial-fact';t.pop('metric_definition_hash')
        new['directed_document']['numeric_targets']=normalize_targets(new['directed_document']['numeric_targets'])
        fixture=revision_fixture.NumericWaitingRevisionTests('runTest')
        current=fixture.admit(h,f,a,parent,new,tag='distinct-target-scope')
        self.assertEqual(current['status'],'fresh');self.assertIsNone(a.admission(current['admission_ref'])['numeric_authority'].get('target_revision_proof'))
        self.assertEqual(a.resolve_for_execution(parent['id']),parent)
        self.assertEqual(old,tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone()))
        self.assertEqual((p.calls,v.calls),(0,0))

    def test_free_admission_wake_is_static_not_a_new_execution_grant(self):
        h,f,a,parent,p,v=self.fixture()
        coordinator=ResearchTaskCoordinator(store=f.store,launcher=None)
        sig=coordinator._signature('plan:test');self.assertEqual(sig['inquiry_equivalence'],RULE_REF)
        self.assertEqual(sig,coordinator._signature('plan:test'));self.assertEqual((p.calls,v.calls),(0,0))

    def test_new_revision_cannot_borrow_another_question_via_calendar_or_horizon(self):
        fixture=revision_fixture.NumericWaitingRevisionTests('runTest');self.addCleanup(fixture.doCleanups)
        h,f,a,parent,result,p,v=fixture.fixture()
        new=fixture.inquiry(parent)
        new['question']='Different metric at 24 months and December 31, 2025?'
        from dalton_core.numeric_target_revision import derive_revision_proof
        self.assertIsNone(derive_revision_proof(a,parent['mission_ref'],new))
        fresh=fixture.admit(h,f,a,parent,new,tag='unrelated-normal-question')
        self.assertEqual(fresh['status'],'fresh')
        self.assertIsNone(a.admission(fresh['admission_ref'])['numeric_authority'].get('target_revision_proof'))
        self.assertEqual((p.calls,v.calls),(1,1))

if __name__=='__main__':unittest.main()
