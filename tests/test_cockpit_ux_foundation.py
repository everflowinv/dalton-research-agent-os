"""Analyst UX review 2026-10-08, batch a: the interaction foundation.

docs/reports/cockpit-analyst-ux-review-2026-10-08.md section 3 (G03/G04/G05/
G07/G08/G10/G11/G12), 1.1 and 1.5: drafts and selections that survive a
re-read, a page switch and a refresh; the page, company, tab and filter in the
address; a per-page scroll position; a layout that fits 768/1024/1280; readable
semantic colours; real buttons and a focus ring that only holds what can be
seen; persistent labels and inline validation; a nested reader that returns to
its company; and a to-do card that either has a way to finish or says there is
none.  Page functions run under node; the layout and the click-throughs run in a
headless Chromium when one is installed (skipped otherwise).
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from dalton_core import needs_human


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"
BRIEF = ROOT / "src" / "dalton_core" / "cockpit_weekly_brief.html"


def _function(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin) + len(end)]


def _node(script: str) -> str:
    return subprocess.run(["node", "-e", script], text=True, capture_output=True,
                          check=True, timeout=60).stdout.strip()


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")
        cls.css = cls.source.split("<style>", 1)[1].split("</style>", 1)[0]
        cls.foundation = cls.css[cls.css.index("2026-10-08 analyst UX review"):]

    def last_rule(self, selector: str) -> str:
        found = re.findall(re.escape(selector) + r"\{([^}]*)\}", self.foundation)
        self.assertTrue(found, selector)
        return found[-1]

    def colour(self, rule: str, prop: str) -> str:
        match = re.search(r"(?:^|;)" + prop + r":#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", rule)
        self.assertIsNotNone(match, (prop, rule))
        value = match.group(1)
        return "#" + (value if len(value) == 6 else "".join(c * 2 for c in value))


class LayoutCssTests(SourceTests):
    """G05: the main column may shrink; only tables and the matrix scroll sideways."""

    def test_main_column_and_grids_can_shrink(self):
        self.assertIn(".app{grid-template-columns:232px minmax(0,1fr)}main{min-width:0}", self.foundation)
        self.assertIn(".grid.c4,.lanes{grid-template-columns:repeat(4,minmax(0,1fr))}", self.foundation)
        self.assertIn("@media(max-width:1280px)", self.foundation)
        # The stage matrix keeps its own horizontal scroll.
        self.assertIn(".journey{overflow:auto;", self.css)


class ContrastTests(SourceTests):
    """G07: semantic colours on the light theme, at least 4.5:1."""

    def test_danger_button_text_is_readable(self):
        rule = self.last_rule("button.danger,.danger")
        self.assertGreaterEqual(contrast(self.colour(rule, "color"), self.colour(rule, "background")), 4.5)
        # The light theme's later rule now wins over the dark-era button.danger.
        self.assertGreater(self.css.rindex("button.danger,.danger{"), self.css.index("button.danger{background:rgba"))

    def test_future_state_and_small_text_is_readable(self):
        dim = re.search(r":root\{--dim:(#[0-9a-f]{6})\}", self.foundation).group(1)
        for surface in ("#ffffff", "#f8fafc", "#f1faf5", "#f7f9fc"):
            self.assertGreaterEqual(contrast(dim, surface), 4.5, surface)
        self.assertIn(".figp{opacity:1;color:var(--muted)}", self.foundation)
        self.assertIn(".checks li{color:inherit}", self.foundation)

    def test_other_light_on_light_leftovers_are_fixed(self):
        for selector, surface in ((".stat.alarm .n", "#f8fafc"), (".mkt .prov", "#fff6e5"),
                                  (".tag.spoken", "#fbf3e2"), (".draft .add", "#f8fafc"),
                                  (".draft .rm", "#f8fafc")):
            rule = self.last_rule(selector)
            self.assertGreaterEqual(contrast(self.colour(rule, "color"), surface), 4.5, selector)
        badge = self.last_rule(".nav-item .badge")
        self.assertGreaterEqual(contrast(self.colour(badge, "color"), self.colour(badge, "background")), 4.5)
        primary = self.last_rule("button.primary")
        for stop in re.findall(r"#[0-9a-f]{6}", primary.split("color:")[0]):
            self.assertGreaterEqual(contrast("#ffffff", stop), 4.5, stop)

    def test_draft_additions_and_removals_are_named_not_only_coloured(self):
        self.assertIn('function diffItem(text,kind,word)', self.source)
        for call in ('diffItem(q,"add","新增")', 'diffItem(q,"rm","删除")',
                     'diffItem(x.ticker||"一家公司","rm","移除")', '"bad","未能识别"'):
            self.assertIn(call, self.source)
        # The question text itself is passed unchanged, so its reviewed
        # translation (keyed by the hash of the exact string) still applies.
        self.assertIn('li.append(rawNode("span",word,"diff-mark"),node("span",text))', self.source)


class MarkupTests(SourceTests):
    """G08 / G10: real buttons and persistent labels."""

    def test_log_filters_are_pressed_buttons(self):
        chips = self.source[self.source.index('id="log-filters"'):self.source.index('id="log-status"')]
        self.assertIn('role="group"', chips)
        self.assertEqual(chips.count("<button type=\"button\" class=\"chip"), 7)
        self.assertEqual(chips.count('aria-pressed="true"'), 1)
        self.assertNotIn("<span class=\"chip", chips)

    def test_main_inputs_have_labels_that_stay(self):
        for field in ("goal-input", "steer-input", "ask-input", "lever-reason", "lever-hours", "ftpl-reason",
                      "ftpl-file", "ftpl-slug", "ftpl-name", "ftpl-desc", "ftpl-sheet-valuation",
                      "ftpl-sheet-financials", "ftpl-sheet-driver", "cov-add", "cov-tier", "cov-reason"):
            self.assertRegex(self.source, rf'<label class="field-label" for="{field}">[^<]+</label>', field)
        self.assertIn('aria-labelledby="cov-remove-label"', self.source)

    def test_tiles_are_buttons_and_tabs_name_their_panels(self):
        panel = _function(self.source, "function panel(n,l,s,go,onclick,alarm)", "return d}")
        self.assertIn('node("button",null,"stat link"', panel)
        self.assertIn('b.setAttribute("aria-controls",`company-panel-${i}`)', self.source)
        self.assertIn('p.setAttribute("aria-labelledby",b.id)', self.source)

    def test_goal_draft_is_not_wiped_by_the_overview_reread(self):
        active = self.source.split('$("goal-submit").textContent="整理成研究目标"', 1)[1]
        active = active[:active.index("const g=o.goal;")]
        # Only the blank-workspace setup draft is cleared once a mission is live.
        self.assertIn('shownDraft.dataset.draftKind==="setup"', active)
        self.assertIn('restoreSavedDraft("goal",{setup:false})', active)


DOM = r"""
class El{constructor(tag,attrs={},children=[]){this.tagName=tag.toUpperCase();this.attrs=attrs;this.children=[];this.parentElement=null;
  this.hidden=!!attrs.hidden;this.open=!!attrs.open;this.disabled=!!attrs.disabled;this.type=attrs.type||"";
  this.tabIndex=attrs.tabindex!=null?Number(attrs.tabindex):(["BUTTON","INPUT","SELECT","TEXTAREA","A","SUMMARY"].includes(this.tagName)?0:-1);
  this.value=attrs.value||"";this.checked=!!attrs.checked;this.name=attrs.name||"";children.forEach(c=>this.append(c))}
  append(c){c.parentElement=this;this.children.push(c)}
  getAttribute(k){return k==="tabindex"&&this.attrs.tabindex!=null?String(this.attrs.tabindex):(this.attrs[k]??null)}
  all(){return this.children.flatMap(c=>[c,...c.all()])}
  querySelectorAll(sel){const tags=sel.split(",").map(s=>s.trim().replace(/\[.*$/,"").toUpperCase());
    return this.all().filter(e=>tags.includes(e.tagName)||(sel.includes("[tabindex]")&&e.attrs.tabindex!=null)||(sel.includes("a[href]")&&e.tagName==="A"&&e.attrs.href))}}
const h=(t,a,...c)=>new El(t,a||{},c);
"""


class PageFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_focus_ring_holds_only_what_can_be_seen(self):
        code = _function(self.source, 'const READER_FOCUSABLE=', "return at===-1||at===items.length-1?items[0]:null}")
        out = json.loads(_node(DOM + code + r"""
const close=h("button",{id:"close"}),tab1=h("button",{}),tab2=h("button",{tabindex:-1}),
  visible=h("a",{href:"#x"}),hiddenBtn=h("button",{}),foldSummary=h("summary",{}),foldInput=h("input",{}),
  disabled=h("button",{disabled:true}),last=h("button",{id:"last"});
const root=h("div",{},close,h("div",{},tab1,tab2),h("div",{},visible,h("details",{},foldSummary,foldInput),disabled,last),h("div",{hidden:true},hiddenBtn));
const items=readerFocusables(root);
console.log(JSON.stringify({n:items.length,first:items[0]===close,last:items[items.length-1]===last,
  hiddenOut:!items.includes(hiddenBtn),tabOut:!items.includes(tab2),foldInputOut:!items.includes(foldInput),summaryIn:items.includes(foldSummary),linkIn:items.includes(visible),
  shiftFromClose:readerTab(root,close,true)===last,tabFromLast:readerTab(root,last,false)===close,strayBack:readerTab(root,root,true)===last,middle:readerTab(root,visible,false)}));
"""))
        self.assertEqual(out["n"], 5)
        for key in ("first", "last", "hiddenOut", "tabOut", "foldInputOut", "summaryIn", "linkIn",
                    "shiftFromClose", "tabFromLast", "strayBack"):
            self.assertTrue(out[key], key)
        self.assertIsNone(out["middle"])

    def test_routes_round_trip(self):
        code = _function(self.source, "const COMPANY_TABS=", "try{history[replace?\"replaceState\":\"pushState\"]")
        out = json.loads(_node(r"""const titles={workspace:1,goal:1,steer:1,log:1,ask:1,approve:1};""" + code + r"""}catch(_){}}
const cases=[{view:"steer"},{view:"log",filter:"claim"},{view:"goal",company:"company:sec-cik:1467373",tab:"scenarios"},{view:"approve",company:"company:x/y",tab:null}];
console.log(JSON.stringify({hashes:cases.map(routeHash),back:cases.map(c=>parseRoute(routeHash(c))),
  junk:parseRoute("#/nowhere/company"),empty:parseRoute(""),badTab:parseRoute("#/goal/company/a/zzz")}));
"""))
        self.assertEqual(out["hashes"], ["#/steer", "#/log?filter=claim",
                                         "#/goal/company/company%3Asec-cik%3A1467373/scenarios",
                                         "#/approve/company/company%3Ax%2Fy"])
        self.assertEqual(out["back"][1]["filter"], "claim")
        self.assertEqual((out["back"][2]["company"], out["back"][2]["tab"]), ("company:sec-cik:1467373", "scenarios"))
        self.assertEqual(out["back"][3]["company"], "company:x/y")
        self.assertEqual(out["junk"]["view"], "goal")
        self.assertIsNone(out["junk"]["company"])
        self.assertEqual(out["empty"]["view"], "goal")
        self.assertIsNone(out["badTab"]["tab"])

    def test_drafts_are_kept_per_environment_and_expire(self):
        code = _function(self.source, "const DRAFT_NS=", "function draftDrop(...keys){keys.forEach(key=>draftSet(key,null))}")
        out = json.loads(_node(r"""
const store=new Map();const localStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
let location={host:"127.0.0.1:8793",pathname:"/"};""" + code + r"""
draftSet("goal:draft",{draft_id:"d"});draftSet("field:x","");const a=draftGet("goal:draft"),keys=[...store.keys()];
location={host:"127.0.0.1:8795",pathname:"/"};
const other=(()=>{const NS=`dalton-cockpit:${location.host}${location.pathname}:`;return store.get(NS+"goal:draft")||null})();
store.set(keys[0],JSON.stringify({at:Date.now()-15*24*3600*1000,value:{old:true}}));const expired=draftGet("goal:draft");
draftSet("k",[1]);draftDrop("k");
console.log(JSON.stringify({a,keys,other,expired,left:[...store.keys()]}));
"""))
        self.assertEqual(out["a"], {"draft_id": "d"})
        self.assertEqual(out["keys"], ["dalton-cockpit:127.0.0.1:8793/:goal:draft"])
        self.assertIsNone(out["other"])
        self.assertIsNone(out["expired"])
        self.assertEqual(out["left"], [])

    def test_form_snapshot_restores_by_position(self):
        code = _function(self.source, "function formSnapshot(root)", "else el.value=snap[i]??\"\"});return true}")
        out = json.loads(_node(DOM + code + r"""
const make=()=>h("div",{},h("input",{value:"7"}),h("input",{type:"checkbox"}),h("select",{value:"A"}),h("textarea",{value:""}));
const a=make();const [i,c,s,t]=a.querySelectorAll("input,select,textarea");i.value="12";c.checked=true;s.value="B";t.value="理由";
const snap=formSnapshot(a),b=make(),ok=formRestore(b,snap),short=formRestore(h("div",{},h("input",{})),snap);
console.log(JSON.stringify({snap,ok,short,values:b.querySelectorAll("input,select,textarea").map(e=>e.type==="checkbox"?e.checked:e.value)}));
"""))
        self.assertEqual(out["snap"], ["12", True, "B", "理由"])
        self.assertTrue(out["ok"])
        self.assertFalse(out["short"])
        self.assertEqual(out["values"], ["12", True, "B", "理由"])

    def test_every_needs_human_entry_mode_renders_an_action_or_a_plain_label(self):
        code = _function(self.source, "const WHERE_OPENERS", 'return node("span",label)}')
        out = json.loads(_node(r"""
class N{constructor(tag,text,cls){this.tag=tag;this.textContent=text||"";this.className=cls||""}}
const node=(t,x,c)=>new N(t,x,c);let calls=[];
function openSources(){calls.push("sources")}function openModels(){calls.push("models")}
function show(v){calls.push("show:"+v)}function jumpTo(){calls.push("jump")}function openCompanyByRef(r){calls.push("company:"+r)}
const $=id=>({focus(){calls.push("focus:"+id)}});
const document={querySelector:s=>s.includes('"steer"')};
""" + code + r"""
const make=e=>whereNode({where:"x",entry:e});
const nodes=[make({mode:"dialog",open:"sources",label:"打开「资料来源」"}),make({mode:"approvals",label:"到下方「待决事项」处理"}),
  make({mode:"company",company_ref:"company:1",label:"打开这家公司的研究详情"}),make({mode:"goal_input",label:"去写研究目标"}),
  make({mode:"view",view:"steer",label:"打开「策略指引」页"}),make({mode:"link",href:"https://www.sec.gov/x",label:"打开 SEC 上的 8-K 原文"}),
  make({mode:"link",href:"javascript:alert(1)",label:"bad"}),make({mode:"none",label:"交给技术管理员"})];
nodes.forEach(n=>{if(n.onclick)n.onclick()});
console.log(JSON.stringify({tags:nodes.map(n=>n.tag),texts:nodes.map(n=>n.textContent),href:nodes[5].href,rel:nodes[5].rel,calls}));
"""))
        self.assertEqual(out["tags"], ["button", "button", "button", "button", "button", "a", "span", "span"])
        self.assertEqual(out["href"], "https://www.sec.gov/x")
        self.assertEqual(out["rel"], "noopener noreferrer")
        self.assertEqual(out["calls"], ["sources", "show:approve", "jump", "focus:approvals-card",
                                        "company:company:1", "show:goal", "jump", "focus:goal-input",
                                        "show:steer"])

    def test_weekly_brief_checks_the_plan_before_sending(self):
        source = BRIEF.read_text(encoding="utf-8")
        code = _function(source, "function intField(raw,min,max)", "  return out}")
        out = json.loads(_node(code + r"""
console.log(JSON.stringify([planProblems({hour:"99",minute:"0",timezone:"America/New_York"}),
  planProblems({hour:"7",minute:"",timezone:"Mars/Base"}),planProblems({hour:"23",minute:"59",timezone:"Asia/Shanghai"}),
  planProblems({hour:"7.5",minute:"-1",timezone:""})]));
"""))
        self.assertEqual(set(out[0]), {"hour"})
        self.assertEqual(set(out[1]), {"minute", "timezone"})
        self.assertEqual(out[2], {})
        self.assertEqual(set(out[3]), {"hour", "minute", "timezone"})
        self.assertIn("planProblems({hour:$(\"hour\").value", source)
        self.assertIn("连接失败，请检查网络后重试。", source)


class CompletionEntryTests(unittest.TestCase):
    """Review 1.5: each 「需要你处理」 card has a way to finish on the page, or says plainly there is none."""

    def item(self, kind, **place):
        return needs_human._item(kind if kind in needs_human.URGENCY else "governance_record",
                                 ref="r", at="2026-10-08T00:00:00+00:00", title="t", why="w",
                                 action="a", consequence="c", **place) | {"kind": kind}

    def test_modes(self):
        nh = needs_human
        cases = {
            "sources": self.item("governance_record", where=nh.SOURCES_DIALOG_LABEL,
                                 where_view=nh.GOAL_VIEW, where_open=nh.SOURCES_DIALOG),
            "models": self.item("provider_failure", where=nh.MODELS_DIALOG_LABEL,
                                where_view=nh.GOAL_VIEW, where_open=nh.MODELS_DIALOG),
            "approve": self.item("deep_insight_gate", where=nh.APPROVE_VIEW_LABEL, where_view=nh.APPROVE_VIEW),
            "company": self.item("data_foundation_stalled", where=nh.GOAL_VIEW_LABEL, where_view=nh.GOAL_VIEW,
                                 detail={"company_ref": "company:sec-cik:1"}),
            "goal": self.item("no_active_mission", where=nh.WORKSPACE_VIEW_LABEL, where_view=nh.WORKSPACE_VIEW),
            "admin": self.item("governance_record", where=nh.TECHNICAL_ADMIN_WHERE, actionable=False),
            "quiet": self.item("fetch_failure_capped", where=nh.NO_ACTION_WHERE),
            "login": self.item("connector_login_expired", where="AlphaEngine（本机）"),
            "sec": self.item("earnings_release_unread", where="SEC 上的这份 8-K 原文（链接见详情）",
                             detail={"exhibit_url": "https://www.sec.gov/Archives/x.htm"}),
        }
        entries = {k: nh.completion_entry(v) for k, v in cases.items()}
        self.assertEqual(entries["sources"]["mode"], "dialog")
        self.assertIn("没有批准、连接或补传资料的按钮", entries["sources"]["note"])
        self.assertEqual((entries["models"]["mode"], entries["models"]["open"]), ("dialog", "models"))
        self.assertNotIn("note", entries["models"])
        self.assertEqual(entries["approve"]["mode"], "approvals")
        self.assertEqual((entries["company"]["mode"], entries["company"]["company_ref"]),
                         ("company", "company:sec-cik:1"))
        self.assertIn("暂时没有补传资料", entries["company"]["note"])
        # D07: the empty environment goes to the goal input, not to 研究环境.
        self.assertEqual(entries["goal"]["mode"], "goal_input")
        for key in ("admin", "login"):
            self.assertEqual(entries[key]["mode"], "none", key)
            self.assertTrue(entries[key]["note"], key)
        # 2026-10-09: a card with nothing to press can be acknowledged; what
        # the page could do with it before stays as its secondary entry.
        self.assertEqual(entries["quiet"]["mode"], "acknowledge")
        self.assertTrue(entries["quiet"]["secondary_note"])
        self.assertEqual(entries["sec"]["mode"], "acknowledge")
        self.assertEqual(entries["sec"]["secondary"]["mode"], "link")
        self.assertTrue(entries["sec"]["secondary"]["href"].startswith("https://"))

    def test_collect_attaches_an_entry_to_every_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            governance = Path(tmp) / needs_human.GOVERNANCE_DIR_NAME
            governance.mkdir()
            (governance / "x.json").write_text(json.dumps({
                "id": "gov:x", "capability_id": "connector:example", "status": "proposed",
                "allowed_permissions": {"risk_class": "low"}}), encoding="utf-8")
            listed = needs_human.collect(state_dir=tmp)
        self.assertTrue(listed["items"])
        for row in listed["items"]:
            entry = row["entry"]
            self.assertIn(entry["mode"], {"dialog", "approvals", "company", "goal_input", "view", "link", "none"})
            # A card with no in-page action always says so.
            if entry["mode"] == "none":
                self.assertTrue(entry.get("note"))


SHELLS = sorted(glob.glob(os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
SHELL = os.environ.get("DALTON_HEADLESS_SHELL") or (SHELLS[-1] if SHELLS else "")

COMPANIES = [{
    "ticker": t, "name": f"{t} Corporation Holdings International", "company_ref": f"company:sec-cik:{1000 + i}",
    "stage_ref": "initial_screen", "stage_status_label": "等待确认初步研究结果", "note": "资料底座缺电话会纪要",
    "progress": {"found": 120, "read": 80}, "claims": {"total": 3, "latest": []}, "figures": {"total": 0, "latest": []},
    "checklist": [], "invariants": {}, "events": {"total": 0}} for i, t in enumerate(
        ["ACN", "CTSH", "EPAM", "IBM", "DXC", "AMZN", "GOOGL", "META", "MSFT"])]
OVERVIEW = {
    "workspace": {"mode": "isolated", "name": "美国 IT 服务研究"}, "state": "active", "as_of": "2026-10-08T09:00:00Z",
    "goal": {"title": "美国 IT 服务公司在 AI 浪潮下的定价权变化研究", "objective": "弄清楚定价权变化。" * 8, "version": 7,
             "published_at": "2026-10-01T00:00:00Z", "research_questions": ["问题一"], "sources": [], "history": []},
    "plan": None, "companies": COMPANIES,
    "totals": {"theses": 4, "found": 2400, "read": 1800, "waiting": 600, "claims": 9000, "claims_today": 12},
    "budgets": {"model_calls": {"used": 120, "cost_usd": "3.21"}},
    "ops": {"lanes": {"counts": {"running": 3}, "total": 27, "note": "27 项任务中 3 项正在运行", "waiting_on_you": 2},
            "gaps": {"headline": 1345, "note": "缺口"}, "failures": {"headline": 27, "note": "受阻"},
            "acceptance": {"available": True, "published": 4, "note": "上周", "week": "2026-W40"}},
    "activity": {"lanes": [{"label": "资料检索与采集（SEC、AlphaEngine、网页）", "status": "running", "detail": "", "note": "运行中"}] * 8,
                 "running": [], "ticks": None, "service_state": "running", "last_tick_at": "2026-10-08T08:59:00Z"},
    "model_available": {"available": True}}
ROUTES = {
    "/v1/cockpit/overview": OVERVIEW,
    "/v1/cockpit/levers": {"companies": [{"label": c["ticker"], "company_ref": c["company_ref"], "coverage_tier": "B"} for c in COMPANIES],
                           "lanes": [], "lines": [], "receipts": [], "emergency": False},
    "/v1/cockpit/approvals": {"count": 0, "as_of": "2026-10-08T09:00:00Z", "items": []},
    "/v1/cockpit/needs-human": {"count": 0, "items": []},
    "/v1/cockpit/log": {"events": [], "as_of": "2026-10-08T09:00:00Z"},
    "/v1/cockpit/history": {"items": [], "drafts": []},
    "/v1/cockpit/ui-texts": {"texts": {}, "same": []},
    "/v1/cockpit/research": {"products": []},
    "/v1/cockpit/claims": {"filters": {"canonical_only": True}, "companies": [], "vocabulary": {}, "indexed": True,
                           "total": 1, "items": [{"statement": "收入同比增长 7%", "company": "ACN", "at": "2026-10-08T00:00:00Z", "indexed": True}],
                           "next_cursor": None},
}
STUB = ("<script>window.__R=%s;window.fetch=async u=>{const v=window.__R[String(u).split('?')[0]]||{};"
        "return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(v))}};</script>")


@unittest.skipUnless(SHELL and os.path.exists(SHELL), "no headless Chromium installed")
class HeadlessPageTests(unittest.TestCase):
    """The page itself, with its reads answered from fixtures (no network)."""

    def run_page(self, width: int, script: str) -> dict:
        page = HTML.read_text(encoding="utf-8").replace("<script>", STUB % json.dumps(ROUTES) + "<script>", 1)
        page = page.replace("</body>", "<script>window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message)));"
                            "const sleep=ms=>new Promise(r=>setTimeout(r,ms));(async()=>{const out={};try{await sleep(1200);"
                            + script + "}catch(e){out.exception=String(e)}out.errors=__errors;const p=document.createElement('pre');"
                            "p.id='__out';p.textContent=JSON.stringify(out);document.body.append(p)})();</script></body>")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.html"
            path.write_text(page, encoding="utf-8")
            dom = subprocess.run([SHELL, "--headless", "--no-sandbox", "--disable-gpu", f"--user-data-dir={tmp}/profile",
                                  f"--window-size={width},900", "--virtual-time-budget=15000", "--dump-dom", path.as_uri()],
                                 capture_output=True, text=True, timeout=120).stdout
        match = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
        self.assertIsNotNone(match, dom[-1500:])
        out = json.loads(html.unescape(match.group(1)))
        self.assertNotIn("exception", out, out)
        self.assertEqual(out["errors"], [])
        return out

    def test_no_page_wider_than_the_window_at_768_1024_1280(self):
        script = ("out.widths={};for(const v of ['goal','steer','log','ask','approve','workspace']){show(v);await sleep(150);"
                  "out.widths[v]=document.documentElement.scrollWidth}out.vw=document.documentElement.clientWidth;")
        for width in (768, 1024, 1280):
            out = self.run_page(width, script)
            for view, measured in out["widths"].items():
                self.assertLessEqual(measured, out["vw"], (width, view))

    def test_state_address_scroll_and_nested_reader(self):
        out = self.run_page(1280, r"""
renderGoalDraft({draft_id:"d1",draft_hash:"h",draft:{summary:"草稿",title:"T",objective:"O",research_questions:["q"],subtasks:[],suggested_companies:[]}});
await loadOverview(true);await sleep(200);out.draft=$("goal-draft").childNodes.length;
show("steer");await sleep(200);const box=$("cov-remove").querySelector("input");box.checked=true;box.onchange();await loadLevers(true);await sleep(100);
out.ticked=$("cov-remove").querySelectorAll("input:checked").length;out.steerHash=location.hash;
show("goal");await sleep(100);window.scrollTo(0,400);saveViewScroll("goal");show("steer");await sleep(50);out.steerTop=window.scrollY;show("goal");await sleep(50);out.goalBack=window.scrollY;
document.querySelector(".company-launch").click();await sleep(150);document.querySelectorAll(".detail-tab")[6].click();await sleep(50);out.companyHash=location.hash;
[...document.querySelectorAll("#reader-sheet button")].find(b=>b.textContent==="查看公司研究结论").click();await sleep(200);
out.back=document.querySelector("#reader-sheet .close").textContent;document.querySelector("#reader-sheet .close").click();await sleep(150);
out.returned=[$("reader").classList.contains("show"),document.querySelector('.detail-tab[aria-selected="true"]').textContent];
const close=document.querySelector("#reader-sheet .close");close.focus();document.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",shiftKey:true,bubbles:true}));
out.wrapped=document.activeElement!==close&&document.activeElement.getClientRects().length>0;
document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));await sleep(200);out.closed=[$("reader").classList.contains("show"),location.hash];
""")
        self.assertEqual(out["draft"], 1)
        self.assertEqual(out["ticked"], 1)
        self.assertEqual(out["steerHash"], "#/steer")
        self.assertEqual((out["steerTop"], out["goalBack"]), (0, 400))
        self.assertEqual(out["companyHash"], "#/goal/company/company%3Asec-cik%3A1000/products")
        self.assertEqual(out["back"], "返回「ACN」研究详情")
        self.assertEqual(out["returned"], [True, "产物"])
        self.assertTrue(out["wrapped"])
        self.assertEqual(out["closed"], [False, "#/goal"])

    def test_unsaved_edits_are_named_and_a_failed_list_is_not_empty(self):
        out = self.run_page(1280, r"""
document.querySelector(".company-launch").click();await sleep(150);
unsavedChainEditors.add("budget");let asked=0;window.confirm=()=>{asked++;return false};
document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));await sleep(50);out.keptOpen=$("reader").classList.contains("show");out.asked=asked;
window.confirm=()=>true;closeReader();await sleep(50);out.closedAfterYes=!$("reader").classList.contains("show");
const real=window.fetch;window.fetch=async u=>String(u).includes("needs-human")?{ok:false,status:503,json:async()=>({message:"down"})}:real(u);
show("approve");await loadNeedsHuman(true);await sleep(50);window.fetch=real;
out.failed=[$("needs-human-card").hidden,$("needs-human-status").textContent];
""")
        self.assertTrue(out["keptOpen"])
        self.assertEqual(out["asked"], 1)
        self.assertTrue(out["closedAfterYes"])
        self.assertFalse(out["failed"][0])
        self.assertIn("这不代表没有待办", out["failed"][1])
        self.assertIn("重新读取", out["failed"][1])


if __name__ == "__main__":
    unittest.main()
