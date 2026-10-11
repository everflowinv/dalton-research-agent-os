"""Exact proved long output tasks gain room without changing money or history."""
import json,unittest
from dalton_core.call_budget import default_call_budget,resolve_call_budget,freeze_shared_budget_policy
from dalton_core.shared_call_budget_policy import load_shared_call_budget_policy
from dalton_core.workspace_manager import set_shared_model_budget,_write
from dalton_core.model_output_contract_hold import scope
from dalton_core.store import content_hash
from tests.test_global_model_budget_limits import legacy_policy

class ResearchOutputRoomTests(unittest.TestCase):
    def test_two_proved_producers_gain_output_room_only(self):
        for purpose,seconds in [('industry_framework',180),('model_spec',300)]:
            budget=default_call_budget(purpose)
            self.assertEqual(budget,{'max_input_tokens':120000,'max_output_tokens':32768,
                                    'max_cost_usd':1.0,'timeout_seconds':seconds})
        self.assertEqual(default_call_budget('industry_framework_verifier')['max_output_tokens'],3000)

    def test_real_global_cas_reaches_existing_and_future_consumers_without_rewriting_batch(self):
        from tests.test_workspace_manager import WorkspaceManagerTests
        fixture=WorkspaceManagerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        path=fixture.root/'approved-shared.json';old=legacy_policy()
        path.write_text(json.dumps(old));path.chmod(0o600)
        fixture.config['shared_call_budget_policy_path']=str(path);_write(fixture.path,fixture.config)
        current=old
        # Three differently installed consumers, including a new workspace,
        # all bind the single approved shared policy rather than copying caps.
        configs=[{'shared_call_budget_policy_path':str(path),'purpose_call_budgets':
                 {'industry_framework':{'max_output_tokens':3000},'model_spec':{'max_output_tokens':16384}}},
                 {'shared_call_budget_policy_path':str(path)},
                 {'shared_call_budget_policy_path':str(path)}]
        for purpose in ('industry_framework','model_spec'):
            result=set_shared_model_budget(fixture.path,'owner@example.com',purpose,
                {'max_output_tokens':32768},current['content_hash'])
            current=result['policy']
        self.assertEqual(old['purpose_max_cost_usd'],current['purpose_max_cost_usd'])
        self.assertEqual(old['default_max_cost_usd'],current['default_max_cost_usd'])
        self.assertEqual(current,load_shared_call_budget_policy(path))
        for config in configs:
            for purpose in ('industry_framework','model_spec'):
                self.assertEqual(resolve_call_budget(config,purpose,defaults=default_call_budget(purpose))['max_output_tokens'],32768)
        # A real 0.2 batch snapshot is frozen; later global edits do not revise it.
        batch=freeze_shared_budget_policy(configs[0])
        set_shared_model_budget(fixture.path,'owner@example.com','model_spec',
            {'max_output_tokens':40000},current['content_hash'])
        self.assertEqual(resolve_call_budget(batch,'model_spec',defaults=default_call_budget('model_spec'))['max_output_tokens'],32768)
        self.assertEqual(resolve_call_budget(configs[0],'model_spec',defaults=default_call_budget('model_spec'))['max_output_tokens'],40000)

    def test_changed_output_cap_has_a_distinct_exact_hold_scope(self):
        profile={'profile_version_ref':'model-profile-version:grok:4','content_hash':'a'*64,
                 'provider':'xai','model':'grok-4.7','adapter_ref':'adapter:broker:0.1'}
        for purpose,old_cap in [('industry_framework',3000),('model_spec',16384)]:
            budget={'max_input_tokens':120000,'max_output_tokens':old_cap,'max_total_tokens':120000+old_cap,'max_cost_usd':1.,'max_seconds':300}
            old=scope(profile,policy_ref='model-routing-policy-version:test:1',policy_hash='b'*64,purpose=purpose,budget=budget,thinking_level='medium')
            updated=scope(profile,policy_ref='model-routing-policy-version:test:1',policy_hash='b'*64,purpose=purpose,budget={**budget,'max_output_tokens':32768,'max_total_tokens':152768},thinking_level='medium')
            self.assertNotEqual(content_hash(old),content_hash(updated))
            self.assertEqual(old['budget']['max_output_tokens'],old_cap)

if __name__=='__main__':unittest.main()
