"""Cockpit v2 shell (ADR-0016), checked by running its modules under node.

* URL state: every page, company, tab and filter has a stable hash.
* Page registration: the five analyst pages and the standalone company page.
* Copy: section 8 of the 2026-10-08 analyst UX review, every row.
* Formatting matches ``numeric_display``; UI-text keys match the server's.
* Rendering with a fake DOM: page order, persistent labels, fallbacks, and
  no "null"/"undefined" leaking into the page.
* Colour pairs meet WCAG AA; the old cockpit gains exactly one link.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from dalton_core.numeric_display import format_display_number, format_typed_value

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "src/dalton_core/cockpit_v2"
HARNESS = ROOT / "tests/cockpit_v2_harness.mjs"
REVIEW = ROOT / "docs/reports/cockpit-analyst-ux-review-2026-10-08.md"


def node(command: str, value: dict) -> dict:
    result = subprocess.run(["node", str(HARNESS), command, json.dumps(value, ensure_ascii=False)],
                            text=True, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-3000:])
    return json.loads(result.stdout)


def review_copy_rows() -> list[list[str]]:
    section = REVIEW.read_text(encoding="utf-8").split("## 8.")[1].split("## 9.")[0]
    rows = []
    for line in section.splitlines():
        if line.startswith("| ") and not line.startswith("| 当前表达") and not line.startswith("|---"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def company(ticker, name, ref, *, ret="9.42", notes=("代理口径：驱动模型没有算出净利润",), bear="0.30", month=5.2):
    return {
        "company_ref": ref, "ticker": ticker, "name": name, "stage": "公司模型", "stage_status": "已通过",
        "note": "还差：过去 4 个季度的电话会纪要（3/4）",
        "gate_decision": {"label": "历史检查已通过"},
        "source_readiness": {"ready": False, "label": "当前资料待补齐", "gaps": ["earnings_calls"]},
        "market": {"as_of": "2026-10-07", "close": "196.64", "currency": "USD", "note": "收盘价", "provisional": False,
                   "changes": [{"days": 5, "horizon": "5D", "label": "近5个交易日", "status": "available", "percent": 7.2},
                               {"days": 30, "horizon": "30D", "label": "近30个交易日", "status": "available", "percent": month}]},
        "valuation": {"as_of": "2026-10-07", "metrics": [
            {"metric": "trailing_pe", "label": "trailing price / earnings", "unit": "ratio", "status": "available",
             "value": "15.4475", "percentile": "48.59", "sample_size": 249, "percentile_basis_label": "股价与已报基本面一起算出来的"},
            {"metric": "fcf_yield", "label": "free cash flow yield", "unit": "fraction", "status": "available", "value": "0.104558"},
            {"metric": "ev_to_ebitda", "label": "enterprise value / EBITDA", "unit": "ratio", "status": "unavailable",
             "value": None, "reason": "the filings held carry no depreciation"}]},
        "model": {"note": "8 个未来季度，9/15 条驱动因素有假设"},
        "figures": {"latest": [{"label": "Adjusted diluted earnings per share", "display_label": "调整后每股收益",
                                "period_label": "2025财年", "value": "12.93", "unit": "currency", "currency": "USD",
                                "metric_ref": "metric:adj-eps", "grade_label": "公司文件披露"}]},
        "events": {"total": 2, "latest": [{"kind": "filing", "kind_label": "公司报表", "occurred_at": "2026-10-08T04:29:00+00:00",
                                            "summary": "sec:filing:0001688568-26-000068", "tier_label": "一手"}]},
        "judgements": {"total": 3, "latest": [{"at": "2026-10-08T06:19:06+00:00", "decision": "NO_CHANGE", "action": "no_change",
                                                "decision_label": "维持现有判断", "because": "收入假设维持原判。管理层称 EPS 增长 10%。",
                                                "verifier_label": "独立复核通过", "verifier_verdict": "pass", "event_kind_label": "新结论"}]},
        "catalyst": {"event_label": "除息日", "expected_date": "2026-10-12", "days_until": 4, "date_unconfirmed": True},
        "scenarios": {"probability_sum": "1.00", "in_force": [
            {"key": "base", "name": "订单温和", "probability": "0.50", "narrative": "可选咨询仍偏弱。",
             "triggers": [{"kind": "trigger", "display_label": "单季营收超过上季", "rule": "营业收入 > 187.2 亿美元"}]},
            {"key": "bear", "name": "AI 通缩", "probability": bear, "narrative": "否定：收入下滑。", "triggers": []},
            {"key": "bull", "name": "AI 加速", "probability": "0.20", "narrative": "增长。", "triggers": []}]} if bear else {"in_force": []},
        "return_attribution": {"headline": "24 个申报期的利润桥", "returns": {"legs": [
            {"leg": "realised_trailing_year", "label": "已实现（过去 12 个月）", "status": "available",
             "window": {"start": "2025-10-07", "end": "2026-10-07"}, "total_return_percent": "-19.19", "price_return_percent": "-21.73",
             "components": [{"label": "估值倍数变化", "contribution_pp": "-24.39"}, {"label": "股息", "contribution_pp": "2.54"}],
             "notes": ["EPS 按财务期间对齐，不是当时已知的数"]},
            {"leg": "expected_driver_model", "label": "预期：我们的驱动模型（未来 4 个季度）", "status": "available" if ret else "unavailable",
             "reason": None if ret else "模型没有净利润", "window": {"start": "2026-06-01", "end": "2027-05-31"},
             "total_return_percent": ret, "price_return_percent": "6.18",
             "components": [{"label": "盈利增长", "contribution_pp": "6.18"}, {"label": "股数变化", "contribution_pp": None}],
             "notes": list(notes)}]}},
    }


OVERVIEW = {
    "as_of": "2026-10-08T10:17:56+00:00",
    "workspace": {"name": "美国 IT 服务研究"},
    "goal": {"title": "US IT", "display_title": "美国 IT 服务首次覆盖任务", "display_objective": "建立首次覆盖。",
             "version": 28, "published_at": "2026-09-29T11:37:07+00:00", "research_questions": ["需求何时见底？"]},
    "companies": [company("ACN", "Accenture", "company:sec-cik:0001467373"),
                  company("IBM", "IBM", "company:sec-cik:0000051143", ret=None, bear=None, month=-5.8)],
    "theses": [{"summary": "US IT services demand is approaching a bottom.", "confidence": "low"}],
    "totals": {"claims_today": 0},
    "budgets": {"model_calls": {"used": 1345, "cap": 100000, "cost_usd": 88.4165, "cost_cap_usd": 500.0},
                "shared_daily": {"cost_usd": 212.16, "cost_cap_usd": 500.0, "note": "上限按所有研究环境合并计算。"}},
    "model_available": {"available": True},
}
NEEDS = {"as_of": "2026-10-08T10:17:58+00:00", "items": [
    {"kind": "raw_spool_capacity", "ref": "spool", "at": "2026-10-08T06:40:00+00:00", "urgency": 2, "urgency_label": "有一整个环节停着",
     "title": "连接器原始资料存储已用到上限的 97%", "why_blocked": "x", "action": "python -m dalton_core.raw_spool_maintenance --apply",
     "consequence": "下一次抓取会失败。", "actionable": True, "detail": {}},
    {"kind": "controlled_recovery", "ref": "rec", "at": "2026-10-08T06:38:00+00:00", "urgency": 2, "urgency_label": "有一整个环节停着",
     "title": "等你决定：EPAM · 收入怎么拆分？", "why_blocked": "重新进入失败（已自动改绑票据并重试一次）",
     "action": "（不加 --apply 是只读预览，只看不改）", "consequence": "问题一直没有答案。", "actionable": True,
     "detail": {"admission_ref": "mission-document-research-admission:09b4", "company_ref": "company:sec-cik:0000051143",
                "reason_label": "重新进入失败（已自动改绑票据并重试一次）", "lane": "mission_document_research"}},
    {"kind": "controlled_recovery", "ref": "rec-all", "at": "2026-10-08T06:38:00+00:00", "urgency": 2, "urgency_label": "有一整个环节停着",
     "title": "有 2 项研究任务停着", "why_blocked": "", "action": "", "consequence": "", "actionable": True,
     "detail": {"lane": "mission_document_research", "held": 2}},
    {"kind": "earnings_release_unread", "ref": "er", "at": "2026-10-08T04:46:00+00:00", "urgency": 4, "urgency_label": "积压的待办",
     "title": "ACN · Accenture 的业绩稿没取到附件 99.1", "why_blocked": "", "action": "", "consequence": "不影响其他研究。",
     "actionable": True, "detail": {"company_ref": "company:sec-cik:0001467373"}},
    {"kind": "no_active_mission", "ref": "nm", "at": None, "urgency": 1, "urgency_label": "挡住了整条研究链",
     "title": "研究环境「新研究环境」还没有研究目标", "why_blocked": "", "action": "",
     "consequence": "这个环境就一直空转：占着端口和进程，不产出任何研究。", "actionable": True, "detail": {}},
]}
APPROVALS = {"items": [{"kind": "gate_decision", "ref": "gate:1", "hash": "h" * 64, "title": "确认 ACN 的初步筛选", "who": "ACN · Accenture",
                        "at": "2026-10-08T01:00:00+00:00", "summary": "初步筛选已完成。", "needs_rationale": True,
                        "actions": [{"decision": "approve", "label": "通过"}, {"decision": "reject", "label": "退回补充"}]}]}
RESEARCH = {"products": [
    {"kind": "initial_screen", "label": "初步筛选", "status": "available", "version_label": "第 3 版", "created_at": "2026-09-14T00:00:00+00:00",
     "publication_status": "draft", "draft_label": "草稿，文字表达尚未校对",
     "sections": [{"title": "关键信息 + 执行摘要", "body": "当前判断是：需求仍有支撑。", "display_body": "当前判断是：需求仍有支撑。",
                   "sources": [{"text": "ACN defines its two types of work.", "period": "Fiscal 2025"}], "display_gaps": ["缺少订单数据。"]}]},
    {"kind": "debate_map", "label": "核心争议", "status": "available", "version_label": "第 15 版", "created_at": "2026-10-01T00:00:00+00:00",
     "publication_status": "ready", "sections": [{"title": "AI 能否抵消？ · 多方", "body": "能。"}, {"title": "AI 能否抵消？ · 我们的判断", "body": "待验证。"}]},
    {"kind": "investment_memo", "label": "投资备忘录", "status": "missing", "reason": "尚未发布此类研究产物"}]}
MODEL = {"version": 8, "created_at": "2026-10-07T23:43:02+00:00", "change_reason_label": "关键假设已经复核",
         "note": "仍有 12 项结果缺少计算条件",
         "readiness": {"forecast_quarters": 8, "history_quarters": 8, "drivers": 15, "drivers_with_assumptions": 9,
                       "results_unavailable_labels": ["净利润", "每股收益"]},
         "table": "驱动模型\n  本行为收入（Revenues）科目。 [收入驱动]  17,596.3\n  少数股东损益  1.0",
         "history": [{"version": 8, "created_at": "2026-10-07T23:43:02+00:00", "change_reason_label": "关键假设已经复核"},
                     {"version": 7, "created_at": "2026-10-06T23:43:02+00:00", "change_reason_label": "新财报"}]}
CLAIMS = {"total": 2086, "all_versions_total": 2312, "items": [
    {"ref": "claim-version:1", "statement": "Accenture reported Revenues of USD 18718144000.", "display_statement": "埃森哲营收 187.2 亿美元。",
     "company": "ACN · Accenture", "period_label": "2026年3月1日至2026年5月31日", "aspect_label": "业务构成",
     "importance_label": "公司报表原文", "as_of": "2026-05-31", "at": "2026-08-26T19:02:30+00:00"}],
    "next_cursor": None, "vocabulary": {"aspects": [{"value": "business_model", "label": "商业模式与盈利来源"}],
                                        "importance": [{"value": "filing", "label": "公司报表原文"}]}}
# The chart API's comparison payload: the one ranking source (O01/O13).
COMPARISON = {"kind": "comparison", "status": "ok", "sort": "return", "dir": "desc", "notes": [],
    "rule": {"id": "same-basis-same-window-v1", "label": "同口径、同期间才排名", "text": "每一列只把同一口径、同一期间的数放在一起排名。"},
    "columns": [
    {"key": "change", "label": "近 30 个交易日涨跌", "sort": "change", "basis": {"basis_label": "截至 2026-10-07 的近 30 个交易日"}},
    {"key": "expected", "label": "模型预期总回报（简化估算）", "sort": "return", "basis": {"basis_label": "模型预期总回报，2026-06-01 至 2027-05-31"}},
    {"key": "risk", "label": "风险：悲观情景概率", "sort": "risk", "basis": None}],
    "rows": [
        {"company_ref": "company:sec-cik:0001467373", "ticker": "ACN", "company": "Accenture ACN", "cells": {
            "change": {"value": "5.2", "display": "+5.2%", "comparable": True},
            "expected": {"value": "9.42", "display": "+9.42%", "comparable": False, "estimated": True,
                         "incomparable_reason": "口径不同：模型预期总回报，2026-06-01 至 2027-05-31"},
            "risk": {"value": "30", "display": "30%", "comparable": True}}},
        {"company_ref": "company:sec-cik:0000051143", "ticker": "IBM", "company": "IBM", "cells": {
            "change": {"value": "-5.8", "display": "-5.8%", "comparable": True},
            "expected": {"value": None, "missing_reason": "模型没有净利润"},
            "risk": {"value": None, "missing_reason": "还没有情景假设"}}}]}
FIXTURES = {
    "/v2/data/charts/comparison": COMPARISON,
    "/v1/cockpit/overview": OVERVIEW, "/v1/cockpit/needs-human": NEEDS, "/v1/cockpit/approvals": APPROVALS,
    "/v1/cockpit/research": RESEARCH, "/v1/cockpit/model": MODEL, "/v1/cockpit/claims": CLAIMS,
    "/v1/cockpit/history?kind=ask": {"items": [
        {"status": "failed", "request": {"question": "ACN 最近的业绩怎么解读？"}, "display_message": "请缩小问题范围后再问。",
         "created_at": "2026-10-07T23:25:55+00:00"},
        {"status": "done", "request": {"question": "DXC 利润改善来自哪里？"}, "created_at": "2026-09-14T03:07:00+00:00",
         "result": {"display_answer": "DXC 收入仍在收缩。利润修复靠降本。尚未形成需求复苏。第四句。", "confidence": "medium", "cost_usd": 0.4026,
                    "display_gaps": ["缺少订单金额。"], "citations": [{"statement": "最新变化是需求走弱。", "block_label": "公司档案"}]}}]},
    "/v1/cockpit/history?kind=goal": {"items": [], "drafts": []},
    "/v1/cockpit/levers": {"companies": [{"label": "ACN · Accenture", "coverage_tier": "A", "paused": False},
                                         {"label": "IBM", "coverage_tier": "C", "paused": True, "paused_label": "暂停到明天"}]},
    "/v1/cockpit/conviction-policy": {"effective": {"source_label": "代码默认（尚未发布任何版本）",
                                                    "fields": [{"label": "判断规则版本", "value": "v2"}]}},
    # batch ah: the company page's grid and the profit-bridge line read these.
    "/v2/data/charts/model": {
        "kind": "model", "status": "ok", "company": "Accenture ACN", "version": 8, "model_created_at": "2026-10-07T19:43:00+00:00",
        "unit": "亿美元", "unit_header": "金额：亿美元", "period": {"label": "单季（公司财年口径）"}, "boundary_index": 1, "notes": [],
        "columns": [{"end": "2026-05-31", "kind": "history", "label": "26/05", "title": "2026年3月1日至2026年5月31日（已披露）"},
                    {"end": "2026-08-31", "kind": "forecast", "label": "26/08", "title": "2026年6月1日至2026年8月31日（预测）"}],
        "sections": [{"key": "income", "label": "利润表"}],
        "rows": [{"key": "r1", "label": "营业收入", "role": "revenue", "section": "income", "unit_kind": "amount", "unit_label": "亿美元",
                  "cells": [{"status": "ok", "display": "187.18", "basis": "reported"}, {"status": "ok", "display": "180.44", "basis": "estimate"}]},
                 {"key": "r2", "label": "少数股东损益", "role": "nci_other", "section": "income", "unit_kind": "amount", "unit_label": "亿美元",
                  "cells": [{"status": "ok", "display": "0.33", "basis": "reported"}, {"status": "missing", "display": None, "reason": "缺少净利润基准"}]}],
        "completeness": {"results_total": 2, "results_computed": 1, "label": "预测结果 1/2 行已算出",
                         "results_blocked": [{"label": "少数股东损益", "reason": "缺少净利润基准"}]},
        "series": {"revenue": {"label": "营业收入", "unit": "亿美元", "points": []}, "margins": []}},
    "/v2/data/evidence/statement-line": {
        "status": "ok", "company": "Accenture ACN",
        "lines": [{"relation_label": None, "document_type_label": "季报 10-Q", "source_kind": "sec_statement_line",
                   "title": "Accenture plc 10-Q", "filing": {"form": "10-Q", "accession": "0001467373-26-000032", "filed": "2026-06-20",
                                                              "report_date": "2026-05-31", "entity_name": "Accenture plc",
                                                              "period_label": "2026年3月1日至2026年5月31日"},
                   "location": {"label": "利润表 第 12 行：Revenues"},
                   "passage": {"text": "营业收入（2026年3月1日至2026年5月31日）：187.18 亿美元", "highlight": [0, 4], "kind": "filed_line"},
                   "links": [{"kind": "original", "label": "在 SEC EDGAR 打开原始申报（10-Q）",
                              "url": "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000032/0001467373-26-000032-index.htm"}],
                   "claims": ["claim-version:1"], "technical": {"accession": "0001467373-26-000032", "concept": "us-gaap:Revenues"}}]},
}
EMPTY_FIXTURES = {
    "/v1/cockpit/overview": {"as_of": "2026-10-08T10:18:06+00:00", "state": "awaiting_mission", "goal": None,
                             "workspace": {"name": "新研究环境"}},
    "/v1/cockpit/needs-human": {"items": [NEEDS["items"][-1]]},
    "/v1/cockpit/approvals": {"items": []},
    "/v1/cockpit/history?kind=ask": {"items": []},
    "/v1/cockpit/history?kind=goal": {"items": [], "drafts": []},
    "/v1/cockpit/levers": {"companies": []},
    "/v1/cockpit/conviction-policy": {"effective": None},
}
# batch am: the registry's section titles (terminology.LABELS["company_section"]).
COMPANY_ORDER = ["核心投资观点与论证", "估值分析与目标价测算", "下行风险与关键催化剂", "事实底稿与引用证据库", "财务预测模型（三大表）",
                 "重大事件与研判跟踪", "情景分析与概率配置", "业绩拆解与回报归因", "投研报告与投资备忘录"]


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ShellLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fmt_cases = [
            ["money", ["18718144000"]], ["money", [15620000]], ["money", ["-9999.995"]], ["money", [0]],
            ["money", ["123456789012"]], ["money", ["12345.678"]], ["money", ["99999.5"]], ["money", ["1.005"]],
            ["percent", ["5.59"]], ["percent", ["-0.05"]], ["eps", ["12.925"]], ["eps", ["4.8898"]],
            ["typed", ["12.93", {"unit": "currency", "currency": "USD", "metric": "eps"}]],
            ["typed", ["279000", {"unit": "usd", "scale": "million"}]],
            ["typed", ["10", {"unit": "percent"}]],
            ["typed", ["3.5", {"unit": "usd_per_user"}]],
            ["typed", ["30", {"unit": "basis_points"}]],
        ]
        cls.texts = ["研究项目", "Accenture plc reported Revenues of USD 18718144000", "", "😀 emoji · 中文", "x" * 300]
        cls.copy_inputs = ["研究环境「新研究环境」还没有研究目标", "维持现状", "维持现有判断", "本行为收入（Revenues）科目。",
                           "本行为服务成本（Cost of services）科目。", "否定：收入下滑", "重新进入失败（已自动改绑票据并重试一次）",
                           "先看原因（不加 --apply 是只读预览，只看不改）；不值得再试", "占着端口和进程，不产出任何研究",
                           "已绑定来源 · 已绑定来源", "15 份研究产物", "Conviction v2 阈值", "少数股东损益", "可赎回少数股东损益",
                           "草稿，文字表达尚未校对", "下载 HTML 报告", "历史检查已通过", "代码默认（尚未发布任何版本）",
                           "这一条由你判断，程序不读", "等你在待办里裁决", "研究车道与研究线", "资料底座齐了", "book-to-bill 低于一",
                           "目的地", "销售纸条", "只读投影", "没有基线", "本机连接器目录已登记", "研究目标尚未声明该来源",
                           "由你观察", "投资者日说法（由你判断，2026-10-31 前）"]
        cls.out = node("pure", {
            "hashes": ["#/overview", "#/company/ACN/model?aspect=demand_drivers&importance=filing", "#/companies?sort=ret&dir=asc",
                       "#/tasks?group=urgency", "", "#/ask?q=%E5%88%A9%E6%B6%A6%E7%8E%87"],
            "texts": cls.texts, "fmt": cls.fmt_cases, "copy": cls.copy_inputs,
            "gloss": "EPS 与 EPS、10-K 及 ROIC、CapEx", "debate": "AI 能否抵消？ · 市场看法"})

    def test_hash_round_trip_and_state_rules(self):
        out = self.out
        self.assertEqual(out["parse"][1], {"page": "company", "segments": ["ACN", "model"],
                                           "filters": {"aspect": "demand_drivers", "importance": "filing"}})
        self.assertEqual(out["roundtrip"][1], "#/company/ACN/model?aspect=demand_drivers&importance=filing")
        self.assertEqual(out["roundtrip"][2], "#/companies?dir=asc&sort=ret")
        self.assertEqual(out["roundtrip"][4], "#/overview")
        self.assertEqual(out["parse"][5]["filters"], {"q": "利润率"})
        self.assertEqual(out["resolved"], [
            "#/company/ACN",
            "#/company/ACN/events?aspect=demand_drivers",   # tab change keeps the filters
            "#/company/MSFT",                                # company change drops tab and filters
            "#/overview",                                    # page change drops everything
            "#/company/ACN/model?dir=asc&sort=ret",
        ])
        self.assertEqual(out["fromHash"]["company"], "ACN")
        self.assertEqual(out["fromHash"]["tab"], "model")
        self.assertEqual(out["unknown"]["page"], "overview")
        self.assertEqual(len(out["errors"]), 3)

    def test_formatting_matches_the_server_formatters(self):
        kinds = {"money": "amount_usd", "percent": "percent", "eps": "eps"}
        for (name, args), shown in zip(self.fmt_cases, self.out["fmt"]):
            with self.subTest(name=name, args=args):
                if name == "typed":
                    options = args[1]
                    expected = format_typed_value(args[0], unit=options.get("unit", ""), scale=options.get("scale"),
                                                  currency=options.get("currency"), metric=options.get("metric", ""))
                else:
                    expected = format_display_number(args[0], kind=kinds[name])
                self.assertEqual(shown, expected)

    def test_ui_text_keys_are_the_servers(self):
        for text, key in zip(self.texts, self.out["keys"]):
            self.assertEqual(key, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])

    def test_every_row_of_the_review_copy_table_is_implemented(self):
        table = {row["current"]: row for row in self.out["table"]}
        rows = review_copy_rows()
        self.assertEqual(len(rows), 35)
        for current, suggested, note in rows:
            with self.subTest(current=current):
                self.assertIn(current, table)
                self.assertEqual(table[current]["suggested"], suggested)
                self.assertEqual(table[current]["note"], note)
                mode = table[current]["mode"]
                self.assertIn(mode, {"replace", "keep", "format", "structure", "remove", "page"})
                if mode in {"replace", "remove", "page"}:
                    self.assertGreater(table[current]["rules"], 0)

    def test_copy_rules(self):
        shown = dict(zip(self.copy_inputs, self.out["copy"]))
        self.assertTrue(all(self.out["copyTwice"]))
        self.assertEqual(shown["研究环境「新研究环境」还没有研究目标"], "研究项目「新研究环境」还没有研究目标")  # names untouched
        self.assertEqual(shown["维持现状"], "当前观点不变")
        self.assertEqual(shown["维持现有判断"], "当前观点不变")
        self.assertEqual(shown["本行为收入（Revenues）科目。"], "营业收入")
        self.assertEqual(shown["本行为服务成本（Cost of services）科目。"], "服务成本")
        self.assertEqual(shown["否定：收入下滑"], "情景失效条件：收入下滑")
        self.assertEqual(shown["重新进入失败（已自动改绑票据并重试一次）"], "研究恢复失败，需管理员处理")
        self.assertEqual(shown["先看原因（不加 --apply 是只读预览，只看不改）；不值得再试"], "先看原因；不值得再试")
        self.assertEqual(shown["占着端口和进程，不产出任何研究"], "设定研究目标后即可开始")
        self.assertEqual(shown["已绑定来源 · 已绑定来源"], "来源信息待补齐")
        self.assertEqual(shown["15 份研究产物"], "15 份研究报告")
        self.assertEqual(shown["Conviction v2 阈值"], "投资建议准入与纪律规则 阈值")
        self.assertEqual(shown["少数股东损益"], "少数股东损益")
        self.assertEqual(shown["可赎回少数股东损益"], "可赎回少数股东损益")
        self.assertEqual(shown["草稿，文字表达尚未校对"], "初稿（待审核签发）")
        self.assertEqual(shown["下载 HTML 报告"], "下载研究报告")
        self.assertEqual(shown["历史检查已通过"], "上次评审通过")
        self.assertEqual(shown["代码默认（尚未发布任何版本）"], "机构默认投研纪律标准")
        self.assertEqual(shown["这一条由你判断，程序不读"], "需人工核实确认")   # 2026-10-09 review wording
        self.assertEqual(shown["等你在待办里裁决"], "请确认情景假设")
        self.assertEqual(shown["研究车道与研究线"], "研究流程与专项研究")
        self.assertEqual(shown["资料底座齐了"], "所需研究资料已齐备")
        self.assertEqual(shown["book-to-bill 低于一"], "订单收入比低于 1 倍")
        self.assertEqual(shown["目的地"], "发送到")
        self.assertEqual(shown["销售纸条"], "销售简报")
        self.assertEqual(shown["只读投影"], "记录")
        self.assertEqual(shown["没有基线"], "尚未设置更新频率")
        self.assertEqual(shown["本机连接器目录已登记"], "服务已配置")
        self.assertEqual(shown["研究目标尚未声明该来源"], "本项目尚未启用")
        self.assertEqual(shown["由你观察"], "需人工核实确认")
        self.assertEqual(shown["投资者日说法（由你判断，2026-10-31 前）"], "投资者日说法（需人工核实确认，2026-10-31 前）")

    def test_finance_terms_are_kept_and_glossed_once(self):
        terms = [part for part in self.out["gloss"] if part.get("term")]
        self.assertEqual([t["term"] for t in terms], ["EPS", "10-K", "ROIC", "CapEx"])
        self.assertEqual(terms[0]["gloss"], "每股收益")
        self.assertEqual("".join(p["text"] for p in self.out["gloss"]), "EPS 与 EPS、10-K 及 ROIC、CapEx")
        self.assertEqual(self.out["debate"], {"question": "AI 能否抵消？", "role": "市场观点"})

    def test_widget_slots_of_the_contract(self):
        for slot in ("overview.header", "company.summary", "company.model", "company.valuation", "company.returns",
                     "company.scenarios", "settings.admin_link"):
            self.assertIn(slot, self.out["slots"])


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class PageRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = node("pages", {"fixtures": FIXTURES, "render": {
            "overview": "#/overview", "companies": "#/companies", "company": "#/company/ACN",
            "ibm": "#/company/IBM/returns", "missing": "#/company/ZZZ", "ask": "#/ask", "tasks": "#/tasks",
            "tasks_flat": "#/tasks?group=urgency", "settings": "#/settings?focus=goal"}})
        cls.empty = node("pages", {"fixtures": EMPTY_FIXTURES, "render": {
            "overview": "#/overview", "companies": "#/companies", "tasks": "#/tasks", "settings": "#/settings", "ask": "#/ask"}})
        cls.widget = node("pages", {"fixtures": FIXTURES, "widgets": ["company.model", "company.summary"], "admin": True,
                                    "render": {"company": "#/company/ACN/model", "settings": "#/settings"},
                                    "event": {"page": "company", "type": "dalton:statement-line",
                                              "detail": {"label": "营业收入", "concept": "us-gaap:Revenues", "line_id": "statement-ingest:abc#12",
                                                         "accession": "0001467373-26-000032"}}})
        cls.line = node("pages", {"fixtures": FIXTURES, "render": {
            "evidence": "#/company/ACN/evidence?line=%E8%90%A5%E4%B8%9A%E6%94%B6%E5%85%A5&line_id=statement-ingest%3Aabc%2312"}})

    def test_registration_and_navigation(self):
        self.assertEqual(self.out["nav"], ["研究总览", "公司研究", "智能投研问答", "待办决策", "投研规则与设置"])
        pages = {p["id"]: p for p in self.out["pages"]}
        self.assertEqual(pages["company"]["params"], ["company", "tab"])
        self.assertEqual(pages["company"]["parent"], "companies")
        self.assertIsNone(pages["company"]["nav"])

    def test_no_page_prints_null_or_undefined(self):
        for out in (self.out, self.empty, self.widget):
            for name, result in out["results"].items():
                if isinstance(result, dict):
                    with self.subTest(page=name):
                        self.assertFalse(result["nullText"], result["text"][:400])

    def test_overview_puts_conclusions_changes_and_decisions_first(self):
        r = self.out["results"]["overview"]
        self.assertEqual(r["h1"], "研究总览 · 投研动态看板")
        self.assertEqual(r["sections"][:4], ["conclusions", "changes", "decide", "compare"])
        self.assertIn("新增 SEC 官方申报（内容解析中）", r["text"])   # not "sec:filing:..."
        self.assertNotIn("sec:filing", r["text"])
        # 2026-10-09 / am 3.1: the first screen answers four questions.
        for label in ("核心投资判断", "研判依据与主要变化", "受影响标的", "待推进投研事项"):
            self.assertIn(label, r["text"])
        self.assertLess(r["text"].index("核心投资判断"), r["text"].index("近期重大异动与增量信息"))
        self.assertNotIn("维持观点：", r["text"])                    # am: no 「维持观点：维持现状」 echo
        self.assertIn("已入库基础事实与研报摘录（共", r["text"])     # am 3.3: material apart from views
        # One comparison from the chart payload (O01): incomparable said, estimate labelled.
        self.assertIn("/v2/data/charts/comparison", self.out["fetched"])   # company_compare.comparisonSection
        self.assertIn("口径暂不可比（预测周期不一致）", r["text"])
        self.assertIn("口径不同：模型预期总回报", r["text"])          # the server's reason, verbatim
        self.assertIn("模型没有净利润", r["text"])                  # IBM: the reason, not a blank
        self.assertNotIn("运行故障与失败台账", r["text"])            # G02: no admin ledger here
        self.assertIn("overview.compare", r["fallbacks"])
        self.assertIn("overview.header", r["fallbacks"])
        self.assertIn(["Accenture ACN", "#/company/ACN"], r["links"])
        self.assertIn(["IBM", "#/company/IBM"], r["links"])       # no "IBM IBM" (C02)

    def test_company_page_order_toc_and_fallbacks(self):
        r = self.out["results"]["company"]
        self.assertEqual(r["h1"], "Accenture ACN")
        self.assertEqual([t[0] for t in r["toc"]], COMPANY_ORDER)
        self.assertEqual(r["toc"][4], ["财务预测模型（三大表）", "#/company/ACN/model"])
        self.assertEqual(r["h2"], COMPANY_ORDER)
        # The view and the valuation table are the page's own; the chart slots
        # there add a picture.  Model, scenarios and returns fall back to tables.
        self.assertEqual(set(r["fallbacks"]), {"company.model", "company.scenarios", "company.returns"})
        text = r["text"]
        self.assertIn("上次评审通过", text)
        self.assertIn("当前资料待补齐", text)
        self.assertIn("当前观点不变", text)
        # A scenario name is not a formally admitted house thesis.
        self.assertIn("正式本所观点尚未形成", text)
        self.assertNotIn("当前判断以基准情景为准", text)
        self.assertIn("市盈率（过去 12 个月，倍）", text)
        self.assertIn("自由现金流收益率（%）", text)
        self.assertIn("10.5%", text)
        self.assertIn("营业收入", text)                            # model row name, not the sentence
        self.assertNotIn("本行为收入", text)
        self.assertIn("少数股东损益", text)
        self.assertIn("TSR Attribution", text)
        self.assertIn("初稿（待审核签发）", text)
        self.assertIn("情景失效条件：", text)
        self.assertIn("埃森哲营收 187.2 亿美元。", text)
        self.assertIn("下载财务预测底稿（.xlsx）", r["buttons"])
        self.assertIn("导出公司研究全景报告（可在浏览器中打印为 PDF）", r["buttons"])
        self.assertIn(["研究议题分类", "ev-aspect"], r["labels"])
        self.assertIn("三大表预测完备度：", text)                    # am 5.5
        self.assertNotIn("个百分点", text.split("TSR Attribution")[1])   # am 5.8: one unit declaration

    def test_company_without_a_return_or_scenarios(self):
        text = self.out["results"]["ibm"]["text"]
        self.assertIn("还没有生效的情景", text)
        self.assertIn("模型没有净利润", text)
        missing = self.out["results"]["missing"]
        self.assertEqual(missing["h1"], "找不到这家公司")

    def test_tasks_group_by_kind_with_completing_entries(self):
        r = self.out["results"]["tasks"]
        self.assertEqual(r["h2"][0], "需要你确认的研究决定")
        self.assertIn("投研决策与审批（2）", r["h2"])               # the aggregate recovery card folded away
        self.assertIn("技术与连接协助（1）", r["h2"])
        self.assertIn("后台自动运行中（无需操作，仅供备查）（1）", r["h2"])
        self.assertIn("暂缓当前专项课题（保留现有底稿）", r["buttons"])
        self.assertIn("通过", r["buttons"])
        self.assertIn("一键复制故障信息供 IT 协助", r["buttons"])     # kept, under the technical details
        self.assertIn("待开始", r["text"])                          # O09: no goal is not 「挡住了整条研究链」
        self.assertNotIn("挡住了整条研究链", r["text"])
        self.assertIn(["设定研究目标", "#/settings?focus=goal"], r["links"])
        self.assertNotIn("--apply", r["text"])
        self.assertNotIn("端口", r["text"])
        self.assertIn("研究对象：IBM", r["text"])
        self.assertIn("研究对象：Accenture ACN", r["text"])
        flat = self.out["results"]["tasks_flat"]
        self.assertIn("全部事项", flat["h2"])

    def test_checklist_period_step_shows_under_the_item_note(self):
        step = "候选已选中，等待获取：AlphaEngine 24 小时调用额度已用完（已用 49/50，另有 1 次留给搜索），约 2026-10-08 20:23 UTC 起恢复"
        fixtures = json.loads(json.dumps(FIXTURES, ensure_ascii=False))
        acn = fixtures["/v1/cockpit/overview"]["companies"][0]
        acn["checklist"] = [
            {"item_ref": "earnings_calls", "label": "过去 4 个季度的电话会纪要", "status": "partial",
             "note": "还差 1 份", "period_step": step},
            {"item_ref": "annual_report", "label": "最新年报（10-K）正文", "status": "complete", "note": "已有 1 份，够了", "period_step": ""},
            {"item_ref": "broker_research", "label": "近 6 个月的多空券商观点", "status": "complete", "note": "已有 40 份，够了"}]
        out = node("pages", {"fixtures": fixtures, "render": {"overview": "#/overview", "company": "#/company/ACN"}})["results"]
        company = out["company"]["text"]
        self.assertIn("资料清单", company)
        self.assertIn("过去 4 个季度的电话会纪要：还差 1 份" + step, company)   # note unchanged, the step right under it
        self.assertNotIn("最新年报（10-K）正文：已有 1 份", company)               # an empty step adds nothing
        overview = out["overview"]["text"]
        self.assertIn("资料收集中断", overview)
        self.assertIn(f"过去 4 个季度的电话会纪要：{step}", overview)
        self.assertLess(overview.index(step), overview.index("重点待办决策"))       # in the changes, before decisions
        acn["checklist"][0]["status"] = "complete"                                  # not blocking: company page only
        out = node("pages", {"fixtures": fixtures, "render": {"overview": "#/overview", "company": "#/company/ACN"}})["results"]
        self.assertNotIn(step, out["overview"]["text"])
        self.assertIn(step, out["company"]["text"])

    def test_admin_handoff_uses_the_admin_interface_when_installed(self):
        settings = self.widget["results"]["settings"]
        self.assertIn(["打开管理员界面", "/v2/admin"], settings["links"])
        self.assertIn(["历史界面（旧版）", "/legacy"], self.out["results"]["settings"]["links"])   # G01: in 帮助

    def test_widgets_replace_fallbacks(self):
        r = self.widget["results"]["company"]
        self.assertIn("WIDGET:company.model", r["text"])
        self.assertNotIn("company.model", r["fallbacks"])
        self.assertIn("WIDGET:company.summary", r["text"])
        self.assertIn("当前判断是：需求仍有支撑。", r["text"])      # the view stays beside the chart
        self.assertLess(r["text"].index("当前判断是"), r["text"].index("WIDGET:company.summary"))
        self.assertEqual(sorted(self.widget["widgetsUsed"]), [["company.model", "company", "company_ref", "model"],
                                                              ["company.summary", "company", "company_ref", "research", "header_shown"]])

    def test_profit_bridge_line_opens_the_evidence(self):
        # C13: the exact filed line travels (its id), not a text to search for.
        self.assertEqual(self.widget["navigations"], [["company", {
            "tab": "evidence", "filters": {"line": "营业收入", "line_id": "statement-ingest:abc#12", "accession": "", "importance": ""}}, None]])
        self.assertTrue(any(p.startswith("/v2/data/evidence/statement-line?line=statement-ingest") for p in self.line["fetched"]))
        text = self.line["results"]["evidence"]["text"]
        self.assertIn("已锁定利润桥科目：营业收入", text)
        self.assertIn("利润表 第 12 行", text)
        self.assertIn("在 SEC EDGAR 查看申报原件", text)
        # Internal ids only inside the collapsed 「数据版本与溯源指纹」.
        self.assertNotIn("us-gaap:", text.split("数据版本与溯源指纹")[0])
        self.assertIn("数据版本与溯源指纹", text)

    def test_ask_page(self):
        r = self.out["results"]["ask"]
        self.assertIn(["聚焦标的", "ask-scope"], r["labels"])
        self.assertIn(["你的问题（必填）", "ask-question"], r["labels"])
        self.assertIn("编辑后重问", r["buttons"])
        self.assertIn("DXC 收入仍在收缩。", r["text"])
        # am 6.3: no fee at all on this page, not even per answer.
        self.assertNotIn("US$0.40", r["text"])
        self.assertNotIn("公司名会写在问题开头", r["text"])
        self.assertNotIn("AI 调用费用", r["text"])
        self.assertNotIn("不会自动带上上一条问题的上下文", r["text"])

    def test_settings_scope_cost_rules(self):
        r = self.out["results"]["settings"]
        self.assertEqual(r["h2"], ["研究课题与覆盖池管理", "投研预算与算力额度", "投资建议准入与纪律规则", "财务模型导出底稿模板",
                                   "每周投研周报", "帮助"])
        self.assertIn(["帮助", "#/settings?focus=help"], r["links"])          # the page's own table of contents
        self.assertIn("US$88.42 ｜ 每日授权预算上限 US$500.00", r["text"])
        self.assertNotIn("调用 1,345 次", r["text"])                  # am 8.2: no per-call counts
        self.assertIn("机构默认投研纪律标准", r["text"])
        self.assertIn("观察池（C）", r["text"])
        self.assertNotIn("增减公司由管理员", r["text"])                     # am 8.1: no helplessness line
        self.assertIn(["修订投研核心目标与考察范围（必填）", "goal-text"], r["labels"])

    def test_fresh_workspace(self):
        r = self.empty["results"]
        self.assertIn("当前研究项目尚未设定研究目标", r["overview"]["text"])
        self.assertIn(["设定研究目标", "#/settings?focus=goal"], r["overview"]["links"])
        self.assertIn("新研究环境", r["overview"]["text"])          # the owner's name, unchanged
        self.assertIn("还没有研究公司", r["companies"]["text"])
        self.assertIn(["研究目标（必填）", "goal-text"], r["settings"]["labels"])
        self.assertIn("设定研究目标后即可开始研究", r["tasks"]["text"])


class StaticChecks(unittest.TestCase):
    # Text colours against every solid surface they sit on.  The glass panels
    # are translucent; surface / raised / glow / cell are the composites the
    # text actually lands on (see the comment at the top of shell.css).
    AA_PAIRS = [*__import__("itertools").product(("ink", "ink-2", "muted", "link", "accent", "ok", "warn", "danger"),
                                                 ("bg", "surface", "raised", "glow", "cell", "sheet")),
                ("accent-ink", "accent"), ("accent-ink", "grad-a"), ("accent-ink", "grad-b"),
                ("accent", "accent-bg"), ("link", "accent-bg"), ("ink", "accent-bg"),
                ("ok", "ok-bg"), ("ink", "ok-bg"), ("warn", "warn-bg"), ("ink", "warn-bg"),
                ("danger", "danger-bg"), ("ink", "danger-bg"), ("ink-2", "danger-bg"), ("ink-2", "warn-bg"),
                ("danger-ink", "danger"), ("danger-btn-ink", "danger-btn"), ("badge-ink", "badge")]

    @staticmethod
    def _themes(css):
        """The dark (default :root) and light (prefers-color-scheme: light) token sets."""
        dark_block = re.search(r":root\{(.*?)\}", css, re.S).group(1)
        light_block = re.search(r"@media \(prefers-color-scheme: light\)\{\s*:root\{(.*?)\}", css, re.S).group(1)
        tokens = lambda block: dict(re.findall(r"--v2-([a-z0-9-]+):(#[0-9a-f]{6})", block))
        return {"dark": tokens(dark_block), "light": tokens(light_block)}

    @staticmethod
    def _ratio(a_hex, b_hex):
        def luminance(hex_colour):
            channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
        a, b = luminance(a_hex), luminance(b_hex)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    def test_colour_pairs_meet_aa(self):
        css = (V2 / "shell.css").read_text(encoding="utf-8")
        themes = self._themes(css)
        needed = {name for pair in self.AA_PAIRS for name in pair}
        for theme, colours in themes.items():
            self.assertFalse(needed - set(colours), theme)              # every checked token in both themes
        for theme, colours in themes.items():
            for fg, bg in self.AA_PAIRS:
                with self.subTest(theme=theme, pair=(fg, bg)):
                    self.assertGreaterEqual(self._ratio(colours[fg], colours[bg]), 4.5)

    def test_dark_navy_is_the_default_and_light_follows_the_system(self):
        css = (V2 / "shell.css").read_text(encoding="utf-8")
        themes = self._themes(css)
        self.assertEqual(themes["dark"]["bg"], "#0d1326")            # the first cockpit's navy
        self.assertEqual((themes["dark"]["grad-a"], themes["dark"]["grad-b"]), ("#3ee0c8", "#8b7cf6"))
        self.assertEqual(themes["light"]["bg"], "#f5f7fa")
        self.assertIn("color-scheme:dark", css.split("@media")[0])
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)

    def test_shell_links_to_legacy_and_admin(self):
        html = (V2 / "shell.html").read_text(encoding="utf-8")
        # G01: the old interface is not in the main navigation (研究设置 → 帮助).
        self.assertNotIn('href="/legacy"', html)
        self.assertIn('id="v2-rail-toggle"', html)                     # G04: collapsible navigation
        self.assertIn('id="v2-refresh"', html)                         # G07: 刷新研究数据
        self.assertIn('role="banner"', html)
        self.assertIn('aria-label="主导航"', html)
        js = (V2 / "shell.js").read_text(encoding="utf-8")
        self.assertIn('return (globalThis.DaltonPublicURL || (x => x))("/admin" + (q ? "?" + q : ""));', js)
        self.assertIn('pages.includes("/v2/admin")', js)              # an install that only has /v2/admin still links

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_service_pill_degraded_is_not_paused(self):
        # Live 2026-10-10: heartbeat state "degraded" (weekly brief lane in
        # error) with ticks every 5 minutes read 「后台更新已暂停」.
        now = "2026-10-10T00:35:00Z"
        now_ms = int(__import__("datetime").datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() * 1000)
        lanes = [{"key": "weekly_brief", "label": "每周简报", "state": "error"}]
        calls = [
            ["serviceStatus", {"service_state": "degraded", "last_tick_at": "2026-10-10T00:34:41Z",
                               "lanes_in_error": lanes}, now_ms, "/admin#health"],
            ["serviceStatus", {"service_state": "degraded", "last_tick_at": "2026-10-10T00:34:41Z"}, now_ms, None],
            ["serviceStatus", {"service_state": "running", "last_tick_at": "2026-10-10T00:34:41Z",
                               "lanes_in_error": []}, now_ms, "/admin#health"],
            ["serviceStatus", {"service_state": "degraded", "last_tick_at": "2026-10-10T00:10:00Z",
                               "lanes_in_error": lanes}, now_ms, "/admin#health"],
            ["serviceStatus", {"service_state": "stopping", "last_tick_at": "2026-10-10T00:34:41Z"}, now_ms, None],
            ["serviceStatus", {"service_state": "awaiting_mission", "last_tick_at": None}, now_ms, None],
        ]
        out = [r["ok"] for r in node("call", {"module": "shell.js", "calls": calls})]
        degraded, plain, running, stale, stopping, awaiting = out
        self.assertEqual(degraded["kind"], "degraded")
        self.assertEqual(degraded["text"], "研究服务正常，但有 1 项后台任务异常（点击查看）")
        self.assertEqual(degraded["href"], "/admin#health")
        self.assertIn("每周简报", degraded["title"])
        self.assertNotIn("暂停", degraded["text"] + degraded["title"])
        self.assertEqual(plain["text"], "研究服务正常，但有 1 项后台任务异常")
        self.assertIsNone(plain["href"])
        self.assertEqual(running["kind"], "running")
        self.assertEqual((stale["kind"], stopping["kind"]), ("paused", "paused"))
        self.assertIn("暂停", stale["text"])
        self.assertEqual(awaiting["kind"], "awaiting_mission")
        js = (V2 / "shell.js").read_text(encoding="utf-8")
        self.assertIn("serviceStatus(log, Date.now(), admin ? `${admin}#health` : null)", js)

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_service_pill_starting_is_not_normal(self):
        now_ms = int(__import__("datetime").datetime.fromisoformat("2026-10-10T00:35:00+00:00").timestamp() * 1000)
        calls = [
            ["serviceStatus", {"service_state": "starting", "last_tick_at": "2026-10-10T00:34:41Z",
                               "lanes_in_error": [{"key": "projection"}]}, now_ms, "/admin#health"],
            ["serviceStatus", {"service_state": "starting", "last_tick_at": "2026-10-10T00:10:00Z"}, now_ms, None],
            ["serviceStatus", {"service_state": "running", "last_tick_at": "2026-10-10T00:34:41Z"}, now_ms, None],
        ]
        starting, stale, running = [r["ok"] for r in node("call", {"module": "shell.js", "calls": calls})]
        self.assertEqual(starting["kind"], "starting")
        self.assertEqual(starting["text"], "研究服务正在启动")
        self.assertEqual(starting["title"], "正在整理已有研究记录，完成后继续任务。")
        self.assertNotIn("正常", starting["text"])
        self.assertIsNone(starting["href"])
        self.assertEqual(stale["kind"], "paused")
        self.assertEqual(running["kind"], "running")

    def test_responsive_breakpoints(self):
        css = (V2 / "shell.css").read_text(encoding="utf-8")
        for width in (1280, 1024, 768):
            self.assertIn(f"@media (max-width:{width}px)", css)
        self.assertIn("min-width:0", css)

    def test_old_cockpit_gains_one_link(self):
        # The old cockpit (now at /legacy) links back to the new one at /.
        html = (ROOT / "src/dalton_core/cockpit_control.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('href="/#/overview"'), 1)
        self.assertEqual(html.count("返回新版"), 1)

    def test_no_developer_wording_in_v2_pages(self):
        banned = ["产物", "研究车道", "--apply", "端口", "研究环境", "Conviction", "技术详情", "链尾", "执行凭证", "交付物"]
        for path in [V2 / "shell.js", V2 / "shell.html", *sorted((V2 / "pages").glob("*.js"))]:
            text = path.read_text(encoding="utf-8")
            # Comments may name what they replace; strings shown to people may not.
            strings = re.findall(r'"([^"\n]*)"|`([^`]*)`', re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S))
            shown = " ".join(a or b for a, b in strings)
            for word in banned:
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, shown)

    def test_scripts_parse(self):
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        for path in [V2 / "shell.js", V2 / "copy.js", *sorted((V2 / "pages").glob("*.js"))]:
            with self.subTest(file=path.name):
                result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
