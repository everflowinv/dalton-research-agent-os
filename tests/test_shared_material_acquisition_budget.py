"""Material settings reuse the administrator CAS; accounting remains immutable."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core import host_research_settings as hs, host_research_reconcile as reconcile
from dalton_core import budget_pools as bp, material_acquisition as ma
from dalton_core.store import content_hash
from tests import test_host_research_settings as fixtures


class SharedMaterialBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HostSettingsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.prior = self.f.seed()
        self.old_budget = {k: self.f.mission['budget'][k] for k in ('max_daily_paid_calls','max_daily_cost_usd','max_alphaengine_calls_24h')}

    def save(self, calls=180, usd='6.00', prior=None):
        return hs.save(self.f.manager, actor_ref='human:shared-test',
            expected_hash=(prior or self.prior)['content_hash'], source_state_dir=self.f.state,
            research_budget={**self.old_budget, 'material_acquisition':{'calls_per_day':calls,'usd_per_day':usd}})['desired']

    def test_real_cas_reader_active_paused_and_new_environment_inherit_without_mission_rewrite(self):
        old = hs.current_mission(self.f.state)
        desired = self.save()
        self.assertEqual(hs.SCHEMA_VERSION, desired['schema_version'])
        self.assertEqual(old, hs.current_mission(self.f.state))
        caps = bp.material_acquisition_caps(None, self.f.state)
        self.assertEqual((180,6_000_000),(caps['calls_per_day'],caps['usd_micros_per_day']))
        self.assertEqual(desired['content_hash'],caps['shared_desired_hash'])
        # No fan-out writes are needed: stopped/paused/future consumers read
        # the same durable host instruction at their next admission.
        newstate = Path(self.f.temp.name)/'new'/'state'/'dalton-core' if hasattr(self.f,'temp') else self.f.root/'new'/'state'/'dalton-core'
        newstate.mkdir(parents=True)
        config = newstate.parents[1]/'config'/'service.json';config.parent.mkdir()
        config.write_text(json.dumps({'core_db':str(newstate/'core.sqlite'),'control':{'config':{'cockpit':{'workspace_manager_config_path':str(self.f.manager)}}}}))
        for state in (self.f.state,newstate):
            self.assertEqual(180,bp.material_acquisition_caps(None,state)['calls_per_day'])
        with patch('dalton_core.workspace_host_scheme.resolve_manager_config',return_value=self.f.manager):
            mission_defaults, pin=hs.defaults_for_workspace(newstate)
        self.assertEqual(self.old_budget,mission_defaults)
        self.assertEqual(desired['content_hash'],pin)
        self.assertEqual(desired['research_budget'],desired['creation_budget_defaults']['budget'])
        self.assertTrue(reconcile.budget_landed(old,desired['research_budget']))
        with self.assertRaises(hs.HostSettingsConflict):self.save(240)
        self.assertEqual(desired,hs.load(self.f.manager))

    def test_explicit_partial_environment_override_visible_and_preserved(self):
        self.save()
        path=self.f.state/bp.MATERIAL_ACQUISITION_POOL_FILE
        path.write_text(json.dumps({'calls_per_day':90}))
        before=path.read_bytes()
        caps=bp.material_acquisition_caps(None,self.f.state)
        self.assertEqual((90,6_000_000),(caps['calls_per_day'],caps['usd_micros_per_day']))
        self.assertTrue(caps['explicit_override'])
        self.assertEqual(bp.MATERIAL_ACQUISITION_POOL_FILE,caps['source'])
        self.assertEqual(180,caps['shared_caps']['calls_per_day'])
        self.assertEqual(before,path.read_bytes())

    def test_legacy_record_hash_and_older_save_do_not_drop_material_setting(self):
        legacy={**self.prior,'schema_version':hs.LEGACY_SCHEMA_VERSION}
        legacy['content_hash']=content_hash({k:v for k,v in legacy.items()if k!='content_hash'})
        self.assertEqual(legacy,hs.validate(legacy))
        desired=self.save()
        next_record=hs.save(self.f.manager,actor_ref='human:shared-test',expected_hash=desired['content_hash'],
            research_budget={**self.old_budget,'max_daily_document_reads':500},source_state_dir=self.f.state)['desired']
        self.assertEqual(desired['research_budget']['material_acquisition'],next_record['research_budget']['material_acquisition'])
        forged={**next_record,'schema_version':hs.LEGACY_SCHEMA_VERSION}
        forged['content_hash']=content_hash({k:v for k,v in forged.items()if k!='content_hash'})
        with self.assertRaises(hs.HostSettingsError):hs.validate(forged)

    def test_real_pool_exhaustion_reopens_only_budget_gate_accounting_unchanged(self):
        # Existing day entries are estimates, not claims of settled/free fees.
        ma._record_spend(self.f.state,'material-request:synthetic','sec_edgar',60,840000,now='2026-10-12T00:00:00+00:00')
        with ma._reader(self.f.state)as c:before=[tuple(r)for r in c.execute('SELECT * FROM material_acquisition_spend')]
        self.save(60,'2.00')
        self.assertEqual(0,ma.pool_status(self.f.state,now='2026-10-12T01:00:00+00:00')['remaining_calls'])
        current=hs.load(self.f.manager);self.save(180,'6.00',prior=current)
        status=ma.pool_status(self.f.state,now='2026-10-12T01:00:00+00:00')
        self.assertEqual((120,5_160_000),(status['remaining_calls'],status['remaining_usd_micros']))
        for i in range(7):
            self.assertIsNone(ma._budget_check(self.f.state,{'id':f'material-request:fresh-{i}','budget':{}},1,0,now='2026-10-12T01:00:00+00:00'))
        with ma._reader(self.f.state)as c:after=[tuple(r)for r in c.execute('SELECT * FROM material_acquisition_spend')]
        self.assertEqual(before,after)
        self.assertEqual(self.old_budget,hs.current_mission(self.f.state)['budget'])

    def test_invalid_zero_and_corrupt_shared_fail_closed(self):
        for cap in ({'calls_per_day':True,'usd_per_day':'6'}, {'calls_per_day':1,'usd_per_day':'NaN'},
                    {'calls_per_day':-1,'usd_per_day':6}, {'calls_per_day':1},
                    {'calls_per_day':1,'usd_per_day':6,'extra':1}):
            with self.subTest(cap=cap),self.assertRaises(hs.HostSettingsError):
                hs.validate_budget({**self.old_budget,'material_acquisition':cap})
        self.save(0,'0')
        self.assertEqual(0,bp.material_acquisition_caps(None,self.f.state)['calls_per_day'])
        path=hs.location(self.f.manager);wire=json.loads(path.read_bytes());wire['research_budget']['material_acquisition']['calls_per_day']=999
        path.write_text(json.dumps(wire))
        with self.assertRaises(hs.HostSettingsError):bp.material_acquisition_caps(None,self.f.state)

    def test_actual_public_admin_budget_save_cas_and_plain_actor_refused(self):
        from types import SimpleNamespace
        from dalton_core.cockpit_plane import CockpitPlane, CockpitError, CockpitConflict
        from dalton_core.shared_access_identity import SharedLogin, shared_actor
        plane=object.__new__(CockpitPlane)
        plane.config=SimpleNamespace(state_dir=self.f.state,workspace_manager_config_path=self.f.manager)
        events=[]
        plane.journal=SimpleNamespace(record_event=lambda **kw:events.append(kw))
        mission=hs.current_mission(self.f.state)
        payload={'mission_ref':mission['mission_ref'],'expected_mission_hash':mission['content_hash'],
            'expected_host_settings_hash':self.prior['content_hash'],
            'budget':{**self.old_budget,'material_acquisition':{'calls_per_day':180,'usd_per_day':'6.00'}}}
        login=SharedLogin('shared-test')
        with patch('dalton_core.host_research_reconcile.fan_out',return_value={'note':'applied','host_settings':{'status':'applied'}}):
            result=plane.set_research_budget(login,payload)
            with self.assertRaises(CockpitConflict):plane.set_research_budget(login,payload)
            with self.assertRaises(CockpitError):plane.set_research_budget(str(login),{**payload,'expected_host_settings_hash':result['desired_hash']})
        current=hs.load(self.f.manager)
        self.assertEqual(shared_actor(login),current['actor_ref'])
        self.assertEqual(mission,hs.current_mission(self.f.state))
        self.assertEqual(180,plane._host_settings_view()['material_acquisition_effective']['calls_per_day'])
        self.assertEqual(result['desired_hash'],events[0]['refs']['desired_hash'])

    def test_new_unmanaged_default_is_bounded_and_ui_actual_fields(self):
        self.assertEqual((180,6_000_000),(bp.material_acquisition_caps()['calls_per_day'],bp.material_acquisition_caps()['usd_micros_per_day']))
        js=(Path(__file__).parents[1]/'src/dalton_core/cockpit_v2/admin/admin.js').read_text()
        self.assertIn('budget.material_acquisition = { calls_per_day:',js)
        self.assertIn('material_acquisition_effective.explicit_override',js)
        self.assertIn('各项目分别计数',js)
        self.assertIn('材料获取当前有效上限',js)
        self.assertIn('本项目使用单独设置的额度：每天 ${count(host.material_acquisition_effective.calls_per_day)}',js)
