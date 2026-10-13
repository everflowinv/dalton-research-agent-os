"""A lossy historical key cannot erase a typed period disagreement."""
import unittest
from tests import test_directed_numeric_measure_conflict as conflict_fixture
from dalton_core.directed_numeric_executor import source_binding
from dalton_core.directed_numeric_output import quotes
from dalton_core.directed_numeric_research import CONTRACT_REF
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver


class NumericPeriodIdentityCollisionTests(unittest.TestCase):
    def test_original_period_collision_becomes_source_bound_pending_without_new_calls(self):
        for old_period in ('fy2025', 'FY2025全年', 'FY2025上半年'):
            with self.subTest(old_period=old_period):
                fixture = conflict_fixture.NumericMeasureConflictTests(methodName='runTest')
                self.addCleanup(fixture.doCleanups)
                h, w, f, authority, admission, executor, producer, verifier = fixture.fixture()
                proof = authority.registry.search(admission['request'])
                quote, text = next(iter(quotes(proof).items()))
                original = {**producer.candidate_wire['targets'][0]['figure'],
                            'period': old_period, 'quote_id': quote, 'citation_text': text}
                source = source_binding(f.store.connection, admission['request']['registration'],
                                        admission['company_ref'])
                authority._missions.record_document_figures(
                    company_ref=admission['company_ref'], review_ref=source['review_id'],
                    document_ref=source['document_ref'], source_manifest_hash=source['source_manifest_hash'],
                    source_grade=source['source_grade'], figures=[original],
                    observed_by=admission['actor_ref'], numeric_contract_ref=CONTRACT_REF)
                old = authority._missions.document_figures(company_ref=admission['company_ref'], limit=2000)[0]
                result = w.drive(executor, admission)
                receipt = result['numeric_result']
                self.assertEqual(result['research_status'], 'numeric_waiting_evidence')
                self.assertEqual(len(receipt['completed_targets']), 2)
                conflict = receipt['pending_targets'][0]['measure_conflict_proof']
                self.assertEqual(conflict['differing_fields'], ['period'])
                self.assertEqual(conflict['held_fact']['period'], old_period)
                self.assertEqual(conflict['verified_answer']['period'], 'FY2025')
                self.assertEqual(MissionFigureAuthorityResolver(f.store.connection).figure(old['figure_id']), old)
                # Even similar text is not accepted as an equivalent reporting
                # interval. Other supported targets can still be committed.
                self.assertEqual(receipt['material_requests'], [])
                self.assertEqual(executor.run_once(admission['id']), result)
                self.assertEqual((producer.calls, verifier.calls), (1, 1))


if __name__ == '__main__':
    unittest.main()
