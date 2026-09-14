"""Every option the CLI accepts must reach every path it can dispatch to.

--max-daily-cost-usd was threaded into the rehearse path and not into the live
one, so the rehearsal passed and the owner's actual command died with
"live() got an unexpected keyword argument". A rehearsal that does not
exercise the same signature as the real run is not a rehearsal.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_extraction_authority_chain.py"
ENTRY_POINTS = ("rehearse", "live", "build_chain")


class ChainOptionTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    def parser_options(self) -> set[str]:
        """The dest names argparse would produce for every --option."""

        names: set[str] = set()
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    names.add(str(arg.value)[2:].replace("-", "_"))
        return names

    def accepted_by(self, function: str) -> set[str]:
        node = next(n for n in self.tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == function)
        return {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}

    def test_the_budget_options_reach_both_paths(self):
        budget = {"max_daily_paid_calls", "max_alphaengine_calls_24h", "max_daily_cost_usd"}
        self.assertTrue(budget <= self.parser_options(), "an option was removed from the CLI")
        for entry in ENTRY_POINTS:
            self.assertTrue(
                budget <= self.accepted_by(entry),
                f"{entry} does not accept {sorted(budget - self.accepted_by(entry))}",
            )

    def test_rehearse_and_live_accept_the_same_options(self):
        # The whole point of a rehearsal is that it is the same call.
        self.assertEqual(
            self.accepted_by("rehearse") - {"target"},
            self.accepted_by("live"),
        )

    def test_writer_rejects_a_mission_budget_outside_bound_authorities(self):
        from dalton_core.writer_server import WriterServer, ValidationError
        server = object.__new__(WriterServer)
        old = {"max_daily_paid_calls": 1, "max_daily_cost_usd": 1,
               "max_alphaengine_calls_24h": 1}
        server._coverage_mission = SimpleNamespace(mission=lambda ref: {"budget": old})
        server._agenda = SimpleNamespace(mandate_version=lambda ref: {
            "constraints": {"research_budget": old}})
        server._store = SimpleNamespace(active_policy_version=lambda: SimpleNamespace(
            to_dict=lambda: {"policy": {"research_budget": old}}))
        params = {"mission_ref": "coverage-mission:test", "prior_version_ref": "v1",
                  "budget": {**old, "max_daily_cost_usd": 2},
                  "bindings": {"mandate_version": {"ref": "m1"}}}
        with self.assertRaisesRegex(ValidationError, "policy/mandate/constitution cascade"):
            server._op_create_coverage_mission(params)

    def test_owner_budget_operation_publishes_the_complete_chain(self):
        from dalton_core.writer_server import WriterServer
        server = object.__new__(WriterServer)
        old_budget = {"max_daily_paid_calls": 1, "max_daily_cost_usd": 1,
                      "max_alphaengine_calls_24h": 1, "pools": {
                          "coverage": .7, "event_response": .15,
                          "adhoc": .1, "maintenance": .05}}
        mission = {"mission_ref": "coverage-mission:test", "id": "coverage-mission-version:test:1",
            "version": 1, "content_hash": "a"*64, "budget": old_budget,
            "bindings": {"mandate_version": {"ref":"mandate-version:test:1"},
                         "constitution_version": {"ref":"constitution-version:test:1"}},
            **{k: [] for k in ("universe","research_questions","deliverables","source_plan")},
            "title":"t","objective":"o","industry_ref":"industry:test","autonomy":{}}
        made = {}
        server._coverage_mission = SimpleNamespace(active_mission=lambda ref: mission,
            create_mission=lambda ref, **kw: made.setdefault("mission", {"id":"coverage-mission-version:test:2", **kw}))
        server._agenda = SimpleNamespace(mandate_version=lambda ref: {"id":ref,"version":1,"mandate_ref":"mandate:test",
            "objective":"o","scope_refs":["scope:test"],"constraints":{"research_budget":old_budget},"success_criteria":{}},
            active_mandates=lambda: [],
            create_mandate=lambda ref, **kw: made.setdefault("mandate", {"id":"mandate-version:test:2","content_hash":"b"*64}))
        server._research_constitution = SimpleNamespace(constitution=lambda ref: {"id":ref,"version":1,
            "constitution_ref":"constitution:test","industry_ref":"industry:test","title":"t","bindings":{
                "mandate_version":{},"driver_pack_version":{},"governance_policy_version":{},"doctrine_pack_version":None,"weekly_brief_plan":None},"method":{}},
            active_constitution=lambda ref: {"id":"constitution-version:test:1","version":1,
                "constitution_ref":"constitution:test","industry_ref":"industry:test","title":"t","bindings":{
                    "mandate_version":{},"driver_pack_version":{},"governance_policy_version":{},"doctrine_pack_version":None,"weekly_brief_plan":None},"method":{}},
            publish_constitution=lambda ref, **kw: made.setdefault("constitution", {"id":"constitution-version:test:2","content_hash":"c"*64}))
        server._store = SimpleNamespace(active_policy_version=lambda: SimpleNamespace(to_dict=lambda: {
            "id":"policy-1","version":1,"policy_ref":"commit-gate","policy":{"allowed_verdicts":["pass"],"required_verification":True,"research_budget":old_budget}}),
            create_policy=lambda *a, **k: made.setdefault("policy", {"id":"policy-2","content_hash":"d"*64}))
        new = {"max_daily_paid_calls":2,"max_daily_cost_usd":2,"max_alphaengine_calls_24h":2}
        day_module = SimpleNamespace(synchronize_day_budget_policy=lambda *a, **kw: {
            "policy_version_id": "thesis-impact-day-budget-policy:production:2"})
        server._workspace = None
        server.db_path = "/tmp/dalton-budget-test/core.db"
        server._planner_model_config = None
        server._document_extraction_model_config = None
        with patch.dict(sys.modules, {"dalton_core.day_budget_configuration": day_module}):
            result = server._op_set_research_budget_authority_chain({"mission_ref":"coverage-mission:test",
                "budget":new,"expected_mission_hash":"a"*64,"actor_ref":"human:owner"})
        self.assertEqual(set(made), {"policy","mandate","constitution","mission"})
        self.assertEqual(made["mission"]["budget"], {**old_budget, **new})
        self.assertEqual(result["mission"], "coverage-mission-version:test:2")
        made.clear()
        with self.assertRaisesRegex(Exception, "mission changed"):
            server._op_set_research_budget_authority_chain({"mission_ref":"coverage-mission:test",
                "budget":new,"expected_mission_hash":"e"*64,"actor_ref":"human:owner"})
        self.assertEqual(made, {})

        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaisesRegex(Exception, "values are invalid"):
                server._op_set_research_budget_authority_chain({"mission_ref":"coverage-mission:test",
                    "budget":{**new, "max_daily_cost_usd":invalid},
                    "expected_mission_hash":"a"*64,"actor_ref":"human:owner"})
        self.assertEqual(made, {})

    def test_cockpit_budget_update_is_one_governance_request(self):
        from dalton_core.cockpit_plane import CockpitPlane
        plane = object.__new__(CockpitPlane)
        calls = []
        plane._governance = lambda login, operation, params, failure: (
            calls.append((login, operation, params, failure)) or
            {"status": "updated", "mission": "coverage-mission-version:test:2"})
        plane.journal = SimpleNamespace(record_event=lambda **kw: None)
        budget = {"max_daily_paid_calls": 9000, "max_daily_cost_usd": 180,
                  "max_alphaengine_calls_24h": 130}
        result = plane.set_research_budget("owner", {
            "mission_ref": "coverage-mission:test", "budget": budget,
            "expected_mission_hash": "a" * 64})
        self.assertEqual(result["mission"], "coverage-mission-version:test:2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "set_research_budget_authority_chain")
        self.assertEqual(calls[0][2], {
            "mission_ref": "coverage-mission:test", "budget": budget,
            "expected_mission_hash": "a" * 64})

    def test_retry_resumes_after_three_authorities_were_already_published(self):
        from dalton_core.writer_server import WriterServer
        server = object.__new__(WriterServer)
        budget = {"max_daily_paid_calls": 2, "max_daily_cost_usd": 2,
                  "max_alphaengine_calls_24h": 2}
        old_budget = {**budget, "max_daily_cost_usd": 1, "pools": {
            "coverage": .7, "event_response": .15, "adhoc": .1, "maintenance": .05}}
        mission = {"mission_ref":"coverage-mission:test","id":"coverage-mission-version:test:1",
            "version":1,"content_hash":"a"*64,"budget":old_budget,"title":"t","objective":"o",
            "industry_ref":"industry:test","universe":[],"research_questions":[],"deliverables":[],
            "source_plan":[],"autonomy":{},"bindings":{"mandate_version":{"ref":"mandate-version:test:1"},
            "constitution_version":{"ref":"constitution-version:test:1"}}}
        policy = {"id":"policy-2","content_hash":"b"*64,"version":2,"policy_ref":"commit-gate",
                  "policy":{"research_budget":budget}}
        mandate = {"id":"mandate-version:test:2","content_hash":"c"*64,"version":2,
            "mandate_ref":"mandate:test","objective":"o","scope_refs":["scope:test"],
            "constraints":{"research_budget":budget},"success_criteria":{}}
        bindings = {"mandate_version":{"ref":mandate["id"],"hash":mandate["content_hash"]},
            "governance_policy_version":{"ref":policy["id"],"hash":policy["content_hash"]},
            "driver_pack_version":{},"doctrine_pack_version":None,"weekly_brief_plan":None}
        constitution = {"id":"constitution-version:test:2","content_hash":"d"*64,"version":2,
            "constitution_ref":"constitution:test","industry_ref":"industry:test","title":"t",
            "bindings":bindings,"method":{}}
        made = []
        server._store = SimpleNamespace(active_policy_version=lambda:SimpleNamespace(to_dict=lambda:policy),
                                        create_policy=lambda *a,**k: self.fail("policy duplicated"))
        server._agenda = SimpleNamespace(mandate_version=lambda ref:mandate, active_mandates=lambda:[mandate],
                                         create_mandate=lambda *a,**k:self.fail("mandate duplicated"))
        server._research_constitution = SimpleNamespace(constitution=lambda ref:constitution,
            active_constitution=lambda ref:constitution,
            publish_constitution=lambda *a,**k:self.fail("constitution duplicated"))
        server._coverage_mission = SimpleNamespace(active_mission=lambda ref:mission,
            create_mission=lambda ref,**kw: made.append(kw) or {"id":"coverage-mission-version:test:2"})
        server._workspace=None; server.db_path="/tmp/dalton-budget-test/core.db"
        server._planner_model_config=None; server._document_extraction_model_config=None
        day_module=SimpleNamespace(synchronize_day_budget_policy=lambda *a,**kw:{"policy_version_id":"day:2"})
        with patch.dict(sys.modules,{"dalton_core.day_budget_configuration":day_module}):
            server._op_set_research_budget_authority_chain({"mission_ref":mission["mission_ref"],
                "budget":budget,"expected_mission_hash":"a"*64,"actor_ref":"human:owner"})
        self.assertEqual(len(made),1)
        self.assertEqual(made[0]["bindings"]["constitution_version"]["ref"],constitution["id"])


if __name__ == "__main__":
    unittest.main()
