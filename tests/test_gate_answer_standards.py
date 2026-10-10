"""Batch D / ap: Deep Insight Gate answer standards and confidence calibration.

q7 needs a >=5-year attribution series, q9 a higher-than-quarterly indicator
with a source and a threshold, q10 the full OP -> pretax -> NI -> EPS ->
multiple -> return chain on a model that passed its invariants.  An answer
that misses its standard is published as ``unknown`` (``standard_not_met``)
naming the MaterialRequest opened for it; ``high`` without a decomposition is
lowered to ``medium``.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.deep_insight_gate import (
    GROUP_OF, QUESTION_REFS, UNKNOWN_REASONS, validate_answer,
)
from dalton_core.deep_insight_gate_standards import (
    STANDARD_REASON, apply_answer_standards, attribution_years, calibrate_confidence,
    model_invariants_passed, open_request, standard_findings,
)
from dalton_core.product_contract import MaterialPackUnavailable

COMPANY = "company:ticker:synw"


def answer(ref, sentences, sources, confidence="medium"):
    return {
        "question_ref": ref, "question": f"question {ref}", "group": GROUP_OF[ref],
        "status": "answered", "confidence": confidence, "unknown": None,
        "sentences": [{"text": text, "refs": [row["ref"] for row in sources]} for text in sentences],
        "sources": sources, "gaps": [],
    }


def src(ref, kind="claim", text="材料", period=None):
    return {"kind": kind, "ref": ref, "text": text, "period": period}


Q7_GOOD = answer("q7", [
    "2019 至 2021 年股价上涨主要由盈利增长驱动，2022 年估值收缩是主因。",
    "2023 年与 2024 年估值修复，盈利贡献有限。"], [src("claim-version:a")])
Q7_SHORT = answer("q7", ["2023 年与 2024 年股价主要由估值驱动。"], [src("claim-version:a")])
Q9_GOOD = answer("q9", [
    "跟踪月度招聘职位数（来源：公司官网职位数据），连续两个月同比低于 -10% 即证伪需求判断。"],
    [src("claim-version:b")])
Q9_QUARTERLY = answer("q9", ["跟踪每季度订单（来源：季报），低于 1 倍即警示。"], [src("claim-version:b")])
Q10_GOOD = answer("q10", [
    "营业利润按模型增长后，税前利润与净利润同步上升，EPS 随之提高。",
    "按当前倍数 P/E 计算，回报空间来自 EPS 增长。"],
    [src("forecast-cell:m1:eps", kind="forecast_cell", text="EPS 5.00"), src("claim-version:c")])
Q10_NO_CELL = answer("q10", [
    "营业利润、税前利润、净利润、EPS、倍数与回报都会改善。"], [src("claim-version:c")])


def opener(calls):
    def open_(state_dir, *, company_ref, question_ref, question, problems):
        calls.append((company_ref, question_ref, tuple(problems)))
        return f"material-request:{question_ref}-1", None
    return open_


class StandardFindingsTests(unittest.TestCase):
    def test_q7_needs_five_distinct_years_and_an_attribution(self):
        self.assertEqual(standard_findings("q7", Q7_GOOD), [])
        self.assertGreaterEqual(len(attribution_years(Q7_GOOD)), 5)
        problems = standard_findings("q7", Q7_SHORT)
        self.assertTrue(any("年份" in item for item in problems))

    def test_q9_needs_a_higher_than_quarterly_indicator_with_source_and_threshold(self):
        self.assertEqual(standard_findings("q9", Q9_GOOD), [])
        self.assertTrue(standard_findings("q9", Q9_QUARTERLY))

    def test_q10_needs_the_full_chain_a_model_cell_and_passed_invariants(self):
        self.assertEqual(standard_findings("q10", Q10_GOOD, model_invariants_passed=True), [])
        self.assertIn("模型未通过不变量检查",
                      standard_findings("q10", Q10_GOOD, model_invariants_passed=False))
        self.assertIn("模型不变量检查结果未知",
                      standard_findings("q10", Q10_GOOD, model_invariants_passed=None))
        self.assertIn("没有引用模型预测格",
                      standard_findings("q10", Q10_NO_CELL, model_invariants_passed=True))
        broken = answer("q10", ["EPS 会提高，回报可观。"],
                        [src("forecast-cell:m1:eps", kind="forecast_cell")])
        problems = standard_findings("q10", broken, model_invariants_passed=True)
        self.assertIn("链条缺「营业利润」", problems)
        self.assertIn("链条缺「税前利润」", problems)

    def test_other_questions_and_unknowns_have_no_standard_here(self):
        self.assertEqual(standard_findings("q2", answer("q2", ["随便"], [src("c:1")])), [])
        self.assertEqual(standard_findings("q7", {"status": "unknown"}), [])


class ApplyStandardsTests(unittest.TestCase):
    def test_a_missed_standard_becomes_unknown_with_a_material_request(self):
        calls = []
        out, report = apply_answer_standards(
            {"q7": Q7_SHORT, "q9": Q9_GOOD, "q10": Q10_GOOD}, company_ref=COMPANY,
            model_invariants_passed=True, request_opener=opener(calls))
        self.assertEqual(out["q7"]["status"], "unknown")
        self.assertEqual(out["q7"]["unknown"]["reason"], STANDARD_REASON)
        self.assertEqual(out["q7"]["unknown"]["material_request_ref"], "material-request:q7-1")
        self.assertEqual(out["q9"]["status"], "answered")
        self.assertEqual(out["q10"]["status"], "answered")
        self.assertEqual([row["question_ref"] for row in report], ["q7"])
        self.assertEqual(calls[0][:2], (COMPANY, "q7"))
        # The authority accepts the shape it produces.
        checked = validate_answer(out["q7"], "answers[6]", question_ref="q7")
        self.assertEqual(checked["unknown"]["material_request_ref"], "material-request:q7-1")
        self.assertIn(STANDARD_REASON, UNKNOWN_REASONS)

    def test_q10_on_a_model_that_failed_invariants_is_unanswered(self):
        out, report = apply_answer_standards(
            {"q10": Q10_GOOD}, company_ref=COMPANY, model_invariants_passed=False,
            request_opener=opener([]))
        self.assertEqual(out["q10"]["status"], "unknown")
        self.assertIn("不变量", out["q10"]["unknown"]["missing"])

    def test_without_the_request_ledger_the_need_is_recorded_without_a_ref(self):
        with patch("dalton_core.product_contract.open_material_request",
                   side_effect=MaterialPackUnavailable("dalton_core.material_requests is not installed")):
            ref, why = open_request("/tmp/nowhere", company_ref=COMPANY, question_ref="q9",
                                    question="q", problems=["没有阈值"])
        self.assertIsNone(ref)
        self.assertIn("未登记", why)
        out, _ = apply_answer_standards(
            {"q9": Q9_QUARTERLY}, company_ref=COMPANY, model_invariants_passed=None,
            request_opener=lambda *a, **k: (None, "MaterialRequest 未登记：模块缺失"))
        self.assertNotIn("material_request_ref", out["q9"]["unknown"])
        self.assertIn("未登记", out["q9"]["unknown"]["evidence_that_would_answer"])
        validate_answer(out["q9"], "answers[8]", question_ref="q9")

    def test_the_old_unknown_shape_still_validates(self):
        old = {"question_ref": "q2", "question": "q", "group": GROUP_OF["q2"],
               "status": "unknown", "confidence": None, "sentences": [], "sources": [],
               "gaps": [], "unknown": {"reason": "material_insufficient", "missing": "x",
                                       "evidence_that_would_answer": "y"}}
        self.assertNotIn("material_request_ref",
                         validate_answer(old, "a", question_ref="q2")["unknown"])


class ConfidenceCalibrationTests(unittest.TestCase):
    def test_high_without_a_decomposition_is_lowered_to_medium(self):
        high = answer("q2", ["需求由大客户转型项目决定。"], [src("claim-version:a")], confidence="high")
        out, note = calibrate_confidence(high)
        self.assertEqual(out["confidence"], "medium")
        self.assertIn("置信度由 high 降为 medium", note)
        self.assertIn(note, out["gaps"])

    def test_high_with_two_source_kinds_and_a_model_cell_stays_high(self):
        high = answer("q2", ["收入增长拆分为量与价两部分。"],
                      [src("claim-version:a"), src("forecast-cell:m:rev", kind="forecast_cell")],
                      confidence="high")
        out, note = calibrate_confidence(high)
        self.assertIsNone(note)
        self.assertEqual(out["confidence"], "high")

    def test_apply_reports_a_lowered_confidence(self):
        high = answer("q3", ["供给由产能决定。"], [src("claim-version:a")], confidence="high")
        out, report = apply_answer_standards({"q3": high}, company_ref=COMPANY,
                                             model_invariants_passed=None, request_opener=opener([]))
        self.assertEqual(out["q3"]["confidence"], "medium")
        self.assertEqual(report[0]["action"], "confidence_lowered")
        validate_answer(out["q3"], "answers[2]", question_ref="q3")


class InvariantsReaderTests(unittest.TestCase):
    def test_no_model_table_or_no_model_is_unknown(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        self.assertIsNone(model_invariants_passed(connection, COMPANY))
        connection.execute("CREATE TABLE forecast_model_versions(company_ref TEXT, record_json TEXT, created_at TEXT)")
        self.assertIsNone(model_invariants_passed(connection, COMPANY))


class LaneIntegrationTests(unittest.TestCase):
    def test_the_lane_applies_the_standards_before_assembling(self):
        from tests import test_deep_insight_gate_lane as _gate

        harness = _gate.Harness()
        self.addCleanup(harness.close)
        summary = harness.run(answer_standards=True,
                              model_factory=lambda: _gate.FakeModel(answer_all=True))
        if summary.get("gate_status") in {"nothing_drafted", "classification_conflict"}:
            self.skipTest(f"fixture stopped earlier: {summary['gate_status']}")
        report = {row["question_ref"]: row for row in summary.get("answer_standards") or []}
        # One generic sentence answers nothing to the q7/q9/q10 standard.
        for ref in ("q7", "q9", "q10"):
            if ref in report:
                self.assertEqual(report[ref]["action"], "unanswered")
        self.assertTrue(set(report) & {"q7", "q9", "q10"}, summary)
        self.assertEqual(len(QUESTION_REFS), 12)


if __name__ == "__main__":
    unittest.main()
