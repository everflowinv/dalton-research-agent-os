import json,types,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.document_completed_model_replay import candidate,CONTRACT,ERROR
from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
from dalton_core.mission_document_research_executor import MissionDocumentResearchExecutorError
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as numeric_fixtures
from tests import test_directed_numeric_local_reconciliation as local_fixtures

class CompletedModelReplayTests(unittest.TestCase):
    fixture=numeric_fixtures.NumericWorkflowTests.fixture
    _executor=numeric_fixtures.NumericWorkflowTests._executor
    _record_plan=numeric_fixtures.NumericWorkflowTests._record_plan

    def models_complete(self):
        f,a,admission,e,p,v=self.fixture()
        for _ in range(12):
            outcome=e.run_once(admission['id'])
            if v.calls:break
        self.assertEqual((p.calls,v.calls),(1,1))
        return f,a,admission,e,p,v

    def test_cli_exhausted_local_stage_finishes_without_executor_model_loop(self):
        from contextlib import nullcontext
        from dalton_core.mission_document_research_cli import run_admission
        h=local_fixtures.NumericLocalReconciliationTests(methodName='runTest');self.addCleanup(h.doCleanups)
        helper,w,f,a,admission,e,p,v,works=h.fixture()
        h.attempted(f,e,works[3],exhaust=True)
        original=e.scheduler.attempt_history(works[3]['id'])
        runtime=types.SimpleNamespace(admission=admission,executor=e)
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(e,'run_once',side_effect=AssertionError('no generic execution')),patch.object(e.draft_worker,'run_once',side_effect=AssertionError('no model')),patch.object(e.verifier_worker,'run_once',side_effect=AssertionError('no model')):
            result=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',planner_model_config=f.state/'unused',document_research_config=f.state/'unused',admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],completed_models_only=True)
        self.assertEqual(result['status'],'complete');self.assertEqual(result['outcomes'][0]['model_calls'],0)
        self.assertEqual(result['outcomes'][0]['research_status'],'numeric_targets_completed')
        self.assertEqual((p.calls,v.calls),(1,1));self.assertEqual(e.scheduler.attempt_history(works[3]['id']),original)

    def proof_fixture(self):
        f,a,admission,e,p,v=self.models_complete()
        directory=f.state/'test-replay-ticket';directory.mkdir()
        body={'admission_ref':admission['id'],'admission_hash':admission['content_hash'],'status':'failed','error':ERROR}
        summary={**body,'content_hash':content_hash(body)};(directory/'summary.json').write_text(json.dumps(summary))
        ticket={**body,'id':'mission-document-research:test','summary':summary}
        c=object.__new__(MissionDocumentResearchCoordinator);c.store=f.store
        c.launcher=types.SimpleNamespace(status=lambda ref:ticket,_ticket_path=lambda ref:directory/'ticket.json')
        return f,admission,e,c,ticket

    def test_candidate_binds_successful_stages_and_rejects_other_error(self):
        f,a,e,c,t=self.proof_fixture();proof=candidate(c,a,t['id'])
        self.assertEqual(proof['model_calls_allowed'],0);self.assertEqual(len(proof['completed_stages']),3)
        t['summary']['error']='provider TIMEOUT';self.assertIsNone(candidate(c,a,t['id']))

    def test_source_summary_change_is_not_replayed(self):
        f,a,e,c,t=self.proof_fixture();c.launcher._ticket_path(t['id']).with_name('summary.json').write_text('{}')
        self.assertIsNone(candidate(c,a,t['id']))

    def test_orphaned_child_requires_the_same_durable_failed_summary(self):
        f,a,e,c,t=self.proof_fixture();t['status']='orphaned'
        self.assertIsNotNone(candidate(c,a,t['id']))
        t['status']='running';self.assertIsNone(candidate(c,a,t['id']))

    def test_postprocess_scope_stays_one_across_configuration_changes(self):
        f,a,e,c,t=self.proof_fixture();proof=candidate(c,a,t['id'])
        launcher=object.__new__(MissionDocumentResearchLauncher);directory=f.state/'new-launch';directory.mkdir()
        launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'new':'configuration'}
        def spawn(**kw):
            ref=launcher.TICKET_PREFIX+':'+kw['digest'];launcher._ticket_path(ref).write_text('{}')
            self.assertTrue(kw['completed_models_only']);return {'id':ref,'status':'running'}
        launcher.spawn=spawn;launcher.status=lambda ref:{'id':ref,'status':'failed'}
        launcher.replay_completed_models(admission_ref=a['id'],admission_hash=a['content_hash'],proof=proof)
        launcher.configuration=lambda:{'another':'configuration'}
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):
            launcher.replay_completed_models(admission_ref=a['id'],admission_hash=a['content_hash'],proof=proof)

if __name__=='__main__':unittest.main()
