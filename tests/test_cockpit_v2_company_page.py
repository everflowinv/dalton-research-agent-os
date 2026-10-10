"""Company pages after the three 2026-10-09 analyst UX reviews (batch ah).

Run under node with the shell harness (tests/cockpit_v2_harness.mjs):

* one comparison for every surface (O01/O13): the company list reads
  GET /v2/data/charts/comparison and ranks with charts/comparison.mjs sortRows,
  the same function the overview widget uses; 不可比较 says why; the sort is
  in the URL; company names link to their pages;
* the company header: name + ticker, never a CIK; price, signed changes,
  multiples (financial review 2.3 / 4.2);
* 原文与出处 (C10/C13/A03): the drawer shows the document, location, passage,
  the original link and a stored-text reader -- two steps -- and keeps
  internal ids under 「技术标识」;
* 投资观点 (O02): one current view per thesis; earlier versions are history;
* honest empty states (G12) and first-use glossary (MUSE 9/10.5).
"""

from __future__ import annotations

import copy
import json
import shutil
import unittest

from tests.test_cockpit_v2_shell import FIXTURES, node

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"


def cell(value, display, *, comparable=True, basis="2026-07-01..2027-06-30", label="2026年7月1日至2027年6月30日", **extra):
    return {"value": value, "display": display, "comparable": comparable, "basis_key": basis, "basis_label": label,
            "missing_reason": None if value is not None else "还没有回报归因", **extra}


COMPARISON = {
    "kind": "comparison", "status": "ok", "sort": "return", "dir": "desc", "data_as_of": "2026-10-08", "notes": [],
    "rule": {"id": "same-basis-same-window-v1", "label": "同口径、同期间才排名",
             "text": "每一列只把同一口径、同一期间的数放在一起排名。"},
    "columns": [{"key": "expected", "label": "模型预期总回报", "sort": "return",
                 "basis": {"basis_key": "2026-07-01..2027-06-30", "basis_label": "2026年7月1日至2027年6月30日"}},
                {"key": "risk", "label": "悲观情景概率", "sort": "risk",
                 "basis": {"basis_key": "bear_probability", "basis_label": "已采纳情景中悲观情景的主观概率"}}],
    "sort_options": [{"value": "return", "label": "按模型预期总回报"}, {"value": "risk", "label": "按悲观情景概率"}],
    "rows": [
        {"company_ref": ACN, "ticker": "ACN", "company": "Accenture ACN", "stage": "公司模型", "rank": None, "cells": {
            "expected": cell("9.24", "+9.24%", comparable=False, basis="2026-06-01..2027-05-31",
                             label="2026年6月1日至2027年5月31日",
                             incomparable_reason="期间不同：这家公司是2026年6月1日至2027年5月31日，本列对比的是2026年7月1日至2027年6月30日；不参与排名"),
            "risk": cell("30", "30%", basis="bear_probability", label="悲观")}},
        {"company_ref": IBM, "ticker": "IBM", "company": "IBM", "stage": "公司模型", "rank": 1, "cells": {
            "expected": cell("3.25", "+3.25%", badges=[{"text": "预测期已开始", "tone": "warn", "detail": "部分期间已经过去"},
                                                     {"text": "简化估算", "tone": "warn", "detail": "假设：股数持平"}]),
            "risk": cell("25", "25%", basis="bear_probability", label="悲观")}},
    ],
}

SOURCE = {
    "kind": "evidence_source", "status": "ok", "available_original": True, "button_label": "查看原文与出处",
    "claim": {"statement": "埃森哲营收 187.2 亿美元。", "statement_original": "Accenture reported Revenues of USD 18718144000.",
              "original_lang": "en", "period_label": "2026年3月1日至2026年5月31日", "company": "Accenture ACN",
              "kind_label": "定量结论", "recorded_at": "2026-08-26T19:02:30+00:00"},
    "sources": [
        {"index": 0, "relation": "supports", "relation_label": "支持", "source_kind": "document",
         "document_type_label": "卖方研报", "title": "Sunday Spotlight - A Look at Valuation Changes YTD", "house": "Wolfe Research",
         "published_at": "2026-09-06", "filing": None,
         "location": {"label": "原文第 16,375–16,582 字（第 965 行）", "start": 16374, "end": 16582, "line_number": 965},
         "passage": {"text": "Before. Exhibit 10 plots the change. After.", "highlight": [8, 35], "lang": "en", "kind": "excerpt"},
         "links": [{"kind": "reader", "label": "阅读存档全文", "href": "/v2/data/evidence/document?claim=claim-version%3A1&source=0"}],
         "technical": {"evidence_version_ref": "evidence-version:abc", "document_ref": "alphaengine-doc:320000610304599"}}],
    "technical": {"claim_ref": "claim-version:1"},
}
DOCUMENT = {"kind": "evidence_document", "status": "ok", "document_type_label": "卖方研报", "title": "Sunday Spotlight",
            "house": "Wolfe Research", "published_at": "2026-09-06", "location_label": "原文第 16,375–16,582 字",
            "text": "Header line.\nThe cited sentence sits here.\nTail.", "highlight": {"start": 13, "end": 42},
            "truncated": False, "links": []}


def overview_with(**changes):
    ov = copy.deepcopy(FIXTURES["/v1/cockpit/overview"])
    for key, value in changes.items():
        ov[key] = value
    return ov


def fixtures(**extra):
    f = copy.deepcopy(FIXTURES)
    f["/v2/data/charts/comparison"] = copy.deepcopy(COMPARISON)
    f["/v2/data/evidence/source"] = copy.deepcopy(SOURCE)
    f["/v2/data/evidence/document"] = copy.deepcopy(DOCUMENT)
    f.update(extra)
    return f


def run(render, steps=(), **kw):
    value = {"fixtures": kw.pop("fixtures", None) or fixtures(), "render": render, "steps": list(steps)}
    value.update(kw)
    return node("pages", value)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ComparisonTests(unittest.TestCase):
    def test_the_list_ranks_by_the_one_comparison_payload(self):
        out = run({"l": "#/companies"})
        r = out["results"]["l"]
        self.assertTrue(any(p.startswith("/v2/data/charts/comparison") for p in out["fetched"]))
        text = r["text"]
        # IBM is ranked; ACN's window differs: shown with its value, 不可比较 and why, after IBM.
        self.assertLess(text.index("IBM"), text.index("Accenture ACN"))
        self.assertIn("口径暂不可比（预测周期不一致）", text)
        self.assertIn("期间不同：这家公司是2026年6月1日至2027年5月31日", text)
        self.assertIn("对比期间：2026年7月1日至2027年6月30日", text)
        self.assertIn("预测期已开始", text)                                 # C07 beside the number
        self.assertIn(["Accenture ACN", "#/company/ACN"], r["links"])       # names are links (O13)
        self.assertIn("同口径、同期间才排名", text)
        self.assertFalse(r["nullText"])

    def test_the_sort_is_in_the_url_and_the_same_function_orders_asc(self):
        out = run({"l": "#/companies?dir=asc&sort=risk"})
        self.assertTrue(any("sort=risk" in p and "dir=asc" in p for p in out["fetched"]))
        text = out["results"]["l"]["text"]
        self.assertLess(text.index("IBM"), text.index("Accenture ACN"))   # 25% before 30% ascending
        self.assertIn("（从低到高）", text)
        out = run({"l": "#/companies"}, [{"page": "l", "op": "click", "attr": ["aria-pressed", "false"], "text": None, "tag": "button"}])
        navs = [n for n in out["navigations"] if n[0] == "l"]
        self.assertTrue(navs and navs[0][1]["filters"]["sort"] in {"return", "risk"})

    def test_search_narrows_rows_without_changing_the_rule(self):
        r = run({"l": "#/companies?q=ibm"})["results"]["l"]
        self.assertIn("IBM", r["text"])
        self.assertNotIn("Accenture ACN", r["text"])

    def test_first_use_glossary(self):
        r = run({"l": "#/companies"})["results"]["l"]
        self.assertIn("?", r["buttons"])
        self.assertIn("悲观情景的发生概率与潜在跌幅", r["text"])          # the popover text is in the page, hidden until pressed

    def test_unreadable_comparison_is_an_honest_state(self):
        f = fixtures()
        del f["/v2/data/charts/comparison"]
        text = run({"l": "#/companies"}, fixtures=f)["results"]["l"]["text"]
        for part in ("当前状态", "影响", "谁处理", "你可以"):
            self.assertIn(part, text)
        self.assertIn("标的横向比较数据暂时读不到", text)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class HeaderTests(unittest.TestCase):
    def test_name_ticker_price_signed_changes_and_multiples(self):
        r = run({"c": "#/company/ACN"})["results"]["c"]
        self.assertEqual(r["h1"], "Accenture ACN")
        self.assertIn("196.64 美元", r["text"])
        self.assertIn("近5个交易日 +7.2%", r["text"])
        self.assertIn("市盈率（过去四季） 15.4 倍", r["text"])
        self.assertIn("EV/EBITDA 数据未补齐（缺经调 EBITDA）", r["text"])

    def test_never_a_cik(self):
        ov = overview_with()
        msft = copy.deepcopy(ov["companies"][1])
        msft.update({"ticker": "MSFT", "name": "0000789019", "company_ref": "company:sec-cik:0000789019", "display_name": None})
        ov["companies"].append(msft)
        r = run({"c": "#/company/MSFT"}, fixtures=fixtures(**{"/v1/cockpit/overview": ov}))["results"]["c"]
        self.assertEqual(r["h1"], "MSFT")
        self.assertNotIn("0000789019", r["text"])
        ov["companies"][-1]["display_name"] = "Microsoft MSFT"
        r = run({"c": "#/company/MSFT"}, fixtures=fixtures(**{"/v1/cockpit/overview": ov}))["results"]["c"]
        self.assertEqual(r["h1"], "Microsoft MSFT")


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class EvidenceDrawerTests(unittest.TestCase):
    def test_the_drawer_opens_the_real_source_in_one_step(self):
        out = run({"c": "#/company/ACN/evidence"}, [{"page": "c", "op": "click", "text": "核验申报出处与原文段落"}])
        self.assertTrue(any(p.startswith("/v2/data/evidence/source?claim=claim-version%3A1") for p in out["fetched"]))
        side = out["results"]["c:after"]["side"]
        for part in ("埃森哲营收 187.2 亿美元。", "原文（英文）", "支持 · 卖方研报", "Sunday Spotlight", "Wolfe Research",
                     "2026年9月6日", "原文第 16,375–16,582 字（第 965 行）", "Exhibit 10 plots the change", "查看纪要 / 资料全文"):
            self.assertIn(part, side)
        # internal ids only inside the collapsed 数据版本与溯源指纹
        visible = side.split("数据版本与溯源指纹")[0]
        self.assertNotIn("claim-version:", visible)
        self.assertNotIn("alphaengine-doc:", visible)
        self.assertIn("数据版本与溯源指纹", side)

    def test_the_stored_text_is_step_two(self):
        out = run({"c": "#/company/ACN/evidence"}, [
            {"page": "c", "op": "click", "text": "核验申报出处与原文段落"},
            {"page": "c", "op": "click", "text": "查看纪要 / 资料全文"}])
        self.assertTrue(any(p.startswith("/v2/data/evidence/document?claim=") for p in out["fetched"]))
        side = out["results"]["c:after"]["side"]
        self.assertIn("The cited sentence sits here.", side)
        self.assertIn("返回出处", side)

    def test_no_original_says_so_and_names_the_record(self):
        src = {**copy.deepcopy(SOURCE), "available_original": False, "button_label": "查看结论记录", "sources": [],
               "sources_empty_reason": "这条结论没有登记出处。"}
        out = run({"c": "#/company/ACN/evidence"}, [{"page": "c", "op": "click", "text": "核验申报出处与原文段落"}],
                  fixtures=fixtures(**{"/v2/data/evidence/source": src}))
        side = out["results"]["c:after"]["side"]
        self.assertIn("结论记录", side)
        self.assertIn("未关联原始文件全文（仅保留系统摘录记录，请谨慎作为核心投资依据）", side)

    def test_an_out_of_scope_claim_is_an_honest_state(self):
        src = {"kind": "evidence_source", "status": "empty",
               "empty": {"state": "这条结论不在当前研究项目中。", "impact": "无法打开出处。", "owner": "—", "action": "回到列表。"}}
        out = run({"c": "#/company/ACN/evidence"}, [{"page": "c", "op": "click", "text": "核验申报出处与原文段落"}],
                  fixtures=fixtures(**{"/v2/data/evidence/source": src}))
        side = out["results"]["c:after"]["side"]
        self.assertIn("这条结论不在当前研究项目中。", side)
        self.assertIn("谁处理", side)

    def test_the_button_names_its_object(self):
        out = run({"c": "#/company/ACN/evidence"})
        self.assertIn("核验申报出处与原文段落", out["results"]["c"]["buttons"])
        # the accessible name carries the conclusion (G09); checked in the source
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "src/dalton_core/cockpit_v2/pages/company_evidence.js").read_text(encoding="utf-8")
        self.assertIn("的申报出处与原文段落`", js)

    def test_history_versions_are_a_separate_list(self):
        f = fixtures()
        f["/v1/cockpit/claims"]["items"].append({**f["/v1/cockpit/claims"]["items"][0], "ref": "claim-version:old", "canonical": False,
                                                  "display_statement": "旧版：埃森哲营收 187 亿美元。"})
        out = run({"c": "#/company/ACN/evidence?versions=all"}, fixtures=f)
        text = out["results"]["c"]["text"]
        current, history = text.split("已废止 / 已合并的历史证据（不是当前依据）")
        self.assertNotIn("旧版：埃森哲营收", current)
        self.assertIn("旧版：埃森哲营收", history)
        self.assertIn("已废止 / 已合并的历史证据", history)


#: The four thesis rows of /v1/cockpit/overview as served live on 2026-10-09
#: (two theses x two versions), summaries in Chinese.
LIVE_THESES = [
    {"ref": "thesis:us-it-services:demand-bottoming", "confidence": "low", "summary": "美国 IT 服务需求正接近底部，未来两到四个季度恢复低个位数增长。",
     "version_ref": "thesis-version:b35b", "version": 1, "at": "2026-08-27T12:28:24+00:00", "current": False,
     "superseded_at": "2026-09-14T13:46:04+00:00", "change_label": "首次建立", "company_ref": None},
    {"ref": "thesis:acn:ai-reinvention-growth", "confidence": "medium", "summary": "AI 与重塑业务可抵消咨询疲软，埃森哲可维持中个位数增长。",
     "version_ref": "thesis-version:0048", "version": 1, "at": "2026-08-27T12:28:30+00:00", "current": False,
     "superseded_at": "2026-09-14T13:46:06+00:00", "change_label": "首次建立", "company_ref": ACN},
    {"ref": "thesis:us-it-services:demand-bottoming", "confidence": "low", "summary": "美国 IT 服务需求底部仍未确认，行业恢复取决于需求企稳。",
     "version_ref": "thesis-version:5db8", "version": 2, "at": "2026-09-14T13:46:04+00:00", "current": True,
     "superseded_at": None, "change_label": "论点受损", "company_ref": None},
    {"ref": "thesis:acn:ai-reinvention-growth", "confidence": "medium", "summary": "埃森哲的 AI 路径可信但仍是低置信假设，取决于净增量收入与重组成效。",
     "version_ref": "thesis-version:1089", "version": 2, "at": "2026-09-14T13:46:06+00:00", "current": True,
     "superseded_at": None, "change_label": "论点受损", "company_ref": ACN},
]


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ViewTests(unittest.TestCase):
    def test_one_current_view_and_dated_history(self):
        theses = [
            {"ref": "thesis:acn:ai", "confidence": "medium", "summary": "旧：AI 能稳住中个位数增长。", "version": 1,
             "at": "2026-08-27T10:00:00+00:00", "current": False, "superseded_at": "2026-09-14T13:46:06+00:00",
             "change_label": "首次建立", "company_ref": ACN},
            {"ref": "thesis:acn:ai", "confidence": "medium", "summary": "新：AI 路径可信但证据仍弱。", "version": 2,
             "at": "2026-09-14T13:46:06+00:00", "current": True, "change_label": "论点受损", "company_ref": ACN},
            {"ref": "thesis:us-it:bottom", "confidence": "low", "summary": "行业需求见底。", "version": 1,
             "at": "2026-08-27T10:00:00+00:00", "current": True, "company_ref": None}]
        r = run({"c": "#/company/ACN"}, fixtures=fixtures(**{"/v1/cockpit/overview": overview_with(theses=theses)}))["results"]["c"]
        text = r["text"]
        current, rest = text.split("历史观点演变与分歧记录", 1)
        self.assertIn("新：AI 路径可信但证据仍弱。", current)
        # P0 2026-10-09: the current industry thesis is its own section, not in the history fold
        self.assertIn("本研究行业的共同前提（生效中）", current)
        self.assertIn("行业需求见底。", current)
        self.assertNotIn("行业需求见底。", rest)
        self.assertNotIn("本研究行业的共同前提", rest)
        self.assertIn("生效基准日：2026年9月14日", current)
        self.assertIn("最近变化：论点受损", current)
        self.assertNotIn("旧：AI 能稳住中个位数增长。", current)
        self.assertIn("旧：AI 能稳住中个位数增长。", rest)
        self.assertIn("历史归档", rest)
        self.assertIn("2026年9月14日被修订取代", rest)
        # a report's opening paragraph is a dated summary, not the view (C02, am 5.1)
        self.assertIn("历史底稿参考：立项初筛底稿", text)

    def test_live_shaped_versions_place_industry_current_outside_the_history_fold(self):
        theses = LIVE_THESES
        html = run({"c": "#/company/ACN"}, fixtures=fixtures(**{"/v1/cockpit/overview": overview_with(theses=theses)}))["results"]["c"]
        text = html["text"]
        before, fold = text.split("历史观点演变与分歧记录", 1)
        self.assertIn("本研究行业的共同前提（生效中）", before)
        self.assertIn("底部仍未确认", before)
        self.assertIn("AI 路径可信但仍是低置信假设", before)
        self.assertNotIn("需求正接近底部", before)
        self.assertNotIn("可维持中个位数增长", before)
        # the fold holds only the two superseded versions
        self.assertIn("（2 条，已归档）", fold)
        self.assertIn("需求正接近底部", fold)
        self.assertIn("可维持中个位数增长", fold)
        self.assertNotIn("底部仍未确认", fold)

    def test_no_view_is_said_plainly(self):
        ov = overview_with()
        ov["companies"][0]["scenarios"] = {"in_force": []}
        text = run({"c": "#/company/ACN"}, fixtures=fixtures(**{"/v1/cockpit/overview": ov}))["results"]["c"]["text"]
        self.assertIn("尚未建立正式投资观点", text)
        self.assertNotIn("当前观点不变", text.split("当前生效论点")[1].split("驱动本标的")[0])   # O07: no "维持" without a view
        self.assertNotIn("以基准情景为准", text)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ScenarioEditorPureTests(unittest.TestCase):
    def test_group_body_lists_every_scenario_and_keeps_unchanged_ones(self):
        r = node("call", {"module": "pages/scenario_edit.js", "calls": [
            ["groupBody", ACN, [{"scenario_ref": "a", "version_ref": "va", "version_hash": "h1", "probability": "0.50"},
                                {"scenario_ref": "b", "version_ref": "vb", "version_hash": "h2", "probability": "0.30"},
                                {"scenario_ref": "c", "version_ref": "vc", "version_hash": "h3", "probability": "0.20"}],
             {"changes": [{"ref": "a", "percent": 60}, {"ref": "c", "retire": True}]}, "", "rid-1"]]})
        body = r[0]["ok"]
        self.assertEqual(body["request_id"], "rid-1")
        self.assertEqual([(c["scenario_ref"], c["probability"], c["retire"]) for c in body["changes"]],
                         [("a", "0.60", False), ("b", "0.30", False), ("c", None, True)])


class DisplayFieldTests(unittest.TestCase):
    """The overview's new display fields (cockpit_plane, after localization)."""

    def test_thesis_versions_carry_current_and_dates(self):
        from dalton_core.cockpit_plane import _thesis_views

        theses = [
            {"thesis_ref": "thesis:acn:ai", "id": "thesis-version:1", "version": 1, "confidence": "medium",
             "summary": "v1", "change_reason": "Initial human-reviewed thesis", "created_at": "2026-08-27T00:00:00+00:00"},
            {"thesis_ref": "thesis:us-it:bottom", "id": "thesis-version:2", "version": 1, "confidence": "low",
             "summary": "industry", "created_at": "2026-08-27T00:00:00+00:00"},
            {"thesis_ref": "thesis:acn:ai", "id": "thesis-version:3", "version": 2, "confidence": "medium",
             "summary": "v2", "change_reason": "THESIS_WEAKENED｜人裁决的修订", "created_at": "2026-09-14T00:00:00+00:00"}]
        members = {ACN: {"ticker": "ACN", "company_ref": ACN}}
        views = _thesis_views(theses, members)
        # unchanged fields keep their order and values (localization keys are the summaries)
        self.assertEqual([(v["ref"], v["summary"], v["confidence"]) for v in views],
                         [("thesis:acn:ai", "v1", "medium"), ("thesis:us-it:bottom", "industry", "low"), ("thesis:acn:ai", "v2", "medium")])
        self.assertEqual([v["current"] for v in views], [False, True, True])
        self.assertEqual(views[0]["superseded_at"], "2026-09-14T00:00:00+00:00")
        self.assertEqual(views[2]["change_label"], "论点受损")
        self.assertEqual(views[0]["change_label"], "首次建立")
        self.assertEqual([v["company_ref"] for v in views], [ACN, None, ACN])

    def test_theses_current_keeps_one_current_version_per_thesis(self):
        from dalton_core.cockpit_plane import _current_thesis_views, _thesis_views

        theses = [
            {"thesis_ref": "thesis:us-it:bottom", "id": "tv:1", "version": 1, "confidence": "low",
             "summary": "approaching a bottom", "change_reason": "Initial", "created_at": "2026-08-27T12:28:24+00:00"},
            {"thesis_ref": "thesis:acn:ai", "id": "tv:2", "version": 1, "confidence": "medium",
             "summary": "acn v1", "change_reason": "Initial", "created_at": "2026-08-27T12:28:30+00:00"},
            {"thesis_ref": "thesis:us-it:bottom", "id": "tv:3", "version": 2, "confidence": "low",
             "summary": "bottom unconfirmed", "change_reason": "THESIS_WEAKENED｜x", "created_at": "2026-09-14T13:46:04+00:00"},
            {"thesis_ref": "thesis:acn:ai", "id": "tv:4", "version": 2, "confidence": "medium",
             "summary": "acn v2", "change_reason": "THESIS_WEAKENED｜x", "created_at": "2026-09-14T13:46:06+00:00"}]
        views = _thesis_views(theses, {ACN: {"ticker": "ACN", "company_ref": ACN}})
        before = json.loads(json.dumps(views))
        current = _current_thesis_views(views)
        self.assertEqual([(v["ref"], v["version"], v["summary"]) for v in current],
                         [("thesis:us-it:bottom", 2, "bottom unconfirmed"), ("thesis:acn:ai", 2, "acn v2")])
        self.assertTrue(all(v["current"] is True for v in current))
        self.assertEqual([v["company_ref"] for v in current], [None, ACN])
        self.assertEqual(views, before)          # ``theses`` itself is unchanged
        self.assertEqual(len(views), 4)
        # a duplicate claim of current never yields two versions of one thesis
        dup = [dict(views[2]), dict(views[2], version=3, summary="later")]
        self.assertEqual([v["summary"] for v in _current_thesis_views(dup)], ["later"])
        self.assertEqual(_current_thesis_views([dict(views[0])]), [])

    def test_company_display_name_is_never_a_cik(self):
        from dalton_core.financial_display import company_display_name, model_line_display

        self.assertEqual(company_display_name({"ticker": "MSFT", "name": "0000789019"}, "company:sec-cik:0000789019"), "Microsoft MSFT")
        self.assertEqual(company_display_name({"ticker": "ACN", "name": "Accenture"}), "Accenture ACN")
        self.assertEqual(company_display_name({"ticker": "IBM"}), "IBM")
        self.assertEqual(company_display_name(None, "company:sec-cik:0000789019"), "未登记代码的公司")
        self.assertEqual(model_line_display("本行为收入（Revenues）科目。"), "营业收入")
        self.assertEqual(model_line_display("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"), "营业收入")
        self.assertEqual(model_line_display("us-gaap:SomethingNobodyMapped"), "Something Nobody Mapped")
        self.assertNotIn("us-gaap:", model_line_display("Revenues (us-gaap:Revenues)"))


if __name__ == "__main__":
    unittest.main()
