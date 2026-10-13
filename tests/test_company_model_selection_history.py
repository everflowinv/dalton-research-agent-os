"""Historical disclosures must not turn one company into repeated queue slots."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from dalton_core.company_model_cli import choose_company
from dalton_core.company_model_spec import TASK_HASH
from dalton_core.company_model_state import (
    CompanyModelPromptBudgetError, CompanyModelStateError,
)
from dalton_core.store import content_hash


class HistoricalFilings:
    def __init__(self, filings, current=()):
        self.filings = copy.deepcopy(filings)
        self.current = set(current)
        self.lookups = []

    def statement_filings(self, company_ref=None):
        return copy.deepcopy([
            row for row in self.filings
            if company_ref is None or row['company_ref'] == company_ref
        ])

    def company_model_spec_for_state(self, company_ref, state_hash, *, task_hash):
        self.lookups.append((company_ref, state_hash, task_hash))
        return {'id': 'existing:' + company_ref} if company_ref in self.current else None


class CompanySelectionHistoryTests(unittest.TestCase):
    def setUp(self):
        # A appears again after B, so sorting or treating filings as queue
        # entries changes both progress and the original first-seen order.
        self.filings = [
            {'company_ref': ref, 'ingest_id': f'filing:{i}', 'content_hash': str(i)}
            for i, ref in enumerate(['a', 'b', 'a', 'a', 'c', 'b', 'd'])
        ]
        self.mission = {'universe': [
            {'company_ref': ref, 'ticker': ref.upper()} for ref in 'abcd'
        ]}
        self.built = []

    def builder(self, authority, ref, **kwargs):
        filings = authority.statement_filings(ref)
        self.built.append((ref, filings, kwargs))
        body = {'company_ref': ref, 'filings': filings, **kwargs}
        return {**body, 'state_hash': content_hash(body)}

    def test_current_companies_project_once_and_pending_tail_keeps_all_sources(self):
        authority = HistoricalFilings(self.filings, current=('a', 'b'))
        before = copy.deepcopy(authority.filings)
        with patch('dalton_core.company_model_cli.build_company_model_state', self.builder):
            ref, state = choose_company(authority, self.mission)
        self.assertEqual(ref, 'c')
        self.assertEqual([row[0] for row in self.built], ['a', 'b', 'c'])
        self.assertEqual(self.built[0][1], [r for r in before if r['company_ref'] == 'a'])
        self.assertEqual(self.built[1][1], [r for r in before if r['company_ref'] == 'b'])
        self.assertEqual(authority.filings, before)
        self.assertEqual(state['ticker'], 'C')
        self.assertEqual(authority.lookups[-1], ('c', state['state_hash'], TASK_HASH))

    def test_exclusions_and_foreign_filings_leave_remaining_company_order(self):
        authority = HistoricalFilings([
            {'company_ref': 'foreign', 'ingest_id': 'x'}, *self.filings,
        ], current=('b',))
        with patch('dalton_core.company_model_cli.build_company_model_state', self.builder):
            ref, _ = choose_company(authority, self.mission,
                                    exclude_company_refs=frozenset({'a'}))
        self.assertEqual(ref, 'c')
        self.assertEqual([row[0] for row in self.built], ['b', 'c'])

    def test_invalid_company_is_tried_once_and_next_company_remains_eligible(self):
        authority = HistoricalFilings(self.filings)
        def build(authority, ref, **kwargs):
            self.built.append(ref)
            if ref == 'a':
                raise CompanyModelStateError('exact source rejected')
            return {'state_hash': 'b' * 64}
        with patch('dalton_core.company_model_cli.build_company_model_state', build):
            self.assertEqual(choose_company(authority, self.mission)[0], 'b')
        self.assertEqual(self.built, ['a', 'b'])

    def test_prompt_budget_and_unexpected_errors_propagate(self):
        authority = HistoricalFilings(self.filings)
        for error in (CompanyModelPromptBudgetError(base_prompt_bytes=101, prompt_byte_limit=100), RuntimeError('unexpected')):
            with self.subTest(error=type(error).__name__), patch(
                'dalton_core.company_model_cli.build_company_model_state', side_effect=error,
            ) as build:
                with self.assertRaises(type(error)):
                    choose_company(authority, self.mission)
                self.assertEqual(build.call_count, 1)

    def test_explicit_company_retains_hand_run_behavior_and_source_error(self):
        authority = HistoricalFilings(self.filings)
        with patch('dalton_core.company_model_cli.build_company_model_state', self.builder):
            ref, _ = choose_company(authority, self.mission, company_ref='a',
                                   exclude_company_refs=frozenset({'a'}))
        self.assertEqual(ref, 'a')
        self.assertEqual(len(self.built), 1)
        with patch('dalton_core.company_model_cli.build_company_model_state',
                   side_effect=CompanyModelStateError('exact source rejected')):
            with self.assertRaises(CompanyModelStateError):
                choose_company(authority, self.mission, company_ref='a')


if __name__ == '__main__':
    unittest.main()
