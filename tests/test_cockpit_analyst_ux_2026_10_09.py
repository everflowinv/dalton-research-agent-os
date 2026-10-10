"""The three analyst UX reviews of 2026-10-09, analyst-interaction part.

Reviews: docs/reports/cockpit-independent-analyst-ux-review-2026-10-09.md
(G/O/A/T/S/W IDs), docs/COCKPIT_ANALYST_UX_REVIEW_MUSE_2026-10-09.md (P0 #1-#4)
and docs/COCKPIT_FINANCIAL_ANALYST_UX_REVIEW_2026-10-09.md (痛点 1-5, 5.2/5.3).

* 待我处理: decisions confirm first, with the consequence (MUSE P0 #2); light
  actions are one click with an auto-generated reason (痛点 1, T05); parked
  UI-text batches are one grouped card (T01); cards settle done / queued /
  rejected and the count follows (T06/T07).
* 研究问答: elapsed time and 「停止等待」 (MUSE P0 #1); a real follow-up that
  names the answer it follows; no fee warning (痛点 2); failed questions asked
  again under the same request id.
* Server: the ask follow-up contract (bounded previous turn, prompt block,
  subject hint), needs_human grouping and labels, final_text_state's
  show_draft flag, the weekly brief's next send time and destinations.
* Shell: plain-language status, refresh, side panel, density classes, the
  shared state component, banned wording (canaries).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.test_cockpit_v2_shell import FIXTURES, node

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "src/dalton_core/cockpit_v2"
NODE = shutil.which("node") is not None


def fixtures(**overrides):
    out = json.loads(json.dumps(FIXTURES, ensure_ascii=False))
    out.update(overrides)
    return out


def posts(out, path):
    return [body for p, body in out["posted"] if p == path]


ACK = {"card_ref": "fetch-cap:x", "kind": "fetch_failure_capped", "condition_hash": "0" * 64,
       "choices": [{"choice": "noted", "label": "已知悉"}, {"choice": "snooze", "label": "暂不提醒（选天数）", "days": [1, 7, 30]},
                   {"choice": "abandon_document", "label": "放弃这份资料", "danger": True}]}
CARDS = {"as_of": "2026-10-09T10:00:00+00:00", "items": [
    {"kind": "fetch_failure_capped", "ref": "fetch-cap:x", "at": "2026-10-09T09:00:00+00:00", "urgency": 4,
     "urgency_label": "积压的待办", "title": "ACN · Accenture 的一份资料连续 32 次没取回来", "why_blocked": "", "action": "",
     "consequence": "不影响其他研究。", "actionable": True, "detail": {}, "entry": {"mode": "acknowledge", **ACK}},
    {"kind": "publication_parked", "ref": "publication-parked:ui-text-batches", "at": "2026-10-09T09:00:00+00:00",
     "urgency": 4, "urgency_label": "积压的待办", "title": "部分研究结论的中文译稿待重新生成（2 批）",
     "why_blocked": "", "action": "", "consequence": "", "actionable": True,
     "detail": {"group": "ui_text_batches", "count": 2},
     "entry": {"mode": "action_group", "action": "language_retry", "label": "全部重新质检",
               "payloads": [{"batch_ref": "ui-text-batch:" + "1" * 64}, {"batch_ref": "ui-text-batch:" + "2" * 64}],
               "members": [
                   {"ref": "publication-parked:ui-text-batch:" + "1" * 64, "attempts": 6,
                    "retry": {"action": "language_retry", "payload": {"batch_ref": "ui-text-batch:" + "1" * 64}},
                    "acknowledge": {"card_ref": "publication-parked:ui-text-batch:" + "1" * 64, "kind": "publication_parked",
                                    "condition_hash": "1" * 64, "choices": [{"choice": "noted", "label": "已知悉"}]}},
                   {"ref": "publication-parked:ui-text-batch:" + "2" * 64, "attempts": 6,
                    "retry": {"action": "language_retry", "payload": {"batch_ref": "ui-text-batch:" + "2" * 64}},
                    "acknowledge": {"card_ref": "publication-parked:ui-text-batch:" + "2" * 64, "kind": "publication_parked",
                                    "condition_hash": "2" * 64, "choices": [{"choice": "noted", "label": "已知悉"}]}}]}},
    {"kind": "controlled_recovery", "ref": "lane:x:a", "at": "2026-10-09T09:00:00+00:00", "urgency": 2,
     "urgency_label": "有一整个环节停着", "title": "等你决定：EPAM · 收入怎么拆分？", "why_blocked": "", "action": "",
     "consequence": "", "actionable": True, "detail": {"admission_ref": "mission-document-research-admission:09b4", "lane": "x"},
     "entry": {"mode": "admin", "href": "/admin#run", "label": "在「管理员设置 → 运行控制」处理",
               "actions": [{"door": "reentry", "label": "放行一次重新进入（不花钱）", "admission_ref": "mission-document-research-admission:09b4",
                            "confirm": "恢复研究任务：系统下一轮继续这项研究。确认？"}]}},
]}


@unittest.skipUnless(NODE, "node is not installed")
class TasksPageTests(unittest.TestCase):
    def run_tasks(self, steps, **extra):
        return node("pages", {"fixtures": fixtures(**{"/v1/cockpit/needs-human": CARDS}), "render": {"t": "#/tasks"},
                              "steps": [{"page": "t", **s} for s in steps], **extra})

    def test_a_decision_asks_for_confirmation_with_its_consequence(self):
        out = self.run_tasks([{"op": "click", "text": "通过"}])
        after = out["results"]["t:after"]
        self.assertEqual(posts(out, "/v1/cockpit/decide"), [])                # nothing sent yet
        self.assertIn("确认「通过」？", after["dialog"])
        self.assertIn("决定对象：确认 ACN 的初步筛选", after["dialog"])
        out = self.run_tasks([{"op": "click", "text": "通过"}, {"op": "click", "text": "取消", "dialog": True}])
        self.assertEqual(posts(out, "/v1/cockpit/decide"), [])                # cancelled
        self.assertIsNone(out["results"]["t:after"]["dialog"])
        out = self.run_tasks([{"op": "click", "text": "通过"}, {"op": "click", "text": "确认通过", "dialog": True}],
                             postReplies={"/v1/cockpit/decide": {"status": "decided"}})
        sent = posts(out, "/v1/cockpit/decide")
        self.assertEqual([b["decision"] for b in sent], ["approve"])
        after = out["results"]["t:after"]
        self.assertIn("done", after["states"])                                # T06: the card says it is done
        self.assertIn("已处理：通过", after["text"])
        self.assertIn("3 项需要处理", after["text"])                           # 4 -> 3: the count follows

    def test_light_actions_are_one_click_with_an_auto_reason(self):
        out = self.run_tasks([{"op": "click", "text": "已阅知悉"}],
                             postReplies={"/v1/cockpit/needs_human_ack": {"status": "recorded", "message": "已记下。"}})
        sent = posts(out, "/v1/cockpit/needs_human_ack")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["choice"], "noted")
        self.assertTrue(sent[0]["reason"].startswith("分析师在待办决策页标记已阅知悉"), sent[0]["reason"])
        self.assertIn("done", out["results"]["t:after"]["states"])
        out = self.run_tasks([{"op": "click", "text": "静音该提示"}],
                             postReplies={"/v1/cockpit/needs_human_ack": {"status": "recorded"}})
        sent = posts(out, "/v1/cockpit/needs_human_ack")
        self.assertEqual((sent[0]["choice"], sent[0]["snooze_days"]), ("snooze", 7))
        self.assertIn("静音该提示 7 天", sent[0]["reason"])
        # A note the analyst wrote replaces the generated reason.
        out = self.run_tasks([{"op": "set", "tag": "textarea", "index": 1, "value": "下周再看"}, {"op": "click", "text": "已阅知悉"}],
                             postReplies={"/v1/cockpit/needs_human_ack": {"status": "recorded"}})
        self.assertEqual(posts(out, "/v1/cockpit/needs_human_ack")[0]["reason"], "下周再看")

    def test_a_refused_write_is_not_shown_as_done(self):
        out = self.run_tasks([{"op": "click", "text": "已阅知悉"}],
                             postReplies={"/v1/cockpit/needs_human_ack": {"error": "这张卡片不能这样处理", "status": 400}})
        after = out["results"]["t:after"]
        self.assertIn("rejected", after["states"])
        self.assertIn("没有完成", after["text"])
        self.assertIn("4 项需要处理", after["text"])                           # the count did not move

    def test_abandon_confirms_and_the_doors_keep_a_required_reason(self):
        out = self.run_tasks([{"op": "click", "text": "忽略该缺失资料（采用现有数据推导）"}])
        self.assertIn("忽略该缺失资料？", out["results"]["t:after"]["dialog"])
        self.assertEqual(posts(out, "/v1/cockpit/needs_human_ack"), [])
        # The recovery door: relabelled, confirmed, and refused without a reason.
        out = self.run_tasks([{"op": "click", "text": "恢复专项课题研究"},
                              {"op": "click", "text": "确认恢复专项课题研究", "dialog": True}])
        self.assertEqual(posts(out, "/v1/cockpit/owner_door"), [])
        self.assertIn("请填写「理由（必填，会和操作一起存档）」", out["results"]["t:after"]["dialog"])
        out = self.run_tasks([{"op": "click", "text": "恢复专项课题研究"},
                              {"op": "set", "tag": "textarea", "dialog": True, "value": "路由已恢复"},
                              {"op": "click", "text": "确认恢复专项课题研究", "dialog": True}],
                             postReplies={"/v1/cockpit/owner_door": {"door": "reentry", "result": {"status": "authorized"}, "message": "已恢复研究任务。"}})
        sent = posts(out, "/v1/cockpit/owner_door")
        self.assertEqual((sent[0]["door"], sent[0]["reason"]), ("reentry", "路由已恢复"))

    def test_parked_batches_are_one_card_with_retry_all(self):
        out = self.run_tasks([{"op": "click", "text": "全部重新质检"}],
                             postReplies={"/v1/cockpit/language_retry": {"status": "requested"}})
        r = out["results"]["t"]
        self.assertIn("全部重新质检", r["buttons"])
        self.assertIn("全部已阅知悉", r["buttons"])
        self.assertIn("逐批查看（2 批）", r["text"])
        self.assertIn("资料补充与报告校准（2）", r["h2"])                      # T02: not a research decision
        sent = posts(out, "/v1/cockpit/language_retry")
        self.assertEqual([b["batch_ref"] for b in sent], ["ui-text-batch:" + "1" * 64, "ui-text-batch:" + "2" * 64])
        self.assertTrue(all(b["reason"].startswith("分析师在待办决策页要求重新生成中文定稿") for b in sent))
        self.assertIn("queued", out["results"]["t:after"]["states"])
        self.assertIn("已受理 2 批", out["results"]["t:after"]["text"])
        out = self.run_tasks([{"op": "click", "text": "全部已阅知悉"}],
                             postReplies={"/v1/cockpit/needs_human_ack": {"status": "recorded"}})
        self.assertEqual([b["card_ref"] for b in posts(out, "/v1/cockpit/needs_human_ack")],
                         ["publication-parked:ui-text-batch:" + "1" * 64, "publication-parked:ui-text-batch:" + "2" * 64])

    def test_the_admin_handoff_is_a_business_summary(self):
        out = node("pages", {"fixtures": FIXTURES, "render": {"t": "#/tasks"}})
        text = out["results"]["t"]["text"]
        self.assertIn("技术交办单内容", text)
        self.assertIn("事项：连接器原始资料存储已用到上限的 97%", text)
        self.assertIn("技术信息（供管理员）", text)                           # the ref only under it
        for word in ("放行一次", "再买一次", "改绑票据", "挡住了整条研究链", "有一整个环节停着", "付费重试", "不产生费用"):
            self.assertNotIn(word, text)

    def test_empty_list_uses_the_state_component(self):
        out = node("pages", {"fixtures": fixtures(**{"/v1/cockpit/needs-human": {"items": []}, "/v1/cockpit/approvals": {"items": []}}),
                             "render": {"t": "#/tasks"}})
        text = out["results"]["t"]["text"]
        for label in ("当前状态", "影响", "谁处理", "我能做什么"):
            self.assertIn(label, text)
        self.assertIn("打开研究总览", [link[0] for link in out["results"]["t"]["links"]])


ASK_HISTORY = {"items": [
    {"job_id": "cockpit-job:aaa", "status": "done", "created_at": "2026-10-09T03:00:00+00:00",
     "request": {"question": "ACN 最近一个季度的需求怎么样？", "request_id": "r-done"},
     "result": {"display_answer": "需求偏弱。订单回升慢。", "confidence": "medium", "cost_usd": 0.02, "citations": []}},
    {"job_id": "cockpit-job:bbb", "status": "failed", "created_at": "2026-10-09T02:00:00+00:00",
     "request": {"question": "DXC 利润怎么样？", "request_id": "r-failed"}, "display_message": "回答已生成，但仍在等待语言审查，尚未发布"}]}


@unittest.skipUnless(NODE, "node is not installed")
class AskPageTests(unittest.TestCase):
    def run_ask(self, steps, **extra):
        return node("pages", {"fixtures": fixtures(**{"/v1/cockpit/history?kind=ask": ASK_HISTORY}), "render": {"a": "#/ask"},
                              "steps": [{"page": "a", **s} for s in steps], **extra})

    def test_progress_shows_the_elapsed_time_and_can_stop_waiting(self):
        out = self.run_ask([{"op": "set", "id": "ask-question", "value": "IBM 的软件收入？"}, {"op": "submit", "tag": "form"}],
                           postReplies={"/v1/cockpit/ask": {"job_id": "cockpit-job:new"}}, jobHang=True)
        after = out["results"]["a:after"]
        self.assertIn("已耗时", after["text"])
        self.assertIn("后台继续分析（离开当前等待）", after["text"])
        self.assertNotIn("停止等待", after["text"])                           # am 6.2: reads as cancel
        self.assertEqual(out["jobs"], ["cockpit-job:new"])
        out = self.run_ask([{"op": "set", "id": "ask-question", "value": "IBM 的软件收入？"}, {"op": "submit", "tag": "form"},
                            {"op": "click", "text": "后台继续分析（离开当前等待）"}],
                           postReplies={"/v1/cockpit/ask": {"job_id": "cockpit-job:new"}}, jobHang=True)
        after = out["results"]["a:after"]
        self.assertIn("已转入后台继续分析", after["text"])
        self.assertIn("回到前台等待", after["text"])
        self.assertNotIn("已耗时", after["text"])
        self.assertEqual(json.loads(after["drafts"]["ask:pending"])["job_id"], "cockpit-job:new")   # survives a reload

    def test_a_follow_up_names_the_answer_it_follows(self):
        out = self.run_ask([{"op": "click", "text": "继续深入追问 →"}, {"op": "set", "id": "ask-question", "value": "那利润率呢？"},
                            {"op": "submit", "tag": "form"}],
                           postReplies={"/v1/cockpit/ask": {"job_id": "cockpit-job:new"}},
                           jobResult={"display_answer": "利润率小幅下降。", "follow_up": {"of": "cockpit-job:aaa", "question": "ACN 最近一个季度的需求怎么样？"}})
        sent = posts(out, "/v1/cockpit/ask")
        self.assertEqual(sent[0]["follow_up_of"], "cockpit-job:aaa")
        self.assertEqual(sent[0]["question"], "那利润率呢？")                   # not pasted text
        self.assertIn("已关联上下文：紧接上一回答「ACN 最近一个季度的需求怎么样？」继续追问", out["results"]["a:after"]["text"])
        self.assertIn("利润率小幅下降。", out["results"]["a:after"]["text"])

    def test_no_fee_warning_and_a_failed_answer_is_fetched_again_without_a_new_request(self):
        out = self.run_ask([{"op": "click", "text": "原问题直接重试"}], postReplies={"/v1/cockpit/ask": {"job_id": "cockpit-job:again"}},
                           jobResult={"display_answer": "DXC 利润靠降本。"})
        r = out["results"]["a"]
        self.assertNotIn("AI 调用费用", r["text"])
        self.assertNotIn("产生一次", r["text"])
        self.assertNotIn("US$0.02", r["text"])                                  # am 6.3: no per-answer cost
        sent = posts(out, "/v1/cockpit/ask")
        self.assertEqual((sent[0]["question"], sent[0]["request_id"]), ("DXC 利润怎么样？", "r-failed"))
        self.assertIn("DXC 利润靠降本。", out["results"]["a:after"]["text"])

    def test_pure_helpers(self):
        r = node("call", {"module": "pages/ask.js", "calls": [
            ["elapsedText", 45], ["elapsedText", 65], ["askRequest", "  问题  ", {"requestId": "x", "followUp": {"job_id": "j"}}],
            ["askRequest", "问题", {"requestId": "x"}]]})
        self.assertEqual([r[0]["ok"], r[1]["ok"]], ["45 秒", "1 分 05 秒"])
        self.assertEqual(r[2]["ok"], {"question": "问题", "request_id": "x", "follow_up_of": "j"})
        self.assertNotIn("follow_up_of", r[3]["ok"])


@unittest.skipUnless(NODE, "node is not installed")
class ShellAndCopyTests(unittest.TestCase):
    def js(self, source):
        result = subprocess.run(["node", "--input-type=module", "-e", source], capture_output=True, text=True, timeout=60, cwd=V2)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_status_pills_say_the_impact_in_plain_words(self):
        out = self.js('import {UI} from "./copy.js"; console.log(JSON.stringify({on: UI.serviceOnline, off: UI.serviceOffline,'
                      ' b0: UI.blocked(0), b3: UI.blocked(3), up: UI.updated("2026-10-09 10:00"), badge: UI.badge(2)}));')
        self.assertEqual(out["on"], "研究服务正常")                              # am: kept over 「投研引擎在线」
        self.assertEqual(out["off"], "后台更新已暂停（已有研究可正常查看，新资料暂停录入）")
        self.assertEqual((out["b0"], out["b3"]), ("决策事项已全部处理", "待审阅与决策：3 项"))
        self.assertEqual(out["up"], "工作台界面同步于 2026-10-09 10:00")        # G06: not 数据更新于
        self.assertEqual(out["badge"], "2 项待处理")
        shell = (V2 / "shell.js").read_text(encoding="utf-8")
        self.assertNotIn("后台在线", shell + (V2 / "copy.js").read_text(encoding="utf-8").split("export const UI")[1])
        self.assertIn('setPill("updated", UI.updated(', shell)
        self.assertIn("function refreshPage()", shell)                        # G07
        self.assertIn('panel.setAttribute("aria-modal", "true")', shell)      # G08: overlay is modal
        self.assertIn('e.key !== "Tab" || !modal', shell)                     # focus stays inside

    def test_terminology_rules(self):
        out = node("pure", {"copy": [
            "放行一次重新进入（不花钱）", "再买一次模型调用（上限由你定）", "撤回这项研究（规划器可按当前预算重新立项）",
            "停止这条研究线", "否定条件已出现 1 个", "这一条由你判断，程序不读", "资料底座齐了", "重新进行语言质检",
            "流水线和工单", "按钮通过写入服务以你的身份执行，每次都需要写理由。", "已自动改绑票据", "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "us-gaap:SomethingUnknown", "多空地图", "挡住了整条研究链"]})
        shown = out["copy"]
        # am (report chapter 10) overrides ah's wording of these four.
        self.assertEqual(shown[:4], ["恢复专项课题研究", "扩大资料检索范围并重新获取",
                                     "暂缓当前专项课题（保留现有底稿）", "暂缓当前专项课题"])
        self.assertEqual(shown[4], "核心假设证伪预警触发 1 个")
        self.assertEqual(shown[5], "需人工核实确认")
        self.assertEqual(shown[6], "所需研究资料已齐备")
        self.assertEqual(shown[7], "重新生成中文定稿")
        self.assertEqual(shown[8], "研究流程和待办决策事项")
        self.assertEqual(shown[9], "操作会记录在项目历史中。")
        self.assertNotIn("票据", shown[10])
        self.assertEqual(shown[11], "经营活动现金流净额")
        self.assertEqual(shown[12], "us-gaap:SomethingUnknown")                # unknown concepts are not guessed
        self.assertEqual((shown[13], shown[14]), ("核心争议", "P0 · 阻断核心研判推进"))
        self.assertTrue(all(out["copyTwice"]))

    def test_unknown_enums_and_metrics_never_show_raw(self):
        r = node("call", {"module": "copy.js", "calls": [
            ["enumText", {"a": "甲"}, "a"], ["enumText", {"a": "甲"}, "zzz_status"],
            ["metricLabel", "price_to_book"], ["metricLabel", "weird_metric", "weird metric"], ["metricLabel", None, "自定义指标"]]})
        self.assertEqual(r[0]["ok"], {"text": "甲", "known": True, "code": "a"})
        self.assertEqual(r[1]["ok"], {"text": "状态未知", "known": False, "code": "zzz_status"})
        self.assertEqual(r[2]["ok"], "市净率（倍）")
        self.assertEqual(r[3]["ok"], "暂无中文名称的指标")
        self.assertEqual(r[4]["ok"], "自定义指标")

    def test_density_and_shared_classes_exist(self):
        css = (V2 / "shell.css").read_text(encoding="utf-8")
        for selector in (".v2-toc--collapsible", ".v2-compact", ".v2-nav-collapsed", ".v2-state", "dialog.v2-dialog",
                         ".v2-refresh", ".v2-fresh", '.v2-card[data-state="done"]', ".v2-progress"):
            self.assertIn(selector, css)
        section = re.search(r"\.v2-section\{[^}]*\}", css).group(0)
        self.assertNotIn("box-shadow", section)                               # G03: shadows only on floating layers
        self.assertIn("box-shadow:var(--v2-shadow)", re.search(r"dialog\.v2-dialog\{[^}]*\}", css).group(0))

    def test_shared_components_are_exported_for_the_other_pages(self):
        common = (V2 / "pages/common.js").read_text(encoding="utf-8")
        for name in ("export {confirmDialog, glossSegments}", "export function collapsibleToc",
                     "export function confirmAction", "export function autoReason"):
            self.assertIn(name, common)
        self.assertNotIn("export function compareTable", common)                 # one comparison: company_compare.js
        self.assertRegex((V2 / "pages/overview.js").read_text(encoding="utf-8"), r'import \{[^}]*\bcomparisonSection\b[^}]*\} from "\./company_compare\.js"')
        shell = (V2 / "shell.js").read_text(encoding="utf-8")
        self.assertIn("export function confirmDialog", shell)
        self.assertIn("confirmAction: ({title, consequence, danger = false, typeToConfirm = null", shell)   # the admin page's shape

    def test_canaries_banned_wording_on_analyst_pages(self):
        banned = ["再买一次", "放行一次", "改绑票据", "停止这条研究线", "否定条件已出现", "由你判断，程序不读", "后台在线", "后台未响应",
                  "数据更新于", "跳到正文", "返回旧版", "研究受阻", "重新进行语言质检", "本次 AI 费用", "会产生一次 AI 调用费用",
                  "不会自动带上上一条问题的上下文", "对比：左", "对比：右"]
        files = [V2 / "shell.js", V2 / "shell.html", *sorted((V2 / "pages").glob("*.js"))]
        for path in files:
            if path.name in {"companies.js", "scenario_edit.js", "research_log.js", "feedback.js"}:
                continue   # other owners' pages; checked by their own reviews
            text = path.read_text(encoding="utf-8")
            strings = re.findall(r'"([^"\n]*)"|`([^`]*)`', re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S))
            shown = " ".join(a or b for a, b in strings) if path.suffix == ".js" else re.sub(r"<!--.*?-->", "", text, flags=re.S)
            for word in banned:
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, shown)


@unittest.skipUnless(NODE, "node is not installed")
class OverviewAndSettingsTests(unittest.TestCase):
    def test_overview_never_prints_an_internal_reference(self):
        data = fixtures()
        acn = data["/v1/cockpit/overview"]["companies"][0]
        acn["events"]["latest"].append({"kind": "document", "kind_label": "专家访谈", "occurred_at": "2026-10-08T05:00:00+00:00",
                                        "summary": "guidepoint-excerpt:sha256:" + "a" * 64})
        acn["events"]["latest"].append({"kind": "document", "kind_label": "网页", "occurred_at": "2026-10-08T05:00:00+00:00",
                                        "summary": "public-web-url:sha256:" + "b" * 64})
        text = node("pages", {"fixtures": data, "render": {"o": "#/overview"}})["results"]["o"]["text"]
        self.assertNotIn("sha256", text)
        self.assertIn("新增专家访谈纪要（内容解析中）", text)
        self.assertIn("新增公开网页资料（内容解析中）", text)
        self.assertIn("新入库事实纪要", text)                                  # am 3.3: material apart from views

    def _overview_with_live_theses(self):
        from tests.test_cockpit_v2_company_page import LIVE_THESES

        data = fixtures()
        ov = data["/v1/cockpit/overview"]
        ov["theses"] = json.loads(json.dumps(LIVE_THESES, ensure_ascii=False))
        # every recent judgement is NO_CHANGE: the headline falls back to a thesis
        for c in ov["companies"]:
            for j in (c.get("judgements") or {}).get("latest") or []:
                j["decision"], j["action"] = "NO_CHANGE", "no_change"
        return data

    def test_overview_shows_only_current_theses_split_industry_and_company(self):
        """P0 2026-10-09: two contradictory versions of one thesis side by side."""
        data = self._overview_with_live_theses()
        r = node("pages", {"fixtures": data, "render": {"o": "#/overview"}})["results"]["o"]
        text = r["text"]
        conclusions, _ = text.split("各标的当前研判", 1)
        # 核心投资判断 never reads a superseded version; the industry thesis is preferred
        latest = conclusions.split("核心投资判断", 1)[1].split("研判依据与主要变化", 1)[0]
        body = conclusions.split("待推进投研事项", 1)[1].split("行业核心观点与宏观假设", 1)[1]
        self.assertIn("底部仍未确认", latest)
        self.assertIn("行业核心论点，截至2026年9月14日", latest)
        self.assertNotIn("需求正接近底部", latest)
        main, evolution = body.split("历史观点演变与分歧记录", 1)
        # exactly the two current rows, industry then company, with date, version and change
        self.assertIn("底部仍未确认", main)
        self.assertIn("AI 路径可信但仍是低置信假设", main)
        self.assertNotIn("需求正接近底部", main)
        self.assertNotIn("可维持中个位数增长", main)
        self.assertIn("生效基准日：2026年9月14日 · 研判版本：第 2 版 · 论点受损", main)
        industry, company = main.split("公司核心论点", 1)
        self.assertIn("底部仍未确认", industry)
        self.assertNotIn("AI 路径可信", industry)
        self.assertIn("Accenture ACN", company)
        self.assertNotIn("行业投资论点", text)
        # the evolution fold lists the two superseded versions, marked 历史归档
        self.assertIn("历史观点演变与分歧记录（2 条，已归档，仅供研判脉络对照）", text)
        self.assertIn("需求正接近底部", evolution)
        self.assertIn("可维持中个位数增长", evolution)
        self.assertEqual(evolution.count("历史归档"), 2)   # the two items
        self.assertFalse(r["nullText"])

    def test_headline_never_picks_a_superseded_thesis(self):
        data = self._overview_with_live_theses()
        ov = data["/v1/cockpit/overview"]
        # only company theses, and the current one listed last: still never v1
        ov["theses"] = [t for t in ov["theses"] if t["company_ref"]]
        text = node("pages", {"fixtures": data, "render": {"o": "#/overview"}})["results"]["o"]["text"]
        latest = text.split("核心投资判断", 1)[1].split("研判依据与主要变化", 1)[0]
        self.assertIn("AI 路径可信但仍是低置信假设", latest)
        self.assertNotIn("可维持中个位数增长", latest)
        # the server's guard wins over a row that claims to be current
        ov["theses"] = [dict(t, current=True) for t in ov["theses"]]
        ov["theses_current"] = [t for t in ov["theses"] if t["version"] == 2]
        text = node("pages", {"fixtures": data, "render": {"o": "#/overview"}})["results"]["o"]["text"]
        latest = text.split("核心投资判断", 1)[1].split("研判依据与主要变化", 1)[0]
        self.assertNotIn("可维持中个位数增长", latest)
        self.assertIn("AI 路径可信但仍是低置信假设", latest)

    def test_settings_presets_fill_the_form_without_publishing(self):
        from tests.test_cockpit_v2_migration import POLICY
        data = fixtures(**{"/v1/cockpit/conviction-policy": POLICY})
        out = node("pages", {"fixtures": data, "render": {"s": "#/settings"},
                             "steps": [{"page": "s", "op": "click", "text": "稳健"}]})
        after = out["results"]["s:after"]
        self.assertIn("已按「稳健」填好表单，尚未发布", after["text"])
        self.assertIn("当前为「稳健」", after["text"])
        self.assertEqual(posts(out, "/v1/cockpit/conviction_policy"), [])
        self.assertIn("高级：逐项调整（共 15 项，含单位和说明）", after["text"])
        self.assertIn("单位：天", after["text"])
        held = json.loads(after["drafts"]["policy:form"])
        self.assertEqual(held["values"]["min_evidence_confidence"], "high")
        self.assertEqual(held["values"]["min_expected_return_percent"]["6_12_months"], "20")
        r = node("call", {"module": "pages/policy.js", "calls": [["matchPreset", {}]]})
        self.assertIsNone(r[0]["ok"])

    def test_weekly_brief_section_chooses_a_destination_and_shows_the_next_send(self):
        view = {"enabled": True, "plan_hash": "h" * 64, "next_run_at": "2026-10-14T11:00:00+00:00",
                "plan": {"weekday": 2, "weekday_label": "周三", "hour": 7, "minute": 0, "timezone": "America/New_York",
                         "destination_ref": "openclaw:feishu:default:chat:oc_1234abcd", "local": False},
                "destinations": ["openclaw:feishu:default:chat:oc_1234abcd"],
                "cycles": [{"scheduled_for": "2026-09-30T11:00:00+00:00", "issue_version_ref": "v1", "destination_ref": "openclaw:feishu:default:chat:oc_1234abcd"},
                           {"scheduled_for": "2026-10-07T11:00:00+00:00", "issue_version_ref": "v2", "destination_ref": "openclaw:feishu:default:chat:oc_1234abcd"}],
                "deliveries": [{"issue_version_ref": "v1", "delivered_at": "2026-09-30T11:02:00+00:00"}], "local_briefs": []}
        out = node("pages", {"fixtures": fixtures(**{"/v1/cockpit/weekly-brief": view}), "render": {"s": "#/settings?focus=brief"},
                             "steps": [{"page": "s", "op": "set", "id": "brief-time", "value": "08:30"},
                                       {"page": "s", "op": "submit", "id": "brief-form"}],
                             "postReplies": {"/v1/cockpit/weekly_brief_plan": {"status": "published"}}})
        sent = posts(out, "/v1/cockpit/weekly_brief_plan")
        self.assertEqual(sent, [{"weekday": 2, "hour": 8, "minute": 30, "timezone": "America/New_York",
                                 "destination_ref": "openclaw:feishu:default:chat:oc_1234abcd", "expected_plan_hash": "h" * 64}])
        text = out["results"]["s"]["text"]
        self.assertIn("飞书群聊（尾号 abcd）", text)
        self.assertNotIn("openclaw:", text)                                   # W06
        self.assertIn("2026-10-14 07:00（纽约时间）", text)
        self.assertIn("已送达", text)
        self.assertIn("未送达", text)
        self.assertIn("有周报尚未送达。", text)
        self.assertIn("可以查看已生成的正文，核对发送设置。", text)
        self.assertIn("查看周报", text)
        self.assertNotIn("本期周报尚未生成。", text)

        # A failed cycle without an issue cannot offer a document or claim
        # that a completed report has been archived or delivered.
        missing = json.loads(json.dumps(view))
        missing["cycles"] = [dict(view["cycles"][-1], issue_version_ref=None)]
        missing["deliveries"] = []
        pending = node("pages", {"fixtures": fixtures(**{"/v1/cockpit/weekly-brief": missing}),
                                 "render": {"s": "#/settings?focus=brief"}})["results"]["s"]["text"]
        self.assertIn("本期周报尚未生成。", pending)
        self.assertIn("暂时没有可查看的正文。", pending)
        self.assertNotIn("查看周报", pending)
        self.assertNotIn("可以查看已生成的正文", pending)


class ServerFollowUpContractTests(unittest.TestCase):
    def test_previous_turn_is_bounded_and_has_no_refs(self):
        from dalton_core.ask_context import (MAX_FOLLOWUP_ANSWER_CHARS, MAX_FOLLOWUP_CITATIONS, previous_turn)

        job = {"job_id": "cockpit-job:a", "request": {"question": "问" * 900},
               "result": {"answer": "raw", "display_answer": "答" * 9000,
                          "citations": [{"statement": "证" * 900, "ref": "claim-version:" + "a" * 64, "company": "ACN",
                                         "period": "2026Q3", "period_label": "2026 年第三季度"}] * 20}}
        turn = previous_turn(job)
        self.assertLessEqual(len(turn["answer"]), MAX_FOLLOWUP_ANSWER_CHARS + 1)
        self.assertEqual(len(turn["citations"]), MAX_FOLLOWUP_CITATIONS)
        self.assertNotIn("claim-version", json.dumps(turn, ensure_ascii=False))
        self.assertEqual(turn["citations"][0]["period"], "2026 年第三季度")
        self.assertLess(len(json.dumps(turn, ensure_ascii=False)), 6000)
        self.assertIsNone(previous_turn({"request": {"question": "x"}, "result": {"answer": ""}}))

    def test_the_prompt_carries_the_previous_turn_only_for_a_follow_up(self):
        from dalton_core import ask_answer
        from tests.test_ask_v2 import context_for, empty_core, mission

        context = context_for("那利润率呢？", empty_core())
        plain = ask_answer.build_prompt(context, mission=mission())
        self.assertNotIn("上一个问题", plain)
        previous = {"job_id": "j", "question": "ACN 需求怎么样？", "answer": "需求偏弱。",
                    "citations": [{"statement": "订单下滑。", "company": "ACN", "period": "2026Q3"}]}
        follow = ask_answer.build_prompt(context, mission=mission(), previous=previous)
        self.assertIn("上一个问题：ACN 需求怎么样？", follow)
        self.assertIn("- 订单下滑。（ACN，2026Q3）", follow)
        self.assertIn("不能引用", follow)
        self.assertTrue(follow.rstrip().endswith("问题：那利润率呢？"))

    def test_a_follow_up_without_a_company_reads_the_previous_question_subjects(self):
        from dalton_core import ask_context
        from tests.test_ask_v2 import ACN, TODAY, claims, empty_core, label, members, mission, names

        def build(hint):
            return ask_context.build_context(empty_core(), question="那利润率呢？", mission=mission(), members=members(),
                                             claims=claims(), company_names=names(), label=label, today=TODAY, subject_hint=hint)
        self.assertEqual(build("")["subjects"]["scope"], "universe")
        hinted = build("ACN 最近的需求怎么样？")
        self.assertEqual(hinted["subjects"]["companies"], [ACN])
        self.assertEqual(hinted["subjects"]["scope"], "follow_up")

    def test_the_plane_carries_the_previous_answer_end_to_end(self):
        from dalton_core import ask_answer
        from dalton_core.cockpit_plane import CockpitError
        from tests.test_cockpit_plane import CockpitHarness

        with tempfile.TemporaryDirectory() as temp:
            c = CockpitHarness(Path(temp))
            try:
                login = "owner@example.com"
                first = c.plane.ask(login, {"question": "ACN 需求怎么样？", "request_id": "fu-1"})
                self.assertEqual(c.wait(first["job_id"], login)["status"], "done")
                with mock.patch.object(ask_answer, "build_prompt", wraps=ask_answer.build_prompt) as built:
                    second = c.plane.ask(login, {"question": "那利润率呢？", "request_id": "fu-2", "follow_up_of": first["job_id"]})
                    done = c.wait(second["job_id"], login)
                self.assertEqual(done["status"], "done", done.get("error"))
                self.assertEqual(done["request"]["follow_up_of"], first["job_id"])
                self.assertEqual(done["request"]["follow_up_question"], "ACN 需求怎么样？")
                self.assertEqual(done["result"]["follow_up"]["of"], first["job_id"])
                self.assertEqual(built.call_args.kwargs["previous"]["question"], "ACN 需求怎么样？")
                with self.assertRaises(CockpitError):
                    c.plane.ask(login, {"question": "x", "request_id": "fu-3", "follow_up_of": "cockpit-job:nope"})
                with self.assertRaises(CockpitError):   # another owner's answer is not found either
                    c.plane.ask("someone-else@example.com", {"question": "x", "request_id": "fu-4", "follow_up_of": first["job_id"]})
            finally:
                c.close()


class NeedsHumanGroupingTests(unittest.TestCase):
    def batch(self, n):
        from dalton_core.needs_human import _item
        ref = "ui-text-batch:" + str(n) * 64
        return _item("publication_parked", ref=f"publication-parked:{ref}", at=f"2026-10-0{n}T00:00:00+00:00",
                     title="部分研究结论的中文译稿待重新生成：已连续 6 次没有通过校对", why="w", action="a", consequence="c",
                     where="本卡片", detail={"batch_ref": ref, "attempts": 6, "reason": "independent localization verifier did not pass",
                                           "last_failure": "independent localization verifier did not pass",
                                           "retry": {"action": "language_retry", "payload": {"batch_ref": ref}}})

    def test_several_batches_become_one_card_and_one_stays_itself(self):
        from dalton_core.needs_human import UI_TEXT_GROUP_REF, completion_entry, group_ui_text_batches

        other = {"kind": "raw_spool_capacity", "ref": "spool"}
        single = group_ui_text_batches([self.batch(1), other])
        self.assertEqual([x["ref"] for x in single], [self.batch(1)["ref"], "spool"])
        grouped = group_ui_text_batches([self.batch(1), other, self.batch(2), self.batch(3)])
        self.assertEqual(len(grouped), 2)
        card = next(x for x in grouped if x["ref"] == UI_TEXT_GROUP_REF)
        self.assertIn("（3 批）", card["title"])
        self.assertFalse(card['actionable'])
        self.assertEqual(card['responsibility'],'administrator')
        self.assertEqual([m['attempts'] for m in card['detail']['members']],[6,6,6])
        self.assertEqual(card["at"], "2026-10-03T00:00:00+00:00")
        entry = completion_entry(card)
        self.assertEqual(entry["mode"], "action_group")
        self.assertEqual(entry["label"], "全部重新生成中文定稿")
        self.assertEqual(len(entry["payloads"]), 3)
        self.assertEqual([m["acknowledge"]["card_ref"] for m in entry["members"]], [self.batch(n)["ref"] for n in (1, 2, 3)])
        self.assertNotIn("acknowledge", entry)
        shown = json.dumps({k: card[k] for k in ("title", "why_blocked", "action")}, ensure_ascii=False)
        for word in ("independent localization verifier", "语言质检", "sha256"):
            self.assertNotIn(word, shown)

    def test_door_labels_are_business_words(self):
        from dalton_core.cockpit_owner_doors import DOOR_LABELS
        from dalton_core.needs_human import LANGUAGE_RETRY_LABEL, hold_doors

        text = json.dumps([DOOR_LABELS, LANGUAGE_RETRY_LABEL,
                           hold_doors("mission_document_research", "mission-document-research-admission:" + "a" * 32,
                                      "reentry_failed_after_automatic_rebind:x")], ensure_ascii=False)
        for word in ("再买一次", "放行", "撤回", "规划器", "车道", "语言质检", "付费重试", "不产生费用"):
            self.assertNotIn(word, text)
        # Batch am: the registry's words (text evaluation 7.3).
        self.assertEqual(DOOR_LABELS["reentry"], "恢复专项课题研究")
        self.assertEqual(DOOR_LABELS["paid"], "扩大资料检索范围并重新获取")
        self.assertEqual(DOOR_LABELS["withdraw"], "暂缓当前专项课题（保留现有底稿）")


class FinalTextAndWeeklyBriefTests(unittest.TestCase):
    def test_pending_text_is_shown_as_a_badged_draft(self):
        from dalton_core.final_text_state import PENDING, PENDING_BADGE, final_text_state

        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "research-language-policy.json").write_text(json.dumps({"required": True}), encoding="utf-8")
            now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
            state = final_text_state(temp, None, reviewed=False, since="2026-10-09T11:50:00+00:00", now=now)
            self.assertEqual(state["status"], PENDING)
            self.assertTrue(state["show_draft"])
            self.assertEqual(state["badge"], PENDING_BADGE)
            self.assertEqual(PENDING_BADGE, "初稿（待审核签发）")
            self.assertEqual(final_text_state(temp, None, reviewed=True, now=now), {"status": "ready"})   # verified wins

    def test_next_send_time_follows_the_plan_zone(self):
        from dalton_core.cockpit_weekly_brief import _destinations, next_run_at

        now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)   # a Friday
        self.assertEqual(next_run_at({"timezone": "America/New_York", "weekday": 2, "hour": 7, "minute": 0}, now),
                         "2026-10-14T11:00:00+00:00")
        self.assertEqual(next_run_at({"timezone": "America/New_York", "weekday": 2, "hour": 7, "minute": 0},
                                     datetime(2026, 11, 3, 12, tzinfo=timezone.utc)), "2026-11-04T12:00:00+00:00")   # after DST
        self.assertIsNone(next_run_at({"timezone": "Not/AZone", "weekday": 0, "hour": 1, "minute": 0}, now))
        self.assertEqual(_destinations({"destination_ref": "a"}, {"cycles": [{"destination_ref": "b"}, {"destination_ref": "a"}],
                                                                  "deliveries": [{"destination_ref": "c"}]}), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
