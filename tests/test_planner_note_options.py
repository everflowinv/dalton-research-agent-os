import copy
import json
import unittest

from dalton_core.planner_note_options import option_ref, prompt_state
from dalton_core.research_planner import build_prompt, plan_from_response, ResearchPlanError, prompt_size_report
from tests.test_document_research_strategy import with_document, evidence_target, strategy
from tests.test_research_planner import ACN, IBM, NOW, inquiry, response


class PlannerNoteOptionsTests(unittest.TestCase):
    def fixture(self):
        state = with_document()
        document = state['companies'][0]['readable_documents'][0]
        document['evidence_targets'] = [evidence_target()]
        return state, document

    def test_prompt_compacts_without_changing_original_and_expands_before_normal_validation(self):
        state, doc = self.fixture()
        before = copy.deepcopy(state)
        rendered = json.loads(build_prompt(state).split('RESEARCH_STATE=', 1)[1])
        target = rendered['companies'][0]['readable_documents'][0]['evidence_target_options'][0]
        selected = strategy(evidence_target_ref=target['option_ref'])
        planned = plan_from_response(state, response(inquiries=[inquiry(directed_document=selected)]), created_at=NOW)
        self.assertEqual(planned['inquiries'][0]['directed_document'], strategy(evidence_target=evidence_target()))
        self.assertEqual(state, before)
        self.assertEqual(target['periods'], evidence_target()['periods'])
        self.assertNotIn('evidence_targets', rendered['companies'][0]['readable_documents'][0])
        report = prompt_size_report(state)
        self.assertGreater(report['fixed_prompt_bytes'], 0)
        self.assertEqual(report['state_bytes'], len(json.dumps(rendered, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()))

    def test_options_cannot_cross_company_document_version_or_changed_target(self):
        state, doc = self.fixture()
        selected = strategy(evidence_target_ref=option_ref(ACN, doc, doc['evidence_targets'][0]))
        for change in ('company', 'document', 'version', 'target', 'ambiguous', 'unreadable'):
            changed = copy.deepcopy(state)
            d = changed['companies'][0]['readable_documents'][0]
            q = inquiry(directed_document=selected)
            if change == 'company': q['company_ref'] = IBM
            elif change == 'document': d['document_ref'] = 'other'
            elif change == 'version': d['document_version_hash'] = 'd'*64
            elif change == 'target': d['evidence_targets'][0]['statement_filing_hash'] = 'e'*64
            elif change == 'ambiguous': d['evidence_targets'].append(copy.deepcopy(d['evidence_targets'][0]))
            else: d['readable'] = False
            with self.subTest(change=change), self.assertRaises(ResearchPlanError):
                plan_from_response(changed, response(inquiries=[q]), created_at=NOW)

    def test_both_full_and_option_target_unknown_ref_and_numeric_mix_are_refused(self):
        state, doc = self.fixture()
        ref = option_ref(ACN, doc, doc['evidence_targets'][0])
        for extra in ({'evidence_target': evidence_target()}, {'evidence_target_ref':'unknown'},
                      {'numeric_targets': []}, {'evidence_target_ref': None}):
            value = {**strategy(evidence_target_ref=ref), **extra}
            with self.subTest(extra=extra), self.assertRaises(ResearchPlanError):
                plan_from_response(state, response(inquiries=[inquiry(directed_document=value)]), created_at=NOW)

    def test_every_company_and_target_remains_selectable(self):
        state, doc = self.fixture()
        for n in range(1, 20):
            company = copy.deepcopy(state['companies'][0])
            company['company_ref'] = 'company:example:'+str(n)
            state['companies'].append(company)
        rendered, count = prompt_state(state)
        self.assertEqual(count, 20)
        refs = [d['evidence_target_options'][0]['option_ref'] for c in rendered['companies']
                for d in c.get('readable_documents', []) if d.get('evidence_target_options')]
        self.assertEqual(len(set(refs)), count)

    def test_no_target_keeps_prompt_schema_and_payload_unchanged(self):
        state = with_document()
        rendered, count = prompt_state(state)
        self.assertEqual(rendered, state)
        self.assertEqual(count, 0)
        self.assertNotIn('evidence_target_ref', build_prompt(state))

    def test_dual_source_lineage_restores_exactly_and_changed_lineage_is_not_an_option(self):
        from tests.test_financial_note_dual_source import DualSourceNoteTests
        from dalton_core.planner_note_options import expand_selection
        from dalton_core.document_research_strategy import DocumentResearchStrategyError
        from dalton_core.store import content_hash
        helper = DualSourceNoteTests()
        self.addCleanup(helper.doCleanups)
        _, _, admission, target = helper.fixture()
        company = admission['company_ref']
        reg = admission['request']['registration']
        doc = {'document_ref': reg['document_ref'], 'document_version_hash': reg['content_hash'],
               'evidence_targets': [target]}
        state = {'companies': [{'company_ref': company, 'readable_documents': [doc]}]}
        selected = {'document_ref': doc['document_ref'], 'document_version_hash': doc['document_version_hash'],
                    'evidence_target_ref': option_ref(company, doc, target)}
        self.assertEqual(expand_selection(selected, company_ref=company, state=state)['evidence_target'], target)
        changed = copy.deepcopy(state)
        lineage = changed['companies'][0]['readable_documents'][0]['evidence_targets'][0]['source_lineage_binding']
        lineage['statement_mission_hash'] = '0'*64
        lineage['content_hash'] = content_hash({k:v for k,v in lineage.items() if k != 'content_hash'})
        with self.assertRaises(DocumentResearchStrategyError):
            expand_selection(selected, company_ref=company, state=changed)


if __name__ == '__main__': unittest.main()
