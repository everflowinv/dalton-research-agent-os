"""A verified zero-answer result is not an answer, nor an unlimited retry door."""
import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.directed_numeric_output import candidate_ref
from dalton_core.directed_numeric_research import normalize_targets, pending_numeric_result
from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending, MissionDocumentResearchError, _settlement_instants
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.numeric_target_revision import derive_revision_proof, verify_revision_proof, revision_consumer, target_scope
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as workflow


class NumericWaitingRevisionTests(unittest.TestCase):
    def fixture(self, *, zero=True, percent=True):
        helper = workflow.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay')
        self.addCleanup(helper.doCleanups)
        def growth_targets(value):
            adjusted = []
            for t in value:
                t = {**t, 'unit': 'percent', 'currency': None,
                     'period': 'FY2025 vs FY2024'}
                t.pop('metric_definition_hash', None)
                adjusted.append(t)
            return normalize_targets(adjusted)
        with patch('tests.test_directed_numeric_workflow.normalize_targets', side_effect=growth_targets if percent else normalize_targets):
            f, a, admission, executor, producer, verifier = helper.fixture(missing=not zero)
        if zero:
            draft = copy.deepcopy(producer.candidate_wire)
            for row in draft['targets']:
                row.update(status='unavailable', figure=None, reason='The original does not directly disclose this requested growth percentage.')
            verdict = {'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass',
                       'targets':[{'target_ref':r['target_ref'],'verdict':'unavailable',
                                   'reason':'The original does not directly disclose this requested value.'} for r in draft['targets']]}
            producer.candidate_wire = draft
            verifier.candidate_wire = verdict
        result = helper.drive(executor, admission)
        self.assertEqual(result['research_status'], 'numeric_waiting_evidence')
        self.assertEqual(len(result['numeric_result']['completed_targets']), 0 if zero else 2)
        return helper, f, a, admission, result, producer, verifier

    def inquiry(self, admission, *, changed=True):
        inquiry=copy.deepcopy(admission['planner_inquiry'])
        inquiry['because'] += ' Read directly disclosed amounts instead of undisclosed derived changes.'
        inquiry['wants'] += ' Retain exact original source proof.'
        if changed:
            targets=[]
            for old in inquiry['directed_document']['numeric_targets']:
                row={**old, 'target_ref':old['target_ref']+'-amount',
                     'metric_ref':old['metric_ref']+'-amount', 'period':'FY2025',
                     'unit':'currency','currency':'USD'}
                row.pop('metric_definition_hash',None)
                targets.append(row)
            inquiry['directed_document']['numeric_targets']=normalize_targets(targets)
        return inquiry

    def admit(self, helper, f, a, parent, inquiry, *, tag='revised'):
        plan={'schema_version':'0.1','task_ref':'task:research-plan-directives:0.1',
              'created_at':f.mission['created_at'],'state_hash':content_hash(tag),
              'mission_version_ref':f.mission['id'],'assessment':'Read the source-proved revised numeric scope.',
              'directives':[],'inquiries':[inquiry],'sufficiency':[]}
        plan['content_hash']=content_hash(plan)
        stored=helper._record_plan(f,plan)
        return admit_directed_inquiry(authority=a, registrations={parent['document_authority_ref']:parent['request']['registration']},
            backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=stored,inquiry=inquiry)

    def test_zero_answer_same_question_amount_revision_fresh_and_old_bytes_unchanged(self):
        h,f,a,parent,result,p,v=self.fixture()
        before=tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone())
        original=tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(parent['id'],)).fetchone())
        admitted=self.admit(h,f,a,parent,self.inquiry(parent))
        current=a.resolve_for_execution(admitted['admission_ref'])
        proof=current['numeric_authority']['target_revision_proof']
        self.assertEqual(proof['numeric_result_hash'],result['numeric_result']['content_hash'])
        self.assertEqual(current['planner_inquiry']['question'],parent['planner_inquiry']['question'])
        self.assertEqual(current['model_execution'],parent['model_execution'])
        self.assertEqual(current['outer_budget'],parent['outer_budget'])
        self.assertEqual(before,tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(parent['id'],)).fetchone()))
        self.assertEqual(original,tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(parent['id'],)).fetchone()))
        self.assertEqual(a.resolve_for_execution(parent['id']),parent)
        self.assertEqual((p.calls,v.calls),(1,1))
        self.assertEqual(self.admit(h,f,a,parent,self.inquiry(parent),tag='duplicate')['status'],'duplicate')

    def test_same_targets_wait_not_answered_without_new_question_or_calls(self):
        h,f,a,parent,result,p,v=self.fixture()
        before=f.store.connection.execute('SELECT COUNT(*) FROM backlog_question_versions').fetchone()[0]
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as ctx:
            self.admit(h,f,a,parent,self.inquiry(parent,changed=False))
        self.assertEqual(ctx.exception.equivalent['state'],'waiting_evidence')
        self.assertIn('仍缺所需原文证据，暂不重复读取',str(ctx.exception))
        self.assertNotIn(parent['id'],_settlement_instants(f.store.connection,[parent['id']]))
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM backlog_question_versions').fetchone()[0],before)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_renaming_rewording_deletion_and_empty_do_not_authorize_revision(self):
        h,f,a,parent,result,p,v=self.fixture()
        for mode in ('ref','metric','words','delete'):
            inquiry=self.inquiry(parent,changed=False)
            targets=inquiry['directed_document']['numeric_targets']
            for t in targets:
                t.pop('metric_definition_hash')
                if mode=='ref':t['target_ref']+='-renamed'
                if mode=='metric':t['metric_ref']+='-renamed'
                if mode=='words':t['metric_definition']['meaning']+=' Same disclosed scope.'
            if mode=='delete':targets.pop()
            inquiry['directed_document']['numeric_targets']=normalize_targets(targets)
            with self.subTest(mode=mode):
                self.assertIsNone(derive_revision_proof(a,parent['mission_ref'],inquiry))
                with self.assertRaises(MissionDocumentResearchEquivalentPending):self.admit(h,f,a,parent,inquiry,tag=mode)
        inquiry=self.inquiry(parent);inquiry['directed_document']['numeric_targets']=[]
        with self.assertRaises(ValueError):derive_revision_proof(a,parent['mission_ref'],inquiry)

    def test_partial_completed_unknown_and_foreign_source_never_use_exception(self):
        h,f,a,parent,result,p,v=self.fixture(zero=False,percent=False)
        changed = self.inquiry(parent)
        for target in changed['directed_document']['numeric_targets']:
            target['period'] = 'FY2024'
        self.assertTrue({tuple(row[1:]) for row in target_scope(changed['directed_document']['numeric_targets'])}
                        - {tuple(row[1:]) for row in target_scope(parent['numeric_authority']['targets'])})
        self.assertIsNone(derive_revision_proof(a,parent['mission_ref'],changed))
        with self.assertRaises(MissionDocumentResearchEquivalentPending):self.admit(h,f,a,parent,changed)
        h2,f2,a2,parent2,result2,p2,v2=self.fixture()
        for field,value in (('document_ref','source:other'),('document_version_hash','e'*64)):
            inquiry=self.inquiry(parent2);inquiry['directed_document'][field]=value
            self.assertIsNone(derive_revision_proof(a2,parent2['mission_ref'],inquiry))
        self.assertIsNone(derive_revision_proof(a2,'coverage-mission:foreign',self.inquiry(parent2)))
        inquiry=self.inquiry(parent2);inquiry['company_ref']='company:foreign'
        self.assertIsNone(derive_revision_proof(a2,parent2['mission_ref'],inquiry))
        # Losing the formal result is unknown, not a zero-answer authorization.
        with patch('dalton_core.numeric_target_revision.pending_numeric_result',return_value=None):
            self.assertIsNone(derive_revision_proof(a2,parent2['mission_ref'],self.inquiry(parent2)))

    def test_one_result_once_and_withdrawal_does_not_replenish_revision(self):
        h,f,a,parent,result,p,v=self.fixture()
        first=self.admit(h,f,a,parent,self.inquiry(parent))
        from dalton_core.mission_document_research_withdrawal import MissionDocumentWithdrawalAuthority
        MissionDocumentWithdrawalAuthority(f.store).withdraw(first['admission_ref'],actor_ref='human:owner',reason='Close this revised request.')
        another=self.inquiry(parent)
        for t in another['directed_document']['numeric_targets']:t['period']='FY2024'
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as ctx:self.admit(h,f,a,parent,another,tag='second')
        self.assertEqual(ctx.exception.equivalent['state'],'target_revision_already_admitted')
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_admissions').fetchone()[0],2)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_transaction_rechecks_once_when_preflight_missed_competing_admission(self):
        h,f,a,parent,result,p,v=self.fixture()
        self.admit(h,f,a,parent,self.inquiry(parent))
        second=self.inquiry(parent)
        for t in second['directed_document']['numeric_targets']:t['period']='FY2024'
        with patch.object(a,'equivalent_open_admission',return_value=None):
            with self.assertRaises(MissionDocumentResearchEquivalentPending):self.admit(h,f,a,parent,second,tag='race-loser')
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_admissions').fetchone()[0],2)

    def test_revised_completion_does_not_close_old_wide_question_or_material_requests(self):
        h,f,a,parent,result,p,v=self.fixture()
        inquiry=self.inquiry(parent)
        # A real disclosed RPO total, a distinct currency target from the old percentages.
        inquiry['directed_document']['numeric_targets']=normalize_targets([{
            'target_ref':'target:actual-rpo','metric_ref':'metric:synthetic-rpo',
            'metric_definition':{'label':'Remaining performance obligations','meaning':'Issuer consolidated remaining performance obligations'},
            'period':'FY2025','unit':'currency','currency':'USD','basis':'gaap-reported'}])
        admitted=self.admit(h,f,a,parent,inquiry)
        current=a.resolve_for_execution(admitted['admission_ref'])
        draft=copy.deepcopy(p.candidate_wire)
        from dalton_core.directed_numeric_output import quotes
        quote=next(iter(quotes(a.registry.search(current['request']))))
        draft['targets']=[{'target_ref':'target:actual-rpo','status':'supported','reason':None,'figure':{
            'quote_id':quote,'metric_ref':'metric:synthetic-rpo','subject_as_named':'Synthetic issuer',
            'as_reported_label':'Remaining performance obligations','value':'12.3','unit':'currency',
            'currency':'USD','period':'FY2025','basis':'gaap-reported','scale':'billion'}}]
        verdict={'schema_version':'0.1','candidate_ref':candidate_ref(draft),'verdict':'pass',
                 'targets':[{'target_ref':'target:actual-rpo','verdict':'accept','reason':None}]}
        from tests.test_mission_document_research import RouteBoundCountingFakeAdapter
        ex,np,nv=h._executor(f,a,draft_adapter=RouteBoundCountingFakeAdapter(draft),verifier_adapter=RouteBoundCountingFakeAdapter(verdict))
        done=h.drive(ex,current)
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(done['numeric_result']['resolved_material_requests'],[])
        self.assertEqual(pending_numeric_result(f.store.connection,parent['id'])['result'],result['numeric_result'])
        from dalton_core.material_requests import request
        self.assertTrue(all(request(f.state,row['request_ref'])['status']=='open' for row in result['numeric_result']['material_requests']))
        self.assertNotIn(parent['id'],_settlement_instants(f.store.connection,[parent['id']]))
        self.assertEqual((np.calls,nv.calls),(1,1))

    def test_exact_legacy_admit_replay_does_not_retroactively_add_revision_identity(self):
        h,f,a,parent,result,p,v=self.fixture()
        inquiry=self.inquiry(parent)
        # This independently admitted record predates the new revision rule.
        with patch('dalton_core.numeric_target_revision.derive_revision_proof',return_value=None), \
             patch.object(a,'equivalent_open_admission',return_value=None):
            saved=self.admit(h,f,a,parent,inquiry,tag='legacy-admitted')
        legacy=a.admission(saved['admission_ref'])
        before=tuple(f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',
            (legacy['id'],)).fetchone())
        self.assertNotIn('target_revision_proof',legacy['numeric_authority'])
        self.assertIsNone(derive_revision_proof(a,parent['mission_ref'],inquiry))
        replay=a.admit_from_plan(plan_ref=legacy['plan_ref'],inquiry_ref=legacy['inquiry_ref'],
            question_version_ref=legacy['question_version_ref'],document_authority_ref=legacy['document_authority_ref'])
        self.assertEqual(replay['status_marker'],'duplicate')
        self.assertEqual(a.resolve_for_execution(legacy['id']),legacy)
        self.assertEqual(before,tuple(f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',
            (legacy['id'],)).fetchone()))
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_admissions').fetchone()[0],2)

    def test_complete_answer_blocks_and_forged_revision_marker_refuses(self):
        h=workflow.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay');self.addCleanup(h.doCleanups)
        f,a,parent,ex,p,v=h.fixture();h.drive(ex,parent)
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as ctx:self.admit(h,f,a,parent,self.inquiry(parent))
        self.assertEqual(ctx.exception.equivalent['state'],'recently_answered')
        with self.assertRaises(MissionDocumentResearchError):verify_revision_proof(a,parent['mission_ref'],self.inquiry(parent),{'rule_ref':'invented'})

if __name__=='__main__':unittest.main()
