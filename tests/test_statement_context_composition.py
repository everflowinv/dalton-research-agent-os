"""Two exact sources compose new context hashes; old records are not repaired."""
import copy,hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.store import canonical_json,content_hash
from dalton_core.raw_spool import RawSpool
from dalton_core.statement_companyfacts_context import supplemental_lines,validate_supplement,rebind_supplement_after_units
from dalton_core.sec_filing_units import supplemental_unit_lines,validate_unit_supplement,FilingUnitError, RULE_REF
from tests.test_sec_filing_unit_context import XML,row

class CompositionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.state=Path(self.tmp.name)
  self.c=sqlite3.connect(self.state/'core.sqlite');self.addCleanup(self.c.close)
  self.filing={'cik':'12','accession':'0000000012-26-000001','filed':'2026-04-10','form':'10-Q','report_date':'2026-03-31'}
  self.xml=XML.replace('xmlns:g=', 'xmlns:us-gaap=').replace('g:Revenue','us-gaap:Revenues').replace('g:Shares','us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding').replace('id="opaque-share"','id="shares"').replace('unitRef="opaque-share"','unitRef="shares"').encode()
  fact={'accn':self.filing['accession'],'filed':self.filing['filed'],'form':'10-Q','start':'2026-01-01','end':'2026-03-31','val':100}
  payload={'cik':12,'facts':{'us-gaap':{'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[fact]}}}}}
  raw=canonical_json(payload).encode();digest=hashlib.sha256(raw).hexdigest()
  spool=RawSpool(str(self.state/'raw-spool'),max_total_bytes=1000000);sink=spool.open_sink('raw-sink:'+digest,max_response_bytes=100000);sink.write(raw);sink.finalize()
  self.before,self.proof=supplemental_lines(self.state,self.filing,[row(concept='us-gaap:Revenues')],{'raw_hash':digest,'facts':payload['facts']})
  self.source={'record_id':'synthetic-original','raw_sha256':hashlib.sha256(self.xml).hexdigest()}
  with patch('dalton_core.sec_filing_units._source',return_value=(self.source,self.xml)):
   self.after,self.units=supplemental_unit_lines(self.c,self.state,self.filing,self.before)
 def record(self):return {'kind':'complete_snapshot','lines':self.after,'supplementary_units':self.units}
 def rebind(self,before=None,after=None,proof=None,units=None):return rebind_supplement_after_units(proof or self.proof,before or self.before,after or self.after,units or self.units,self.filing)
 def test_both_strong_proofs_pass_and_old_hash_remains_refused(self):
  record=self.record()
  with patch('dalton_core.sec_filing_units._source',return_value=(self.source,self.xml)):validate_unit_supplement(self.c,record,self.filing)
  with self.assertRaises(ValueError):validate_supplement({**record,'supplementary_companyfacts':self.proof},self.filing)
  prior=copy.deepcopy(self.proof);updated=self.rebind()
  validate_supplement({**record,'supplementary_companyfacts':updated},self.filing)
  self.assertEqual(self.proof,prior);self.assertNotEqual(updated['evidence_hash'],prior['evidence_hash'])
  self.assertEqual(updated['evidence'][0]['fact'],prior['evidence'][0]['fact'])
 def test_prior_companyfacts_hash_tamper_refuses_even_self_consistent_evidence_hash(self):
  bad=copy.deepcopy(self.proof);bad['evidence'][0]['line_hash']='a'*64;bad['evidence_hash']=content_hash(bad['evidence'])
  with self.assertRaises(ValueError):self.rebind(proof=bad)
 def test_overlay_cannot_change_number_period_concept_issuer_or_unrelated_field(self):
  for key,value in [('value','101'),('period_end','2026-04-01'),('concept','us-gaap:Other'),('dimensions',{'axis':'member'}),('label','invented'),('company_ref','company:foreign')]:
   with self.subTest(key=key):
    bad=copy.deepcopy(self.after);bad[1][key]=value
    with self.assertRaises(ValueError):self.rebind(after=bad)
 def test_unproved_source_unit_or_definition_hash_refuses(self):
  for key in ['source_unit_ref','unit_definition_hash']:
   bad=copy.deepcopy(self.after);bad[1][key]='forged'
   with self.assertRaises(ValueError):self.rebind(after=bad)
 def test_self_consistent_unit_proof_tamper_is_rejected_by_original_xml(self):
  r=copy.deepcopy(self.record());r['lines'][1]['value']='101';e=r['supplementary_units']['evidence'][1];e['value']='101';e['content_hash']=content_hash({k:v for k,v in e.items() if k!='content_hash'});r['lines'][1]['unit_definition_hash']=e['content_hash'];r['supplementary_units']['evidence_hash']=content_hash(r['supplementary_units']['evidence'])
  with patch('dalton_core.sec_filing_units._source',return_value=(self.source,self.xml)):
   with self.assertRaises(FilingUnitError):validate_unit_supplement(self.c,r,self.filing)
 def test_missing_unit_binding_or_changed_unrelated_line_cannot_rebind(self):
  u=copy.deepcopy(self.units);u['evidence']=u['evidence'][:1];u['evidence_hash']=content_hash(u['evidence'])
  with self.assertRaises(ValueError):self.rebind(units=u)
  bad=copy.deepcopy(self.after);bad[0]['value']='999'
  with self.assertRaises(ValueError):self.rebind(after=bad)

class PrepareAppendTests(unittest.TestCase):
 def test_prepare_appends_new_composed_context_and_strong_reader_replays(self):
  from tests.test_ar_segment_bridge_identity import ContextBackfillTests
  from dalton_core.store import DaltonStore
  from dalton_core.statement_context import StatementContextAuthority,context_record,normalizer_hash
  from dalton_core.sec_financials_normalise import normalise_filing
  f=ContextBackfillTests('test_context_overlay_is_exact_append_only_and_preserves_old_proof_resolution');f.setUp();self.addCleanup(f.doCleanups)
  payload={'cik':'0000999999','entity_name':'Synthetic Company','filings':[{'accession':'0000999999-26-000001','form':'10-Q','filed':'2026-08-01','report_date':'2026-06-30','statements':{'income':{'structure':[{'concept':'us-gaap_Revenues','label':'Revenue','level':1,'parent_concept':None,'abstract':False,'is_breakdown':False,'balance':'credit'}],'facts':[{'concept':'us-gaap:Revenues','period_start':'2026-04-01','period_end':'2026-06-30','value':'1234500000','unit_ref':'opaque-dollar','balance':'credit','dim_srt_ConsolidationItemsAxis':None}]}}}]}
  f.raw_parse(payload);rawfiling=payload['filings'][0]
  lines=normalise_filing(accession=rawfiling['accession'],form='10-Q',filed=rawfiling['filed'],report_date=rawfiling['report_date'],statements=rawfiling['statements'])['lines']
  filing,_=f.ingest(lines)
  db=f.state_dir/'core.sqlite';backup=sqlite3.connect(db);f.store.connection.backup(backup);backup.close()
  store=DaltonStore(str(db));self.addCleanup(store.close)
  fact={'start':'2026-04-01','end':'2026-06-30','val':100,'accn':filing['accession'],'filed':filing['filed'],'form':'10-Q'}
  data={'cik':999999,'facts':{'us-gaap':{'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[fact]}}}}};raw=canonical_json(data).encode();digest=hashlib.sha256(raw).hexdigest()
  spool=RawSpool(str(f.state_dir/'raw-spool'),max_total_bytes=1000000);sink=spool.open_sink('raw-sink:'+digest,max_response_bytes=100000);sink.write(raw);sink.finalize();source={'raw_hash':digest,'facts':data['facts']}
  xml=XML.replace('xmlns:g=','xmlns:us-gaap=').replace('g:Revenue','us-gaap:Revenues').replace('g:Shares','us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding').replace('id="opaque-share"','id="shares"').replace('unitRef="opaque-share"','unitRef="shares"').replace('>12<','>999999<').replace('2026-01-01','2026-04-01').replace('2026-03-31','2026-06-30').encode()
  with patch('dalton_core.sec_filing_units._source',return_value=({'record_id':'original'},xml)):
   authority=StatementContextAuthority(store);record=authority.backfill(f.state_dir,filing['ingest_id'],companyfacts_source=source,discover_companyfacts=False)
   self.assertEqual(record['status'],'available',record.get('reason'))
   held=context_record(store.connection,record['version_id']);self.assertEqual(held,record)
   self.assertEqual(held['normalizer_hash'],normalizer_hash())
   replay=authority.backfill(f.state_dir,filing['ingest_id'],companyfacts_source=source,discover_companyfacts=False)
   self.assertEqual(replay['version_id'],held['version_id'])
   self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM statement_context_versions').fetchone()[0],1)
