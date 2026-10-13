from __future__ import annotations
import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core import host_source_budget_sync as sync,host_research_settings as settings
from dalton_core.store import content_hash
from dalton_core.mission_source_discovery import build_discovery_plan,load_discovery_plan
from dalton_core.shared_daily_budget import policy_wire,write_owner_only,install_binding

class SourceBudgetSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'env/state/dalton-core';self.state.mkdir(parents=True)
        self.manager=self.root/'manager.json';self.policy=self.root/'daily.json'
        self.config={'host_root':str(self.root/'host'),'release_path':str(self.root/'release'),'release_ref':'release:sha256:'+'a'*64,'owner_login':'owner@example.test','tailscale_host':'example.test','tailscale_executable':'/usr/bin/true','launch_agents_dir':str(self.root/'agents'),'ports':[18820,18821],'shared_daily_budget_policy_path':str(self.policy)}
        settings._write(self.manager,self.config)
        self.service=self.root/'env/config/service.json';settings._write(self.service,{'core_db':str(self.state/'core.sqlite'),'alphaengine_owner_call_cap':130,'unrelated':'preserve'})
        self.budget={'max_daily_cost_usd':500.,'max_daily_paid_calls':100000,'max_alphaengine_calls_24h':130,'max_daily_document_reads':2000}
        self.desired=self.record(1,None,None)
        self.plan=build_discovery_plan(plan_id='discovery-plan:synthetic:alphaengine:1',created_at='2026-10-12T00:00:00.000000+00:00',mission_ref='coverage-mission:synthetic',companies={'company:ticker:demo':'Demo'},specs=[{'spec_ref':'earnings-call-transcripts','document_type':'meeting_minutes','query_template':'{terms} earnings call transcript','lookback_days':400,'rediscovery_interval_days':7,'retry_interval_days':1}],max_calls_24h=130)
        self.planpath=self.state/'discovery-plans/us-it-services-alphaengine-v1.json';settings._write(self.planpath,self.plan);self.original=self.planpath.read_bytes()
        self.server=SimpleNamespace(state_dir=self.state,_discovery_plan_path=str(self.planpath),_alphaengine_owner_call_cap=130,_source_discovery=SimpleNamespace(owner_call_cap=130))
        def install(*,alpha_path):
            self.server._discovery_plan_path=str(alpha_path);self.server._source_discovery=SimpleNamespace(owner_call_cap=self.server._alphaengine_owner_call_cap,plan=load_discovery_plan(alpha_path))
        self.server._install_discovery_plans=install
    def record(self,revision,prior,budget):
        b={'schema_version':settings.SCHEMA_VERSION,'revision':revision,'prior_hash':None if prior is None else prior['content_hash'],'updated_at':f'2026-10-12T00:{revision:02d}:00.000000+00:00','actor_ref':'human:owner','selections':{'tiers':{},'purposes':{},'thinking':{}},'research_budget':budget,'creation_budget_defaults':{'budget':dict(self.budget),'basis':'held_consensus','source_bindings':[]}}
        d=settings.validate({**b,'content_hash':content_hash(b)});settings._write(settings.location(self.manager),d);settings._write(settings.location(self.manager).parent/'shared-research-settings-versions'/(d['content_hash']+'.json'),d);return d
    def change(self,cap):
        self.desired=self.record(self.desired['revision']+1,self.desired,{**self.budget,'max_alphaengine_calls_24h':cap});return self.desired
    def test_raise_and_lower_select_new_immutable_plan_and_resident_cap(self):
        for cap in (260,80):
            sync.apply(self.server,self.manager,self.change(cap))
            self.assertEqual(self.server._source_discovery.owner_call_cap,cap);self.assertEqual(self.server._source_discovery.plan['budget']['max_calls_24h'],cap)
            self.assertFalse(sync.pending(self.state,self.manager,self.desired));self.assertEqual(json.loads(self.service.read_text())['unrelated'],'preserve')
        self.assertEqual(self.original,self.planpath.read_bytes())
    def test_independent_lower_owner_and_plan_remain_lower(self):
        settings._write(self.service,{'core_db':str(self.state/'core.sqlite'),'alphaengine_owner_call_cap':40})
        b={k:v for k,v in self.plan.items()if k!='content_hash'};b['budget']={'max_calls_24h':50};settings._write(self.planpath,{**b,'content_hash':content_hash(b)})
        sync.apply(self.server,self.manager,self.change(260))
        self.assertEqual(self.server._source_discovery.owner_call_cap,40);self.assertEqual(self.server._source_discovery.plan['budget']['max_calls_24h'],50)
    def test_zero_denies_without_invalid_plan_then_reenable_inheritance(self):
        sync.apply(self.server,self.manager,self.change(260));path=Path(self.server._discovery_plan_path);frozen=path.read_bytes()
        sync.apply(self.server,self.manager,self.change(0));self.assertEqual(path.read_bytes(),frozen)
        from dalton_core.mission_source_discovery import alphaengine_calls_remaining
        with patch('dalton_core.mission_source_discovery.count_recent_alphaengine_calls',return_value=0):
            self.assertEqual(alphaengine_calls_remaining(None,mission_cap=0,owner_cap=260,plan_cap=260)['remaining'],0)
        sync.apply(self.server,self.manager,self.change(300));self.assertEqual(self.server._source_discovery.plan['budget']['max_calls_24h'],300)
    def test_restart_old_arguments_hot_reloaded_without_new_plan(self):
        sync.apply(self.server,self.manager,self.change(260));files=set(self.planpath.parent.iterdir());self.server._alphaengine_owner_call_cap=130;self.server._source_discovery.owner_call_cap=130
        sync.apply(self.server,self.manager,self.desired);self.assertEqual(self.server._source_discovery.owner_call_cap,260);self.assertEqual(files,set(self.planpath.parent.iterdir()))
    def test_external_lower_plan_does_not_borrow_managed_receipt(self):
        sync.apply(self.server,self.manager,self.change(260));path=Path(self.server._discovery_plan_path);p=load_discovery_plan(path);b={k:v for k,v in p.items()if k!='content_hash'};b['budget']={'max_calls_24h':20};replacement=self.planpath.parent/'independent.json';settings._write(replacement,{**b,'content_hash':content_hash(b)});self.server._discovery_plan_path=str(replacement)
        sync.apply(self.server,self.manager,self.change(400));self.assertEqual(self.server._source_discovery.plan['budget']['max_calls_24h'],20)
    def test_missing_shared_pin_recovered_from_binding_to_prior_policy_hash(self):
        prior=policy_wire(max_daily_cost_usd=500,max_daily_paid_calls=100000,max_alphaengine_calls_24h=130,revision=1,prior_hash=None,updated_at='2026-10-12T00:00:00+00:00',actor_ref='human:owner');write_owner_only(self.policy,prior)
        install_binding(self.state,environment_id='env',manager_config_path=self.manager,policy_path=self.policy)
        current=policy_wire(max_daily_cost_usd=500,max_daily_paid_calls=100000,max_alphaengine_calls_24h=260,revision=2,prior_hash=prior['content_hash'],updated_at='2026-10-12T01:00:00+00:00',actor_ref='human:owner');write_owner_only(self.policy,current)
        conf=dict(self.config);conf.pop('shared_daily_budget_policy_path');settings._write(self.manager,conf)
        with patch('dalton_core.host_research_reconcile.environments',return_value=[(SimpleNamespace(state_dir=self.state,environment_id='env'),'active')]):
            self.assertEqual(sync.shared_policy_path(self.manager,persist=True),self.policy)
        self.assertEqual(json.loads(self.manager.read_text())['shared_daily_budget_policy_path'],str(self.policy));self.assertEqual(json.loads(self.policy.read_text()),current)
    def test_missing_shared_pin_foreign_environment_refused_before_write(self):
        prior=policy_wire(max_daily_cost_usd=500,max_daily_paid_calls=100000,max_alphaengine_calls_24h=130,revision=1,prior_hash=None,updated_at='2026-10-12T00:00:00+00:00',actor_ref='human:owner');write_owner_only(self.policy,prior);install_binding(self.state,environment_id='wrong',manager_config_path=self.manager,policy_path=self.policy)
        conf=dict(self.config);conf.pop('shared_daily_budget_policy_path');settings._write(self.manager,conf);old=self.manager.read_bytes()
        with patch('dalton_core.host_research_reconcile.environments',return_value=[(SimpleNamespace(state_dir=self.state,environment_id='env'),'active')]):
            with self.assertRaises(settings.HostSettingsError):sync.shared_policy_path(self.manager,persist=True)
        self.assertEqual(self.manager.read_bytes(),old)
    def test_cap_out_of_range_refused_before_desired_save(self):
        original=settings.location(self.manager).read_bytes()
        with self.assertRaises(settings.HostSettingsError):
            settings.save(self.manager,actor_ref='human:owner',expected_hash=self.desired['content_hash'],research_budget={**self.budget,'max_alphaengine_calls_24h':2001})
        self.assertEqual(settings.location(self.manager).read_bytes(),original)
    def test_partial_selector_then_config_failure_recovers_inheritance(self):
        d=self.change(260);real=settings._write
        def fail_service(path,value):
            if Path(path)==self.service:raise OSError('synthetic config failure')
            return real(path,value)
        with patch.object(settings,'_write',side_effect=fail_service):
            with self.assertRaises(OSError):sync.apply(self.server,self.manager,d)
        self.assertTrue(sync.pending(self.state,self.manager,d))
        sync.apply(self.server,self.manager,d);self.assertFalse(sync.pending(self.state,self.manager,d))
        sync.apply(self.server,self.manager,self.change(300))
        self.assertEqual(self.server._source_discovery.plan['budget']['max_calls_24h'],300)
        self.assertEqual(self.original,self.planpath.read_bytes())
    def test_actual_writer_installer_hot_loads_and_restart_selector_preserves_plan(self):
        from dalton_core.writer_server import WriterServer
        from dalton_core.macos_launchagent import _alphaengine_discovery_plan
        server=object.__new__(WriterServer);server.db_path=self.state/'core.sqlite';server._store=SimpleNamespace();server._coverage_mission=SimpleNamespace()
        server._discovery_plan_path=str(self.planpath);server._source_discovery=None;server._alphaengine_owner_call_cap=130
        server._search_launcher=SimpleNamespace(plan_path=self.planpath);server._acquisition_launcher=None;server._discovery_selection_launcher=None;server._transcript_spool_dir=None
        sync.apply(server,self.manager,self.change(260))
        selected=_alphaengine_discovery_plan(self.state)
        self.assertEqual(selected.resolve(),Path(server._discovery_plan_path).resolve())
        self.assertEqual(server._search_launcher.plan_path.resolve(),selected.resolve())
        self.assertEqual(server._source_discovery.owner_call_cap,260)
        self.assertEqual(server._source_discovery.plan['budget']['max_calls_24h'],260)
        self.assertEqual(load_discovery_plan(selected)['mission_ref'],self.plan['mission_ref'])
        server._source_discovery=None;server._alphaengine_owner_call_cap=130
        sync.apply(server,self.manager,self.desired)
        self.assertEqual(server._source_discovery.owner_call_cap,260)
    def test_exact_receipt_short_circuits_history_reads(self):
        sync.apply(self.server,self.manager,self.change(260))
        with patch.object(sync,'_previous_cap',side_effect=AssertionError('history should not be read')):
            self.assertFalse(sync.pending(self.state,self.manager,self.desired))

    def test_real_blank_writer_syncs_default_cap_without_creating_mission_or_plan(self):
        from dalton_core.writer_server import WriterServer
        from dalton_core.store import DaltonStore
        from dalton_core.coverage_mission import CoverageMissionAuthority
        self.planpath.unlink()
        store=DaltonStore(self.state/'core.sqlite');self.addCleanup(store.close)
        missions=CoverageMissionAuthority(store)
        server=object.__new__(WriterServer);server.db_path=self.state/'core.sqlite'
        server._store=store;server._coverage_mission=missions
        server._discovery_plan_path=str(self.planpath);server._source_discovery=None
        server._alphaengine_owner_call_cap=130
        desired=self.change(260)
        before=store.connection.total_changes
        with patch.object(WriterServer,'_install_discovery_plans',side_effect=AssertionError('no source plan to install')):
            sync.apply(server,self.manager,desired)
            sync.apply(server,self.manager,desired)
        self.assertEqual(server._alphaengine_owner_call_cap,260)
        self.assertEqual(json.loads(self.service.read_text())['alphaengine_owner_call_cap'],260)
        self.assertFalse(sync.pending(self.state,self.manager,desired))
        self.assertFalse(self.planpath.exists());self.assertIsNone(server._source_discovery)
        self.assertEqual(store.connection.total_changes,before)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM coverage_mission_versions').fetchone()[0],0)
        self.assertEqual(sync._receipt(self.state)['plan_hash'],None)

    def test_explicit_missing_plan_refuses_before_cap_or_receipt_write(self):
        self.planpath.unlink();self.server._discovery_plan_path=str(self.planpath.with_name('explicit-custom.json'))
        old=self.service.read_bytes()
        with self.assertRaises(FileNotFoundError):sync.apply(self.server,self.manager,self.change(260))
        self.assertEqual(self.service.read_bytes(),old);self.assertFalse((self.state/sync.RECEIPT).exists())
        self.assertEqual(self.server._alphaengine_owner_call_cap,130)

    def test_damaged_selector_refuses_even_with_missing_default(self):
        from dalton_core.macos_launchagent import ALPHAENGINE_PLAN_SELECTOR
        self.planpath.unlink();selector=self.planpath.parent/ALPHAENGINE_PLAN_SELECTOR
        selector.write_text('{invalid')
        old=self.service.read_bytes()
        with self.assertRaises(ValueError):sync.apply(self.server,self.manager,self.change(260))
        self.assertEqual(self.service.read_bytes(),old);self.assertFalse((self.state/sync.RECEIPT).exists())

    def test_approved_selector_missing_original_plan_refuses(self):
        from dalton_core.macos_launchagent import ALPHAENGINE_PLAN_SELECTOR
        body={'schema_version':'alphaengine-discovery-plan-selection-0.1','id':'discovery-plan-selection:fixture',
              'status':'approved','source_ref':self.plan['source_ref'],'plan_ref':self.plan['id'],
              'plan_hash':self.plan['content_hash'],'plan_path':self.planpath.name}
        settings._write(self.planpath.parent/ALPHAENGINE_PLAN_SELECTOR,{**body,'content_hash':content_hash(body)})
        self.planpath.unlink();old=self.service.read_bytes()
        with self.assertRaises(FileNotFoundError):sync.apply(self.server,self.manager,self.change(260))
        self.assertEqual(self.service.read_bytes(),old);self.assertFalse((self.state/sync.RECEIPT).exists())

    def test_broken_default_symlink_is_not_an_unconfigured_source(self):
        self.planpath.unlink();self.planpath.symlink_to(self.planpath.parent/'missing.json')
        old=self.service.read_bytes()
        with self.assertRaises(FileNotFoundError):sync.apply(self.server,self.manager,self.change(260))
        self.assertEqual(self.service.read_bytes(),old);self.assertFalse((self.state/sync.RECEIPT).exists())
