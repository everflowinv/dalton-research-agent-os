import copy
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from dalton_core import model_history_note_priority as priority
from dalton_core.store import content_hash, canonical_json
from dalton_core.planner_paid_output_recovery import recovery_readers
from dalton_core.planner_note_options import expand_selection
from tests import test_cockpit_model_fallback as runtime
from tests import test_model_spec_maintenance_repair as model_fixtures


class AssociationTests(unittest.TestCase):
    def fixture(self, company='company:synthetic:alpha', currency='usd'):
        accession='0000000001-26-000001'
        target={'schema_version':'financial-note-target-0.2','target_ref':'financial_note:diluted_eps_numerator:0.1',
            'kind':'diluted_eps_numerator','statement_ingest_ref':'statement-ingest:'+'a'*32,
            'statement_filing_hash':'b'*64,'statement_context_ref':'statement-context:'+'c'*32,
            'statement_context_hash':'d'*64,'accession':accession,'form':'10-Q','applicability_kind':'quarter',
            'periods':[{'period_start':'2025-07-01','period_end':'2025-09-30'}]}
        document={'document_ref':'sec:filing:'+accession,'document_version_hash':'e'*64,
            'authority_ref':'registered:exact','authority_hash':'e'*64,'source_ref':'source:sec-edgar',
            'readable':True,'operations':['search_registered_document'],'evidence_targets':[target]}
        state={'company_ref':company,'filings':[{'accession':accession}]};state['state_hash']=content_hash(state)
        formula={'output_ref':'eps','operator':'divide','numerator_ref':'numerator','denominator_ref':'shares',
            'tie_out_concept':'us-gaap:EarningsPerShareDiluted','evidence_refs':[accession]}
        spec={'company_ref':company,'financial_statement_structure':{'lines':[
            {'ref':'eps','role':'diluted_eps','unit':currency+'_per_share'},
            {'ref':'numerator','role':'diluted_eps_numerator'},
            {'ref':'shares','role':'diluted_weighted_average_shares'}],'formulas':[formula]}}
        diagnostic={'contract_ref':'model-history-tie-diagnostics:0.1','output_ref':'eps','operator':'divide',
            'tie_out_concept':formula['tie_out_concept'],'formula_hash':content_hash(formula),
            'mismatches':[{'period_start':'2025-07-01','period_end':'2025-09-30','calculated':'-4.547','filed':'-4.52'}],
            'financial_evidence':False,'retry_authority':False};diagnostic['content_hash']=content_hash(diagnostic)
        rows={content_hash(target):[{'statement':'income','concept':formula['tie_out_concept'],'unit':currency.upper()+'PerShare',
            'statement_context_ref':target['statement_context_ref'],'period_start':'2025-07-01','period_end':'2025-09-30','value':'-4.52'}]}
        return {'state':state,'spec':spec,'diagnostic':diagnostic,'documents':[document],
                'root_proof':{'work_ref':'exact:root','fees_hash':'f'*64},'source_rows':rows}

    def test_two_companies_and_currency_preserve_exact_source_choice_without_grant(self):
        for company,currency in [('company:synthetic:alpha','usd'),('company:synthetic:beta','eur')]:
            inputs=self.fixture(company,currency);before=copy.deepcopy(inputs);got=priority.associate(**inputs)
            self.assertIsNotNone(got);self.assertFalse(got['retry_authority']);self.assertFalse(got['financial_evidence'])
            choice=got['note_options'][0];state={'companies':[{'company_ref':company,'readable_documents':inputs['documents']}]}
            expanded=expand_selection({'document_ref':choice['document_ref'],'document_version_hash':choice['document_version_hash'],
                                      'evidence_target_ref':choice['option_ref']},company_ref=company,state=state)
            self.assertEqual(expanded['evidence_target'],inputs['documents'][0]['evidence_targets'][0])
            self.assertEqual(inputs,before)

    def test_same_amount_or_date_different_metric_period_context_currency_is_not_equivalent(self):
        for mutation in ('metric','period','context','currency','dimensions','ambiguous','filing','value'):
            args=self.fixture();row=next(iter(args['source_rows'].values()))[0]
            if mutation=='metric':row['concept']='us-gaap:OperatingIncomeLoss'
            if mutation=='period':row['period_start']='2025-04-01'
            if mutation=='context':row['statement_context_ref']='statement-context:'+'f'*32
            if mutation=='currency':row['unit']='EURPerShare'
            if mutation=='value':row['value']='-9.99'
            if mutation=='dimensions':row['dimensions']={'axis':'member'}
            if mutation=='ambiguous':next(iter(args['source_rows'].values())).append(copy.deepcopy(row))
            if mutation=='filing':args['state']['filings']=[];args['state']['state_hash']=content_hash({k:v for k,v in args['state'].items() if k!='state_hash'})
            with self.subTest(mutation=mutation):self.assertIsNone(priority.associate(**args))

    def test_wrong_company_formula_operator_role_or_forged_diagnostic_is_refused(self):
        for mutation in ('company','formula','operator','role','diagnostic'):
            args=self.fixture()
            if mutation=='company':args['spec']['company_ref']='company:foreign'
            if mutation=='formula':args['spec']['financial_statement_structure']['formulas'][0]['numerator_ref']='shares'
            if mutation=='operator':args['diagnostic']['operator']='sum';args['diagnostic']['content_hash']=content_hash({k:v for k,v in args['diagnostic'].items()if k!='content_hash'})
            if mutation=='role':args['spec']['financial_statement_structure']['lines'][0]['role']='operating_income'
            if mutation=='diagnostic':args['diagnostic']['mismatches'][0]['filed']='99'
            with self.subTest(mutation=mutation):
                if mutation in ('company','diagnostic'):
                    with self.assertRaises(ValueError):priority.associate(**args)
                else:self.assertIsNone(priority.associate(**args))

    def test_inaccessible_document_or_unknown_target_cannot_become_priority(self):
        args=self.fixture();args['documents'][0]['readable']=False;self.assertIsNone(priority.associate(**args))
        args=self.fixture();args['documents'][0]['evidence_targets'][0]['kind']='made_up'
        with self.assertRaises(ValueError):priority.associate(**args)

    def test_priority_changes_material_hash_and_survives_compaction_with_all_note_options(self):
        from dalton_core.research_state import planning_hash, state_content_hash
        from dalton_core.research_planner import build_prompt, typed_note_directory_contract
        args=self.fixture();detail=priority.associate(**args)
        state={'companies':[{'company_ref':args['state']['company_ref'],'readable_documents':args['documents']}],
            'goal':{},'sources':[],'industry':None,'totals':{},'budget':{},'spend':{}}
        before=planning_hash(state);old_prompt=build_prompt(state)
        state['companies'][0]['model_history_note_priority']=detail
        self.assertNotEqual(planning_hash(state),before)
        self.assertIn(detail['note_options'][0]['option_ref'],build_prompt(state))
        self.assertIn('Address that current model blocker',build_prompt(state))
        self.assertIn('model_history_note_priorities_hash',typed_note_directory_contract(state))
        restored=copy.deepcopy(state);del restored['companies'][0]['model_history_note_priority']
        self.assertEqual(build_prompt(restored),old_prompt)

    def test_company_state_places_exact_current_blocker_original_first_and_keeps_all_documents(self):
        from dalton_core.research_state import company_state
        args=self.fixture();detail=priority.associate(**args);other={**args['documents'][0],'document_ref':'sec:filing:unrelated','evidence_targets':[]}
        got=company_state({'company_ref':args['state']['company_ref'],'items':[]},
            financial_model={'model_history_note_priority':detail},readable_documents=[other,*args['documents']])
        self.assertEqual(got['readable_documents'],[*args['documents'],other])
        self.assertEqual(got['model_history_note_priority'],detail)

    def test_signature_checks_original_events_ignores_repeated_tick_time_and_retains_changes(self):
        from dalton_core.lane_failure_ledger import LaneFailureLedger
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, LaneFailureLedger(Path(tmp)/'lane-failure-ledger.sqlite') as ledger:
            def event(reason,at):
                ledger.append_event(lane='mission_model_spec',item_key='company:synthetic:alpha|'+'a'*64,
                    event='terminal',failure_class='content_refused',reason=reason,rule='lane_content_refusal',status='refused',at=at)
            event('typed historical discrepancy',now);first=priority.signature(None,tmp)
            event('typed historical discrepancy',now+timedelta(seconds=10));self.assertEqual(priority.signature(None,tmp),first)
            event('different actual discrepancy',now+timedelta(seconds=20));self.assertNotEqual(priority.signature(None,tmp),first)
            ledger.connection.execute('DROP TRIGGER lane_failure_events_no_update')
            ledger.connection.execute("UPDATE lane_failure_events SET reason='tampered'")
            self.assertEqual(priority.signature(None,tmp)['status'],'unavailable')

    def test_seventeen_company_projection_preserves_every_exact_choice_at_192kb(self):
        from tests.test_document_research_strategy import with_document
        from dalton_core.research_planner import project_state_for_prompt, build_prompt
        from dalton_core.planner_note_options import prompt_state
        state=with_document();state['companies']=[]
        for i in range(17):
            args=self.fixture('company:synthetic:'+str(i));detail=priority.associate(**args)
            documents=[]
            for j in range(7):
                document=copy.deepcopy(args['documents'][0]);document['original_preview']='Original retained in source authority. '*1000
                document['document_ref']='sec:filing:0000000001-26-'+str(j+1).zfill(6)
                document['evidence_targets'][0]['accession']=document['document_ref'].rsplit(':',1)[1]
                documents.append(document)
            state['companies'].append({'company_ref':args['state']['company_ref'],'items':[],
                'readable_documents':documents,'model_history_note_priority':detail,
                'financial_model':{'model_history_note_priority':copy.deepcopy(detail)}})
        before=copy.deepcopy(state);projection=project_state_for_prompt(state,max_input_bytes=192000)
        self.assertLessEqual(len(build_prompt(projection).encode()),192000)
        rendered,count=prompt_state(projection)
        self.assertEqual(len(rendered['companies']),17);self.assertEqual(count,119)
        self.assertTrue(all(c['model_history_note_priority']['content_hash']==state['companies'][i]['model_history_note_priority']['content_hash']
                            for i,c in enumerate(rendered['companies'])))
        self.assertEqual(state,before)

    def test_duplicate_financial_feedback_is_shown_once_but_full_state_remains_exact(self):
        from dalton_core.planner_note_options import prompt_state
        args=self.fixture();detail=priority.associate(**args)
        state={'companies':[{'company_ref':args['state']['company_ref'],
            'model_history_note_priority':detail,'financial_model':{'model_history_note_priority':copy.deepcopy(detail)},
            'readable_documents':args['documents']}]};before=copy.deepcopy(state)
        rendered,_=prompt_state(state)
        self.assertNotIn('model_history_note_priority',rendered['companies'][0]['financial_model'])
        self.assertEqual(rendered['companies'][0]['financial_model']['model_history_note_priority_ref'],detail['content_hash'])
        self.assertEqual(state,before)


class RootProofTests(unittest.TestCase):
    def fixture(self):
        f=runtime.CockpitChainTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        state,body,_=model_fixtures.case('company:synthetic:alpha')
        f.mission['content_hash']=content_hash({k:v for k,v in f.mission.items() if k!='content_hash'})
        class Adapter(runtime.ChainAdapter):
            def execute(self,work,route,profile):
                inv,result=super().execute(work,route,profile)
                return inv,replace(result,outputs={'text':json.dumps(body)})
        adapter=Adapter({});model=f._model(adapter,policy_version_ref=f.pinned_policy,model_spec_output_tokens=6000)
        from dalton_core.company_model_cli import model_spec_request_identity,model_spec_request_id
        from dalton_core.company_model_spec import build_prompt
        model.call(purpose='model_spec',request_id=model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=build_prompt(state),mission=f.mission,
            _model_spec_request_identity=model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        return f,state,adapter

    def root(self,f,state,mission=None):
        with sqlite3.connect(f.root/'scheduler.sqlite') as sc,recovery_readers(f.router_db,f.root/'budget.sqlite',now=datetime.now(timezone.utc)) as (router,budget):
            sc.row_factory=sqlite3.Row
            return priority._root(sc,router,budget,state_hash=state['state_hash'],mission=mission or f.mission)

    def test_real_cockpit_formal_route_all_settled_fees_replay_without_new_work(self):
        f,state,adapter=self.fixture()
        got=self.root(f,state);self.assertIsNotNone(got);self.assertEqual(len(adapter.served),1)
        self.assertEqual(self.root(f,state),got);self.assertEqual(len(adapter.served),1)

    def test_wrong_current_mission_or_changed_input_cannot_borrow_root(self):
        f,state,adapter=self.fixture();mission=copy.deepcopy(f.mission);mission['content_hash']='f'*64
        self.assertIsNone(self.root(f,state,mission));changed=copy.deepcopy(state);changed['state_hash']='f'*64
        self.assertIsNone(self.root(f,changed));self.assertEqual(len(adapter.served),1)

    def test_unsettled_fee_or_invalid_amount_cannot_make_priority(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        f,state,adapter=self.fixture();original=ThesisImpactBudgetStore.admission
        for bad in (None,-1,True,'unknown'):
            def admission(reader,**kw):
                result=original(reader,**kw);result['settlement']['actual_micros']=bad;return result
            with patch.object(ThesisImpactBudgetStore,'admission',admission):self.assertIsNone(self.root(f,state))
        def unsettled(reader,**kw):
            result=original(reader,**kw);result['settlement']=None;return result
        with patch.object(ThesisImpactBudgetStore,'admission',unsettled):self.assertIsNone(self.root(f,state))
        self.assertEqual(len(adapter.served),1)
