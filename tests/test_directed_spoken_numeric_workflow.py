"""A dated management answer is not a filed quantitative financial fact."""
import copy
import unittest
from tests import test_directed_numeric_workflow as numeric_fixture
from dalton_core.directed_numeric_source_capabilities import SPOKEN_CONTRACT_REF, planning_capability, spoken_answer
from dalton_core.directed_numeric_executor import source_binding, numeric_feedback
from dalton_core.document_figure_grade import SPOKEN
from dalton_core.store import canonical_json, content_hash

class SpokenNumericWorkflowTests(unittest.TestCase):
    _record_plan=numeric_fixture.NumericWorkflowTests._record_plan
    _executor=numeric_fixture.NumericWorkflowTests._executor
    def fixture(self, **kwargs):
        kwargs.setdefault("source_spec", "earnings-call-transcripts")
        kwargs.setdefault("strategy_version", SPOKEN_CONTRACT_REF)
        kwargs.setdefault("basis", "management-reported")
        return numeric_fixture.NumericWorkflowTests.fixture(self, **kwargs)
    drive=numeric_fixture.NumericWorkflowTests.drive

    def spoken(self, **kwargs):
        return self.fixture(source_spec='earnings-call-transcripts',
            strategy_version=SPOKEN_CONTRACT_REF,basis='management-reported',**kwargs)

    def test_three_management_targets_independently_verified_no_filed_claim_and_replay(self):
        f,authority,admission,executor,producer,verifier=self.spoken(old_rejection=True)
        before=f.store.connection.execute('SELECT COUNT(*) FROM claim_versions').fetchone()[0]
        result=self.drive(executor,admission)
        receipt=result['numeric_result']
        self.assertEqual(result['research_status'],'numeric_targets_completed')
        self.assertEqual(len(receipt['completed_targets']),3)
        self.assertEqual(receipt['canonical_claims'],[])
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM claim_versions').fetchone()[0],before)
        answer=receipt['management_answer']
        self.assertEqual(answer['attribution'],'管理层表述')
        self.assertEqual(answer['source_binding']['document_date'],admission['request']['registration']['doc_date'])
        self.assertEqual(answer['source_binding']['source_grade'],SPOKEN)
        self.assertEqual(len(answer['statements']),3)
        self.assertIn('Remaining performance obligations were $12.3 billion.',answer['statements'][0]['original_quote'])
        self.assertEqual(result,executor.run_once(admission['id']))
        self.assertEqual((producer.calls,verifier.calls),(1,1))
        feedback=numeric_feedback(f.store.connection,admission['mission_version_ref'])[-1]
        self.assertEqual(feedback['management_answer'],answer)
        self.assertEqual(feedback['answer_contract_ref'],SPOKEN_CONTRACT_REF)
        self.assertEqual(feedback['source_grade'],SPOKEN)
        from dalton_core.claim_index_figures import MissionFigureAuthorityResolver,build_figure_candidate,FigureAdmissionError
        resolver=MissionFigureAuthorityResolver(f.store.connection)
        for item in receipt['completed_targets']:
            figure=resolver.figure(item['figure_ref'])
            self.assertEqual(figure['source_grade'],SPOKEN)
            with self.assertRaises(FigureAdmissionError):
                build_figure_candidate(f.store.connection,figure=figure,actor_ref=admission['actor_ref'],resolver=resolver)

    def test_partial_preserves_missing_and_material_broker_cannot_passage_close(self):
        f,authority,admission,executor,producer,verifier=self.spoken(missing=True)
        result=self.drive(executor,admission);receipt=result['numeric_result']
        self.assertEqual(result['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(receipt['completed_targets']),2)
        self.assertEqual(receipt['pending_targets'][0]['target_ref'],'target:leases')
        self.assertEqual(receipt['canonical_claims'],[])
        self.assertEqual(len(receipt['management_answer']['statements']),2)
        from dalton_core.material_requests import request
        from dalton_core.material_acquisition import evaluate_acceptance
        held=request(f.state,receipt['material_requests'][0]['request_ref'])
        self.assertEqual(held['acceptance']['answer_contract_ref'],SPOKEN_CONTRACT_REF)
        self.assertEqual(held['need']['doc_kinds'],['earnings_call'])
        self.assertFalse(evaluate_acceptance(f.state,held)['met'])

    def test_source_grade_strategy_crossovers_reject_before_provider(self):
        with self.assertRaisesRegex(Exception,'company-filed'):
            self.fixture(source_spec='earnings-call-transcripts',strategy_version='directed-document-numeric:0.1')
        with self.assertRaisesRegex(Exception,'earnings-call'):
            self.fixture(source_spec='annual-report-10k',strategy_version=SPOKEN_CONTRACT_REF)

    def test_missing_or_forged_answer_marker_cannot_be_accepted(self):
        f,authority,admission,executor,producer,verifier=self.spoken()
        missing=copy.deepcopy(admission);missing['numeric_authority'].pop('answer_contract_ref')
        with self.assertRaisesRegex(ValueError,'versioned authority'):spoken_answer(missing)
        wrong=copy.deepcopy(admission);wrong['numeric_authority']['answer_contract_ref']='directed-document-numeric:0.1'
        with self.assertRaises(ValueError):spoken_answer(wrong)
        wrong['planner_inquiry']['directed_document']['strategy_version']='directed-document-numeric:0.1'
        with self.assertRaises(ValueError):spoken_answer(wrong)
        self.assertEqual((producer.calls,verifier.calls),(0,0))

    def test_advertised_capability_comes_from_exact_discovery_not_doc_kind(self):
        f,authority,admission,executor,producer,verifier=self.spoken()
        registration=admission['request']['registration']
        cap=planning_capability(f.store.connection,registration,admission['company_ref'])
        self.assertEqual(cap['source_grade'],SPOKEN)
        # The test acquired fixture is deliberately labelled as an annual report;
        # the actual source discovery, rather than that label, controls grade.
        altered={**registration,'doc_kind':'annual_report'}
        self.assertEqual(cap,planning_capability(f.store.connection,altered,admission['company_ref']))

    def test_management_display_tamper_not_ignored_by_strong_replay(self):
        f,authority,admission,executor,producer,verifier=self.spoken()
        result=self.drive(executor,admission)
        row=f.store.connection.execute('SELECT record_json FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
        # Immutable ledger denies changing even a date/attribution-only field.
        with self.assertRaises(Exception):
            f.store.connection.execute('UPDATE mission_document_numeric_results SET record_json=? WHERE admission_ref=?',
                (row['record_json'].replace('管理层表述','已申报事实'),admission['id']))
        self.assertEqual(result,executor.run_once(admission['id']))

    def test_unproved_grade_preserves_readable_inventory_without_numeric_authority(self):
        f,authority,admission,executor,producer,verifier=self.spoken()
        registration=admission['request']['registration']
        # A legitimate readable source lacking review/discovery ownership is not
        # permission to infer a numeric grade, and does not erase readability.
        unknown=copy.deepcopy(registration)
        unknown['source_authority']['ref']='mission-discovered-document:unknown'
        capability=planning_capability(f.store.connection,unknown,admission['company_ref'])
        self.assertIsNone(capability['source_grade'])


    def test_unknown_source_date_does_not_invent_date_or_block_verified_management_answer(self):
        f,authority,admission,executor,producer,verifier=self.spoken()
        result=self.drive(executor,admission)
        answer=result['numeric_result']['management_answer']
        self.assertIsNone(answer['source_binding']['document_date'])
        self.assertEqual(answer['document_date_status'],'not_provided')
        self.assertEqual(answer['document_date_label'],'原文日期未提供')
        self.assertTrue(answer['statements'])
        from dalton_core.directed_numeric_source_capabilities import document_date
        for value in (None,'not-a-date','2025-02-30','20250101'):
            self.assertIsNone(document_date({'doc_date':value}))
        self.assertEqual(document_date({'doc_date':'2025-01-01'}),'2025-01-01')
        self.assertEqual(document_date({'doc_date':'2025-01-01T00:00:00Z'}),'2025-01-01T00:00:00Z')

    def test_independent_semantic_rejection_keeps_all_targets_pending(self):
        numeric_fixture.NumericWorkflowTests.test_independent_reject_is_missing_evidence_not_a_qualitative_escalation(self)

    def test_same_original_resume_requires_actual_new_registered_passages(self):
        numeric_fixture.NumericWorkflowTests.test_same_original_new_verified_passages_can_resume_without_new_file(self)

    def test_numeric_target_period_unit_currency_and_metric_must_not_drift(self):
        numeric_fixture.NumericWorkflowTests.test_actual_window_and_typed_definition_drift_refuse_before_model_call(self)

    def test_management_strategy_rejects_a_filed_basis(self):
        with self.assertRaisesRegex(Exception,'management-reported basis'):
            self.fixture(basis='gaap-reported')

    def test_new_capability_material_hash_wakes_once_then_existing_pacing_is_preserved(self):
        from dalton_core.research_state import planning_core_hash
        from dalton_core.research_planner_cli import replan_pacing_hold
        from datetime import datetime,timezone
        old={'companies':[],'document_research_contract_ref':'directed-document:0.1'}
        from dalton_core.research_planner import TASK_HASH
        current={**old,'planner_task_hash':TASK_HASH}
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        last={'at':now.isoformat(),'planning_hash':'old','planning_core_hash':planning_core_hash(old)}
        self.assertIsNone(replan_pacing_hold(last,'new',now=now,planning_core_hash=planning_core_hash(current)))
        last.update(planning_hash='new',planning_core_hash=planning_core_hash(current))
        self.assertIsNotNone(replan_pacing_hold(last,'new',now=now,planning_core_hash=planning_core_hash(current)))

    def test_new_planner_strategy_uses_authority_grade_and_rejects_missing_or_wrong_grade(self):
        from dalton_core.document_research_strategy import inventory_document,resolve_strategy
        f,authority,admission,executor,producer,verifier=self.spoken()
        registration=admission['request']['registration']
        document=inventory_document(registration=registration,company_ref=admission['company_ref'])
        document.update(planning_capability(f.store.connection,registration,admission['company_ref']))
        state={'document_research_policy':executor.registry.policy,
            'companies':[{'company_ref':admission['company_ref'],'readable_documents':[document]}]}
        strategy=admission['planner_inquiry']['directed_document']
        self.assertEqual(resolve_strategy(strategy,company_ref=admission['company_ref'],state=state)[0],strategy)
        for grade in (None,'company-filed-document'):
            document['source_grade']=grade
            with self.assertRaisesRegex(ValueError,'source grade'):
                resolve_strategy(strategy,company_ref=admission['company_ref'],state=state)
        document.pop('source_grade')
        with self.assertRaisesRegex(ValueError,'source grade'):
            resolve_strategy(strategy,company_ref=admission['company_ref'],state=state)

    def test_completed_management_hint_does_not_clear_a_filed_target_requirement(self):
        from dalton_core.planner_numeric_requirements import numeric_waiting_targets
        f,authority,admission,executor,producer,verifier=self.spoken()
        self.drive(executor,admission)
        management=numeric_feedback(f.store.connection,admission['mission_version_ref'])[-1]
        pending=copy.deepcopy(management)
        for key in ('answer_contract_ref','source_grade','document_date','attribution','management_answer','resolved_numeric_rejections'):
            pending.pop(key,None)
        pending['outcome']='numeric_waiting_evidence';pending['completed_targets']=[]
        pending['missing']=[{'target_ref':t['target_ref'],'reason':'Still needs a company-filed fact.'}for t in pending['numeric_targets']]
        pending['created_at']='2020-01-01T00:00:00+00:00'
        pending.pop('content_hash');pending['content_hash']=content_hash(pending)
        waiting=numeric_waiting_targets([pending,management])
        self.assertEqual({r['target_ref']for r in waiting['targets']},{t['target_ref']for t in pending['numeric_targets']})
