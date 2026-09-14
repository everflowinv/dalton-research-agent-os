import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"


class CockpitRuntimeTextBoundariesTests(unittest.TestCase):
    def test_lane_terminal_words_have_visible_labels_and_failure_styling(self):
        source = HTML.read_text(encoding="utf-8")
        self.assertIn('terminal:"本次任务已结束"', source)
        self.assertIn('recovery_required:"需要恢复后继续"', source)
        self.assertIn('duplicate:"已有相同结果"', source)
        self.assertIn('"terminal","recovery_required"', source)

    def _evaluate(self, expression: str):
        if shutil.which("node") is None:
            self.skipTest("node is unavailable")
        source = HTML.read_text(encoding="utf-8")
        names = (
            "readableLaneDetail",
            "readableLogText",
            "readableReflectionAuthority",
            "displayMissionText",
            "readableEventJudgement",
        )
        functions = []
        for name in names:
            match = re.search(
                rf"function {name}\([^\n]+\)\{{.*?\n\}}", source, re.DOTALL
            )
            self.assertIsNotNone(match, name)
            functions.append(match.group(0))
        program = "\n".join([
            "const UI_TEXT={};",
            "const FINAL_RESEARCH_REQUIRED=false;",
            "const displayText=value=>typeof value==='string' && Object.prototype.hasOwnProperty.call(UI_TEXT,value)?UI_TEXT[value]:value;",
            "const finalResearchText=displayText;",
            "const looksTechnical=value=>typeof value==='string' && /[a-z_]{4,}/.test(value);",
            *functions,
            f"console.log(JSON.stringify({expression}));",
        ])
        result = subprocess.run(
            ["node", "-e", program], text=True, capture_output=True, check=True
        )
        return json.loads(result.stdout)

    def test_plan_machine_reasons_are_readable_and_raw_value_is_retained(self):
        raw = (
            "company:sec-cik:0000051143：硬性校验失败（numbers_without_refs）；"
            '首个目标为 {"check":"numbers_without_refs","figure":"18"}。'
        )
        result = self._evaluate(f"readableLaneDetail({json.dumps(raw)})")
        self.assertEqual(result["display"], "研究报告未通过内容结构或数字来源检查。")
        self.assertEqual(result["technical"], raw)

        unknown = "unrecognized_future_reason"
        result = self._evaluate(f"readableLaneDetail({json.dumps(unknown)})")
        self.assertEqual(result["display"], "运行说明见技术详情。")
        self.assertEqual(result["technical"], unknown)

    def test_reviewed_lane_translation_is_matched_before_raw_is_folded(self):
        raw = "no source:prior-research lane on this writer"
        mapped = "该写作者没有source:prior-research通道。"
        expression = (
            f"(Object.assign(UI_TEXT, {{{json.dumps(raw)}:{json.dumps(mapped)}}}),"
            f" readableLaneDetail({json.dumps(raw)}))"
        )
        result = self._evaluate(expression)
        self.assertEqual(result["display"], "当前环境尚未接入既有研究资料来源。")
        self.assertEqual(result["technical"], raw)

        reviewed = "配置正常，本轮没有待办。"
        future_raw = "future_internal_code:alpha"
        expression = (
            f"(Object.assign(UI_TEXT, {{{json.dumps(future_raw)}:{json.dumps(reviewed)}}}),"
            f" readableLaneDetail({json.dumps(future_raw)}))"
        )
        result = self._evaluate(expression)
        self.assertEqual(result["display"], reviewed)
        self.assertEqual(result["technical"], future_raw)

    def test_all_observed_lane_templates_have_specific_plain_language(self):
        examples = {
            "the catalog has already been read this hour": "本小时已检查过模型目录",
            "this mission covers no Hong Kong listing; the universe is company:sec-cik: names and admitting a company:hk-secucode: one is an owner decision": "当前研究范围不含港股公司",
            "every ownership filing in the window has been read；跳过原因：held": "持仓变动申报已全部读取",
            "every pending company is durably held": "已记录为等待状态",
            "list_notes governance record is not approved; owner approval is required；governance_record=sales-notes-list-notes-v1.json": "数据访问尚未获批",
            "no source:company-wiki lane on this writer": "尚未接入公司知识库资料来源",
            "no crowd-source lane on this writer": "尚未接入散户舆情或职场评价资料来源",
            "annual research lane is absent": "尚未配置年报专题研究流程",
            "no unstarted document research admission": "尚未启动且已获准的原文专题研究任务",
        }
        for raw, phrase in examples.items():
            with self.subTest(raw=raw):
                result = self._evaluate(f"readableLaneDetail({json.dumps(raw)})")
                self.assertIn(phrase, result["display"])
                self.assertNotEqual(result["display"], "运行说明见技术详情。")
                self.assertEqual(result["technical"], raw)

    def test_source_connection_and_connector_approval_are_not_conflated(self):
        guidepoint = (
            "all_grants_refused；跳过原因：CoverageMissionConflict: "
            "mission marks source:guidepoint as not_connected"
        )
        result = self._evaluate(f"readableLaneDetail({json.dumps(guidepoint)})")
        self.assertEqual(result["display"], "当前研究任务未连接专家访谈资料来源，本轮未获取。")
        self.assertEqual(result["technical"], guidepoint)

        daily_prices = (
            "covered-company refresh is blocked (not_permitted=5)；"
            "governance_record=yfinance-daily-prices-v1.json；跳过原因：not_permitted"
        )
        result = self._evaluate(f"readableLaneDetail({json.dumps(daily_prices)})")
        self.assertEqual(result["display"], "每日行情数据源已配置，等待批准使用。")
        self.assertEqual(result["technical"], daily_prices)

        calendar = (
            "覆盖公司的刷新操作受阻（not_permitted=5）；"
            "治理记录：yfinance-calendar-v1.json；跳过原因：not_permitted。"
        )
        result = self._evaluate(f"readableLaneDetail({json.dumps(calendar)})")
        self.assertEqual(result["display"], "财报与分红日程的数据源已配置，等待批准使用。")
        self.assertEqual(result["technical"], calendar)

    def test_deep_insight_log_classification_is_translated_without_guessing_unknown(self):
        known = "深度认知门十二问草稿 v1，分类 turnaround，等待人裁决"
        result = self._evaluate(f"readableLogText({json.dumps(known)})")
        self.assertIn("经营改善观察", result["display"])
        self.assertEqual(result["technical"], known)

        unknown = "深度认知门十二问草稿 v2，分类 future_kind，等待人裁决"
        result = self._evaluate(f"readableLogText({json.dumps(unknown)})")
        self.assertIn("分类尚未识别", result["display"])
        self.assertNotIn("future_kind", result["display"])
        self.assertEqual(result["technical"], unknown)

    def test_legacy_reflection_authority_note_gets_plain_language(self):
        raw = (
            "这条记录只读不写：它不写 Ledger、不改 policy、不登记问题。"
            "backlog_candidates 是候选，要由 planner 或人经 ResearchQuestionBacklog 的准入路径登记；"
            "policy_suggestions 是句子。"
        )
        result = self._evaluate(f"readableReflectionAuthority({json.dumps(raw)})")
        self.assertIn("不会直接修改研究结论、规则或研究计划", result["display"])
        self.assertEqual(result["technical"], raw)

        near_match = raw + "另有一项尚未核实的事实。"
        result = self._evaluate(f"readableReflectionAuthority({json.dumps(near_match)})")
        self.assertEqual(result["display"], near_match)
        self.assertIsNone(result["technical"])

        short = "这条记录不改 policy，也不登记问题。"
        result = self._evaluate(f"readableReflectionAuthority({json.dumps(short)})")
        self.assertEqual(result["display"], "这条记录不会修改规则，也不会登记研究问题。")
        self.assertNotIn("研究结论", result["display"])
        self.assertEqual(result["technical"], short)

    def test_mission_terms_are_localized_without_touching_other_text(self):
        raw = "五家公司初筛（Initial Screen）与投资逻辑（Thesis）；验证 variant view"
        result = self._evaluate(f"displayMissionText({json.dumps(raw)})")
        self.assertEqual(result, "五家公司初步筛查报告与投资论点；验证 差异化观点")

    def test_known_event_classifications_are_readable_and_raw_is_retained(self):
        raw = "该事件是 qualitative、derived 层级的表述。"
        result = self._evaluate(f"readableEventJudgement({json.dumps(raw)})")
        self.assertEqual(result["display"], "该事件是 定性、系统推导层级的表述。")
        self.assertEqual(result["technical"], raw)
        unknown = "该事件是 vendor-special 层级的表述。"
        result = self._evaluate(f"readableEventJudgement({json.dumps(unknown)})")
        self.assertEqual(result, {"display": unknown, "technical": None})


if __name__ == "__main__":
    unittest.main()
