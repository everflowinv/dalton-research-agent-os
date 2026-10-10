"""Batch D: the Investment Memo contract after owner decision 2026-10-09.

Two contradictions fixed: the Key Information title asks for 「30 日 ADV」
while the material carried none, and it asks for 「建议（多/空/pass + 仓位 +
期限）」 while the prompt said the memo did not authorise a recommendation.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from dalton_core.investment_memo_cli import (
    lint_check, market_block_row, recommendation_check, run_memo,
)
from dalton_core.investment_memo_contract import (
    ADV_UNAVAILABLE_LINE, CHECK_REFS, CHECK_REFS_V2, InvestmentMemoContractError,
    RECOMMENDATION_FORMAT, SCHEMA_VERSION, SCHEMA_VERSION_V2, adv_findings,
    recommendation_findings, validate_memo_gate,
)
from dalton_core.investment_memo_draft import (
    FINDING_CODES, InvestmentMemoCitationError, build_group_prompt, build_verifier_prompt,
    parse_group_output,
)
from dalton_core.mission_deliverable import validate_pack_number
from dalton_core.product_contract import NUMBER_SOURCE_RULES
from tests.test_investment_memo_draft import (
    GROUPS, QUESTIONS, TITLES, FakeModel, group_payload,
)

KEY = "Key Information：名称/ticker、日期、股价、市值（本币+USD）、30 日 ADV、建议（多/空/pass + 仓位 + 期限）"
GOOD = "建议（研究建议）：方向：多；仓位区间：标准仓（组合权重 1 至 3 个百分点）；期限：6 至 12 个月。"


class RecommendationTests(unittest.TestCase):
    def test_a_labelled_direction_position_and_horizon_pass(self):
        self.assertEqual(recommendation_findings(GOOD), [])
        self.assertEqual(recommendation_findings("建议（研究建议）：方向：pass，估值已充分反映。"), [])

    def test_a_missing_part_is_named(self):
        codes = [item["code"] for item in recommendation_findings(
            "建议（研究建议）：方向：空；期限：6 至 12 个月。")]
        self.assertEqual(codes, ["recommendation_position_missing"])
        codes = [item["code"] for item in recommendation_findings("空间有限，等待更好的价格。")]
        self.assertEqual(codes, ["recommendation_label_missing", "recommendation_direction_missing"])

    def test_the_check_reads_the_key_information_section(self):
        ok, _ = recommendation_check([{"title": "Key Information", "body": GOOD}])
        self.assertTrue(ok)
        ok, reason = recommendation_check([{"title": "Key Information", "body": "公司很好。"}])
        self.assertFalse(ok)
        self.assertIn("recommendation_direction_missing", reason)


class AdvTests(unittest.TestCase):
    def test_adv_from_the_material_or_an_unavailable_line_bound_to_a_request(self):
        self.assertEqual(adv_findings(KEY, "30 日 ADV 45.0 百万美元。"), [])
        line = "30 日 ADV：暂不可得（行情序列没有成交量）"
        self.assertEqual(adv_findings(KEY, line, [{"kind": "not_acquired",
                                                   "request_ref": "material-request:adv"}]), [])
        self.assertEqual([i["code"] for i in adv_findings(KEY, line)],
                         ["adv_unavailable_without_request"])
        self.assertEqual([i["code"] for i in adv_findings(KEY, "股价 80 美元。")], ["adv_missing"])
        self.assertEqual(adv_findings("Executive Summary", "无"), [])
        self.assertTrue(ADV_UNAVAILABLE_LINE.startswith("30 日 ADV"))

    def test_the_market_block_row_is_a_citable_adv_source(self):
        block = {"status": "ok", "currency": "USD", "series_version_ref": "market-price-series-version:x",
                 "price": {"close": "80.00", "date": "2026-08-14"},
                 "market_cap": {"value": "8000000000", "formula": "close × shares_outstanding"},
                 "adv_30d": {"status": "available", "value": "45000000", "shares": "562500",
                             "since": "2026-07-02"}}
        with patch("dalton_core.market_data_block.market_block_from", return_value=block):
            row = market_block_row(object(), "company:x")
        self.assertEqual(row["kind"], "market_block")
        self.assertIn("30 日 ADV 45000000 USD", row["text"])
        checked = validate_pack_number({"text": row["text"], "number_ref": row["ref"]},
                                       {row["ref"]: row["text"]}.get)
        self.assertEqual(checked["number_ref"], row["ref"])
        with patch("dalton_core.market_data_block.market_block_from",
                   return_value={**block, "adv_30d": {"status": "unavailable", "reason": "没有成交量"}}):
            row = market_block_row(object(), "company:x")
        self.assertIn("30 日 ADV 不可得（没有成交量）", row["text"])


class PromptTests(unittest.TestCase):
    def test_the_producer_prompt_asks_for_a_recommendation_and_the_shared_rules(self):
        prompt = build_group_prompt(group="identity_background", section_titles=[KEY],
                                    questions=[{"question_ref": "memo_q01", "question": "q"}],
                                    material=[], company={"company_ref": "company:x"})
        self.assertNotIn("does not authorize a", prompt)
        self.assertIn(RECOMMENDATION_FORMAT, prompt)
        self.assertIn(ADV_UNAVAILABLE_LINE, prompt)
        self.assertIn(NUMBER_SOURCE_RULES[0], prompt)
        self.assertIn("[consensus_without_authority]", prompt)
        self.assertIn("Number rows:", prompt)

    def test_the_verifier_is_taught_the_same_rules_and_new_codes(self):
        prompt = build_verifier_prompt(sections=[], questions=[], material=[], material_hash="a" * 64)
        self.assertIn("recommendation_missing", prompt)
        self.assertIn("[usd_revenue_as_organic]", prompt)
        self.assertIn("A recommendation is required, not forbidden", prompt)
        for code in ("recommendation_missing", "reasoning_lint", "material_missing_unbound"):
            self.assertIn(code, FINDING_CODES)


class CitationTests(unittest.TestCase):
    MATERIAL = [
        {"ref": "claim-version:1", "kind": "claim", "text": "source"},
        {"ref": "market-block:v:2026-08-14", "kind": "market_block", "text": "30 日 ADV 45.0 百万美元"},
        {"ref": "doc-passage:doc:x:0-10:aaaaaaaaaaaa", "kind": "pack_passage", "text": "EPS of $1.05"},
        {"ref": "assumption:x.eps@2027-12-31:estimate", "kind": "pack_ours", "text": "我们的模型 EPS 5.00"},
    ]

    def payload(self, numbers):
        questions = [{"question_ref": "memo_q01", "question": QUESTIONS[0]}]
        value = group_payload(TITLES[:1], questions)
        value["sections"][0]["numbers"] = numbers
        return value, questions

    def test_pack_numbers_passages_estimates_and_derivations_pass_through(self):
        value, questions = self.payload([
            {"text": "30 日 ADV 45.0 百万美元", "number_ref": "market-block:v:2026-08-14"},
            {"text": "EPS of $1.05", "passage_ref": "doc-passage:doc:x:0-10:aaaaaaaaaaaa"},
            {"text": "我们的估计 EPS 5.00", "estimate": {"assumption_ref": "assumption:x.eps@2027-12-31:estimate"}},
            {"text": "高 11.1%", "derivation": {"expression": "(5.00/4.50-1)*100", "result": "11.1"}},
        ])
        parsed = parse_group_output(value, section_titles=TITLES[:1], questions=questions,
                                    allowed_refs={row["ref"] for row in self.MATERIAL},
                                    material=self.MATERIAL)
        numbers = parsed["sections"][0]["numbers"]
        self.assertEqual([sorted(set(n) - {"text"}) for n in numbers],
                         [["number_ref"], ["passage_ref"], ["estimate"], ["derivation"]])
        self.assertEqual(parsed["sections"][0]["gap_items"][0]["request_ref"], "material-request:memo-adv")

    def test_a_number_ref_outside_the_material_is_refused(self):
        value, questions = self.payload([{"text": "1", "number_ref": "market-block:invented"}])
        with self.assertRaises(InvestmentMemoCitationError):
            parse_group_output(value, section_titles=TITLES[:1], questions=questions,
                               allowed_refs={row["ref"] for row in self.MATERIAL},
                               material=self.MATERIAL)


class GateSchemaTests(unittest.TestCase):
    def test_check_lists_by_schema(self):
        self.assertEqual(CHECK_REFS_V2[:4], CHECK_REFS)
        self.assertEqual(CHECK_REFS_V2[4:], ("recommendation_stated", "reasoning_lint"))
        self.assertNotEqual(SCHEMA_VERSION, SCHEMA_VERSION_V2)


class LintCheckTests(unittest.TestCase):
    def test_management_share_claim_as_fact_fails_the_memo_lint(self):
        ok, reason = lint_check([{"title": "S2", "body": "公司正在赢得份额。", "numbers": []}], [])
        self.assertFalse(ok)
        self.assertIn("management_share_gain_as_fact", reason)
        ok, _ = lint_check([{"title": "S2", "body": "管理层称公司正在赢得份额。", "numbers": []}], [])
        self.assertTrue(ok)


class RunMemoTests(unittest.TestCase):
    def frozen(self):
        mission = {"id": "mission:1", "content_hash": "a" * 64,
                   "autonomy": {"automation_principal": "automation:mission"}}
        playbook = {"id": "playbook:1", "content_hash": "b" * 64,
                    "deliverable_templates": {"investment_memo": TITLES}, "key_questions": QUESTIONS}
        return {"status": "ready", "mission": mission, "playbook": playbook,
                "company": {"company_ref": "company:ACN", "ticker": "ACN"},
                "material": [{"ref": "claim-version:1", "hash": "c" * 64, "kind": "claim", "text": "source"}],
                "input_bindings": [{"ref": "claim-version:1", "hash": "c" * 64, "kind": "claim"}]}

    def responses(self, mutate=None):
        out = []
        for _, sections, questions in GROUPS:
            payload = group_payload([TITLES[i] for i in sections],
                                    [{"question_ref": f"memo_q{i+1:02d}", "question": QUESTIONS[i]}
                                     for i in questions])
            if mutate:
                mutate(payload)
            out.append(payload)
        return out

    @patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority")
    def test_a_memo_without_a_recommendation_fails_the_deterministic_gate(self, authority):
        def drop(payload):
            for section in payload["sections"]:
                if section["title"].startswith("Key Information"):
                    section["body"] = section["body"].replace("方向：多；", "")
        result = run_memo(store=SimpleNamespace(connection=None), frozen=self.frozen(), model=FakeModel(self.responses(drop)),
                          verifier_model=FakeModel([]))
        self.assertEqual(result["memo_status"], "deterministic_gate_failed", result)
        failed = [c["check_ref"] for c in result["checks"] if c["status"] != "pass"]
        self.assertEqual(failed, ["recommendation_stated"])
        authority.return_value.publish.assert_not_called()

    @patch("dalton_core.investment_memo_cli.MissionDeliverableAuthority")
    def test_a_published_memo_binds_the_contract_and_the_pack_numbers(self, authority):
        verifier = FakeModel([], "verifier")
        original = verifier.call

        def bound(**kwargs):
            line = next(l for l in kwargs["prompt"].splitlines() if "verified_body_hash" in l and l.startswith("{"))
            verifier.responses = iter([{**json.loads(line), "verdict": "pass", "finding_codes": []}])
            return original(**kwargs)
        verifier.call = bound
        authority.return_value.publish.return_value = {"status": "fresh", "id": "memo:v1",
                                                       "content_hash": "f" * 64}
        result = run_memo(store=SimpleNamespace(connection=None), frozen=self.frozen(), model=FakeModel(self.responses()),
                          verifier_model=verifier)
        self.assertEqual(result["status"], "succeeded", result)
        kwargs = authority.return_value.publish.call_args.kwargs
        self.assertEqual(kwargs["gate"]["schema_version"], SCHEMA_VERSION_V2)
        self.assertEqual([c["check_ref"] for c in kwargs["gate"]["checks"]], list(CHECK_REFS_V2))
        self.assertIn("version", kwargs["contract"])
        self.assertEqual(kwargs["pack_numbers"], {})
        validate_memo_gate(kwargs["gate"], material_hash=kwargs["gate"]["verified_body_hash"],
                           expected_questions=[{"question_ref": f"memo_q{i:02d}", "question": q}
                                               for i, q in enumerate(QUESTIONS, 1)])
        broken = copy.deepcopy(kwargs["gate"])
        broken["checks"] = broken["checks"][:4]
        with self.assertRaises(InvestmentMemoContractError):
            validate_memo_gate(broken, material_hash=broken["verified_body_hash"],
                               expected_questions=[{"question_ref": f"memo_q{i:02d}", "question": q}
                                                   for i, q in enumerate(QUESTIONS, 1)])


if __name__ == "__main__":
    unittest.main()
