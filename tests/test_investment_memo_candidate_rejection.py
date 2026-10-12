"""Company-local launch refusal cannot starve independent memo candidates."""
import hashlib
import json
import unittest
from unittest.mock import patch

from dalton_core.investment_memo_launcher import InvestmentMemoLauncher
from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.mission_investment_memo_lane import company_signature
from tests import test_investment_memo_lane as fixtures


class MemoCandidateRejectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MemoFairnessTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.launcher = self.fixture.launcher

    def test_first_rejected_company_does_not_starve_next_or_modify_budget(self):
        coordinator = self.fixture.coordinator()
        start = self.launcher.start
        def dispatch(**kwargs):
            if kwargs['company_ref'] == 'company:A':
                raise LaneChildRejected('memo pause is unproved; no automatic resume')
            return start(**kwargs)
        with patch.object(self.launcher, 'start', side_effect=dispatch), \
                patch.object(coordinator.budget, 'record') as record, \
                patch.object(coordinator.budget, 'clear') as clear:
            result = coordinator.dispatch_once()
        self.assertEqual(result['status'], 'launched')
        self.assertEqual(result['company_ref'], 'company:B')
        self.assertEqual(list(result['rejected']), ['company:A'])
        self.assertEqual(coordinator.open_ticket, result['ticket_ref'])
        self.assertEqual([t['company_ref'] for t in self.launcher.started], ['company:B'])
        record.assert_not_called(); clear.assert_not_called()

    def test_all_rejected_are_explained_and_no_child_is_opened(self):
        coordinator = self.fixture.coordinator()
        with patch.object(self.launcher, 'start', side_effect=LaneChildRejected('config unproved')) as start:
            result = coordinator.dispatch_once()
        self.assertEqual(start.call_count, 2)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['rejected'], {'company:A': 'config unproved', 'company:B': 'config unproved'})
        self.assertIn('company:A', result['reason']); self.assertIn('company:B', result['reason'])
        self.assertIsNone(coordinator.open_ticket)

    def test_real_shared_slot_conflict_stops_later_candidates(self):
        coordinator = self.fixture.coordinator()
        with patch.object(self.launcher, 'start', side_effect=LaneChildConflict('child still running')) as start:
            result = coordinator.dispatch_once()
        self.assertEqual(result['status'], 'busy')
        self.assertEqual(start.call_count, 1)
        self.assertIsNone(coordinator.open_ticket)

    def test_real_old_unproved_pause_remains_rejected_while_second_can_start(self):
        root = self.fixture.root
        launcher = InvestmentMemoLauncher(state_dir=root,
            model_config_path=self.launcher.model_config_path,
            verifier_model_config_path=self.launcher.verifier_model_config_path)
        self.fixture.launcher = launcher
        provider = 'f' * 64
        with patch('dalton_core.cockpit_model.verifier_provider_contract_fingerprint', return_value=provider):
            signature = company_signature(self.fixture.inputs['company:A'], launcher)
        digest = hashlib.sha256(f'memo|company:A|{signature}|{provider}'.encode()).hexdigest()[:24]
        directory = launcher.tickets_dir / digest
        directory.mkdir(parents=True)
        request = directory / 'quiesce-request.json'
        request.write_text(json.dumps({'unproved': True}))
        before = request.read_bytes()
        old_ticket = {'id': 'investment-memo-run:' + digest, 'status': 'failed',
                      'summary': {'status': 'refused', 'memo_status': 'draft_refused'}}
        coordinator = self.fixture.coordinator()
        def spawned(**kwargs):
            return {'id': 'investment-memo-run:' + kwargs['digest'], **kwargs['record']}
        with patch('dalton_core.cockpit_model.verifier_provider_contract_fingerprint', return_value=provider), \
                patch.object(launcher, 'status', return_value=old_ticket), \
                patch.object(launcher, 'spawn', side_effect=spawned) as spawn:
            result = coordinator.dispatch_once()
        self.assertEqual(result['company_ref'], 'company:B')
        self.assertIn('memo pause is unproved', result['rejected']['company:A'])
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.kwargs['company_ref'], 'company:B')
        self.assertEqual(request.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
