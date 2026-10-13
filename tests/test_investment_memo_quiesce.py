"""Real formal replay plus exact deployment-control boundary regressions."""
import hashlib
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.investment_memo_draft import GROUPS, draft_group, verify_memo
from dalton_core.investment_memo_quiesce import (
    RunQuiesce, MemoQuiesced, MemoQuiesceUnproved, request_quiesce,
    settled_call, quiesced_summary_matches,
)
from dalton_core.investment_memo_launcher import InvestmentMemoLauncher
from dalton_core.store import content_hash
from tests import test_cockpit_model_fallback as fixtures
from tests import test_investment_memo_draft as memo_fixtures


class MemoPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CockpitChainTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.directory = self.root / 'memo'; self.directory.mkdir()
        self.producer = self.root / 'producer.json'; self.verifier = self.root / 'verifier.json'
        self.producer.write_text('{}'); self.verifier.write_text('{}')
        self.ticket = {
            'id': 'investment-memo-run:' + 'a'*24, 'pid': os.getpid(),
            'started_at': '2026-10-11T07:00:00+00:00', 'command': ['python', '-m', 'dalton_core.investment_memo_cli'],
            'model_config_path': str(self.producer), 'model_config_fingerprint': hashlib.sha256(b'{}').hexdigest(),
            'verifier_model_config_path': str(self.verifier), 'verifier_model_config_fingerprint': hashlib.sha256(b'{}').hexdigest(),
            'status': 'running', 'exit_code': None, 'completed_at': None,
        }
        (self.directory / 'ticket.json').write_text(json.dumps(self.ticket))
        self.q = self.quiesce()
        self.adapter = JsonAdapter()
        self.model = self.fixture._model(self.adapter, policy_version_ref=self.fixture.chain_policy)
        self.material = [{'ref': 'claim-version:1', 'hash': 'c'*64, 'kind': 'claim', 'text': 'source'}]

    def quiesce(self, **kwargs):
        return RunQuiesce(self.directory, self.producer, self.verifier,
                          producer_bytes=kwargs.get('producer_bytes', b'{}'), verifier_bytes=b'{}')

    def request(self):
        with patch('dalton_core.lane_child_launcher.ticket_identity', return_value=True):
            return request_quiesce(self.directory, expected_ticket_hash=content_hash(self.ticket))

    def kwargs(self, n=0):
        group, sections, questions = GROUPS[n]
        return dict(group=group, section_titles=[memo_fixtures.TITLES[i] for i in sections],
                    questions=[{'question_ref': f'memo_q{i+1:02d}', 'question': memo_fixtures.QUESTIONS[i]}
                               for i in questions], material=self.material,
                    company={'company_ref': 'company:fixture'}, mission=self.fixture.mission)

    def call(self, n=0):
        return self.model.call(purpose='investment_memo', request_id=f'memo-proof-{n}',
                               prompt='Group: identity_background', mission=self.fixture.mission)

    def test_exact_request_idempotent_and_live_identity_required(self):
        first = self.request(); self.assertEqual(self.request(), first)
        self.assertEqual((self.directory/'quiesce-request.json').stat().st_mode & 0o777, 0o600)
        with patch('dalton_core.lane_child_launcher.ticket_identity', return_value=False):
            with self.assertRaisesRegex(ValueError, 'identity'):
                request_quiesce(self.directory, expected_ticket_hash=content_hash(self.ticket))
        with patch('dalton_core.lane_child_launcher.ticket_identity', return_value=True):
            with self.assertRaisesRegex(ValueError, 'changed'):
                request_quiesce(self.directory, expected_ticket_hash='0'*64)

    def test_pid_start_and_loaded_config_drift_refuse_safe_pause(self):
        self.request()
        with self.assertRaises(MemoQuiesceUnproved):
            self.quiesce(producer_bytes=b'{"changed":true}').checkpoint()
        self.ticket['started_at'] = '2026-10-11T07:00:01+00:00'
        (self.directory/'ticket.json').write_text(json.dumps(self.ticket))
        with self.assertRaises(MemoQuiesceUnproved): self.q.checkpoint()
        self.ticket['started_at'] = '2026-10-11T07:00:00+00:00'; self.ticket['pid'] += 1
        (self.directory/'ticket.json').write_text(json.dumps(self.ticket))
        with self.assertRaises(MemoQuiesceUnproved): self.q.checkpoint()

    def test_no_provider_after_request_already_at_boundary(self):
        self.request()
        with self.assertRaises(MemoQuiesced) as caught:
            draft_group(self.model, **self.kwargs(), quiesce=self.q)
        self.assertEqual(caught.exception.completed, [])
        self.assertEqual(self.adapter.served, [])

    def test_settled_first_group_pauses_then_same_formal_is_free_replayed(self):
        original = self.adapter.execute
        def finish_then_request(work, route, profile):
            answer = original(work, route, profile)
            self.request()  # Arrives while the provider's group is in flight.
            return answer
        self.adapter.execute = finish_then_request
        with self.assertRaises(MemoQuiesced) as stopped:
            draft_group(self.model, **self.kwargs(), quiesce=self.q)
        proof = stopped.exception.completed[0]
        self.assertEqual(len(self.adapter.served), 1)
        self.assertTrue(proof['settlements'])
        resumed = draft_group(self.model, **self.kwargs())
        self.assertEqual(resumed['status'], 'drafted', resumed)
        self.assertEqual(resumed['model']['work_order_ref'], proof['work_order_ref'])
        self.assertEqual(resumed['model']['cost_micros'], 0)
        self.assertEqual(len(self.adapter.served), 1)
        # The next not-yet-produced group is one new ordinary call.
        self.adapter.execute = original
        self.assertEqual(draft_group(self.model, **self.kwargs(1))['status'], 'drafted')
        self.assertEqual(len(self.adapter.served), 2)

    def test_fallback_uses_the_settled_first_link_reservation(self):
        self.adapter.script['profile:gpt-6-astra'] = fixtures.BrokerDefinitelyNotSent('offline fixture')
        call = self.call()
        proof = settled_call(self.model, call, self.fixture.mission, purpose='investment_memo')
        self.assertEqual(self.adapter.served[:2], ['profile:gpt-6-astra', 'profile:claude-fable-5-1'])
        self.assertTrue(proof['settlements'])

    def test_unsettled_or_wrong_formal_never_becomes_resumable(self):
        call = self.call()
        self.q.returned(self.model, call, self.fixture.mission, purpose='investment_memo')
        self.request()
        # A real ledger with its settlement missing is not complete proof.
        import sqlite3
        connection = sqlite3.connect(self.root/'budget.sqlite')
        connection.execute('DROP TRIGGER IF EXISTS thesis_impact_day_settlements_no_delete')
        # Append-only authorizer is connection local; this deliberate corrupt
        # private fixture is never a production repair or refund.
        names = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='thesis_impact_day_settlements'").fetchall()
        for (name,) in names: connection.execute('DROP TRIGGER "'+name+'"')
        connection.execute('DELETE FROM thesis_impact_day_settlements'); connection.commit(); connection.close()
        with self.assertRaisesRegex(MemoQuiesceUnproved, 'unsettled'):
            self.q.checkpoint()

    def test_wrong_mission_and_tampered_result_ref_rejected(self):
        call = self.call()
        wrong = {**self.fixture.mission, 'content_hash': 'f'*64}
        with self.assertRaisesRegex(ValueError, 'binding'):
            settled_call(self.model, call, wrong, purpose='investment_memo')
        with self.assertRaisesRegex(ValueError, 'binding'):
            settled_call(self.model, {**call, 'result_envelope_ref': 'result:foreign'},
                         self.fixture.mission, purpose='investment_memo')

    def test_request_arriving_between_groups_blocks_next_call(self):
        first = draft_group(self.model, **self.kwargs(), quiesce=self.q)
        self.assertEqual(first['status'], 'drafted')
        self.request()
        with self.assertRaises(MemoQuiesced) as stopped:
            draft_group(self.model, **self.kwargs(1), quiesce=self.q)
        self.assertEqual(len(stopped.exception.completed), 1)
        self.assertEqual(len(self.adapter.served), 1)

    def test_repair_and_verifier_are_also_guarded_before_dispatch(self):
        self.request()
        with self.assertRaises(MemoQuiesced):
            draft_group(self.model, **self.kwargs(), repair={'reason': 'bad', 'refs': []}, quiesce=self.q)
        with self.assertRaises(MemoQuiesced):
            verify_memo(self.model, sections=[], questions=[], material=[], material_hash='a'*64,
                        mission=self.fixture.mission, producer_route_decision_refs=['route:old'], quiesce=self.q)
        self.assertEqual(self.adapter.served, [])

    def test_pause_proof_does_not_depend_on_ticket_terminal_projection(self):
        req = self.request()
        summary = {'status': 'quiesced', 'resumable': True, 'stop_reason': 'cooperative_quiesce', 'quiesce': req}
        completed = {**self.ticket, 'status': 'succeeded', 'completed_at': 'later', 'exit_code': 0,
                     'summary': summary}
        self.assertTrue(quiesced_summary_matches(summary, completed))
        completed['pid'] += 1
        self.assertFalse(quiesced_summary_matches(summary, completed))

    def test_actual_cli_run_catches_pause_without_reaching_verifier_or_publication(self):
        from dalton_core.investment_memo_cli import run
        frozen = {'status': 'ready', 'mission': self.fixture.mission,
                  'playbook': {'id': 'pb:1', 'content_hash': 'b'*64,
                               'deliverable_templates': {'investment_memo': memo_fixtures.TITLES},
                               'key_questions': memo_fixtures.QUESTIONS},
                  'company': {'company_ref': 'company:fixture'},
                  'material': self.material, 'input_bindings': []}
        execute = self.adapter.execute
        def arriving(work, route, profile):
            answer = execute(work, route, profile)
            self.request()
            return answer
        self.adapter.execute = arriving
        verifier = memo_fixtures.FakeModel([], 'verifier')
        with patch('dalton_core.investment_memo_cli.DaltonStore', return_value=SimpleNamespace()), \
                patch('dalton_core.investment_memo_cli.collect_frozen_input', return_value=frozen), \
                patch('dalton_core.investment_memo_cli.MissionDeliverableAuthority') as publish:
            result = run(state_dir=self.root, model_config_path=self.producer,
                         verifier_model_config_path=self.verifier, summary_dir=self.directory,
                         model_factory=lambda: self.model, verifier_model_factory=lambda: verifier)
        self.assertEqual(result['status'], 'quiesced', result)
        self.assertTrue(result['resumable'])
        self.assertEqual(result['frozen_input_hash'], content_hash(frozen))
        self.assertEqual(result['budget']['max_repairs'], 1)
        self.assertEqual(len(result['completed_calls']), 1)
        self.assertEqual(verifier.calls, [])
        publish.assert_not_called()
        self.assertNotIn('text', result['completed_calls'][0])

    def test_coordinator_does_not_classify_verified_pause_as_content_or_budget_failure(self):
        from dalton_core.mission_investment_memo_lane import MissionInvestmentMemoLaneCoordinator
        from tests import test_investment_memo_lane as lane_fixtures
        launcher = lane_fixtures.MemoFairnessTests.Launcher(self.root)
        frozen = lane_fixtures.MemoFairnessTests.frozen('company:A', 'a'*64)
        coordinator = MissionInvestmentMemoLaneCoordinator(connection=None, launcher=launcher,
            companies=lambda: ['company:A'], frozen_input=lambda _: frozen)
        coordinator.dispatch_once()
        original = {**self.ticket, 'id': coordinator.open_ticket,
                    'signature': launcher.started[0]['signature']}
        proof = {'schema_version': 'investment-memo-quiesce:0.1',
                 'ticket_binding': {k: original[k] for k in (
                     'id','pid','started_at','command','model_config_path','model_config_fingerprint',
                     'verifier_model_config_path','verifier_model_config_fingerprint')},
                 'ticket_hash_at_request': content_hash(original)}
        proof['content_hash'] = content_hash(proof)
        summary = {'status': 'quiesced', 'memo_status': 'quiesced', 'resumable': True,
                   'stop_reason': 'cooperative_quiesce', 'quiesce': proof}
        terminal = {**original, 'status': 'succeeded', 'completed_at': 'later', 'exit_code': 0, 'summary': summary}
        with patch.object(launcher, 'status', return_value=terminal), \
                patch.object(coordinator.budget, 'record_settled') as record, \
                patch.object(coordinator.budget, 'record') as content:
            resumed = coordinator.dispatch_once()
        self.assertEqual(resumed['status'], 'launched', resumed)
        record.assert_not_called(); content.assert_not_called()

    def test_launcher_preserves_parent_and_new_run_has_no_stop_request(self):
        from dalton_core.cockpit_model import verifier_provider_contract_fingerprint
        launcher = InvestmentMemoLauncher(state_dir=self.root, model_config_path=self.producer,
                                          verifier_model_config_path=self.verifier)
        self.addCleanup(launcher.close)
        signature = 'same-input'
        base = hashlib.sha256(f"memo|company:fixture|{signature}|{verifier_provider_contract_fingerprint('investment_memo_verifier')}".encode()).hexdigest()[:24]
        directory = launcher.tickets_dir/base; directory.mkdir(parents=True)
        original = {**self.ticket, 'id': 'investment-memo-run:'+base}
        req = {'schema_version': 'investment-memo-quiesce:0.1', 'ticket_binding': {k: original[k] for k in (
            'id','pid','started_at','command','model_config_path','model_config_fingerprint',
            'verifier_model_config_path','verifier_model_config_fingerprint')},
               'ticket_hash_at_request': content_hash(original)}
        req['content_hash'] = content_hash(req)
        (directory/'quiesce-request.json').write_text(json.dumps(req))
        summary = {'status': 'quiesced', 'resumable': True, 'stop_reason': 'cooperative_quiesce', 'quiesce': req}
        terminal = {**original, 'status': 'succeeded', 'exit_code': 0, 'completed_at': 'later', 'summary': summary}
        with patch.object(launcher, 'status', return_value=terminal), patch.object(launcher, 'spawn', side_effect=lambda **k:k):
            next_run = launcher.start(signature=signature, company_ref='company:fixture')
            again = launcher.start(signature=signature, company_ref='company:fixture')
        self.assertEqual(next_run['digest'], again['digest'])
        self.assertNotEqual(next_run['digest'], base)
        self.assertEqual(next_run['record']['resumed_from'], original['id'])
        self.assertTrue((directory/'quiesce-request.json').exists())
        self.assertFalse((launcher.tickets_dir/next_run['digest']/'quiesce-request.json').exists())


class JsonAdapter(fixtures.ChainAdapter):
    def __init__(self): super().__init__({})
    def execute(self, work, route, profile):
        invocation, result = super().execute(work, route, profile)
        group = next(line[7:] for line in work.question.splitlines() if line.startswith('Group: '))
        _, indexes, question_indexes = next(row for row in GROUPS if row[0] == group)
        payload = memo_fixtures.group_payload([memo_fixtures.TITLES[i] for i in indexes],
            [{'question_ref': f'memo_q{i+1:02d}', 'question': memo_fixtures.QUESTIONS[i]} for i in question_indexes])
        return invocation, replace(result, outputs={'text': json.dumps(payload)})
