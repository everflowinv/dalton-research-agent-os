"""Different active pins in one policy family must not disable goal planning."""
import copy,json,unittest
from unittest.mock import patch
from dalton_core import host_research_reconcile as reconcile
from dalton_core import host_research_settings as settings
from dalton_core.cockpit_plane import _job_error_message
from dalton_core.store import content_hash
from dalton_core.shared_daily_budget import write_owner_only

class ActivePolicyGoalTests(unittest.TestCase):
    def setUp(self):
        from tests.test_host_research_settings import HostSettingsTests
        self.f=HostSettingsTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.config=json.loads((self.f.state/'research-planner-model-config.json').read_text())
        self.one=self.f.router.get_policy(self.config['routing_policy_ref'])
        self.two=self.version(self.one,2)
        self.f.service['bounded_planner']['config']['planner_routing_policy_ref']=self.two['policy_version_ref']
        write_owner_only(self.f.workspace.config_path,self.f.service)
        self.desired=self.f.seed()
        self.environment=__import__('types').SimpleNamespace(state_dir=self.f.state)

    def version(self, prior, number):
        body={k:v for k,v in prior.items()if k!='content_hash'}
        body.update(version=number,prior_version_ref=prior['policy_version_ref'],
                    policy_version_ref='model-routing-policy-version:goal-test:'+str(number))
        wire={**body,'content_hash':content_hash(body)};self.f.router.register_policy(wire);return wire

    def resolve(self, config=None):
        with patch.object(reconcile,'environments',return_value=[(self.environment,'active')]), patch(
            'dalton_core.workspace_host_scheme.resolve_manager_config',return_value=self.f.manager):
            return reconcile.resolved_model_config(self.f.state,self.config if config is None else config,'goal')

    def test_two_purpose_active_versions_keep_exact_goal_pin(self):
        self.assertEqual(len(reconcile.consumer_policies(self.environment)),2)
        self.assertFalse(reconcile.pending(self.environment,self.f.manager,self.desired))
        actual=self.resolve()
        self.assertEqual(actual['routing_policy_ref'],self.one['policy_version_ref'])
        self.assertEqual(actual['budget_policy_ref'],self.config['budget_policy_ref'])

    def test_expired_pin_only_recovers_when_active_head_is_unique(self):
        # Removing the old pin from all live consumers leaves one legitimate
        # replacement; the caller may still hold the old immutable snapshot.
        fresh={**self.config,'routing_policy_ref':self.two['policy_version_ref']}
        write_owner_only(self.f.state/'research-planner-model-config.json',fresh)
        actual=self.resolve()
        self.assertEqual(actual['routing_policy_ref'],self.two['policy_version_ref'])

    def test_expired_pin_with_two_different_active_heads_is_rejected(self):
        third=self.version(self.two,3)
        stale={**self.config,'routing_policy_ref':third['policy_version_ref']}
        with self.assertRaisesRegex(settings.HostSettingsError,'仍未完整同步'):
            self.resolve(stale)

    def test_wrong_active_hash_is_not_used(self):
        policies=reconcile.consumer_policies(self.environment)
        bad=copy.deepcopy(policies);next(p for p in bad if p['policy_version_ref']==self.one['policy_version_ref'])['content_hash']='0'*64
        with patch.object(reconcile,'consumer_policies',return_value=bad):
            with self.assertRaisesRegex(settings.HostSettingsError,'内容校验失败'):self.resolve()

    def test_desired_not_landed_is_still_refused(self):
        value=copy.deepcopy(self.desired);value['selections']['tiers']['brain']={'mode':'explicit','chain':['profile:deepseek-v4-flash']}
        value['content_hash']=content_hash({k:v for k,v in value.items()if k!='content_hash'})
        with patch.object(settings,'load',return_value=value):
            with self.assertRaisesRegex(settings.HostSettingsError,'尚待应用'):self.resolve()

    def test_explicit_research_budget_keeps_unique_consumer_authority_guard(self):
        budget={'max_daily_paid_calls':100000,'max_daily_cost_usd':500,'max_alphaengine_calls_24h':130}
        value=settings.save(self.f.manager,actor_ref='human:owner',expected_hash=self.desired['content_hash'],
            research_budget=budget,source_state_dir=self.f.state)['desired']
        reconcile.reconcile_writer(self.f.writer())
        fresh=json.loads((self.f.state/'research-planner-model-config.json').read_text())
        actual=self.resolve()
        self.assertEqual(actual['budget_policy_ref'],fresh['budget_policy_ref'])
        other={**fresh,'budget_policy_ref':'budget:foreign'}
        write_owner_only(self.f.state/'document-extraction-model-config.json',other)
        with self.assertRaisesRegex(settings.HostSettingsError,'研究预算授权尚未同步'):
            self.resolve()

class GoalFailureDisplayTests(unittest.TestCase):
    def test_only_closed_settings_pre_call_reasons_are_readable(self):
        text=_job_error_message('HostSettingsError: 当前模型策略仍未完整同步，新调用暂缓')
        self.assertIn('模型或预算设置',text);self.assertIn('尚未开始',text)
        self.assertNotIn('HostSettingsError',text)
        self.assertIsNone(_job_error_message('HostSettingsError: /private/secret unexpected database detail'))
        self.assertIsNone(_job_error_message('HostSettingsError: 内部路径 /private/secret 非公开错误'))

    def test_actual_async_goal_job_polling_has_safe_sync_message(self):
        import datetime,tempfile,threading,time
        from pathlib import Path
        from dalton_core.cockpit_plane import CockpitPlane,CockpitJournal
        with tempfile.TemporaryDirectory() as td:
            plane=object.__new__(CockpitPlane);plane.clock=lambda:datetime.datetime.now(datetime.timezone.utc)
            plane.journal=CockpitJournal(Path(td)/'journal.sqlite')
            plane._jobs={};plane._jobs_lock=threading.Lock()
            def refused():raise settings.HostSettingsError('当前模型策略仍未完整同步，新调用暂缓')
            try:
                initial=plane._start_job('goal','shared-test',{'text':'Goal','request_id':'original'},refused)
                deadline=time.monotonic()+1
                while True:
                    public=plane.job('shared-test',initial['job_id'])
                    if public['status']=='failed':break
                    self.assertLess(time.monotonic(),deadline);time.sleep(.001)
                self.assertIn('尚未开始',public['display_message'])
                self.assertNotIn('HostSettingsError',public['display_message'])
                # The durable journal read must show the same guidance after
                # the live job cache is no longer used.
                while not plane.journal.rows("SELECT * FROM cockpit_jobs WHERE status='failed'"):
                    self.assertLess(time.monotonic(),deadline);time.sleep(.001)
                plane._jobs.clear();durable=plane.job('shared-test',initial['job_id'])
                self.assertEqual(durable['display_message'],public['display_message'])
                self.assertEqual(durable['request']['request_id'],'original')
            finally:plane.journal.close()

if __name__=='__main__':unittest.main()
