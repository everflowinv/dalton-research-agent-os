import copy
import json
import unittest
from unittest.mock import patch
from dalton_core.store import canonical_json, content_hash
from dalton_core.directed_numeric_maintenance import CLASSIFICATION, FAILED_REASON, REPAIR_VERSION, evidence, transform
from dalton_core.directed_numeric_output import legacy_draft_prompt_v0
from tests import test_directed_numeric_workflow as workflow


class NumericTechnicalMaintenanceTests(unittest.TestCase):
    def fixture(self, *, verifier_ceiling=0.1):
        f = workflow.NumericWorkflowTests('runTest'); f.addCleanup=self.addCleanup
        original = workflow.MissionAnnualFixture
        def configured(case, **kwargs):
            instance = original(case, mission_calls=10, **kwargs)
            from tests.test_mission_annual_research import VERIFIER_MODEL_CONFIG_NAME
            path=instance.state / VERIFIER_MODEL_CONFIG_NAME
            config=json.loads(path.read_text()); config['call_budget']['max_cost_usd']=verifier_ceiling
            path.write_text(canonical_json(config))  # before admission; original authority reads this exact config
            return instance
        with patch.object(workflow, 'MissionAnnualFixture', side_effect=configured):
            result = f.fixture()
        result[3].scheduler.max_attempts = 1  # trusted construction cap, independent of historic defaults
        return result

    def drive(self, executor, admission, reason, limit=40):
        for _ in range(limit):
            result=executor.run_once(admission['id'])
            if result.get('reason')==reason: return result
        self.fail(str(result))

    def old_draft(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period']='year ended December 31, 2025'
        with patch('dalton_core.directed_numeric_executor.draft_prompt',legacy_draft_prompt_v0), \
             patch('dalton_core.directed_numeric_recovery.build_numeric_output_repair',return_value=None), \
             patch('dalton_core.directed_numeric_maintenance.attempt',return_value=None):
            self.drive(executor,admission,'contract_failed_after_automatic_retry')
        return f,authority,admission,executor,producer,verifier

    def test_settled_legacy_format_gets_exactly_one_versioned_work_and_retains_old_records(self):
        f,authority,admission,executor,producer,verifier=self.old_draft()
        old_links=[tuple(r) for r in f.store.connection.execute(
            'SELECT * FROM mission_document_research_recovery_links ORDER BY recovery_number')]
        old_work=[tuple(r) for r in executor.scheduler.connection.execute(
            'SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders')]
        calls=producer.calls
        result=executor.run_once(admission['id'])
        self.assertEqual(result['reason'],CLASSIFICATION)
        self.assertEqual(producer.calls,calls)  # authorize/enqueue is zero-call
        link=json.loads(f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_recovery_links ORDER BY recovery_number DESC LIMIT 1').fetchone()[0])
        parent=executor.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        child=executor.scheduler.work_order_authority(link['recovery_work_order_ref'])['work_order']
        self.assertEqual(parent['budget'],child['budget'])
        self.assertEqual(json.loads(parent['question'])['quotes'],json.loads(child['question'])['quotes'])
        self.assertEqual(json.loads(parent['question'])['targets'],json.loads(child['question'])['targets'])
        self.assertEqual(child['metadata']['numeric_technical_maintenance']['repair_version'],REPAIR_VERSION)
        for row in old_work:
            self.assertEqual(tuple(executor.scheduler.connection.execute(
                'SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',(row[0],)).fetchone()),row)
        self.assertEqual([tuple(r) for r in f.store.connection.execute(
            'SELECT * FROM mission_document_research_recovery_links ORDER BY recovery_number LIMIT 2')],old_links)
        self.drive(executor,admission,FAILED_REASON)
        calls=producer.calls
        count=f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_links').fetchone()[0]
        for _ in range(3): self.assertEqual(executor.run_once(admission['id'])['reason'],FAILED_REASON)
        self.assertEqual(producer.calls,calls)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_links').fetchone()[0],count)
        self.assertEqual(count,3)

    def test_day_cap_waits_without_new_work_or_call(self):
        f,authority,admission,executor,producer,verifier=self.old_draft()
        executor.max_automatic_contract_retries_per_day=2
        before=producer.calls
        for _ in range(2):
            result=executor.run_once(admission['id'])
            self.assertEqual(result['reason'],'numeric_technical_maintenance_day_cap_reached')
        self.assertEqual(producer.calls,before)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_links').fetchone()[0],2)

    def test_new_template_failure_is_not_another_technical_generation(self):
        f,authority,admission,executor,producer,verifier=self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period']='FY2024'
        result=self.drive(executor,admission,'numeric_repeated_contract_failure')
        self.assertEqual(f.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_controlled_recovery_authorizations WHERE json_extract(record_json,'$.kind')=?",(CLASSIFICATION,)).fetchone()[0],0)

    def test_old_paid_proof_missing_or_unsettled_never_opens_technical_door(self):
        f,authority,admission,executor,producer,verifier=self.old_draft()
        from dalton_core.mission_document_research_executor import _effective_stage
        work,links=_effective_stage(authority,executor.scheduler,admission,1,worker=executor.draft_worker)
        formal=executor.scheduler.formal_result(work['id'])
        with patch('dalton_core.mission_document_research_executor._paid_contract_failure_proof',return_value=None):
            self.assertIsNone(evidence(authority,executor.scheduler,admission,work,formal,1,executor.draft_worker))
        proof=evidence(authority,executor.scheduler,admission,work,formal,1,executor.draft_worker)
        changed=copy.deepcopy(proof); changed['defect']['repair']['replacement_prompt']='{}'
        changed['content_hash']=content_hash({k:v for k,v in changed.items() if k!='content_hash'})
        with self.assertRaises(ValueError): transform(work,changed)

    def test_maintenance_may_complete_only_through_original_independent_figure_path(self):
        f,authority,admission,executor,producer,verifier=self.old_draft()
        self.assertEqual(executor.run_once(admission['id'])['reason'],CLASSIFICATION)
        producer.candidate_wire['targets'][0]['figure']['period']='FY2025'
        for _ in range(15):
            result=executor.run_once(admission['id'])
            if result.get('status')=='complete': break
        self.assertEqual(result.get('research_status'),'numeric_targets_completed',str(result))
        self.assertGreater(verifier.calls,0)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM coverage_mission_document_figures').fetchone()[0],3)

    def old_verifier(self, *, unavailable=False, verifier_ceiling=0.1):
        from dalton_core.directed_numeric_executor import LEGACY_VERIFIER_SCHEMA_HASHES
        from dalton_core.openclaw_model_adapter import _required_provider_controls, PROVIDER_CONTROL_MODE_REQUIRED
        from dalton_core.directed_numeric_output import candidate_ref
        from dalton_core.contracts import WorkOrder
        f,authority,admission,executor,producer,verifier=self.fixture(verifier_ceiling=verifier_ceiling)
        if unavailable:
            producer.candidate_wire['targets']=[{'target_ref':r['target_ref'],'status':'unavailable',
                'figure':None,'reason':'Exact source does not disclose this target.'} for r in producer.candidate_wire['targets']]
            verifier.candidate_wire['candidate_ref']=candidate_ref(producer.candidate_wire)
            verifier.candidate_wire['targets']=[{'target_ref':r['target_ref'],'verdict':'unavailable',
                'reason':'Exact source does not disclose this target.'} for r in producer.candidate_wire['targets']]
        # Actual deployed provider-retry workers use the direct route branch,
        # preserving MODEL_ADAPTER_REJECTED instead of chain wrapper HALTED.
        executor.verifier_worker._provider_retry_state=lambda work: {
            'excluded_profile_ids': [], 'retry_profile_version_ref': None}
        original_execute=verifier.execute
        def checking(work,route,selected):
            _required_provider_controls(work,route,work.budget['max_output_tokens'],PROVIDER_CONTROL_MODE_REQUIRED)
            return original_execute(work,route,selected)
        verifier.execute=checking
        with patch('dalton_core.directed_numeric_executor.verifier_schema_hash',return_value=LEGACY_VERIFIER_SCHEMA_HASHES[0]), \
             patch('dalton_core.directed_numeric_maintenance.attempt',return_value=None):
            self.drive(executor,admission,'unproved_send_failed_after_automatic_retry')
        return f,authority,admission,executor,producer,verifier

    def test_finite_raw_schema_gets_one_correction_preserving_unknown_charges(self):
        from dalton_core.directed_numeric_executor import verifier_schema_hash
        f,authority,admission,executor,producer,verifier=self.old_verifier()
        budget=executor.verifier_worker.budget_store.connection
        old_settlements=[tuple(r) for r in budget.execute('SELECT * FROM thesis_impact_day_settlements')]
        result=executor.run_once(admission['id'])
        self.assertEqual(result['reason'],CLASSIFICATION)
        child=executor.scheduler.work_order_authority(result['work_order_ref'])['work_order']
        self.assertEqual(child['metadata']['verifier_provider_schema_hash'],verifier_schema_hash())
        link=json.loads(f.store.connection.execute('SELECT record_json FROM mission_document_research_recovery_links ORDER BY recovery_number DESC LIMIT 1').fetchone()[0])
        proof=link['failure_proof']['technical_evidence']
        self.assertEqual(proof['defect']['historical_send_state'],'retained_unproved')
        self.assertNotIn('definitely_not_sent',canonical_json(proof))
        for _ in range(12):
            result=executor.run_once(admission['id'])
            if result.get('status')=='complete': break
        self.assertEqual(result.get('research_status'),'numeric_targets_completed',str(result))
        self.assertEqual(verifier.calls,1)
        for row in old_settlements:
            self.assertIn(row,[tuple(r) for r in budget.execute('SELECT * FROM thesis_impact_day_settlements')])

    def test_all_unavailable_schema_repair_stays_material_gap_not_claims(self):
        f,authority,admission,executor,producer,verifier=self.old_verifier(unavailable=True)
        self.assertEqual(executor.run_once(admission['id'])['reason'],CLASSIFICATION)
        for _ in range(12):
            result=executor.run_once(admission['id'])
            if result.get('status')=='complete': break
        self.assertEqual(result.get('research_status'),'numeric_waiting_evidence',str(result))
        self.assertEqual(result['numeric_result']['canonical_claims'],[])
        self.assertEqual(result['numeric_result']['completed_targets'],[])
        self.assertEqual(len(result['numeric_result']['pending_targets']),3)

    def test_unknown_hash_post_send_or_non_numeric_are_never_migrated(self):
        from dalton_core.mission_document_research_executor import _effective_stage
        f,authority,admission,executor,producer,verifier=self.old_verifier()
        work,_=_effective_stage(authority,executor.scheduler,admission,2,worker=executor.verifier_worker)
        formal=executor.scheduler.formal_result(work['id'])
        for field,value in [('verifier_provider_schema_hash','f'*64),('numeric_contract_ref','other')]:
            changed=copy.deepcopy(work); changed['metadata'][field]=value
            self.assertIsNone(evidence(authority,executor.scheduler,admission,changed,formal,2,executor.verifier_worker))
        changed=copy.deepcopy(formal); changed['result_envelope']['error']['code']='POST_SEND_RESULT_UNKNOWN'
        changed['result_envelope_hash']=content_hash(changed['result_envelope'])
        from dalton_core.mission_document_research_executor import MissionDocumentResearchExecutorError
        with self.assertRaises(MissionDocumentResearchExecutorError):
            evidence(authority,executor.scheduler,admission,work,changed,2,executor.verifier_worker)

    def test_lane_wakes_old_proven_candidate_and_failed_migration_is_administrator(self):
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        from dalton_core.needs_human import _numeric_contract_system_item
        f,authority,admission,executor,producer,verifier=self.old_draft()
        from dalton_core.mission_document_research_executor import _effective_stage
        work,_=_effective_stage(authority,executor.scheduler,admission,1,worker=executor.draft_worker)
        lane=MissionDocumentResearchCoordinator(store=f.store,launcher=None,clock=f.harness.clock)
        state=lane._typed_recovery_state(admission,work['id'])
        self.assertEqual(state['reason'],'numeric_technical_maintenance_candidate')
        executor.run_once(admission['id']); self.drive(executor,admission,FAILED_REASON)
        card=_numeric_contract_system_item(f.store.connection,'mission_document_research',
            {'admission_ref':admission['id'],'reason':FAILED_REASON},at='',path='/private/holds.json',subject={})
        self.assertFalse(card['actionable'])
        self.assertEqual(card['responsibility'],'administrator')
        self.assertEqual(card['detail']['owner_doors'],[])
        self.assertNotIn('买',card['action'])

    def test_budget_pool_waits_same_day_then_one_proved_no_send_round_completes(self):
        from datetime import timedelta
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        f,authority,admission,executor,producer,verifier=self.old_verifier(verifier_ceiling=1.0)
        self.assertEqual(executor.run_once(admission['id'])['reason'],CLASSIFICATION)
        executor.run_once(admission['id'])  # original budget rejects before fake provider
        result=executor.run_once(admission['id'])
        self.assertEqual(result['reason'],'numeric_maintenance_budget_waiting')
        self.assertEqual(verifier.calls,0)
        before=f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0]
        for _ in range(4):
            self.assertEqual(executor.run_once(admission['id'])['reason'],'numeric_maintenance_budget_waiting')
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],before)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_rounds').fetchone()[0],0)
        lane=MissionDocumentResearchCoordinator(store=f.store,launcher=None,clock=f.harness.clock)
        self.assertEqual(lane._typed_recovery_state(admission,result['work_order_ref'])['action'],'waiting')
        old_settlements=[tuple(r) for r in executor.verifier_worker.budget_store.connection.execute('SELECT * FROM thesis_impact_day_settlements')]
        f.harness.clock.value += timedelta(days=1)
        for _ in range(12):
            result=executor.run_once(admission['id'])
            if result.get('status')=='complete': break
        self.assertEqual(result.get('research_status'),'numeric_targets_completed',str(result))
        self.assertEqual(verifier.calls,1)
        rows=[json.loads(r[0]) for r in f.store.connection.execute('SELECT record_json FROM mission_document_research_recovery_links')]
        self.assertEqual(sum(r['failure_proof']['classification']==CLASSIFICATION for r in rows),1)
        rounds=[json.loads(r[0]) for r in f.store.connection.execute('SELECT record_json FROM mission_document_research_recovery_rounds')]
        self.assertEqual(len(rounds),1)
        self.assertEqual(rounds[0]['actor_ref'],'automation:document-research-fresh-work-round')
        self.assertEqual(rounds[0]['basis']['no_send_budget_proof']['classification'],'atomic_pool_budget_refusal')
        for row in old_settlements:
            self.assertIn(row,[tuple(r) for r in executor.verifier_worker.budget_store.connection.execute('SELECT * FROM thesis_impact_day_settlements')])

    def test_maintenance_day_cap_waiting_controls_lane_and_successive_days(self):
        from datetime import timedelta
        from dalton_core.directed_numeric_recovery import read_numeric_contract_hold
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        f,authority,admission,executor,producer,verifier=self.old_draft()
        executor.max_automatic_contract_retries_per_day=2
        result=executor.run_once(admission['id'])
        lane=MissionDocumentResearchCoordinator(store=f.store,launcher=None,clock=f.harness.clock)
        self.assertEqual(lane._typed_recovery_state(admission,result['work_order_ref'])['action'],'waiting')
        before=producer.calls
        f.harness.clock.value += timedelta(days=1)
        executor.max_automatic_contract_retries_per_day=0
        for _ in range(2): self.assertEqual(executor.run_once(admission['id'])['reason'],'numeric_technical_maintenance_day_cap_reached')
        self.assertEqual(lane._typed_recovery_state(admission,result['work_order_ref'])['action'],'waiting')
        state=read_numeric_contract_hold(f.store.connection,admission['id'])
        self.assertTrue(state['budget_waiting'])
        self.assertEqual(state['retry_at'][:10],(f.harness.clock.value+timedelta(days=1)).date().isoformat())
        self.assertEqual(producer.calls,before)

    def test_two_connections_claim_same_scope_and_partial_claim_replays_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import contextmanager
        from types import SimpleNamespace
        from dalton_core.store import DaltonStore
        from dalton_core.mission_document_research_executor import _effective_stage
        from dalton_core.directed_numeric_maintenance import attempt
        f,authority,admission,executor,producer,verifier=self.old_draft()
        work,links=_effective_stage(authority,executor.scheduler,admission,1,worker=executor.draft_worker)
        formal=executor.scheduler.formal_result(work['id'])
        verified=evidence(authority,executor.scheduler,admission,work,formal,1,executor.draft_worker)
        def claim(_):
            store=DaltonStore(f.store.path)
            try:
                from dalton_core.store import authorization_flag
                flag=authorization_flag(store.connection,'dalton_mission_document_research_executor_authorized')
                @contextmanager
                def transaction():
                    flag.authorized=True
                    try:
                        with store._transaction() as cur: yield cur
                    finally: flag.authorized=False
                proxy=SimpleNamespace(authority=None,scheduler=None,connection=store.connection,
                    clock=f.harness.clock,draft_worker=None,verifier_worker=None,
                    _transaction=transaction,max_automatic_contract_retries_per_day=20,
                    _contract_retry_disposition=executor._contract_retry_disposition,
                    _unproved_retry_disposition=executor._unproved_retry_disposition,
                    _append_controlled_retry=lambda *args,**kwargs:{'status':'admitted','work_order_ref':'not-enqueued','authorization_ref':args[5]['id']},
                    _recovery_observation=lambda *args,**kwargs:None)
                # The exact strong proof above is frozen before concurrent SQL.
                # Only claim+atomic persistence is under test, never fee proof.
                return attempt(proxy,admission,work,formal,1,links)['authorization_ref']
            finally: store.close()
        from dalton_core.mission_document_research_executor import _expected_budget_binding
        binding=_expected_budget_binding(authority,admission,1)
        with patch('dalton_core.directed_numeric_maintenance.evidence',return_value=verified), \
             patch('dalton_core.mission_document_research_executor._expected_budget_binding',return_value=binding):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(claim,range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(f.store.connection.execute(
            "SELECT count(*) FROM mission_document_research_controlled_recovery_authorizations WHERE json_extract(record_json,'$.kind')=?",
            (CLASSIFICATION,)).fetchone()[0],1)
        result=executor.run_once(admission['id'])
        self.assertEqual(result['authorization_ref'],results[0])
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_links').fetchone()[0],3)

    def test_budget_proof_with_invocation_or_nonbudget_classification_cannot_backoff(self):
        from dalton_core.mission_document_research_executor import _effective_stage
        f,authority,admission,executor,producer,verifier=self.old_verifier(verifier_ceiling=1.0)
        executor.run_once(admission['id']); executor.run_once(admission['id'])
        work,links=_effective_stage(authority,executor.scheduler,admission,2,worker=executor.verifier_worker)
        formal=executor.scheduler.formal_result(work['id'])
        with patch.object(executor,'_safe_failure_proof',return_value={'classification':'proved_paid_output_contract_failure'}):
            result=executor.run_once(admission['id'])
        self.assertEqual(result['reason'],FAILED_REASON)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_recovery_rounds').fetchone()[0],0)

    def test_model_authority_roll_preserves_technical_generation_marker(self):
        f,authority,admission,executor,producer,verifier=self.old_draft()
        execution=copy.deepcopy(admission['model_execution'])
        model_authority=copy.deepcopy(admission['model_authority'])
        model_authority['draft']['routing_policy_hash']='a'*64
        authority.model_execution_resolver=lambda: (execution,model_authority)
        result=executor.run_once(admission['id'])
        self.assertEqual(result['reason'],CLASSIFICATION)
        rebound=executor.scheduler.work_order_authority(result['work_order_ref'])['work_order']
        self.assertEqual(rebound['metadata']['numeric_technical_maintenance']['repair_version'],REPAIR_VERSION)
        self.assertIn('previous_output_error',json.loads(rebound['question']))
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_model_authority_epoch_rebinds').fetchone()[0],1)
