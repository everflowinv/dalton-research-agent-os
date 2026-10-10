"""Exact XML gaps enter the normal material ladder and acquisition refresh."""
import sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from dalton_core.sec_filing_units import open_unit_gap_request,unit_requirement_status,RULE_REF
from dalton_core.material_acquisition import evaluate_acceptance
from dalton_core.coverage_mission import CoverageMissionAuthority

class UnitAcquisitionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.filing={'ingest_id':'statement-ingest:synthetic','company_ref':'company:ticker:syn','cik':'0000000012','accession':'0000000012-26-000001','form':'10-Q','filed':'2026-04-20','report_date':'2026-03-31'}
  self.record={'version_id':'context:synthetic','status':'available','lines':[{'unit':'opaque-currency'}]}
 def request(self):return open_unit_gap_request(self.root,self.filing,self.record)
 def test_request_is_exact_original_and_idempotent_without_model_budget(self):
  a=self.request();b=self.request();self.assertEqual(a['id'],b['id']);self.assertEqual(b['write_status'],'duplicate')
  self.assertEqual(a['acceptance']['document_ref'],'sec:filing:'+self.filing['accession']);self.assertEqual(a['acceptance']['unit_proof_rule_ref'],RULE_REF);self.assertEqual(a['source_priority'],['indexed_corpus','sec_edgar']);self.assertEqual(a['acceptance']['date_from'],self.filing['filed'])
 def test_no_false_request_when_canonical_or_refused_context(self):
  for record in ({**self.record,'status':'refused'},{**self.record,'lines':[{'unit':'USD'},{'unit':'shares'}]}):self.assertIsNone(open_unit_gap_request(self.root,self.filing,record))
 def test_indexed_passage_cannot_close_exact_xml_unit_gap(self):
  request=self.request()
  with patch('dalton_core.evidence_index.passages_for_document',return_value=[{'quote':'some revenue','document_ref':request['acceptance']['document_ref']}]) as lookup:
   result=evaluate_acceptance(self.root,request,identity={});self.assertFalse(result['met']);lookup.assert_not_called()
 def test_unknown_typed_marker_cannot_be_treated_as_ordinary_document(self):
  request=self.request();request['acceptance']['unit_proof_rule_ref']='unsupported'
  with patch('dalton_core.evidence_index.passages_for_document',return_value=[{'quote':'text'}]) as lookup:
   self.assertFalse(evaluate_acceptance(self.root,request,identity={})['met']);lookup.assert_not_called()
 def test_ordinary_exact_document_acceptance_remains_compatible(self):
  request=self.request();request['acceptance'].pop('unit_proof_rule_ref')
  with patch('dalton_core.evidence_index.passages_for_document',return_value=[{'quote':'ordinary text','document_ref':request['acceptance']['document_ref']}]) as lookup:
   self.assertTrue(evaluate_acceptance(self.root,request,identity={})['met']);lookup.assert_called_once()
 def test_only_exact_bound_complete_snapshot_meets_unit_need(self):
  request=self.request();c=sqlite3.connect(self.root/'core.sqlite');c.execute('create table coverage_mission_statement_filings(ingest_id,company_ref,accession,cik)');c.execute('insert into coverage_mission_statement_filings values(?,?,?,?)',tuple(self.filing[k] for k in ('ingest_id','company_ref','accession','cik')));c.commit();c.close()
  record={'content_hash':'a'*64,'lines':[{'unit':'USD'}],'supplementary_units':{'evidence':[{'unit':'USD'}],'raw_sha256':'b'*64,'source':{'manifest_hash':'c'*64}}}
  with patch('dalton_core.statement_context._latest',return_value='context:bound'),patch('dalton_core.statement_context.context_record',return_value=record) as reader:
   result=unit_requirement_status(self.root,request);self.assertTrue(result['met']);self.assertEqual(result['items'][0]['statement_context_ref'],'context:bound');self.assertEqual(result['items'][0]['raw_sha256'],'b'*64)
   request['acceptance']['accession']='0000000012-26-000002';self.assertFalse(unit_requirement_status(self.root,request)['met'])
 def test_acquired_original_refreshes_exact_ingest_and_replays_after_commit(self):
  c=sqlite3.connect(self.root/'core.sqlite');c.execute('create table coverage_mission_statement_filings(ingest_id,company_ref,accession)');c.execute('insert into coverage_mission_statement_filings values(?,?,?)',tuple(self.filing[k] for k in ('ingest_id','company_ref','accession')));c.commit()
  a=object.__new__(CoverageMissionAuthority);a.connection=c;a.store=SimpleNamespace(path=str(self.root/'core.sqlite'))
  result={'status':'acquired','source_ref':'source:sec-edgar','document_ref':'sec:filing:'+self.filing['accession'],'company_ref':self.filing['company_ref']}
  a._settle_discovered_document_record=Mock(return_value=result)
  with patch('dalton_core.statement_context.refresh_company_contexts',return_value=[{'status':'available'}]) as refresh:
   observed=a.settle_discovered_document('doc:synthetic',status='acquired');self.assertEqual(observed['statement_contexts'],[{'status':'available'}]);refresh.assert_called_once_with(a.store,self.root,self.filing['company_ref'],ingest_ids=[self.filing['ingest_id']],force=True)
   a.settle_discovered_document('doc:synthetic',status='acquired');self.assertEqual(refresh.call_count,2)
  c.close()
 def test_sec_10q_normal_rung_enqueues_exact_material_need(self):
  from dalton_core.material_acquisition import TargetedSourceRung,RungContext,build_queries,identity_for,pending_targeted_queries
  request=self.request();identity=identity_for(self.root,request['subject']);queries=build_queries(request,identity,now='2026-10-10T12:00:00+00:00')
  ctx=RungContext(state_dir=self.root,request=request,identity=identity,queries=queries,now='2026-10-10T12:00:00+00:00')
  rung=TargetedSourceRung('sec_edgar');result=rung.run(ctx);self.assertEqual(result.outcome,'dispatched');pending=pending_targeted_queries(self.root,'source:sec-edgar');self.assertEqual(pending[0]['request_id'],request['id']);self.assertEqual(pending[0]['spec_ref'],'quarterly-report-10q')
 def test_overlay_unit_gap_also_opens_document_request(self):
  record={**self.record};record.pop('lines');record['unresolved_unit_refs']=['opaque-currency'];self.assertIsNotNone(open_unit_gap_request(self.root,self.filing,record))
 def test_new_sec_plan_only_adds_10q_budget_and_scope_unchanged(self):
  from dalton_core.mission_source_discovery import build_discovery_plan,build_discovery_parameters
  from scripts.build_sec_8k_discovery_proposal import build_candidate_plan
  from datetime import date
  from tests.test_sec_8k_discovery_proposal import Sec8KDiscoveryProposalTests
  from dalton_core.mission_source_discovery import validate_discovery_plan
  from dalton_core.store import content_hash
  base=Sec8KDiscoveryProposalTests().active_plan();base.pop('content_hash');base['companies']={self.filing['company_ref']:{'cik':self.filing['cik']}};base=validate_discovery_plan({**base,'content_hash':content_hash(base)})
  spec={'spec_ref':'quarterly-report-10q','form':'10-Q','lookback_days':1096,'rediscovery_interval_days':7,'retry_interval_days':1}
  candidate=build_candidate_plan(base,created_at='2026-10-10T12:00:00+00:00',spec=spec);self.assertEqual(candidate['budget'],base['budget']);self.assertEqual(candidate['companies'],base['companies']);self.assertEqual(candidate['mission_ref'],base['mission_ref']);self.assertEqual(candidate['specs'][:-1],base['specs']);self.assertEqual(build_discovery_parameters(candidate,spec_ref='quarterly-report-10q',company_ref=self.filing['company_ref'],as_of=date(2026,10,10))['form'],'10-Q')
 def test_missing_unit_preflight_returns_before_any_paid_model_constructor(self):
  from tests.test_company_model_cli import ChooseCompanyTests
  from dalton_core.company_model_cli import run_model_spec
  h=ChooseCompanyTests('test_a_company_with_no_statements_is_skipped_not_chosen');h.setUp()
  try:
   with patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'refreshed':True,'unresolved':[{'ingest_id':'filing','source_unit_refs':['opaque'],'material_request_ref':'material:exact'}]}),patch('dalton_core.company_model_cli.CockpitModel') as model:
    r=run_model_spec(state_dir=h.state_dir,company_ref=h.company_ref if hasattr(h,'company_ref') else 'company:sec-cik:0001467373',model_config_path=None,summary_dir=h.state_dir/'summaries',scheduler_db=h.state_dir/'scheduler.sqlite')
   model.assert_not_called();self.assertEqual(r['cost_micros'],0);self.assertEqual(r['spec_status'],'refused');self.assertTrue(r['unit_material_gaps'])
  finally:h.doCleanups()

 def test_context_refresh_failure_never_changes_acquired_or_masks_formal_failure(self):
  c=sqlite3.connect(self.root/'core.sqlite');c.execute('create table coverage_mission_statement_filings(ingest_id,company_ref,accession)');c.execute('insert into coverage_mission_statement_filings values(?,?,?)',tuple(self.filing[k] for k in ('ingest_id','company_ref','accession')));c.commit();self.addCleanup(c.close)
  a=object.__new__(CoverageMissionAuthority);a.connection=c;a.store=SimpleNamespace(path=str(self.root/'core.sqlite'))
  row={'status':'acquired','source_ref':'source:sec-edgar','document_ref':'sec:filing:'+self.filing['accession'],'company_ref':self.filing['company_ref']}
  a._settle_discovered_document_record=Mock(return_value=dict(row))
  with patch('dalton_core.statement_context.refresh_company_contexts',side_effect=OSError('synthetic unavailable disk')):
   result=a.settle_discovered_document('doc:synthetic',status='acquired');self.assertEqual(result['status'],'acquired');self.assertEqual(result['statement_context_refresh']['status'],'refused');self.assertIn('OSError',result['statement_context_refresh']['reason']);self.assertNotIn('failure_retryable',result)
  with patch('dalton_core.statement_context.refresh_company_contexts',return_value=[]) as refresh:
   a.settle_discovered_document('doc:synthetic',status='acquired');refresh.assert_called_once()
  a._settle_discovered_document_record.side_effect=ValueError('formal acquisition failed')
  with patch('dalton_core.statement_context.refresh_company_contexts') as refresh:
   with self.assertRaisesRegex(ValueError,'formal acquisition failed'):a.settle_discovered_document('doc:synthetic',status='acquired')
   refresh.assert_not_called()

 def test_normal_settle_receipt_carries_context_failure_without_retrying_acquisition(self):
  from tests.test_mission_source_discovery import CoordinatorTests,seed_known_document
  h=CoordinatorTests('test_backfill_recovers_acquired_document_after_interrupted_registration');h.setUp()
  try:
   h.mission_v2(h.create_mission());seed_known_document(h.h);h.coordinator.dispatch_once();h.coordinator.dispatch_once();h.acquisition_launcher.finish()
   original=h.missions.settle_discovered_document
   def settle(*args,**kwargs):
    row=original(*args,**kwargs);return {**row,'statement_context_refresh':{'status':'refused','reason':'OSError: synthetic disk','retry_via':'model_unit_preflight'}}
   with patch.object(h.missions,'settle_discovered_document',side_effect=settle):
    settled=h.coordinator.settle_documents()
   self.assertEqual(settled[0]['status'],'acquired');self.assertEqual(settled[0]['statement_context_refresh']['status'],'refused');self.assertEqual(h.coordinator.settle_documents(),[])
  finally:h.doCleanups()
