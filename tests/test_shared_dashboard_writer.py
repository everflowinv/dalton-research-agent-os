"""Shared dashboard subjects pass the actual writer authorization and storage path."""
import json
import unittest
from pathlib import Path

from dalton_core.shared_access_identity import SharedLogin, shared_actor
from dalton_core.writer_protocol import Request
from dalton_core.writer_server import WriterServer, Principal, DASHBOARD_CONTROL_OPERATIONS
from dalton_core.store import DaltonStore, content_hash


class SharedDashboardWriterTests(unittest.TestCase):
    def server(self, state, workspace=None, store=None):
        server = object.__new__(WriterServer)
        server._workspace = workspace
        server.db_path = state / 'core.sqlite'
        server._store = store
        server._agenda = None
        server._token_config_path = None
        principal = Principal('dashboard-control', 'test-token', DASHBOARD_CONTROL_OPERATIONS,
                              actor_ref='bridge:tailscale-dashboard')
        server._by_token = {'test-token': principal}
        self.actor = shared_actor(SharedLogin('shared-test'))
        config = workspace.config_path if workspace else state.parents[1] / 'config' / 'service.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({'control': {'config': {'shared_gateway': {
            'secret_file': str(state / 'gateway.key'), 'public_origin': 'https://test.example',
            'principal': 'shared-test'}}}}))
        return server

    def call(self, server, operation, params):
        return server._handle(Request('0.1', 'test-request', operation, params, 'test-token'))

    def test_shared_first_goal_is_formally_published_under_its_own_actor(self):
        from tests.test_workspace_mission_setup import WorkspaceFirstMissionTests
        from tests.p9a_fixtures import bootstrap_method_authorities, constitution_method
        from dalton_core.workspace_mission_setup import draft_first_mission
        fixture = WorkspaceFirstMissionTests('test_production_preparer_publishes_real_authority_chain_and_active_mission')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        state = fixture.workspace.state_dir
        state.mkdir(parents=True, exist_ok=True)
        store = DaltonStore(state / 'core.sqlite'); self.addCleanup(store.close)
        seeded = bootstrap_method_authorities(store)
        body = {k: v for k, v in fixture.foundation.items() if k != 'content_hash'}
        method = constitution_method()
        drivers = {'drivers': [{'driver_ref': 'driver:volume', 'label': 'Volume',
                    'mechanism': 'Demand changes units sold.', 'metric_refs': ['metric:revenue']}],
                   'metric_specs': [{'metric_ref': 'metric:revenue', 'label': 'Revenue',
                    'definition': 'Reported revenue', 'unit': 'USD', 'periodicity': 'quarterly',
                    'preferred_source_refs': ['source:sec-edgar'], 'verification_kind': 'numeric',
                    'caveats': []}],
                   'thesis_templates': [{'template_ref': 'template:demand', 'statement': 'Demand changes revenue.',
                    'mechanism': 'Volume', 'driver_refs': ['driver:volume'],
                    'implied_expectation': 'Revenue follows demand.', 'falsifier_refs': ['falsifier:revenue']}]}
        body['methods'] = {'playbook': {'binding': {'ref': seeded['playbook']['id'],
                          'hash': seeded['playbook']['content_hash']}},
                          'driver_pack_template': {'value': drivers, 'content_hash': content_hash(drivers)},
                          'constitution_method': {'value': method, 'content_hash': content_hash(method)}}
        foundation = {**body, 'content_hash': content_hash(body)}
        (state / 'research-foundation.json').write_text(json.dumps(foundation))
        server = self.server(state, fixture.workspace, store)
        server._sec_company_resolver_identity = None
        server._load_first_mission_discovery_plans = lambda: None
        draft = draft_first_mission(fixture.workspace, goal='Research $ASML', industry='chips',
                                    method_foundation=foundation)
        params = {'workspace_manifest': json.loads(fixture.workspace.manifest_path.read_text()),
                  'method_foundation': foundation, 'proposal': draft,
                  'proposal_hash': draft['content_hash'], 'actor_ref': self.actor}
        mission = self.call(server, 'publish_first_workspace_mission', params)
        self.assertEqual(self.actor, mission['actor_ref'])
        from dalton_core.coverage_mission import CoverageMissionAuthority
        self.assertEqual(mission['content_hash'], CoverageMissionAuthority(store).active_mission(
            mission['mission_ref'])['content_hash'])
        params['actor_ref'] = shared_actor(SharedLogin('foreign'))
        with self.assertRaises(PermissionError):
            self.call(server, 'publish_first_workspace_mission', params)

    def test_shared_feedback_uses_exact_subject_and_honest_source_in_real_ledger(self):
        from tests.test_agenda_control import AgendaControlTests
        fixture = AgendaControlTests()
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        state = Path(fixture.temp.name) / 'installed' / 'state' / 'dalton-core'
        server = self.server(state, store=fixture.store)
        server._agenda = fixture.agenda
        params = {'decision_id': fixture.decision['id'], 'verdict': 'agree', 'notes': '',
                  'feedback_id': 'agenda-feedback:shared-test', 'idempotency_key': 'shared-test',
                  'subject_ref': self.actor, 'source': 'shared_dashboard',
                  'source_event_ref': 'dashboard-feedback:shared-test'}
        result = self.call(server, 'record_agenda_feedback', params)
        self.assertEqual('fresh', result['status'])
        row = fixture.store.connection.execute('SELECT subject_ref,source,actor_ref FROM agenda_feedback WHERE feedback_id=?',
                                             ('agenda-feedback:shared-test',)).fetchone()
        record = dict(zip(('subject_ref', 'source', 'actor_ref'), row))
        self.assertEqual(self.actor, record['subject_ref'])
        self.assertEqual('shared_dashboard', record['source'])
        self.assertEqual('bridge:tailscale-dashboard', record['actor_ref'])
        params['subject_ref'] = shared_actor(SharedLogin('foreign'))
        with self.assertRaises(PermissionError):
            self.call(server, 'record_agenda_feedback', params)
        params['subject_ref'] = self.actor; params['source'] = 'tailscale_dashboard'
        with self.assertRaises(PermissionError):
            self.call(server, 'record_agenda_feedback', params)

    def test_weekly_scope_accepts_only_exact_configured_shared_subject(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            server = self.server(Path(directory) / 'host' / 'state' / 'dalton-core')
            principal = server._by_token['test-token']
            params = {'subject_ref': self.actor}
            authorized = server._authorized_params(principal, 'record_weekly_brief_feedback', params)
            self.assertEqual(self.actor, authorized['subject_ref'])
            with self.assertRaises(PermissionError):
                server._authorized_params(principal, 'record_weekly_brief_feedback',
                                          {'subject_ref': shared_actor(SharedLogin('foreign'))})

if __name__ == '__main__':
    unittest.main()
