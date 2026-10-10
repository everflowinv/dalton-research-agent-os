"""Deployment stops between fully persisted artifacts, never inside a call."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
from dalton_core.research_publication_quiesce import QuiesceControl, main
from dalton_core.research_publication_worker import poll_once
from dalton_core import research_output_preparation as prep
from tests.test_research_publication_worker import MISSION, reader
from tests import test_ui_text_formal_retry as ui_fixture
from dalton_core.store import canonical_json, content_hash


class PublicationQuiesceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work=self.root/'work';self.work.mkdir(mode=0o700)
        self.config=self.root/'worker.json'
        self.cfg={k:str(self.root/k) for k in ('core_db','scheduler_db','model_config',
                    'verifier_config','checker_config','brain_config','output_directory')}
        self.cfg.update(work_dir=str(self.work),schema_version='research-publication-worker-config:0.1',
                        workers=1,chunk_chars=4500,max_cost_per_call=1,draft_attempts=2,
                        publication_gate={})
        self.config.write_text(json.dumps(self.cfg));self.control=QuiesceControl(self.config,self.work)
        (self.work/'.worker.lock').touch(mode=0o600)

    def test_request_is_idempotent_and_resume_is_exact_and_retains_ack(self):
        first=self.control.request();self.assertEqual(first,self.control.request())
        checkpoint=self.work/'worker-last-run.json';checkpoint.write_text('{"status":"quiesced"}')
        ack=self.control.acknowledge(checkpoint);self.assertFalse(ack['fee_settlement_proof'])
        with self.assertRaisesRegex(ValueError,'CAS'):self.control.resume('wrong')
        # New installed config bytes must not silently lift the stop.
        self.config.write_text(json.dumps({**self.cfg,'chunk_chars':4600}))
        self.assertTrue(self.control.requested())
        self.control.resume(first['content_hash']);self.assertFalse(self.control.requested())
        self.assertEqual(json.loads((self.work/f"quiesce-retired-{first['request_id']}.json").read_text()),first)
        self.assertEqual(json.loads((self.work/f"quiesce-retired-{first['request_id']}-ack.json").read_text()),ack)
        second=self.control.request();self.assertNotEqual(first['content_hash'],second['content_hash'])
        # Prior ack is history, not an acknowledgement of the new request.
        self.assertEqual(main(['inspect','--config',str(self.config)]),0)

    def test_stopped_scheduled_run_preserves_previous_product_diagnostics(self):
        before={'schema_version':'research-publication-worker-checkpoint:0.1',
                'checked_at':'2026-10-01T00:00:00+00:00','products':[{'status':'skipped'}],
                'skipped_products':[{'identity':{'kind':'initial_screen'}}],
                'pending':7,'ui_texts':{'blocked':2}}
        (self.work/'worker-last-run.json').write_text(json.dumps(before))
        self.control.request()
        with patch('dalton_core.research_publication_authority.published_runtime_gate',return_value={'status':'active'}), \
             patch.object(prep.sqlite3,'connect',side_effect=AssertionError('database opened')):
            self.assertEqual(prep.run_worker(self.config),0)
        after=json.loads((self.work/'worker-last-run.json').read_text())
        for key in ('products','skipped_products','pending','ui_texts'):self.assertEqual(after[key],before[key])
        self.assertEqual(after['last_pass_checked_at'],before['checked_at'])
        self.assertEqual(after['status'],'quiesced')

    def test_active_pass_prevents_resume_even_if_old_ack_exists(self):
        r=self.control.request();fd=os.open(self.work/'.worker.lock',os.O_RDWR)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'still active'):self.control.resume(r['content_hash'])
        finally:os.close(fd)
        self.assertTrue(self.control.requested())

    def test_invalid_request_or_symlink_is_fail_closed(self):
        request=self.control.request();request['work_dir']=str(self.root/'other')
        self.control.request_path.write_text(json.dumps(request))
        with self.assertRaises(ValueError):self.control.requested()
        self.control.request_path.unlink();self.control.request_path.symlink_to(self.config)
        with self.assertRaises(ValueError):self.control.requested()

    def test_product_result_persists_before_yield_and_resume_replays_without_new_call(self):
        calls=[]
        def prepare(p):
            calls.append(p['version_ref']);self.control.request()
            return {'status':'completed'}
        result=poll_once(None,MISSION,state_dir=self.work/'products',prepare=prepare,
                         library_reader=reader,stop_requested=self.control.requested)
        self.assertEqual(len(calls),1);self.assertTrue(result['quiesced']);self.assertEqual(result['unprocessed'],2)
        states=[json.loads(p.read_text()) for p in (self.work/'products').glob('*.json')]
        self.assertEqual([s['status'] for s in states],['completed'])
        self.assertEqual(result['pending'],2)
        self.control.resume(self.control.read_request()['content_hash'])
        again=poll_once(None,MISSION,state_dir=self.work/'products',prepare=lambda p:(calls.append(p['version_ref'])or{'status':'completed'}),
                        library_reader=reader,stop_requested=self.control.requested)
        self.assertEqual(again['unchanged'],1);self.assertEqual(again['completed'],2);self.assertEqual(len(calls),3)
        db=sqlite3.connect(self.work/'publication-limits.sqlite3')
        try:self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_events WHERE kind='run'").fetchone()[0],3)
        finally:db.close()

    def test_pre_requested_poll_has_no_states_run_admissions_or_callbacks(self):
        self.control.request()
        result=poll_once(None,MISSION,state_dir=self.work/'products',prepare=lambda p:self.fail('spent'),
                         library_reader=reader,stop_requested=self.control.requested)
        self.assertEqual(result['unprocessed'],3)
        self.assertFalse((self.work/'publication-limits.sqlite3').exists())
        self.assertFalse((self.work/'products').exists())

    def test_failed_artifact_finishes_its_pending_state_before_yield(self):
        def prepare(p):self.control.request();raise ValueError('permanent verifier failure')
        result=poll_once(None,MISSION,state_dir=self.work/'products',prepare=prepare,
                         library_reader=reader,stop_requested=self.control.requested)
        states=[json.loads(p.read_text()) for p in (self.work/'products').glob('*.json')]
        self.assertEqual(states[0]['status'],'pending');self.assertIn('permanent',states[0]['result']['reason'])
        self.assertEqual(result['pending'],3)

    def test_ui_batches_yield_only_after_persisting_first_result(self):
        fixture=ui_fixture.FormalRetryTests('test_failure_classes');fixture.setUp();self.addCleanup(fixture.doCleanups)
        wire={'id':'claim:2','claim_ref':'claim:2','subject_ref':'company:A',
              'normalized_statement':'The second independent management observation describes changing customer requirements.'}
        fixture.db.execute('INSERT INTO claim_versions VALUES(?,?,?,?,?,?,?)',('claim:2','claim:2',1,canonical_json(wire),content_hash(wire),None,'2026-09-01T00:00:00Z'));fixture.db.commit()
        calls=[]
        def prepare(p,**kwargs):calls.append(p);self.control.request();return {'status':'completed'}
        from dalton_core import ui_text_discovery as ui
        with patch.object(ui,'_chunks',side_effect=lambda entries:([e] for e in entries)), patch.object(fixture,'prepare',side_effect=prepare):
            result=fixture.poll(stop_requested=self.control.requested)
        self.assertEqual(len(calls),1);self.assertTrue(result['quiesced'])
        saved=[json.loads(p.read_text()) for p in (fixture.state/'results').glob('*.json')]
        self.assertEqual([s['status'] for s in saved],['completed']);self.assertEqual(result['attempted'],1)

    def test_scheduled_worker_before_request_acknowledges_without_db_or_model(self):
        self.control.request()
        with patch('dalton_core.research_publication_authority.published_runtime_gate',return_value={'status':'active'}), \
             patch.object(prep.sqlite3,'connect',side_effect=AssertionError('database opened')), \
             patch.object(prep,'CockpitModel',side_effect=AssertionError('model opened')):
            self.assertEqual(prep.run_worker(self.config),0)
        self.assertEqual(self.control.read_ack()['status'],'worker_at_persisted_boundary')
        self.assertEqual(json.loads((self.work/'worker-last-run.json').read_text())['status'],'quiesced')

    def test_actual_worker_ack_is_after_product_state_and_all_callbacks_returned(self):
        con=sqlite3.connect(self.cfg['core_db'])
        con.executescript('CREATE TABLE coverage_mission_pointer(mission_ref TEXT,mission_version_id TEXT); CREATE TABLE coverage_mission_versions(mission_version_id TEXT,record_json TEXT);')
        con.execute('INSERT INTO coverage_mission_pointer VALUES(?,?)',('mission:test','v1'))
        con.execute('INSERT INTO coverage_mission_versions VALUES(?,?)',('v1',json.dumps({**MISSION,'mission_ref':'mission:test'})));con.commit();con.close()
        Path(self.cfg['brain_config']).write_text(json.dumps({'budget_db':str(self.root/'budget.sqlite')}))
        calls=[]
        def build(args,payload):
            calls.append(payload);self.control.request()
            (self.work/'result.json').write_text('{"status":"completed"}')
            self.assertFalse(self.control.ack_path.exists())
            return 0
        with ExitStack() as stack:
            for target,value in [('dalton_core.research_publication_authority.published_runtime_gate',{'status':'active'}),
                                 ('dalton_core.final_surface_products.final_surface_products',[]),
                                 ('dalton_core.research_localization_store.load_ui_texts',{}),
                                 ('dalton_core.ui_text_discovery._discover',[])]:stack.enter_context(patch(target,return_value=value))
            for target in ('dalton_core.cockpit_research_library.research_library',
                           'dalton_core.research_publication_worker.research_library'):
                stack.enter_context(patch(target,side_effect=lambda c,m,company,**kw:reader(c,m,company)))
            stack.enter_context(patch.object(prep,'build',side_effect=build));stack.enter_context(patch.object(prep,'has_reviewed_attachment',return_value=False))
            stack.enter_context(patch.object(prep,'with_spend_gate',side_effect=lambda args,*a:args))
            stack.enter_context(patch.object(prep,'language_tier',return_value='standard'))
            self.assertEqual(prep.run_worker(self.config),0)
        self.assertEqual(len(calls),1)
        checkpoint=json.loads((self.work/'worker-last-run.json').read_text())
        self.assertEqual(checkpoint['status'],'quiesced');self.assertEqual(checkpoint['unprocessed'],2)
        self.assertEqual([json.loads(p.read_text())['status'] for p in (self.work/'products').glob('*.json')],['completed'])
        self.assertFalse(self.control.read_ack()['fee_settlement_proof'])

if __name__=='__main__':unittest.main()
