import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.document_research_inventory import inventory_with_registry
from dalton_core.mission_document_research import PURPOSE
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as workflow
from tests.test_mission_annual_research import COMPANY


class NumericOriginalInventoryTests(unittest.TestCase):
    fixture = workflow.NumericWorkflowTests.fixture
    drive = workflow.NumericWorkflowTests.drive
    _record_plan = workflow.NumericWorkflowTests._record_plan
    _executor = workflow.NumericWorkflowTests._executor

    def inventory(self, f, authority):
        return inventory_with_registry(core=f.store, mission=f.mission,
                                       registry=authority.registry, purpose=PURPOSE)

    def current_rendering(self, admission):
        current = copy.deepcopy(admission['request']['registration'])
        current['doc_date'] = '2026-09-30'
        current.pop('content_hash')
        current['content_hash'] = content_hash(current)
        return current

    def test_open_exact_original_and_trusted_current_rendering_coexist_without_writes(self):
        f, authority, admission, _, producer, verifier = self.fixture(old_rejection=True)
        old = admission['request']['registration']
        current = self.current_rendering(admission)
        before = f.store.connection.total_changes
        rows = list(f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_admissions'))
        with patch.object(authority.registry, 'inspect_acquired_document',
                          side_effect=lambda **kw: ({'available': True, 'registration': current} if kw['record_id'] == current['source_authority']['ref'] else {'available': False, 'reason': 'fixture source adapter absent'})):
            result = self.inventory(f, authority)
            replay = self.inventory(f, authority)
        self.assertEqual(result, replay)
        docs = result['readable_documents_by_company'][COMPANY]
        self.assertEqual({d['document_version_hash'] for d in docs},
                         {old['content_hash'], current['content_hash']})
        self.assertEqual(result['registration_by_hash'][old['content_hash']], old)
        self.assertEqual(len(docs), 2)
        self.assertEqual(f.store.connection.total_changes, before)
        self.assertEqual(list(f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_admissions')), rows)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_pending_supersession_keeps_source_without_reopening_parent_operation(self):
        from dalton_core.document_research_feedback import read_document_research_feedback
        from dalton_core.document_research_inventory import _required_numeric_originals
        from dalton_core.planner_numeric_requirements import numeric_requirements
        f, authority, admission, _, producer, verifier = self.fixture(old_rejection=True)
        feedback = read_document_research_feedback(f.store, f.mission)
        observations = feedback['by_company'][COMPANY]
        upgraded = [r for r in observations if r.get('numeric_upgrade_supersession')]
        self.assertEqual(len(upgraded), 1)
        self.assertEqual(upgraded[0]['numeric_upgrade_supersession']['successor_admission_ref'], admission['id'])
        self.assertEqual(numeric_requirements(observations)['requirements'], [])
        before = f.store.connection.total_changes
        with patch.object(authority.registry, '_reresolve', wraps=authority.registry._reresolve) as replay:
            originals, failures = _required_numeric_originals(core=f.store, mission=f.mission,
                registry=authority.registry, purpose=PURPOSE)
        self.assertEqual(originals, [(COMPANY, admission['request']['registration'])])
        self.assertEqual(failures, [])
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(f.store.connection.total_changes, before)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_rehashed_supersession_identity_or_target_drift_cannot_pin_source(self):
        from dalton_core.document_research_feedback import read_document_research_feedback
        from dalton_core.document_research_inventory import _required_numeric_originals
        f, authority, _, _, producer, verifier = self.fixture(old_rejection=True)
        feedback = read_document_research_feedback(f.store, f.mission)
        before = f.store.connection.total_changes
        for field, value in [('successor_admission_ref', 'mission-document-admission:foreign'),
                             ('numeric_targets_hash', '0'*64),
                             ('document_version_hash', '0'*64),
                             ('rejection_binding', {'admission_ref': {'foreign': 'record'}}),
                             ('company_ref', 'company:foreign')]:
            with self.subTest(field=field):
                altered = copy.deepcopy(feedback)
                row = next(r for r in altered['by_company'][COMPANY] if r.get('numeric_upgrade_supersession'))
                proof = row['numeric_upgrade_supersession']
                proof[field] = value
                proof['content_hash'] = content_hash({k:v for k,v in proof.items() if k != 'content_hash'})
                with patch('dalton_core.document_research_feedback.read_document_research_feedback', return_value=altered), \
                     patch.object(authority.registry, '_reresolve', side_effect=AssertionError('unproved source replay')):
                    originals, failures = _required_numeric_originals(core=f.store, mission=f.mission,
                        registry=authority.registry, purpose=PURPOSE)
                self.assertEqual(originals, [])
                self.assertEqual([r['reason'] for r in failures], ['numeric_required_original_unavailable'])
        self.assertEqual(f.store.connection.total_changes, before)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_full_formal_numeric_completion_stops_pinning_old_original(self):
        f, authority, admission, executor, _, _ = self.fixture(old_rejection=True)
        current = self.current_rendering(admission)
        self.assertEqual(self.drive(executor, admission)['research_status'], 'numeric_targets_completed')
        with patch.object(authority.registry, 'inspect_acquired_document',
                          side_effect=lambda **kw: ({'available': True, 'registration': current} if kw['record_id'] == current['source_authority']['ref'] else {'available': False, 'reason': 'fixture source adapter absent'})):
            result = self.inventory(f, authority)
        self.assertEqual(set(result['registration_by_hash']), {current['content_hash']})
        from dalton_core.document_research_inventory import _required_numeric_originals
        with patch.object(authority.registry, '_reresolve', side_effect=AssertionError('completed old source pinned')):
            self.assertEqual(_required_numeric_originals(core=f.store, mission=f.mission,
                registry=authority.registry, purpose=PURPOSE), ([], []))

    def test_partial_formal_result_keeps_required_original(self):
        f, authority, admission, executor, _, _ = self.fixture(old_rejection=True, missing=True)
        current = self.current_rendering(admission)
        self.assertEqual(self.drive(executor, admission)['research_status'], 'numeric_waiting_evidence')
        with patch.object(authority.registry, 'inspect_acquired_document',
                          side_effect=lambda **kw: ({'available': True, 'registration': current} if kw['record_id'] == current['source_authority']['ref'] else {'available': False, 'reason': 'fixture source adapter absent'})), \
             patch.object(authority.registry, '_reresolve', wraps=authority.registry._reresolve) as replay:
            result = self.inventory(f, authority)
        self.assertIn(admission['request']['registration']['content_hash'], result['registration_by_hash'])
        self.assertEqual(replay.call_count, 1)

    def test_unreplayable_old_original_is_diagnostic_not_current_version_alias(self):
        f, authority, admission, _, _, _ = self.fixture(old_rejection=True)
        current = self.current_rendering(admission)
        with patch.object(authority.registry, 'inspect_acquired_document',
                          side_effect=lambda **kw: ({'available': True, 'registration': current} if kw['record_id'] == current['source_authority']['ref'] else {'available': False, 'reason': 'fixture source adapter absent'})), \
             patch.object(authority.registry, '_reresolve', return_value=(current, 'irrelevant')):
            result = self.inventory(f, authority)
        self.assertEqual(set(result['registration_by_hash']), {current['content_hash']})
        self.assertIn('numeric_required_original_unavailable',
                      [d['reason'] for d in result['unavailable_documents_by_company'][COMPANY]])

    def test_multiple_rejections_share_one_strong_original_replay(self):
        f, authority, admission, _, _, _ = self.fixture(old_rejection=2)
        with patch.object(authority.registry, '_reresolve', wraps=authority.registry._reresolve) as replay:
            result = self.inventory(f, authority)
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(len(result['readable_documents_by_company'][COMPANY]), 1)

    def test_changed_parent_bytes_do_not_pin_old_original(self):
        f, authority, admission, _, _, _ = self.fixture(old_rejection=True)
        old_ref = admission['numeric_authority']['capability_upgrade_proofs'][0]['admission_ref']
        row = f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_admissions WHERE admission_id=?', (old_ref,)).fetchone()
        damaged = json.loads(row[0]); damaged['company_ref'] = 'company:foreign'
        # Simulate external corruption only in this disposable synthetic database.
        f.store.connection.execute('DROP TRIGGER mission_document_research_admissions_no_update')
        f.store.connection.execute(
            'UPDATE mission_document_research_admissions SET record_json=? WHERE admission_id=?',
            (json.dumps(damaged), old_ref))
        current = self.current_rendering(admission)
        with patch.object(authority.registry, 'inspect_acquired_document',
                          side_effect=lambda **kw: ({'available': True, 'registration': current} if kw['record_id'] == current['source_authority']['ref'] else {'available': False, 'reason': 'fixture source adapter absent'})):
            result = self.inventory(f, authority)
        self.assertNotIn(admission['request']['registration']['content_hash'], result['registration_by_hash'])
