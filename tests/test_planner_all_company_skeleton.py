"""A fixed prompt bound must not permanently exclude lower priority companies."""
from __future__ import annotations
import copy
import unittest
from dalton_core.planner_numeric_requirements import numeric_requirements
from dalton_core.research_planner import (
    ResearchPlanInputTooLarge, build_prompt, followup_context_matches_prompt,
    plan_from_response, project_state_for_prompt,
)
from dalton_core.store import content_hash
from tests import test_research_planner as f


class AllCompanySkeletonTests(unittest.TestCase):
    def inventory(self, count=5, *, numeric=False):
        built = f.FeedbackProjectionTests().worst_case(count)
        built['document_research_policy'] = {'max_query_terms': 16, 'max_query_term_chars': 240}
        for index, company in enumerate(built['companies']):
            # Held statement history is large without constituting new work.
            company['filed_statement_figures'] = {
                'filings_held': 24, 'segment_lines': 320,
                'segments': [{'period': str(n), 'label': 'historical segment ' * 25} for n in range(120)],
            }
            company['reading_pipeline'] = {
                'by_kind': {'annual_report': {'open': 1, 'staged': 4}},
                'open_blockers': [{'document_ref': f'old:{n}', 'blocker': 'Queued for normal reading.'} for n in range(40)],
                'last_run': {'status': 'completed'},
            }
            document = {
                'document_ref': f'document:synthetic-open:{index}',
                'document_version_hash': content_hash(index), 'authority_hash': content_hash(index),
                'authority_ref': f'registered-document:synthetic:{index}',
                'readable': True, 'completeness': 'complete', 'doc_date': '2020-01-01',
                'operations': ['search_registered_document'],
                'prior_review': {'detail': 'Historical annotation. ' * 2500},
            }
            company['readable_documents'].append(document)
            question = f'What is the remaining obligation for synthetic issuer {index}?'
            company['document_research_reopened'] = [{'question': question, 'document_ref': document['document_ref']}]
            if numeric:
                observation = {
                    'id': f'observation:synthetic:{index}', 'question': question,
                    'document_ref': document['document_ref'],
                    'document_authority_hash': document['document_version_hash'],
                    'numeric_strategy_required': True,
                    'replaces_numeric_rejection': {
                        'admission_ref': f'admission:synthetic:{index}', 'admission_hash': content_hash(['a', index]),
                        'rejection_ref': f'rejection:synthetic:{index}', 'rejection_hash': content_hash(['r', index]),
                    },
                }
                company['numeric_reading_requirements'] = numeric_requirements([observation])
        return f.FeedbackProjectionTests.rebind(built)

    def project(self, built, bound=64000):
        before = copy.deepcopy(built)
        projected = project_state_for_prompt(built, max_input_bytes=bound)
        self.assertEqual(built, before)
        self.assertLessEqual(len(build_prompt(projected).encode()), bound)
        self.assertEqual([c['company_ref'] for c in projected['companies']],
                         [c['company_ref'] for c in built['companies']])
        self.assertEqual(projected['prompt_projection']['companies_carried'], len(built['companies']))
        self.assertNotIn('companies_omitted_summary', projected)
        self.assertNotIn('must not be directed', projected['prompt_projection']['notice'])
        return projected

    def test_last_company_unique_open_question_remains_executable_at_64kb(self):
        built = self.inventory()
        last = built['companies'][-1]
        for company in built['companies'][:-1]:
            company['document_research_reopened'] = []
        built = f.FeedbackProjectionTests.rebind(built)
        projected = self.project(built)
        self.assertIn('aggregate_companies', projected['prompt_projection']['stages_applied'])
        carried = projected['companies'][-1]
        self.assertEqual(carried['document_research_reopened'], last['document_research_reopened'])
        document = last['readable_documents'][-1]
        self.assertIn((document['document_ref'], document['document_version_hash']),
                      [(d['document_ref'], d['document_version_hash']) for d in carried['readable_documents']])
        strategy = {'strategy_version': 'directed-document:0.1', 'document_ref': document['document_ref'],
                    'document_version_hash': document['document_version_hash'],
                    'query_terms': ['remaining obligation'], 'query_rationale': 'Read the held original.'}
        plan = plan_from_response(built, f.response(inquiries=[f.inquiry(
            company_ref=last['company_ref'], question=last['document_research_reopened'][0]['question'],
            directed_document=strategy)]), created_at=f.NOW)
        self.assertEqual(plan['inquiries'][0]['company_ref'], last['company_ref'])

    def test_five_company_execution_core_fits_with_large_global_followup_context(self):
        built = self.inventory()
        built['reflection_followup_context'] = {
            'groups': [{'group_ref': 'synthetic:group', 'suggestions': [
                {'reason': 'Research priority proposal. ' * 260} for _ in range(3)]}],
            'backlog_questions': [
                {'question': 'Compare only source-bound current obligations. ' * 25}
                for _ in range(3)],
        }
        built = f.FeedbackProjectionTests.rebind(built)
        projected = self.project(built)
        context = projected['reflection_followup_context']
        full = built['reflection_followup_context']
        # Bounded prompt material adds provenance, not a different obligation.
        self.assertEqual(context['groups'], full['groups'])
        self.assertEqual(context['backlog_questions'], full['backlog_questions'])
        self.assertEqual(context['projection']['full_context_hash'], content_hash(full))
        self.assertTrue(followup_context_matches_prompt(context, full))
        changed = copy.deepcopy(context)
        changed['groups'][0]['suggestions'][0]['reason'] = 'Unbound replacement obligation'
        self.assertFalse(followup_context_matches_prompt(changed, full))

    def test_twelve_companies_all_have_a_selectable_document_under_the_bound(self):
        projected = self.project(self.inventory(12))
        for company in projected['companies']:
            self.assertTrue(company['items'])
            self.assertTrue(company['readable_documents'])
            self.assertTrue(company['document_research_reopened'])

    def test_every_numeric_requirement_binding_and_old_original_survive(self):
        built = self.inventory(numeric=True)
        projected = self.project(built)
        for original, carried in zip(built['companies'], projected['companies']):
            self.assertEqual(carried['numeric_reading_requirements'], original['numeric_reading_requirements'])
            requirement = carried['numeric_reading_requirements']['requirements'][0]
            self.assertIn((requirement['document_ref'], requirement['document_version_hash']),
                          [(d['document_ref'], d['document_version_hash']) for d in carried['readable_documents']])
            self.assertEqual(set(requirement['replaces_numeric_rejection']),
                             {'admission_ref', 'admission_hash', 'rejection_ref', 'rejection_hash'})

    def test_irreducible_requirement_is_refused_instead_of_dropping_its_company(self):
        built = self.inventory(numeric=True)
        built['companies'][-1]['numeric_reading_requirements']['requirements'][0]['question'] = 'Irreducible original ' * 6000
        built = f.FeedbackProjectionTests.rebind(built)
        with self.assertRaises(ResearchPlanInputTooLarge) as caught:
            project_state_for_prompt(built, max_input_bytes=64000)
        self.assertIn('aggregate_companies', caught.exception.report['stages_applied'])
        self.assertEqual(len(built['companies']), 5)

    def test_historical_detail_summaries_are_exact_not_missing_evidence_claims(self):
        built = self.inventory()
        projected = self.project(built)
        for original, carried in zip(built['companies'], projected['companies']):
            summary = carried['historical_detail_summary']
            self.assertEqual(summary['filed_statement_figures']['content_hash'], content_hash(original['filed_statement_figures']))
            self.assertEqual(carried['filed_statement_figures']['filings_held'], 24)
            self.assertEqual(carried['reading_pipeline']['open_blockers_omitted'], 39)
            self.assertEqual(carried['reading_pipeline']['open_blockers'][0], original['reading_pipeline']['open_blockers'][0])
            self.assertEqual(carried['reading_pipeline']['omitted_blockers_hash'], content_hash(original['reading_pipeline']['open_blockers'][1:]))


if __name__ == '__main__':
    unittest.main()
