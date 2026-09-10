import copy
import unittest

from dalton_core.investment_memo_contract import (
    CHECK_REFS,
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
            "producer_calls": [{"group": str(i), "work_order_ref": f"work:{i}",
                                "route_decision_ref": f"route:{i}"} for i in range(4)],
            "verifier": {"verdict": "pass", "work_order_ref": "work:v",
                         "route_decision_ref": "route:v",
                         "producer_route_decision_refs": [f"route:{i}" for i in range(4)],
                         "finding_codes": []},
        }
        record["gate"]["verified_body_hash"] = verified_body_hash(record)
        return record

    def test_gate_recomputes_complete_material_and_provenance(self):
        record = self.record()
        validate_memo_gate(record["gate"], material_hash=verified_body_hash(record))
        self.assertEqual(model_work_order_refs(record["gate"]),
                         ["work:0", "work:1", "work:2", "work:3", "work:v"])

    def test_body_or_input_tamper_is_rejected(self):
        record = self.record()
        record["sections"][0]["body"] = "changed"
        with self.assertRaisesRegex(InvestmentMemoContractError, "does not bind"):
            validate_memo_gate(record["gate"], material_hash=verified_body_hash(record))

    def test_unknown_question_or_missing_producer_is_rejected(self):
        for mutate in (
            lambda gate: gate["key_questions"][0].update(unknown=True, answer=""),
            lambda gate: gate["producer_calls"].pop(),
        ):
            record = self.record()
            mutate(record["gate"])
            with self.assertRaises(InvestmentMemoContractError):
                validate_memo_gate(record["gate"], material_hash=record["gate"]["verified_body_hash"])
