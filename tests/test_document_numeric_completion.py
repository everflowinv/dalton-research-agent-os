import copy,json,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_directed_numeric_promotion_reconciliation as promotion
from tests import test_mission_document_research_lane as lane_fixture
from dalton_core.document_numeric_completion import completed_numeric_admission
from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator,_read_holds
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.store import canonical_json,content_hash


class NumericCompletionTests(unittest.TestCase):
    def setUp(self):
        self.parent=promotion.PromotionReconciliationTests('runTest');self.addCleanup(self.parent.doCleanups)
        h,w,f,a,self.admission,self.executor,p,v=self.parent.fixture()
        self.done=self.parent.call(self.executor,self.admission)
        self.connection=self.executor.connection

    def test_actual_zero_cost_promotion_retires_old_hold_without_dispatch(self):
        self.assertTrue(completed_numeric_admission(self.connection,self.admission['id']))
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        launcher=lane_fixture._Launcher(Path(temp.name)/'tickets')
        store=types.SimpleNamespace(connection=self.connection,active_policy=lambda:{'policy':{}})
        lane=MissionDocumentResearchCoordinator(store=store,launcher=launcher)
        lane._hold({},self.admission,reason='reentry_failed_after_automatic_rebind:controlled reentry was already attempted',ticket_ref='mission-document-research:old',disposition='recovery_required')
        self.assertNotIn(self.admission['id'],[a['id'] for a in lane._admissions()])
        before=self.connection.total_changes
        with patch.object(lane,'_latest_plan_row',return_value=None):result=lane.dispatch_once()
        self.assertNotIn(self.admission['id'],_read_holds(lane.holds_path))
        self.assertEqual(launcher.resumed,[]);self.assertEqual(launcher.started,[])
        self.assertEqual(self.connection.total_changes,before)

    def test_pending_targets_never_retire_a_hold(self):
        value=copy.deepcopy(self.done['numeric_result']);value['status']='waiting_evidence'
        value['pending_targets']=[{'target_ref':value['completed_targets'][0]['target_ref'],'reason':'still unverified'}]
        self.replace_receipt(value)
        self.assertFalse(completed_numeric_admission(self.connection,self.admission['id']))

    def replace_receipt(self,value):
        """An isolated sealed owner-output shape; never touches production."""
        from dalton_core.mission_document_research_executor import _formal_hash
        value['content_hash']=content_hash({k:v for k,v in value.items()if k!='content_hash'})
        formal=dict(self.connection.execute('SELECT * FROM scheduler_formal_results WHERE work_order_id=?',(value['work_order_ref'],)).fetchone())
        envelope=json.loads(formal['result_envelope_json']);envelope['outputs']=value
        formal['result_envelope_json']=canonical_json(envelope);formal['result_envelope_hash']=content_hash(envelope)
        formal['content_hash']=_formal_hash(formal)
        for table in ('mission_document_numeric_results','scheduler_formal_results'):
            rows=self.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall()
            for row in rows:self.connection.execute('DROP TRIGGER '+row['name'])
        self.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=? WHERE admission_ref=?',
            (canonical_json(value),value['content_hash'],self.admission['id']))
        self.connection.execute('UPDATE scheduler_formal_results SET result_envelope_json=?,result_envelope_hash=?,content_hash=? WHERE work_order_id=?',
            (formal['result_envelope_json'],formal['result_envelope_hash'],formal['content_hash'],value['work_order_ref']))

    def test_missing_or_duplicate_completed_target_does_not_close(self):
        original=copy.deepcopy(self.done['numeric_result'])
        for targets in (original['completed_targets'][:-1],original['completed_targets']+[original['completed_targets'][0]]):
            value=copy.deepcopy(original);value['completed_targets']=targets;self.replace_receipt(value)
            with self.assertRaisesRegex(DirectedNumericError,'every admitted target'):
                completed_numeric_admission(self.connection,self.admission['id'])

    def test_exact_formal_outputs_and_result_parent_cannot_be_substituted(self):
        value=copy.deepcopy(self.done['numeric_result']);value['admission_hash']='f'*64;self.replace_receipt(value)
        with self.assertRaisesRegex(DirectedNumericError,'parent binding'):
            completed_numeric_admission(self.connection,self.admission['id'])

    def test_receipt_hash_drift_preserves_hold(self):
        for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='mission_document_numeric_results'"):
            self.connection.execute('DROP TRIGGER '+row['name'])
        self.connection.execute('UPDATE mission_document_numeric_results SET content_hash=? WHERE admission_ref=?',('0'*64,self.admission['id']))
        with self.assertRaises(DirectedNumericError):completed_numeric_admission(self.connection,self.admission['id'])

    def test_formal_missing_does_not_retire_hold(self):
        from dalton_core.scheduler import Scheduler
        with patch.object(Scheduler,'formal_result',return_value=None):
            self.assertFalse(completed_numeric_admission(self.connection,self.admission['id']))

    def test_wrong_admission_or_absent_result_never_retires_hold(self):
        self.assertFalse(completed_numeric_admission(self.connection,'mission-document-research-admission:unknown'))
        row=self.connection.execute('SELECT * FROM mission_document_numeric_results WHERE admission_ref=?',(self.admission['id'],)).fetchone()
        with patch('dalton_core.document_numeric_completion.json.loads',side_effect=[json.loads(row['record_json']),{**self.admission,'id':'foreign'}]):
            with self.assertRaises(DirectedNumericError):completed_numeric_admission(self.connection,self.admission['id'])
