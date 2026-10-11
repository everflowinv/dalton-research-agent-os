"""Unperioded filing originals stay available; explicit authority stays strict."""
import copy
from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core.material_retrieval import RULE_REF, contract
from tests import test_material_acquisition as f
from tests import test_material_pack as corpus_fixture

NOW = '2026-10-11T12:00:00+00:00'


class HeldFilingAnswerWindowTests(f.Fixture):
    def answer(self, *, subject=corpus_fixture.SYNW, period=None, acceptance=None, question=None):
        return self.request(subject=subject, period=period, now=NOW,
            need={'kind': 'answer', 'question': question or
                  '10-K 原文指标 -- terms: utilization, consulting new bookings'},
            acceptance=acceptance or {'min_passages': 1, 'min_coverage': .5},
            source_priority=['indexed_corpus', 'web'])

    def annual(self, *, company=corpus_fixture.SYNW, date='2025-10-10'):
        title = ('Synthetic Widgets Co' if company == corpus_fixture.SYNW else 'Other Gadgets Inc')
        return self.corpus.add('sec_10k', title+' annual 10-K',
            'Utilization was 92 percent. '+corpus_fixture.FILL*3,
            date=date, company=company)

    def test_cross_company_anniversary_does_not_hide_old_original_or_complete_missing_targets(self):
        for company in (corpus_fixture.SYNW, corpus_fixture.OTHER):
            with self.subTest(company=company):
                req = self.answer(subject=company)
                before = copy.deepcopy(req)
                ref = self.annual(company=company)
                for today in ('2026-10-09', '2026-10-10', '2026-10-11'):
                    self.assertEqual(ma._window_with_acceptance(req, now=today), (None, None))
                    result = ma.evaluate_acceptance(self.state, req, now=today)
                    self.assertEqual((result['met'], result['partial']), (False, True), result)
                    self.assertEqual({x['document_ref'] for x in result['items']}, {ref})
                    self.assertTrue(result['target_passages']['utilization'])
                    self.assertEqual(result['missing_targets'], ['consulting new bookings'])
                self.assertEqual(req, before)

    def test_explicit_period_current_and_freshness_limits_still_exclude_old_filing(self):
        self.annual()
        for period, bounds in [
            ('current', {}), ('quarter_ending:2026-08-31', {}), ('as_of:2026-10-11', {}),
            (None, {'max_age_days': 365}), (None, {'published_after': '2025-10-10'}),
            (None, {'date_from': '2025-10-11'}), (None, {'date_to': '2025-10-09'}),
        ]:
            with self.subTest(period=period, bounds=bounds):
                req = self.answer(period=period, acceptance={'min_passages': 1, **bounds})
                result = ma.evaluate_acceptance(self.state, req, now=NOW)
                self.assertEqual((result['met'], result['partial']), (False, False), result)
                self.assertEqual(result['items'], [])

    def test_explicit_historical_bounds_are_not_silently_narrowed_to_one_year(self):
        self.annual()
        req = self.answer(acceptance={'min_passages': 1, 'max_age_days': 430,
                                    'date_from': '2025-10-01', 'date_to': '2025-10-10'})
        self.assertEqual(ma._window_with_acceptance(req, now=NOW), ('2025-10-01', '2025-10-10'))
        self.assertTrue(ma.evaluate_acceptance(self.state, req, now=NOW)['partial'])
        req['acceptance']['published_after'] = '2025-10-10'
        self.assertEqual(ma._window_with_acceptance(req, now=NOW), ('2025-10-11', '2025-10-10'))
        self.assertFalse(ma.evaluate_acceptance(self.state, req, now=NOW)['partial'])

    def test_bound_fiscal_period_and_selected_version_stay_unproved(self):
        ref = self.annual()
        period = self.answer(question='FY2025 10-K 原文 -- terms: utilization')
        result = ma.evaluate_acceptance(self.state, period, now=NOW)
        self.assertEqual((result['met'], result['partial']), (False, True))
        self.assertIn('财政期间', result['reason'])
        acceptance = {'min_passages': 1, 'retrieval': {'rule_ref': RULE_REF,
            'query_terms': ['utilization'], 'wants': '原文', 'doc_kinds': ['sec_10k'],
            'selected_document': {'document_ref': 'doc:foreign', 'document_version_hash': 'a'*64}}}
        req = self.answer(acceptance=acceptance)
        before = copy.deepcopy(req)
        self.assertFalse(ma.evaluate_acceptance(self.state, req, now=NOW)['partial'])
        req['acceptance']['retrieval']['selected_document']['document_ref'] = ref
        selected = copy.deepcopy(contract(req)['selected_document'])
        result = ma.evaluate_acceptance(self.state, req, now=NOW)
        self.assertEqual((result['met'], result['partial']), (False, True))
        self.assertIn('确切版本', result['reason'])
        self.assertEqual(contract(req)['selected_document'], selected)
        self.assertEqual(before['acceptance']['retrieval']['selected_document']['document_ref'], 'doc:foreign')

    def test_existing_closed_request_rechecks_locally_without_rewriting_need_or_spend(self):
        req = self.answer()
        ma.ensure_schema(self.state)
        ma._record_spend(self.state, req['id'], 'web', 3, 105000, now=NOW)
        old_marker = {'rule_ref': RULE_REF, 'retrieval_contract_hash': contract(req)['content_hash']}
        mr.record_attempt(self.state, req['id'], source='indexed_corpus', outcome='empty',
                          detail='prior rolling-window miss', query=old_marker, now=NOW)
        mr.close(self.state, req['id'], status='exhausted', reason='prior local date-window miss', now=NOW)
        before = mr.request(self.state, req['id'])
        self.annual()
        web = f.FakeRung('web', [])
        result = ma.serve(self.state, req['id'], rungs={'web': web}, now=NOW)
        after = mr.request(self.state, req['id'])
        self.assertEqual((result['outcome'], result['status']), ('cached', 'exhausted'), result)
        self.assertEqual((result['acceptance']['met'], result['acceptance']['partial']), (False, True))
        self.assertTrue(result['arrived'])
        self.assertEqual(result['free_recheck'], old_marker | {
            'window_rule_ref': ma.HELD_FILING_ANSWER_WINDOW_RULE_REF})
        self.assertEqual(web.runs, 0)
        self.assertEqual(ma.request_spend(self.state, req['id']), {'calls': 3, 'cost_micros': 105000})
        self.assertEqual(after['events'][:len(before['events'])], before['events'])
        again = ma.serve(self.state, req['id'], rungs={'web': web}, now=NOW)
        self.assertEqual(again['outcome'], 'cached')
        self.assertEqual(mr.request(self.state, req['id'])['events'], after['events'])
        self.assertEqual(web.runs, 0)
        self.assertEqual(ma.request_spend(self.state, req['id']), {'calls': 3, 'cost_micros': 105000})
        for field in ('id', 'subject', 'need', 'period', 'acceptance', 'budget'):
            self.assertEqual(after[field], before[field])

    def test_complete_unperioded_filing_answer_can_finish_existing_request_without_paid_rung(self):
        req = self.answer(question='10-K 原文 -- terms: utilization')
        mr.close(self.state, req['id'], status='exhausted', reason='prior local miss', now=NOW)
        self.annual()
        web = f.FakeRung('web', [])
        result = ma.serve(self.state, req['id'], rungs={'web': web}, now=NOW)
        self.assertEqual((result['status'], result['outcome']), ('found', 'found'))
        self.assertTrue(result['acceptance']['met'])
        self.assertEqual(result['acceptance']['missing_targets'], [])
        self.assertTrue(result['arrived'])
        self.assertEqual(web.runs, 0)
        self.assertEqual(ma.request_spend(self.state, req['id']), {'calls': 0, 'cost_micros': 0})

    def test_other_unperioded_needs_retain_default_window(self):
        for need in ({'kind': 'answer', 'question': 'Find utilization in a research report'},
                     {'kind': 'data', 'data_item': 'utilization'},
                     {'kind': 'document', 'doc_kinds': ['sec_10k']}):
            with self.subTest(need=need):
                req = {'need': need, 'period': None, 'acceptance': {}}
                self.assertEqual(ma._window_with_acceptance(req, now=NOW), ('2025-10-11', None))
        self.assertEqual(ma.period_window(None, now=NOW)['date_from'], '2025-10-11')
