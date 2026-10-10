from __future__ import annotations
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.store import DaltonStore, content_hash
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.model_cell_reconciliation import ModelCellReconciliationAuthority
from dalton_core.research_followup import ResearchFollowupAuthority, requests, brief_entries, replay_brief_entries, approval_items
from dalton_core.writer_server import WriterServer, Principal, HUMAN_GOVERNANCE_OPERATIONS
from tests.test_model_forecast_driver import model as forecast_body


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'core.sqlite'
        self.store=DaltonStore(str(self.path));self.addCleanup(self.store.close)
        self.models=ForecastModelAuthority(self.store)
        self.model=self.models.publish(forecast_body())
        self.now='2030-01-01T00:00:00+00:00'
        self.authority=ResearchFollowupAuthority(self.store,clock=lambda:self.now)

    def request(self):
        return self.authority.record(kind='model_redraft',company_ref=self.model['company_ref'],
            scope={'model_version_ref':self.model['id']},reason='Review a held result.',
            evidence=[{'kind':'forecast_model','ref':self.model['id'],'hash':self.model['content_hash']}])

    def test_major_actual_opens_independent_work_without_changing_the_model(self):
        cell=self.model['results'][0]['cells'][0]
        actual={'concept':'us-gaap:Revenues','statement':'income','dimensions':{},'unit':self.model['results'][0]['unit'],
                'period_start':cell['period']['start'],'period_end':cell['period']['end'],'value':str(float(cell['value'])*1.10),
                'basis':'preliminary','available_on':'2029-12-01','refs':[{'ref':'held-preliminary:synthetic'}],'source_hash':'a'*64}
        event={'period_end':cell['period']['end'],'available_on':'2029-12-01','source_ref':'held-preliminary:synthetic','source_hash':'a'*64}
        with patch('dalton_core.model_cell_reconciliation.collect_actuals',return_value=([actual],[event])):
            ledger=ModelCellReconciliationAuthority(self.store).refresh(self.model['company_ref'])
        major=[r for r in ledger if r.get('redraft_required')]
        self.assertTrue(major)
        queue=requests(self.store.connection)
        self.assertEqual(len(queue),len(major))
        self.assertTrue(all(r['kind']=='model_redraft' and r['human_checkpoint'] for r in queue))
        self.assertEqual(self.models.model(self.model['id'])['content_hash'],self.model['content_hash'])

    def test_one_terminal_result_owner_deadline_and_frozen_weekly_replay(self):
        request=self.request()
        self.assertEqual(self.request()['id'],request['id'])
        self.authority.decide(request['id'],action='assign',actor_ref='human:analyst',reason='I will review.',owner_ref='human:analyst',due_on='2030-02-01')
        current=requests(self.store.connection,today='2030-02-01')[0]
        self.assertTrue(current['overdue'])
        self.assertEqual(current['owner_ref'],'human:analyst')
        with self.assertRaisesRegex(ValueError,'new held result'):
            self.authority.decide(request['id'],action='completed',actor_ref='human:analyst',reason='Done.',closure_evidence=request['evidence'])
        frozen=brief_entries(self.store.connection,[self.model['company_ref']],as_of='2030-01-02T00:00:00+00:00')
        self.now='2030-01-03T00:00:00+00:00'
        self.authority.decide(request['id'],action='no_revision_needed',actor_ref='human:analyst',reason='Held source was reviewed; no numerical correction is needed.',closure_evidence=request['evidence'])
        self.assertEqual(requests(self.store.connection)[0]['state'],'no_revision_needed')
        replay_brief_entries(self.store.connection,frozen,[self.model['company_ref']],as_of='2030-01-02T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError,'one terminal'):
            self.authority.decide(request['id'],action='withdrawn',actor_ref='human:analyst',reason='Different answer.')
        with self.assertRaises(sqlite3.IntegrityError):self.store.connection.execute('DELETE FROM research_followup_requests')

    def test_real_writer_refuses_machine_and_hash_drift_then_saves_a_real_terminal(self):
        request=self.request()
        principal=Principal('owner','owner-token',frozenset(HUMAN_GOVERNANCE_OPERATIONS),actor_ref='human:analyst')
        writer=WriterServer(str(self.path),str(self.path.with_suffix('.sock')),{'owner':principal})
        writer._open_store();self.addCleanup(writer.store.close)
        writer._principal=lambda token:principal
        params={'request_ref':request['id'],'request_hash':request['content_hash'],'action':'declined','reason':'No additional work is justified.'}
        call=type('R',(),{'auth_token':None,'operation':'decide_research_followup','params':params})()
        saved=writer._handle(call)
        self.assertEqual(saved['actor_ref'],'human:analyst')
        self.assertEqual(requests(self.store.connection)[0]['terminal_decision']['id'],saved['id'])
        machine=Principal('machine','machine-token',frozenset(HUMAN_GOVERNANCE_OPERATIONS),actor_ref='automation:synthetic')
        writer._principal=lambda token:machine
        with self.assertRaises(PermissionError):writer._handle(call)

    def test_capture_reflection_and_mapping_failure_are_evidence_bound_and_idempotent(self):
        self.store.connection.execute('CREATE TABLE research_cycle_reflection_versions(version_id TEXT,record_json TEXT,content_hash TEXT)')
        reflection={'id':'reflection:synthetic:1','mission_ref':'mission:synthetic','created_at':'2026-01-01',
                    'backlog_candidates':[{'question':'Which research was wasted?','because':'No question was admitted.'}]}
        reflection['content_hash']=content_hash(reflection)
        self.store.connection.execute('INSERT INTO research_cycle_reflection_versions VALUES(?,?,?)',
            (reflection['id'],json.dumps(reflection),reflection['content_hash']))
        first=self.authority.capture_existing([reflection['id']]);second=self.authority.capture_existing([reflection['id']])
        self.assertEqual(first,second)
        self.assertEqual(first[0]['kind'],'research_reflection')
        self.assertTrue(requests(self.store.connection)[0]['overdue'])
        cards=approval_items(self.store.connection,lambda ref:ref)
        self.assertTrue(cards[0]['followup_fields'])
        self.assertTrue(any(a['decision']=='no_revision_needed' for a in cards[0]['actions']))

    def test_cockpit_uses_real_writer_and_requires_readback_before_success(self):
        from tests.test_cockpit_plane import CockpitHarness
        from dalton_core.cockpit_plane import CockpitConflict
        from tests.test_cockpit_v2_migration import fixtures, run, posts
        root=Path(self.tmp.name)/'cockpit';root.mkdir()
        harness=CockpitHarness(root);self.addCleanup(harness.close)
        store=harness.h.h.core
        held=ForecastModelAuthority(store).publish(forecast_body())
        request=ResearchFollowupAuthority(store).record(kind='model_redraft',company_ref=held['company_ref'],
            scope={'model_version_ref':held['id']},reason='Held actual requires independent review.',
            evidence=[{'kind':'forecast_model','ref':held['id'],'hash':held['content_hash']}])
        cards=harness.plane._revision_checkpoints()
        card=next(c for c in cards if c['ref']==request['id'])
        fixture=fixtures();fixture['/v1/cockpit/approvals']={'items':[card],'count':1}
        out=run({'t':'#/tasks'},[
            {'page':'t','op':'set','id':'why-'+request['id'].replace(':','').replace('-','')[-24:],'value':'Review completed; no revision justified.'},
            {'page':'t','op':'set','id':'evidence-'+request['id'][-24:],'value':str(len(card['close_evidence_choices'])-1)},
            {'page':'t','op':'click','text':'审查后无需修订'},
            {'page':'t','op':'click','dialog':True,'text':'确认审查后无需修订'},
        ],fixtures=fixture)
        body=posts(out,'/v1/cockpit/decide')[0]
        self.assertEqual(body['closure_evidence'],request['evidence'])
        harness.plane.governance_call=lambda *args,**kw:{'id':'fake-save'}
        with self.assertRaisesRegex(CockpitConflict,'尚未读回'):
            harness.plane.decide('owner@example.com',body)
        owner=Principal('owner','held-token',frozenset(HUMAN_GOVERNANCE_OPERATIONS),actor_ref='human:synthetic-owner')
        writer=WriterServer(str(harness.core_path),str(harness.root/'real-writer.sock'),{'owner':owner})
        writer._open_store();self.addCleanup(writer.store.close)
        def real_call(*args,actor_ref,operation,params,**kw):
            principal=Principal('owner','held-token',frozenset(HUMAN_GOVERNANCE_OPERATIONS),actor_ref=actor_ref)
            writer._principal=lambda token:principal
            return writer._handle(type('R',(),{'auth_token':None,'operation':operation,'params':params})())
        harness.plane.governance_call=real_call
        response=harness.plane.decide('owner@example.com',body)
        self.assertEqual(response['status'],'decided')
        self.assertEqual(requests(store.connection)[0]['state'],'no_revision_needed')
        self.assertTrue(harness.plane.decision_status(body)['decided'])
        self.assertFalse(any(c['ref']==request['id'] for c in harness.plane._revision_checkpoints()))
