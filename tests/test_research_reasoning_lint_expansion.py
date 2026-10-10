"""Batch as: numeric growth bridges and production verification entry points."""
import unittest
from unittest.mock import Mock

from dalton_core.research_reasoning_lint import lint_text, source_kinds_for_refs


class GrowthBridgeTests(unittest.TestCase):
    def test_chinese_numbers_and_translated_classifications_keep_the_same_rule(self):
        self.assertIn("fixed_lag_uncalibrated", {x["rule"] for x in lint_text("宏观冲击滞后一个季度传到收入。")})
        self.assertEqual(lint_text("宏观冲击滞后一个季度传到收入，这是未校准假设。"), [])
        self.assertIn("classification_without_counter_check", {x["rule"] for x in lint_text("这家公司是合约型复利企业。")})
        self.assertEqual(lint_text("这家公司是合约型复利企业，反例是客户续约率下降。"), [])

    def test_research_questions_and_explicit_gaps_are_not_claims(self):
        cases = (
            ("consensus_without_authority", "需补齐市场预期后再判断。"),
            ("consensus_without_authority", "卖方一致预期都缺来源。"),
            ("consensus_without_authority", "我们缺卖方对该业务增速和自由现金流的一致预期。"),
            ("backlog_as_near_term_revenue", "应观察在手订单能否转化为收入。"),
            ("fixed_lag_uncalibrated", "四季度多个领域价格走低，是否已经传导到 FY2027 一季度的合同利润率？"),
            ("management_share_gain_as_fact", "持续夺取份额的说法尚不能核实。"),
            ("management_share_gain_as_fact", "所以我们把份额提升看作有方向性支持的公司观点，量化还要靠同业季度收入对比。"),
            ("usd_revenue_as_organic", "美元收入增长5%可作报告口径基线，但不能替代有机增长基线。"),
        )
        for rule, text in cases:
            with self.subTest(text=text):
                self.assertNotIn(rule, {item["rule"] for item in lint_text(text)})

    def test_a_gap_does_not_exempt_a_separate_unsupported_assertion(self):
        self.assertIn("fixed_lag_uncalibrated", {item["rule"] for item in lint_text(
            "能否传导到下季度仍待观察，但需求必定滞后一个季度传到收入。")})
        for text in ("缺少收入一致预期，利润高于市场预期。",
                     "卖方一致预期缺来源，但收入高于一致预期。"):
            self.assertIn("consensus_without_authority", {item["rule"] for item in lint_text(text)})

    def test_reported_growth_must_reconcile_with_separate_contributions(self):
        findings = lint_text("美元收入增长8%，有机增长7%，汇率贡献1个百分点，并购贡献2个百分点。")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["difference_percentage_points"], "-2")
        self.assertEqual(lint_text("美元收入增长8%，有机增长9%，汇率贡献-2个百分点，并购贡献1个百分点。"), [])

    def test_rounded_disclosures_and_distinct_periods_are_not_false_failures(self):
        for text in (
            "美元收入增长8%，有机增长5.1%，汇率贡献1.1个百分点，并购贡献2个百分点。",
            "FY2025美元收入增长8%，FY2026有机增长7%，汇率贡献1个百分点，并购贡献2个百分点。",
            "美元收入增长8%，有机增长7%，汇率贡献1%，并购贡献2%。",
        ):
            with self.subTest(text=text):
                self.assertEqual(lint_text(text), [])

    def test_fx_and_acquisitions_cannot_hide_in_one_combined_number(self):
        self.assertTrue(lint_text("美元收入增长8%，有机增长5%，并购及汇率合计贡献3个百分点。"))
        self.assertEqual(lint_text("美元收入增长8%，有机增长5%，并购贡献2个百分点，汇率贡献1个百分点。"), [])

    def test_consensus_exemption_requires_a_cited_authority(self):
        aliases = {"N1": "consensus-estimate-version:synthetic:1", "P1": "doc-passage:synthetic:0:abc"}
        self.assertEqual(source_kinds_for_refs(["[N1]"], aliases), {"consensus"})
        self.assertEqual(source_kinds_for_refs(["P1"], aliases), set())
        self.assertEqual(source_kinds_for_refs(["street-estimate:synthetic:1"]), set())


class ProductionEntryTests(unittest.TestCase):
    def test_event_lint_refuses_before_paying_an_independent_verifier(self):
        from dalton_core.event_judgement import verify
        model = Mock()
        result = verify({}, {"status": "judged", "because": "创纪录的RPO意味着未来几个季度收入加速。",
                             "citations": []}, model=model, mission={}, request_id="synthetic-lint",
                        family_resolver=lambda _: "synthetic-family")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reasoning_findings"][0]["rule"], "backlog_as_near_term_revenue")
        model.call.assert_not_called()

    def test_event_questions_and_quoted_sources_are_not_assertions(self):
        from dalton_core.event_judgement import reasoning_findings
        result = reasoning_findings({"result": {"tags": {"N1": "consensus-estimate-version:synthetic:1"},
                                                     "tag_texts": {"P2": "公司赢得份额。"}}},
                                    {"because": "一致预期为增长5%。", "citations": ["N1"],
                                     "research_question": "RPO是否对应收入增长？"})
        self.assertEqual(result, [])

    def test_debate_production_path_stops_before_verifying_bad_reasoning(self):
        from dalton_core.debate_map_draft import draft_debate_map
        from tests.test_debate_map_draft import table, draft_reply, FakeModel, METHOD, MISSION, NOW
        inputs = table()
        inputs["subject_ref"] = "company:ticker:synw"
        model = FakeModel([draft_reply(bull={"statement": "创纪录的RPO意味着未来几个季度收入加速。",
                                            "claim_refs": ["C1", "C4"]})])
        result = draft_debate_map(table=inputs, method=METHOD, model=model, mission=MISSION,
                                  created_at=NOW, family_of=model.family_of)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["reason"], "reasoning_lint")
        self.assertEqual(len(model.calls), 1)

    def test_memo_and_answer_share_the_numeric_rule(self):
        from dalton_core.investment_memo_cli import lint_check
        from dalton_core.ask_answer import lint_sentence
        text = "美元收入增长8%，有机增长7%，汇率贡献1个百分点，并购贡献2个百分点。"
        self.assertFalse(lint_check([{"title": "增长口径", "body": text}], [])[0])
        self.assertEqual(lint_sentence(text)[0]["difference_percentage_points"], "-2")


if __name__ == "__main__":
    unittest.main()
