"""Exact native annual admission -> child -> durable read -> context, no HTTP."""
import copy
import json
import unittest
from datetime import date,datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from dalton_core.connector_governance import build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority,CoverageMissionConflict
from dalton_core.sec_financials_cli import build_parser,run
from dalton_core.sec_financials_core import KIND
from dalton_core.sec_native_annual import (annual_candidates,make_request,build_document_set,compact_document_set,verify_request)
from dalton_core.sec_financials_launcher import SecFinancialsLauncher
from dalton_core.mission_statement_lane import MissionStatementLaneCoordinator
from dalton_core.company_model_annual_projection import verify_statement_filing,AnnualProjectionError
from dalton_core.statement_context import prepare_context
from dalton_core.store import content_hash,canonical_json
from tests import test_sec_issuer_reporting_profile as profiles
from tests import test_sec_filings_index_discovery as roots


class NativeAnnualPipelineTests(unittest.TestCase):
    setUp=profiles.ForeignProfileTests.setUp
    run_once=profiles.ForeignProfileTests.run_once
    adapter=profiles.ForeignProfileTests.adapter
    root=profiles.ForeignProfileTests.root
    profile=profiles.ForeignProfileTests.profile

    def body(self,annual='40-F'):
        today=date.today().isoformat()
        return json.dumps({'cik':roots.CIK,'filings':{'recent':{
            'accessionNumber':[roots.CIK+'-26-000001',roots.CIK+'-26-000002',roots.CIK+'-20-000001'],
            'form':[annual,'6-K','6-K'],'filingDate':[today,today,'2020-01-01'],
            'reportDate':['2025-12-31',today,'2020-01-01'],'primaryDocument':['annual.htm','news.htm','old.htm'],
            'isXBRL':[1,0,0],'isInlineXBRL':[1,0,0]}}}).encode()

    def admitted(self,annual='40-F'):
        profile=self.root(annual,raw=self.body(annual))
        missions=CoverageMissionAuthority(self.core)
        auth=missions.sec_lane_authorization_for_company(roots.ACN)
        candidates=annual_candidates(missions.connection,self.spool,profile,company_ref=roots.ACN,
                                     issuer=roots.CIK,as_of=date.today(),limit=1)
        request=make_request(authorization=auth,reporting_profile=profile,filings=candidates,as_of=date.today())
        dispatch=missions.queue_statement_dispatch(authorization=auth,form=annual,filing_limit=1,
                                                   native_request=request,native_spool=self.spool)
        gov=build_governance_record(KIND,approved_by='human:synthetic-owner',status='approved',
                                    effective_from='2026-01-01T00:00:00+00:00',version=6)
        govpath=self.state/'native-governance.json';govpath.write_text(json.dumps(gov))
        directory=self.state/'sec-native-statement-requests';directory.mkdir(exist_ok=True)
        packet={'dispatch_ref':dispatch['dispatch_id'],'request':request};packet['content_hash']=content_hash(packet)
        path=directory/(dispatch['dispatch_id'].split(':',1)[1]+'.json');path.write_text(json.dumps(packet))
        missions.mark_statement_dispatch_launched(dispatch['dispatch_id'],'sec-financials-run:'+'1'*24)
        return missions,request,dispatch,gov,govpath,path

    def captured(self,request):
        filing=request['filings'][0];issuer=request['reporting_profile']['issuer']
        base=f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{filing["accession"].replace("-","")}/'
        members=[{'document':name,'document_type':kind,'sequence':str(n+1),'description':kind,'url':base+name}
                 for n,(name,kind) in enumerate([('annual.htm',filing['form']),('facts.xml','EX-101.INS')])]
        originals={'annual.htm':b'<html>Annual statements</html>','facts.xml':b'<xbrl>Native source facts</xbrl>'}
        docset=build_document_set(issuer=issuer,filing=filing,reporting_profile_hash=request['reporting_profile']['content_hash'],
                                 members=members,originals=originals)
        statements={}
        for role,concept in [('income','ifrs-full:Revenue'),('balance','ifrs-full:Assets'),('cash','ifrs-full:CashFlowsFromUsedInOperatingActivities')]:
            structure=[{'concept':concept,'label':concept,'level':0,'abstract':False,'parent_concept':None,
                        'is_breakdown':False,'dimension_axis':None,'dimension_member':None,'balance':'credit'}]
            fact={'concept':concept,'period_start':None if role=='balance' else '2025-01-01',
                  'period_end':'2025-12-31','unit_ref':'USD','value':'100','balance':'credit',
                  'dimension':None,'member':None,'dim_example:UnusedAxis':None}
            statements[role]={'structure':structure,'facts':[fact]}
        return {'cik':issuer,'entity_name':'Synthetic foreign issuer','filings':[
            {k:filing[k] for k in ('accession','form','filed','report_date')}|{'statements':statements,'native_document_set':docset}]}

    def summary(self,request,govpath,path,raw):
        fixture=self.state/'native-parse.json';fixture.write_text(json.dumps(raw))
        args=build_parser().parse_args(['--state-dir',str(self.state),'--governance',str(govpath),
            '--ticker',request['authorization']['ticker'],'--form',request['filings'][0]['form'],'--limit','1',
            '--fixture-file',str(fixture),'--native-request',str(path),'--summary-dir',str(self.state/'native-summary')])
        return run(args)

    def durable_roundtrip(self,annual):
        m,r,d,g,gp,p=self.admitted(annual)
        raw=self.captured(r);s=self.summary(r,gp,p,raw)
        self.assertEqual(s['status'],'succeeded',s['failure_reason'])
        recorded=m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        m.settle_statement_dispatch(d['dispatch_id'],outcome='succeeded')
        f=recorded['filings'][0]
        self.assertEqual(verify_statement_filing(m.connection,f),'line_bound')
        reread=next(x for x in m.statement_filings(roots.ACN) if x['ingest_id']==f['ingest_id'])
        self.assertEqual(verify_statement_filing(m.connection,reread),'line_bound')
        context=prepare_context(m.connection,self.state,f['ingest_id'],discover_companyfacts=False)
        self.assertEqual(context['status'],'available',context['reason'])
        self.assertEqual(context['kind'],'ordinal_overlay')
        self.assertEqual({x['concept'] for x in m.statement_lines(f['ingest_id'])},{
            'ifrs-full:Revenue','ifrs-full:Assets','ifrs-full:CashFlowsFromUsedInOperatingActivities'})
        self.assertEqual(m.statement_coverage(roots.ACN)['held_by_form'][annual],1)
        self.assertNotIn('10-Q',m.statement_coverage(roots.ACN)['held_by_form'])


    def test_native_20f_child_durable_reader_and_context_keep_actual_annual_periods(self):
        self.durable_roundtrip("20-F")

    def test_native_40f_child_durable_reader_and_context_keep_actual_annual_periods(self):
        self.durable_roundtrip("40-F")

    def test_missing_original_wrong_native_header_and_incomplete_roles_never_record(self):
        m,r,d,g,gp,p=self.admitted()
        for reason in ('missing_original','wrong_issuer','wrong_end','missing_cash'):
            raw=self.captured(r)
            if reason=='missing_original':raw['filings'][0]['native_document_set']['originals'].pop()
            elif reason=='wrong_issuer':raw['cik']='0000000123'
            elif reason=='wrong_end':raw['filings'][0]['report_date']='2024-12-31'
            else:raw['filings'][0]['statements'].pop('cash')
            with self.subTest(reason=reason):
                s=self.summary(r,gp,p,raw);self.assertEqual(s['status'],'failed')
                self.assertEqual(m.statement_filings(roots.ACN),[])

    def test_valid_parser_body_cannot_be_replaced_by_rehashed_observation_values_or_source_proof(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r));self.assertEqual(s['status'],'succeeded')
        for changed in ('number','source','period'):
            observation=copy.deepcopy(s['observation'])
            if changed=='number':observation['filings'][0]['lines'][0]['value']='999'
            elif changed=='period':observation['filings'][0]['lines'][0]['period_start']='2025-07-01'
            else:
                value=observation['filings'][0]['native_document_set'];value['reporting_profile_hash']='b'*64
                value['content_hash']=content_hash({k:v for k,v in value.items() if k!='content_hash'})
            with self.subTest(changed=changed),self.assertRaises(CoverageMissionConflict):
                m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=observation,
                    governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        self.assertEqual(m.statement_filings(roots.ACN),[])

    def test_native_three_statement_inputs_keep_ifrs_concepts_durations_and_no_invented_quarters(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r))
        m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        from dalton_core.company_model_inputs import build_model_inputs
        from tests.test_company_model_inputs import _spec
        spec=_spec(drivers=[{'ref':'native-revenue','label':'Native revenue','kind':'mix',
            'basis_concept':'ifrs-full:Revenue','unit':'USD','because':'Original declared native concept'}],expenses=[])
        spec['schema_version']='0.4'
        spec['financial_statement_structure']={'schema_version':'0.4','lines':[
            {'ref':'native-'+role,'kind':'filed','statement':role,'concept':concept}
            for role,concept in [('income','ifrs-full:Revenue'),('balance','ifrs-full:Assets'),
                                 ('cash','ifrs-full:CashFlowsFromUsedInOperatingActivities')]],'formulas':[]}
        table=build_model_inputs(m,spec)
        filed={x['concept']:x for x in table['filed_lines']}
        self.assertEqual(set(filed),{'ifrs-full:Revenue','ifrs-full:Assets','ifrs-full:CashFlowsFromUsedInOperatingActivities'})
        self.assertTrue(filed['ifrs-full:Revenue']['duration_facts'])
        self.assertEqual(filed['ifrs-full:Revenue']['duration_facts'][0]['period_start'],'2025-01-01')
        self.assertFalse(filed['ifrs-full:Revenue']['cells'])
        self.assertEqual(filed['ifrs-full:Revenue']['source_evidence']['rows'][0]['accession'],r['filings'][0]['accession'])

    def test_native_admission_rejects_duplicated_complete_accession(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r))
        observation=copy.deepcopy(s['observation'])
        observation['filings'].append(copy.deepcopy(observation['filings'][0]))
        with self.assertRaises(CoverageMissionConflict):
            m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=observation,
                governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        self.assertEqual(m.statement_filings(roots.ACN),[])

    def test_durable_native_source_replay_does_not_depend_on_mutable_profile_spool(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r))
        f=m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)['filings'][0]
        # The original strong root is copied into the hashed native artifact.
        # Remove only the fixture spool root, not any production material.
        digest=r['reporting_profile']['raw_hash']
        (self.state/'connector-spool'/'objects'/digest[:2]/digest).unlink()
        self.assertEqual(verify_statement_filing(m.connection,f),'line_bound')

    def test_foundation_native_annual_label_never_turns_parsed_numbers_into_read_text(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r))
        m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        from dalton_core.mission_stage import evaluate_mission
        mission=m.mission(r['authorization']['mission_version_ref'])
        companies=evaluate_mission(m.connection,mission,planned_specs={'annual-report-40f'})
        item=next(i for c in companies if c['company_ref']==roots.ACN for i in c['items'] if i['item_ref']=='annual_report')
        self.assertEqual(item['label'],'最新年报（40-F）正文')
        self.assertEqual(item['have'],0);self.assertEqual(item['read'],0)

    def test_strong_native_profile_names_40f_gap_before_statement_acquisition(self):
        m,r,d,g,gp,p=self.admitted()
        directory=self.state/'connector-governance';directory.mkdir(exist_ok=True)
        (directory/'sec-financial-statements-v6.json').write_bytes(gp.read_bytes())
        from dalton_core.mission_stage import evaluate_mission
        companies=evaluate_mission(m.connection,m.mission(r['authorization']['mission_version_ref']),planned_specs={'annual-report-40f'})
        item=next(i for c in companies if c['company_ref']==roots.ACN for i in c['items'] if i['item_ref']=='annual_report')
        self.assertEqual(item['label'],'最新年报（40-F）正文');self.assertEqual(item['have'],0)
        self.assertNotEqual(item['status'],'complete')

    def test_acquired_native_annual_original_satisfies_correct_annual_item_without_fake_10k_or_read(self):
        m,r,d,g,gp,p=self.admitted();s=self.summary(r,gp,p,self.captured(r))
        m.record_statement_observation(dispatch_id=d['dispatch_id'],observation=s['observation'],
            governance_ref=g['id'],governance_hash=g['content_hash'],governance_proof=g)
        from dalton_core.sec_plan_specs import effective_sec_plan
        profile=self.profile()
        discovery=self.run_once(plan=effective_sec_plan(roots.sec_plan()),spec_ref='annual-report-40f',
            as_of=date.today(),reporting_profile=profile,adapter=self.adapter(self.body()))
        self.assertEqual(discovery['status'],'succeeded',discovery['failure_reason'])
        doc=next(x for x in m.discovered_documents(self.mission['id']) if x['document_ref']=='sec:filing:'+r['filings'][0]['accession'])
        m.mark_discovered_document_launched(doc['record_id'],'public-web-fetch:synthetic-native-annual')
        m.settle_discovered_document(doc['record_id'],status='acquired')
        from dalton_core.mission_stage import evaluate_mission
        companies=evaluate_mission(m.connection,m.mission(r['authorization']['mission_version_ref']),planned_specs={'annual-report-40f'})
        item=next(i for c in companies if c['company_ref']==roots.ACN for i in c['items'] if i['item_ref']=='annual_report')
        self.assertEqual(item['label'],'最新年报（40-F）正文');self.assertEqual(item['have'],1)
        self.assertEqual(item['read'],0);self.assertEqual(item['status'],'complete')

    def test_changed_profile_request_or_current_mission_cannot_authorize_new_child(self):
        m,r,d,g,gp,p=self.admitted()
        for field in ('authorization','reporting_profile'):
            value=copy.deepcopy(r)
            if field=='authorization':value[field]['mission_version_hash']='b'*64
            else:value[field]['issuer']='0000000123'
            value['content_hash']=content_hash({k:v for k,v in value.items() if k!='content_hash'})
            with self.assertRaises(ValueError):verify_request(m.connection,self.spool,value,current_authorization=r['authorization'])
        launcher=SecFinancialsLauncher(state_dir=self.state,governance_path=gp,mode_args=('--fixture-file','unused'))
        with patch.object(launcher,'spawn',return_value={'id':'sec-financials-run:'+'2'*24}) as spawn:
            ticket=launcher.start_native(missions=m,dispatch=d,actor_ref=d['actor_ref'])
            self.assertEqual(spawn.call_count,1)
        wrong=copy.deepcopy(d);wrong['actor_ref']='automation:foreign'
        with patch.object(launcher,'spawn') as spawn,self.assertRaises(Exception):
            launcher.start_native(missions=m,dispatch=wrong,actor_ref='automation:foreign');spawn.assert_not_called()

    def test_natural_lane_chooses_native_annual_from_profile_and_old_domestic_failures_do_not_charge_it(self):
        profile=self.root(raw=self.body());m=CoverageMissionAuthority(self.core)
        gov=build_governance_record(KIND,approved_by='human:synthetic-owner',status='approved',version=6)
        gp=self.state/'gov6.json';gp.write_text(json.dumps(gov))
        launcher=SecFinancialsLauncher(state_dir=self.state,governance_path=gp,mode_args=('--fixture-file','unused'))
        c=MissionStatementLaneCoordinator(missions=m,launcher=launcher,checklist=lambda:[{'company_ref':roots.ACN,'ticker':'ACN'}],
             forms=('10-K','10-Q'),clock=lambda:datetime.now(timezone.utc))
        outcomes=c._queue();self.assertEqual(len(outcomes),1,outcomes);self.assertEqual(outcomes[0]['form'],'40-F')
        dispatch=m.pending_statement_dispatches()[0]
        self.assertEqual(dispatch['attempt'],0)
        self.assertIsNotNone(m.native_statement_request(dispatch['dispatch_id']))
        self.assertEqual(c._queue(),[])

if __name__=='__main__':unittest.main()
