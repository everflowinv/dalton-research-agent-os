"""Cockpit v2: the functions migrated from /legacy (owner, 2026-10-08).

Each migrated control is driven through its page under node with the fake DOM
(tests/cockpit_v2_harness.mjs): what it sends to the existing /v1/cockpit/*
writer, its inline validation (nothing is sent while a field is wrong), the
receipt that stays on the object, and the draft that survives a re-render.

* 研究设置: 调整研究方向 (steer), the investment-recommendation rules editor
  (conviction_policy), 报告模板 upload (fund_template_register), 每周简报.
* 公司页: feedback on the initial screen and on each evidence item, the scenario
  probability group (scenario_revise), the export template
  (fund_template_select), 事后复盘 / 专项研究 / 跟踪频率.
* 研究问答: feedback on an answer.
* 研究总览: 本周复盘 and 研究轨迹 drawers.
* /v2/data/feedback: the claim feedback binding read.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

from dalton_core.cockpit_plane import CockpitError
from dalton_core.cockpit_v2 import feedback_data
from dalton_core.cockpit_v2.routes import register_v2_routes

from tests.test_cockpit_v2_shell import FIXTURES, OVERVIEW, node

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "src/dalton_core/cockpit_v2"
H1, H2, H3, H4 = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
ACN = "company:sec-cik:0001467373"


def scenario(key, name, p, ref, h):
    return {"scenario_ref": f"company-scenario:{ref}", "version_ref": f"company-scenario-version:{ref}1", "version_hash": h,
            "key": key, "name": name, "probability": p, "narrative": f"{name}的叙述。",
            "triggers": [{"kind": "trigger", "display_label": "单季营收超过上季", "rule": "营业收入 > 187.2 亿美元"}],
            "invalidators": [{"kind": "invalidator", "display_label": "收入同比转负", "rule": "营业收入同比 < 0%"}]}


def overview():
    o = copy.deepcopy(OVERVIEW)
    acn = o["companies"][0]
    acn["document"] = {"version_ref": "mission-deliverable-version:acn1", "content_hash": H1,
                       "feedback": {"target_ref": "mission-deliverable-version:acn1", "target_hash": H1,
                                    "target_kind": "initial_screen", "company_ref": ACN, "enabled": True,
                                    "entries": [{"verdict": "read"}]}}
    acn["scenarios"] = {"probability_sum": "1.00", "in_force": [
        scenario("base", "订单温和", "0.50", "base", H2), scenario("bear", "AI 通缩", "0.30", "bear", H3),
        scenario("bull", "AI 加速", "0.20", "bull", H4)]}
    acn["reflections"] = [{"at": "2026-10-01T00:00:00+00:00", "trigger_label": "股价一直和我们的判断相反",
                           "what_we_expected": "订单回升。", "what_happened": "订单继续下滑。", "why": "大客户推迟项目。",
                           "missed_debates": [{"question": "AI 是否替代外包？"}],
                           "followup_tracking": [{"source_label": "公司公告", "interval_label": "每天"}],
                           "followup_research": [{"question": "大客户预算何时恢复？"}], "note": "这些跟进只是候选，没有改动任何设置。"}]
    acn["research_tasks"] = [{"question": "咨询业务利润率能否回升？", "state_label": "进行中", "budget_label": "预算 US$2.00", "gap": "缺少分部数据"}]
    acn["cadence"] = [{"source_label": "股价", "interval_label": "每天 4 次", "adjustable": False, "decided_by_brain": False,
                       "because": "one close plus one bounded intraday refresh"}]
    acn["preliminary_results"] = {"title": "公司初步业绩（8-K）", "period": "FY2026 Q4", "period_end": "2026-08-31",
                                  "accession": "0001467373-26-000099", "report_date": "2026-09-25",
                                  "rows": [{"label": "营业收入", "text": "176 亿美元"}], "note": "正式申报后替换。"}
    o["plan"] = {"decided_at": "2026-10-08T01:00:00+00:00", "assessment": "先补模型数字。",
                 "directives": [{"action": "extract_figures", "action_label": "提取财务数字", "subject": "ACN", "item": "季度财报", "reason": "模型缺净利润。"}],
                 "inquiries": [{"subject": "ACN", "question": "订单出货比？", "wants": "电话会纪要"}]}
    o["goal"]["history"] = [{"version": 27, "created_at": "2026-09-20T00:00:00+00:00", "title": "旧目标", "research_questions": ["q"], "universe": ["ACN"]},
                            {"version": 28, "created_at": "2026-09-29T00:00:00+00:00", "title": "美国 IT", "research_questions": ["q", "r"], "universe": ["ACN", "IBM"]}]
    o["ops"] = {"acceptance": {"available": True, "week": "2026-W40", "note": "上周完成 1 份质量评估；你还没留反馈"}}
    return o


POLICY = {
    "enabled": True,
    "explainer": "v2 把一条投资建议拆成三部分分别判断。",
    "effective": {"source_label": "代码默认（尚未发布任何版本）", "version": 0,
                  "fields": [{"label": "最低证据置信度", "value": "中"}]},
    "versions": [],
    "choices": [{"key": "default", "label": "代码默认"}, {"key": "v1", "label": "v1（冻结）"}],
    "compare": {"left": "v1", "right": "default", "rows": [{"label": "最低证据置信度", "left_label": "低", "right_label": "中", "changed": True}]},
    "form": {"prior_version_ref": None,
             "values": {"min_evidence_confidence": "medium", "required_scenarios": 3, "allowed_horizons": ["3_6_months", "6_12_months", "1_3_years", "3_5_years"],
                        "allowed_directions": ["long", "short"],
                        "min_expected_return_percent": {"3_6_months": "8", "6_12_months": "12", "1_3_years": "25", "3_5_years": "50"},
                        "same_direction_min_expected_return_percent": {"3_6_months": "15", "6_12_months": "20", "1_3_years": "40", "3_5_years": "80"},
                        "thesis_scope": "company_only", "max_valuation_age_days": 7, "daily_drafting_cost_cap_usd": "1",
                        "max_drafts_per_day": 3, "min_invalidators": 1},
             "options": {"min_evidence_confidence": [{"value": "low", "label": "低"}, {"value": "medium", "label": "中"}, {"value": "high", "label": "高"}],
                         "allowed_horizons": [{"value": "3_6_months", "label": "3–6 个月"}, {"value": "6_12_months", "label": "6–12 个月"},
                                              {"value": "1_3_years", "label": "1–3 年"}, {"value": "3_5_years", "label": "3–5 年"}],
                         "allowed_directions": [{"value": "long", "label": "做多"}, {"value": "short", "label": "做空"}, {"value": "avoid", "label": "回避"}],
                         "thesis_scope": [{"value": "company_only", "label": "只看公司论点"}, {"value": "company_and_industry", "label": "公司和行业论点"}]},
             "labels": {"min_evidence_confidence": "最低证据置信度", "required_scenarios": "至少要列出的情景数",
                        "allowed_horizons": "允许的投资期限", "allowed_directions": "允许的方向",
                        "min_expected_return_percent": "期望回报门槛（%）",
                        "same_direction_min_expected_return_percent": "与市场同向时的期望回报门槛（%）",
                        "thesis_scope": "哪些论点算作公司的生效论点", "max_valuation_age_days": "估值快照最长可用天数",
                        "daily_drafting_cost_cap_usd": "每日起草预算（美元）", "max_drafts_per_day": "每日最多起草次数",
                        "min_invalidators": "至少要写明的失效条件数"}},
}
TEMPLATES = {
    "enabled": True,
    "templates": [{"version_ref": "fund-xlsx-template-version:amzn-source:1", "version_hash": H1, "slug": "amzn-source", "label": "参考基金模板（AMZN 样本）",
                   "origin": "builtin", "origin_label": "系统内置", "created_at": "2026-10-07T00:00:00+00:00", "used_by": ["ACN"],
                   "is_default": True, "is_latest": True},
                  {"version_ref": "fund-xlsx-template-version:owner-tmt:1", "version_hash": H2, "slug": "owner-tmt", "label": "TMT 组模板（第 1 版）",
                   "display_name": "TMT 组模板", "origin": "owner", "origin_label": "你上传登记", "created_at": "2026-10-08T00:00:00+00:00",
                   "used_by": [], "is_default": False, "is_latest": True}],
    "companies": [{"company_ref": ACN, "label": "ACN · Accenture", "template_version_ref": "fund-xlsx-template-version:amzn-source:1",
                   "template_label": "参考基金模板（AMZN 样本）", "basis_label": "系统内置", "company_choice_ref": None}],
    "industries": [{"value": "it_services", "label": "IT 服务"}],
    "default": {"template_version_ref": "fund-xlsx-template-version:amzn-source:1", "template_label": "参考基金模板（AMZN 样本）", "chosen_by_owner": False},
    "limits": {"max_upload_mb": 16},
}
REFLECTION = {"available": True, "iso_week": "2026-W40", "written_at": "2026-10-09T02:43:19+00:00", "version": 150, "historical": True,
              "owner": {"title": "我们把时间花在哪", "prose": "这一周模型调用共 1028 次。",
                        "table": [{"pool_label": "文档深度研究", "cost_usd": 8.97, "calls": 98, "share_of_spend": 0.5924}],
                        "backlog_candidates": [{"question": "哪几个待核实问题值得优先做？", "because": "本周没有安排专项研究。"}],
                        "policy_suggestions": ["预算不是限制因素。"], "authority_note": "这份复盘只做统计。"},
              "judgement_outcomes": {"available": True, "rows": [{"label": "维持原判断正确", "to_date": 191, "this_week": 0}], "note": "这些是候选。"}}
TRAJECTORY = {"as_of": "2026-10-09T03:21:02+00:00", "older": "2026-10-09T02:51:29+00:00", "events": [
    {"at": "2026-10-09T03:20:13+00:00", "kind": "work", "status": "run", "title": "工单开始执行：检查最终研究文字是否易读", "detail": "claimed"},
    {"at": "2026-10-09T03:19:46+00:00", "kind": "call", "status": "ok", "title": "按语言建议修订最终研究文字", "cost_usd": "0.302464",
     "actor": "research_language_revision"}]}


MODEL_CHART = {
    "kind": "model", "status": "ok", "company": "Accenture ACN", "company_ref": ACN, "version": 3,
    "model_created_at": "2026-10-07T10:00:00+00:00", "unit": "亿美元", "unit_header": "金额：亿美元", "currency": "USD",
    "period": {"kind": "quarter", "label": "单季（公司财年口径）"}, "boundary_index": 1, "notes": [],
    "columns": [{"end": "2026-05-31", "kind": "history", "label": "26/05", "title": "2026年3月1日至2026年5月31日（已披露）"},
                {"end": "2026-08-31", "kind": "forecast", "label": "26/08", "title": "2026年6月1日至2026年8月31日（预测）"}],
    "sections": [{"key": "income", "label": "利润表"}],
    "rows": [{"key": "r1", "label": "营业收入", "role": "revenue", "section": "income", "unit_kind": "amount", "unit_label": "亿美元",
              "cells": [{"end": "2026-05-31", "status": "ok", "display": "17,690.00", "basis": "reported"},
                        {"end": "2026-08-31", "status": "ok", "display": "18,044.07", "basis": "estimate"}]},
             {"key": "r2", "label": "净利润", "role": "net_income", "section": "income", "unit_kind": "amount", "unit_label": "亿美元",
              "cells": [{"end": "2026-05-31", "status": "ok", "display": "2,316.00", "basis": "reported"},
                        {"end": "2026-08-31", "status": "missing", "display": None, "basis": None, "reason": "缺少税率假设"}]}],
    "completeness": {"results_total": 2, "results_computed": 1, "label": "预测结果 1/2 行已算出",
                     "results_blocked": [{"label": "净利润", "reason": "缺少税率假设"}]},
    "series": {"revenue": {"label": "营业收入", "unit": "亿美元", "points": []}, "margins": []},
}


def fixtures(**extra):
    f = copy.deepcopy(FIXTURES)
    f["/v2/data/charts/model"] = copy.deepcopy(MODEL_CHART)
    f.update({"/v1/cockpit/overview": overview(), "/v1/cockpit/conviction-policy": POLICY, "/v1/cockpit/fund-templates": TEMPLATES,
              "/v1/cockpit/history?kind=steer": {"items": [], "drafts": []}, "/v1/cockpit/reflection": REFLECTION,
              "/v1/cockpit/trajectory": TRAJECTORY,
              "/v2/data/feedback": {"enabled": True, "bindings": {"claim-version:1": {
                  "target_ref": "claim-version:1", "target_hash": H3, "target_kind": "claim", "company_ref": ACN, "enabled": True, "entries": []}}}})
    f.update(extra)
    return f


def run(render, steps=(), **kw):
    value = {"fixtures": kw.pop("fixtures", None) or fixtures(), "render": render, "steps": list(steps)}
    value.update(kw)
    return node("pages", value)


def xlsx_bytes(sheets) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheets)) + "</sheets></workbook>")
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    return buf.getvalue()


def posts(out, path):
    return [body for p, body in out["posted"] if p == path]


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class FeedbackTests(unittest.TestCase):
    def test_initial_screen_feedback_posts_the_binding_with_note_and_receipt(self):
        out = run({"c": "#/company/ACN/view"}, [
            {"page": "c", "op": "set", "id": "fb-screen-note", "value": "需要看订单数据"},
            {"page": "c", "op": "click", "text": "有参考价值"}])
        body = posts(out, "/v1/cockpit/feedback")
        self.assertEqual(body, [{"target_ref": "mission-deliverable-version:acn1", "target_hash": H1, "target_kind": "initial_screen",
                                 "verdict": "useful", "company_ref": ACN, "note": "需要看订单数据"}])
        after = out["results"]["c:after"]
        self.assertIn("已记下「有参考价值」和你的补充意见", after["text"])
        self.assertIn("反馈不是审批", after["text"])
        self.assertIn("有参考价值", after["pressed"])
        self.assertIn("已阅读", after["pressed"])                 # given before: pressed on load
        self.assertNotIn("feedback:mission-deliverable-version:acn1", after["drafts"])   # spent with the post

    def test_feedback_failure_keeps_the_note_and_says_why(self):
        out = run({"c": "#/company/ACN/view"}, [
            {"page": "c", "op": "set", "id": "fb-screen-note", "value": "理由"},
            {"page": "c", "op": "click", "text": "不同意这个结论"}],
            postReplies={"/v1/cockpit/feedback": {"error": "这条反馈没有被记下：内容已更新", "status": 409}})
        after = out["results"]["c:after"]
        self.assertIn("没有记下：这条反馈没有被记下：内容已更新", after["text"])
        self.assertEqual(after["drafts"]["feedback:mission-deliverable-version:acn1"], "理由")

    def test_note_over_the_limit_is_refused_inline(self):
        out = run({"c": "#/company/ACN/view"}, [
            {"page": "c", "op": "set", "id": "fb-screen-note", "value": "长" * 4001},
            {"page": "c", "op": "click", "text": "已阅读"}])
        self.assertEqual(posts(out, "/v1/cockpit/feedback"), [])
        self.assertIn("最多 4000 个字", out["results"]["c:after"]["text"])
        self.assertIn("fb-screen-note", out["results"]["c:after"]["invalid"])

    def test_evidence_item_feedback_uses_the_claim_binding(self):
        out = run({"c": "#/company/ACN/evidence"}, [{"page": "c", "op": "click", "text": "证据不够", "index": 1}])
        self.assertTrue(any(p.startswith("/v2/data/feedback?") and "refs=claim-version%3A1" in p for p in out["fetched"]))
        self.assertEqual(posts(out, "/v1/cockpit/feedback"), [{"target_ref": "claim-version:1", "target_hash": H3, "target_kind": "claim",
                                                               "verdict": "needs_more_evidence", "company_ref": ACN}])
        self.assertIn("对此条目留反馈", out["results"]["c"]["text"])

    def test_journal_not_enabled_shows_no_buttons(self):
        f = fixtures()
        f["/v1/cockpit/overview"]["companies"][0]["document"]["feedback"]["enabled"] = False
        f["/v2/data/feedback"] = {"enabled": False, "bindings": {}}
        out = run({"c": "#/company/ACN/view"}, fixtures=f)
        self.assertIn("还没有开通反馈记录", out["results"]["c"]["text"])
        self.assertNotIn("有参考价值", out["results"]["c"]["buttons"])

    def test_ask_answer_feedback(self):
        f = fixtures()
        f["/v1/cockpit/history?kind=ask"]["items"][1]["result"]["feedback"] = {
            "target_ref": "cockpit-ask:req1", "target_hash": H4, "target_kind": "ask_answer", "enabled": True}
        out = run({"ask": "#/ask"}, [{"page": "ask", "op": "click", "text": "需要重写"}], fixtures=f)
        self.assertEqual(posts(out, "/v1/cockpit/feedback"), [{"target_ref": "cockpit-ask:req1", "target_hash": H4,
                                                               "target_kind": "ask_answer", "verdict": "revise"}])
        self.assertIn("对这条回答留反馈", out["results"]["ask"]["text"])

    def test_pure_request_refuses_a_binding_without_a_hash(self):
        r = node("call", {"module": "pages/feedback.js", "calls": [
            ["feedbackRequest", {"target_ref": "x", "target_hash": "short", "target_kind": "claim"}, "read", ""],
            ["feedbackRequest", {"target_ref": "x", "target_hash": H1, "target_kind": "claim"}, "nope", ""]]})
        self.assertIn("缺少它的版本信息", r[0]["ok"]["error"])
        self.assertEqual(r[1]["ok"]["error"], "请选择一种反馈。")


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ScenarioEditorTests(unittest.TestCase):
    HASH = "#/company/ACN/scenarios?edit=probabilities"

    @staticmethod
    def ids(key):
        return f"scen-{key}"

    def test_entry_is_a_link_with_a_stable_url_not_a_prompt(self):
        out = run({"c": "#/company/ACN/scenarios"})
        self.assertIn(["自定义配置情景权重", self.HASH], out["results"]["c"]["links"])
        self.assertNotRegex((V2 / "pages/scenario_edit.js").read_text(encoding="utf-8"), r"\bprompt\(|\bconfirm\(")

    GROUP = "/v2/data/scenarios/revise-group"

    def test_sum_must_be_100_and_nothing_is_sent_otherwise(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "45"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}])
        after = out["results"]["c:after"]
        self.assertEqual(posts(out, self.GROUP), [])
        self.assertEqual(posts(out, "/v1/cockpit/scenario_revise"), [])
        self.assertIn("保留的 3 个情景合计 95%（需要 100%，还差 5 个百分点）", after["text"])
        self.assertIn("合计 95%，需要正好 100%", after["text"])

    def test_out_of_range_is_inline_and_the_reason_is_optional(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "99"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}])
        after = out["results"]["c:after"]
        self.assertEqual(posts(out, self.GROUP), [])
        self.assertIn(self.ids("base"), after["invalid"])
        self.assertIn("1%–95%", after["text"])
        self.assertNotIn("scenario-reason", after["invalid"])
        self.assertIn("理由（可选）", after["text"])

    def test_a_valid_group_is_one_atomic_request_with_every_scenario(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "45"},
            {"page": "c", "op": "set", "id": self.ids("bull"), "value": "25"},
            {"page": "c", "op": "set", "id": "scenario-reason", "value": "订单数据弱于预期"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}],
            postReplies={self.GROUP: {"status": "published", "message": "已整组保存（2 项修改）：基准 45%；乐观 25%", "reason": "订单数据弱于预期"}})
        sent = posts(out, self.GROUP)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(body["company_ref"], ACN)
        self.assertEqual(body["reason"], "订单数据弱于预期")
        self.assertTrue(body["request_id"])
        self.assertEqual(sorted((c["scenario_ref"], c["probability"], c["retire"], c["version_hash"]) for c in body["changes"]),
                         [("company-scenario:base", "0.45", False, H2), ("company-scenario:bear", "0.30", False, H3),
                          ("company-scenario:bull", "0.25", False, H4)])
        self.assertEqual(posts(out, "/v1/cockpit/scenario_revise"), [])        # never the per-scenario writer
        self.assertIn("已整组保存", out["results"]["c:after"]["text"])
        self.assertIn(["c", {"filters": {"edit": ""}}, {"replace": True}], out["navigations"])
        self.assertNotIn("scenario:" + ACN, out["drafts"])

    def test_no_reason_is_fine_and_a_reason_is_suggested(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "45"},
            {"page": "c", "op": "set", "id": self.ids("bull"), "value": "25"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}],
            postReplies={self.GROUP: {"status": "published", "reason": "调整情景概率：基准 50%→45%；乐观 20%→25%", "reason_auto": True}})
        sent = posts(out, self.GROUP)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["reason"], "")
        text = out["results"]["c:after"]["text"]
        self.assertIn("（自动记录）", text)

    def test_the_suggestion_and_the_live_outcome_show_while_typing(self):
        out = run({"c": self.HASH}, [{"page": "c", "op": "set", "id": self.ids("base"), "value": "45"}])
        text = out["results"]["c:after"]["text"]
        self.assertIn("建议理由：调整情景概率：基准 50%→45%", text)
        self.assertIn("暂不能计算概率加权目标价", text)                       # no targets -> no weighted return

    def test_the_slider_moves_the_number(self):
        out = run({"c": self.HASH}, [{"page": "c", "op": "set", "id": self.ids("base") + "-range", "value": "40"}])
        self.assertIn("保留的 3 个情景合计 90%", out["results"]["c:after"]["text"])

    def test_retiring_one_scenario_leaves_the_rest_at_100(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "check", "id": self.ids("bull") + "-retire"},
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "70"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}],
            postReplies={self.GROUP: {"status": "published"}})
        sent = posts(out, self.GROUP)
        self.assertEqual(sorted((c["scenario_ref"], c["retire"], c["probability"] or "") for c in sent[0]["changes"]),
                         [("company-scenario:base", False, "0.70"), ("company-scenario:bear", False, "0.30"),
                          ("company-scenario:bull", True, "")])
        self.assertIn("移出不能在这里撤销", out["results"]["c:after"]["text"])

    def test_a_refusal_saves_nothing_and_keeps_the_form_and_request_id(self):
        out = run({"c": self.HASH}, [
            {"page": "c", "op": "set", "id": self.ids("base"), "value": "45"},
            {"page": "c", "op": "set", "id": self.ids("bull"), "value": "25"},
            {"page": "c", "op": "submit", "id": "scenario-editor"}],
            postReplies={self.GROUP: {"error": "情景概率没有保存：版本已变化", "status": 409}})
        after = out["results"]["c:after"]
        self.assertIn("没有保存，整组都没有生效：情景概率没有保存：版本已变化", after["text"])
        held = json.loads(after["drafts"]["scenario:" + ACN])
        self.assertTrue(held["rid"])                                           # a retry replays, never writes twice
        self.assertEqual(held["values"]["company-scenario:base"]["value"], "45")
        self.assertIn("应用并保存情景权重配置", after["text"])                  # the form is still open

    def test_an_unsaved_edit_survives_a_re_render(self):
        held = {"values": {"company-scenario:base": {"value": "40", "retire": False}}, "reason": "草稿理由", "rid": None}
        out = run({"c": self.HASH}, drafts={"scenario:" + ACN: held})
        text = out["results"]["c"]["text"]
        self.assertIn("已恢复你上次没有保存的修改", text)
        self.assertIn("保留的 3 个情景合计 90%", text)

    def test_pure_checks(self):
        sc = [{"scenario_ref": "a", "version_ref": "va", "version_hash": H1, "probability": "0.50"},
              {"scenario_ref": "b", "version_ref": "vb", "version_hash": H2, "probability": "0.50"}]
        r = node("call", {"module": "pages/scenario_edit.js", "calls": [
            ["checkScenarioForm", [{"ref": "a", "current": 50, "value": "50"}, {"ref": "b", "current": 50, "value": "50"}], "x"],
            ["checkScenarioForm", [{"ref": "a", "current": 50, "value": "4.5"}, {"ref": "b", "current": 50, "value": "50"}], ""],
            ["checkScenarioForm", [{"ref": "a", "current": 50, "value": "95"}, {"ref": "b", "current": 50, "retire": True}], ""],
            ["groupBody", ACN, sc, {"changes": [{"ref": "a", "percent": 1}, {"ref": "b", "percent": 99}]}, "", "id"],
            ["weightedOutcome", [{"value": "60", "target": 100}, {"value": "40", "target": 50}]],
            ["weightedOutcome", [{"value": "60"}, {"value": "40"}]],
            ["suggestedReason", [{"ref": "a", "key": "base", "current": 50}, {"ref": "b", "key": "bull", "current": 50}],
             {"changes": [{"ref": "a", "percent": 60}, {"ref": "b", "retire": True}]}]]})
        self.assertIn("没有需要保存的修改", r[0]["ok"]["formError"])
        self.assertIn("a", r[1]["ok"]["errors"])
        self.assertEqual(r[1]["ok"]["reasonError"], "")                      # an empty reason is allowed
        self.assertIn("至少保留 2 个情景", r[2]["ok"]["formError"])            # C24: one kept at 95% is never valid
        # 1% goes out as a fraction: the server would read "1" as 100%.
        self.assertEqual([c["probability"] for c in r[3]["ok"]["changes"]], ["0.01", "0.99"])
        self.assertAlmostEqual(r[4]["ok"]["target"], 80.0)
        self.assertEqual(r[5]["ok"]["status"], "unvalued")
        self.assertEqual(r[6]["ok"], "调整情景概率：基准 50%→60%；移出本轮推演：乐观")


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class SettingsMigrationTests(unittest.TestCase):
    def test_steer_generates_a_draft_and_publishes_only_on_confirmation(self):
        job = {"draft_id": "cockpit-draft:steer:1", "draft_hash": H1, "cost_usd": 0.12,
               "draft": {"kind": "steer", "understood_as": "加大 IBM 软件跟踪", "summary": "新增一个问题。",
                         "add_questions": ["IBM 软件收入增速？"], "remove_questions": ["旧问题"], "changes": {"objective": False},
                         "not_possible": ["新增资料来源"]}}
        out = run({"s": "#/settings?focus=steer"}, [
            {"page": "s", "op": "set", "id": "steer-text", "value": "本周加大对 IBM 软件的跟踪"},
            {"page": "s", "op": "submit", "tag": "form", "index": 1},
            {"page": "s", "op": "click", "text": "确认研究范围变更"}],
            jobResult=job, postReplies={"/v1/cockpit/steer": {"job_id": "j1"}, "/v1/cockpit/publish": {"version": 29}})
        self.assertEqual(posts(out, "/v1/cockpit/steer")[0]["text"], "本周加大对 IBM 软件的跟踪")
        self.assertEqual(posts(out, "/v1/cockpit/publish")[0]["draft_id"], "cockpit-draft:steer:1")
        after = out["results"]["s:after"]
        # 2026-10-09: no per-action cost on analyst pages (研究设置 → 费用 has the totals).
        self.assertNotIn("本次费用", out["results"]["s:after"]["text"])
        for text in ("我理解为：加大 IBM 软件跟踪", "新增 IBM 软件收入增速？", "删除 旧问题", "新增资料来源", "已发布为第 29 版研究目标"):
            self.assertIn(text, after["text"])
        self.assertNotIn("steer:text", after["drafts"])

    def test_steer_text_is_required_and_kept(self):
        out = run({"s": "#/settings"}, [{"page": "s", "op": "submit", "tag": "form", "index": 1}])
        self.assertEqual(posts(out, "/v1/cockpit/steer"), [])
        self.assertIn("请填写「想怎样调整研究方向」", out["results"]["s:after"]["text"])
        out = run({"s": "#/settings"}, [{"page": "s", "op": "set", "id": "steer-text", "value": "半句话"}])
        self.assertEqual(out["results"]["s:after"]["drafts"]["steer:text"], "半句话")

    def test_an_open_server_draft_comes_back_without_a_new_call(self):
        f = fixtures()
        f["/v1/cockpit/history?kind=steer"] = {"drafts": [{"draft_id": "d9", "draft_hash": H2, "status": "open", "created_at": "2026-10-08T01:00:00+00:00",
                                                           "input_text": "少看媒体", "draft": {"understood_as": "少看媒体通稿", "add_questions": [],
                                                                                              "remove_questions": ["媒体热度？"], "changes": {}}}]}
        out = run({"s": "#/settings"}, fixtures=f)
        text = out["results"]["s"]["text"]
        self.assertIn("我理解为：少看媒体通稿", text)
        self.assertIn("还没有确认（尚未生效）", text)
        self.assertIn("重新整理会替换它", text)
        self.assertIn("待确认", text)                                 # the history row
        self.assertEqual(out["posted"], [])

    def test_without_a_goal_steering_waits(self):
        f = fixtures()
        f["/v1/cockpit/overview"] = {"as_of": "2026-10-08T10:18:06+00:00", "goal": None, "workspace": {"name": "新研究项目"}}
        out = run({"s": "#/settings"}, fixtures=f)
        self.assertIn("先设定研究目标；之后可以在这里用一句话调整研究方向", out["results"]["s"]["text"])

    def test_policy_editor_validates_inline_and_publishes_with_the_prior_version(self):
        out = run({"s": "#/settings?focus=rules"}, [
            {"page": "s", "op": "set", "id": "pol-required_scenarios", "value": "9"},
            {"page": "s", "op": "set", "id": "pol-same_direction_min_expected_return_percent-6_12_months", "value": "10"},
            {"page": "s", "op": "submit", "id": "policy-editor"}])
        after = out["results"]["s:after"]
        self.assertEqual(posts(out, "/v1/cockpit/conviction_policy"), [])
        self.assertIn("请填 3–3 之间的整数", after["text"])
        self.assertIn("与市场同向的门槛不能低于一般门槛（12%）", after["text"])
        self.assertIn("pol-required_scenarios", after["invalid"])
        self.assertIn("policy-reason", after["invalid"])
        held = json.loads(after["drafts"]["policy:form"])
        self.assertEqual(held["values"]["required_scenarios"], "9")         # the edit survives a refresh

        out = run({"s": "#/settings?focus=rules"}, [
            {"page": "s", "op": "set", "id": "pol-required_scenarios", "value": "3"},
            {"page": "s", "op": "check", "id": "pol-allowed_directions-avoid"},
            {"page": "s", "op": "set", "id": "policy-reason", "value": "采用三情景和最低收益风险比"},
            {"page": "s", "op": "submit", "id": "policy-editor"}],
            postReplies={"/v1/cockpit/conviction_policy": {"status": "published", "message": "发布了投资建议策略第 1 版"}})
        sent = posts(out, "/v1/cockpit/conviction_policy")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["policy"]["required_scenarios"], 3)
        self.assertEqual(sent[0]["policy"]["allowed_directions"], ["long", "short", "avoid"])
        self.assertEqual(sent[0]["policy"]["min_expected_return_percent"]["1_3_years"], "25")
        self.assertEqual(sent[0]["policy"]["allowed_horizons"], ["6_12_months"])
        self.assertEqual(sent[0]["policy"]["min_upside_downside_ratio"], "2")
        self.assertIsNone(sent[0]["prior_version_ref"])
        self.assertEqual(sent[0]["reason"], "采用三情景和最低收益风险比")
        self.assertIn("发布了投资建议策略第 1 版（", out["results"]["s:after"]["text"])   # the server's receipt, kept on the section
        self.assertNotIn("policy:form", out["results"]["s:after"]["drafts"])

    def test_policy_draft_from_an_older_version_is_dropped_with_a_notice(self):
        out = run({"s": "#/settings"}, drafts={"policy:form": {"base": "conviction-policy-version:old", "values": {"required_scenarios": "5"}}})
        self.assertIn("之前未发布的修改已作废", out["results"]["s"]["text"])
        out = run({"s": "#/settings"}, drafts={"policy:form": {"base": "", "values": {**POLICY["form"]["values"], "required_scenarios": "5"}}})
        self.assertIn("已恢复你上次没有发布的修改", out["results"]["s"]["text"])

    def test_policy_versions_and_comparison(self):
        out = run({"s": "#/settings?focus=rules&left=v1&right=default"})
        text = out["results"]["s"]["text"]
        self.assertIn("还没有发布过任何版本：当前执行机构默认投研纪律标准", text)
        # 2026-10-09 (S11 / financial 2.3): 基准版本 vs 对比版本, only with history.
        self.assertNotIn("对比：左", text)

    def test_template_upload_reads_sheet_names_and_registers(self):
        data = base64.b64encode(xlsx_bytes(["Cover", "Valuation - TMT", "Financials - TMT", "Driver - TMT"])).decode()
        out = run({"s": "#/settings?focus=templates"}, [
            {"page": "s", "op": "file", "id": "tpl-file", "name": "TMT model.xlsx", "bytes_b64": data},
            {"page": "s", "op": "set", "id": "tpl-name", "value": "TMT Group Template"},
            {"page": "s", "op": "check", "id": "tpl-ind-it_services"},
            {"page": "s", "op": "set", "id": "tpl-reason", "value": "TMT 组统一模板"},
            {"page": "s", "op": "submit", "id": "template-upload"}],
            postReplies={"/v1/cockpit/fund_template_register": {"status": "published", "message": "已登记「TMT Group Template」第 1 版。"}})
        sent = posts(out, "/v1/cockpit/fund_template_register")
        self.assertEqual(len(sent), 1)
        b = sent[0]
        self.assertEqual(b["sheets"], {"valuation": "Valuation - TMT", "financials": "Financials - TMT", "driver": "Driver - TMT"})
        self.assertEqual(b["template_slug"], "tmt-group-template")
        self.assertEqual(base64.b64decode(b["content_base64"]), base64.b64decode(data))
        self.assertEqual((b["filename"], b["industries"], b["reason"]), ("TMT model.xlsx", ["it_services"], "TMT 组统一模板"))
        after = out["results"]["s:after"]
        self.assertIn("已从文件里读到 4 张表", after["text"])
        self.assertIn("已登记「TMT Group Template」第 1 版。", after["text"])

    def test_template_upload_refuses_a_wrong_file_and_missing_fields(self):
        out = run({"s": "#/settings"}, [
            {"page": "s", "op": "file", "id": "tpl-file", "name": "model.xls", "size": 100},
            {"page": "s", "op": "submit", "id": "template-upload"}])
        after = out["results"]["s:after"]
        self.assertEqual(posts(out, "/v1/cockpit/fund_template_register"), [])
        for text in ("只支持 .xlsx 文件", "请给模板起一个名字", "请选择估值表"):
            self.assertIn(text, after["text"])
        self.assertNotIn("请写一句理由", after["text"])   # 2026-10-09: the reason is optional

    def test_template_pure_helpers(self):
        data = base64.b64encode(xlsx_bytes(["估值", "财务报表", "驱动 &amp; 假设"])).decode()
        r = node("call", {"module": "pages/templates.js", "calls": [
            ["slugFor", "TMT 组模板", "Owner TMT v2.xlsx"], ["slugFor", "模板", "模板.xlsx"],
            ["guessSheets", ["Cover", "DCF Valuation", "Financials", "Drivers"]],
            ["sheetNames", {"__bytes_b64": data}], ["validSlug", "-bad"], ["validSlug", "owner-tmt"]]})
        self.assertEqual(r[0]["ok"], "tmt")
        self.assertRegex(r[1]["ok"], r"^template-\d{8}-[0-9a-z]{3}$")   # S15: two on one day differ
        self.assertEqual(r[2]["ok"], {"valuation": "DCF Valuation", "financials": "Financials", "driver": "Drivers"})
        self.assertEqual(r[3]["ok"], ["估值", "财务报表", "驱动 & 假设"])
        self.assertEqual([r[4]["ok"], r[5]["ok"]], [False, True])

    def test_plan_goal_versions_and_weekly_brief(self):
        out = run({"s": "#/settings"})
        r = out["results"]["s"]
        for text in ("投研跟踪推进日程", "先补模型数字。", "提取财务数字", "研究目标的历史版本（2）", "每周投研周报", "财务模型导出底稿模板", "TMT 组模板"):
            self.assertIn(text, r["text"])
        # W01: the weekly brief lives in the settings page itself.
        self.assertIn("brief", r["sections"])
        self.assertIn(["每周投研周报", "#/settings?focus=brief"], r["links"])


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class CompanyAndOverviewTests(unittest.TestCase):
    def test_events_section_has_the_research_record(self):
        text = run({"c": "#/company/ACN/events"})["results"]["c"]["text"]
        for part in ("公司初步业绩（8-K）", "研判得失复盘与逻辑回溯", "我们当时以为订单回升。", "实际发生的订单继续下滑。", "可能漏掉的争论（1）",
                     "建议跟进：公司公告改为每天；大客户预算何时恢复？", "正在进行的专项课题", "咨询业务利润率能否回升？",
                     "数据更新与跟踪频率（1 项）", "固定频率"):
            self.assertIn(part, text)

    def test_model_export_offers_templates_and_sets_the_company_default(self):
        out = run({"c": "#/company/ACN/model"}, [
            {"page": "c", "op": "set", "id": "model-template", "value": "fund-xlsx-template-version:owner-tmt:1"},
            {"page": "c", "op": "set", "id": "model-template-reason", "value": "按 TMT 组模板出模型"},
            {"page": "c", "op": "click", "text": "设为这家公司的默认模板"}],
            postReplies={"/v1/cockpit/fund_template_select": {"status": "published", "message": "已生效"}})
        sent = posts(out, "/v1/cockpit/fund_template_select")
        self.assertEqual(len(sent), 1)
        self.assertEqual({k: sent[0][k] for k in ("scope_kind", "scope_ref", "template_version_ref", "template_version_hash", "reason")},
                         {"scope_kind": "company", "scope_ref": ACN, "template_version_ref": "fund-xlsx-template-version:owner-tmt:1",
                          "template_version_hash": H2, "reason": "按 TMT 组模板出模型"})
        text = out["results"]["c:after"]["text"]
        self.assertIn("下载财务预测底稿（.xlsx）", out["results"]["c"]["buttons"])
        self.assertIn("该标的以后默认按「TMT 组模板（第 1 版）」的公式与版式导出", text)

    def test_choosing_a_template_needs_no_reason(self):
        out = run({"c": "#/company/ACN/model"}, [
            {"page": "c", "op": "set", "id": "model-template", "value": "fund-xlsx-template-version:owner-tmt:1"},
            {"page": "c", "op": "click", "text": "设为这家公司的默认模板"}],
            postReplies={"/v1/cockpit/fund_template_select": {"status": "published", "message": "已生效"}})
        sent = posts(out, "/v1/cockpit/fund_template_select")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["reason"], "在公司页把导出模板改为「TMT 组模板（第 1 版）」")

    def test_model_is_a_grid_with_one_unit_and_what_is_usable(self):
        r = run({"c": "#/company/ACN/model"})["results"]["c"]
        self.assertNotIn("v2-pre", json.dumps(r))
        self.assertIn("科目（金额：亿美元）", r["text"])
        self.assertIn("三大表预测完备度：", r["text"])                       # am 5.5
        self.assertIn("营业收入预测已就绪 ｜", r["text"])
        self.assertIn("尚未完成测算的预测科目与原因（共 1 项", r["text"])
        self.assertIn("限制提示：导出底稿只含已测算出的预测", r["text"])

    def test_no_model_disables_the_export_and_says_why(self):
        f = fixtures(**{"/v2/data/charts/model": {"kind": "model", "status": "empty", "empty_reason": "该公司还没有预测模型：还没有确定模型规格。"}})
        out = run({"c": "#/company/ACN/model"}, fixtures=f)
        text = out["results"]["c"]["text"]
        self.assertIn("预测结果尚未生成，暂时无法导出 Excel 底稿。", text)
        self.assertIn("该公司还没有预测模型", text)

    def test_export_stays_when_a_chart_module_replaces_the_model_table(self):
        out = run({"c": "#/company/ACN/model"}, widget="company.model")
        self.assertIn("下载财务预测底稿（.xlsx）", out["results"]["c"]["buttons"])

    def test_evidence_audit_view_reads_every_version(self):
        out = run({"c": "#/company/ACN/evidence?versions=all"})
        self.assertTrue(any(p.startswith("/v1/cockpit/claims?") and "canonical=0" in p for p in out["fetched"]))
        self.assertIn("已废止 / 已合并的历史证据（不是当前依据）", out["results"]["c"]["text"])
        out = run({"c": "#/company/ACN/evidence"})
        self.assertFalse(any("canonical=0" in p for p in out["fetched"]))

    def test_overview_links_the_two_drawers(self):
        r = run({"o": "#/overview"})["results"]["o"]
        self.assertIn(["本周小结", "#/overview?panel=reflection"], r["links"])
        self.assertIn(["研究过程记录", "#/overview?panel=trajectory"], r["links"])
        self.assertIn("上周已完成研究的验收", r["text"])

    def test_reflection_drawer_and_a_candidate_seeds_the_steer_text(self):
        before = run({"o": "#/overview?panel=reflection"}, [{"page": "o", "op": "wait"}])
        side = before["results"]["o:after"]["side"]
        out = run({"o": "#/overview?panel=reflection"}, [{"page": "o", "op": "click", "text": "写进「调整研究方向」"}],
                  drafts={"steer:text": "已有的半句"})
        self.assertEqual(out["results"]["o:sideHeading"], "本周复盘")
        for text in ("我们把时间花在哪", "文档深度研究", "维持原判断正确", "哪几个待核实问题值得优先做？", "上周已完成研究的验收（2026-W40）"):
            self.assertIn(text, side)
        self.assertEqual(out["results"]["o:after"]["drafts"]["steer:text"], "已有的半句\n哪几个待核实问题值得优先做？")
        self.assertIn(["o", {"page": "settings", "filters": {"focus": "steer"}}, None], out["navigations"])

    def test_trajectory_drawer_filters_and_pages(self):
        out = run({"o": "#/overview?panel=trajectory"}, [{"page": "o", "op": "click", "text": "AI 调用"}])
        after = out["results"]["o:after"]
        self.assertEqual(out["results"]["o:sideHeading"], "研究轨迹")
        self.assertIn("按语言建议修订最终研究文字", after["side"])
        self.assertNotIn("工单开始执行", after["side"])                     # filtered to AI calls
        self.assertIn("AI 调用", after["pressed"])
        self.assertIn("加载更早的记录", after["side"])

    def test_blank_project_drawers_are_honest(self):
        f = fixtures()
        f["/v1/cockpit/overview"] = {"as_of": "2026-10-08T10:18:06+00:00", "goal": None, "workspace": {"name": "新研究项目"}}
        f["/v1/cockpit/reflection"] = {"available": False, "reason": "本项目尚未开始研究，暂无每周复盘"}
        f["/v1/cockpit/trajectory"] = {"events": [], "older": None}
        out = run({"r": "#/overview?panel=reflection", "t": "#/overview?panel=trajectory"},
                  [{"page": "r", "op": "wait"}, {"page": "t", "op": "wait"}], fixtures=f)
        self.assertIn("暂无每周复盘", out["results"]["r:after"]["side"])
        self.assertIn("还没有研究轨迹", out["results"]["t:after"]["side"])
        self.assertNotIn("加载更早的记录", out["results"]["t:after"]["side"])


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ApprovalBasisTests(unittest.TestCase):
    def test_an_approval_card_shows_what_the_decision_rests_on(self):
        f = fixtures()
        f["/v1/cockpit/approvals"] = {"items": [{
            "kind": "conviction_call", "ref": "call:1", "hash": H1, "title": "是否发布 ACN 的投资建议", "who": "ACN · Accenture",
            "at": "2026-10-08T01:00:00+00:00", "summary": "做多，6–12 个月。", "prior_review_note": "上一版缺少失效条件。",
            "change_note": "补了两条失效条件。",
            "conviction_v2": {"evidence": {"level_label": "中", "statement": "订单数据支持。", "rests_on": "3 条公司报表结论"},
                              "scenarios": [{"name": "基准", "held": True, "probability": "50%", "payoff": "+12%", "trigger": "营收超预期"}],
                              "expected_return": {"line": "期望回报 +9.5%", "stock": "+8%"},
                              "alignment": {"label": "与市场相反", "market_view": "市场看空", "where_market_is_wrong": "低估 AI 订单"},
                              "invalidators": ["营收同比转负"], "valuation_as_of": "2026-10-07", "policy": {"label": "投资建议规则第 1 版"}},
            "memo_review": {"producer_groups": [{"purpose_label": "起草", "status": "passed"}],
                            "key_questions": [{"question": "需求何时见底？", "answer": "明年一季度。", "falsifier": "订单继续下滑"}],
                            "checks": [{"label": "结构完整", "status": "pass"}], "formal_evidence": {"status": "verified"},
                            "human_signature_required": True},
            "zero_base_review": {"title": "从零复盘", "sections": [{"heading": "结论", "body": "维持判断。"}], "authority_note": "只读复盘。"},
            "reflection": {"trigger_label": "股价一直和我们的判断相反", "what_we_expected": "上涨", "what_happened": "下跌"},
            "actions": [{"decision": "approve", "label": "发布"}, {"decision": "reject", "label": "退回"}]}]}
        text = run({"t": "#/tasks"}, fixtures=f)["results"]["t"]["text"]
        for part in ("上一版审阅意见：上一版缺少失效条件。", "本版改了什么：补了两条失效条件。", "① 底稿证据充分度：中", "依据：3 条公司报表结论",
                     "基准（已采纳的情景）", "期望回报 +9.5%（股价期望变动 +8%）", "③ 与市场看法：与市场相反", "市场错在哪：低估 AI 订单",
                     "营收同比转负", "投资备忘录的核验依据", "第 1 组：起草 · 通过", "1. 需求何时见底？", "证伪条件：订单继续下滑",
                     "结构完整：通过", "独立模型证据：已核验", "需要人工签署", "完整复盘依据", "维持判断。", "相关复盘", "我们当时以为上涨"):
            self.assertIn(part, text)


class FeedbackDataTests(unittest.TestCase):
    @contextmanager
    def core(self, rows, entries=()):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "core.sqlite"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE claim_versions (claim_version_id TEXT PRIMARY KEY, claim_ref TEXT, version_number INTEGER, "
                        "claim_json TEXT, content_hash TEXT, prior_version_id TEXT, created_at TEXT)")
            con.execute("CREATE TABLE analyst_journal_entries (target_ref TEXT, target_kind TEXT, company_ref TEXT, verdict TEXT, "
                        "note TEXT, created_at TEXT, entry_number INTEGER)")
            for ref, digest, subject in rows:
                con.execute("INSERT INTO claim_versions VALUES (?,?,?,?,?,?,?)",
                            (ref, ref + ":c", 1, json.dumps({"id": ref, "subject_ref": subject}), digest, None, "2026-10-01"))
            for i, (ref, verdict) in enumerate(entries):
                con.execute("INSERT INTO analyst_journal_entries VALUES (?,?,?,?,?,?,?)", (ref, "claim", ACN, verdict, None, "2026-10-02", i + 1))
            con.commit(); con.close()
            from dalton_core.cockpit_plane import CockpitPlane

            class Plane:
                _core = CockpitPlane._core
                _journal = CockpitPlane._journal
                _rows = CockpitPlane.__dict__["_rows"]

                class config:
                    core_db = path
            app = type("App", (), {})()
            app.cockpit_plane = Plane()
            yield app

    def test_bindings_carry_the_content_hash_and_what_was_said(self):
        with self.core([("claim-version:1", H1, ACN), ("claim-version:2", "short", ACN), ("claim-version:3", H2, "company:other")],
                       [("claim-version:1", "read")]) as app:
            value = feedback_data.feedback_bindings(app, "x", {"company": ACN, "refs": "claim-version:1,claim-version:2,claim-version:3"}, "")
        self.assertTrue(value["enabled"])
        self.assertEqual(list(value["bindings"]), ["claim-version:1"])     # bad hash and other company left out
        b = value["bindings"]["claim-version:1"]
        self.assertEqual((b["target_hash"], b["target_kind"], b["company_ref"]), (H1, "claim", ACN))
        self.assertEqual([e["verdict"] for e in b["entries"]], ["read"])

    def test_refusals_and_empty_states(self):
        app = type("App", (), {"cockpit_plane": None})()
        self.assertFalse(feedback_data.feedback_bindings(app, "x", {"refs": "a"}, "")["enabled"])
        with self.core([]) as app:
            self.assertEqual(feedback_data.feedback_bindings(app, "x", {}, ""), {"enabled": True, "bindings": {}})
            with self.assertRaises(CockpitError):
                feedback_data.feedback_bindings(app, "x", {"refs": ",".join(f"c{i}" for i in range(51))}, "")
            with self.assertRaises(CockpitError):
                feedback_data.feedback_bindings(app, "x", {"refs": "bad ref'"}, "")
        with tempfile.TemporaryDirectory() as tmp:
            from dalton_core.cockpit_plane import CockpitPlane

            class Plane:
                _core = CockpitPlane._core

                class config:
                    core_db = Path(tmp) / "missing.sqlite"
            app = type("App", (), {"cockpit_plane": Plane()})()
            self.assertFalse(feedback_data.feedback_bindings(app, "x", {"refs": "claim-version:1"}, "")["enabled"])

    def test_registered_on_the_v2_router(self):
        router = register_v2_routes(type("App", (), {})())
        self.assertIn("/v2/data/feedback", router.get_routes())


class StaticMigrationChecks(unittest.TestCase):
    def test_only_existing_writers_are_used(self):
        """Every POST a v2 page makes is an existing /v1/cockpit writer."""
        import re
        allowed = {"/v1/cockpit/feedback", "/v1/cockpit/conviction_policy", "/v1/cockpit/steer",
                   "/v1/cockpit/coverage_draft",
                   "/v1/cockpit/publish", "/v1/cockpit/decide", "/v1/cockpit/goal", "/v1/cockpit/ask",
                   "/v1/cockpit/fund_template_register", "/v1/cockpit/fund_template_select",
                   "/v1/cockpit/stop_line",   # 待我处理 (batch ac)
                   # 2026-10-09 (batch ag): a held task's doors and a card's
                   # acknowledgement, both cockpit writers through the writer.
                   "/v1/cockpit/owner_door", "/v1/cockpit/needs_human_ack",
                   # 2026-10-09: the weekly brief inside 研究设置 (W01).
                   "/v1/cockpit/weekly_brief_plan",
                   # 2026-10-09 (batch ah): the scenario probability group, one
                   # atomic writer op (revise_company_scenario_group).
                   "/v2/data/scenarios/revise-group",
                   # Batch G: resume one answer over newly acquired material.
                   "/v1/cockpit/ask_resume", "/v1/cockpit/workspace_create"}
        found = set()
        for path in sorted((V2 / "pages").glob("*.js")):
            found |= set(re.findall(r'api\.post\("(/[^"]+)"', path.read_text(encoding="utf-8")))
        self.assertEqual(found, allowed)

    def test_admin_health_has_the_failure_ledger_detail_and_points_uploads_to_settings(self):
        js = (V2 / "admin/admin.js").read_text(encoding="utf-8")
        self.assertIn('getJson("/v1/cockpit/ops")', js)
        self.assertIn("失败台账 · 明细", js)
        self.assertIn("/#/settings?focus=templates", js)
        self.assertNotIn("上传新模板仍在原投研工作台", js)

    def test_shell_side_panel_close_hook(self):
        js = (V2 / "shell.js").read_text(encoding="utf-8")
        self.assertIn("onClose: closing = null", js)
        self.assertIn("side.close(true)", js)                           # a page change closes it silently


if __name__ == "__main__":
    unittest.main()
