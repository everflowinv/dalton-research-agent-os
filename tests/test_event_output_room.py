"""More event output room changes neither fees nor historical hold identity."""
import json
import unittest
from dalton_core.call_budget import default_call_budget, freeze_shared_budget_policy, resolve_call_budget
from dalton_core.event_judgement_cli import EventCockpitModel, event_model_contract_ref
from dalton_core.model_output_contract_hold import scope
from dalton_core.shared_call_budget_policy import load_shared_call_budget_policy
from dalton_core.store import content_hash
from dalton_core.workspace_manager import _write, set_shared_model_budget
from tests.test_global_model_budget_limits import legacy_policy

class EventOutputRoomTests(unittest.TestCase):
    def test_actual_event_model_budget_uses_catalog_with_legacy_constructor_fallback(self):
        from dalton_core import event_judgement_cli as cli
        model=object.__new__(EventCockpitModel)
        model.config={}
        model.max_input_tokens=cli.MAX_INPUT_TOKENS
        model.max_output_tokens=cli.MAX_OUTPUT_TOKENS
        model.max_cost_usd=cli.MAX_COST_USD
        model.timeout_seconds=cli.TIMEOUT_SECONDS
        effective=model.budget_for('event_judgement')
        self.assertEqual(effective,{'max_input_tokens':60000,'max_output_tokens':32768,
                                   'max_cost_usd':1.0,'timeout_seconds':180})
        self.assertEqual(default_call_budget('event_judgement_verifier')['max_output_tokens'],1500)

    def test_official_global_cas_reaches_existing_and_future_consumers_but_keeps_old_batch(self):
        from tests.test_workspace_manager import WorkspaceManagerTests
        fixture=WorkspaceManagerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        path=fixture.root/'shared.json';old=legacy_policy();path.write_text(json.dumps(old));path.chmod(0o600)
        fixture.config['shared_call_budget_policy_path']=str(path);_write(fixture.path,fixture.config)
        first=set_shared_model_budget(fixture.path,'owner@example.com','event_judgement',
                                      {'max_output_tokens':8192},old['content_hash'])['policy']
        configs=[{'shared_call_budget_policy_path':str(path),'purpose_call_budgets':
                   {'event_judgement':{'max_output_tokens':4000}}},
                 {'shared_call_budget_policy_path':str(path)},
                 {'shared_call_budget_policy_path':str(path)}]
        frozen=freeze_shared_budget_policy(configs[0])
        # A default alone cannot silently override the user's existing shared cap.
        self.assertEqual(resolve_call_budget(configs[0],'event_judgement',defaults=default_call_budget('event_judgement'))['max_output_tokens'],8192)
        saved=set_shared_model_budget(fixture.path,'owner@example.com','event_judgement',
                                     {'max_output_tokens':32768},first['content_hash'])['policy']
        self.assertEqual(saved,load_shared_call_budget_policy(path))
        for config in configs:
            self.assertEqual(resolve_call_budget(config,'event_judgement',defaults=default_call_budget('event_judgement'))['max_output_tokens'],32768)
        self.assertEqual(resolve_call_budget(frozen,'event_judgement',defaults=default_call_budget('event_judgement'))['max_output_tokens'],8192)
        for key in ('default_max_cost_usd','purpose_max_cost_usd','purpose_run_limits'):
            self.assertEqual(first[key],saved[key])
        self.assertEqual(saved['purpose_call_limits']['event_judgement'],{'max_output_tokens':32768})

    def test_new_cap_rekeys_exact_hold_and_event_contract_without_mutating_old_record(self):
        profile={'profile_version_ref':'model-profile-version:grok:4','content_hash':'a'*64,
                 'provider':'xai','model':'grok-4.7','adapter_ref':'adapter:broker:0.1'}
        budget={'max_input_tokens':60000,'max_output_tokens':8192,'max_total_tokens':68192,
                'max_cost_usd':1.0,'max_seconds':180}
        old=scope(profile,policy_ref='policy:event:80',policy_hash='b'*64,purpose='event_judgement',budget=budget,thinking_level='low')
        old_bytes=json.dumps(old,sort_keys=True)
        new=scope(profile,policy_ref='policy:event:80',policy_hash='b'*64,purpose='event_judgement',budget={**budget,'max_output_tokens':32768,'max_total_tokens':92768},thinking_level='low')
        self.assertNotEqual(content_hash(old),content_hash(new))
        old_call={'max_input_tokens':60000,'max_output_tokens':8192,'max_cost_usd':1.0,'timeout_seconds':180}
        self.assertNotEqual(event_model_contract_ref(old_call),event_model_contract_ref({**old_call,'max_output_tokens':32768}))
        self.assertEqual(json.dumps(old,sort_keys=True),old_bytes)

if __name__=='__main__':unittest.main()
