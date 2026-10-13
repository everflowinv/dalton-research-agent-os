"""Executable periods are a new-write gate, not a rewrite of old authority."""
import copy
import unittest
from unittest.mock import patch

from dalton_core.directed_numeric_research import (
    DirectedNumericError, assert_executable_target_periods, normalize_targets,
)
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.numeric_source_admission_feedback import PERIOD_OUTCOME, read_numeric_source_feedback
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for
from dalton_core.research_task_cli import run_admissions
from dalton_core.store import content_hash
from tests import test_research_task_numeric_refusal_isolation as isolation
from tests import test_directed_numeric_workflow as workflow
from tests import test_research_planner as planning


class NumericTargetPeriodAdmissionTests(unittest.TestCase):
    def fixture(self):
        harness = isolation.NumericAdmissionRefusalIsolationTests('runTest')
        harness.addCleanup = self.addCleanup
        f, authority, original, runtime, kwargs, producer, verifier = harness.fixture()
        from dalton_core.mission_document_research import _exact_plan
        original = _exact_plan(f.store.connection, original['plan_id'])[0]
        plan = {key: copy.deepcopy(original[key]) for key in (
            'schema_version', 'task_ref', 'created_at', 'state_hash',
            'mission_version_ref', 'assessment', 'directives', 'inquiries', 'sufficiency')}
        bad = copy.deepcopy(original['inquiries'][1]); bad['rank'] = 0
        bad['directed_document']['numeric_targets'][0]['period'] = '文件所载最近报告期末'
        good = copy.deepcopy(original['inquiries'][2]); good['rank'] = 1
        plan.update(state_hash='e' * 64, inquiries=[bad, good])
        plan['content_hash'] = content_hash(plan)
        plan = workflow.NumericWorkflowTests('runTest')._record_plan(f, plan)
        plan = _exact_plan(f.store.connection, plan['plan_id'])[0]
        return f, authority, plan, runtime, kwargs, producer, verifier

    def test_vague_first_target_refuses_before_work_and_next_exact_period_is_admitted(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        before = f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0]
        work_count = f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0]
        with patch('dalton_core.mission_document_research_admission.open_mission_document_admission_authority', runtime):
            result = run_admissions(**kwargs)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['admitted'], 1)
        self.assertEqual(result['refused'][0]['inquiry_ref'], inquiry_ref_for(inquiry_content_hash(plan['inquiries'][0])))
        self.assertIn('numeric_target_period_unspecified', result['refused'][0]['reason'])
        self.assertIn('do not guess a date', result['refused'][0]['reason'])
        admitted = authority.admission(result['tasks'][0]['admission_ref'])
        self.assertEqual(admitted['planner_inquiry'], plan['inquiries'][1])
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM mission_document_research_admissions').fetchone()[0], before + 1)
        from dalton_core.mission_document_research import _exact_plan
        self.assertEqual(_exact_plan(f.store.connection, plan['plan_id'])[0], plan)
        question = f.store.connection.execute('SELECT record_json FROM backlog_question_versions WHERE question=?',
            (plan['inquiries'][0]['question'],)).fetchone()
        self.assertIsNotNone(question)  # unresolved question is retained, not erased
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0], work_count)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_historical_vague_admission_still_reads_hash_replays_and_duplicates(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        inquiry = plan['inquiries'][0]
        registration = authority.registration_resolver.__self__
        args = dict(authority=authority, registrations=registration, backlog=ResearchQuestionBacklog(f.store),
            mission=f.mission, plan=plan, inquiry=inquiry)
        # Materialize a genuine old admission under the previous new-write rule.
        with patch('dalton_core.directed_numeric_research.assert_executable_target_periods'):
            old = admit_directed_inquiry(**args)
        before = f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',
            (old['admission_ref'],)).fetchone()
        stored = authority.admission(old['admission_ref'])
        self.assertEqual(stored, authority.resolve_for_execution(old['admission_ref']))
        self.assertEqual(normalize_targets(stored['numeric_authority']['targets']), stored['numeric_authority']['targets'])
        with patch('dalton_core.directed_numeric_research.assert_executable_target_periods', side_effect=AssertionError('must not reinterpret old admission')):
            self.assertEqual(admit_directed_inquiry(**args)['status'], 'duplicate')
        after = f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',
            (old['admission_ref'],)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_exact_period_preflight_reuses_candidate_semantics_without_guessing(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        target = plan['inquiries'][0]['directed_document']['numeric_targets'][0]
        for period in ('FY2025', '2026-06-30', 'Q2 2026', '1H25', 'Q3'):
            with self.subTest(period=period):
                assert_executable_target_periods([{**target, 'period': period}])
        for period in ('current', 'latest reporting period', '文件所载最近报告期末'):
            targets = [{**target, 'period': period}]
            self.assertEqual(normalize_targets(targets)[0]['period'], period)
            with self.subTest(period=period), self.assertRaisesRegex(DirectedNumericError, 'numeric_target_period_unspecified'):
                assert_executable_target_periods(targets)

    def test_strong_feedback_retains_period_and_exact_original_under_compression(self):
        f, authority, plan, runtime, kwargs, producer, verifier = self.fixture()
        def inventory(**kwargs):
            return {'registration_by_hash': {r['content_hash']: r for r in authority.registration_resolver.__self__.values()}}
        feedback = read_numeric_source_feedback(f.store, f.mission, inventory_loader=inventory)
        self.assertEqual(feedback['status'], 'verified')
        self.assertEqual(len(feedback['records']), 1)
        row = feedback['records'][0]
        self.assertEqual(row['outcome'], PERIOD_OUTCOME)
        self.assertEqual(row['inquiry_ref'], inquiry_ref_for(inquiry_content_hash(plan['inquiries'][0])))
        self.assertEqual(row['document_version_hash'], plan['inquiries'][0]['directed_document']['document_version_hash'])
        self.assertEqual(row['unexecutable_period_targets'], [{'target_ref': plan['inquiries'][0]['directed_document']['numeric_targets'][0]['target_ref'],
            'period': '文件所载最近报告期末'}])
        company = row['company_ref']
        with patch('dalton_core.document_research_inventory.load_document_inventory_authority', side_effect=inventory):
            from dalton_core.document_research_feedback import read_document_research_feedback
            actual = read_document_research_feedback(f.store, f.mission)
        state = planning.state(mission=f.mission, checklist=[planning.entry(company, 'SYN')], document_research_feedback=actual)
        self.assertIn(row['id'], state['companies'][0]['numeric_source_refusal_ids'])
        state['companies'][0]['old_company_detail'] = 'x' * 100000
        state['companies'][0]['financial_model'] = None
        projection = planning.project_state_for_prompt(state, max_input_bytes=64000)
        projected = projection['companies'][0]['numeric_source_refusals'][0]
        self.assertEqual(projected['unexecutable_period_targets'], row['unexecutable_period_targets'])
        self.assertEqual(projected['document_version_hash'], row['document_version_hash'])
        self.assertIn('never guess a date', planning.build_prompt(projection))
        self.assertEqual((producer.calls, verifier.calls), (0, 0))
