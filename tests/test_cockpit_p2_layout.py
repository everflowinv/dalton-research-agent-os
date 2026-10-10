"""P2 fixes from the 2026-10-07 cockpit inspection: layout, interaction, wording.

BUG-04 model detail inside the company drawer, BUG-08 data-source rows,
BUG-09 the "需要你处理" cards, BUG-10 the weekly brief page.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core import needs_human


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"
BRIEF = ROOT / "src" / "dalton_core" / "cockpit_weekly_brief.html"

# A tiny DOM: enough for the functions under test to build and replace nodes.
FAKE_DOM = r"""
class El{constructor(tag,text,cls){this.tag=tag;this.textContent=text==null?"":String(text);this.className=cls||"";this.children=[];this.dataset={};this.isConnected=true}
  append(...xs){this.children.push(...xs)}replaceChildren(...xs){this.children=[...xs]}
  querySelectorAll(sel){const cls=sel.replace(/^\./,"");const out=[];const walk=e=>{(e.children||[]).forEach(c=>{if(c&&String(c.className).split(" ").includes(cls))out.push(c);walk(c)})};walk(this);return out}}
function node(tag,text,cls){return new El(tag,text,cls)}
function texts(e){return [e.textContent,...(e.children||[]).flatMap(texts)].filter(Boolean)}
"""


def _function(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin) + len(end)]


def _node(script: str) -> str:
    return subprocess.run(["node", "-e", script], text=True, capture_output=True,
                          check=True).stdout.strip()


class CompanyDrawerModelTabTests(unittest.TestCase):
    """BUG-04: 查看模型详情 no longer replaces the drawer and its seven tabs."""

    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_the_model_tab_does_not_open_a_separate_reader(self):
        block = _function(self.source, "function modelBlock(c)", "return d}")
        self.assertNotIn("openModel(", block)
        self.assertIn("inlineModelDetail(c.company_ref)", block)
        # The tab switch loads it; the other tabs are still in the drawer.
        self.assertIn('panels[i].querySelectorAll(".model-inline").forEach(x=>x.loadDetail&&x.loadDetail())',
                      self.source)
        self.assertIn("async function openModel(company)", self.source)

    def test_detail_loads_once_per_company_and_renders_inside_the_panel(self):
        code = _function(self.source, "const MODEL_DETAIL_TTL_MS", "\n  return box}")
        script = FAKE_DOM + r"""
let calls=0;async function getJson(url){calls++;return {company:"ACN",version:3,table:"收入 | 100",invariants:{},readiness:{},history:[]}}
function showProblem(box,e){box.replaceChildren(node("div","失败","gap"))}
function versionLabel(v){return `版本 ${v}`}function fmtDate(x){return x||""}function displayText(x){return x}
function invariantRefusal(){return null}function readinessGroup(){return null}function technicalDetails(){return null}
function readerClose(){return node("button","关闭")}
""" + code + "\n" + _function(self.source, "function renderModelBody(sheet,m,standalone)", "\n    sheet.append(h)}}") + r"""
(async()=>{const a=inlineModelDetail("company:ACN"),b=inlineModelDetail("company:ACN");
  a.loadDetail();a.loadDetail();await new Promise(r=>setTimeout(r,0));b.loadDetail();await new Promise(r=>setTimeout(r,0));
  console.log(JSON.stringify({calls,state:a.dataset.state,texts:texts(a),b:b.dataset.state}))})();
"""
        out = json.loads(_node(script))
        self.assertEqual(out["calls"], 1)
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["b"], "done")
        self.assertIn("模型详情", out["texts"])
        self.assertIn("收入 | 100", out["texts"])
        self.assertNotIn("关闭", out["texts"])


class DataSourceRowTests(unittest.TestCase):
    """BUG-08: each line of a source row is its own line."""

    def test_lane_small_is_a_block_in_muted_colour(self):
        source = HTML.read_text(encoding="utf-8")
        rule = re.search(r"\.lane small\{([^}]*)\}", source).group(1)
        self.assertIn("display:block", rule)
        self.assertIn("color:var(--muted)", rule)
        self.assertIn("margin-top", rule)


class NeedsHumanWordingTests(unittest.TestCase):
    """BUG-09: real places, owner units, Chinese reasons, company names."""

    def test_units(self):
        self.assertEqual(needs_human._gigabytes(3_758_735_605), "3.76 GB")
        self.assertEqual(needs_human._gigabytes(4_000_000_000), "4.00 GB")
        self.assertEqual(needs_human._duration(604_800), "7 天")
        self.assertEqual(needs_human._duration(90_000), "1 天 1 小时")
        self.assertEqual(needs_human._duration(7_200), "2 小时")

    def test_times_are_in_the_owners_zone(self):
        self.assertEqual(needs_human._local_time("2026-10-08T02:46:37.439233+00:00"),
                         "2026年10月8日 10:46")
        self.assertEqual(needs_human._local_time("not a time"), "not a time")

    def test_exception_names_become_sentences(self):
        self.assertEqual(
            needs_human.fetch_reason_label(
                "fetch outcome retryable; PublicTransportResponseTooLarge: response "
                "Content-Length exceeds the configured limit"),
            "目标文件过大，超出单次下载上限")
        self.assertEqual(
            needs_human.fetch_reason_label(
                "fetch outcome retryable; connector transport exceeded the authority deadline"),
            "连接超时，稍后自动重试")
        self.assertEqual(needs_human.fetch_reason_label("something new"),
                         "获取失败（原始原因见技术详情）")

    def test_no_item_points_at_a_page_that_does_not_exist(self):
        source = Path(needs_human.__file__).read_text(encoding="utf-8")
        for stale in ('where="运行状态"', 'where="来源"', 'where="模型"', 'where="待办"',
                      'where="研究进度"', 'where="研究目标"', "打开「待办」页", "「模型」页",
                      "打开「来源」页"):
            self.assertNotIn(stale, source)
        nav = re.findall(r'data-view="(\w+)"><span class="ic">.*?</span>([^<]+)<',
                         HTML.read_text(encoding="utf-8"))
        labels = dict(nav)
        self.assertEqual(labels["approve"], needs_human.APPROVE_VIEW_LABEL)
        self.assertEqual(labels["goal"], needs_human.GOAL_VIEW_LABEL)
        self.assertEqual(labels["workspace"], needs_human.WORKSPACE_VIEW_LABEL)

    def test_capped_fetch_names_the_company_not_its_cik(self):
        import sqlite3
        from datetime import timedelta
        from dalton_core.coverage_mission import FETCH_FAILURE_CAP

        core = sqlite3.connect(":memory:")
        self.addCleanup(core.close)
        core.row_factory = sqlite3.Row
        core.executescript(
            "CREATE TABLE coverage_mission_acquisition_attempts(attempt_ref TEXT, source_ref TEXT,"
            " host TEXT, document_ref TEXT, outcome TEXT, created_at TEXT);"
            "CREATE TABLE coverage_mission_discovered_documents(source_ref TEXT, document_ref TEXT,"
            " company_ref TEXT, failure_reason TEXT, updated_at TEXT);"
            "CREATE TABLE coverage_mission_versions(mission_version_id TEXT, record_json TEXT);"
            "CREATE TABLE coverage_mission_pointer(mission_version_id TEXT, updated_at TEXT);")
        t0 = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
        doc = "public-web-url:sha256:" + "a" * 64
        for n in range(FETCH_FAILURE_CAP):
            core.execute("INSERT INTO coverage_mission_acquisition_attempts VALUES(?,?,?,?,?,?)",
                         (f"a{n}", "source:web-search", "www.cognizant.com", doc,
                          "transport_retryable", (t0 + timedelta(minutes=n)).isoformat()))
        cik = "company:sec-cik:0001058290"
        core.execute("INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?)",
                     ("source:web-search", doc, cik,
                      "fetch outcome retryable; connector transport exceeded the authority deadline",
                      t0.isoformat()))
        core.execute("INSERT INTO coverage_mission_versions VALUES(?,?)", ("v1", json.dumps(
            {"universe": [{"company_ref": cik, "ticker": "CTSH", "name": "Cognizant"}]})))
        core.execute("INSERT INTO coverage_mission_pointer VALUES(?,?)", ("v1", t0.isoformat()))
        items = needs_human.capped_fetches(core, now=t0 + timedelta(hours=1))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["title"].startswith("CTSH · Cognizant 的一份资料"))
        self.assertNotIn("0001058290", item["title"])
        self.assertIn("连接超时，稍后自动重试", item["why_blocked"])
        self.assertNotIn("authority deadline", item["why_blocked"])
        self.assertEqual(item["where"], "资料来源")
        self.assertEqual((item["where_view"], item["where_open"]), ("goal", "sources"))

    def test_the_page_opens_the_named_dialog_or_view(self):
        source = HTML.read_text(encoding="utf-8")
        code = _function(source, "const WHERE_OPENERS", 'return node("span",label)}')
        script = FAKE_DOM + r"""
let opened=[],shown=[];function openSources(){opened.push("sources")}function openModels(){opened.push("models")}
function show(v){shown.push(v)}const document={querySelector:s=>s.includes('"approve"')||s.includes('"goal"')};
""" + code + r"""
const a=whereNode({where:"资料来源",where_view:"goal",where_open:"sources"});a.onclick();
const b=whereNode({where:"待办审批",where_view:"approve"});b.onclick();
const c=whereNode({where:"交给技术管理员在本机处理（命令见技术详情）"});
console.log(JSON.stringify({a:a.textContent,b:b.textContent,c:c.textContent,tags:[a.tag,b.tag,c.tag],opened,shown}));
"""
        out = json.loads(_node(script))
        self.assertEqual(out["a"], "打开「资料来源」")
        self.assertEqual(out["b"], "待办审批 页")
        self.assertEqual(out["c"], "交给技术管理员在本机处理（命令见技术详情）")
        self.assertEqual(out["tags"], ["button", "button", "span"])
        self.assertEqual((out["opened"], out["shown"]), (["sources"], ["approve"]))


class WeeklyBriefPageTests(unittest.TestCase):
    """BUG-10: product name, channel names, owner-zone timestamps."""

    @classmethod
    def setUpClass(cls):
        cls.source = BRIEF.read_text(encoding="utf-8")

    def test_back_link_uses_the_product_name(self):
        self.assertIn("← 返回研究设置 · 每周简报", self.source)   # 2026-10-09 (W01): back to where it lives
        self.assertNotIn("驾驶舱", self.source)
        self.assertIn("Dalton 投研工作台", HTML.read_text(encoding="utf-8"))

    def test_channels_times_and_plans_are_readable(self):
        code = _function(self.source, 'const OWNER_TZ="Asia/Shanghai";', "(n/1048576).toFixed(2)} MB`}")
        out = json.loads(_node(code + r"""
console.log(JSON.stringify([fmtTime("2026-10-01T11:00:00.000000+00:00"),
  destLabel("openclaw:discord:default:channel:1481256083589697566"),
  destLabel("cockpit:weekly-brief:us-it-services"),planLabel("weekly-brief-plan:us-it-services:v4"),
  sizeLabel(20480)]));"""))
        self.assertEqual(out, ["2026年10月1日 19:00", "Discord 默认频道（尾号 7566）",
                               "只在工作台保存", "第 4 版计划", "20.0 KB"])
        self.assertIn('["scheduled_for","计划时间",fmtTime]', self.source)
        self.assertIn('["destination_ref","发送到",destLabel]', self.source)


if __name__ == "__main__":
    unittest.main()
