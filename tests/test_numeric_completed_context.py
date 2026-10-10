import copy
import unittest

from dalton_core.directed_numeric_research import normalize_targets
from dalton_core.planner_numeric_requirements import numeric_completed_context, numeric_requirements
from dalton_core.research_state import company_state, planning_core_hash
from dalton_core.store import content_hash
from tests import test_research_planner as planner_fixtures
from tests import test_directed_numeric_workflow as numeric_fixtures


COMPANY = planner_fixtures.ACN
DOC = 'sec:filing:synthetic'
VERSION = 'b' * 64


def bind(body):
    body = copy.deepcopy(body)
    body.pop('content_hash', None)
    return {**body, 'content_hash': content_hash(body)}


def requirement(doc=DOC, version=VERSION):
    return {'numeric_strategy_required': True, 'company_ref': COMPANY,
            'question': 'What is the issuer amount and its separate business scope?',
            'document_ref': doc, 'document_authority_hash': version,
            'replaces_numeric_rejection': {'admission_ref': 'admission:old', 'admission_hash': 'd'*64,
                'rejection_ref': 'rejection:old', 'rejection_hash': 'e'*64}}


def completed(count=1, **changes):
    targets = normalize_targets([{'target_ref': f'target:{n}', 'metric_ref': f'metric:amount-{n}',
        'period': 'FY2025', 'unit': 'currency', 'currency': 'USD', 'basis': 'gaap-reported',
        'metric_definition': {'label': 'Issuer total amount', 'meaning': f'Issuer total, not a business subtotal {n}'}}
        for n in range(count)])
    return bind({'id': 'feedback:completed', 'company_ref': COMPANY, 'outcome': 'numeric_targets_completed',
        'created_at': '2026-10-10T18:00:00+00:00', 'document_ref': DOC, 'document_version_hash': VERSION,
        'admission_ref': 'admission:numeric', 'admission_hash': 'a'*64, 'numeric_result_hash': 'c'*64,
        'numeric_targets': targets, 'completed_targets': [{'target_ref': t['target_ref'],
            'metric_definition_hash': t['metric_definition_hash'],
            'figure_ref': f'mission-document-figure:{n}', 'figure_hash': content_hash(t)}
            for n, t in enumerate(targets)], 'missing': [], **changes})


class NumericCompletedContextTests(unittest.TestCase):
    def context(self, rows):
        return numeric_completed_context(rows, company_ref=COMPANY)

    def test_same_source_success_keeps_exact_definition_proofs_and_does_not_close_question(self):
        rows = [requirement(), completed()]
        old = numeric_requirements(rows)
        result = self.context(rows)
        target = result['sources'][0]['targets'][0]
        self.assertEqual(target['metric_definition'], rows[1]['numeric_targets'][0]['metric_definition'])
        self.assertEqual(target['metric_definition_hash'], rows[1]['numeric_targets'][0]['metric_definition_hash'])
        self.assertEqual(target['figure_hash'], rows[1]['completed_targets'][0]['figure_hash'])
        self.assertEqual(result['sources'][0]['numeric_result_hash'], 'c'*64)
        self.assertNotIn('value', target)
        self.assertEqual(numeric_requirements(rows), old)
        self.assertEqual(len(old['requirements']), 1)

    def test_no_pending_or_different_company_original_or_version_never_reuses_success(self):
        row = completed()
        foreign = requirement(); foreign['company_ref'] = 'company:ticker:other'
        for rows in ([foreign, row], [row], [requirement(doc='other:original'), row],
                     [requirement(version='f'*64), row],
                     [requirement(), completed(company_ref='company:ticker:other')]):
            self.assertEqual(self.context(rows), {'sources': [], 'omitted': 0})

    def test_partial_or_unverified_outcome_does_not_assert_completed_scope(self):
        for outcome in ('candidate_staged', 'numeric_waiting_evidence', 'query_miss'):
            self.assertEqual(self.context([requirement(), completed(outcome=outcome)]),
                             {'sources': [], 'omitted': 0})

    def test_complete_hash_definition_and_figure_binding_tampering_refused(self):
        row = completed(); row['numeric_targets'][0]['period'] = 'FY2026'
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.context([requirement(), row])
        for key, value in [('figure_ref', None), ('figure_hash', None), ('metric_definition_hash', 'f'*64)]:
            row = completed(); row['completed_targets'][0][key] = value; row = bind(row)
            with self.assertRaises(ValueError): self.context([requirement(), row])
        row = completed(); row['completed_targets'] = []; row = bind(row)
        with self.assertRaisesRegex(ValueError, 'every target'): self.context([requirement(), row])

    def test_different_definitions_are_kept_not_merged_or_called_equivalent(self):
        first = completed()
        second = completed(created_at='2026-10-10T19:00:00+00:00')
        target = second['numeric_targets'][0]
        target['metric_definition']['meaning'] = 'A separately defined contract scope'
        target.pop('metric_definition_hash')
        second['numeric_targets'] = normalize_targets([target])
        second['completed_targets'][0]['metric_definition_hash'] = second['numeric_targets'][0]['metric_definition_hash']
        second['completed_targets'][0]['figure_ref'] = 'mission-document-figure:other'
        second = bind(second)
        result = self.context([requirement(), first, second])
        targets = result['sources'][0]['targets']
        self.assertEqual(len(targets), 2)
        self.assertEqual(len({t['metric_definition_hash'] for t in targets}), 2)
        self.assertEqual(len(numeric_requirements([requirement(), first, second])['requirements']), 1)

    def test_four_target_limit_has_exact_omission_hash_and_shared_source(self):
        rows = [requirement(), completed(count=6)]
        result = self.context(rows)
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(len(result['sources'][0]['targets']), 4)
        self.assertEqual(result['omitted'], 2)
        self.assertEqual(len(result['omitted_hash']), 64)
        self.assertEqual(result, self.context(rows))

    def test_completed_material_wakes_once_without_plan_timestamp_churn(self):
        def state(rows):
            return planner_fixtures.state(document_research_feedback={'status': 'available', 'by_company': {COMPANY: rows}})
        before = state([requirement()]); after = state([requirement(), completed()])
        self.assertNotEqual(planning_core_hash(before), planning_core_hash(after))
        later = completed(id='feedback:later-plan', created_at='2026-10-10T19:00:00+00:00')
        self.assertEqual(planning_core_hash(after), planning_core_hash(state([requirement(), later])))
        self.assertNotIn('numeric_completed_context', company_state({'company_ref': COMPANY, 'items': []}))

    def test_actual_numeric_authority_success_is_available_only_after_formal_completion(self):
        h = numeric_fixtures.NumericWorkflowTests('runTest'); h.addCleanup = self.addCleanup
        f, authority, admission, executor, producer, verifier = h.fixture()
        h.drive(executor, admission)
        from dalton_core.directed_numeric_executor import numeric_feedback
        rows = numeric_feedback(executor.connection, f.mission['id'])
        pending = requirement(admission['request']['registration']['document_ref'], admission['request']['registration']['content_hash'])
        pending['company_ref'] = admission['company_ref']
        result = numeric_completed_context([pending, *rows], company_ref=admission['company_ref'])
        self.assertEqual(sum(len(x['targets']) for x in result['sources']), 3)
        self.assertEqual(result['sources'][0]['admission_ref'], admission['id'])
        class UnsettledView:
            def execute(inner, sql, args=()):
                if sql.startswith('SELECT result_envelope_json,result_envelope_hash,terminal_state'):
                    return executor.connection.execute('SELECT 1 WHERE 0')
                return executor.connection.execute(sql, args)
        self.assertEqual(numeric_completed_context([pending, *numeric_feedback(UnsettledView(), f.mission['id'])],
            company_ref=admission['company_ref']), {'sources': [], 'omitted': 0})


if __name__ == '__main__':
    unittest.main()
