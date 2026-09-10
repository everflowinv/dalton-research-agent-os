import copy
import unittest

from dalton_core.investment_memo_contract import (
    CHECK_REFS,
    PRODUCER_GROUPS,
    InvestmentMemoContractError,
    model_work_order_refs,
    validate_memo_gate,
    verified_body_hash,
)


class InvestmentMemoContractTests(unittest.TestCase):
    def record(self):
        questions = [{"question_ref": f"q{i}", "question": f"Q{i}",
                      "answer": "answer", "refs": [f"claim:{i}"],
                      "unknown": False, "falsifier": "what would disprove it"}
                     for i in range(1, 13)]
        record = {"kind": "investment_memo", "subject_ref": "company:ACN",
                  "mission_version_ref": "mission:1", "mission_version_hash": "a" * 64,
                  "playbook_version_ref": "playbook:1", "playbook_version_hash": "b" * 64,
                  "template_ref": "investment_memo", "summary": "summary",
                  "sections": [{"title": "Key Information", "body": "body",
                                "claim_refs": ["claim:1"], "numbers": [], "gaps": []}],
                  "gaps": []}
        record["gate"] = {
            "schema_version": "investment-memo-gate-0.1", "passed": True,
            "checks": [{"check_ref": ref, "status": "pass", "reason": "ok",
                        "evidence_refs": ["claim:1"]} for ref in CHECK_REFS],
            "verified_body_hash": "", "key_questions": questions,
            "input_bindings": [{"ref": "dossier:1", "hash": "c" * 64, "kind": "company_dossier"}],
            "producer_calls": [{"group": group, "work_order_ref": f"work:{i}",
                                "route_decision_ref": f"route-decision:{i}", "result_envelope_ref": f"result:{i}",
                                "invocation_ref": f"invocation:{i}"} for i, group in enumerate(PRODUCER_GROUPS)],
            "verifier": {"verdict": "pass", "work_order_ref": "work:v",
                         "route_decision_ref": "route-decision:v", "result_envelope_ref": "result:v",
                         "invocation_ref": "invocation:v",
                         "producer_route_decision_refs": [f"route-decision:{i}" for i in range(4)],
                         "finding_codes": []},
        }
        record["gate"]["verified_body_hash"] = verified_body_hash(record)
        return record

    def test_gate_recomputes_complete_material_and_provenance(self):
        record = self.record()
        validate_memo_gate(record["gate"], material_hash=verified_body_hash(record),
                           expected_questions=[{"question_ref": f"q{i}", "question": f"Q{i}"} for i in range(1, 13)])
        self.assertEqual(model_work_order_refs(record["gate"]),
                         ["work:0", "work:1", "work:2", "work:3", "work:v"])

    def test_body_or_input_tamper_is_rejected(self):
        record = self.record()
        record["sections"][0]["body"] = "changed"
        with self.assertRaisesRegex(InvestmentMemoContractError, "does not bind"):
            validate_memo_gate(record["gate"], material_hash=verified_body_hash(record),
                           expected_questions=[{"question_ref": f"q{i}", "question": f"Q{i}"} for i in range(1, 13)])

    def test_unknown_question_or_missing_producer_is_rejected(self):
        for mutate in (
            lambda gate: gate["key_questions"][0].update(unknown=True, answer=""),
            lambda gate: gate["producer_calls"].pop(),
        ):
            record = self.record()
            mutate(record["gate"])
            with self.assertRaises(InvestmentMemoContractError):
                validate_memo_gate(record["gate"], material_hash=record["gate"]["verified_body_hash"],
                                   expected_questions=[{"question_ref": f"q{i}", "question": f"Q{i}"} for i in range(1, 13)])

    def test_none_hash_wrong_question_and_duplicate_route_are_rejected(self):
        mutations = (
            lambda gate: gate["input_bindings"][0].update(ref=None),
            lambda gate: gate["input_bindings"][0].update(hash="not-a-hash"),
            lambda gate: gate["key_questions"][0].update(question="another question"),
            lambda gate: gate["producer_calls"][1].update(
                route_decision_ref=gate["producer_calls"][0]["route_decision_ref"]),
        )
        expected = [{"question_ref": f"q{i}", "question": f"Q{i}"} for i in range(1, 13)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                record = self.record()
                mutate(record["gate"])
                with self.assertRaises(InvestmentMemoContractError):
                    validate_memo_gate(record["gate"], material_hash=record["gate"]["verified_body_hash"],
                                       expected_questions=expected)
