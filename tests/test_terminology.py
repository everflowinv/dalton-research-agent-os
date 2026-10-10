"""The terminology registry (batch am, 2026-10-09).

* the generated front-end module equals the registry export (no drift);
* the JS tables copy.js / charts/labels.js export are the registry's;
* scrub() (server) and copy() (page) give the same text for the same input,
  and both are idempotent;
* label() and condition_sentence() say what the report asks for;
* no registry label carries a banned term.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from dalton_core import terminology
from dalton_core.terminology import (BANNED_TERMS, COPY_TABLE, LABELS, banned_hits, condition_sentence,
                                     js_export, label, render_js_module, scrub)

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "src" / "dalton_core" / "cockpit_v2"
NODE = shutil.which("node") is not None

#: Server text of every kind the copy table rewrites (and some it must not).
CORPUS = [
    "研究环境「新研究环境」还没有研究目标",
    "维持现状", "维持现有判断", "本行为收入（Revenues）科目。", "本行为服务成本（Cost of services）科目。",
    "否定：收入下滑", "重新进入失败（已自动改绑票据并重试一次）", "先看原因（不加 --apply 是只读预览，只看不改）；不值得再试",
    "占着端口和进程，不产出任何研究", "已绑定来源 · 已绑定来源", "15 份研究产物", "Conviction v2 阈值",
    "少数股东损益", "草稿，文字表达尚未校对", "下载 HTML 报告", "代码默认（尚未发布任何版本）",
    "这一条由你判断，程序不读", "投资者日说法（由你判断，2026-10-31 前）", "研究车道与研究线",
    "放行一次重新进入（不花钱）", "再买一次模型调用（上限由你定）", "撤回这项研究（规划器可按当前预算重新立项）",
    "恢复研究任务（不产生费用）", "付费重试获取资料（费用上限由你定）", "停止这项研究（已有成果保留，需要时可重新开始）",
    "2 个否定条件已出现", "证伪预警触发：1 个情景已失效", "语言质检没有通过", "中文校对", "流水线、工单与规划器",
    "按钮通过写入服务以你的身份执行，记录在案。", "票据过期", "挡住了整条研究链", "积压的待办",
    "us-gaap:Revenues 与 us-gaap:UnknownThing", "草稿 · 尚未审核", "投资建议规则", "使用系统默认规则",
    "原文哈希 abc，内容哈希不符", "原文 SHA-256：abc", "停止等待", "「放行一次重新进入」是原话",
    # Machine-precision numbers (coordinator 2026-10-09): shown as a reader says them.
    "毛利率为 0.577264（即57.73%），净利润", "金额 20167113474.15073849 美元", "得分 28.14828 分",
    "毛利 24033000000 除以营业收入 44,479,000,000 得出", "accession 0000051143-24-000049", "CIK 0001467373",
    "日期 20261009", "比率 0.153908", "增长 1.09%", "12345678万", "1234567 股", "-0.0012345", "收入 1,716,200 美元",
    "毛利24033000000元", "v2.1 版本", "28.145 与 0.00001234567",
    # Machine words in stored text (batch am).
    "ACN · Accenture · price_move：price_move", "ACN · Accenture：NO_CHANGE / research —— 投资论点",
    "10月8日 ACN 的单日涨跌幅（price → xueqiu）", "卖方点评（技术细节：`transcript → alphaengine`）",
    "THESIS_BROKEN", "the research note", "claim_ref 不变", "的price_move与unknown_kind",
]


def node_eval(script: str) -> object:
    result = subprocess.run(["node", "--input-type=module", "-e", script], text=True, capture_output=True,
                            timeout=60, cwd=str(V2))
    if result.returncode != 0:
        raise AssertionError(result.stderr[-3000:])
    return json.loads(result.stdout)


class RegistryTests(unittest.TestCase):
    def test_generated_module_equals_the_registry(self):
        on_disk = terminology.JS_MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(on_disk, render_js_module(),
                         "cockpit_v2/charts/vocabulary.js is stale: PYTHONPATH=src python -m dalton_core.terminology --write")

    def test_round_trip_of_the_export(self):
        data = js_export()
        self.assertEqual(json.loads(json.dumps(data, ensure_ascii=False)), data)
        self.assertEqual(set(data["labels"]), set(LABELS))
        self.assertEqual(len(data["copyTable"]), len(COPY_TABLE))
        self.assertEqual([b["term"] for b in data["banned"]], [b.term for b in BANNED_TERMS])

    def test_label_fills_placeholders_and_refuses_unknown_keys(self):
        self.assertEqual(label("shell", "blocked", n=3), "待审阅与决策：3 项")
        self.assertEqual(label("comparison", "bear"), "下行风险情景（发生概率 / 潜在跌幅）")
        self.assertEqual(label("comparison", "expected_return"), "12M 预期回报率")
        self.assertEqual(label("comparison", "not_comparable"), "口径暂不可比（预测周期不一致）")
        self.assertEqual(label("recovery_door", "reentry"), "恢复专项课题研究")
        self.assertEqual(label("recovery_door", "paid"), "扩大资料检索范围并重新获取")
        self.assertEqual(label("recovery_door", "withdraw"), "暂缓当前专项课题（保留现有底稿）")
        self.assertEqual(label("scenario", "invalidated"), "核心假设证伪预警触发")
        self.assertEqual(label("ack", "noted"), "已阅知悉")
        self.assertEqual(label("nav", "nope", "默认"), "默认")
        with self.assertRaises(KeyError):
            label("nav", "nope")

    def test_no_label_carries_a_banned_term(self):
        def walk(value, path):
            if isinstance(value, dict) or hasattr(value, "items"):
                for k, v in value.items():
                    yield from walk(v, f"{path}.{k}")
            elif isinstance(value, str):
                yield path, value
        for path, text in walk(js_export()["labels"], "labels"):
            self.assertEqual(banned_hits(text), [], path)

    def test_scrub_removes_the_banned_terms_it_has_rules_for(self):
        for text in CORPUS:
            once = scrub(text)
            self.assertEqual(scrub(once), once, text)          # idempotent
            if "「" in text:
                continue
            for term in ("再买一次", "付费重试", "放行", "规划器", "流水线", "工单", "票据", "改绑", "车道",
                         "否定条件已出现", "程序不读", "代码默认", "Conviction v2", "--apply", "哈希", "SHA-256"):
                self.assertNotIn(term, once, text)
        self.assertEqual(scrub("恢复研究任务（不产生费用）"), "恢复专项课题研究")
        self.assertEqual(scrub("付费重试获取资料（费用上限由你定）"), "扩大资料检索范围并重新获取")
        self.assertEqual(scrub("停止这项研究（已有成果保留，需要时可重新开始）"), "暂缓当前专项课题（保留现有底稿）")
        self.assertEqual(scrub("2 个否定条件已出现"), "2 个核心假设证伪预警触发")
        self.assertEqual(scrub("证伪预警触发：1 个情景已失效"), "核心假设证伪预警触发：1 个情景已失效")
        self.assertEqual(scrub("语言质检没有通过"), "中文定稿校对没有通过")
        self.assertEqual(scrub("us-gaap:Revenues 与 us-gaap:UnknownThing"), "营业收入 与 us-gaap:UnknownThing")
        self.assertEqual(scrub("「放行一次重新进入」是原话"), "「放行一次重新进入」是原话")   # quotations untouched
        self.assertEqual(scrub(None), None)

    def test_machine_numbers_are_shown_as_a_reader_says_them(self):
        self.assertEqual(scrub("毛利率为 0.577264（即57.73%），净利润"), "毛利率为 57.73%，净利润")
        self.assertEqual(scrub("金额 20167113474.15073849 美元"), "金额 201.7 亿美元")
        self.assertEqual(scrub("得分 28.14828 分"), "得分 28.15 分")
        self.assertEqual(scrub("毛利 24033000000 除以营业收入 44,479,000,000 得出"), "毛利 240.3 亿 除以营业收入 444.8 亿 得出")
        for kept in ("accession 0000051143-24-000049", "CIK 0001467373", "日期 20261009", "增长 1.09%", "12345678万"):
            self.assertEqual(scrub(kept), kept)
        for text in CORPUS:
            self.assertEqual(banned_hits(scrub(text)) and [h for h in banned_hits(scrub(text)) if h[0].term == "机器精度数字"], [], text)

    def test_machine_words_are_looked_up_in_the_registry(self):
        self.assertEqual(scrub("ACN · Accenture · price_move：price_move"), "ACN · Accenture · 股价异动：股价异动")
        self.assertEqual(scrub("ACN：NO_CHANGE / research —— 理由"), "ACN：维持基准判断（启动专项研究） —— 理由")
        self.assertEqual(scrub("单日涨跌幅（price → xueqiu）"), "单日涨跌幅")
        self.assertEqual(scrub("the research note"), "the research note")       # English prose untouched
        self.assertEqual(scrub("unknown_kind 不变"), "unknown_kind 不变")         # unknown words stay

    def test_scrub_tree_touches_only_chinese_display_strings(self):
        from dalton_core.terminology import scrub_tree
        payload = {"source_label": "Yahoo Finance 每日行情（经行情同步车道记录）", "value": "18044066000",
                   "claim_ref": "claim-version:abc（车道）", "rows": [{"label": "营业利润率为 0.153908（即 15.39%）"}],
                   "title": "Revenue 18044066000", "n": 3}
        self.assertEqual(scrub_tree(payload), {
            "source_label": "Yahoo Finance 每日行情（经行情同步流程记录）", "value": "18044066000",
            "claim_ref": "claim-version:abc（车道）", "rows": [{"label": "营业利润率为 15.39%"}],
            "title": "Revenue 18044066000", "n": 3})

    def test_condition_sentences(self):
        self.assertEqual(condition_sentence(kind="invalidator", metric="营业收入", transform_label="同比增速",
                                            comparator="lt", threshold="3.0%", horizon="2027-03-31"),
                         "预警触发线：营业收入同比增速放缓至 3.0% 以下（观察至 2027-03-31）")
        self.assertEqual(condition_sentence(kind="trigger", metric="营业收入", transform_label="",
                                            comparator="gt", threshold="187.2 亿美元"),
                         "触发信号：营业收入高于 187.2 亿美元")
        self.assertEqual(condition_sentence(kind="invalidator", metric="", transform_label="", comparator="gte",
                                            threshold="1 次", count_event="核心论点被推翻", horizon="2026-12-31"),
                         "预警触发线：出现研判为「核心论点被推翻」的事件（观察至 2026-12-31）")
        self.assertEqual(condition_sentence(kind="trigger", metric="", transform_label="", comparator="gte",
                                            threshold="3 次", count_event="核心论点增强"),
                         "触发信号：研判为「核心论点增强」的事件累计达到 3 起")
        self.assertEqual(condition_sentence(kind="trigger", metric="", transform_label="", comparator="gte",
                                            threshold=None, owner_label="投资者日说法", horizon="2026-10-31"),
                         "触发信号：投资者日说法（需人工核实确认，观察至 2026-10-31）")
        for text in ("<", "≥", "rule:"):
            self.assertNotIn(text, condition_sentence(kind="trigger", metric="毛利率", transform_label="",
                                                      comparator="gte", threshold="30%"))


@unittest.skipUnless(NODE, "node is not installed")
class FrontEndParityTests(unittest.TestCase):
    def test_js_tables_are_the_registry_export(self):
        out = node_eval(
            'import * as c from "./copy.js"; import {TERMS} from "./charts/labels.js";'
            'import {LABELS} from "./charts/vocabulary.js";'
            'console.log(JSON.stringify({labels: LABELS, terms: TERMS, glossary: c.GLOSSARY, metrics: c.METRIC_LABELS,'
            ' concepts: c.CONCEPT_LABELS, sections: c.SECTION_TITLES, roles: c.DEBATE_ROLES,'
            ' table: c.COPY_TABLE.map(r => ({current: r.current, suggested: r.suggested, note: r.note, mode: r.mode, rules: r.rules.length})),'
            ' ui: {online: c.UI.serviceOnline, blocked: c.UI.blocked(2), none: c.UI.blocked(0), nav: c.UI.nav, refresh: c.UI.refresh}}));')
        export = js_export()
        self.assertEqual(out["labels"], export["labels"])
        self.assertEqual(out["terms"], export["labels"]["term_gloss"])
        self.assertEqual(out["glossary"], export["labels"]["glossary"])
        self.assertEqual(out["metrics"], export["labels"]["metric"])
        self.assertEqual(out["concepts"], export["labels"]["statement_line"])
        self.assertEqual(out["sections"], export["labels"]["section_title"])
        self.assertEqual(out["roles"], export["labels"]["debate_role"])
        self.assertEqual(out["table"], [{"current": r["current"], "suggested": r["suggested"], "note": r["note"],
                                         "mode": r["mode"], "rules": len(r["rules"])} for r in export["copyTable"]])
        self.assertEqual(out["ui"], {"online": label("shell", "service_online"), "blocked": label("shell", "blocked", n=2),
                                     "none": label("shell", "blocked_none"), "nav": export["labels"]["nav"],
                                     "refresh": label("shell", "refresh")})

    def test_scrub_and_copy_agree(self):
        out = node_eval('import {copy} from "./copy.js"; const texts = ' + json.dumps(CORPUS, ensure_ascii=False)
                        + '; console.log(JSON.stringify(texts.map(t => [copy(t), copy(copy(t))])));')
        for text, (shown, twice) in zip(CORPUS, out):
            with self.subTest(text=text):
                self.assertEqual(shown, scrub(text))
                self.assertEqual(twice, shown)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
