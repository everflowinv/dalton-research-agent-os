"""A lossy historical key cannot erase a typed period disagreement."""
import unittest
from tests import test_directed_numeric_measure_conflict as conflict_fixture
from dalton_core.directed_numeric_executor import source_binding
from dalton_core.directed_numeric_output import quotes
from dalton_core.directed_numeric_research import CONTRACT_REF
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver


class NumericPeriodIdentityCollisionTests(unittest.TestCase):
    def test_original_period_collision_becomes_source_bound_pending_without_new_calls(self):
        for old_period in ('fy2025', 'FY2025全年', 'FY2025上半年'):
            with self.subTest(old_period=old_period):
                fixture = conflict_fixture.NumericMeasureConflictTests(methodName='runTest')
                self.addCleanup(fixture.doCleanups)
                h, w, f, authority, admission, executor, producer, verifier = fixture.fixture()
                proof = authority.registry.search(admission['request'])
                quote, text = next(iter(quotes(proof).items()))
                original = {**producer.candidate_wire['targets'][0]['figure'],
                            'period': old_period, 'quote_id': quote, 'citation_text': text}
                source = source_binding(f.store.connection, admission['request']['registration'],
                                        admission['company_ref'])
                authority._missions.record_document_figures(
                    company_ref=admission['company_ref'], review_ref=source['review_id'],
                    document_ref=source['document_ref'], source_manifest_hash=source['source_manifest_hash'],
                    source_grade=source['source_grade'], figures=[original],
                    observed_by=admission['actor_ref'], numeric_contract_ref=CONTRACT_REF)
                old = authority._missions.document_figures(company_ref=admission['company_ref'], limit=2000)[0]
                result = w.drive(executor, admission)
                receipt = result['numeric_result']
                self.assertEqual(result['research_status'], 'numeric_waiting_evidence')
                self.assertEqual(len(receipt['completed_targets']), 2)
                conflict = receipt['pending_targets'][0]['measure_conflict_proof']
                self.assertEqual(conflict['differing_fields'], ['period'])
                self.assertEqual(conflict['held_fact']['period'], old_period)
                self.assertEqual(conflict['verified_answer']['period'], 'FY2025')
                self.assertEqual(MissionFigureAuthorityResolver(f.store.connection).figure(old['figure_id']), old)
                # Even similar text is not accepted as an equivalent reporting
                # interval. Other supported targets can still be committed.
                self.assertEqual(receipt['material_requests'], [])
                self.assertEqual(executor.run_once(admission['id']), result)
                self.assertEqual((producer.calls, verifier.calls), (1, 1))


class PeriodIdentityLocalReplayTests(unittest.TestCase):
    def fixture(self,old_period='FY2025上半年'):
        from tests import test_directed_numeric_local_reconciliation as local_fixture
        helper=local_fixture.NumericLocalReconciliationTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        h,w,f,a,admission,ex,p,v,works=helper.fixture(seed=False)
        proof=a.registry.search(admission['request']);quote,text=next(iter(quotes(proof).items()))
        original={**p.candidate_wire['targets'][0]['figure'],'period':old_period,'quote_id':quote,'citation_text':text}
        source=source_binding(f.store.connection,admission['request']['registration'],admission['company_ref'])
        a._missions.record_document_figures(company_ref=admission['company_ref'],review_ref=source['review_id'],
            document_ref=source['document_ref'],source_manifest_hash=source['source_manifest_hash'],
            source_grade=source['source_grade'],figures=[original],observed_by=admission['actor_ref'],numeric_contract_ref=CONTRACT_REF)
        old=a._missions.document_figures(company_ref=admission['company_ref'],metric_ref=original['metric_ref'],limit=2000)[0]
        helper.attempted(f,ex,works[3],exhaust=True)
        return helper,h,w,f,a,admission,ex,p,v,works,old

    def preserved(self,h,f,ex,works):
        tables=('model_invocations','observability_usage_entries','observability_cost_entries')
        return {'paid':{t:h.rows(ex.connection,t)for t in tables},
            'budget':{t:h.rows(f.budget.connection,t)for t in ('thesis_impact_day_admissions','thesis_impact_day_settlements')},
            'parents':[(ex.scheduler.work_order_authority(w['id']),ex.scheduler.attempt_history(w['id']))for w in works]}

    def test_exhausted_old_local_failure_replays_to_period_conflict_without_changing_fee_or_parent(self):
        from unittest.mock import patch
        from dalton_core.directed_numeric_local_reconciliation import qualification,reconcile,TABLE
        _,h,w,f,a,admission,ex,p,v,works,old=self.fixture();before=self.preserved(h,f,ex,works)
        changes=ex.connection.total_changes
        evidence=qualification(ex,admission['id'],admission['content_hash'])[2]['repair_evidence']
        conflicts=[item['proof']for item in evidence if item['kind']=='numeric_measure_identity_conflict']
        self.assertEqual(len(conflicts),1);self.assertEqual(conflicts[0]['differing_fields'],['period'])
        self.assertEqual(ex.connection.total_changes,changes)
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no model')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
            result=reconcile(ex,admission['id'],expected_admission_hash=admission['content_hash'],requested_by='automation:test')
        self.assertEqual(result['model_calls'],0);self.assertEqual(result['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(result['numeric_result']['completed_targets']),2)
        self.assertEqual(result['numeric_result']['pending_targets'][0]['measure_conflict_proof']['differing_fields'],['period'])
        self.assertEqual(MissionFigureAuthorityResolver(ex.connection).figure(old['figure_id']),old)
        self.assertEqual(self.preserved(h,f,ex,works),before)
        self.assertEqual(ex.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_actual_candidate_lane_launcher_cli_stays_one_child_and_never_marks_period_conflict_completed(self):
        import json,types
        from contextlib import nullcontext
        from unittest.mock import patch
        from dalton_core.store import content_hash
        from dalton_core.document_completed_model_replay import candidate,IDENTITY_ERROR,IDENTITY_CONTRACT
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator,_write_holds
        from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
        from dalton_core.mission_document_research_cli import run_admission
        from dalton_core.lane_child_launcher import LaneChildRejected
        _,h,w,f,a,admission,ex,p,v,works,old=self.fixture();before=self.preserved(h,f,ex,works)
        directory=f.state/'period-identity-tickets';directory.mkdir();original=directory/'old';original.mkdir()
        body={'status':'failed','error':IDENTITY_ERROR,'admission_ref':admission['id'],'admission_hash':admission['content_hash']}
        summary={**body,'content_hash':content_hash(body)};(original/'summary.json').write_text(json.dumps(summary))
        ticket={**body,'id':'mission-document-research:old','summary':summary}
        launcher=object.__new__(MissionDocumentResearchLauncher);launcher.tickets_dir=directory
        launcher._ticket_path=lambda ref:original/'ticket.json'if ref==ticket['id']else directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'isolated':'no physical child'}
        started=[];completed={};launcher.status=lambda ref:ticket if ref==ticket['id']else completed[ref]
        def spawn(**kwargs):
            self.assertEqual(kwargs['record']['contract_ref'],IDENTITY_CONTRACT)
            self.assertTrue(kwargs['completed_models_only']);self.assertEqual(kwargs['record']['completed_model_replay']['model_calls_allowed'],0)
            started.append(kwargs);runtime=types.SimpleNamespace(admission=admission,executor=ex)
            with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(ex,'run_once',side_effect=AssertionError('no generic model loop')):
                done=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',planner_model_config=f.state/'unused',document_research_config=f.state/'unused',admission_ref=admission['id'],expected_admission_hash=admission['content_hash'],completed_models_only=True)
            self.assertEqual(done['outcomes'][0]['model_calls'],0)
            self.assertEqual(done['outcomes'][0]['research_status'],'numeric_waiting_evidence')
            self.assertEqual(len(done['outcomes'][0]['numeric_result']['pending_targets']),1)
            ref=launcher.TICKET_PREFIX+':'+kwargs['digest'];launcher._ticket_path(ref).write_text('{}')
            completed[ref]={'id':ref,'status':'succeeded','summary':done};return completed[ref]
        launcher.spawn=spawn
        from dalton_core.mission_document_research_promotion import ensure_promotion_authority
        ensure_promotion_authority(ex.connection)
        coordinator=MissionDocumentResearchCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        proof=candidate(coordinator,admission,ticket['id']);self.assertEqual(proof['contract_ref'],IDENTITY_CONTRACT)
        _write_holds(coordinator.holds_path,{admission['id']:{'reason':'reentry_failed_after_automatic_rebind:controlled reentry was already attempted','admission_hash':admission['content_hash'],'ticket_ref':ticket['id'],'disposition':'recovery_required','retry_at':None}})
        with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no model')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
            first=coordinator.dispatch_once()
            self.assertEqual(first['reason'],'figure_identity_local_reconciliation',first)
            coordinator.dispatch_once()
        self.assertEqual(len(started),1)
        launcher.configuration=lambda:{'changed':'policy cannot grant a second child'}
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):
            launcher.replay_completed_models(admission_ref=admission['id'],admission_hash=admission['content_hash'],proof=proof)
        self.assertEqual(MissionFigureAuthorityResolver(ex.connection).figure(old['figure_id']),old)
        self.assertEqual(self.preserved(h,f,ex,works),before);self.assertEqual((p.calls,v.calls),(1,1))


if __name__ == '__main__':
    unittest.main()
