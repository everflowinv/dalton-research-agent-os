"""Durable global intent, writer authorities, and future-goal budget defaults."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dalton_core import host_research_settings as settings
from dalton_core import host_research_reconcile as reconcile
from dalton_core.store import DaltonStore, content_hash
from dalton_core.shared_daily_budget import policy_wire, write_owner_only, install_binding, ZERO_COUNTS_SCHEMA_VERSION, SCHEMA_VERSION, SharedDailyBudgetError, shared_alphaengine_cap
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactDayBudgetExceeded


class HostSettingsTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workspace_mission_setup import WorkspaceFirstMissionTests
        fixture = WorkspaceFirstMissionTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.workspace = fixture.workspace
        self.state = self.workspace.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        self.root = Path(fixture.temp.name)
        self.manager = self.root / 'manager.json'
        self.policy = self.root / 'shared-daily.json'
        self.config = {'host_root': str(self.root / 'fleet'), 'release_path': str(self.root / 'release'),
            'release_ref': 'release:sha256:'+'a'*64, 'owner_login': 'owner@example.com',
            'tailscale_host': 'test.example', 'tailscale_executable': '/usr/bin/true',
            'launch_agents_dir': str(self.root / 'agents'), 'ports': [18820,18821],
            'shared_daily_budget_policy_path': str(self.policy),
            'shared_gateway': {'principal':'shared-test','public_origin':'https://test.example','secret_file': str(self.root/'key')}}
        write_owner_only(self.manager, self.config)
        write_owner_only(self.policy, policy_wire(max_daily_cost_usd=500, max_daily_paid_calls=100000,
            max_alphaengine_calls_24h=130, revision=1, prior_hash=None,
            updated_at='2026-10-10T00:00:00+00:00', actor_ref='human:owner'))
        self.service = {'core_db':str(self.state/'core.sqlite'), 'control':{'config':{
            'cockpit':{'workspace_manager_config_path':str(self.manager)}}}}
        write_owner_only(self.workspace.config_path, self.service)
        from tests.test_model_selection import StateDirectoryCase
        routing = StateDirectoryCase(); routing.setUp(); self.addCleanup(routing.doCleanups)
        target = __import__('sqlite3').connect(self.state/'model-router.sqlite')
        routing.router.connection.backup(target); target.close()
        from dalton_core.model_router import ModelRouter
        self.router = ModelRouter(self.state/'model-router.sqlite'); self.addCleanup(self.router.close)
        model = dict(routing.model_config)
        model.update(model_router_db=str(self.state/'model-router.sqlite'), budget_db=str(self.state/'thesis-impact-budget.sqlite'))
        write_owner_only(self.state/'research-planner-model-config.json', model)
        self.service['bounded_planner']={'config':{'planner_routing_policy_ref':model['routing_policy_ref'],
            'planner_credential_slot_refs':model['credential_slot_refs'],
            'planner_model_router_db':model['model_router_db'],'budget_db':model['budget_db']}}
        write_owner_only(self.workspace.config_path,self.service)
        budget = ThesisImpactBudgetStore(model['budget_db'])
        budget.register_policy(policy_version_id=model['budget_policy_ref'], day_cap_micros=500_000_000)
        self.addCleanup(budget.close)
        self.store = DaltonStore(self.state/'core.sqlite'); self.addCleanup(self.store.close)
        self.foundation = self.make_foundation(self.workspace)
        write_owner_only(self.state/'research-foundation.json', self.foundation)
        self.mission = self.publish(self.workspace, self.foundation)
        install_binding(self.state, environment_id=self.workspace.workspace_id, manager_config_path=self.manager, policy_path=self.policy)

    def make_foundation(self, workspace):
        from tests.p9a_fixtures import bootstrap_method_authorities, constitution_method
        seeded = bootstrap_method_authorities(self.store)
        method = constitution_method()
        drivers = {'drivers':[{'driver_ref':'driver:volume','label':'Volume','mechanism':'Demand changes units.', 'metric_refs':['metric:revenue']}],
            'metric_specs':[{'metric_ref':'metric:revenue','label':'Revenue','definition':'Reported revenue','unit':'USD','periodicity':'quarterly','preferred_source_refs':['source:sec-edgar'],'verification_kind':'numeric','caveats':[]}],
            'thesis_templates':[{'template_ref':'template:demand','statement':'Demand changes revenue.','mechanism':'Volume','driver_refs':['driver:volume'],'implied_expectation':'Revenue follows demand.','falsifier_refs':['falsifier:revenue']}]}
        body = {k:v for k,v in self.fixture.foundation.items() if k!='content_hash'}
        body['workspace_id'] = workspace.workspace_id
        body['methods'] = {'playbook':{'binding':{'ref':seeded['playbook']['id'],'hash':seeded['playbook']['content_hash']}},
             'driver_pack_template':{'value':drivers,'content_hash':content_hash(drivers)},
             'constitution_method':{'value':method,'content_hash':content_hash(method)}}
        return {**body,'content_hash':content_hash(body)}

    def publish(self, workspace, foundation):
        from dalton_core.workspace_mission_setup import draft_first_mission, publish_first_mission_to_store
        draft = draft_first_mission(workspace, goal='Research $ASML', industry='chips', method_foundation=foundation)
        return publish_first_mission_to_store(self.store, workspace, proposal=draft,
            proposal_hash=draft['content_hash'], actor_ref='human:owner', method_foundation=foundation)

    def seed(self):
        return settings.save(self.manager, actor_ref='human:owner', expected_hash=None, source_state_dir=self.state)['desired']

    def writer(self):
        from dalton_core.writer_server import WriterServer
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.research_constitution import ResearchConstitutionAuthority
        server = object.__new__(WriterServer)
        server.db_path = self.state/'core.sqlite'; server._store=self.store
        server._workspace=self.workspace
        server._coverage_mission=CoverageMissionAuthority(self.store)
        server._research_constitution=ResearchConstitutionAuthority(self.store)
        server._agenda=None; server._planner_model_config=None
        server._reload_planner_model_config=lambda:(True,'applied')
        return server

    def test_seed_preserves_formal_authorities_and_records_strong_creation_defaults(self):
        before = self.store.connection.execute('SELECT record_json FROM coverage_mission_versions').fetchall()
        desired = self.seed()
        self.assertIsNone(desired['research_budget'])
        self.assertEqual(desired['creation_budget_defaults']['budget'], {'max_daily_paid_calls':20,'max_daily_cost_usd':3.0,'max_alphaengine_calls_24h':0})
        self.assertEqual(desired['creation_budget_defaults']['source_bindings'][0]['mission_hash'],self.mission['content_hash'])
        self.assertEqual(before, self.store.connection.execute('SELECT record_json FROM coverage_mission_versions').fetchall())
        self.assertEqual(desired, settings.load(self.manager))
        self.assertEqual('applied',reconcile.status(self.manager)['status'])

    def test_cas_and_actor_remain_durable_before_stopped_writer_fanout(self):
        desired=self.seed()
        selection={'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra']}
        next_record=settings.save(self.manager,actor_ref='human:shared-test',expected_hash=desired['content_hash'], selection=selection,source_state_dir=self.state)['desired']
        self.assertEqual(next_record['prior_hash'],desired['content_hash'])
        self.assertEqual(next_record['actor_ref'],'human:shared-test')
        sync=reconcile.fan_out(self.manager,next_record)
        self.assertEqual('pending',sync['status']);self.assertEqual(1,len(sync['failed']))
        with self.assertRaises(settings.HostSettingsConflict):
            settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],selection=selection,source_state_dir=self.state)
        with self.assertRaises(settings.HostSettingsError):
            reconcile.assert_reconciled(self.state)
        reconcile.reconcile_writer(self.writer())
        reconcile.assert_reconciled(self.state)
        from dalton_core.model_routing_sync import agreed_selections
        self.assertEqual(agreed_selections(self.state/'model-router.sqlite')['purposes']['plan'],['profile:gpt-6-astra'])
        with self.assertRaises(Exception):
            settings.save(self.manager,actor_ref='human:owner',expected_hash=next_record['content_hash'],selection={**selection,'chain':['profile:invented']},source_state_dir=self.state)
        self.assertEqual(settings.load(self.manager),next_record)

    def test_global_zero_is_published_in_real_authority_chain_and_no_ledger_history_changes(self):
        desired=self.seed()
        old_row=self.store.connection.execute('SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?',(self.mission['id'],)).fetchone()[0]
        budget={'max_daily_paid_calls':0,'max_daily_cost_usd':500,'max_alphaengine_calls_24h':0,'max_daily_document_reads':0}
        updated=settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],research_budget=budget,source_state_dir=self.state)['desired']
        reconcile.reconcile_writer(self.writer())
        mission=settings.current_mission(self.state)
        self.assertEqual({k:mission['budget'][k] for k in budget},budget)
        self.assertEqual(old_row,self.store.connection.execute('SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?',(self.mission['id'],)).fetchone()[0])
        self.assertEqual(0,shared_alphaengine_cap(json.loads(self.policy.read_text())))
        ledger=ThesisImpactBudgetStore(self.state/'thesis-impact-budget.sqlite');self.addCleanup(ledger.close)
        ref=json.loads((self.state/'research-planner-model-config.json').read_text())['budget_policy_ref']
        with self.assertRaises(ThesisImpactDayBudgetExceeded) as blocked:
            ledger.admit(policy_version_id=ref,day='2026-10-10',work_order_ref='work:zero',attempt_number=1,phase='assessment',route_decision_ref='route:zero',reserved_micros=1)
        self.assertEqual('shared_daily_budget_exceeded',blocked.exception.rejection['reason'])
        count=self.store.connection.execute('SELECT COUNT(*) FROM coverage_mission_versions').fetchone()[0]
        reconcile.reconcile_writer(self.writer())
        self.assertEqual(count,self.store.connection.execute('SELECT COUNT(*) FROM coverage_mission_versions').fetchone()[0])
        self.assertEqual(updated['content_hash'],reconcile.status(self.manager)['desired_hash'])

    def test_new_and_preexisting_empty_workspace_draft_and_formal_publish_use_zero_defaults(self):
        from dalton_core.workspace import create_workspace_manifest
        from dalton_core.workspace_mission_setup import draft_first_mission,publish_first_mission_to_store,WorkspaceMissionSetupError
        desired=self.seed()
        budget={'max_daily_paid_calls':0,'max_daily_cost_usd':500,'max_alphaengine_calls_24h':0,'max_daily_document_reads':0}
        updated=settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],research_budget=budget,source_state_dir=self.state)['desired']
        future=create_workspace_manifest(self.root/'fleet','future',18821,self.workspace.release_ref,self.workspace.release_path,shared_readonly_paths=[self.workspace.release_path])
        future.state_dir.mkdir(parents=True,exist_ok=True)
        write_owner_only(future.config_path,{'core_db':str(future.state_dir/'core.sqlite'),'control':{'config':{'cockpit':{'workspace_manager_config_path':str(self.manager)}}}})
        foundation_body={k:v for k,v in self.foundation.items() if k!='content_hash'}
        foundation_body['workspace_id']=future.workspace_id
        foundation={**foundation_body,'content_hash':content_hash(foundation_body)}
        draft=draft_first_mission(future,goal='Research $ASML',industry='chips',method_foundation=foundation)
        self.assertEqual(updated['content_hash'],draft['host_settings_hash'])
        self.assertEqual({k:draft['mission_body']['budget'][k] for k in budget},budget)
        # Publish through a new real store, with the same held playbook authority.
        future_store=DaltonStore(future.state_dir/'core.sqlite');self.addCleanup(future_store.close)
        from tests.p9a_fixtures import bootstrap_method_authorities
        seeded=bootstrap_method_authorities(future_store)
        foundation_body['methods']={**foundation_body['methods'], 'playbook':{'binding':{'ref':seeded['playbook']['id'],'hash':seeded['playbook']['content_hash']}}}
        foundation={**foundation_body,'content_hash':content_hash(foundation_body)}
        draft=draft_first_mission(future,goal='Research $ASML',industry='chips',method_foundation=foundation)
        mission=publish_first_mission_to_store(future_store,future,proposal=draft,proposal_hash=draft['content_hash'],actor_ref='human:owner',method_foundation=foundation)
        self.assertEqual({k:mission['budget'][k] for k in budget},budget)
        settings.save(self.manager,actor_ref='human:owner',expected_hash=updated['content_hash'],research_budget={**budget,'max_daily_document_reads':1},source_state_dir=self.state)
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'重新起草'):
            publish_first_mission_to_store(future_store,future,proposal=draft,proposal_hash=draft['content_hash'],actor_ref='human:owner',method_foundation=foundation)

    def test_paused_is_retained_pending_and_deleted_is_not_a_target(self):
        from dalton_core.workspace_lifecycle import _manifest_state as set_workspace_lifecycle
        self.seed()
        # Real lifecycle helper preserves the canonical manifest hash.
        set_workspace_lifecycle(self.workspace.manifest_path,'paused')
        self.assertEqual('paused',reconcile.environments(self.manager)[0][1])
        set_workspace_lifecycle(self.workspace.manifest_path,'deleted')
        self.assertEqual([],reconcile.environments(self.manager))

    def test_shared_admin_saves_with_its_actual_actor_and_plain_string_does_not(self):
        from dalton_core.cockpit_plane import CockpitPlane,CockpitError
        from dalton_core.shared_access_identity import SharedLogin,shared_actor
        desired=self.seed();plane=object.__new__(CockpitPlane)
        plane.config=SimpleNamespace(state_dir=self.state,workspace_manager_config_path=self.manager)
        plane.journal=SimpleNamespace(record_event=lambda **kwargs:None)
        login=SharedLogin('shared-test')
        with patch('dalton_core.host_research_reconcile.fan_out',return_value={'note':'pending','host_settings':{'status':'pending'}}):
            result=plane.select_model(login,{'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra'],'expected_host_settings_hash':desired['content_hash']})
        self.assertEqual(shared_actor(login),settings.load(self.manager)['actor_ref'])
        with self.assertRaises(CockpitError):
            plane.select_model(str(login),{'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra'],'expected_host_settings_hash':result['desired_hash']})

    def test_normal_writer_replays_saved_instruction_before_requested_operation_and_is_idempotent(self):
        from dalton_core.writer_server import Principal, HUMAN_GOVERNANCE_OPERATIONS
        from dalton_core.writer_protocol import Request
        desired=self.seed()
        selection={'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra']}
        saved=settings.save(self.manager,actor_ref='human:shared-test',expected_hash=desired['content_hash'],selection=selection,source_state_dir=self.state)['desired']
        server=self.writer();server._token_config_path=None
        server._by_token={'token':Principal('governance','token',HUMAN_GOVERNANCE_OPERATIONS,actor_ref='human:shared-test')}
        request=Request('0.1','reconcile','set_model_selection',selection,'token')
        result=server._handle(request)
        self.assertEqual('unchanged',result['status'])
        rows=self.router.connection.execute('SELECT policy_json FROM model_routing_policy_versions ORDER BY rowid').fetchall()
        server._handle(request)
        self.assertEqual(rows,self.router.connection.execute('SELECT policy_json FROM model_routing_policy_versions ORDER BY rowid').fetchall())
        self.assertEqual('applied',reconcile.status(self.manager)['status'])

    def test_pre_payment_gate_refuses_pending_and_stale_cached_model_before_any_adapter_call(self):
        from dalton_core.cockpit_model import CockpitModel
        desired=self.seed();old_config=json.loads((self.state/'research-planner-model-config.json').read_text())
        saved=settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],selection={'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra']},source_state_dir=self.state)['desired']
        model=object.__new__(CockpitModel);model.scheduler_db=self.state/'scheduler.sqlite';model.config=old_config
        calls=[];model._call_once=lambda **kwargs:calls.append(kwargs) or {'status':'success'}
        with self.assertRaises(settings.HostSettingsError):model.call(purpose='plan',request_id='pending',prompt='text',mission=self.mission)
        self.assertEqual([],calls)
        reconcile.reconcile_writer(self.writer())
        model.call(purpose='plan',request_id='fresh-instance',prompt='text',mission=self.mission)
        self.assertEqual(1,len(calls))
        self.assertEqual(old_config,model.config)
        model.config=json.loads((self.state/'research-planner-model-config.json').read_text())
        model.call(purpose='plan',request_id='current',prompt='text',mission=self.mission)
        self.assertEqual(2,len(calls))

    def test_thinking_instruction_is_in_formal_router_and_repeat_resume_adds_nothing(self):
        desired=self.seed()
        saved=settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],selection={'purpose':'plan','thinking_level':'medium','reason':'需要核对经营关系'},source_state_dir=self.state)['desired']
        reconcile.reconcile_writer(self.writer())
        policies=reconcile.consumer_policies(reconcile.environments(self.manager)[0][0])
        self.assertTrue(all(p['thinking_level_overrides']['plan']['level']=='medium' for p in policies))
        rows=self.router.connection.execute('SELECT policy_json FROM model_routing_policy_versions').fetchall()
        reconcile.reconcile_writer(self.writer())
        self.assertEqual(rows,self.router.connection.execute('SELECT policy_json FROM model_routing_policy_versions').fetchall())
        self.assertEqual(saved['selections']['thinking'],settings.seed_selections(self.state)['thinking'])

    def test_corrupt_head_and_zero_booleans_never_enable_dispatch(self):
        desired=self.seed();path=settings.location(self.manager)
        bad={**desired,'research_budget':{'max_daily_paid_calls':False,'max_daily_cost_usd':500,'max_alphaengine_calls_24h':0}}
        bad['content_hash']=content_hash({k:v for k,v in bad.items() if k!='content_hash'})
        write_owner_only(path,bad)
        with self.assertRaises(settings.HostSettingsError):reconcile.assert_reconciled(self.state)
        with self.assertRaises(settings.HostSettingsError):reconcile.reconcile_writer(self.writer())

    def test_parallel_calls_resolve_independent_context_without_mutating_shared_instance(self):
        from dalton_core.cockpit_model import CockpitModel
        from concurrent.futures import ThreadPoolExecutor
        import threading
        desired=self.seed();old_config=json.loads((self.state/'research-planner-model-config.json').read_text())
        settings.save(self.manager,actor_ref='human:owner',expected_hash=desired['content_hash'],selection={'purpose':'plan','mode':'explicit','chain':['profile:gpt-6-astra']},source_state_dir=self.state)
        reconcile.reconcile_writer(self.writer())
        model=object.__new__(CockpitModel);model.scheduler_db=self.state/'scheduler.sqlite';model.config=old_config
        contexts=[];barrier=threading.Barrier(2)
        def resolved(local, **kwargs):
            contexts.append(local.config);barrier.wait(timeout=5)
            return {'status':'success'}
        with patch.object(CockpitModel,'_call_resolved',resolved),ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(model.call,purpose='plan',request_id=str(i),prompt='text',mission=self.mission) for i in range(2)]
            for future in futures:future.result()
        self.assertEqual(old_config,model.config)
        self.assertEqual(2,len(contexts));self.assertIsNot(contexts[0],contexts[1])
        self.assertEqual(contexts[0]['routing_policy_ref'],contexts[1]['routing_policy_ref'])
        self.assertNotEqual(old_config['routing_policy_ref'],contexts[0]['routing_policy_ref'])

    def test_seed_cold_router_is_read_only_and_its_formal_hash_is_verified(self):
        self.router.close()
        database=self.state/'model-router.sqlite'
        before=database.read_bytes()
        self.assertFalse(Path(str(database)+'-wal').exists())
        result=settings.seed_selections(self.state)
        self.assertTrue(result['tiers'])
        self.assertEqual(before,database.read_bytes())
        self.assertFalse(Path(str(database)+'-wal').exists());self.assertFalse(Path(str(database)+'-shm').exists())
        import sqlite3
        connection=sqlite3.connect(database)
        # Simulate damaged storage in this disposable fixture only. Production
        # immutability triggers remain untouched by every settings operation.
        names=connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='model_routing_policy_versions'").fetchall()
        for name, in names:connection.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
        connection.execute("UPDATE model_routing_policy_versions SET policy_hash=? WHERE rowid=(SELECT MAX(rowid) FROM model_routing_policy_versions)",('0'*64,));connection.commit();connection.close()
        with self.assertRaises(settings.HostSettingsError):settings.seed_selections(self.state)

    def test_old_zero_policy_stays_invalid_new_zero_policy_is_exact_and_hashed(self):
        arguments=dict(max_daily_cost_usd=500,max_daily_paid_calls=0,max_alphaengine_calls_24h=0,revision=1,prior_hash=None,updated_at='2026-10-10T00:00:00+00:00',actor_ref='human:owner')
        with self.assertRaises(SharedDailyBudgetError): policy_wire(**arguments)
        wire=policy_wire(**arguments,schema_version=ZERO_COUNTS_SCHEMA_VERSION)
        self.assertEqual(0,wire['max_daily_paid_calls']);self.assertEqual(0,shared_alphaengine_cap(wire))

if __name__=='__main__':unittest.main()
