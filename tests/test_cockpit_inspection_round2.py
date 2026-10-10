"""2026-10-08 cockpit inspection, round two: display rules, one test each.

Every rule here is display only: the stored record is unchanged and the
original stays under 技术详情.  Each test is named for the inspection finding
it pins.
"""

from __future__ import annotations

import unittest

from dalton_core.cockpit_model_display import statement_line_label
from dalton_core.financial_display import (
    filing_figure_display, financial_display_text, source_display_label, statement_display,
)

GATE = "正文正在检查文字表达，完成后会显示。"


class FinancialNumbersTests(unittest.TestCase):
    """DEFECT-04 / DEFECT-05: amounts in 亿美元, concepts by account name."""

    def test_the_chinese_xbrl_claim_reads_as_account_period_and_amount(self):
        raw = ("AMAZON COM INC在10-Q（accession 0001018724-26-000026）中列报的营业收入，"
               "期间 2026-04-01..2026-06-30，为 USD 200606000000。该数字直接取自申报的 XBRL 行 "
               "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax（Net sales，第 424 行），"
               "未经任何换算。")
        self.assertEqual(filing_figure_display(raw),
                         "营业收入（2026年4月1日至2026年6月30日）：2006 亿美元 · 10-Q 申报原值")

    def test_the_english_statement_line_source_reads_in_chinese(self):
        raw = ("Revenues (us-gaap:Revenues) for 2026-01-01..2026-06-30 was 10894000000 usd, "
               "as filed in 0001058290-26-000031")
        shown = source_display_label({"kind": "figure", "text": raw})
        self.assertEqual(shown, "营业收入（2026年1月1日至2026年6月30日）：108.9 亿美元 · SEC 申报原值")
        self.assertNotIn("us-gaap", shown)

    def test_scaled_and_bare_amounts_on_chinese_and_english_lines(self):
        self.assertEqual(
            financial_display_text("2025 年营业收入为 USD 716924 million。"),
            "2025 年营业收入为 7169 亿美元。")
        self.assertEqual(
            financial_display_text("Net sales was USD 716924 million (currency), as published."),
            "Net sales was USD 716,924 million, as published.")
        self.assertEqual(
            financial_display_text("毛利 24033000000 除以营业收入 44479000000（accession 0000051143-24-000049）"),
            "毛利 24,033,000,000 除以营业收入 44,479,000,000（申报编号 0000051143-24-000049）")

    def test_quotations_identifiers_and_unchanged_text_are_left_alone(self):
        quoted = '管理层原话："revenue of USD 200606000000"'
        self.assertEqual(financial_display_text(quoted), quoted)
        self.assertIsNone(statement_display("没有数字的句子。"))
        self.assertIsNone(source_display_label("claim-version:" + "a" * 64))

    def test_xbrl_ids_in_prose_become_account_names(self):
        shown = financial_display_text("该数字直接取自申报的 XBRL 行 us-gaap:OperatingIncomeLoss（AWS，第 434 行）")
        self.assertIn("「营业利润」", shown)
        self.assertNotIn("us-gaap", shown)

    def test_the_html_export_shows_the_figure_and_keeps_the_sentence(self):
        from dalton_core.research_html_export import _number_text

        raw = ("Meta Platforms, Inc.在10-K（accession 0001628280-26-003942）中列报的营业利润"
               "（分部：Reality Labs），期间 2023-01-01..2023-12-31，为 USD -16120000000。"
               "该数字直接取自申报的 XBRL 行 us-gaap:OperatingIncomeLoss（Reality Labs，第 211 行），未经任何换算。")
        shown, original = _number_text({"text": raw, "period": "2023-01-01..2023-12-31"}, {})
        self.assertEqual(shown, "营业利润（分部：Reality Labs）（2023年1月1日至2023年12月31日）："
                                "-161.2 亿美元 · 10-K 申报原值")
        self.assertEqual(original, raw)

    def test_a_peer_table_of_ticker_keyed_companies_renders_as_a_table(self):
        # Section 二 #2: ws-7d's GOOGL/META table, and a localized title,
        # fell back to one line of text.
        from dalton_core.cockpit_research_library import _comparison_table_display

        body = ("company\tmetric\t2026Q1\t2026Q2\n"
                "GOOGL\trevenue\t109896000000\t119796000000\n"
                "META\trevenue\t56311000000\t60801000000\n"
                "META\tgross_margin\t81.8%\t82.1%\n"
                "# 毛利率口径不完全一致。")
        numbers = [{"text": "META 2026Q2（期末 2026-06-30）revenue 60801000000", "period": "2026Q2",
                    "cell": {"kind": "statement_accession",
                             "ref": "comparison-cell:company-ticker-meta:revenue:2026Q2",
                             "accession": "0001628280-26-050705"}}]
        for title in ("cross_company_comparison", "跨公司横向对比"):
            with self.subTest(title=title):
                table = _comparison_table_display(title, body, numbers)
                self.assertIsNotNone(table)
                self.assertEqual(table["headers"], ["公司", "指标", "2026年第一季度", "2026年第二季度"])
                self.assertEqual(table["rows"][0], ["GOOGL", "营业收入", "1099 亿美元", "1198 亿美元"])
                self.assertEqual(table["rows"][2][1], "毛利率")

    def test_a_cell_naming_another_company_is_still_refused(self):
        from dalton_core.cockpit_research_library import _comparison_table_display

        body = "company\tmetric\t2026Q2\nMETA\trevenue\t60801000000\n"
        numbers = [{"text": "META 2026Q2（期末 2026-06-30）revenue 60801000000", "period": "2026Q2",
                    "cell": {"kind": "statement_accession",
                             "ref": "comparison-cell:company-ticker-googl:revenue:2026Q2",
                             "accession": "0001628280-26-050705"}}]
        self.assertIsNone(_comparison_table_display("cross_company_comparison", body, numbers))


class ModelLineLabelTests(unittest.TestCase):
    """DEFECT-02: a statement line's name is never the prose gate."""

    def test_lines_read_by_name_concept_or_role_and_never_the_gate(self):
        unreviewed = lambda value: None  # noqa: E731 - nothing reviewed yet
        cases = (
            ({"label": "NCI – other", "ref": "result:noncontrolling_interest:nci-other"}, "其他少数股东损益"),
            ({"label": "Provision (benefit) for income taxes",
              "ref": "result:income_tax_expense"}, "所得税费用"),
            ({"label": "Widget fees", "ref": "concept:us-gaap:CostOfRevenue",
              "concept": "us-gaap:CostOfRevenue"}, "营业成本"),
            ({"label": "Ad-hoc line", "ref": "result:operating_expense:sales-marketing"}, "销售与营销费用"),
            ({"label": "Something new", "ref": "result:other:unknown"}, "Something new"),
        )
        for row, expected in cases:
            with self.subTest(row=row["label"]):
                self.assertEqual(statement_line_label(row, unreviewed), expected)
        self.assertEqual(statement_line_label({"label": "Revenues"}, lambda v: "收入（已审阅）"),
                         "收入（已审阅）")

    def test_the_model_table_gates_prose_but_not_line_names(self):
        from dalton_core.company_model_report import render_forecast_model

        record = {
            "id": "forecast-model-version:test", "version": 3, "change_reason": "assumption_review",
            "history_periods": ["2026-06-30"], "realised_periods": [],
            "forecast_periods": [{"end": "2026-09-30"}],
            "drivers": [{"ref": "concept:us-gaap:NetIncomeLossAttributableToNoncontrollingInterest",
                         "label": "Net income attributable to NCI",
                         "concept": "us-gaap:NetIncomeLossAttributableToNoncontrollingInterest",
                         "role": None,
                         "history": [{"period_end": "2026-06-30", "value": "4", "unit": "usd"}]}],
            "assumptions": [{"driver_ref": "concept:us-gaap:NetIncomeLossAttributableToNoncontrollingInterest",
                             "measure": "share_of_line", "kind": "estimate", "value": "0.01",
                             "period": {"end": "2026-09-30"},
                             "because": "an analyst's free-text reason nobody has reviewed"}],
            "results": [{"ref": "result:noncontrolling_interest", "label": "Net income attributable to NCI",
                         "status": "unavailable",
                         "reason": "statement line nci has no forecast assumption for this quarter",
                         "cells": [{"period": {"end": "2026-09-30"}, "status": "unavailable",
                                    "value": None}]}],
        }
        gate = lambda value: GATE  # noqa: E731 - every prose string unreviewed
        table = render_forecast_model(
            record, display_text=gate, include_technical=False,
            line_label=lambda row: statement_line_label(row, lambda value: None))
        label_rows = [line for line in table.splitlines()
                      if line.startswith("  ") and not line.startswith("   ")]
        self.assertTrue(any(line.strip().startswith("少数股东损益") for line in label_rows), table)
        self.assertFalse(any(GATE in line for line in label_rows), table)
        # The free-text reason is prose: still gated until reviewed.
        self.assertIn(f"依据：{GATE}", table)
        # A closed engine reason is translated, not gated.
        self.assertIn("本季度缺少该报表项目的预测假设", table)

    def test_an_empty_model_tab_says_why(self):
        from dalton_core.cockpit_plane import _model_empty_label

        entry = {"stage": "initial_screen", "stage_label": "初步筛选", "stage_status_label": "进行中"}
        self.assertEqual(
            _model_empty_label(entry, "还差：过去 4 个季度的电话会纪要（3/4）", False),
            "该公司的预测模型还没有生成：还没有确定模型规格（预测哪些报表行、用哪些驱动因素）；"
            "目前处于「初步筛选」阶段（进行中），还差：过去 4 个季度的电话会纪要（3/4）。")
        self.assertIn("模型规格已经确定", _model_empty_label({"stage": None}, None, True))
        self.assertIn("研究还没有开始", _model_empty_label({"stage": None}, None, True))


from tests.test_cockpit_wave1 import ACN, CTSH, Wave1Case  # noqa: E402


class ModelTabOnThePlaneTests(Wave1Case):
    """DEFECT-02 / DEFECT-03 through ``CockpitPlane`` itself."""

    def test_with_the_language_policy_on_no_line_name_is_gated(self):
        import json

        from dalton_core.model_forecast_driver import ForecastModelAuthority
        from tests.test_model_forecast_driver import model as forecast_body

        # Filer line names nobody has reviewed a translation for: before the
        # fix both printed 「正文正在检查文字表达，完成后会显示。」.
        body = forecast_body()
        body["drivers"][0]["label"] = "Subscription and platform revenues"
        for result in body["results"]:
            if result["ref"] == "result:net_income":
                result["label"] = "Net income attributable to Accenture plc"
        ForecastModelAuthority(self.store).publish(body)
        policy = self.plane.config.core_db.parent / "research-language-policy.json"
        policy.write_text(json.dumps({"schema_version": "research-language-policy:0.1",
                                      "required": True}))
        view = self.plane.company_model(ACN)
        rows = [line for line in view["table"].splitlines()
                if line.startswith("  ") and not line.startswith("   ")]
        self.assertTrue(rows)
        self.assertFalse([line for line in rows if GATE in line], view["table"])
        self.assertTrue([line for line in rows if line.strip().startswith("营业收入 [收入驱动]")])
        self.assertTrue([line for line in rows if line.strip().startswith("净利润")])
        self.assertFalse([label for label in view["readiness"].get("results_unavailable_labels", [])
                          if GATE in label])

    def test_a_company_without_a_model_says_so_on_its_card(self):
        self.publish_model()
        overview = self.plane.overview()
        self.assertIsNone(self.card(overview)["model_empty_label"])
        label = self.card(overview, CTSH)["model_empty_label"]
        self.assertTrue(label.startswith("该公司的预测模型还没有生成："), label)


class OwnerWordsTests(unittest.TestCase):
    """Section 三 and 四: plain Chinese for the owner, ids under 技术详情."""

    def test_goal_and_dossier_terms(self):
        from dalton_core.research_gap_display import display_metadata_text, owner_terms_text

        self.assertEqual(
            owner_terms_text("建立对 US IT services 行业的首次覆盖：五家公司的 Initial Screen、行业与公司 Thesis。"),
            "建立对美国 IT 服务行业的首次覆盖：五家公司的初步筛选、行业与公司投资论点。")
        self.assertEqual(
            owner_terms_text("横向对比云业务 CapEx、GW Capacity 与 Backlog，厘清核心业务 driver。"),
            "横向对比云业务资本开支（CapEx）、电力容量（GW）与在手订单（Backlog），厘清核心业务驱动因素。")
        self.assertEqual(
            display_metadata_text("fiscal 2026 第二季度 订单 book-to-bill 低于一。"),
            "2026 财年第二季度订单订单收入比（book-to-bill）低于 1。")
        self.assertEqual(
            owner_terms_text("人类给方向、初始 Thesis 和检查点裁决；其余由 automation 推进。"),
            "你负责研究方向、初始投资论点和关键决策；其余由系统自动流程推进。")
        # English lines and quotations are left as written.
        for text in ("CapEx guidance was raised.", "管理层说 “Thesis intact”。"):
            self.assertEqual(owner_terms_text(text), text)

    def test_a_review_candidate_is_labelled_for_the_owner(self):
        from dalton_core.research_review_control import candidate_display

        shown = candidate_display({
            "subject_ref": "company:ticker:amzn", "metric_or_aspect": "metric:revenue",
            "period": "Year Ended December 31, 2025", "basis": "company-filed-document",
            "normalized_statement": ("Net sales for Year Ended December 31, 2025 was USD 716924 million "
                                     "(currency), as published by the company in this document."),
            "value": "716924", "unit": "currency", "currency": "USD", "scale": "million",
            "source_type": "official_filing", "source_ref": "source:sec-edgar",
            "promotion": {"authority": "policy", "claim_version_ref": "claim-version:" + "b" * 64},
        })
        self.assertEqual(shown["subject"], "AMZN")
        self.assertEqual(candidate_display({"subject_ref": "company:sec-cik:0001467373"})["subject"],
                         "ACN · Accenture")
        self.assertEqual(shown["metric"], "营业收入")
        self.assertEqual(shown["source"], "公司申报文件 · SEC 财报数据")
        self.assertEqual(shown["value"], "7169 亿美元")
        self.assertEqual(shown["basis"], "公司申报文件")
        self.assertEqual(shown["promotion"], "已由规则自动入库")
        self.assertIn("营业收入", shown["statement"])
        self.assertNotIn("claim-version", repr(shown))

    def test_a_context_overflow_is_not_called_temporary(self):
        from dalton_core.cockpit_plane import _job_error_message

        shown = _job_error_message("the question and its context exceed the model input bound")
        self.assertIn("超出了模型单次能读的上限", shown)
        self.assertIn("缩小问题范围", shown)
        self.assertNotIn("稍后重试", shown)
        self.assertIsNone(_job_error_message(None))


if __name__ == "__main__":
    unittest.main()
