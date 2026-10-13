"""An explicit administrator CAS enables two; old/new projects share one path."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from dalton_core.shared_call_budget_policy import (updated_shared_limits,validate_shared_call_budget_policy,
    MEMO_MAINTENANCE_SCHEMA_VERSION,SharedCallBudgetPolicyError)
from dalton_core.call_budget import MEMO_MAINTENANCE_ROUNDS_FIELD as FIELD,resolve_run_budget,freeze_shared_budget_policy
from dalton_core.workspace_manager import set_shared_model_budget
from dalton_core.store import content_hash
from tests import test_global_model_budget_limits as fixtures


class SharedMemoMaintenancePolicyTests(unittest.TestCase):
    def test_old_schema_bytes_default_and_closed_cap(self):
        old=fixtures.legacy_policy();before=copy.deepcopy(old)
        self.assertEqual(validate_shared_call_budget_policy(old),before)
        for rounds in (True,0,3,2.0,'2'):
            with self.subTest(rounds=rounds),self.assertRaises(ValueError):
                updated_shared_limits(old,purpose='investment_memo',kind='run',budget={FIELD:rounds},
                    actor_ref='human:owner',updated_at='2026-10-13T08:00:00+00:00')
        value=fixtures.successor(old,'investment_memo',{FIELD:2},'run')
        self.assertEqual(value['schema_version'],MEMO_MAINTENANCE_SCHEMA_VERSION)
        self.assertEqual(value['prior_hash'],old['content_hash']);self.assertEqual(old,before)
        for purpose,kind in (('initial_screen','run'),('investment_memo','call')):
            with self.assertRaises(ValueError):fixtures.successor(old,purpose,{FIELD:2},kind)
        body={k:v for k,v in value.items() if k!='content_hash'};body['schema_version']=fixtures.LIMITS_SCHEMA_VERSION
        with self.assertRaises(ValueError):validate_shared_call_budget_policy({**body,'content_hash':content_hash(body)})

    def test_other_fields_edits_preserve_rounds_and_no_automatic_budget_raise(self):
        old=fixtures.successor(fixtures.legacy_policy(),'investment_memo',{'max_cost_usd':112},'run')
        new=fixtures.successor(old,'investment_memo',{FIELD:2},'run')
        after=fixtures.successor(new,'plan',{'max_output_tokens':12000})
        self.assertEqual(after['purpose_run_limits']['investment_memo'],{'max_cost_usd':112,FIELD:2})
        self.assertEqual(after['schema_version'],MEMO_MAINTENANCE_SCHEMA_VERSION)
        self.assertEqual(after['purpose_max_cost_usd'],old['purpose_max_cost_usd'])
        self.assertEqual(fixtures.successor(after,'investment_memo',{FIELD:2},'run'),after)

    def test_real_admin_cas_and_existing_new_project_resolution(self):
        f=fixtures.GlobalModelBudgetLimitsTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        manager=f.manager()
        result=set_shared_model_budget(manager.path,'owner@example.com','investment_memo',{FIELD:2},
                                      f.policy['content_hash'],kind='run')
        self.assertEqual(result['status'],'updated')
        for config in ({'shared_call_budget_policy_path':str(f.path)},
                       {'shared_call_budget_policy_path':str(f.path),'purpose_run_budgets':{'investment_memo':{FIELD:1}}}):
            effective=resolve_run_budget(config,'investment_memo',defaults={'max_cost_usd':112,'max_units':4})
            self.assertEqual(effective[FIELD],2)
        from dalton_core.workspace import WorkspaceError
        with self.assertRaises(WorkspaceError):
            set_shared_model_budget(manager.path,'owner@example.com','investment_memo',{FIELD:1},
                                   f.policy['content_hash'],kind='run')
        other=set_shared_model_budget(manager.path,'owner@example.com','plan',{'max_output_tokens':12000},
                                     result['policy']['content_hash'])
        self.assertEqual(other['policy']['purpose_run_limits']['investment_memo'][FIELD],2)

    def test_memo_only_policy_change_does_not_replace_extraction_execution_identity(self):
        from dalton_core.extraction_execution_identity import execution_config_binding
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'shared.json';old=fixtures.successor(fixtures.legacy_policy())
            path.write_text(json.dumps(old));raw={'shared_call_budget_policy_path':str(path)}
            before=execution_config_binding(freeze_shared_budget_policy(raw))
            path.write_text(json.dumps(fixtures.successor(old,'investment_memo',{FIELD:2},'run')))
            self.assertEqual(execution_config_binding(freeze_shared_budget_policy(raw)),before)

    def test_shared_two_round_save_wakes_without_revision_restart_generations(self):
        from types import SimpleNamespace
        from dalton_core.mission_investment_memo_lane import company_signature
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'shared.json';producer=root/'producer.json';verifier=root/'verifier.json'
            old=fixtures.successor(fixtures.legacy_policy());path.write_text(json.dumps(old))
            producer.write_text(json.dumps({'shared_call_budget_policy_path':str(path)}));verifier.write_text('{}')
            launcher=SimpleNamespace(model_config_path=producer,verifier_model_config_path=verifier)
            frozen={'mission':{'id':'mission:one','content_hash':'a'*64},'playbook':{'id':'playbook:one','content_hash':'b'*64},
                'company':{'company_ref':'company:synthetic'},'input_bindings':[]}
            before=company_signature(frozen,launcher)
            new=fixtures.successor(old,'investment_memo',{FIELD:2},'run');path.write_text(json.dumps(new))
            after=company_signature(frozen,launcher);self.assertNotEqual(after,before)
            other=fixtures.successor(new,'plan',{'max_output_tokens':12000});path.write_text(json.dumps(other))
            self.assertEqual(company_signature(frozen,launcher),after)
