from __future__ import annotations

import json
import unittest
from dataclasses import replace

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.investment_memo_contract import CHECK_REFS, PRODUCER_GROUPS, verified_body_hash
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.store import DaltonStore
from dalton_core.writer_server import WriterServer
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests import test_cockpit_model_fallback as fallback_fixtures

ChainAdapter = fallback_fixtures.ChainAdapter


class JsonAdapter(ChainAdapter):
    def __init__(self, value): super().__init__({}); self.value = value
    def execute(self, work, route, profile):
        invocation, result = super().execute(work, route, profile)
        return invocation, replace(result, outputs={"text": json.dumps(self.value)})


class RealInvestmentMemoDecisionTests(unittest.TestCase):
    def setUp(self):
        self.chain = fallback_fixtures.CockpitChainTests("runTest"); self.chain.setUp()
        self.addCleanup(self.chain.doCleanups)
        self.store = DaltonStore(":memory:"); self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        params = mission_params(state)
        params["autonomy"] = {**params["autonomy"], "may_write": sorted(set(
            params["autonomy"]["may_write"]) | {"deliverable", "stage_record"})}
        ref = params.pop("mission_ref")
        self.missions = CoverageMissionAuthority(self.store)
        self.mission = self.missions.create_mission(ref, **params)
        self.playbook = state["playbook"]
        self.company = self.mission["universe"][0]["company_ref"]
        for stage in ("initial_screen", "deep_insight_gate", "industry_model", "company_model"):
            self.missions.record_stage(mission_version_ref=self.mission["id"],
                mission_version_hash=self.mission["content_hash"], company_ref=self.company,
                stage_ref=stage, status="entered", evidence_refs=[f"evidence:{stage}"],
                rationale="fixture", actor_ref="human:owner", idempotency_key=f"enter:{stage}")
            self.missions.record_stage(mission_version_ref=self.mission["id"],
                mission_version_hash=self.mission["content_hash"], company_ref=self.company,
                stage_ref=stage, status="gate_passed", evidence_refs=[f"evidence:{stage}"],
                rationale="fixture", actor_ref="human:owner", idempotency_key=f"pass:{stage}")

    def test_real_store_scheduler_router_publish_and_approve(self):
        producers = []
        for index, group in enumerate(PRODUCER_GROUPS):
            call = self.chain._model(ChainAdapter({}), policy_version_ref=self.chain.chain_policy).call(
                purpose="investment_memo", request_id=f"memo-real-{index}", prompt=group,
                mission=self.mission)
            producers.append({"group": group, **{key: call[key] for key in (
                "work_order_ref", "route_decision_ref", "result_envelope_ref", "invocation_ref")}})
        questions = [{"question_ref": f"memo_q{i:02d}", "question": question,
                      "answer": "supported", "refs": ["evidence:fixture"],
                      "unknown": False, "falsifier": "would be false if contradicted"}
                     for i, question in enumerate(self.playbook["key_questions"], 1)]
        sections = [{"title": title, "body": "Supported qualitative analysis.",
                     "claim_refs": [], "numbers": [], "gaps": []}
                    for title in self.playbook["deliverable_templates"]["investment_memo"]]
        material = {"kind": "investment_memo", "subject_ref": self.company,
            "mission_version_ref": self.mission["id"], "mission_version_hash": self.mission["content_hash"],
            "playbook_version_ref": self.playbook["id"], "playbook_version_hash": self.playbook["content_hash"],
            "template_ref": "investment_memo", "summary": "Supported memo", "sections": sections,
            "gaps": [], "gate": {"key_questions": questions,
                                   "input_bindings": [{"ref": "evidence:fixture", "hash": "c"*64,
                                                       "kind": "evidence"}]}}
        digest = verified_body_hash(material)
        verifier_value = {"verdict": "pass", "verified_body_hash": digest, "finding_codes": []}
        verifier = self.chain._model(JsonAdapter(verifier_value),
            policy_version_ref=self.chain.verifier_policy, slots=self.chain.verifier_slots).call(
                purpose="investment_memo_verifier", request_id="memo-real-verify",
                prompt=json.dumps(verifier_value), mission=self.mission,
                producer_route_decision_refs=[row["route_decision_ref"] for row in producers])
        gate = {"schema_version": "investment-memo-gate-0.1", "passed": True,
            "verified_body_hash": digest, "key_questions": questions,
            "input_bindings": material["gate"]["input_bindings"], "producer_calls": producers,
            "checks": [{"check_ref": ref, "status": "pass", "reason": "verified",
                        "evidence_refs": ["evidence:fixture"]} for ref in CHECK_REFS],
            "verifier": {"verdict": "pass", "finding_codes": [],
                "producer_route_decision_refs": [row["route_decision_ref"] for row in producers],
                **{key: verifier[key] for key in ("work_order_ref", "route_decision_ref",
                                                  "result_envelope_ref", "invocation_ref")}}}
        memo = MissionDeliverableAuthority(self.store).publish(kind="investment_memo",
            subject_ref=self.company, mission=self.mission, playbook=self.playbook,
            template_ref="investment_memo", sections=sections, summary="Supported memo", gaps=[],
            model_invocation_refs=[row["work_order_ref"] for row in producers] + [verifier["work_order_ref"]],
            gate=gate, actor_ref=self.mission["autonomy"]["automation_principal"])
        server = object.__new__(WriterServer); server._store = self.store
        server._coverage_mission = self.missions
        server._research_playbook = type("P", (), {"playbook": lambda _, ref: self.playbook})()
        server._mission_deliverables = MissionDeliverableAuthority(self.store)
        server._scheduler = self.chain._model(ChainAdapter({}), policy_version_ref=self.chain.chain_policy)
        # Use the same real Scheduler authority that executed all five calls.
        from dalton_core.scheduler import Scheduler
        server._scheduler = Scheduler(self.chain.root / "scheduler.sqlite"); self.addCleanup(server._scheduler.close)
        server._model_router_db = lambda: self.chain.router_db
        result = WriterServer._op_decide_investment_memo(server, {
            "memo_version_ref": memo["id"], "memo_version_hash": memo["content_hash"],
            "decision": "approve", "reason": "owner approved", "actor_ref": "human:owner"})
        self.assertIsNotNone(result["active_coverage_record_ref"])
        state = self.missions.current_stage_state(self.mission["mission_ref"], self.company)
        self.assertEqual(state["stages"]["investment_memo"]["status"], "gate_passed")
        self.assertEqual(state["stages"]["active_coverage"]["status"], "entered")


if __name__ == "__main__": unittest.main()
