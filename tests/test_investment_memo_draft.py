import json
import unittest
from unittest.mock import patch

from dalton_core.investment_memo_cli import run_memo
from dalton_core.investment_memo_contract import CHECK_REFS
from dalton_core.investment_memo_draft import (
    GROUPS, InvestmentMemoDraftError, build_group_prompt, parse_group_output, verify_memo,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params


TITLES = ["Key Information", "Executive Summary", "S1 company", "S2 industry",
          "S3 thesis", "S4 风险与 Anti-thesis", "S5 风险回报", "S6 management",
          "S7 expectations", "S8 valuation", "S9 monitoring", "附录"]
QUESTIONS = [f"playbook question {i}" for i in range(1, 13)]


def group_payload(section_titles, questions):
    return {"sections": [{"title": title, "body": "supported prose", "claim_refs": ["claim-version:1"],
                          "numbers": [], "gaps": []} for title in section_titles],
            "key_questions": [{"question_ref": q["question_ref"], "question": q["question"],
                               "answer": "supported answer", "refs": ["claim-version:1"],
                               "unknown": False, "falsifier": "a contrary filing"} for q in questions]}


class FakeModel:
    def __init__(self, responses, prefix="producer"):
        self.responses = iter(responses)
        self.calls = []
        self.prefix = prefix

    def call(self, *, producer_route_decision_refs=(), **kwargs):
        kwargs["producer_route_decision_refs"] = tuple(producer_route_decision_refs)
        self.calls.append(kwargs)
        response = next(self.responses)
        return {"text": json.dumps(response), "work_order_ref": f"work:{self.prefix}:{len(self.calls)}",
                "route_decision_ref": f"route-decision:{self.prefix}:{len(self.calls)}",
                "result_envelope_ref": f"result:{self.prefix}:{len(self.calls)}",
                "invocation_ref": f"invocation:{self.prefix}:{len(self.calls)}", "cost_micros": 10}


class InvestmentMemoDraftTests(unittest.TestCase):
    def test_prompt_keeps_complete_material_and_exact_contract(self):
        rows = [{"ref": "claim-version:1", "kind": "claim", "text": "x" * 12000}]
        questions = [{"question_ref": "memo_q01", "question": QUESTIONS[0]}]
        prompt = build_group_prompt(group="identity_background", section_titles=TITLES[:4],
                                    questions=questions, material=rows, company={"company_ref": "company:ACN"})
        self.assertIn("x" * 12000, prompt)
        self.assertIn(TITLES[3], prompt)
        self.assertIn(QUESTIONS[0], prompt)

    def test_parser_refuses_unknown_ref_and_missing_section(self):
        questions = [{"question_ref": "memo_q01", "question": QUESTIONS[0]}]
        value = group_payload(TITLES[:4], questions)
        value["sections"][0]["claim_refs"] = ["artifact:not-a-claim"]
        with self.assertRaises(InvestmentMemoDraftError):
            parse_group_output(value, section_titles=TITLES[:4], questions=questions,
                               allowed_refs={"claim-version:1", "artifact:not-a-claim"})

    def test_verifier_receives_all_producer_routes_and_full_draft(self):
        model = FakeModel([{"verdict": "pass", "verified_body_hash": "a" * 64,
                            "finding_codes": []}])
        result = verify_memo(model, sections=[{"title": "all", "body": "complete"}],
            questions=[{"question_ref": "q", "answer": "complete"}],
            material=[{"ref": "claim-version:1", "text": "actual source"}],
            material_hash="a" * 64, mission={}, producer_route_decision_refs=["route-decision:a", "route-decision:b"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(model.calls[0]["producer_route_decision_refs"], ("route-decision:a", "route-decision:b"))
        self.assertIn("actual source", model.calls[0]["prompt"])

    @patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority")
    def test_four_groups_then_one_verifier_publish_only_after_all_pass(self, authority_type):
        group_responses = []
        for _, section_indexes, question_indexes in GROUPS:
            group_responses.append(group_payload([TITLES[i] for i in section_indexes],
                [{"question_ref": f"memo_q{i+1:02d}", "question": QUESTIONS[i]} for i in question_indexes]))
        producer = FakeModel(group_responses)
        verifier = FakeModel([{"verdict": "pass", "verified_body_hash": "placeholder", "finding_codes": []}], "verifier")
        # The verifier hash is content-derived; echo it from the prompt like a real closed verifier.
        original = verifier.call
        def bound_call(**kwargs):
            contract_line = next(line for line in kwargs["prompt"].splitlines() if 'verified_body_hash' in line)
            verifier.responses = iter([{**json.loads(contract_line), "verdict": "pass", "finding_codes": []}])
            return original(**kwargs)
        verifier.call = bound_call
        authority_type.return_value.publish.return_value = {"status": "fresh", "id": "memo:v1", "content_hash": "f" * 64}
        mission = {"id": "mission:1", "content_hash": "a" * 64,
                   "autonomy": {"automation_principal": "automation:mission"}}
        playbook = {"id": "playbook:1", "content_hash": "b" * 64,
                    "deliverable_templates": {"investment_memo": TITLES}, "key_questions": QUESTIONS}
        frozen = {"status": "ready", "mission": mission, "playbook": playbook,
                  "company": {"company_ref": "company:ACN", "ticker": "ACN"},
                  "material": [{"ref": "claim-version:1", "hash": "c" * 64,
                                "kind": "claim", "text": "source"}],
                  "input_bindings": [{"ref": "claim-version:1", "hash": "c" * 64, "kind": "claim"}]}
        result = run_memo(store=object(), frozen=frozen, model=producer, verifier_model=verifier)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(producer.calls), 4)
        self.assertEqual(len(verifier.calls), 1)
        gate = authority_type.return_value.publish.call_args.kwargs["gate"]
        self.assertEqual([row["check_ref"] for row in gate["checks"]], list(CHECK_REFS))
        self.assertEqual(authority_type.return_value.publish.call_args.kwargs["model_invocation_refs"],
                         ["work:producer:1", "work:producer:2", "work:producer:3", "work:producer:4", "work:verifier:1"])

    @patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority")
    def test_verifier_reject_means_no_publish(self, authority_type):
        # Exercise the invariant at the orchestration boundary with a malformed first draft.
        producer = FakeModel([{"sections": [], "key_questions": []}])
        frozen = {"status": "ready", "mission": {}, "playbook": {"deliverable_templates": {"investment_memo": TITLES},
                  "key_questions": QUESTIONS}, "company": {"company_ref": "company:ACN"}, "material": [], "input_bindings": []}
        result = run_memo(store=object(), frozen=frozen, model=producer, verifier_model=FakeModel([]))
        self.assertEqual(result["status"], "refused")
        authority_type.return_value.publish.assert_not_called()

    def test_success_publishes_through_real_mission_deliverable_authority(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        state = bootstrap_method_authorities(store)
        params = mission_params(state)
        params["autonomy"] = {**params["autonomy"],
                              "may_write": [*params["autonomy"]["may_write"], "deliverable"]}
        mission_ref = params.pop("mission_ref")
        mission = CoverageMissionAuthority(store).create_mission(mission_ref, **params)
        claim = {"schema_version": "0.2", "id": "claim-version:" + "1" * 64,
                 "claim_ref": "claim:memo:1", "version": 1,
                 "subject_ref": mission["universe"][0]["company_ref"],
                 "metric_or_aspect": "business", "period": "2026Q2", "basis": "fixture",
                 "normalized_statement": "Demand improved.", "claim_kind": "qualitative",
                 "value": None, "unit": None, "currency": None, "scale": None,
                 "producer_execution_refs": [], "semantic_review_ref": None,
                 "semantic_review_hash": None, "candidate_origin_ref": None,
                 "candidate_origin_hash": None, "actor_ref": "system:test",
                 "prior_version_ref": None, "created_at": "2026-09-10T00:00:00+00:00"}
        claim["content_hash"] = content_hash({k: v for k, v in claim.items() if k != "content_hash"})
        with store._transaction() as cur:
            cur.execute("INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,content_hash,created_at) VALUES(?,?,?,?,?,?)",
                        (claim["id"], claim["claim_ref"], 1, json.dumps(claim), claim["content_hash"], claim["created_at"]))
        actual_titles = state["playbook"]["deliverable_templates"]["investment_memo"]
        actual_questions = state["playbook"]["key_questions"]
        responses = []
        for _, section_indexes, question_indexes in GROUPS:
            payload = group_payload([actual_titles[i] for i in section_indexes],
                [{"question_ref": f"memo_q{i+1:02d}", "question": actual_questions[i]} for i in question_indexes])
            for section in payload["sections"]:
                section["claim_refs"] = [claim["id"]]
            for answer in payload["key_questions"]:
                answer["refs"] = [claim["id"]]
            responses.append(payload)
        verifier = FakeModel([], "verifier")
        def bound_call(**kwargs):
            contract = json.loads(next(line for line in kwargs["prompt"].splitlines()
                                       if line.startswith('{"finding_codes"')))
            verifier.responses = iter([{**contract, "verdict": "pass", "finding_codes": []}])
            return FakeModel.call(verifier, **kwargs)
        verifier.call = bound_call
        frozen = {"status": "ready", "mission": mission, "playbook": state["playbook"],
                  "company": mission["universe"][0],
                  "material": [{"ref": claim["id"], "hash": claim["content_hash"],
                                "kind": "claim", "text": claim["normalized_statement"]}],
                  "input_bindings": [{"ref": claim["id"], "hash": claim["content_hash"], "kind": "claim"}]}
        result = run_memo(store=store, frozen=frozen, model=FakeModel(responses), verifier_model=verifier)
        self.assertEqual(result["status"], "succeeded", result)
        stored = MissionDeliverableAuthority(store).latest(
            "mission-deliverable:investment_memo:" + mission["universe"][0]["company_ref"].rsplit(":", 1)[-1])
        self.assertEqual(stored["gate"]["verified_body_hash"], result["gate"]["verified_body_hash"])
