"""Local maintenance may archive paid proofs, never buy another answer."""
import copy
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_directed_numeric_figure_support as support_fixture
from tests import test_directed_numeric_measure_conflict as conflict_fixture
from dalton_core.store import canonical_json, content_hash
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.directed_numeric_local_reconciliation import qualification, reconcile, CONTRACT, TABLE, ACTOR
from dalton_core.writer_server import WriterServer, HUMAN_GOVERNANCE_OPERATIONS, OPERATION_FIELDS


class NumericLocalReconciliationTests(unittest.TestCase):
    def fixture(self, *, conflict=False, seed=True):
        h=support_fixture.NumericFigureSupportTests(methodName='runTest');self.addCleanup(h.doCleanups)
        w,f,a,admission,ex,p,v=h.fixture()
        if conflict:
            helper=conflict_fixture.NumericMeasureConflictTests(methodName='runTest')
            helper.seed_collision(h,f,a,admission,ex,p)
        elif seed:h.seed(f,a,admission,ex,p,canonical=True)
        # Run the real retrieval and paid producer/verifier; stop before local
        # execution, without mocking formal/accounting/source authority.
        for _ in range(15):
            result=ex.run_once(admission['id'])
            if result.get('stage')=='qualitative_candidate_staging' or (
                    result.get('work_order_ref') and ':staging:' in result['work_order_ref']):break
        from dalton_core.directed_numeric_local_reconciliation import _chain
        works=_chain(ex,admission)
        self.assertIsNotNone(ex.scheduler.work_order_authority(works[3]['id']))
        self.assertIsNone(ex.scheduler.formal_result(works[3]['id']))
        self.assertEqual((p.calls,v.calls),(1,1))
        return h,w,f,a,admission,ex,p,v,works

    def attempted(self, f, ex, parent, *, exhaust=False):
        count=ex.scheduler.status(parent['id'])['max_attempts'] if exhaust else 1
        for _ in range(count):
            claimed=ex.scheduler.claim(ex.actor_ref,work_order_id=parent['id'])
            self.assertIsNotNone(claimed)
            f.harness.clock.advance(1800)
        # Claim expires the final old lease, but may never start an extra
        # model. At the old policy cap it returns None.
        if exhaust:self.assertIsNone(ex.scheduler.claim(ex.actor_ref,work_order_id=parent['id']))

    def call(self, ex, a):
        return reconcile(ex,a['id'],expected_admission_hash=a['content_hash'],requested_by='human:synthetic-maintainer')

    def test_exhausted_local_four_attempts_reconcile_once_and_normal_reader_replays_no_models(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        self.attempted(f,ex,works[3],exhaust=True)
        old_work=ex.scheduler.work_order_authority(works[3]['id']);old_history=ex.scheduler.attempt_history(works[3]['id'])
        paid=[h.rows(f.store.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')]
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no producer')), patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
            done=self.call(ex,admission)
            self.assertEqual(done['research_status'],'numeric_targets_completed')
            self.assertEqual(done['model_calls'],0)
            self.assertEqual(self.call(ex,admission),done)
            replay=ex.run_once(admission['id'])
            self.assertEqual(replay['numeric_result'],done['numeric_result'])
        self.assertNotEqual(done['numeric_result']['work_order_ref'],works[3]['id'])
        self.assertEqual(ex.scheduler.work_order_authority(works[3]['id']),old_work)
        self.assertEqual(ex.scheduler.attempt_history(works[3]['id']),old_history)
        self.assertEqual([h.rows(f.store.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')],paid)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)
        record=json.loads(f.store.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0])
        self.assertEqual(record['actor_ref'],ACTOR);self.assertEqual(record['requested_by'],'human:synthetic-maintainer')
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_measure_conflict_is_partial_and_never_turns_the_whole_question_completed(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture(conflict=True)
        self.attempted(f,ex,works[3])
        done=self.call(ex,admission)
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(done['numeric_result']['completed_targets']),2)
        self.assertEqual(len(done['numeric_result']['pending_targets']),1)
        self.assertIn('measure_conflict_proof',done['numeric_result']['pending_targets'][0])
        self.assertEqual(ex.run_once(admission['id'])['numeric_result'],done['numeric_result'])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_active_parent_lease_and_missing_paid_proof_never_authorize(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        ex.scheduler.claim(ex.actor_ref,work_order_id=works[3]['id'])
        with self.assertRaisesRegex(DirectedNumericError,'active lease'):self.call(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)
        f.harness.clock.advance(1800)
        with patch('dalton_core.directed_numeric_executor._execution_proof',side_effect=DirectedNumericError('paid proof absent')):
            with self.assertRaisesRegex(DirectedNumericError,'paid proof absent'):self.call(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)

    def test_wrong_hash_withdrawal_retracted_figure_and_unknown_original_refuse(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        self.attempted(f,ex,works[3])
        with self.assertRaisesRegex(DirectedNumericError,'hash/numeric'):reconcile(ex,admission['id'],expected_admission_hash='f'*64,requested_by='human:synthetic')
        with patch.object(a,'resolve_for_execution',side_effect=DirectedNumericError('withdrawn')):
            with self.assertRaisesRegex(DirectedNumericError,'withdrawn'):self.call(ex,admission)
        with patch.object(a.registry,'read',side_effect=RuntimeError('source unavailable')):
            with self.assertRaisesRegex(RuntimeError,'source unavailable'):self.call(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)

    def test_repair_failure_has_one_terminal_zero_cost_work_not_another_generation(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        self.attempted(f,ex,works[3])
        with patch('dalton_core.directed_numeric_executor._record_numeric',side_effect=RuntimeError('local archival unavailable')):
            with self.assertRaisesRegex(RuntimeError,'archival unavailable'):self.call(ex,admission)
        row=f.store.connection.execute('SELECT record_json FROM '+TABLE).fetchone();authorization=json.loads(row[0])
        work='work:numeric-local-reconciliation:'+authorization['id'].rsplit(':',1)[1]
        self.assertEqual(ex.scheduler.formal_result(work)['terminal_state'],'failed')
        counts=h.rows(f.store.connection,'scheduler_work_orders')
        result=self.call(ex,admission)
        self.assertEqual(result['reason'],'numeric_local_reconciliation_failed')
        self.assertEqual(result['responsibility'],'administrator')
        self.assertEqual(h.rows(f.store.connection,'scheduler_work_orders'),counts)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_no_replacement_of_existing_result_and_strong_authorization_replay(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        done=w.drive(ex,admission)
        with self.assertRaisesRegex(DirectedNumericError,'existing numeric result'):self.call(ex,admission)
        self.assertEqual(done,ex.run_once(admission['id']))
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)

    def test_writer_operation_is_closed_uses_exact_runtime_and_blocks_active_child(self):
        self.assertIn('reconcile_numeric_local_figures',HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS['reconcile_numeric_local_figures'],frozenset({'admission_ref','expected_admission_hash','actor_ref'}))
        h,w,f,a,admission,ex,p,v,works=self.fixture();self.attempted(f,ex,works[3])
        import threading
        launcher=SimpleNamespace(_lock=threading.Lock(),_running_index={},_children={})
        runtime=SimpleNamespace(executor=ex,close=lambda:None)
        server=WriterServer.__new__(WriterServer);server.lane_launcher=lambda name:launcher
        params={'admission_ref':admission['id'],'expected_admission_hash':admission['content_hash'],'actor_ref':'human:synthetic-maintainer'}
        with patch('dalton_core.mission_document_research_lane._open_runtime',return_value=runtime) as opened:
            opened.side_effect=lambda *args: (self.assertTrue(launcher._lock.locked()) or runtime)
            result=server._op_reconcile_numeric_local_figures(params)
            self.assertEqual(result['model_calls'],0)
            opened.assert_called_once_with(launcher,admission['id'],admission['content_hash'])

    def test_no_existing_fact_defect_does_not_create_a_general_local_reentry_door(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture(seed=False)
        self.attempted(f,ex,works[3])
        with self.assertRaisesRegex(DirectedNumericError,'no proved Figure-support/measure defect'):self.call(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_authorization_insert_is_guarded_and_every_reader_rechecks_full_mapping(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        self.attempted(f,ex,works[3])
        done=self.call(ex,admission)
        with self.assertRaisesRegex(Exception,'append-only'):
            f.store.connection.execute('DELETE FROM '+TABLE)
        row=f.store.connection.execute('SELECT * FROM '+TABLE).fetchone();saved=row['record_json'];saved_hash=row['content_hash']
        h.corruptible_copy(f.store.connection,TABLE)
        changed=json.loads(saved);changed['admission_hash']='f'*64
        changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
        f.store.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?', (canonical_json(changed),changed['content_hash']))
        f.store.connection.commit()
        with self.assertRaisesRegex(DirectedNumericError,'authorization differs'):ex.run_once(admission['id'])
        f.store.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',(saved,saved_hash));f.store.connection.commit()
        self.assertEqual(ex.run_once(admission['id'])['numeric_result'],done['numeric_result'])
        changed=json.loads(saved);changed['accounting_hash']='e'*64
        changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
        f.store.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',(canonical_json(changed),changed['content_hash']));f.store.connection.commit()
        with self.assertRaises(DirectedNumericError):self.call(ex,admission)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_durable_authorization_recovers_enqueue_interruption_with_the_same_work(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        self.attempted(f,ex,works[3])
        with patch.object(ex.scheduler,'enqueue',side_effect=RuntimeError('synthetic enqueue crash')):
            with self.assertRaisesRegex(RuntimeError,'enqueue crash'):self.call(ex,admission)
        row=f.store.connection.execute('SELECT record_json FROM '+TABLE).fetchone();saved=row[0]
        done=self.call(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0],saved)
        self.assertEqual(done['model_calls'],0)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_writer_active_owned_child_cannot_cross_local_reconciliation_boundary(self):
        h,w,f,a,admission,ex,p,v,works=self.fixture()
        import threading
        ticket=f.state/'synthetic-live-ticket.json';ticket.write_text(canonical_json({'admission_ref':admission['id'],'status':'running'}))
        launcher=SimpleNamespace(_lock=threading.Lock(),_running_index={'ticket:synthetic':ticket},
            _children={'ticket:synthetic':SimpleNamespace(poll=lambda:None)})
        server=WriterServer.__new__(WriterServer);server.lane_launcher=lambda name:launcher
        with patch('dalton_core.mission_document_research_lane._open_runtime') as opened:
            with self.assertRaisesRegex(DirectedNumericError,'child must settle'):
                server._op_reconcile_numeric_local_figures({'admission_ref':admission['id'],
                    'expected_admission_hash':admission['content_hash'],'actor_ref':'human:synthetic-maintainer'})
            opened.assert_not_called()
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)
