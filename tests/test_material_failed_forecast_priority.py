"""Exact current first-forecast failures get service, not retry permission."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from dalton_core import material_failed_forecast_priority as failed
from dalton_core import material_current_gap_priority as priority, material_requests as mr
from dalton_core.forecast_material_gaps import open_gap_requests
from dalton_core.economic_invariants import FORECAST_INVARIANT_CONTRACT_HASH
from dalton_core.store import canonical_json, content_hash
from tests import test_material_current_gap_priority as proof_fixtures
from tests.test_material_acquisition import SYNW, OTHER


class FailedForecastPriorityTests(unittest.TestCase):
    def setUp(self):
        self.fx=proof_fixtures.CurrentGapProofTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.state=self.fx.state;self.mission=self.fx.mission
        self.report={'status':'unexecuted','drivers':[{'ref':'segment','status':'unexecuted','reason':'missing exact history'}],
                     'formulas':[],'publication_blockers':['missing exact history']}
        self.table={'periods':['2026-06-30']};self.digest='d'*64;self.contract='e'*64
        self.reason='ForecastDriverExecutionRefused: missing exact history'
        self.spec=self.fx.spec(intrinsic=False)
        self.request=open_gap_requests(self.state,self.spec,self.table,record={'execution_contract':{'drivers':self.report}})[0]
        self.write_ticket()
        self.expected=self.expect(self.spec,self.request)

    def expect(self,spec,request):
        return {'contract_ref':priority.CONTRACT_REF,'company_ref':spec['company_ref'],'request_ref':request['id'],
            'request_hash':mr.request(self.state,request['id'])['content_hash'],'mission_ref':self.mission['id'],
            'mission_hash':self.mission['content_hash'],'spec_ref':spec['spec_id'],'spec_hash':spec['content_hash']}

    def write_ticket(self,spec=None,request=None):
        spec=spec or self.spec;request=request or self.request
        suffix=hashlib.sha256(f'model-forecast-run|{spec["company_ref"]}|{self.digest}|{FORECAST_INVARIANT_CONTRACT_HASH}'.encode()).hexdigest()[:24]
        directory=self.state/'model-forecast-runs'/suffix;directory.mkdir(parents=True,exist_ok=True);self.directory=directory
        ticket={'id':'model-forecast-run:'+suffix,'company_ref':spec['company_ref'],'model_digest':self.digest,
                'validator_contract_hash':FORECAST_INVARIANT_CONTRACT_HASH,'status':'succeeded','exit_code':0}
        summary={'status':'succeeded','company_ref':spec['company_ref'],'spec_ref':spec['spec_id'],'digest':self.digest,
            'forecast_status':'refused:unexecuted_driver_plan','driver_execution':self.report,
            'execution_contract_hash':self.contract,'failure_reason':self.reason,
            'material_requests':[{'id':request['id'],'gap_key':'economic_driver_history'}]}
        for name,wire in [('ticket.json',ticket),('summary.json',summary)]:
            p=directory/name;p.write_text(canonical_json(wire));p.chmod(0o600)

    def replay(self,*args,**kwargs):
        return self.table,self.digest,self.report,self.contract,self.reason

    def prove(self,expected=None):
        with patch.object(failed,'_replay',side_effect=self.replay):
            try:
                return failed._prove(self.state,expected or self.expected,deadline=time.monotonic()+4)
            except (ValueError,OSError,sqlite3.Error):
                return None

    def edit(self,file,key,value):
        p=self.directory/file;wire=json.loads(p.read_text());wire[key]=value;p.write_text(canonical_json(wire))

    def test_exact_failure_multiple_companies_and_no_production_writes(self):
        for company in (SYNW,OTHER):
            spec=self.spec if company==SYNW else self.fx.spec(company,intrinsic=False)
            request=self.request if company==SYNW else open_gap_requests(self.state,spec,self.table,record={'execution_contract':{'drivers':self.report}})[0]
            self.write_ticket(spec,request);proof=self.prove(self.expect(spec,request))
            self.assertEqual(proof['basis'],failed.BASIS);self.assertEqual(proof['company_ref'],company)
            self.assertEqual(proof['inputs_hash'],content_hash(self.table));self.assertEqual(proof['driver_report_hash'],content_hash(self.report))
            self.assertEqual(mr.request(self.state,request['id'])['attempt_count'],0)

    def test_summary_cannot_self_certify_different_report_contract_or_reason(self):
        for key,value in [('driver_execution',{'status':'unexecuted'}),('execution_contract_hash','0'*64),
                          ('failure_reason','invented failure'),('digest','0'*64),('spec_ref','old')]:
            with self.subTest(key=key):
                self.write_ticket();self.edit('summary.json',key,value);self.assertIsNone(self.prove())
        self.write_ticket()
        with patch.object(failed,'_replay',return_value=None):
            self.assertIsNone(failed._prove(self.state,self.expected,deadline=time.monotonic()+3))

    def test_running_or_failed_or_wrong_contract_ticket_is_not_proof(self):
        for key,value in [('status','running'),('status','failed'),('exit_code',1),('company_ref',OTHER),
                          ('validator_contract_hash','0'*64),('model_digest','0'*64)]:
            with self.subTest(key=key):
                self.write_ticket();self.edit('ticket.json',key,value);self.assertIsNone(self.prove())

    def test_current_input_changed_digest_and_rehashed_summary_still_refused(self):
        self.edit('summary.json','driver_execution',{'status':'unexecuted','drivers':[]})
        self.assertIsNone(self.prove())
        self.write_ticket()
        with patch.object(failed,'_replay',return_value=({'periods':['2026-09-30']},'f'*64,self.report,self.contract,self.reason)):
            with self.assertRaises(FileNotFoundError):
                failed._prove(self.state,self.expected,deadline=time.monotonic()+4)

    def test_new_spec_mission_and_closed_request_refuse_before_replay(self):
        self.fx.spec(number=2,intrinsic=False)
        with patch.object(failed,'_replay')as replay:
            self.assertIsNone(failed._prove(self.state,self.expected,deadline=time.monotonic()+3));replay.assert_not_called()
        self.expected['spec_ref']='spec:missing'
        self.assertIsNone(self.prove())
        self.expected['spec_ref']=self.spec['spec_id'];self.expected['mission_hash']='0'*64
        self.assertIsNone(self.prove())
        self.expected['mission_hash']=self.mission['content_hash'];mr.close(self.state,self.request['id'],status='exhausted',reason='no material')
        self.assertIsNone(self.prove())

    def test_wrong_request_accepted_source_and_forged_request_hash_are_not_priority(self):
        with mr._writer(self.state)as c:
            # Corrupt an isolated immutable authority to exercise the proof gate.
            c.execute('DROP TRIGGER material_requests_no_update')
            row=c.execute('SELECT record_json FROM material_requests WHERE request_id=?',(self.request['id'],)).fetchone()
            wire=json.loads(row['record_json']);wire['acceptance']['economic_driver_identities'][0]['label']='Other slice'
            wire['content_hash']=content_hash({k:v for k,v in wire.items()if k!='content_hash'})
            c.execute('UPDATE material_requests SET record_json=?,content_hash=? WHERE request_id=?',
                (canonical_json(wire),wire['content_hash'],self.request['id']))
        expected={**self.expected,'request_hash':wire['content_hash']}
        self.assertIsNone(self.prove(expected))
        self.assertIsNone(self.prove({**self.expected,'request_hash':'0'*64}))

    def test_private_bounded_artifacts_and_missing_artifact_refuse(self):
        self.directory.joinpath('summary.json').chmod(0o644);self.assertIsNone(self.prove())
        self.write_ticket();self.directory.joinpath('summary.json').unlink();self.assertIsNone(self.prove())
        self.write_ticket();self.directory.joinpath('summary.json').write_bytes(b'x'*(failed.MAX_ARTIFACT_BYTES+1));self.assertIsNone(self.prove())

    def test_current_formal_forecast_excludes_first_failure(self):
        self.fx.connection.execute('CREATE TABLE forecast_model_versions(company_ref TEXT)')
        self.fx.connection.execute('INSERT INTO forecast_model_versions VALUES(?)',(SYNW,));self.fx.connection.commit()
        with patch.object(failed,'_replay')as replay:
            self.assertIsNone(failed._prove(self.state,self.expected,deadline=time.monotonic()+4));replay.assert_not_called()

    def test_operation_cache_only_and_fresh_cas_replay(self):
        request=mr.request(self.state,self.request['id']);cache={};proof=self.prove()
        with patch.object(failed,'_bounded',return_value=proof)as bound:
            for _ in range(2):self.assertEqual(failed.failure_priority(self.state,request,self.mission,self.spec,
                contract_ref=priority.CONTRACT_REF,deadline=time.monotonic()+3,cache=cache),proof)
            self.assertEqual(bound.call_count,1)
            self.assertTrue(failed.failure_still_current(self.state,proof,deadline=time.monotonic()+3))
            self.assertEqual(bound.call_count,2)
        with patch.object(failed,'_bounded',return_value=None):
            self.assertFalse(failed.failure_still_current(self.state,proof,deadline=time.monotonic()+3))

    def test_fresh_cas_rejects_changed_input_artifact_or_spec_proof(self):
        proof=self.prove()
        for key in ('inputs_hash','failure_summary_hash','failure_ticket_hash','spec_hash','mission_hash'):
            with self.subTest(key=key):
                altered={**proof,key:'0'*64}
                altered['content_hash']=content_hash({k:v for k,v in altered.items()if k!='content_hash'})
                with patch.object(failed,'_bounded',return_value=altered):
                    self.assertFalse(failed.failure_still_current(self.state,proof,deadline=time.monotonic()+3))

    def test_expired_budget_and_timeout_reap_read_only_worker(self):
        with patch.object(failed.subprocess,'Popen')as run:
            self.assertIsNone(failed._bounded(self.state,self.expected,deadline=time.monotonic()-1));run.assert_not_called()
        real_popen=subprocess.Popen;seen=[]
        def slow(command,**kwargs):
            command=[sys.executable,'-c','import time;time.sleep(10)']
            worker=real_popen(command,**kwargs);seen.append(worker);return worker
        start=time.monotonic()
        with patch.object(failed.subprocess,'Popen',side_effect=slow):
            self.assertIsNone(failed._bounded(self.state,self.expected,deadline=time.monotonic()+0.1))
        self.assertEqual(len(seen),1);self.assertLess(time.monotonic()-start,1)
        self.assertIsNotNone(seen[0].poll())
        with self.assertRaises(ProcessLookupError):os.kill(seen[0].pid,0)

    def test_failure_replay_timeout_keeps_old_qualified_gaps(self):
        intrinsic=self.fx.spec(OTHER);request=open_gap_requests(self.state,intrinsic,self.table)[0]
        def slow(*args,deadline,**kwargs):
            time.sleep(max(0,deadline-time.monotonic())+0.005);return None
        with patch.object(failed,'failure_priority',side_effect=slow):
            proofs,info=priority.priority_gaps(self.state,mr.open_requests(self.state),deadline=time.monotonic()+0.1)
        self.assertIn(request['id'],proofs);self.assertNotIn(self.request['id'],proofs)
