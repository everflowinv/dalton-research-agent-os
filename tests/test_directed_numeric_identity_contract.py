import copy
import json
import unittest
from unittest.mock import patch

from dalton_core.directed_numeric_output import draft_prompt, parse_draft, TARGET_IDENTITY_FIELDS
from dalton_core.directed_numeric_recovery import (
    build_numeric_output_repair, replay_numeric_output_repair, contract_retry_disposition,
)
from dalton_core.store import canonical_json, content_hash
from tests import test_directed_numeric_workflow as workflow_fixtures


class NumericIdentityContractTests(unittest.TestCase):
    def fixture(self):
        harness = workflow_fixtures.NumericWorkflowTests('test_three_required_figures_formal_claims_and_zero_call_replay')
        self.addCleanup(harness.doCleanups)
        return harness.fixture()

    def test_prompt_copies_exact_identity_but_preserves_original_quote_and_metric_label(self):
        f, authority, admission, executor, producer, verifier = self.fixture()
        proof = executor.registry.search(admission['request'])
        targets = admission['numeric_authority']['targets']
        original = copy.deepcopy(targets)
        prompt = json.loads(draft_prompt(question='Read the requested disclosures',
            search_proof=proof, targets=targets,
            catalog=admission['numeric_authority']['metric_definitions']))
        self.assertEqual(targets, original)
        self.assertEqual(len(prompt['identity_templates']), len(targets))
        for row, target in zip(prompt['identity_templates'], targets):
            self.assertEqual(row, {'target_ref': target['target_ref'],
                'figure_identity': {key: target[key] for key in TARGET_IDENTITY_FIELDS}})
        self.assertIn('EXACTLY', prompt['task'])
        self.assertIn('as_reported_label must remain the metric label', prompt['task'])
        self.assertIn('exactly target_ref, status, figure and reason', prompt['task'])
        self.assertEqual(prompt['quotes'], json.loads(draft_prompt(question='Other question',
            search_proof=proof, targets=targets,
            catalog=admission['numeric_authority']['metric_definitions']))['quotes'])

    def test_date_rephrasing_wrong_year_unit_and_basis_still_refuse_with_short_field_diagnostic(self):
        f, authority, admission, executor, producer, verifier = self.fixture()
        metadata = {'numeric_targets': admission['numeric_authority']['targets'],
                    'retrieval_proof': executor.registry.search(admission['request'])}
        self.assertEqual(parse_draft(canonical_json(producer.candidate_wire), metadata=metadata),
                         producer.candidate_wire)
        for field, value in [('period', 'year ended December 31, 2025'),
                             ('period', 'FY2024'), ('unit', 'count'),
                             ('basis', 'issuer GAAP presentation')]:
            changed = copy.deepcopy(producer.candidate_wire)
            changed['targets'][0]['figure'][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, field + ' expected .*; received '):
                parse_draft(canonical_json(changed), metadata=metadata)
        self.assertEqual((producer.calls, verifier.calls), (0, 0))

    def test_new_recovery_freezes_prompt_and_diagnostic_and_replays_without_new_call(self):
        f, authority, admission, executor, producer, verifier = self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period'] = 'year ended December 31, 2025'
        for _ in range(8):
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry':
                break
        else:
            self.fail(str(result))
        rows = f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchall()
        self.assertEqual(len(rows), 1)
        link = json.loads(rows[0][0])
        failed = executor.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        recovered = executor.scheduler.work_order_authority(link['recovery_work_order_ref'])['work_order']
        repair = link['failure_proof']['numeric_output_repair']
        self.assertEqual(recovered['budget'], failed['budget'])
        self.assertEqual(repair['source_work_hash'], content_hash(failed))
        self.assertNotEqual(recovered['question'], failed['question'])
        message = json.loads(recovered['question'])['previous_output_error']
        self.assertIn("period expected 'FY2025'", message)
        self.assertIn('year ended December 31, 2025', message)
        from dalton_core.mission_document_research_executor import _recovery_work
        self.assertEqual(_recovery_work(failed, link), recovered)
        before = producer.calls
        executor.run_once(admission['id'])
        self.assertLessEqual(producer.calls, before + 1)

    def test_repair_cannot_change_source_target_or_diagnostics_from_the_formal_failure(self):
        f, authority, admission, executor, producer, verifier = self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        for _ in range(8):
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry': break
        link = json.loads(f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0])
        failed = executor.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        repair = link['failure_proof']['numeric_output_repair']
        for key in ('targets', 'quotes'):
            changed = copy.deepcopy(repair)
            value = json.loads(changed['replacement_prompt'])
            value[key] = [] if key == 'targets' else {}
            changed['replacement_prompt'] = canonical_json(value)
            changed['replacement_prompt_hash'] = content_hash(changed['replacement_prompt'])
            changed.pop('content_hash')
            changed['content_hash'] = content_hash(changed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                replay_numeric_output_repair(failed, changed)

    def test_remaining_stage_bound_is_not_expanded_by_identity_contract(self):
        from dalton_core.mission_document_research_executor import (
            AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION, CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY,
        )
        link = {'failure_proof': {'classification': AUTOMATIC_CONTRACT_RETRY_CLASSIFICATION}}
        self.assertIsNone(contract_retry_disposition([link]))
        self.assertEqual(contract_retry_disposition([link, link]),
                         CONTRACT_FAILED_AFTER_AUTOMATIC_RETRY)

    def test_old_initial_ready_work_replays_exact_published_template_after_prompt_update(self):
        from dalton_core.directed_numeric_output import legacy_draft_prompt_v0
        from dalton_core.mission_document_research_executor import _effective_stage
        f, authority, admission, executor, producer, verifier = self.fixture()
        with patch('dalton_core.directed_numeric_executor.draft_prompt', legacy_draft_prompt_v0):
            for _ in range(6):
                executor.run_once(admission['id'])
                rows = executor.scheduler.connection.execute(
                    "SELECT work_order_json,work_order_hash FROM scheduler_work_orders "
                    "WHERE json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref')=? "
                    "AND json_extract(work_order_json,'$.metadata.stage')='qualitative_model_draft'",
                    (admission['id'],)).fetchall()
                if rows: break
        self.assertEqual(len(rows), 1)
        original = json.loads(rows[0]['work_order_json'])
        before = rows[0]['work_order_hash']
        self.assertNotIn('identity_templates', json.loads(original['question']))
        work, links = _effective_stage(authority, executor.scheduler, admission, 1,
                                      worker=executor.draft_worker)
        self.assertEqual(work, original)
        self.assertEqual(links, [])
        executor.run_once(admission['id'])
        saved = executor.scheduler.work_order_authority(original['id'])
        self.assertEqual(saved['work_order_hash'], before)
        self.assertEqual(saved['work_order'], original)
        self.assertEqual(producer.calls, 1)

    def test_old_link_and_work_bytes_remain_exact_then_only_new_link_freezes_repair(self):
        from dalton_core.directed_numeric_output import legacy_draft_prompt_v0
        from dalton_core.mission_document_research_executor import _effective_stage, _recovery_work
        f, authority, admission, executor, producer, verifier = self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        with patch('dalton_core.directed_numeric_executor.draft_prompt', legacy_draft_prompt_v0), \
             patch('dalton_core.directed_numeric_recovery.build_numeric_output_repair', return_value=None):
            for _ in range(8):
                result = executor.run_once(admission['id'])
                if result.get('reason') == 'automatic_bounded_contract_retry': break
        row = f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()
        original_json, original_hash = row['record_json'], row['content_hash']
        link = json.loads(original_json)
        self.assertNotIn('numeric_output_repair', link['failure_proof'])
        failed = executor.scheduler.work_order_authority(link['failed_work_order_ref'])['work_order']
        original_work = executor.scheduler.work_order_authority(link['recovery_work_order_ref'])
        self.assertEqual(_recovery_work(failed, link), original_work['work_order'])
        recovered, _ = _effective_stage(authority, executor.scheduler, admission, 1,
                                       worker=executor.draft_worker)
        self.assertEqual(recovered, original_work['work_order'])
        for _ in range(5):
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry': break
        after = f.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_recovery_links WHERE recovery_link_id=?',
            (link['id'],)).fetchone()
        self.assertEqual((after['record_json'], after['content_hash']), (original_json, original_hash))
        self.assertEqual(executor.scheduler.work_order_authority(original_work['work_order']['id']), original_work)
        latest = f.store.connection.execute(
            'SELECT record_json FROM mission_document_research_recovery_links WHERE admission_ref=? ORDER BY recovery_number DESC',
            (admission['id'],)).fetchone()
        self.assertIn('numeric_output_repair', json.loads(latest[0])['failure_proof'])

    def test_actual_exhaustion_stops_at_existing_work_budgets_and_projects_one_non_owner_card(self):
        from dalton_core.directed_numeric_recovery import read_numeric_contract_hold
        from dalton_core.needs_human import _numeric_contract_system_item
        f, authority, admission, executor, producer, verifier = self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        # Distinct diagnostics exhaust the bounded windows; identical failures
        # now stop earlier under the separately tested repeat guard.
        for _ in range(24):
            producer.candidate_wire['targets'][0]['figure']['period'] = f'FY{1900 + producer.calls}'
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'contract_failed_after_automatic_retry': break
        else: self.fail(str(result))
        draft_rows = executor.scheduler.connection.execute(
            "SELECT work_order_json,max_attempts FROM scheduler_work_orders "
            "WHERE json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref')=? "
            "AND json_extract(work_order_json,'$.metadata.stage')='qualitative_model_draft'",
            (admission['id'],)).fetchall()
        self.assertEqual(len(draft_rows), 3)  # initial + two existing bounded replacements
        bought_calls = sum(row['max_attempts'] for row in draft_rows)
        self.assertEqual(producer.calls, bought_calls)
        state = read_numeric_contract_hold(f.store.connection, admission['id'])
        self.assertEqual(state['stage_ordinal'], 2)
        self.assertEqual(state['used_recovery_links'], 2)
        self.assertFalse(state['automatic_window_available'])
        before = f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0]
        for _ in range(3):
            self.assertEqual(executor.run_once(admission['id'])['reason'], 'contract_failed_after_automatic_retry')
        self.assertEqual(producer.calls, bought_calls)
        self.assertEqual(f.store.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0], before)
        hold = {'admission_ref': admission['id'], 'reason': 'contract_failed_after_automatic_retry'}
        card = _numeric_contract_system_item(f.store.connection, 'mission_document_research', hold,
                    at='2026-10-10T00:00:00Z', path='/private/holds.json', subject={'company': 'SYN'})
        self.assertFalse(card['actionable'])
        self.assertEqual(card['responsibility'], 'administrator')
        self.assertEqual(card['status'], 'system_repair_pending')
        self.assertEqual(card['detail']['owner_doors'], [])
        from dalton_core.needs_human import held_lanes
        ledger = f.state / 'mission-document-research-runs' / 'holds.json'
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(canonical_json({'holds': {admission['id']: {
            'disposition': 'recovery_required', 'reason': hold['reason']}}}))
        items = held_lanes(None, state_dir=f.state, core=f.store.connection)
        self.assertEqual(len(items), 1)  # no duplicate lane-level owner escalation
        self.assertFalse(items[0]['actionable'])
        self.assertEqual(items[0]['responsibility'], 'administrator')

    def test_legacy_first_contract_hold_has_only_one_remaining_existing_window(self):
        from dalton_core.directed_numeric_recovery import read_numeric_contract_hold
        from dalton_core.mission_document_research_executor import _effective_stage
        from dalton_core.needs_human import _numeric_contract_system_item
        f, authority, admission, executor, producer, verifier = self.fixture()
        producer.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        for _ in range(8):
            result = executor.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry': break
        for _ in range(5):
            producer.candidate_wire['targets'][0]['figure']['period'] = f'FY{1900 + producer.calls}'
            result = executor.run_once(admission['id'])
            if result.get('status') == 'failed': break
        work, links = _effective_stage(authority, executor.scheduler, admission, 1,
                                      worker=executor.draft_worker)
        self.assertEqual(len(links), 1)
        formal = executor.scheduler.formal_result(work['id'])
        paid = executor._paid_contract_failure_proof(work, formal, 1)
        self.assertIsNotNone(paid)
        # A historical release stopped after one replacement. Preserve that
        # immutable verdict, but assess capacity against the exact current chain.
        executor._recovery_observation(admission, work, formal, 1, {
            'status': 'stopped', 'reason': 'contract_failed_after_automatic_retry',
            'eligible': False, 'used_fresh_work_orders': 1,
            'max_fresh_work_orders': 2, 'retry_at': None,
            'deadline': formal['created_at'], 'proof': paid})
        state = read_numeric_contract_hold(f.store.connection, admission['id'])
        self.assertTrue(state['automatic_window_available'])
        self.assertEqual(state['used_recovery_links'], 1)
        card = _numeric_contract_system_item(f.store.connection, 'mission_document_research',
            {'admission_ref': admission['id'], 'reason': 'contract_failed_after_automatic_retry'},
            at='', path='/private/holds.json', subject={'company': 'SYN'})
        self.assertFalse(card['actionable'])
        self.assertEqual(card['responsibility'], 'background')
        self.assertEqual(card['status'], 'automatic_recovery_pending')
        result = executor.run_once(admission['id'])
        self.assertEqual(result['reason'], 'automatic_bounded_contract_retry')
        self.assertEqual(f.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_recovery_links WHERE admission_ref=?',
            (admission['id'],)).fetchone()[0], 2)

    def test_read_unavailable_does_not_assert_numeric_identity_or_remaining_spending(self):
        from dalton_core.needs_human import _numeric_contract_system_item
        class Unavailable:
            def execute(self, *args, **kwargs): raise TimeoutError('writer store unavailable')
        card = _numeric_contract_system_item(Unavailable(), 'mission_document_research',
            {'admission_ref': 'admission:unreadable', 'reason': 'contract_failed_after_automatic_retry'},
            at='', path='/private/holds.json', subject={'company': 'SYN'})
        self.assertFalse(card['actionable'])
        self.assertEqual(card['status'], 'recovery_state_unverified')
        self.assertNotIn('数字', card['title'])
        self.assertNotIn('used_recovery_links', card['detail']['contract_recovery'])
