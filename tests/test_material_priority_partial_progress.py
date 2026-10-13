"""A slow or invalid later request cannot erase independently proved work."""
import time
import unittest
from unittest.mock import patch

from dalton_core import material_current_gap_priority as priority
from dalton_core import material_requests as requests
from tests import test_material_current_gap_priority as fixture


class PartialPriorityProgressTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.CurrentGapProofTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.first_spec = self.fx.spec(fixture.fixtures.SYNW)
        self.first = self.fx.request(self.first_spec)
        self.second_spec = self.fx.spec(fixture.fixtures.OTHER)
        self.second = self.fx.request(self.second_spec)
        self.pending = [requests.request(self.fx.state, item['id'])
                        for item in (self.first, self.second)]

    def test_deadline_preserves_completed_proof_without_admitting_unchecked_request(self):
        clock = [1.0]
        original = priority._proof
        def prove(*args):
            result = original(*args)
            clock[0] = 11.0
            return result
        with patch.object(priority, '_proof', side_effect=prove), \
             patch.object(priority.time, 'monotonic', side_effect=lambda: clock[0]):
            proofs, info = priority.priority_gaps(self.fx.state, self.pending, deadline=10)
        self.assertEqual(set(proofs), {self.first['id']})
        self.assertEqual(info['status'], 'partial')
        self.assertEqual(info['qualified'], 1)
        self.assertEqual(info['reason_code'], 'priority_read_budget')
        self.assertTrue(priority.still_current(self.fx.state, proofs[self.first['id']],
                                              deadline=time.monotonic()+5))
        self.assertEqual(requests.request(self.fx.state, self.first['id'])['attempt_count'], 0)

    def test_corrupt_other_company_does_not_discard_valid_first_company(self):
        self.fx.connection.execute('UPDATE coverage_mission_company_model_specs SET content_hash=? WHERE spec_id=?',
                                   ('0'*64, self.second_spec['spec_id']))
        self.fx.connection.commit()
        proofs, info = priority.priority_gaps(self.fx.state, self.pending, deadline=time.monotonic()+5)
        self.assertEqual(set(proofs), {self.first['id']})
        self.assertEqual(info['status'], 'partial')
        self.assertEqual(info['reason_code'], 'current_gap_proof_unavailable')

    def test_expired_before_first_candidate_grants_no_priority(self):
        proofs, info = priority.priority_gaps(self.fx.state, self.pending, deadline=time.monotonic()-1)
        self.assertEqual(proofs, {})
        self.assertEqual(info['qualified'], 0)
        self.assertEqual(info['status'], 'unavailable')

    def test_invalid_shared_mission_cannot_reuse_previously_valid_proofs(self):
        self.assertEqual(len(priority.priority_gaps(self.fx.state, self.pending, deadline=time.monotonic()+5)[0]), 2)
        self.fx.connection.execute('UPDATE coverage_mission_versions SET content_hash=?', ('0'*64,))
        self.fx.connection.commit()
        proofs, info = priority.priority_gaps(self.fx.state, self.pending, deadline=time.monotonic()+5)
        self.assertEqual(proofs, {})
        self.assertEqual(info['qualified'], 0)

    def test_partial_proof_still_expires_when_current_spec_changes(self):
        original = priority._proof
        def prove(request, *args):
            if request['id'] == self.second['id']:
                raise ValueError('unrelated malformed source')
            return original(request, *args)
        with patch.object(priority, '_proof', side_effect=prove):
            proofs, _ = priority.priority_gaps(self.fx.state, self.pending, deadline=time.monotonic()+5)
        proof = proofs[self.first['id']]
        self.fx.spec(fixture.fixtures.SYNW, number=2)
        self.assertFalse(priority.still_current(self.fx.state, proof, deadline=time.monotonic()+5))


if __name__ == '__main__':
    unittest.main()
