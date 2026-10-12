"""Real held filing/context prerequisites get one bounded fair opportunity."""
from copy import deepcopy
import sqlite3
import time
import unittest
from unittest.mock import patch
from tests import test_ar_segment_bridge_identity as fixtures
from dalton_core import material_requests as mr, material_acquisition as ma
from dalton_core import material_current_gap_priority as priority
from dalton_core.material_model_unit_priority import unit_gap_request_fields, unit_input_proof
from dalton_core.sec_filing_units import open_unit_gap_request
from dalton_core.sec_financials_normalise import normalise_filing
from dalton_core.statement_context import StatementContextAuthority
from dalton_core.store import DaltonStore, canonical_json


class ModelUnitPriorityTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.ContextBackfillTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        payload={'cik':'0000999999','entity_name':'Synthetic Company','filings':[{
            'accession':'0000999999-26-000001','form':'10-Q','filed':'2026-08-01','report_date':'2026-06-30',
            'statements':{'income':{'structure':[{'concept':'us-gaap_Revenues','label':'Revenue','level':1,'parent_concept':None,'abstract':False,'is_breakdown':False,'balance':'credit'}],
                'facts':[{'concept':'us-gaap:Revenues','period_start':'2026-04-01','period_end':'2026-06-30','value':'1234500000','unit_ref':'opaque-dollar','balance':'credit','dim_srt_ConsolidationItemsAxis':None}]}}}]}
        self.fx.raw_parse(payload)
        lines=normalise_filing(**payload['filings'][0])['lines']
        self.filing,_=self.fx.ingest(lines)
        self.state=self.fx.state_dir
        dest=sqlite3.connect(self.state/'core.sqlite')
        try:self.fx.store.connection.backup(dest)
        finally:dest.close()
        self.store=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.store.close)
        self.context=StatementContextAuthority(self.store).backfill(self.state,self.filing['ingest_id'],discover_companyfacts=False)
        self.assertEqual(self.context['status'],'available')
        self.request=open_unit_gap_request(self.state,self.filing,self.context)
        self.assertIsNotNone(self.request)

    def read(self):
        return priority.priority_gaps(self.state,mr.open_requests(self.state),deadline=time.monotonic()+5)

    def test_real_missing_unit_preflight_qualifies_without_a_formal_spec(self):
        found,info=self.read();self.assertEqual(info['qualified'],1)
        proof=found[self.request['id']]
        self.assertEqual(proof['unit_input']['context_ref'],self.context['version_id'])
        self.assertEqual(proof['unit_input']['filing_hash'],self.filing['content_hash'])
        self.assertIsNone(proof['spec_ref'])
        self.assertTrue(priority.still_current(self.state,proof,deadline=time.monotonic()+5))
        self.assertEqual(mr.request(self.state,self.request['id'])['attempt_count'],0)

    def test_descriptor_parity_with_actual_writer_across_forms_and_statuses(self):
        for form in ('10-K','10-Q','10-Q/A'):
            filing={**self.filing,'form':form}
            for context in (self.context,{**self.context,'status':'refused'}, {'status':'available','lines':[],'unresolved_unit_refs':[]}):
                with self.subTest(form=form,status=context['status']),patch('dalton_core.material_requests.open_request') as writer:
                    open_unit_gap_request(self.state,filing,context)
                    fields=unit_gap_request_fields(filing,context)
                    if fields is None:writer.assert_not_called()
                    else:self.assertEqual(writer.call_args.kwargs,fields)

    def test_forged_caller_view_is_reread_not_promoted(self):
        view=mr.request(self.state,self.request['id'])
        fake=deepcopy(view);fake['acceptance']['cik']='0000000001';fake['period']='2030-01-01'
        found,_=priority.priority_gaps(self.state,[fake],deadline=time.monotonic()+5)
        self.assertEqual(found[self.request['id']]['unit_input']['cik'],self.filing['cik'])
        self.assertEqual(found[self.request['id']]['unit_input']['period'],self.filing['report_date'])

    def test_wrong_issuer_date_source_and_context_are_not_current_prerequisites(self):
        mission=__import__('json').loads(self.store.connection.execute('SELECT record_json FROM coverage_mission_versions ORDER BY version_number DESC LIMIT 1').fetchone()[0])
        request=mr.request(self.state,self.request['id'])
        for field,value in (('cik','0000000001'),('date_from','2026-07-31'),('document_ref','sec:filing:other')):
            changed=deepcopy(request);changed['acceptance'][field]=value
            with self.subTest(field=field):
                self.assertIsNone(unit_input_proof(self.store.connection,changed,mission,contract_ref=priority.CONTRACT_REF,cache={}))
        changed=deepcopy(request);changed['requesters'][0]['run_ref']='statement-context:wrong'
        self.assertIsNone(unit_input_proof(self.store.connection,changed,mission,contract_ref=priority.CONTRACT_REF,cache={}))
        changed=deepcopy(request);changed['source_priority']=['sec_edgar']
        self.assertIsNone(unit_input_proof(self.store.connection,changed,mission,contract_ref=priority.CONTRACT_REF,cache={}))

    def test_closed_or_foreign_mission_unit_gap_loses_priority(self):
        found,_=self.read();proof=found[self.request['id']]
        mission=__import__('json').loads(self.store.connection.execute('SELECT record_json FROM coverage_mission_versions ORDER BY version_number DESC LIMIT 1').fetchone()[0]);mission=deepcopy(mission);mission['universe']=[]
        request=mr.request(self.state,self.request['id'])
        self.assertIsNone(unit_input_proof(self.store.connection,request,mission,contract_ref=priority.CONTRACT_REF,cache={}))
        mr.close(self.state,self.request['id'],status='exhausted',reason='fixture',now='2026-10-12T00:00:00+00:00')
        self.assertEqual(self.read()[0],{})
        self.assertEqual(proof['unit_input']['context_hash'],self.context['content_hash'])

    def test_strong_context_hash_tamper_refuses_priority(self):
        self.store.connection.execute('DROP TRIGGER statement_context_no_update')
        self.store.connection.execute('UPDATE statement_context_versions SET content_hash=?',('f'*64,))
        self.store.connection.commit()
        found,info=self.read();self.assertEqual(found,{})
        self.assertEqual(info['status'],'unavailable')

    def test_current_context_pointer_change_invalidates_claim(self):
        proof=self.read()[0][self.request['id']]
        # An unverified new context is never evidence for the old request.
        with StatementContextAuthority(self.store)._transaction() as cursor:
            cursor.execute('INSERT INTO statement_context_versions VALUES(?,?,?,?,?,?)',
                ('context:new',self.filing['ingest_id'],'available',canonical_json({'status':'refused'}),'f'*64,'2100-01-01'))
        self.assertFalse(priority.still_current(self.state,proof,deadline=time.monotonic()+5))
        self.assertEqual(self.read()[0],{})

    def test_real_priority_has_one_slot_and_preserves_three_ordinary_slots(self):
        for n in range(10):mr.open_request(self.state,requester={'lane':'research_planner','run_ref':str(n)},subject=fixtures.COMPANY,need={'kind':'answer','question':f'Ordinary {n}'})
        with patch.object(ma,'serve',side_effect=lambda state,ref,**kw:{'request_id':ref,'outcome':'waiting'}):
            result=ma.tick(self.state,convert=False,now='2026-10-12T00:00:00+00:00')
        self.assertEqual(result['served'],4)
        urgent=[r for r in result['results'] if r['selection_class']=='current_product_gap']
        self.assertEqual([r['request_id']for r in urgent],[self.request['id']])
        self.assertEqual(sum(r['selection_class']=='ordinary_round_robin'for r in result['results']),3)
        self.assertEqual(mr.request(self.state,self.request['id'])['attempt_count'],0)
