"""Exact question binding, never document-wide or inferred text equivalence."""
import copy
import json
import unittest
from datetime import timedelta
from dalton_core.canonical_document_questions import read_questions,resolve_binding,normalize_binding
from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending, MissionDocumentResearchError
from dalton_core.research_planner import plan_from_response, project_state_for_prompt,build_prompt
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash,inquiry_ref_for
from dalton_core.store import canonical_json,content_hash
from tests import test_mission_document_research as mission_fixtures
from tests.test_mission_annual_research import COMPANY,NOW

class CanonicalDocumentQuestionTests(unittest.TestCase):
    def fixture(self, *, auto_commit=False):
        h=mission_fixtures.MissionDocumentResearchTests('test_source_neutral_executor_searches_models_verifies_and_stages_once')
        self.addCleanup(h.doCleanups)
        f,a,args,reg,launcher=h._fixture(auto_commit=auto_commit)
        first=a.admit_from_plan(**args)
        return h,f,a,args,reg,first

    def followup(self,h,f,a,args,first,*,canonical=True,question='Explain the same accounting policy in the original note.'):
        row=f.store.connection.execute('SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?',(first['plan_ref'],)).fetchone()
        p=json.loads(row[0]);p.pop('content_hash');p['state_hash']=content_hash({'question':question,'canonical':canonical})
        i=p['inquiries'][0];i['question']=question
        if canonical:i['canonical_question']={'version_ref':first['question_version_ref'],'content_hash':first['question_version_hash']}
        p['content_hash']=content_hash(p);stored=h._record_plan(f,p)
        q=ResearchQuestionBacklog(f.store).record_question(mandate_version_ref=f.mission['bindings']['mandate_version']['ref'],
            company_ref=COMPANY,question=i['question'],answer_criteria=i['wants'],source_refs=[first['source_ref']],
            actor_ref=f.mission['autonomy']['automation_principal'],mission_binding={'ref':f.mission['id'],'hash':f.mission['content_hash']})
        return i,{**args,'plan_ref':stored['plan_id'],'inquiry_ref':inquiry_ref_for(inquiry_content_hash(i)),
                   'question_version_ref':q['question_version_ref']}

    def test_inflight_reworded_explicit_question_is_blocked_and_old_rows_unchanged(self):
        h,f,a,args,reg,first=self.fixture()
        before=f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions').fetchall()
        i,new=self.followup(h,f,a,args,first)
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as ctx:a.admit_from_plan(**new)
        self.assertEqual(ctx.exception.equivalent['matched_on'],'canonical_question')
        self.assertEqual([tuple(r) for r in f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions')],[tuple(r) for r in before])
        self.assertNotEqual(inquiry_content_hash(i),inquiry_content_hash({k:v for k,v in i.items() if k!='canonical_question'}))

    def test_answered_same_question_remains_blocked_but_another_question_same_document_is_legal(self):
        h,f,a,args,reg,first=self.fixture(auto_commit=True);executor,draft,verifier=h._executor(f,a)
        h._run_until(executor,first,lambda x:x.get('status')=='complete')
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_research_promotions').fetchone()[0],1)
        i,new=self.followup(h,f,a,args,first)
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as ctx:a.admit_from_plan(**new)
        self.assertEqual(ctx.exception.equivalent['state'],'recently_answered')
        different,new=self.followup(h,f,a,args,first,canonical=False,question='What are contract assets and how are they presented?')
        self.assertEqual(a.admit_from_plan(**new)['status_marker'],'fresh')

    def test_strong_read_state_contains_literal_question_and_exact_source(self):
        h,f,a,args,reg,first=self.fixture()
        rows=read_questions(f.store.connection,f.mission,now=NOW)[COMPANY]['questions']
        self.assertEqual(rows[0]['question'],first['planner_inquiry']['question'])
        self.assertEqual(rows[0]['canonical_question'],{'version_ref':first['question_version_ref'],'content_hash':first['question_version_hash']})
        self.assertEqual(rows[0]['document_version_hash'],reg['content_hash'])
        self.assertEqual(rows[0]['state'],'in_flight')
        projected=read_questions(f.store.connection,f.mission,now=NOW+timedelta(days=5))
        self.assertIn(COMPANY,projected) # Unsettled obligations are not aged away.

    def test_parser_rejects_foreign_hash_source_and_company_without_guessing_new_questions(self):
        h,f,a,args,reg,first=self.fixture();rows=read_questions(f.store.connection,f.mission,now=NOW)
        company={'company_ref':COMPANY,'items':[],'canonical_document_questions':rows[COMPANY],
            'readable_documents':[{'document_ref':reg['document_ref'],'document_version_hash':reg['content_hash'],
                'readable':True,'authority_ref':reg['id'],'authority_hash':reg['content_hash'],'source_ref':reg['source_ref'],'operations':['search_registered_document','read_registered_document']}]}
        state={'schema_version':'0.1','content_hash':'a'*64,'goal':{},'companies':[company],
               'document_research_policy':a.registry.policy,'document_research_contract_ref':'directed-document:0.1'}
        inquiry=copy.deepcopy(first['planner_inquiry']);inquiry.pop('rank');inquiry['question']='Rephrase the same problem'
        inquiry['canonical_question']=rows[COMPANY]['questions'][0]['canonical_question']
        wire={'schema_version':'0.1','assessment':'Read','directives':[],'inquiries':[inquiry],'sufficiency':[]}
        good=plan_from_response(state,canonical_json(wire),created_at=NOW.isoformat())
        self.assertEqual(good['inquiries'][0]['canonical_question'],inquiry['canonical_question'])
        for mutate in ('hash','source','company'):
            bad=copy.deepcopy(wire)
            if mutate=='hash':bad['inquiries'][0]['canonical_question']['content_hash']='f'*64
            if mutate=='source':bad['inquiries'][0]['directed_document']['document_version_hash']='f'*64
            if mutate=='company':bad['inquiries'][0]['company_ref']='company:other'
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):plan_from_response(state,canonical_json(bad),created_at=NOW.isoformat())
        del wire['inquiries'][0]['canonical_question']
        self.assertNotIn('canonical_question',plan_from_response(state,canonical_json(wire),created_at=NOW.isoformat())['inquiries'][0])

    def test_admission_rechecks_hash_and_same_mission_lineage(self):
        h,f,a,args,reg,first=self.fixture();binding={'version_ref':first['question_version_ref'],'content_hash':first['question_version_hash']}
        strategy=first['planner_inquiry']['directed_document']
        self.assertEqual(resolve_binding(f.store.connection,f.mission,company_ref=COMPANY,binding=binding,strategy=strategy)['id'],first['id'])
        current=h._roll_mission(f)
        self.assertEqual(resolve_binding(f.store.connection,current,company_ref=COMPANY,binding=binding,strategy=strategy)['id'],first['id'])
        with self.assertRaises(ValueError):resolve_binding(f.store.connection,current,company_ref=COMPANY,binding={**binding,'content_hash':'f'*64},strategy=strategy)
        with self.assertRaises(ValueError):resolve_binding(f.store.connection,current,company_ref=COMPANY,binding=binding,strategy={**strategy,'document_version_hash':'f'*64})
        with self.assertRaises(ValueError):normalize_binding({**binding,'other':True})

    def test_compact_projection_retains_pending_bindings_before_old_statistics(self):
        binding={'version_ref':'research-question-version:pending','content_hash':'a'*64}
        active={'canonical_question':binding,'question':'An older pending source-bound question',
            'document_ref':'source:original','document_version_hash':'b'*64,
            'state':'in_flight','referenced_by_latest_plan':False}
        history={**active,'canonical_question':{'version_ref':'research-question-version:done','content_hash':'c'*64},
            'question':'A settled historical question','state':'recently_settled'}
        company={'company_ref':COMPANY,'items':[],'canonical_document_questions':{'questions':[history,active]},
            'reading_pipeline':{'by_kind':{'kind:'+str(i):{'open':1,'staged':2,'dismissed':{'old_reason':3}} for i in range(100)},
                'open_blockers':[{'document_ref':'source:original','blocker':'Awaiting an exact source'}]},
            'readable_documents':[],'irrelevant_detail':'x'*50000}
        state={'schema_version':'0.1','content_hash':'d'*64,'goal':{},'companies':[company]}
        # Keep a fixed allowance for this fixture's state, independent of the
        # shared planner instructions. Those instructions can grow without
        # changing which pending bindings outrank historical statistics.
        empty_state={**state,'companies':[]}
        input_bound=len(build_prompt(empty_state).encode())+2700
        self.assertGreater(len(build_prompt(state).encode()),input_bound)
        projected=project_state_for_prompt(state,max_input_bytes=input_bound)
        self.assertLessEqual(len(build_prompt(projected).encode()),input_bound)
        self.assertEqual(projected['companies'][0]['canonical_document_questions']['questions'],[active])
        self.assertEqual(projected['companies'][0]['reading_pipeline']['open_blockers'],company['reading_pipeline']['open_blockers'])
        self.assertEqual(projected['companies'][0]['reading_pipeline']['kind_counts_summary']['open'],100)
        self.assertEqual(state['companies'][0]['canonical_document_questions']['questions'],[history,active])

    def test_foreign_project_cannot_borrow_a_same_company_question(self):
        from dalton_core.coverage_mission import CoverageMissionAuthority
        h,f,a,args,reg,first=self.fixture()
        current=f.mission
        foreign=CoverageMissionAuthority(f.store).create_mission('coverage-mission:foreign-canonical-test',
            title=current['title'],objective=current['objective'],industry_ref=current['industry_ref'],
            universe=current['universe'],research_questions=current['research_questions'],deliverables=current['deliverables'],
            source_plan=current['source_plan'],bindings=current['bindings'],autonomy=current['autonomy'],budget=current['budget'],
            actor_ref='human:test-owner',version_id='coverage-mission-version:foreign-canonical-test:1',
            prior_version_ref=None,idempotency_key='foreign-canonical-test:1')
        with self.assertRaisesRegex(ValueError,'same-mission exact original'):
            resolve_binding(f.store.connection,foreign,company_ref=COMPANY,
                binding={'version_ref':first['question_version_ref'],'content_hash':first['question_version_hash']},
                strategy=first['planner_inquiry']['directed_document'])

    def test_invalid_explicit_identity_refuses_before_host_records_a_new_question(self):
        from dalton_core.mission_document_research_admission import admit_directed_inquiry
        h,f,a,args,reg,first=self.fixture()
        inquiry,new=self.followup(h,f,a,args,first)
        inquiry['canonical_question']['content_hash']='f'*64
        row=f.store.connection.execute('SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?',(new['plan_ref'],)).fetchone()
        plan=json.loads(row[0]);plan.pop('content_hash');plan['inquiries']=[inquiry];plan['state_hash']='e'*64;plan['content_hash']=content_hash(plan)
        stored=h._record_plan(f,plan)
        before=f.store.connection.execute('SELECT COUNT(*) FROM backlog_question_versions').fetchone()[0]
        with self.assertRaises(MissionDocumentResearchError):
            admit_directed_inquiry(authority=a,registrations={reg['id']:reg},backlog=ResearchQuestionBacklog(f.store),
                mission=f.mission,plan=stored,inquiry=inquiry)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM backlog_question_versions').fetchone()[0],before)

    def test_bounded_display_prioritizes_an_older_pending_question_over_a_completed_read(self):
        from unittest.mock import patch
        h,f,a,args,reg,first=self.fixture()
        _,new=self.followup(h,f,a,args,first,canonical=False,question='What is the separately requested contract asset presentation?')
        second=a.admit_from_plan(**new);executor,draft,verifier=h._executor(f,a)
        h._run_until(executor,second,lambda x:x.get('status')=='complete')
        with patch('dalton_core.canonical_document_questions.MAX_QUESTIONS_PER_COMPANY',1):
            context=read_questions(f.store.connection,f.mission,now=NOW)[COMPANY]
        self.assertEqual(context['questions'][0]['canonical_question']['version_ref'],first['question_version_ref'])
        self.assertEqual(context['questions'][0]['state'],'in_flight')
        self.assertEqual(context['omitted_count'],1)
