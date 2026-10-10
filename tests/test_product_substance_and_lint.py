"""Batch ap 1-3 (substance and publication gate) and batch as (reasoning lint)."""

from __future__ import annotations

import unittest

from dalton_core.mission_deliverable import (
    MissionDeliverableConflict,
    MissionDeliverableValidationError,
    validate_section,
)
from dalton_core.product_contract import (
    ProductContractError,
    material_missing_findings,
    placeholder_findings,
    validate_gap_item,
)
from dalton_core.product_substance import (
    display_rounding_findings,
    number_metadata_findings,
    publication_blockers,
    section_substance,
)
from dalton_core.research_localization import ResearchLocalizationError, build_prompt
from dalton_core.research_reasoning_lint import RULE_IDS, lint_fingerprint, lint_text

BODY = ("公司收入来自两类服务：项目制咨询与多年期托管合同，后者占比上升，利润率主要取决于人员利用率与定价，"
        "这决定了经营杠杆的方向。")


class SubstanceTests(unittest.TestCase):
    def status(self, body, title="S1", gaps=()):
        return section_substance({"title": title, "body": body, "gaps": list(gaps)})["status"]

    def test_the_five_statuses(self):
        self.assertEqual(self.status(""), "empty")
        self.assertEqual(self.status("", gaps=["估值快照未发布"]), "gap_only")
        self.assertEqual(self.status("S1 公司概览", title="S1 公司概览"), "heading_only")
        self.assertEqual(self.status("公司概览"), "heading_only")
        self.assertEqual(self.status("现有材料缺少分部数据。暂无可用的利润率序列。"), "gap_only")
        self.assertEqual(self.status("〔数字见研判记录〕。"), "placeholder_only")
        self.assertEqual(self.status(BODY), "substantive")

    def test_substance_survives_one_gap_sentence(self):
        self.assertEqual(self.status(BODY + "现有材料缺少分部利润率（material-request:x）。"), "substantive")

    def test_a_gap_appendix_is_judged_on_having_a_body(self):
        self.assertEqual(self.status("缺少分部利润率序列。", title="附录：我们不知道什么"), "substantive")
        self.assertEqual(self.status("", title="附录：我们不知道什么"), "empty")

    def test_publication_blockers_name_failed_sections_missing_titles_and_placeholders(self):
        sections = [
            {"title": "A", "body": BODY + "数字〔数字见研判记录〕。", "gaps": []},
            {"title": "B", "body": "", "gaps": ["没写"]},
        ]
        codes = sorted(item["code"] for item in publication_blockers(sections, expected=["A", "B", "C"]))
        self.assertEqual(codes, ["gap_only", "indirect_placeholder:bracket_reference", "missing_section"])

    def test_number_metadata_and_display_rounding(self):
        findings = number_metadata_findings([{"title": "A", "_strict_numbers": True, "numbers": [
            {"text": "收入 1,250", "passage_ref": "doc-passage:x:0-1:aaaaaaaaaaaa", "period": "Q2",
             "metric": "收入", "unit": "百万美元"},
            {"text": "收入 1,250"},
        ]}])
        self.assertEqual(len(findings), 1)
        self.assertEqual(sorted(findings[0]["missing"]), ["metric", "period", "source", "unit"])
        shown = display_rounding_findings("收入 18718000000 美元，利润率 0.212345。")
        self.assertEqual([item["figure"] for item in shown], ["18718000000", "0.212345"])
        self.assertEqual(display_rounding_findings("收入 187.2 亿美元，2026 年，20260930 收盘。"), [])


class GapAndPlaceholderTests(unittest.TestCase):
    def test_not_acquired_needs_a_request_and_what_was_tried(self):
        with self.assertRaises(ProductContractError):
            validate_gap_item({"kind": "not_acquired", "need": "分部利润率"})
        with self.assertRaises(ProductContractError):
            validate_gap_item({"kind": "not_acquired", "need": "分部利润率",
                               "request_ref": "material-request:x"})
        ok = validate_gap_item({"kind": "not_acquired", "need": "分部利润率",
                                "request_ref": "material-request:x", "tried": "年报、电话会"})
        self.assertEqual(ok["request_ref"], "material-request:x")
        self.assertEqual(validate_gap_item({"kind": "company_not_disclosed", "need": "客户续约率"})["kind"],
                         "company_not_disclosed")
        with self.assertRaises(ProductContractError):
            validate_gap_item({"kind": "unknown", "need": "x"})

    def test_material_missing_binds_to_a_ref_or_a_gap_item(self):
        self.assertEqual(len(material_missing_findings("现有材料缺少分部数据。")), 1)
        self.assertEqual(material_missing_findings("现有材料缺少分部数据（MR-12）。"), [])
        self.assertEqual(material_missing_findings(
            "现有材料缺少分部数据。", [{"kind": "not_acquired", "request_ref": "MR-1"}]), [])
        self.assertEqual(len(material_missing_findings(
            "材料缺少 A。材料缺少 B。", [{"kind": "not_acquired", "request_ref": "MR-1"}])), 1)

    def test_placeholders_from_the_registry(self):
        for text in ("〔数字见研判记录〕", "数字见研判记录", "TBD", "{{eps}}", "增速 XX%", "【待补充】"):
            with self.subTest(text=text):
                self.assertTrue(placeholder_findings(text), text)
        self.assertEqual(placeholder_findings("收入 187.2 亿美元（发布稿）"), [])


class StrictSectionTests(unittest.TestCase):
    def section(self, **overrides):
        base = {"title": "S", "body": BODY, "claim_refs": [], "numbers": [], "gaps": []}
        base.update(overrides)
        return base

    def test_strict_refuses_gap_only_and_unbound_material_missing(self):
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(self.section(body="现有材料缺少估值快照，估值一节暂缺。"), strict=True)
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(self.section(body=BODY + "现有材料缺少分部数据。"), strict=True)
        checked = validate_section(self.section(
            body=BODY + "现有材料缺少分部数据。",
            gap_items=[{"kind": "not_acquired", "need": "分部数据", "request_ref": "MR-7",
                        "tried": "年报"}]), strict=True)
        self.assertEqual(checked["gap_items"][0]["request_ref"], "MR-7")
        # Non-strict kinds keep their old behaviour.
        validate_section(self.section(body="", gaps=["没写"]))

    def test_an_estimate_must_be_labelled_and_bound_to_our_model(self):
        number = {"text": "我们的模型 FY2027 EPS 5.00 美元", "estimate": "assumption:x@2027-12-31:estimate"}
        validate_section(self.section(body="我们预计 FY2027 EPS 5.00 美元。", numbers=[number]))
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(self.section(body="FY2027 EPS 5.00 美元。", numbers=[number]))
        with self.assertRaises(MissionDeliverableValidationError):
            validate_section(self.section(body="我们预计 FY2027 EPS 5.00 美元。",
                                          numbers=[{**number, "estimate": "claim-version:x"}]))

    def test_a_pack_number_must_resolve_and_carry_the_figure(self):
        number = {"text": "一致预期 FY2027 EPS 4.50 美元", "number_ref": "consensus-estimate-version:x"}
        resolve = {"consensus-estimate-version:x": "一致预期 FY2027：收入 5,400 百万美元，EPS 4.50 美元"}.get
        checked = validate_section(self.section(body="一致预期 EPS 4.50 美元。", numbers=[number]),
                                   resolve_number=resolve)
        self.assertEqual(checked["numbers"][0]["source_text"][:4], "一致预期")
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(self.section(body="一致预期 EPS 4.50 美元。", numbers=[number]))
        with self.assertRaises(MissionDeliverableConflict):
            validate_section(self.section(body="一致预期 EPS 4.60 美元。",
                                          numbers=[{**number, "text": "EPS 4.60 美元"}]),
                             resolve_number=resolve)

    def test_source_refs_are_kept_closed(self):
        checked = validate_section(self.section(source_refs=[
            {"ref": "doc-passage:x:0-1:aaaaaaaaaaaa", "tag": "P1", "source_kind": "transcript",
             "as_of": "2026-07-30"}]))
        self.assertEqual(checked["source_refs"][0]["source_kind"], "transcript")
        with self.assertRaises(MissionDeliverableValidationError):
            validate_section(self.section(source_refs=[{"ref": "x", "secret": "y"}]))


class LocalisationGateTests(unittest.TestCase):
    def test_an_empty_source_section_is_not_localised(self):
        product = {"kind": "initial_screen", "version_ref": "v", "sections": [
            {"title": "A", "body": BODY, "gaps": []}, {"title": "B", "body": "", "gaps": ["x"]}]}
        with self.assertRaises(ResearchLocalizationError):
            build_prompt(product)

    def test_a_contract_product_with_a_gap_only_section_is_not_localised(self):
        product = {"kind": "initial_screen", "version_ref": "v", "contract": {"version": "x"},
                   "sections": [{"title": "A", "body": BODY, "gaps": []},
                                {"title": "B", "body": "现有材料缺少估值快照，估值一节暂缺。", "gaps": []}]}
        with self.assertRaises(ResearchLocalizationError):
            build_prompt(product)

    def test_a_whole_product_still_localises(self):
        product = {"kind": "initial_screen", "version_ref": "v",
                   "sections": [{"title": "A", "body": BODY, "gaps": []}]}
        self.assertIn("SOURCE SECTIONS", build_prompt(product))


class ReasoningLintTests(unittest.TestCase):
    CASES = {
        "backlog_as_near_term_revenue": (
            "创纪录的 RPO 意味着未来几个季度收入加速。",
            "RPO 中约 45% 在 12 个月内确认，对应未来 12 个月收入。"),
        "consensus_without_authority": (
            "共识认为 FY2027 EPS 为 4.50 美元。",
            "某券商认为 FY2027 EPS 为 4.70 美元。"),
        "fixed_lag_uncalibrated": (
            "新签订单领先收入 2-4 个季度。",
            "新签订单领先收入 2-4 个季度（未校准假设）。"),
        "classification_without_counter_check": (
            "这是一家合同型 compounder。",
            "这是一家合同型 compounder；反例是 2020 年续约率下滑。"),
        "management_share_gain_as_fact": (
            "公司正在赢得份额。",
            "管理层称公司正在赢得份额。"),
        "survey_intent_as_realisation": (
            "CIO 调研显示 AI 支出将带来收入加速。",
            "CIO 调研显示 AI 支出意愿上升，兑现要看下季度订单。"),
        "price_move_without_benchmark": (
            "股价上涨 6% 是因为订单超预期。",
            "股价上涨 6%，同期行业指数上涨 1%，超额部分反映订单超预期。"),
        "product_to_roi_without_economics": (
            "产品升级会带来 ROI 提升。",
            "产品升级带来的增量利润与资本开支之比决定 ROI 提升幅度。"),
        "usd_revenue_as_organic": (
            "美元收入增长 8%，说明有机增长强劲。",
            "美元收入增长 8%，剔除汇率与并购后本币有机增长 5%。"),
    }

    def test_every_rule_has_a_hit_and_a_clean_counterpart(self):
        self.assertEqual(set(self.CASES), set(RULE_IDS))
        for rule, (hit, clean) in self.CASES.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, {item["rule"] for item in lint_text(hit)})
                self.assertNotIn(rule, {item["rule"] for item in lint_text(clean)})

    def test_a_sentence_saying_a_consensus_is_missing_is_not_a_consensus_claim(self):
        for clean in ("缺卖方对明年每股收益的一致预期。", "展示的材料里没有会前的一致预期或券商预览。",
                      "一致预期不可得，无法给出前瞻倍数。", "There is no consensus for FY2028."):
            with self.subTest(clean=clean):
                self.assertNotIn("consensus_without_authority", {i["rule"] for i in lint_text(clean)})
        # The absence must be about the consensus itself, in the same clause.
        for hit in ("没有分歧，一致预期认为 FY2027 EPS 为 4.50 美元。", "市场预期收入增长 5%，没有意外。"):
            with self.subTest(hit=hit):
                self.assertIn("consensus_without_authority", {i["rule"] for i in lint_text(hit)})

    def test_consensus_is_exempt_when_a_consensus_authority_is_cited(self):
        self.assertEqual(lint_text("共识认为 FY2027 EPS 为 4.50 美元。", source_kinds={"consensus"}), [])

    def test_the_registry_is_hashed(self):
        self.assertEqual(len(lint_fingerprint()), 64)


if __name__ == "__main__":
    unittest.main()
