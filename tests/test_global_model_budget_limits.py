"""Global bounds reach real consumers without rewriting a running batch."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from dalton_core.call_budget import resolve_call_budget, resolve_run_budget, freeze_shared_budget_policy
from dalton_core.shared_call_budget_policy import (SCHEMA_VERSION, LIMITS_SCHEMA_VERSION,
    validate_shared_call_budget_policy, updated_shared_limits, SharedCallBudgetPolicyError)
from dalton_core.store import content_hash
from dalton_core.workspace_manager import set_shared_model_budget, request_set_shared_model_budget
from dalton_core.workspace import WorkspaceError
from dalton_core.cockpit_model import CockpitModel
from dalton_core.model_budget_configuration import call_budget_view
from tests import test_workspace_manager as manager_fixture
from tests import test_model_selection as model_fixture
from tests import test_metric_discovery_extraction as discovery_fixture


def legacy_policy():
    body = dict(schema_version=SCHEMA_VERSION, default_max_cost_usd=1.0,
        purpose_max_cost_usd={"plan": 1.5}, revision=1, prior_hash=None,
        updated_at="2026-01-01T00:00:00+00:00", actor_ref="human:owner")
    return {**body, "content_hash": content_hash(body)}


def successor(policy, purpose="document_extraction", budget=None, kind="call"):
    return updated_shared_limits(policy, purpose=purpose, kind=kind,
        budget=budget or {"max_output_tokens": 32768}, actor_ref="human:owner",
        updated_at="2026-10-10T23:00:00+00:00")


class GlobalModelBudgetLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.path = self.root / "shared.json"
        self.policy = legacy_policy(); self.install(self.policy)

    def install(self, policy):
        self.path.write_text(json.dumps(policy)); self.path.chmod(0o600)

    def test_old_policy_hash_shape_and_hot_cost_semantics_are_unchanged(self):
        old = legacy_policy()
        self.assertEqual(validate_shared_call_budget_policy(old), old)
        config = {"shared_call_budget_policy_path": str(self.path)}
        self.assertEqual(freeze_shared_budget_policy(config), config)
        defaults = dict(max_input_tokens=10, max_output_tokens=20, max_cost_usd=8, timeout_seconds=30)
        first = resolve_call_budget(config, "test", defaults=defaults)
        self.install(successor(old, "test", {"max_cost_usd": 0.5}))
        self.assertEqual(resolve_call_budget(config, "test", defaults=defaults)["max_cost_usd"], 0.5)
        self.assertEqual(first["max_output_tokens"], 20)

    def test_closed_fields_bad_types_and_duplicate_cost_authority_refused(self):
        new = successor(self.policy)
        for patch_wire in ({"purpose_call_limits": {"test": {"max_output_tokens": True}}},
                           {"purpose_call_limits": {"test": {"max_cost_usd": 2}}},
                           {"purpose_run_limits": {"test": {"max_units": False}}},
                           {"surprise": 1}):
            body = {k:v for k,v in {**new, **patch_wire}.items() if k != "content_hash"}
            with self.subTest(patch_wire=patch_wire), self.assertRaises(SharedCallBudgetPolicyError):
                validate_shared_call_budget_policy({**body, "content_hash": content_hash(body)})

    def test_partial_merge_preserves_siblings_other_purposes_cost_and_audit(self):
        first = successor(self.policy, "plan", {"max_output_tokens":16384,"max_input_tokens":250000,"timeout_seconds":300})
        second = successor(first, "document_extraction", {"max_output_tokens":32768})
        third = successor(second, "plan", {"max_output_tokens":32768})
        self.assertEqual(third["purpose_call_limits"]["plan"], {"max_output_tokens":32768,"max_input_tokens":250000,"timeout_seconds":300})
        self.assertEqual(third["purpose_call_limits"]["document_extraction"], {"max_output_tokens":32768})
        self.assertEqual(third["purpose_max_cost_usd"], self.policy["purpose_max_cost_usd"])
        self.assertEqual(third["default_max_cost_usd"], 1.0)
        self.assertEqual(third["prior_hash"], second["content_hash"])
        self.assertEqual(third["schema_version"], LIMITS_SCHEMA_VERSION)
        self.assertEqual(successor(third, "plan", {"max_output_tokens":32768}), third)

    def test_shared_call_fields_override_local_and_product_floor_survives(self):
        self.install(successor(self.policy, "plan", {"max_input_tokens":250000,"max_output_tokens":16384,"timeout_seconds":300}))
        config = {"shared_call_budget_policy_path":str(self.path),"purpose_call_budgets":{"plan":{"max_output_tokens":4000}}}
        result = resolve_call_budget(config,"plan",defaults=dict(max_input_tokens=80000,max_output_tokens=4000,max_cost_usd=1,timeout_seconds=300))
        self.assertEqual(result,dict(max_input_tokens=250000,max_output_tokens=16384,max_cost_usd=1.5,timeout_seconds=300))
        self.install(successor(self.policy,"investment_memo",{"max_input_tokens":100,"max_output_tokens":100,"timeout_seconds":30}))
        memo = resolve_call_budget(config,"investment_memo",defaults=dict(max_input_tokens=100,max_output_tokens=100,max_cost_usd=1,timeout_seconds=30))
        self.assertEqual(memo["max_input_tokens"],600000)
        self.assertEqual(memo["max_output_tokens"],16000)
        self.assertEqual(memo["max_cost_usd"],14)

    def test_real_three_extraction_builders_frozen_batch_replay_and_next_batch(self):
        from dalton_core.document_extraction import build_work as prose
        from dalton_core.document_numeric_extraction import build_work as numeric
        from dalton_core.metric_discovery_extraction import build_work as discovery
        from tests import test_document_numeric_extraction as numeric_fixture
        new = successor(self.policy)
        new = successor(new,"document_numeric_extraction",{"max_output_tokens":8192})
        new = successor(new,"metric_discovery_extraction",{"max_output_tokens":4096})
        self.install(new)
        raw = {"shared_call_budget_policy_path":str(self.path)}
        frozen = freeze_shared_budget_policy(raw)
        context = {**discovery_fixture.CONTEXT,"offset":0,"end":400}
        numeric_context = {**discovery_fixture.CONTEXT,**numeric_fixture.CONTEXT}
        builds = [lambda cfg: prose(context,model_config=cfg),
                  lambda cfg: numeric(numeric_context,numeric_fixture.SLOTS,model_config=cfg),
                  lambda cfg: discovery(discovery_fixture.CONTEXT,model_config=cfg)]
        old = [fn(frozen) for fn in builds]
        self.install(successor(new,budget={"max_output_tokens":65536}))
        replay = [fn(frozen) for fn in builds]
        self.assertEqual([w.to_dict() for w in old],[w.to_dict() for w in replay])
        after = builds[0](freeze_shared_budget_policy(raw))
        self.assertNotEqual(after.id,old[0].id)
        self.assertEqual(old[0].budget["max_output_tokens"],32768)
        self.assertEqual(after.budget["max_output_tokens"],65536)
        self.assertEqual(after.budget["max_cost_usd"],1.0)

    def test_run_limits_consumed_separately_and_frozen(self):
        self.install(successor(self.policy,"event_judgement",{"max_events":12,"max_events_per_company":5},"run"))
        config = freeze_shared_budget_policy({"shared_call_budget_policy_path":str(self.path),"purpose_run_budgets":{"event_judgement":{"max_events":3}}})
        defaults = {"max_events":8,"max_events_per_company":3}
        before=resolve_run_budget(config,"event_judgement",defaults=defaults)
        self.assertEqual(before,{"max_events":12,"max_events_per_company":5})
        self.install(successor(self.policy,"event_judgement",{"max_events":2},"run"))
        self.assertEqual(resolve_run_budget(config,"event_judgement",defaults=defaults),before)
        self.assertEqual(resolve_run_budget({"shared_call_budget_policy_path":str(self.path)},"event_judgement",defaults=defaults)["max_events"],2)

    def manager(self):
        fixture=manager_fixture.WorkspaceManagerTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.config["shared_call_budget_policy_path"]=str(self.path);fixture.save()
        return fixture

    def test_manager_global_cas_receipt_and_unconfigured_run_field_refusal(self):
        fixture=self.manager()
        result=set_shared_model_budget(fixture.path,"owner@example.com","document_extraction",{"max_output_tokens":32768},self.policy["content_hash"])
        self.assertEqual(result["status"],"updated")
        self.assertEqual(result["effective_at"],"next_batch")
        self.assertTrue((fixture.root/"fleet"/"shared-call-budget-revisions"/(result["policy"]["content_hash"]+".json")).is_file())
        with self.assertRaises(WorkspaceError):
            set_shared_model_budget(fixture.path,"owner@example.com","document_extraction",{"max_output_tokens":60000},self.policy["content_hash"])
        with self.assertRaises(WorkspaceError):
            set_shared_model_budget(fixture.path,"owner@example.com","event_judgement",{"max_units":5},result["policy"]["content_hash"],kind="run")
        self.assertEqual(json.loads(self.path.read_text()),result["policy"])

    def test_public_typed_identity_and_explicit_admin_can_save_but_raw_string_cannot(self):
        from dalton_core.shared_access_identity import SharedLogin,shared_actor
        fixture=self.manager()
        fixture.config.update(admin_logins=["admin@example.test"],shared_gateway={"secret_file":str(self.root/"gateway-secret"),"public_origin":"https://research.example.test","principal":"test-shared"})
        # Use the actual closed binding validator's shape.
        from dalton_core.shared_gateway_auth import validate_config
        binding=fixture.config["shared_gateway"]
        fixture.config["shared_gateway"]=validate_config(binding)
        fixture.save()
        login=SharedLogin("test-shared")
        result=set_shared_model_budget(fixture.path,login,"ask",{"max_output_tokens":9000},self.policy["content_hash"])
        self.assertEqual(result["policy"]["actor_ref"],shared_actor(login))
        with self.assertRaises(PermissionError):
            set_shared_model_budget(fixture.path,str(login),"ask",{"max_output_tokens":8000},result["policy"]["content_hash"])
        admin=set_shared_model_budget(fixture.path,"admin@example.test","ask",{"max_output_tokens":10000},result["policy"]["content_hash"])
        self.assertEqual(admin["status"],"updated")

    def test_real_cockpit_budget_view_matches_runtime_and_refreshes_next_job(self):
        fixture=model_fixture.CockpitModelPageTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        plane=fixture.plane(with_model_config=True)
        source=plane.config.model_config_path
        raw=dict(routing_policy_ref="model-routing-policy-version:test:1", credential_slot_refs=["credential-slot:test"],
            model_router_db=str(fixture.root/"model-router.sqlite"), broker_socket=str(fixture.root/"broker.sock"),
            broker_auth_key=str(fixture.root/"auth"),broker_client_id="client:dalton",expected_agent_id="test",
            budget_db=str(fixture.root/"budget.sqlite"),budget_policy_ref="budget-policy:test:1",
            shared_call_budget_policy_path=str(self.path))
        source.write_text(json.dumps(raw))
        # No provider call: retain actual model construction but never dispatch.
        plane._model_injected=False;plane._model=None;plane._model_factory=lambda cfg:CockpitModel(cfg,scheduler_db=plane.config.scheduler_db,max_cost_usd=2)
        self.install(successor(self.policy,"ask",{"max_output_tokens":8192,"timeout_seconds":240}))
        first=plane._model_instance()
        view=call_budget_view(plane.config.state_dir,"ask",cockpit_model_config_path=source)
        self.assertEqual(view["effective"],first.budget_for("ask"))
        self.assertEqual(first.budget_for("ask")["max_output_tokens"],8192)
        self.install(successor(json.loads(self.path.read_text()),"ask",{"max_output_tokens":16384}))
        second=plane._model_instance()
        self.assertIsNot(first,second)
        self.assertEqual(first.budget_for("ask")["max_output_tokens"],8192)
        self.assertEqual(second.budget_for("ask")["max_output_tokens"],16384)

    def test_seed_one_cas_preserves_higher_limits_costs_old_bytes_and_refuses_stale(self):
        from dalton_core.workspace_manager import seed_shared_model_limits
        fixture=self.manager();oldbytes=self.path.read_bytes()
        first=set_shared_model_budget(fixture.path,"owner@example.com","document_extraction",{"max_output_tokens":65536},self.policy["content_hash"])
        result=seed_shared_model_limits(fixture.path,"owner@example.com",call_limits={"document_extraction":{"max_output_tokens":32768},"plan":{"max_input_tokens":250000,"max_output_tokens":16384}},run_limits={"event_judgement":{"max_events":12}},expected_hash=first["policy"]["content_hash"])
        self.assertEqual(result["policy"]["purpose_call_limits"]["document_extraction"]["max_output_tokens"],65536)
        self.assertEqual(result["policy"]["purpose_max_cost_usd"],self.policy["purpose_max_cost_usd"])
        self.assertEqual(result["policy"]["default_max_cost_usd"],1.0)
        prior_path=fixture.root/"fleet"/"shared-call-budget-revisions"/(first["policy"]["content_hash"]+".json")
        self.assertEqual(json.loads(prior_path.read_text()),first["policy"])
        self.assertEqual(json.loads(oldbytes),self.policy)
        with self.assertRaises(WorkspaceError):
            seed_shared_model_limits(fixture.path,"owner@example.com",call_limits={"plan":{"max_output_tokens":65536}},run_limits={},expected_hash=first["policy"]["content_hash"])
        self.assertEqual(json.loads(self.path.read_text()),result["policy"])

    def test_public_cookie_save_reaches_real_manager_child_and_persistent_policy(self):
        from types import MethodType
        from tests import test_public_sharing_gateway as gateway_fixture
        network=gateway_fixture.SharingNetworkTests();network.setUp();self.addCleanup(network.tearDown)
        fixture=self.manager();fixture.config["shared_gateway"]=network.apps[0].config.shared_gateway;fixture.save()
        plane_fixture=model_fixture.CockpitModelPageTests();plane_fixture.setUp();self.addCleanup(plane_fixture.doCleanups)
        plane=plane_fixture.plane(with_model_config=False)
        plane.config=replace(plane.config,workspace_manager_config_path=fixture.path)
        def actual_save(app,action,login,session,csrf,body):
            value=app._review_body(session,csrf,body)
            if action!="model_budget":raise ValueError("unexpected test action")
            return plane.set_call_budget(login,value)
        network.apps[0].post_cockpit=MethodType(actual_save,network.apps[0])
        cookie=network.bootstrap();csrf,backend_cookie=network.manifest("legacy",cookie)
        status,_,body=network.request("/w/legacy/v1/cockpit/model_budget",method="POST",cookies=cookie+"; "+backend_cookie,
            headers={"X-Dalton-CSRF":csrf},data={"purpose":"ask","kind":"call","budget":{"max_output_tokens":8192},"expected_shared_policy_hash":self.policy["content_hash"]})
        self.assertEqual(status,200,body.decode())
        policy=json.loads(self.path.read_text())
        from dalton_core.shared_access_identity import SharedLogin,shared_actor
        self.assertEqual(policy["actor_ref"],shared_actor(SharedLogin("public")))
        self.assertEqual(policy["purpose_call_limits"]["ask"],{"max_output_tokens":8192})
        self.assertEqual(json.loads(body)["policy"],policy)

    def test_task_count_cannot_exceed_shared_run_cap_but_old_local_semantics_stay(self):
        from dalton_core.call_budget import bound_run_argument
        self.install(successor(self.policy,"dossier",{"max_units":2},"run"))
        config=freeze_shared_budget_policy({"shared_call_budget_policy_path":str(self.path)})
        self.assertEqual(bound_run_argument(config,"dossier","max_units",9,3),2)
        self.assertEqual(bound_run_argument(config,"dossier","max_units",1,3),1)
        self.assertEqual(bound_run_argument({},"dossier","max_units",9,3),9)
        self.assertEqual(resolve_run_budget(config,"dossier",defaults={"max_units":3,"max_cost_usd":56},max_units=9)["max_units"],2)

    def test_shared_plan_view_does_not_require_restart_but_local_plan_still_does(self):
        from dalton_core.model_budget_configuration import _service_budget_view
        from tests import test_bounded_planner_driver as planner_fixture
        from dataclasses import asdict
        raw=json.loads(json.dumps(asdict(planner_fixture.StalledLoopTests()._config(self.root)),default=str))
        service=self.root/"service.json"
        self.install(successor(self.policy,"plan",{"max_output_tokens":16384,"max_input_tokens":250000}))
        raw["shared_call_budget_policy_path"]=str(self.path)
        service.write_text(json.dumps({"bounded_planner":{"config":raw}}))
        with patch("dalton_core.model_budget_configuration.service_config_path",return_value=service):
            view=_service_budget_view(self.root,self.root,"plan",{},"call")
            self.assertFalse(view["requires_restart"])
            self.assertEqual(view["effective"]["max_output_tokens"],16384)
            self.assertEqual(view["shared_limits"]["effective_at"],"next_batch")
            raw.pop("shared_call_budget_policy_path")
            service.write_text(json.dumps({"bounded_planner":{"config":raw}}))
            self.assertTrue(_service_budget_view(self.root,self.root,"plan",{},"call")["requires_restart"])

    def test_ui_global_output_save_uses_shared_cas_without_copying_local_overrides(self):
        from tests import test_admin_model_settings_behavior as admin_fixture
        data=admin_fixture.responses()
        row=data["/v1/cockpit/models"]["body"]["purposes"][0]
        row["call_budget"]={"editable":True,"effective":{"max_input_tokens":120000,"max_output_tokens":32768,"timeout_seconds":600},
            "config_hash":"a"*64,"overrides":{"max_cost_usd":999,"max_input_tokens":100},
            "shared_limits":{"scope":"all_workspaces","policy_hash":"b"*64,"overrides":{"max_output_tokens":32768}}}
        out=admin_fixture.ModelSettingsBehaviorTests().run_js(r'''
const e=editor();out.text=e.textContent;
const input=e.querySelectorAll("input").find(i=>i.attrs["aria-label"]==="起草的单次输出上限");input.value="65536";
const reason=e.querySelectorAll("textarea").find(t=>(t.attrs["aria-label"]||"").startsWith("保存回答长度上限"));reason.value="全局长度调整";
byText(e,"保存回答长度上限（全部研究项目）").click();await settle();out.posts=posts;
''',data=data)
        self.assertIn("下一批任务采用",out["text"])
        self.assertIn("新建项目",out["text"])
        payload=out["posts"][0]["body"]["payload"]
        self.assertEqual(payload["budget"],{"max_output_tokens":65536})
        self.assertEqual(payload["expected_shared_policy_hash"],"b"*64)
        self.assertNotIn("expected_config_hash",payload)

if __name__=="__main__":unittest.main()
