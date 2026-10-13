"""A reported measure split archives paid proof without replacing old facts."""
import copy
import json
import unittest
from unittest.mock import patch
from tests import test_directed_numeric_figure_support as support_fixture
from tests import test_directed_numeric_measure_conflict as conflict_fixture
from tests import test_directed_numeric_workflow as workflow
from dalton_core.store import canonical_json,content_hash
from dalton_core.directed_numeric_executor import read_numeric_result,_execution_proof
from dalton_core.directed_numeric_research import DirectedNumericError
from dalton_core.directed_numeric_measure_reconciliation import reconcile,TABLE,RESULTS,_validate
from dalton_core.directed_numeric_measure_identity import measure_support,verify_measure
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver


class SourceMeasureIdentityTests(unittest.TestCase):
    def fixture(self,*,same_amount=False,company=None,missing=False,local_origin=False):
        h=support_fixture.NumericFigureSupportTests(methodName='runTest');self.addCleanup(h.doCleanups)
        from tests import test_mission_annual_research as annual
        from tests import test_mission_document_research as document
        from tests import test_research_plan_annual_report as plans
        text=workflow.TEXT.replace('. ','.\n')
        from contextlib import ExitStack
        with ExitStack() as patches:
            patches.enter_context(patch.object(workflow,'TEXT',text))
            if company:
                for module in (workflow,annual,document,plans):
                    if hasattr(module,'COMPANY'):patches.enter_context(patch.object(module,'COMPANY',company))
            w,f,a,admission,ex,p,v=h.fixture(missing=missing)
        old=conflict_fixture.NumericMeasureConflictTests(methodName='runTest').seed_collision(h,f,a,admission,ex,p,same_amount=same_amount)
        if local_origin:
            from tests import test_directed_numeric_local_reconciliation as local_fixture
            from dalton_core.directed_numeric_local_reconciliation import _chain,reconcile as original_reconcile
            for _ in range(15):
                outcome=ex.run_once(admission['id'])
                if outcome.get('stage')=='qualitative_candidate_staging':break
            works=_chain(ex,admission)
            local_fixture.NumericLocalReconciliationTests(methodName='runTest').attempted(f,ex,works[3])
            prior=original_reconcile(ex,admission['id'],expected_admission_hash=admission['content_hash'],requested_by='automation:synthetic')['numeric_result']
        else:prior=w.drive(ex,admission)['numeric_result']
        self.assertEqual(prior['status'],'waiting_evidence')
        self.assertEqual(len(prior['completed_targets']),1 if missing else 2)
        return h,w,f,a,admission,ex,p,v,old,prior

    def call(self,ex,a):
        return reconcile(ex,a['id'],expected_admission_hash=a['content_hash'],requested_by='automation:synthetic-maintenance')

    def test_two_companies_split_same_concept_period_reported_measures_without_overwrite_or_calls(self):
        for company in ('company:ticker:alpha','company:ticker:beta'):
            with self.subTest(company=company):
                h,w,f,a,ad,ex,p,v,old,prior=self.fixture(company=company)
                rows=[h.rows(f.store.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')]
                old_result=f.store.connection.execute('SELECT * FROM mission_document_numeric_results').fetchone()
                with patch.object(ex.draft_worker,'run_once',side_effect=AssertionError('no producer')),patch.object(ex.verifier_worker,'run_once',side_effect=AssertionError('no verifier')):
                    done=self.call(ex,ad)
                    self.assertEqual(done['research_status'],'numeric_targets_completed')
                    self.assertEqual(done['model_calls'],0)
                    self.assertEqual(self.call(ex,ad),done)
                    self.assertEqual(ex.run_once(ad['id'])['numeric_result'],done['numeric_result'])
                from dalton_core.directed_numeric_executor import numeric_feedback
                feedback=numeric_feedback(f.store.connection,ad['mission_version_ref'])
                self.assertEqual(feedback[0]['outcome'],'numeric_targets_completed')
                self.assertEqual(feedback[0]['numeric_result_hash'],done['numeric_result']['content_hash'])
                from dalton_core.directed_numeric_research import pending_numeric_result
                self.assertIsNone(pending_numeric_result(f.store.connection,ad['id']))
                self.assertEqual(MissionFigureAuthorityResolver(f.store.connection).figure(old['figure_id']),old)
                self.assertEqual(tuple(f.store.connection.execute('SELECT * FROM mission_document_numeric_results').fetchone()),tuple(old_result))
                self.assertEqual([h.rows(f.store.connection,t) for t in ('model_invocations','observability_usage_entries','observability_cost_entries')],rows)
                self.assertEqual((p.calls,v.calls),(1,1))
                self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],1)
                self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+RESULTS).fetchone()[0],1)
                mapping=done['numeric_result']['source_measures'][0]['mapping']
                self.assertEqual(mapping['measure_identity']['company_ref'],company)
                self.assertNotEqual(mapping['figure_ref'],old['figure_id'])
                works=h.works(ex,prior)
                original=read_numeric_result(ex,ad,works,ex.scheduler.formal_result(works[3]['id']),include_supplement=False)
                self.assertEqual(original['numeric_result'],prior)

    def test_same_measure_new_value_is_not_a_new_measure(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        works=h.works(ex,prior);proof,draft,verifier,_=_execution_proof(ex,ad,works)
        target=ad['numeric_authority']['targets'][0]
        # Genuine same reported line containing both numbers: changing its
        # chosen amount must not bypass the concept identity via new target words.
        held=copy.deepcopy(old);held['as_reported_label']=draft['output']['targets'][0]['figure']['as_reported_label']
        held['content_hash']=content_hash({k:x for k,x in held.items() if k!='content_hash'})
        from dalton_core.directed_numeric_figure_support import NumericMeasureIdentityConflict
        conflict=copy.deepcopy(prior['pending_targets'][0]['measure_conflict_proof'])
        conflict['held_fact']['as_reported_label']=conflict['verified_answer']['as_reported_label'];conflict['differing_fields']=['value']
        conflict['content_hash']=content_hash({k:x for k,x in conflict.items() if k!='content_hash'})
        with patch('dalton_core.directed_numeric_figure_support.figure_support',side_effect=NumericMeasureIdentityConflict(conflict)):
            with self.assertRaisesRegex(DirectedNumericError,'same-measure amount'):measure_support(connection=f.store.connection,registry=a.registry,admission=ad,target=target,figure=held,search_proof=proof,draft=draft,verifier=verifier)

    def test_target_label_definition_or_company_mutation_cannot_authorize_mapping(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        proof,draft,verifier,_=_execution_proof(ex,ad,h.works(ex,prior));target=ad['numeric_authority']['targets'][0]
        for field in ('metric_ref','metric_definition_hash'):
            changed=copy.deepcopy(target);changed[field]='f'*64
            with self.subTest(field=field),self.assertRaises(DirectedNumericError):measure_support(connection=f.store.connection,registry=a.registry,admission=ad,target=changed,figure=old,search_proof=proof,draft=draft,verifier=verifier)
        changed=copy.deepcopy(ad);changed['company_ref']='company:ticker:wrong'
        with self.assertRaises(DirectedNumericError):measure_support(connection=f.store.connection,registry=a.registry,admission=changed,target=target,figure=old,search_proof=proof,draft=draft,verifier=verifier)

    def test_both_quote_proofs_and_actual_current_retractions_are_required(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        done=self.call(ex,ad);auth,works,_=_validate(ex,ad,f.store.connection.execute('SELECT * FROM '+TABLE).fetchone())
        proof,draft,verifier,_=_execution_proof(ex,ad,works);mapping=done['numeric_result']['source_measures'][0]['mapping']
        figure=MissionFigureAuthorityResolver(f.store.connection).figure(mapping['figure_ref']);target=ad['numeric_authority']['targets'][0]
        for key in ('held_quote_hash','answer_quote_hash','draft_proof_hash','verifier_proof_hash'):
            changed=copy.deepcopy(mapping);changed['conflict_proof'][key]='f'*64
            changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
            with self.subTest(key=key),self.assertRaises(DirectedNumericError):verify_measure(changed,connection=f.store.connection,registry=a.registry,admission=ad,target=target,figure=figure,search_proof=proof,draft=draft,verifier=verifier)
        a._missions.retract_document_figure(old['figure_id'],reason='synthetic withdrawal',retracted_by=ad['actor_ref'])
        with self.assertRaises(Exception):self.call(ex,ad)

    def test_failure_consumes_only_one_zero_cost_work_no_successor_generation(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        with patch('dalton_core.directed_numeric_executor._record_numeric',side_effect=RuntimeError('local failure')):
            with self.assertRaisesRegex(RuntimeError,'local failure'):self.call(ex,ad)
        works=h.rows(f.store.connection,'scheduler_work_orders')
        self.assertEqual(self.call(ex,ad)['reason'],'numeric_measure_reconciliation_failed')
        self.assertEqual(h.rows(f.store.connection,'scheduler_work_orders'),works)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_partial_with_other_missing_evidence_stays_partial(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture(same_amount=True,missing=True)
        done=self.call(ex,ad)
        # Qualification does not turn an already complete result into another
        # maintenance generation. The immutable origin remains explicitly bound.
        self.assertEqual(done['numeric_result']['measure_reconciliation']['prior_numeric_result_hash'],prior['content_hash'])
        self.assertEqual(len(done['numeric_result']['completed_targets']),2)
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')
        self.assertEqual([p['target_ref'] for p in done['numeric_result']['pending_targets']],['target:leases'])
        with self.assertRaisesRegex(Exception,'append-only'):f.store.connection.execute('DELETE FROM '+RESULTS)

    def test_historical_local_partial_keeps_original_authorization_and_reader_after_new_measure(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture(local_origin=True)
        old_rows=h.rows(f.store.connection,'mission_document_numeric_local_reconciliations')
        from dalton_core.directed_numeric_local_reconciliation import reconcile as official
        done=official(ex,ad['id'],expected_admission_hash=ad['content_hash'],requested_by='automation:synthetic')
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(h.rows(f.store.connection,'mission_document_numeric_local_reconciliations'),old_rows)
        original_works=h.works(ex,prior)
        self.assertEqual(read_numeric_result(ex,ad,original_works,ex.scheduler.formal_result(original_works[3]['id']),include_supplement=False)['numeric_result'],prior)
        self.assertEqual(ex.run_once(ad['id'])['numeric_result'],done['numeric_result'])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_automatic_local_child_is_fixed_once_and_cli_uses_no_model_loop(self):
        from types import SimpleNamespace
        from contextlib import nullcontext
        from dalton_core.directed_numeric_measure_reconciliation import replay_candidate
        from dalton_core.mission_document_research_launcher import MissionDocumentResearchLauncher
        from dalton_core.mission_document_research_cli import run_admission
        from dalton_core.lane_child_launcher import LaneChildRejected
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        works=h.works(ex,prior)
        launcher=object.__new__(MissionDocumentResearchLauncher)
        directory=f.state/'local-replay';directory.mkdir()
        launcher.tickets_dir=directory
        launcher._ticket_path=lambda ref:directory/(ref.split(':')[1]+'.json')
        launcher.configuration=lambda:{'test':'current'}
        launcher.status=lambda ref:{'id':ref,'status':'failed'}
        def spawn(**kw):
            self.assertTrue(kw['completed_models_only'])
            ref=launcher.TICKET_PREFIX+':'+kw['digest'];launcher._ticket_path(ref).write_text('{}')
            return {'id':ref,'status':'running'}
        launcher.spawn=spawn
        coordinator=SimpleNamespace(store=f.store,launcher=launcher,_effective_work_hints=lambda a:[x['id'] for x in works])
        candidate=replay_candidate(coordinator,ad)
        self.assertIsNotNone(candidate)
        from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator
        from dalton_core.mission_document_research_promotion import ensure_promotion_authority
        ensure_promotion_authority(f.store.connection)
        actual=MissionDocumentResearchCoordinator(store=f.store,launcher=launcher,clock=f.harness.clock)
        self.assertIn(ad['id'],[x['id'] for x in actual._admissions()])
        unrelated=[{**ad,'id':'mission-document-research-admission:unrelated'+str(i),'numeric_authority':None} for i in range(26)]
        with patch.object(actual,'_admissions',return_value=[*unrelated,ad]):
            launched=actual.dispatch_once()
        self.assertEqual(launched['reason'],'source_measure_local_reconciliation')
        self.assertIsNone(replay_candidate(coordinator,ad))
        with self.assertRaisesRegex(LaneChildRejected,'already attempted'):
            launcher.replay_completed_models(admission_ref=ad['id'],admission_hash=ad['content_hash'],proof=candidate)
        runtime=SimpleNamespace(executor=ex,admission=ad,close=lambda:None)
        with patch('dalton_core.mission_document_research_cli.MissionDocumentResearchRuntime',return_value=nullcontext(runtime)),patch.object(ex,'run_once',side_effect=AssertionError('no generic executor/model loop')):
            summary=run_admission(state_dir=f.state,staging=f.state/'unused',planner_scheduler_db=f.state/'unused',planner_model_config=f.state/'unused',document_research_config=f.state/'unused',admission_ref=ad['id'],expected_admission_hash=ad['content_hash'],completed_models_only=True)
        self.assertEqual(summary['status'],'complete')
        self.assertEqual(summary['outcomes'][0]['research_status'],'numeric_targets_completed')
        self.assertNotIn(ad['id'],[x['id'] for x in actual._admissions()])
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_tampered_supplement_and_mapping_cannot_replace_authoritative_feedback(self):
        from dalton_core.directed_numeric_executor import numeric_feedback
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture()
        done=self.call(ex,ad);row=f.store.connection.execute('SELECT * FROM '+TABLE).fetchone()
        auth,works,_=_validate(ex,ad,row);result=done['numeric_result']
        formal=ex.scheduler.formal_result(works[3]['id'])
        h.corruptible_copy(f.store.connection,RESULTS)
        changed=copy.deepcopy(result)
        changed['source_measures'][0]['mapping']['conflict_proof']['metric_definition_hash']='f'*64
        changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
        f.store.connection.execute('UPDATE '+RESULTS+' SET record_json=?,content_hash=?',(canonical_json(changed),changed['content_hash']));f.store.connection.commit()
        offered=copy.deepcopy(formal);offered['result_envelope']['outputs']=changed;offered['result_envelope_hash']=content_hash(offered['result_envelope'])
        with patch.object(ex,'_stage_owned'),self.assertRaises(DirectedNumericError):read_numeric_result(ex,ad,works,offered)
        with self.assertRaises(DirectedNumericError):numeric_feedback(f.store.connection,ad['mission_version_ref'])
        f.store.connection.execute('UPDATE '+RESULTS+' SET record_json=?,content_hash=?',(canonical_json(result),result['content_hash']));f.store.connection.commit()
        self.assertEqual(numeric_feedback(f.store.connection,ad['mission_version_ref'])[0]['numeric_result_hash'],result['content_hash'])

    def test_same_amount_distinct_source_rows_remain_separate_not_reused_as_one_fact(self):
        h,w,f,a,ad,ex,p,v,old,prior=self.fixture(same_amount=True)
        done=self.call(ex,ad)
        mapping=done['numeric_result']['source_measures'][0]['mapping']
        self.assertEqual(mapping['conflict_proof']['held_fact']['value'],mapping['conflict_proof']['verified_answer']['value'])
        self.assertNotEqual(mapping['figure_ref'],old['figure_id'])
        self.assertNotEqual(mapping['held_source_row']['text_hash'],mapping['answer_source_row']['text_hash'])
        self.assertEqual(done['research_status'],'numeric_targets_completed')

    def test_real_same_measure_amount_collision_refuses_before_any_new_local_authority(self):
        h=support_fixture.NumericFigureSupportTests(methodName='runTest');self.addCleanup(h.doCleanups)
        w,f,a,ad,ex,p,v=h.fixture()
        original=a._missions.record_document_figures
        def same_label(**kwargs):
            kwargs['figures']=[{**x,'as_reported_label':'Remaining performance obligations'} for x in kwargs['figures']]
            return original(**kwargs)
        with patch.object(a._missions,'record_document_figures',side_effect=same_label):
            old=conflict_fixture.NumericMeasureConflictTests(methodName='runTest').seed_collision(h,f,a,ad,ex,p)
        prior=w.drive(ex,ad)['numeric_result']
        self.assertEqual(prior['pending_targets'][0]['measure_conflict_proof']['differing_fields'],['value'])
        with self.assertRaisesRegex(DirectedNumericError,'same-measure amount'):self.call(ex,ad)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM '+TABLE).fetchone()[0],0)
        self.assertEqual((p.calls,v.calls),(1,1))
