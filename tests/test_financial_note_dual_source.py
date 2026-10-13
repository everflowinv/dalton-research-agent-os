"""Exact source identities survive independent acquisitions, never foreign scope."""
import copy
import json
import unittest

from dalton_core.document_research_inventory import financial_note_targets_for_registration
from dalton_core.document_research_strategy import normalize_evidence_target
from dalton_core.financial_note_evidence import resolve_financial_note_evidence, FinancialNoteEvidenceError
from dalton_core.store import content_hash, canonical_json
from tests import test_financial_note_evidence as note_fixtures
from dalton_core.coverage_mission import CoverageMissionAuthority


class DualSourceNoteTests(unittest.TestCase):
    def fixture(self, company=None):
        from tests import test_mission_annual_research as annual_fixture
        from tests import test_research_plan_annual_report as annual_support
        from unittest.mock import patch
        helper=note_fixtures.FinancialNoteResolverTests();self.addCleanup(helper.doCleanups)
        kwargs={'form':'10-Q','start':'2025-07-01','end':'2025-09-30',
                'report_date':'2025-12-31','kind':'quarter','dual_source_mission':True}
        if company is None:
            return helper._completed_typed(target_options=kwargs)
        with patch.object(note_fixtures,'COMPANY',company), patch.object(annual_fixture,'COMPANY',company), patch.object(annual_support,'COMPANY',company):
            return helper._completed_typed(target_options=kwargs)

    def test_current_acquisition_prior_statement_promotes_and_replays_without_new_calls(self):
        f, executor, admission, target = self.fixture()
        self.assertEqual(target['schema_version'],'financial-note-target-0.3')
        reg=admission['request']['registration']; binding=target['source_lineage_binding']
        self.assertNotEqual(binding['acquired_mission_ref'],binding['statement_mission_ref'])
        self.assertEqual(binding['registration_hash'],reg['content_hash'])
        before=f.store.connection.total_changes
        kwargs=dict(core_connection=f.store.connection,router_connection=f.router.connection,
            staging_connection=executor.staging.connection,registry=executor.registry,admission_ref=admission['id'])
        result=resolve_financial_note_evidence(**kwargs)
        self.assertEqual(resolve_financial_note_evidence(**kwargs),result)
        self.assertEqual(result['source_lineage_binding'],binding)
        self.assertEqual(result['periods'],target['periods'])
        self.assertNotIn('value',result)
        self.assertEqual(f.store.connection.total_changes,before)
        self.assertEqual(financial_note_targets_for_registration(connection=f.store.connection,
            mission=f.mission,company_ref=admission['company_ref'],registration=reg,
            target_schema_version='financial-note-target-0.2'),[])

    def test_rehashed_source_binding_cannot_replace_independent_original_authority(self):
        f,e,a,target=self.fixture();reg=a['request']['registration']; db=f.store.connection
        for key in ('acquired_authority_hash','statement_mission_hash','registration_hash','issuer_cik'):
            changed=copy.deepcopy(target);b=changed['source_lineage_binding'];b[key]=('0000009999' if key=='issuer_cik' else '0'*64)
            b['content_hash']=content_hash({k:v for k,v in b.items() if k!='content_hash'})
            normalize_evidence_target(changed)
            actual=financial_note_targets_for_registration(connection=db,mission=f.mission,
                company_ref=a['company_ref'],registration=reg,target_schema_version='financial-note-target-0.3',
                context_version_ref=target['statement_context_ref'],latest_context=False)
            self.assertNotIn(changed,actual)

    def test_source_row_dispatch_mission_company_hash_or_url_mismatch_does_not_advertise_target(self):
        f,e,a,t=self.fixture();db=f.store.connection;reg=a['request']['registration']
        rows=[dict(r) for r in db.execute('SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?',
            (t['source_lineage_binding']['statement_dispatch_ref'],))];old=rows[0]
        for field,value in [('mission_version_hash','0'*64),('company_ref','company:foreign')]:
            with CoverageMissionAuthority(f.store)._transaction() as cursor:
                cursor.execute('UPDATE coverage_mission_statement_dispatches SET '+field+'=? WHERE dispatch_id=?',(value,old['dispatch_id']))
            self.assertEqual(financial_note_targets_for_registration(connection=db,mission=f.mission,
                company_ref=a['company_ref'],registration=reg),[])
            with CoverageMissionAuthority(f.store)._transaction() as cursor:
                cursor.execute('UPDATE coverage_mission_statement_dispatches SET '+field+'=? WHERE dispatch_id=?',(old[field],old['dispatch_id']))
        for url in ('https://www.sec.gov/Archives/edgar/data/9999/'+t['accession'].replace('-','')+'/file.htm',
                    'https://elsewhere.example/Archives/edgar/data/1/'+t['accession'].replace('-','')+'/file.htm'):
            changed=copy.deepcopy(reg);changed['origin_ref']=url
            body={k:v for k,v in changed.items() if k not in ('id','content_hash')}
            changed['id']='registered-document:sha256:'+content_hash(body)
            changed['content_hash']=content_hash({k:v for k,v in changed.items() if k!='content_hash'})
            self.assertEqual(financial_note_targets_for_registration(connection=db,mission=f.mission,
                company_ref=a['company_ref'],registration=changed),[])


    def test_two_companies_use_the_same_dual_source_contract(self):
        for company in ('synthetic:issuer:A','synthetic:issuer:B'):
            with self.subTest(company=company):
                f,e,a,t=self.fixture(company)
                self.assertEqual(t['source_lineage_binding']['company_ref'],company)
                self.assertEqual(a['company_ref'],company)
                self.assertEqual(resolve_financial_note_evidence(core_connection=f.store.connection,
                    router_connection=f.router.connection,staging_connection=e.staging.connection,
                    registry=e.registry,admission_ref=a['id'])['company_ref'],company)

    def test_direct_reader_requires_admitted_mission_and_rejects_rehashed_lineage(self):
        from dalton_core.financial_note_evidence import _statement_filing
        f,e,a,t=self.fixture();reg=a['request']['registration']
        with self.assertRaisesRegex(FinancialNoteEvidenceError,'admitted mission'):
            _statement_filing(f.store.connection,t,company_ref=a['company_ref'],registration=reg)
        changed=copy.deepcopy(t);b=changed['source_lineage_binding'];b['acquired_authority_hash']='0'*64
        b['content_hash']=content_hash({k:v for k,v in b.items() if k!='content_hash'})
        with self.assertRaisesRegex(FinancialNoteEvidenceError,'source lineage differs'):
            _statement_filing(f.store.connection,changed,company_ref=a['company_ref'],registration=reg,mission=f.mission)

    def test_planner_output_schema_preserves_closed_version3_and_complete_binding(self):
        from dalton_core.research_planner import _history_note_prompt_schema
        f,e,a,t=self.fixture()
        schema=_history_note_prompt_schema({'companies':[{'readable_documents':[{'evidence_targets':[t]}]}]})
        variants=schema['properties']['inquiries']['items']['properties']['directed_document']['properties']['evidence_target']['oneOf']
        new=next(x for x in variants if x['properties']['schema_version']['const']=='financial-note-target-0.3')
        self.assertIn('source_lineage_binding',new['required'])
        self.assertEqual(set(new['properties']['source_lineage_binding']['required']),set(t['source_lineage_binding']))
        self.assertFalse(new['properties']['source_lineage_binding']['additionalProperties'])

    def test_current_disconnected_source_and_changed_current_hash_cannot_admit_new_target(self):
        from tests import test_mission_document_research as document_fixtures
        f,e,a,t=self.fixture();reg=a['request']['registration']
        changed=copy.deepcopy(f.mission);changed['content_hash']='0'*64
        self.assertEqual(financial_note_targets_for_registration(connection=f.store.connection,
            mission=changed,company_ref=a['company_ref'],registration=reg),[])
        sources=copy.deepcopy(f.mission['source_plan'])
        next(x for x in sources if x['source_ref']=='source:sec-edgar')['status']='not_connected'
        current=document_fixtures.MissionDocumentResearchTests()._roll_mission(f,source_plan=sources,version=3)
        self.assertEqual(financial_note_targets_for_registration(connection=f.store.connection,
            mission=current,company_ref=a['company_ref'],registration=reg),[])

    def test_wrong_acquired_hash_ticket_and_original_bytes_identity_refuse(self):
        f,e,a,t=self.fixture();reg=a['request']['registration']
        for kind in ('authority_hash','ticket','registration_hash'):
            changed=copy.deepcopy(reg)
            if kind=='authority_hash':changed['source_authority']['hash']='0'*64
            elif kind=='ticket':changed['acquisition_ticket_ref']='public-web-fetch:foreign'
            else:changed['content_hash']='0'*64
            if kind!='registration_hash':
                body={k:v for k,v in changed.items() if k not in ('id','content_hash')}
                changed['id']='registered-document:sha256:'+content_hash(body)
                changed['content_hash']=content_hash({k:v for k,v in changed.items() if k!='content_hash'})
            self.assertEqual(financial_note_targets_for_registration(connection=f.store.connection,
                mission=f.mission,company_ref=a['company_ref'],registration=changed),[])

class NoteDirectoryProjectionTests(unittest.TestCase):
    def test_old_note_document_survives_stage5_and_schema_keeps_lineage_target(self):
        from tests import test_research_planner as planner_tests
        from tests import test_financial_note_history_periods as history_tests
        from dalton_core.research_planner import project_state_for_prompt, build_prompt, ResearchPlanInputTooLarge
        from dalton_core.document_research_strategy import FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION
        from dalton_core.planner_note_options import option_ref, expand_selection
        helper=history_tests.HistoryNotePeriodsTests();self.addCleanup(helper.doCleanups)
        target=helper.targets(helper.filing())[0]
        state=planner_tests.PromptTests().heavy_state()
        for company in state['companies']:
            template=company['readable_documents'][0]
            company['readable_documents']=[dict(template,document_ref='doc:'+str(n),doc_date='2026-10-01') for n in range(30)]
            company['readable_documents'][0].update({'document_ref':'sec:filing:'+target['accession'],
                'doc_date':'2020-01-01','evidence_targets':[target]})
        state.pop('content_hash',None);state['content_hash']=content_hash(state)
        full=len(build_prompt(state).encode());checked=False
        for budget in range(full-4000,15000,-4000):
            try:projected=project_state_for_prompt(state,max_input_bytes=budget)
            except ResearchPlanInputTooLarge:continue
            if 'aggregate_readable_document_inventory' not in projected['prompt_projection']['stages_applied']:continue
            checked=True
            for company in projected['companies']:
                held=next(x for x in company['readable_documents'] if x['document_ref']=='sec:filing:'+target['accession'])
                self.assertEqual(held['evidence_targets'],[target])
                selected = {'document_ref': held['document_ref'],
                    'document_version_hash': held['document_version_hash'],
                    'evidence_target_ref': option_ref(company['company_ref'], held, target)}
                self.assertIn(selected['evidence_target_ref'], build_prompt(projected))
                restored = expand_selection(selected, company_ref=company['company_ref'], state=state)
                self.assertEqual(restored['evidence_target'], target)
                self.assertEqual(restored['evidence_target']['schema_version'], FINANCIAL_NOTE_HISTORY_TARGET_SCHEMA_VERSION)
            self.assertIn('typed-note-directory',projected['prompt_projection']['rule_ref'])
            break
        self.assertTrue(checked)

    def test_no_target_projection_contract_is_unchanged_and_invalid_target_is_not_pinned(self):
        from tests import test_research_planner as planner_tests
        from dalton_core.research_planner import _projection_ref_for_state,_projection_hash_for_state,PROMPT_PROJECTION_REF,PROMPT_PROJECTION_HASH
        state=planner_tests.PromptTests().heavy_state()
        self.assertEqual(_projection_ref_for_state(state),PROMPT_PROJECTION_REF)
        self.assertEqual(_projection_hash_for_state(state),PROMPT_PROJECTION_HASH)
        state['companies'][0]['readable_documents'][0]['evidence_targets']=[{'schema_version':'invented'}]
        self.assertEqual(_projection_ref_for_state(state),PROMPT_PROJECTION_REF)

    def test_typed_directory_exceeds_budget_refuses_without_dropping_authority(self):
        from tests import test_research_planner as planner_tests
        from tests import test_financial_note_history_periods as history_tests
        from dalton_core.research_planner import project_state_for_prompt, ResearchPlanInputTooLarge
        helper=history_tests.HistoryNotePeriodsTests();self.addCleanup(helper.doCleanups)
        target=helper.targets(helper.filing())[0]
        state=planner_tests.PromptTests().heavy_state()
        for company in state['companies']:
            for doc in company['readable_documents']:doc['evidence_targets']=[target]
        before=canonical_json(state)
        with self.assertRaises(ResearchPlanInputTooLarge):project_state_for_prompt(state,max_input_bytes=1)
        self.assertEqual(canonical_json(state),before)

if __name__=='__main__':unittest.main()
