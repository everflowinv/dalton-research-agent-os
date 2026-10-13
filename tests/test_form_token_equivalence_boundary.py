import copy
import unittest
from unittest.mock import patch
from dalton_core import document_inquiry_equivalence as equivalence
from dalton_core.numeric_target_revision import derive_revision_proof, verify_revision_proof
from dalton_core.mission_document_research import MissionDocumentResearchError
from tests import test_numeric_waiting_target_revision as fixture


class FormBoundaryTests(unittest.TestCase):
    def test_forms_adjacent_to_chinese_and_english_are_not_financial_figures(self):
        for text in ('10-K中', '10-K收入附注', '年报10-K债务', '10-Q里', '20-F与',
                     '10-K filing', '10-Q: revenue', "20-F's notes", '１０－Ｋ中'):
            with self.subTest(text=text):
                self.assertEqual(equivalence.question_figures(text), frozenset())
        # ASCII identifiers are not silently rewritten into form tokens.
        self.assertEqual(equivalence.question_figures('10-King'), frozenset({'1'}))

    def test_actual_numbers_and_same_question_still_block_rephrasing(self):
        self.assertEqual(equivalence.question_figures('10-K中 15.8% 和 $970 million'), frozenset({'158', '97'}))
        left = equivalence.inquiry_signature('company:test', '10-K中 15.8% 是什么？')
        right = equivalence.inquiry_signature('company:test', '10-Q里 15.8% 的含义？')
        self.assertEqual(equivalence.signatures_equivalent(left, right), 'figure:158')
        self.assertEqual(equivalence.signatures_equivalent(left, left), 'question')
        self.assertIsNone(equivalence.signatures_equivalent(left,
            equivalence.inquiry_signature('company:other', '10-K中 15.8% 是什么？')))

    def historical_consumer(self):
        h = fixture.NumericWaitingRevisionTests('runTest')
        self.addCleanup(h.doCleanups)
        helper, f, a, initial, _, producer, verifier = h.fixture()
        old = h.inquiry(initial, changed=False)
        old['question'] = 'Issuer FY2025 10-K债务附注里的债务金额是多少？'
        def v1_match(company, inquiry, other):
            return equivalence.signatures_equivalent(equivalence.inquiry_signature_for(company,inquiry),
                                                      equivalence.inquiry_signature_for(company,other))
        # Seed the historical record under the actual old text matcher; v2
        # correctly treats an unchanged explicit target scope as a duplicate.
        with patch('dalton_core.document_numeric_inquiry_equivalence.admission_match', side_effect=v1_match):
            admitted = h.admit(helper, f, a, initial, old, tag='distinct-old-debt')
        parent = a.resolve_for_execution(admitted['admission_ref'])
        helper.drive(helper._executor(f, a, draft_adapter=producer, verifier_adapter=verifier)[0], parent)
        revised = h.inquiry(parent)
        revised['question'] = 'Issuer FY2025 10-K收入附注里的商业RPO金额是多少？'
        # Produce historical authority with the old code's documented bug.
        from dalton_core import numeric_target_revision
        old_proof_for = numeric_target_revision._proof_for
        def historical_proof(*args, **kwargs):
            return old_proof_for(*args, **{**kwargs, '_historical_signature':equivalence.inquiry_signature_for})
        with patch.object(equivalence, '_FORM', equivalence._LEGACY_FORM), \
             patch.object(numeric_target_revision, '_proof_for', side_effect=historical_proof):
            consumer = h.admit(helper, f, a, parent, revised, tag='historical-form-collision')
            held = a.resolve_for_execution(consumer['admission_ref'])
        return h, helper, f, a, parent, held

    def test_stored_old_consumer_replays_but_different_new_lease_has_no_revision(self):
        h, helper, f, a, parent, held = self.historical_consumer()
        before = f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?', (held['id'],)).fetchone()
        marker = held['numeric_authority']['target_revision_proof']
        self.assertEqual(verify_revision_proof(a, held['mission_ref'], held['planner_inquiry'], marker), marker)
        self.assertEqual(a.resolve_for_execution(held['id']), held)
        self.assertEqual(tuple(before), tuple(f.store.connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?', (held['id'],)).fetchone()))
        lease = copy.deepcopy(held['planner_inquiry'])
        lease['question'] = 'Issuer FY2025 10-K中，2026财年合并租赁到期额是多少？'
        self.assertIsNone(derive_revision_proof(a, held['mission_ref'], lease))
        self.assertEqual(a.equivalent_open_admission({'company_ref':held['company_ref'],
            'mission_ref':held['mission_ref'], 'planner_inquiry':lease})['state'], 'in_flight')
        from dalton_core.directed_numeric_research import normalize_targets
        target = copy.deepcopy(lease['directed_document']['numeric_targets'][-1])
        target['period'] = 'FY2026'
        lease['directed_document']['numeric_targets'] = normalize_targets([target])
        result = h.admit(helper, f, a, parent, lease, tag='new-lease-normal-admission')
        self.assertEqual(result['status'], 'fresh')
        current = a.resolve_for_execution(result['admission_ref'])
        self.assertIsNone(current['numeric_authority'].get('target_revision_proof'))
        self.assertIsNone(equivalence.signatures_equivalent(
            equivalence.inquiry_signature_for(parent['company_ref'], parent['planner_inquiry']),
            equivalence.inquiry_signature_for(held['company_ref'], held['planner_inquiry'])))

    def test_legacy_proof_cannot_be_borrowed_without_exact_stored_consumer(self):
        _, _, _, a, _, held = self.historical_consumer()
        marker = held['numeric_authority']['target_revision_proof']
        with patch('dalton_core.numeric_target_revision._revision_consumers', return_value=[]):
            with self.assertRaises(MissionDocumentResearchError):
                verify_revision_proof(a, held['mission_ref'], held['planner_inquiry'], marker)
        for mode in ('question', 'because', 'proof_hash', 'source'):
            inquiry = copy.deepcopy(held['planner_inquiry']); proof = dict(marker)
            if mode == 'question': inquiry['question'] = 'Issuer FY2025 10-K中，租赁付款是多少？'
            if mode == 'because': inquiry['because'] += ' changed'
            if mode == 'proof_hash': proof['new_target_scope_hash'] = '0' * 64
            if mode == 'source': inquiry['directed_document']['document_version_hash'] = '0' * 64
            with self.subTest(mode=mode), self.assertRaises(MissionDocumentResearchError):
                verify_revision_proof(a, held['mission_ref'], inquiry, proof)
