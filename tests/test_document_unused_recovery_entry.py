"""A paid retry's unused Work remains reachable without another retry allowance."""
import copy
from datetime import timedelta
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext
from dalton_core.document_unused_recovery_entry import qualification
from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
from dalton_core.mission_document_research_cli import run_admission
from dalton_core.lane_child_launcher import LaneChildRejected
from tests import test_document_paid_route_replay as paid_fixture
from tests import test_directed_numeric_workflow as numeric_fixture
from dalton_core.store import content_hash


class UnusedRecoveryEntryTests(unittest.TestCase):
    def fixture(self):
        h = paid_fixture.DocumentPaidReplayTests('runTest');self.addCleanup(h.doCleanups)
        f,a,admission,e,p,failed,formal,recovery,links = h.fixture()
        return f,a,admission,e,p,recovery,links

    def historical_unproved_fixture(self):
        h=numeric_fixture.NumericWorkflowTests('runTest');self.addCleanup(h.doCleanups)
        f,a,admission,e,p,v=h.fixture()
        p.candidate_wire['targets'][0]['figure']['period']='FY2024'
        original=p.execute;saved=[]
        def late(work,route,profile):
            value=original(work,route,profile);saved.append(value)
            f.harness.clock.value+=timedelta(seconds=2);return value
        p.execute=late;p.replay=lambda *args:saved[-1];e.draft_worker.lease_seconds=1
        with patch.object(e,'_paid_contract_failure_proof',return_value=None):
            for _ in range(12):
                result=e.run_once(admission['id'])
                if result.get('reason')=='automatic_bounded_unproved_send_retry':break
                if result.get('status')=='admitted' and result.get('stage')=='qualitative_model_draft':
                    for r in f.store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_work_orders'").fetchall():
                        f.store.connection.execute('DROP TRIGGER "'+r[0]+'"')
                    f.store.connection.execute('UPDATE scheduler_work_orders SET max_attempts=4 WHERE work_order_id=?',(result['work_order_ref'],))
                f.harness.clock.value+=timedelta(seconds=30);e.scheduler.sweep_expired()
        self.assertEqual(result.get('reason'),'automatic_bounded_unproved_send_retry')
        from dalton_core.mission_document_research_executor import _effective_stage
        recovery,links=_effective_stage(a,e.scheduler,admission,1,worker=e.draft_worker)
        return f,a,admission,e,p,recovery,links

    def prove(self, a,e,admission,recovery):
        return qualification(a,e.scheduler,admission,recovery['id'],e.draft_worker)

    def test_exact_paid_late_reply_and_unused_work_qualify_without_mutation(self):
        f,a,admission,e,p,recovery,links = self.fixture()
        before=f.store.connection.total_changes
        proof=self.prove(a,e,admission,recovery)
        self.assertEqual(proof['recovery_link_hash'],links[-1]['content_hash'])
        self.assertEqual(proof['additional_recovery_windows'],0)
        self.assertEqual(f.store.connection.total_changes,before)
        self.assertEqual(p.calls,1)

    def test_any_lease_even_expired_disallows_a_new_entry(self):
        f,a,admission,e,p,recovery,links=self.fixture()
        e.scheduler.claim(owner_ref=e.draft_worker.worker_ref,work_order_id=recovery['id'],lease_seconds=1)
        f.harness.clock.value += timedelta(seconds=2);e.scheduler.sweep_expired()
        self.assertIsNone(self.prove(a,e,admission,recovery));self.assertEqual(p.calls,1)

    def test_pending_old_fee_or_changed_failure_cannot_authorize_entry(self):
        f,a,admission,e,p,recovery,links=self.fixture()
        store=e.draft_worker.budget_store;original=store.admission
        def pending(**kw):
            saved=original(**kw)
            return None if saved is None else {**saved,'settlement':None}
        with patch.object(store,'admission',side_effect=pending):
            self.assertIsNone(self.prove(a,e,admission,recovery))
        self.assertEqual(p.calls,1)

    def test_existing_epoch_mapping_cannot_reenter_the_ready_placeholder(self):
        f,a,admission,e,p,recovery,links=self.fixture()
        original=f.store.connection.execute
        class BoundConnection:
            def execute(self, sql, params=()):
                if sql.startswith('SELECT 1 FROM mission_document_research_model_authority_epoch_rebinds'):
                    return types.SimpleNamespace(fetchone=lambda: {'exists':1})
                return original(sql, params)
        shadow=types.SimpleNamespace(store=types.SimpleNamespace(connection=BoundConnection()))
        self.assertIsNone(qualification(shadow,e.scheduler,admission,recovery['id'],e.draft_worker))
        self.assertEqual(p.calls,1)

    def test_fixed_ticket_does_not_reset_across_config_or_source_ticket_changes(self):
        f,a,admission,e,p,recovery,links=self.fixture();proof=self.prove(a,e,admission,recovery)
        launcher=object.__new__(MissionDocumentResearchLauncher);directory=f.state/'unused-entry';directory.mkdir()
        launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'first':'configuration'}
        def spawn(**kw):
            ref=launcher.TICKET_PREFIX+':'+kw['digest'];launcher._ticket_path(ref).write_text('{}')
            self.assertEqual(kw['unused_recovery_link_ref'],links[-1]['id'])
            return {'id':ref,'status':'running'}
        launcher.spawn=spawn;launcher.status=lambda ref:{'id':ref,'status':'failed'}
        launcher.resume_unused_recovery(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        launcher.configuration=lambda:{'other':'configuration'}
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):
            launcher.resume_unused_recovery(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        changed=copy.deepcopy(proof);changed['recovery_link_hash']='0'*64
        with self.assertRaisesRegex(LaneChildRejected,'proof differs'):
            launcher.resume_unused_recovery(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=changed)

    def test_lane_bypasses_spent_reentry_only_with_strong_unused_link(self):
        f,a,admission,e,p,recovery,links=self.fixture();proof=self.prove(a,e,admission,recovery)
        lane=object.__new__(MissionDocumentResearchCoordinator);lane.store=f.store
        resumed={'id':'mission-document-research:fixed-unused','status':'running'}
        lane.launcher=types.SimpleNamespace(tickets_dir=f.state, resume_unused_recovery=lambda **kw:resumed,
            resume=lambda **kw:(_ for _ in ()).throw(AssertionError('spent generic reentry')))
        with patch('dalton_core.document_unused_recovery_entry.candidate',return_value=proof):
            result=lane._resume(admission,ticket_ref='mission-document-research:old',
                recovery={'reason':'exact_work_ready','work_order_ref':recovery['id']},settled=None)
        self.assertEqual(result['ticket_ref'],resumed['id']);self.assertEqual(p.calls,1)

    def test_cli_enters_existing_window_then_completes_without_extra_recovery_link(self):
        f,a,admission,e,p,recovery,links=self.fixture()
        p.candidate_wire['targets'][0]['figure']['period']='FY2025'
        e.draft_worker.lease_seconds=30
        runtime=types.SimpleNamespace(admission=admission,authority=a,scheduler=e.scheduler,executor=e,
            transition_budget=lambda admission:40,wait_until_claimable=lambda ref:True)
        old_formal=e.scheduler.formal_result(links[-1]['failed_work_order_ref'])
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)):
            result=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                planner_model_config=f.state/'unused',document_research_config=f.state/'unused',
                admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],
                unused_recovery_link_ref=links[-1]['id'],unused_recovery_link_hash=links[-1]['content_hash'])
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['outcomes'][-1]['research_status'],'numeric_targets_completed')
        self.assertEqual(p.calls,2)  # the old failed call plus the existing authorized window
        self.assertEqual(e.scheduler.formal_result(links[-1]['failed_work_order_ref']),old_formal)
        self.assertEqual(f.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0],1)

    def test_failed_existing_window_does_not_buy_another_recovery_in_this_child(self):
        f,a,admission,e,p,recovery,links=self.fixture();e.draft_worker.lease_seconds=30
        runtime=types.SimpleNamespace(admission=admission,authority=a,scheduler=e.scheduler,executor=e,
            transition_budget=lambda admission:40,wait_until_claimable=lambda ref:True)
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)):
            result=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                planner_model_config=f.state/'unused',document_research_config=f.state/'unused',
                admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],
                unused_recovery_link_ref=links[-1]['id'],unused_recovery_link_hash=links[-1]['content_hash'])
        self.assertEqual(result['status'],'blocked');self.assertEqual(p.calls,2)
        self.assertEqual(f.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0],1)
        self.assertEqual(e.scheduler.formal_result(recovery['id'])['terminal_state'],'failed')

    def test_historical_unproved_link_enters_current_epoch_same_window_and_new_template(self):
        f,a,admission,e,p,recovery,links=self.historical_unproved_fixture()
        proof=self.prove(a,e,admission,recovery)
        self.assertIsNotNone(proof);self.assertIn('paid_contract_proof_hash',proof)
        original_link=copy.deepcopy(links[-1]);original_budget=copy.deepcopy(recovery['budget'])
        p.candidate_wire['targets'][0]['figure']['period']='FY2025';e.draft_worker.lease_seconds=30
        current=copy.deepcopy(admission['model_authority']);current['draft']['routing_policy_hash']='c'*64
        a.model_execution_resolver=lambda:(copy.deepcopy(admission['model_execution']),current)
        refreshed=a.resolve_for_execution(admission['id'])
        self.assertIsNotNone(refreshed.get('model_authority_refresh'))
        runtime=types.SimpleNamespace(admission=refreshed,authority=a,scheduler=e.scheduler,executor=e,
            transition_budget=lambda admission:40,wait_until_claimable=lambda ref:True)
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)):
            result=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                planner_model_config=f.state/'unused',document_research_config=f.state/'unused',
                admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],
                unused_recovery_link_ref=links[-1]['id'],unused_recovery_link_hash=links[-1]['content_hash'])
        self.assertEqual(result['status'],'complete');self.assertEqual(p.calls,2)
        row=f.store.connection.execute('SELECT rebound_work_order_ref FROM mission_document_research_model_authority_epoch_rebinds WHERE recovery_link_ref=?',(links[-1]['id'],)).fetchone()
        rebound=e.scheduler.work_order_authority(row[0])['work_order']
        self.assertNotEqual(rebound['id'],recovery['id']);self.assertEqual(rebound['budget'],original_budget)
        self.assertIn('directed-numeric-original-label-period:0.1',rebound['question'])
        from dalton_core.directed_numeric_recovery import read_contract_retry_links
        self.assertEqual(read_contract_retry_links(f.store.connection,admission,2),[original_link])
        self.assertEqual(e.scheduler.attempt_history(recovery['id'])[0]['state'],'ready')
        self.assertIsNone(self.prove(a,e,admission,recovery))

    def test_cli_rechecks_used_link_before_any_executor_call(self):
        f,a,admission,e,p,recovery,links=self.fixture()
        e.scheduler.claim(owner_ref=e.draft_worker.worker_ref,work_order_id=recovery['id'],lease_seconds=1)
        runtime=types.SimpleNamespace(admission=admission,authority=a,scheduler=e.scheduler,executor=e)
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(e,'run_once',side_effect=AssertionError('no execution')):
            with self.assertRaisesRegex(ValueError,'no longer exact and unused'):
                run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',
                    planner_model_config=f.state/'unused',document_research_config=f.state/'unused',
                    admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],
                    unused_recovery_link_ref=links[-1]['id'],unused_recovery_link_hash=links[-1]['content_hash'])
        self.assertEqual(p.calls,1)
