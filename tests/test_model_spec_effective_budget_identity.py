"""An authorized shared-limit save reaches the lane, ticket and immutable Work."""
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

from dalton_core.call_budget import freeze_shared_budget_policy
from dalton_core.company_model_cli import model_spec_call_budget, run_model_spec
from dalton_core.company_model_launcher import CompanyModelSpecLauncher
from dalton_core.cockpit_model import CockpitModel, CockpitModelError
from dalton_core.lane_failure_class import LaneFailureBudget
from dalton_core.lane_permission_control import current_permission, record_controlled_failure
from dalton_core.mission_model_spec_lane import business_key
from dalton_core.store import content_hash
from dalton_core.workspace_manager import _write, set_shared_model_budget
from tests import test_workspace_manager as manager_fixture
from tests import test_global_model_budget_limits as shared_fixture
from tests import test_cockpit_model_fallback as chain_fixture
from tests import test_model_output_contract_hold as hold_fixture
from tests import test_company_model_cli as cli_fixture
from tests import test_mission_model_spec_lane as lane_fixture


class ModelSpecEffectiveBudgetIdentityTests(unittest.TestCase):
    def setUp(self):
        self.host = manager_fixture.WorkspaceManagerTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.shared = self.host.root / 'shared-limits.json'
        policy = shared_fixture.legacy_policy()
        self.shared.write_text(json.dumps(policy))
        self.shared.chmod(0o600)
        self.host.config['shared_call_budget_policy_path'] = str(self.shared)
        _write(self.host.path, self.host.config)
        self.policy = policy
        self.save(16384)
        self.config = {
            'routing_policy_ref': 'routing-policy:synthetic:1',
            'credential_slot_refs': ['credential-slot:synthetic'],
            'model_router_db': str(self.host.root / 'router.sqlite'),
            'broker_socket': str(self.host.root / 'broker.sock'),
            'broker_auth_key': str(self.host.root / 'broker.key'),
            'broker_client_id': 'client:dalton-core', 'expected_agent_id': 'chem',
            'budget_db': str(self.host.root / 'budget.sqlite'),
            'budget_policy_ref': 'budget-policy:synthetic:1',
            'shared_call_budget_policy_path': str(self.shared),
        }
        self.config_path = self.host.root / 'model.json'
        self.config_path.write_text(json.dumps(self.config))
        # No launcher constructor: these checks do not create a child or state.
        self.launcher = object.__new__(CompanyModelSpecLauncher)
        self.launcher.model_config_path = self.config_path
        self.launcher.scheduler_db = None
        self.launcher.python_executable = sys.executable
        self.launcher.state_dir = self.host.root
        self.key_args = dict(company_ref='company:sec-cik:0000000001',
                             state_hash='a'*64, task_hash='b'*64,
                             repair_policy_hash='c'*64, validation_hash='d'*64)
        self.key = business_key(**self.key_args)
        self.mission = {'id':'coverage-mission-version:synthetic:1'}

    def save(self, output, purpose='model_spec'):
        self.policy = set_shared_model_budget(self.host.path, 'owner@example.com', purpose,
            {'max_output_tokens': output}, self.policy['content_hash'])['policy']

    def ticket(self):
        return self.launcher._ticket_digest(**self.key_args,
            call_budget_hash=self.launcher.call_budget_hash())

    def test_shared_save_changes_permission_and_ticket_not_business_input_or_usd(self):
        budget = LaneFailureBudget('mission_model_spec')
        config_before = self.config_path.read_bytes()
        old_budget = model_spec_call_budget(self.config)
        old_ticket = self.ticket()
        old_permission = current_permission(budget, self.key, self.mission, self.launcher)
        record_controlled_failure(budget, self.key, self.mission, self.launcher,
            status='model_unavailable', reason='PROVIDER_BUDGET_EXCEEDED')
        self.assertIsNotNone(budget.blocked(old_permission))
        self.assertEqual(current_permission(budget, self.key, self.mission, self.launcher), old_permission)
        self.save(32768)
        new_permission = current_permission(budget, self.key, self.mission, self.launcher)
        self.assertNotEqual(new_permission, old_permission)
        self.assertIsNone(budget.blocked(new_permission))
        self.assertNotEqual(self.ticket(), old_ticket)
        self.assertEqual(self.ticket(), self.ticket())
        self.assertEqual(model_spec_call_budget(self.config), {**old_budget,'max_output_tokens':32768})
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(business_key(**self.key_args), self.key)

    def test_real_lane_uses_shared_budget_change_once_without_a_task_upgrade(self):
        fake = lane_fixture.FakeLauncher()
        fake.model_config_path = self.config_path
        fake.permission_control_projection = self.launcher.permission_control_projection
        lane = lane_fixture.MissionModelSpecLaneCoordinator(
            missions=lane_fixture.FakeMissions([lane_fixture.ACN]), launcher=fake,
            mission=lambda: {"id": "coverage-mission-version:synthetic:1",
                             "universe": [{"company_ref": lane_fixture.ACN, "ticker": "ACN"}]})
        first=lane.dispatch_once()
        failure={"spec_status":"model_unavailable", "failure_codes":["PROVIDER_BUDGET_EXCEEDED"],
                 "failure_reason":"provider max_output_tokens telemetry exceeds WorkOrder budget"}
        fake.finish(first["ticket_ref"],summary=failure)
        self.assertEqual(lane.dispatch_once()["status"],"held")
        self.save(32768)
        second=lane.dispatch_once()
        self.assertEqual(second["status"],"launched")
        self.assertEqual(first["state_hash"],second["state_hash"])
        self.assertEqual(fake.started[0]["task_hash"],fake.started[1]["task_hash"])
        fake.finish(second["ticket_ref"],summary=failure)
        for _ in range(3):
            self.assertEqual(lane.dispatch_once()["status"],"held")
        self.assertEqual(len(fake.started),2)

    def test_unrelated_purpose_and_same_effective_limits_do_not_change_identity(self):
        before = (self.launcher.permission_control_projection(), self.ticket())
        self.save(32768, 'document_extraction')
        self.assertEqual((self.launcher.permission_control_projection(), self.ticket()), before)
        self.save(16384)
        self.assertEqual((self.launcher.permission_control_projection(), self.ticket()), before)

    def test_content_and_unknown_or_pending_budget_business_holds_are_not_cleared(self):
        for reason in ['financial_statement_structure is invalid: sum formula roles do not match',
                       'POST_SEND_UNKNOWN', 'pending settlement is unverified']:
            with self.subTest(reason=reason):
                budget=LaneFailureBudget('mission_model_spec')
                for _ in range(3):
                    budget.record(self.key, status='refused', reason=reason)
                prior = budget.blocked(self.key)
                self.assertIsNotNone(prior)
                self.save(32768)
                current_permission(budget, self.key, self.mission, self.launcher)
                self.assertEqual(budget.blocked(self.key).as_wire(), prior.as_wire())
                self.save(16384)

    def test_command_and_child_reject_budget_drift_before_any_model_call(self):
        frozen_hash = self.launcher.call_budget_hash()
        command = self.launcher._command(ticket_dir=self.host.root,
            company_ref=self.key_args['company_ref'], expected_state_hash='a'*64,
            expected_task_hash='b'*64, expected_repair_policy_hash='c'*64,
            expected_financial_validation_contract_hash='d'*64,
            expected_call_budget_hash=frozen_hash)
        self.assertEqual(command[command.index('--expected-call-budget-hash')+1], frozen_hash)
        fixture=cli_fixture.ChooseCompanyTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        config=fixture.state_dir/'frozen-budget.json'
        config.write_text(json.dumps(self.config))
        self.save(32768)
        with patch('dalton_core.company_model_cli.CockpitModel') as model:
            result=run_model_spec(state_dir=fixture.state_dir, model_config_path=config,
                summary_dir=fixture.state_dir/'stale-budget',scheduler_db=None,
                company_ref=cli_fixture.ACN,expected_call_budget_hash=frozen_hash)
        self.assertEqual(result['spec_status'],'stale_input')
        model.assert_not_called()

    def test_old_batch_snapshot_keeps_its_budget_and_constructor_matches_pure_resolver(self):
        frozen=freeze_shared_budget_policy(self.config)
        old=model_spec_call_budget(frozen)
        self.save(32768)
        model=CockpitModel(frozen,scheduler_db=self.host.root/'unused.sqlite',
            max_input_tokens=120000,max_output_tokens=3000,max_cost_usd=1,timeout_seconds=300)
        self.assertEqual(model.budget_for('model_spec'),old)
        self.assertEqual(model_spec_call_budget(self.config)['max_output_tokens'],32768)


class NewBudgetWorkTests(unittest.TestCase):
    def test_new_configuration_gets_one_normal_work_and_preserves_old_paid_terminal(self):
        fixture=chain_fixture.CockpitChainTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        class Adapter(hold_fixture.PaidOverrun):
            def execute(self,work,route,profile):
                if work.budget['max_output_tokens'] <= 6000:
                    return super().execute(work,route,profile)
                return chain_fixture.ChainAdapter.execute(self,work,route,profile)
        adapter=Adapter()
        old=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=6000)
        with self.assertRaises(CockpitModelError):
            old.call(purpose='model_spec',request_id='same-disclosure',prompt='synthetic same company',mission=fixture.mission)
        with sqlite3.connect(fixture.root/'scheduler.sqlite') as db:
            old_work=db.execute('SELECT * FROM scheduler_work_orders').fetchall()
            old_formal=db.execute('SELECT * FROM scheduler_formal_results').fetchall()
        with sqlite3.connect(fixture.root/'budget.sqlite') as db:
            old_fees=db.execute('SELECT * FROM thesis_impact_day_settlements').fetchall()
        new=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=7000)
        self.assertFalse(new.call(purpose='model_spec',request_id='same-disclosure',prompt='synthetic same company',mission=fixture.mission)['replayed'])
        for _ in range(3):
            self.assertTrue(new.call(purpose='model_spec',request_id='same-disclosure',prompt='synthetic same company',mission=fixture.mission)['replayed'])
        self.assertEqual(len(adapter.served),2)
        with sqlite3.connect(fixture.root/'scheduler.sqlite') as db:
            self.assertEqual(db.execute('SELECT * FROM scheduler_work_orders WHERE work_order_id=?',(old_work[0][0],)).fetchall(),old_work)
            self.assertEqual(db.execute('SELECT * FROM scheduler_formal_results WHERE work_order_id=?',(old_formal[0][1],)).fetchall(),old_formal)
            self.assertEqual(db.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)
        with sqlite3.connect(fixture.root/'budget.sqlite') as db:
            self.assertEqual(db.execute('SELECT * FROM thesis_impact_day_settlements WHERE settlement_id=?',(old_fees[0][0],)).fetchall(),old_fees)
