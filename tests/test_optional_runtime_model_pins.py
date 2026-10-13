"""Absent optional consumers do not block durable shared model selections."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core import host_research_reconcile as reconcile
from dalton_core import host_research_settings as settings
from dalton_core import model_selection as selection
from tests import test_model_selection as model_fixture
from tests import test_host_research_settings as host_fixture


class OptionalRuntimePinsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = model_fixture.StateDirectoryCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.service = self.root / 'service.json'
        binding_path = patch.object(selection, 'service_config_path', return_value=self.service)
        binding_path.start()
        self.addCleanup(binding_path.stop)

    def save_service(self, body):
        self.service.write_text(json.dumps(body))

    def test_absent_and_null_intent_persist_owner_selection_without_inventing_consumer(self):
        for body in ({}, {'control': {'config': {}}},
                     {'control': {'config': {'intent_composer': None}}},
                     {'control': {'enabled': False}},
                     {'control': {'enabled': False, 'config': {}}}):
            with self.subTest(body=body):
                self.save_service(body)
                before = self.service.read_bytes()
                result = selection.set_model_selection(self.root, purpose='human_intent',
                    mode='explicit', chain=['profile:gpt-6-astra'], now=model_fixture.NOW)
                self.assertFalse(result['requires_restart'])
                self.assertEqual(before, self.service.read_bytes())
                policy = self.fixture.router.get_policy(self.fixture.stored()['routing_policy_ref'])
                self.assertEqual(['profile:gpt-6-astra'], policy['purpose_overrides']['human_intent']['chain'])

    def test_missing_other_optional_consumers_allow_tier_save(self):
        self.save_service({'control': {'config': {}}, 'agenda': {'enabled': False}})
        selection.set_tier_selection(self.root, tier='brain', mode='explicit',
            chain=['profile:gpt-6-astra'], now=model_fixture.NOW)
        self.assertEqual(['profile:gpt-6-astra'], self.fixture.router.get_policy(
            self.fixture.stored()['routing_policy_ref'])['fallback_chains']['tiers']['brain'])

    def test_present_malformed_intent_refuses_purpose_tier_and_binding_scan_before_writes(self):
        for composer in ({}, {'routing_policy_ref': 1}, 'broken'):
            with self.subTest(composer=composer):
                self.save_service({'control': {'config': {'intent_composer': composer}}})
                before = self.fixture.config_path.read_bytes()
                rows = self.fixture.router.connection.execute('SELECT policy_json FROM model_routing_policy_versions').fetchall()
                for operation in (
                    lambda: selection.set_model_selection(self.root, purpose='human_intent',
                        mode='explicit', chain=['profile:gpt-6-astra'], now=model_fixture.NOW),
                    lambda: selection.set_tier_selection(self.root, tier=selection.tier_for('human_intent'),
                        mode='explicit', chain=['profile:gpt-6-astra'], now=model_fixture.NOW),
                    lambda: reconcile._consumer_policy_bindings(SimpleNamespace(state_dir=self.root))):
                    with self.assertRaises(selection.ModelSelectionError):
                        operation()
                self.assertEqual(before, self.fixture.config_path.read_bytes())
                self.assertEqual(rows, self.fixture.router.connection.execute(
                    'SELECT policy_json FROM model_routing_policy_versions').fetchall())

    def test_present_malformed_service_and_other_pin_are_not_optional(self):
        for body in ([], {'control': {'config': 'broken'}},
                     {'bounded_planner': {'config': {}}}):
            with self.subTest(body=body):
                self.save_service(body)
                with self.assertRaises(selection.ModelSelectionError):
                    reconcile._consumer_policy_bindings(SimpleNamespace(state_dir=self.root))


class SharedOptionalPurposeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = host_fixture.HostSettingsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_shared_three_revisions_reconcile_real_writer_without_optional_composer(self):
        self._three_revisions(self.fixture)

    def test_new_empty_workspace_real_writer_applies_all_three_without_mission(self):
        f = host_fixture.HostSettingsTests()
        with patch.object(host_fixture.HostSettingsTests, 'publish', return_value=None):
            f.setUp()
        self.addCleanup(f.doCleanups)
        f.writer()  # The installed empty writer initializes the mission schema.
        self.assertIsNone(settings.current_mission(f.state))
        self._three_revisions(f)
        self.assertEqual(0, f.store.connection.execute('SELECT COUNT(*) FROM coverage_mission_versions').fetchone()[0])
        self.assertIsNone(settings.current_mission(f.state))

    def _three_revisions(self, f):
        original_service = json.loads(f.workspace.config_path.read_text())
        desired = f.seed()
        for purpose in ('human_intent', 'quality', 'research_localization'):
            desired = settings.save(f.manager, actor_ref='human:owner',
                expected_hash=desired['content_hash'], source_state_dir=f.state,
                selection={'purpose': purpose, 'mode': 'explicit',
                           'chain': ['profile:gpt-6-astra']})['desired']
        before_desired = settings.load(f.manager)
        server = f.writer()
        reconcile.reconcile_writer(server)
        self.assertEqual('applied', reconcile.status(f.manager)['status'])
        self.assertEqual(before_desired, settings.load(f.manager))
        service = json.loads(f.workspace.config_path.read_text())
        self.assertEqual(original_service['control'], service['control'])
        policy = f.router.get_policy(json.loads((f.state/'research-planner-model-config.json').read_text())['routing_policy_ref'])
        for purpose in ('human_intent', 'quality', 'research_localization'):
            self.assertEqual(['profile:gpt-6-astra'], policy['purpose_overrides'][purpose]['chain'])
        snapshots = [p.read_bytes() for p in (f.workspace.config_path, f.state/'research-planner-model-config.json')]
        reconcile.reconcile_writer(server)
        self.assertEqual(snapshots, [p.read_bytes() for p in (f.workspace.config_path, f.state/'research-planner-model-config.json')])
        reconcile.assert_reconciled(f.state)

    def test_shared_malformed_consumer_remains_pending_and_refuses(self):
        f = self.fixture
        desired = f.seed()
        settings.save(f.manager, actor_ref='human:owner', expected_hash=desired['content_hash'],
            source_state_dir=f.state, selection={'purpose':'human_intent','mode':'explicit','chain':['profile:gpt-6-astra']})
        service = json.loads(f.workspace.config_path.read_text())
        service['control']['config']['intent_composer'] = {}
        f.workspace.config_path.write_text(json.dumps(service))
        before = (f.state/'research-planner-model-config.json').read_bytes()
        with self.assertRaises(selection.ModelSelectionError):
            reconcile.reconcile_writer(f.writer())
        self.assertEqual(before, (f.state/'research-planner-model-config.json').read_bytes())
        self.assertEqual('pending', reconcile.status(f.manager)['status'])
