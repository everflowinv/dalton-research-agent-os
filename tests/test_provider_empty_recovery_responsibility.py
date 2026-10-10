"""Execution failures stay visible without becoming duplicate analyst decisions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.needs_human import collect, held_lanes


class ProviderEmptyRecoveryResponsibilityTests(unittest.TestCase):
    EMPTY = 'unproved_send_failed_after_automatic_retry:HOST_EMPTY_COMPLETION:provider_returned_nothing:profile=model-profile-version:synthetic:1'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.path = self.state / 'mission-document-research-runs' / 'holds.json'
        self.path.parent.mkdir()

    def write(self, *reasons):
        holds = {'mission-document-research-admission:' + str(i).zfill(32): {
            'admission_hash': 'a'*64, 'ticket_ref': None, 'reason': reason,
            'disposition': 'recovery_required', 'retry_at': None,
        } for i, reason in enumerate(reasons)}
        self.path.write_text(json.dumps({'schema_version': '0.1', 'holds': holds}))
        return self.path.read_bytes()

    def test_empty_reply_is_admin_and_summary_adds_no_decision_or_writes(self):
        original = self.write(self.EMPTY)
        result = collect(state_dir=self.state)
        cards = [x for x in result['items'] if x['kind'] == 'controlled_recovery']
        self.assertEqual(len(cards), 2)
        self.assertEqual(result['count'], 0)
        self.assertTrue(all(x['actionable'] is False for x in cards))
        detail = next(c for c in cards if c['detail'].get('admission_ref'))
        self.assertEqual(detail['responsibility'], 'administrator')
        self.assertIn('模型未返回内容', detail['title'])
        for card in cards:
            for key in ('title', 'why_blocked', 'action', 'consequence'):
                self.assertNotIn('等你决定', card[key])
                self.assertNotIn('你授权', card[key])
        self.assertIn('没有返回可用内容', detail['why_blocked'])
        self.assertEqual(detail['detail']['reason'], self.EMPTY)
        self.assertEqual([d['door'] for d in detail['detail']['owner_doors']], ['unproved', 'withdraw'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_send_keeps_its_real_detail_decision_only(self):
        self.write('unproved_send_failed_after_automatic_retry:unclassified_transport_failure')
        cards = held_lanes(None, state_dir=self.state)
        self.assertTrue(cards[0]['actionable'])
        self.assertFalse(cards[1]['actionable'])

    def test_empty_reply_does_not_absorb_material_lifetime_budget(self):
        self.write(self.EMPTY)
        budget = {'kind': 'material_budget_exceeded', 'ref': 'material-budget:synthetic',
                  'actionable': True, 'detail': {'scope': 'request', 'approve_usd': '0.10', 'approve_calls': 3}}
        # The actual owner counter must include the request's existing cap,
        # independently of an unrelated provider execution failure.
        from dalton_core.needs_human import _item
        budget = _item('material_budget_exceeded', ref=budget['ref'], at='', title='额度用完',
                       why='累计额度', action='决定是否改变额度', consequence='资料仍缺',
                       where='研究目标', detail=budget['detail'])
        with patch('dalton_core.needs_human.material_acquisition_cards', return_value=[budget]):
            result = collect(state_dir=self.state)
        cards = result['items']
        self.assertEqual(result['count'], 1)
        self.assertEqual([c['ref'] for c in cards if c['actionable']], [budget['ref']])
        self.assertEqual(budget['detail']['approve_usd'], '0.10')

    def test_unrendered_unknown_hold_keeps_summary_actionable(self):
        self.write('unproved_send_failed_after_automatic_retry:unclassified_transport_failure')
        with patch('dalton_core.needs_human._escalated_hold_item', side_effect=RuntimeError('bad read')):
            cards = held_lanes(None, state_dir=self.state)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]['actionable'])
        self.assertEqual(len(cards[0]['detail']['unrendered']), 1)

    def test_code_without_exhausted_empty_reply_cause_is_not_reclassified(self):
        self.write('send_state_unproved:HOST_EMPTY_COMPLETION',
                   'unproved_send_failed_after_automatic_retry:HOST_EMPTY_COMPLETION:unknown_send')
        cards = held_lanes(None, state_dir=self.state)
        self.assertNotEqual(cards[0].get('responsibility'), 'administrator')
        self.assertTrue(cards[1]['actionable'])
