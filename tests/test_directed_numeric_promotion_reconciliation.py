"""An exact prior local promotion failure can finish once without models."""
import copy,json,types,unittest
from unittest.mock import patch
from tests import test_directed_numeric_local_reconciliation as local
from dalton_core.store import content_hash,canonical_json
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.directed_numeric_local_reconciliation import reconcile,_chain
from dalton_core.directed_numeric_promotion_reconciliation import failure,qualification,replay_candidate,CONTRACT,TABLE
from dalton_core.store import IdempotencyConflict
from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
from dalton_core.lane_child_launcher import LaneChildRejected

class PromotionReconciliationTests(unittest.TestCase):
    def fixture(self):
        from tests import test_directed_numeric_figure_support as support
        from dalton_core.claim_index_figures import MissionFigureAuthorityResolver
        from dalton_core.directed_numeric_executor import complete_numeric_stage
        from tests.test_candidate_exact_evidence_reuse import LegacyPeriodResolver
        h=local.NumericLocalReconciliationTests('runTest');self.addCleanup(h.doCleanups)
        helper=support.NumericFigureSupportTests('runTest');self.addCleanup(helper.doCleanups)
        w,f,a,admission,ex,p,v=helper.fixture(period='Year Ended December 31, 2025')
        with patch.object(MissionFigureAuthorityResolver,'identity',LegacyPeriodResolver.identity):
            helper.seed(f,a,admission,ex,p,canonical=True)
        for _ in range(15):
            outcome=ex.run_once(admission['id'])
            if outcome.get('stage')=='qualitative_candidate_staging':break
        works=_chain(ex,admission);h.attempted(f,ex,works[3],exhaust=True)
        # Execute the previous raw-period promoter against the genuinely new,
        # source-derived date identity; no forged paid/model authority.
        with patch('dalton_core.directed_numeric_promotion_replay.replay',return_value=None):
            with self.assertRaisesRegex(IdempotencyConflict,'already promoted'):
                reconcile(ex,admission['id'],expected_admission_hash=admission['content_hash'],requested_by='automation:old-local')
        self.assertIsNotNone(failure(ex.connection,admission))
        return helper,w,f,a,admission,ex,p,v

    def call(self,e,a):
        return reconcile(e,a['id'],expected_admission_hash=a['content_hash'],requested_by='automation:local-maintenance')

    def test_exact_local_failure_reuses_all_paid_stages_and_reader_keeps_old_canonical(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        old={t:h.rows(ex.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries','claim_versions','evidence_versions','reviewed_candidate_commits')}
        failed=failure(ex.connection,admission);parent=ex.scheduler.work_order_authority(failed['parent_work_ref']);attempts=ex.scheduler.attempt_history(failed['parent_work_ref'])
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no model')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
            done=self.call(ex,admission)
            self.assertEqual(done['research_status'],'numeric_targets_completed');self.assertEqual(done['model_calls'],0)
            self.assertEqual(self.call(ex,admission),done)
            self.assertEqual(ex.run_once(admission['id'])['numeric_result'],done['numeric_result'])
        for table,rows in old.items():self.assertTrue(all(r in h.rows(ex.connection,table) for r in rows))
        for table in ('model_invocations','observability_usage_entries','observability_cost_entries'):self.assertEqual(old[table],h.rows(ex.connection,table))
        self.assertEqual(parent,ex.scheduler.work_order_authority(failed['parent_work_ref']));self.assertEqual(attempts,ex.scheduler.attempt_history(failed['parent_work_ref']))
        self.assertEqual((p.calls,v.calls),(1,1));self.assertEqual(ex.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)
        work=ex.scheduler.work_order_authority(done['numeric_result']['work_order_ref'])['work_order']
        self.assertEqual((work['budget']['max_cost_usd'],work['budget']['max_attempts']),(0,1))

    def test_no_paid_proof_changed_source_or_wrong_admission_cannot_authorize(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        with self.assertRaisesRegex(DirectedNumericError,'admission differs'):qualification(ex,admission['id'],'f'*64)
        with patch('dalton_core.directed_numeric_executor._execution_proof',side_effect=DirectedNumericError('unknown paid authority')):
            with self.assertRaisesRegex(DirectedNumericError,'unknown paid'):self.call(ex,admission)
        with patch.object(a.registry,'read',side_effect=RuntimeError('changed source')):
            with self.assertRaisesRegex(RuntimeError,'changed source'):self.call(ex,admission)
        self.assertIsNone(ex.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,)).fetchone())
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_one_fixed_local_ticket_across_configuration_and_no_other_error_qualifies(self):
        h,w,f,a,admission,ex,p,v=self.fixture();directory=f.state/'promotion-ticket';directory.mkdir()
        launcher=object.__new__(MissionDocumentResearchLauncher);launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'epoch':'another'};launcher.status=lambda ref:{'id':ref,'status':'failed'}
        calls=[]
        def spawn(**kwargs):
            calls.append(kwargs);self.assertTrue(kwargs['completed_models_only'])
            ref=launcher.TICKET_PREFIX+':'+kwargs['digest'];launcher._ticket_path(ref).write_text('{}');return {'id':ref,'status':'running'}
        launcher.spawn=spawn
        c=types.SimpleNamespace(store=f.store,launcher=launcher,_effective_work_hints=lambda a:[x['id'] for x in _chain(ex,a)])
        proof=replay_candidate(c,admission);self.assertEqual(proof['model_calls_allowed'],0)
        launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        self.assertIsNone(replay_candidate(c,admission))
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        self.assertEqual(len(calls),1)
        with patch('dalton_core.scheduler.Scheduler.formal_result',return_value=None):self.assertIsNone(failure(ex.connection,admission))

    def test_failed_followup_is_not_another_local_or_paid_window(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        with patch('dalton_core.directed_numeric_executor._record_numeric',side_effect=RuntimeError('cannot safely complete')):
            with self.assertRaisesRegex(RuntimeError,'safely complete'):self.call(ex,admission)
        rows=h.rows(ex.connection,'scheduler_work_orders')
        self.assertEqual(self.call(ex,admission)['reason'],'numeric_promotion_local_failed')
        self.assertEqual(rows,h.rows(ex.connection,'scheduler_work_orders'));self.assertEqual((p.calls,v.calls),(1,1))

    def test_authorization_and_receipt_mapping_are_rechecked_and_interrupted_enqueue_is_same_work(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        with patch.object(ex.scheduler,'enqueue',side_effect=RuntimeError('after durable authorization')):
            with self.assertRaisesRegex(RuntimeError,'durable authorization'):self.call(ex,admission)
        saved=ex.connection.execute('SELECT * FROM '+TABLE).fetchone();done=self.call(ex,admission)
        self.assertEqual(saved['record_json'],ex.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0])
        h.corruptible_copy(ex.connection,TABLE)
        bad=json.loads(saved['record_json']);bad['promotion_mappings'][0]['mapping']['original_review_hash']='f'*64
        bad['content_hash']=content_hash({k:v for k,v in bad.items() if k!='content_hash'})
        ex.connection.execute('UPDATE '+TABLE+' SET record_json=?,content_hash=?',(canonical_json(bad),bad['content_hash']));ex.connection.commit()
        with self.assertRaisesRegex(DirectedNumericError,'authorization no longer replays'):ex.run_once(admission['id'])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_real_coordinator_selects_fixed_child_and_cli_completes_without_paid_execution(self):
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        from dalton_core.mission_document_research_cli import run_admission
        from contextlib import nullcontext
        h,w,f,a,admission,ex,p,v=self.fixture();directory=f.state/'coordinator-local-tickets';directory.mkdir()
        launcher=object.__new__(MissionDocumentResearchLauncher);launcher.tickets_dir=directory
        launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'synthetic':'no real process'}
        outputs=[]
        def spawn(**kwargs):
            self.assertTrue(kwargs['completed_models_only'])
            runtime=types.SimpleNamespace(admission=admission,executor=ex)
            with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(ex,'run_once',side_effect=AssertionError('no generic/model executor')):
                result=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',planner_model_config=f.state/'unused',document_research_config=f.state/'unused',admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],completed_models_only=True)
            outputs.append(result);ref=launcher.TICKET_PREFIX+':'+kwargs['digest'];launcher._ticket_path(ref).write_text('{}')
            return {'id':ref,'status':'running'}
        launcher.spawn=spawn
        from dalton_core.mission_document_research_promotion import ensure_promotion_authority
        ensure_promotion_authority(ex.connection)
        c=MissionDocumentResearchCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no paid model')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no paid verifier')):
            result=c.dispatch_once()
        self.assertEqual(result['reason'],'canonical_promotion_local_reconciliation')
        self.assertEqual(outputs[0]['outcomes'][0]['research_status'],'numeric_targets_completed')
        self.assertEqual(outputs[0]['outcomes'][0]['model_calls'],0);self.assertEqual((p.calls,v.calls),(1,1))
