/* 公司研究: the company list and one standalone page per company with a
   table of contents (C01, G11).  Section order is the review's: 投资观点 →
   估值/回报 → 风险与下一验证事件 → 证据 → 财务模型 → 事件 → 情景与触发 →
   回报归因 → 研究报告.  Each section mounts the chart module's widget when one
   is registered for its slot and falls back to tables. */

import {V, OVERVIEW, NEEDS_HUMAN, companies, companyKey, companyName, findCompany, hasMission, isEstimate,
        lead, load, returnLeg, showText, untranslated, periodSteps, isInternalRef, refLabel} from "./common.js";
import {LABELS, METRIC_LABELS, sectionTitle, splitDebateTitle} from "../copy.js";

const SEC = LABELS.company_section, SCN = LABELS.scenario, REPORT = LABELS.report_state, ATTR = LABELS.attribution;
import {feedbackDisclosure} from "./feedback.js";
import {scenarioEditor, lastReceipt} from "./scenario_edit.js";
import {reflectionCard} from "./decision_basis.js";
import {comparisonSection} from "./company_compare.js";
import {companyHeader} from "./company_header.js";
import {evidenceSection as evidenceBody} from "./company_evidence.js";
import {modelSection as modelBody, exportFile} from "./company_model.js";
import {viewSection as viewBody} from "./company_view.js";
import {companyLabel, forecastBadges, glossTerm, honestState} from "./company_common.js";

export const SECTIONS = Object.freeze([
  {id: "view", title: SEC.view},
  {id: "valuation", title: SEC.valuation},
  {id: "risks", title: SEC.risks},
  {id: "evidence", title: SEC.evidence},
  {id: "model", title: SEC.model},
  {id: "events", title: SEC.events},
  {id: "scenarios", title: SEC.scenarios},
  {id: "returns", title: SEC.returns},
  {id: "reports", title: SEC.reports},
]);
const SECTION_IDS = SECTIONS.map(s => s.id);

/* ---------------------------------------------------------- list page -- */

async function renderList(root, ctx) {
  const {h, ui} = ctx;
  root.append(h("div", {class: "v2-page-head"}, h("h1", null, "公司研究")));
  const [ov, nh] = await Promise.all([load(ctx, OVERVIEW), load(ctx, NEEDS_HUMAN)]);
  if (!ctx.alive()) return;
  if (ov.error) { root.append(ui.errorBox(ov.error, () => ctx.navigate({}, {replace: true}))); return; }
  if (!hasMission(ov.value) || !companies(ov.value).length) {
    root.append(honestState(ctx, {
      state: "这个研究项目还没有研究公司。", impact: "公司列表、比较和公司页都还没有内容。",
      owner: "设定研究目标并确认覆盖名单后，研究系统会把公司加进来。", action: "先设定研究目标。",
      link: {href: ctx.href({page: "settings", filters: {focus: "goal"}}), text: "设定研究目标"}}));
    return;
  }
  const q = ctx.h("div", {class: "v2-field"});
  const search = ui.field({id: "company-filter", label: "筛选覆盖标的", help: "输入公司简称、全称或美股代码（Ticker）", control: "input", attrs: {type: "search", value: ctx.state.filters.q || ""}});
  search.input.addEventListener("input", () => {
    clearTimeout(search.timer);
    search.timer = setTimeout(() => ctx.setFilter("q", search.input.value.trim()), 250);
  });
  q.append(search.wrap);
  root.append(q, h("div", {id: "company-list"}));
  await drawList(ctx, ov.value, nh.value);
}

/* The list ranks with the one comparison rule (company_compare.js, O01). */
async function drawList(ctx, overview, needs, payload = null) {
  const holder = globalThis.document.getElementById("company-list");
  if (!holder) return null;
  const term = String(ctx.state.filters.q || "").toLowerCase();
  const filter = term ? (r => String(r.company || "").toLowerCase().includes(term) || String(r.ticker || "").toLowerCase().includes(term)) : null;
  return comparisonSection(ctx, holder, {overview, needs, filter, payload});
}

async function updateList(ctx) {
  if (!globalThis.document.getElementById("company-list")) return false;
  const [ov, nh] = await Promise.all([load(ctx, OVERVIEW), load(ctx, NEEDS_HUMAN)]);
  if (ov.error) return false;
  await drawList(ctx, ov.value, nh.value);
  return true;
}

/* ------------------------------------------------------- company page -- */

function sectionShell(ctx, id) {
  const {h} = ctx;
  const meta = SECTIONS.find(s => s.id === id);
  return h("section", {class: "v2-section", id: `sec-${id}`, "aria-labelledby": `sec-${id}-title`},
           h("h2", {id: `sec-${id}-title`, tabindex: "-1"}, meta.title),
           h("p", {class: "v2-loading"}, "正在读取…"));
}
function fill(section, ...nodes) {
  const heading = section.firstChild;
  section.replaceChildren(heading, ...nodes.flat().filter(Boolean));
}

function toc(ctx, key, current) {
  const {h, href} = ctx;
  // The shell styles .v2-toc--collapsible (collapse at narrow widths, G04).
  return h("nav", {class: "v2-toc v2-toc--collapsible", "aria-label": "本页目录"}, h("ol", null, SECTIONS.map(s => h("li", null,
    h("a", {href: href({page: "company", company: key, tab: s.id}), "data-sec": s.id, "aria-current": s.id === current ? "true" : null}, s.title)))));
}

function scrollToTab(ctx, tab, focus) {
  const doc = globalThis.document;
  const target = doc.getElementById(`sec-${tab}`);
  for (const a of doc.querySelectorAll(".v2-toc a")) {
    if (a.getAttribute("data-sec") === tab) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
  }
  if (!target) return;
  target.scrollIntoView({block: "start"});
  if (focus) target.querySelector("h2")?.focus({preventScroll: true});
}

async function renderCompany(root, ctx) {
  const {h, ui, fmt, say, href} = ctx;
  const ov = await load(ctx, OVERVIEW);
  if (!ctx.alive()) return;
  if (ov.error) { root.append(h("h1", null, "公司研究"), ui.errorBox(ov.error, () => ctx.navigate({}, {replace: true}))); return; }
  const c = findCompany(ov.value, ctx.state.company);
  if (!c) {
    root.append(h("h1", null, "找不到这家公司"),
      ui.empty(`当前研究项目里没有「${ctx.state.company || ""}」。它可能不在本项目的覆盖名单中。`),
      h("a", {href: href({page: "companies"})}, "返回公司列表"));
    return;
  }
  const key = companyKey(c);
  const tab = SECTION_IDS.includes(ctx.state.tab) ? ctx.state.tab : null;
  const rawName = String(c.full_name || c.name || "").trim();
  const registeredName = rawName && !/^\d{6,10}$/.test(rawName) && !isInternalRef(rawName) ? rawName : companyLabel(c);
  const listing = [c.exchange, c.ticker].filter(Boolean).join(": ");
  root.append(h("div", {class: "v2-crumb"}, h("a", {href: href({page: "companies"})}, "覆盖公司研究"), " › ",
    registeredName, listing && listing !== registeredName ? `（${listing}）` : ""));
  root.append(companyHeader(ctx, c));
  // The profit bridge (chart module) announces the exact filed line; the
  // evidence section opens that line and its filing (C13), no text search.
  root.addEventListener("dalton:statement-line", e => {
    const d = e.detail || {};
    const line = String(d.label || "").slice(0, 120);
    const filters = V().state?.get ? V().state.get().filters : ctx.state.filters;
    ctx.navigate({tab: "evidence", filters: {...filters, line, line_id: d.line_id || "", accession: "", importance: ""}});
  });
  const body = h("div", {class: "v2-company-body"});
  // The shell styles .v2-compact (one surface level, denser rows; G03/G05).
  root.append(h("div", {class: "v2-company v2-compact"}, toc(ctx, key, tab), body));
  const sec = Object.fromEntries(SECTIONS.map(s => [s.id, sectionShell(ctx, s.id)]));
  body.append(...SECTIONS.map(s => sec[s.id]));
  if (tab) setTimeout(() => scrollToTab(ctx, tab, ctx.kind !== "pop"), 0);

  const research = load(ctx, `/v1/cockpit/research?company=${encodeURIComponent(c.company_ref)}`);
  const model = load(ctx, `/v1/cockpit/model?company=${encodeURIComponent(c.company_ref)}`);
  const data = {company: c, overview: ov.value, research: null, model: null};

  // Sections that need only the overview first, then the slower reads.
  await Promise.all([
    viewSection(ctx, sec.view, data, research),
    valuationSection(ctx, sec.valuation, data),
    risksSection(ctx, sec.risks, data, research),
    evidenceSection(ctx, sec.evidence, data),
    modelSection(ctx, sec.model, data, model),
    eventsSection(ctx, sec.events, data),
    scenariosSection(ctx, sec.scenarios, data),
    returnsSection(ctx, sec.returns, data),
    reportsSection(ctx, sec.reports, data, research),
  ].map(p => p.catch(e => console.warn(e))));
  if (tab && ctx.alive() && ctx.kind !== "pop") scrollToTab(ctx, tab, false);
}

async function updateCompany(ctx) {
  if (ctx.prev.page !== "company" || ctx.prev.company !== ctx.state.company) return false;
  const doc = globalThis.document;
  if (!doc.getElementById("sec-view")) return false;
  const tab = SECTION_IDS.includes(ctx.state.tab) ? ctx.state.tab : null;
  if (tab && tab !== ctx.prev.tab) scrollToTab(ctx, tab, true);
  // Evidence filters live in the URL; redraw that section only.
  if (["aspect", "importance", "line", "line_id", "accession", "versions"].some(k => ctx.state.filters[k] !== ctx.prev.filters[k])) {
    const ov = await load(ctx, OVERVIEW);
    const c = findCompany(ov.value, ctx.state.company);
    if (c) await evidenceSection(ctx, doc.getElementById("sec-evidence"), {company: c});
  }
  // The scenario form opens and closes in place (#/company/<代码>/scenarios?edit=probabilities).
  if (ctx.state.filters.edit !== ctx.prev.filters.edit) {
    const ov = await load(ctx, OVERVIEW);
    const c = findCompany(ov.value, ctx.state.company);
    if (c) await scenariosSection(ctx, doc.getElementById("sec-scenarios"), {company: c}, {focusEditor: ctx.state.filters.edit === "probabilities"});
  }
  return true;
}

/* --------------------------------------------------------- 投资观点 -- */

function product(research, kind) { return (research?.products || []).find(p => p.kind === kind) || null; }
function readyText(p) { return p && p.status === "available" && (p.sections || []).length; }
function publicationTag(ctx, p) {
  const {ui, say} = ctx;
  if (!p) return null;
  if (p.publication_status === "ready" || p.final_text?.status === "ready") return ui.tag(REPORT.final, "ok");
  if (!p.publication_status && !p.final_text?.status) return ui.tag("文字审核状态未确认", "warn");
  return ui.tag(say(p.draft_label || REPORT.draft), "warn");
}
/** A research product's name: the registry's word for its kind (am 5.9). */
export function productName(ctx, p) {
  return p.display_label || LABELS.report_kind[p.kind] || ctx.say(p.label);
}

function viewSection(ctx, section, data, researchLoad) {
  return viewBody(ctx, section, data, researchLoad, {fill, product, readyText, publicationTag, openReader,
                                                     feedbackDisclosure, reflect: {untranslated}});
}

/* ------------------------------------------------------- 估值与回报 -- */

function metricValue(ctx, mtr) {
  const {fmt} = ctx;
  if (mtr.status !== "available" || mtr.value == null) return null;
  if (mtr.unit === "fraction") return fmt.fraction(mtr.value);
  if (mtr.unit === "ratio") return fmt.ratio(mtr.value);
  if (mtr.unit === "percent") return fmt.percent(mtr.value);
  return fmt.number(mtr.value, 2);
}

/* A valuation metric's missing reason in Chinese (C06): the reviewed text when
   there is one, else what kind of input is missing -- never an English system
   sentence beside a Chinese table. */
function metricReason(ctx, x) {
  const shown = ctx.say(x.reason || "");
  if (shown && !untranslated(shown)) return shown;
  if (/interest/i.test(x.reason || "")) return "待补充近四个季度利息支出数据";
  if (/debt/i.test(x.reason || "")) return "待补充总债务数据";
  if (/depreciation|amortization|ebitda/i.test(x.reason || "")) return "待补充折旧摊销数据（经调 EBITDA 无法计算）";
  if (/share|diluted/i.test(x.reason || "")) return "待补充稀释股数数据";
  if (/earnings|net income|eps/i.test(x.reason || "")) return "待补充近四个季度净利润数据";
  if (/cash flow|capex|free cash/i.test(x.reason || "")) return "待补充经营现金流与资本开支数据";
  return "待补充计算所需的申报科目数据";
}

/* Each return leg with its labels beside the number (C07/C08, G11): the chart
   payload's badges when it is readable, else the overview's leg. */
function legsTable(ctx, c, chartLegs) {
  const {h, ui, fmt, say} = ctx;
  const legs = c.return_attribution?.returns?.legs || [];
  if (!legs.length) return honestState(ctx, {state: "还没有回报归因。", impact: "看不到实际回报和各类预期回报的拆分。",
    owner: "研究系统在行情、每股数据和模型就绪后计算。", action: "先看估值指标和情景。"});
  const byLeg = new Map((chartLegs || []).map(l => [l.leg, l]));
  return ui.table({
    caption: "预期总回报空间与多口径测算对照：已实现与各类预期分开列示，预测的限制写在数字旁边。",
    columns: [
      {key: "label", label: "回报类型", render: l => (l.leg === "realised_trailing_year" ? "历史已实现总回报（过去 12 个月 TTM 业绩驱动归因）" : say(l.label))},
      {key: "window", label: "期间", render: l => (l.window ? `${fmt.date(l.window.start)} 至 ${fmt.date(l.window.end)}` : "—")},
      {key: "total", label: "总回报", num: true, render: l => {
        if (l.status !== "available") return h("span", null, h("span", {class: "v2-num"}, "—"),
          h("details", {class: "v2-more"}, h("summary", null, "缺项说明"), h("p", {class: "v2-note"}, say(l.reason || "无法计算"))));
        const chart = byLeg.get(l.leg);
        const badges = (chart?.badges || []).filter(b => !/^(简化|粗算)估算$/.test(b.text));
        const simplified = chart?.simplified_label || (isEstimate(l) ? "简化估算" : null);
        return h("span", null, h("span", {class: "v2-num"}, fmt.change(l.total_return_percent)),
          badges.length ? h("div", null, forecastBadges(badges, {seen: ctx.seen})) : null,
          simplified ? h("div", {class: "v2-note"}, glossTerm("简化估算", ctx.seen, "粗算估算"), simplified.replace(/^(简化|粗算)估算/, ""),
                         "；本行按所列假设作方向性测算，不代表完整三大表或 DCF 估值；具体口径见本行说明，财务模型另列。") : null);
      }},
      {key: "price", label: "股价回报", num: true, render: l => (l.status === "available" ? fmt.change(l.price_return_percent) : "—")},
      {key: "target", label: "12 个月基准目标价", num: true, render: l => (l.price_target ? `${fmt.number(l.price_target, 2)} 美元` : "—")},
    ],
    rows: legs,
  });
}

/** The basis-date drift of a forward leg (am 5.2): a window that started
    before today is not "the next 12 months" and says so. */
export function driftWarning(ctx, legs, today = new Date().toISOString().slice(0, 10)) {
  const {fmt} = ctx;
  const l = (legs || []).find(x => x.leg === "expected_driver_model" && x.window?.start);
  if (!l || !(String(l.window.start) < today)) return null;
  const generated = l.generated_at || l.model_created_at || l.window.start;
  return `模型预测周期：${fmt.date(l.window.start)} 至 ${fmt.date(l.window.end)}（基准预测生成于 ${fmt.date(generated)}，部分期间已过，需滚动更新后再作为未来 12 个月预期使用）`;
}

async function valuationSection(ctx, section, data) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const el = h("div");
  const chart = h("div", {class: "v2-slot"});
  fill(section, el, chart);
  const returns = await load(ctx, `/v2/data/charts/returns?company=${encodeURIComponent(c.company_ref)}`);
  {
    const v = c.valuation;
    if (v && (v.metrics || []).length) {
      el.append(ui.table({
        caption: `当前主要估值乘数（基于 ${fmt.date(v.as_of)} 美东收盘行情）`,
        columns: [
          {key: "metric", label: "指标", render: x => METRIC_LABELS[x.metric] || "估值倍数"},
          {key: "value", label: "当前值", num: true, render: x => metricValue(ctx, x) ?? h("span", {class: "v2-note"}, "暂无")},
          {key: "pct", label: "历史分位", header: () => h("span", null, glossTerm("历史分位", ctx.seen)), num: true,
           render: x => (x.percentile != null ? `${fmt.number(x.percentile, 1)}%` : "—")},
          {key: "note", label: "说明", render: x => (x.status !== "available" ? metricReason(ctx, x) : x.sample_size ? `近 ${x.sample_size} 个交易日历史分位样本` : "—")},
        ],
        rows: v.metrics,
      }));
      if (v.basis?.shares_note) el.append(ui.note(say(v.basis.shares_note)));
    } else el.append(honestState(ctx, {state: "还没有估值指标。", impact: "看不到市盈率等倍数和历史位置。",
      owner: "研究系统在行情和申报数据就绪后计算。", action: "先看财务模型和研究报告。"}));
    el.append(h("h3", null, "预期总回报空间与多口径测算对照"));
    const drift = driftWarning(ctx, returns.value?.legs || c.return_attribution?.returns?.legs);
    if (drift) el.append(ui.alert(drift, "warn"));
    el.append(legsTable(ctx, c, returns.value?.legs));
  }
  if (!(await ctx.slot("company.valuation", chart, {company: c, company_ref: c.company_ref}, null))) chart.remove();
}

/* ------------------------------------------------ 风险与下一验证事件 -- */

async function risksSection(ctx, section, data, researchLoad) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const out = [];
  const cat = c.catalyst;
  out.push(h("h3", null, "下一关键催化事件"));
  out.push(cat ? h("p", null, `${say(cat.event_label)}：${fmt.date(cat.expected_date)}`, cat.days_until != null ? `（距今 ${cat.days_until} 天）` : "",
                   cat.date_unconfirmed ? h("span", null, " ", ui.tag("上市公司尚未正式定档", "warn")) : null)
               : honestState(ctx, {state: "还没有已知的下一关键催化事件。", impact: "无法提前安排对观点的检验。",
                   owner: "研究系统从财报日历和公司公告中更新。", action: "关注下面正在跟踪的预警触发线。"}));
  if (cat) {
    const observations = cat.key_observations || cat.verification_points || [];
    out.push(h("h3", null, "关键观测点"), observations.length
      ? h("ul", null, observations.map(point => h("li", null, say(typeof point === "string" ? point : point.label || point.question || ""))))
      : ui.note("尚未明确这次事件要核对哪些经营指标；先查看下方的情景触发条件与多空分歧。"));
  }
  const scenarios = c.scenarios?.in_force || [];
  const watch = scenarios.flatMap(s => (s.triggers || []).map(t => ({s, t}))).filter(x => !x.t.status || x.t.status === "pending" || x.t.status === "open");
  // The condition as a sentence from its structured fields (t.rule), never a
  // code expression; the scenario and its own label as the note (am 5.3).
  if (watch.length) out.push(h("h3", null, "正在跟踪的触发信号与预警线"), h("ul", null, watch.slice(0, 6).map(({s, t}) =>
    h("li", null, say(t.rule || t.display_label || t.label), h("span", {class: "v2-note"}, `（${say(s.name)}：${say(t.display_label || t.label)}）`)))));
  const bear = scenarios.find(s => s.key === "bear");
  if (bear) out.push(h("h3", null, "下行风险敞口（悲观情景推演）"),
    h("p", null, `${say(bear.name)} · 发生概率：${fmt.probability(bear.probability)} ｜ 潜在跌幅：${bear.target_return_percent != null ? fmt.change(bear.target_return_percent) : "待测算（情景尚无目标价）"}`),
    h("p", {class: "v2-note"}, lead(say(bear.narrative), 240)));
  fill(section, out);
  const r = await researchLoad;
  const debate = product(r.value, "debate_map");
  if (debate && (debate.sections || []).length) {
    const questions = [...new Set(debate.sections.map(s => splitDebateTitle(s.title).question))];
    section.append(h("h3", null, "多空核心分歧议题"), h("ol", null, questions.map(q => h("li", null, say(q)))),
      ui.button("查看多空分歧与论据全文 →", () => openReader(ctx, debate), {variant: "quiet"}));
  }
  const gaps = c.source_readiness?.ready === false ? c.note : null;
  if (gaps) section.append(ui.alert(`资料缺口：${say(gaps)}`));
  // The acquisition step under the checklist item's own note (note unchanged).
  const steps = periodSteps(c);
  if (steps.length) section.append(h("h3", null, "资料清单"), h("ul", {class: "v2-list"}, steps.map(x => h("li", {class: "v2-card"},
    h("p", null, say(x.label), x.note ? `：${say(x.note)}` : ""),
    h("p", {class: "v2-note"}, say(x.step))))));
}

/* ------------------------------------------------------------- 证据 -- */

function evidenceSection(ctx, section, data) {
  return evidenceBody(ctx, section, data, fill);
}

/* --------------------------------------------------------- 财务模型 -- */

function modelSection(ctx, section, data, modelLoad) {
  return modelBody(ctx, section, data, modelLoad, fill);
}

/* ------------------------------------------------------------- 事件 -- */

async function eventsSection(ctx, section, data) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const events = c.events?.latest || [];
  const seen = new Set();
  const judgements = (c.judgements?.latest || []).filter(j => { const k = String(j.because || "").slice(0, 200); if (seen.has(k)) return false; seen.add(k); return true; });
  const out = [];
  out.push(h("h3", null, "公司事件与研究更新"));
  out.push(events.length ? h("ul", null, events.map(e => h("li", null, `${fmt.datetime(e.occurred_at)} · ${say(e.kind_label || "")}：`,
    e.display_summary ? say(e.display_summary) : isInternalRef(e.summary) ? refLabel(e.summary) : say(e.summary), " ", ui.tag(say(e.tier_label || "")))))
    : honestState(ctx, {state: "尚无可显示的重大事件记录。", impact: "没有事件研判记录，暂不能确认新事件是否影响现有观点。", owner: "事件记录由研究系统根据已取得的公告、财报和新闻生成。", action: "如需核对具体事件，可在智能投研问答中说明公司与事件日期。"}));
  if (c.events?.total) out.push(ui.note(`共 ${fmt.number(c.events.total)} 个事件，显示最近 ${events.length} 个。`));
  out.push(h("h3", null, "研判轨迹与基准调整日志"));
  if (judgements.length) {
    out.push(h("ul", {class: "v2-list"}, judgements.map(j => h("li", {class: "v2-card"},
      h("div", {class: "v2-card-meta"}, h("span", null, fmt.datetime(j.at)), ui.tag(say(j.event_kind_label || "")), j.verifier_label ? ui.tag(say(j.verifier_label), j.verifier_verdict === "pass" ? "ok" : "warn") : null),
      h("p", null, h("strong", null, say(j.decision_label || j.action_label)), " ", lead(say(j.because), 160)),
      h("details", {class: "v2-more"}, h("summary", null, "完整论证"), ...ui.paragraphs(say(j.because), ctx.seen))))));
    out.push(ui.note(`共 ${fmt.number(c.judgements.total)} 条研判，显示最近 ${judgements.length} 条（相同理由已合并）。`));
  } else out.push(honestState(ctx, {state: "还没有研判记录。", impact: "还看不到事件对观点的影响。", owner: "有新事件时研究系统会写判断。", action: "无需操作。"}));
  out.push(...researchRecord(ctx, c));
  fill(section, out);
}

/* What used to sit at the bottom of the old company panel: the company's own
   preliminary results, the after-the-fact reviews of its judgements (复盘),
   the special research under way and how often each source is refreshed. */
export function researchRecord(ctx, c) {
  const {h, ui, fmt, say} = ctx;
  const out = [];
  const p = c.preliminary_results;
  if (p) {
    out.push(h("h3", {id: "events-preliminary"}, `业绩快报对账与偏离度检视 · ${say(p.title || "公司初步业绩")}`),
      ui.note(`${say(p.period || "")}（截至 ${fmt.date(p.period_end)}）· 8-K ${p.accession || "—"} · 公告日 ${fmt.date(p.report_date)}；正式 10-Q/10-K 申报后以申报数为准。`),
      (p.rows || []).length ? h("ul", null, p.rows.map(r => h("li", null, `${say(r.label)}：${say(r.text)}`,
        r.status === "superseded" && r.filed ? h("span", {class: "v2-note"}, `（申报数 ${say(r.filed)}）`) : null))) : null,
      ...(p.provisional_reconciliations || []).map(r => ui.note(reconciliationSentence(ctx, p, r))),
      p.note ? ui.note(say(p.note)) : null);
  }
  const reflections = c.reflections || [];
  out.push(h("h3", {id: "events-reflection"}, "研判得失复盘与逻辑回溯"));
  if (reflections.length) out.push(h("ul", {class: "v2-list"}, reflections.map(r => reflectionCard(ctx, r))));
  else out.push(honestState(ctx, {state: "还没有针对这家公司的事后复盘。", impact: "尚不能从复盘记录判断预测与实际结果是否存在偏差。",
    owner: "股价或业绩和我们的判断明显相反时，研究系统会写一份复盘。", action: "无需操作。"}));
  const tasks = c.research_tasks || [];
  if (tasks.length) out.push(h("h3", {id: "events-tasks"}, "正在进行的专项课题"), h("ul", {class: "v2-list"}, tasks.map(t => h("li", {class: "v2-card"},
    h("p", null, say(t.question || "专项研究")),
    h("div", {class: "v2-card-meta"}, ui.tag(say(t.state_label || "")), t.budget_label ? h("span", null, say(t.budget_label)) : null),
    t.conclusion || t.gap ? h("p", {class: "v2-note"}, say(t.conclusion || t.gap)) : null))));
  const cadence = c.cadence || [];
  if (cadence.length) out.push(h("details", {class: "v2-more", id: "events-cadence"}, h("summary", null, `数据更新与跟踪频率（${cadence.length} 项）`),
    ui.table({caption: "数据源抓取与跟踪监控频次；「按近期事件提高」表示已根据近期重大事件自动提高监控敏感度",
      columns: [
        {key: "source", label: "资料", render: r => say(r.source_label || "资料")},
        {key: "interval", label: "更新频率", render: r => say(r.interval_label || "—")},
        {key: "who", label: "调整", render: r => (r.decided_by_brain ? ui.tag("按近期事件提高", "ok") : r.adjustable ? "可随事件调整" : "固定频率")},
        {key: "because", label: "依据", render: r => (r.because ? showText(ctx, r.because) : "—")},
      ], rows: cadence})));
  return out;
}

/* ------------------------------------------------------- 情景与触发 -- */

async function scenariosSection(ctx, section, data, {focusEditor = false} = {}) {
  const {h, ui, fmt, say, href} = ctx;
  const c = data.company;
  const holder = h("div");
  const receiptHolder = h("div", {id: "scenario-receipt", tabindex: "-1"});
  const editorHolder = h("div");
  const receipt = lastReceipt(c.company_ref);
  if (receipt) receiptHolder.append(ui.receipt(receipt.text, receipt.tone));
  fill(section, receiptHolder, holder, editorHolder);
  if (receipt && receipt.fresh) { receipt.fresh = false; setTimeout(() => receiptHolder.focus?.(), 0); }
  await ctx.slot("company.scenarios", holder, {company: c, company_ref: c.company_ref}, el => {
    const s = c.scenarios || {};
    const list = s.in_force || [];
    if (!list.length) el.append(honestState(ctx, {state: "还没有生效的情景。", impact: "没有情景概率，也不跟踪触发条件。",
      owner: `研究系统提出情景后，会出现在「${LABELS.nav.tasks}」里请你确认。`, action: `去「${LABELS.nav.tasks}」查看有没有情景提案。`,
      link: {href: href({page: "tasks"}), text: `打开${LABELS.nav.tasks}`}}));
    else {
      const order = {bull: 0, base: 1, bear: 2};
      el.append(ui.table({
        caption: `多情景推演对照表 ｜ 当前情景概率合计 ${fmt.probability(s.probability_sum)} ｜ 情景尚无目标价或回报区间，暂不展示概率加权目标价`,
        columns: [
          {key: "name", label: "宏观与经营情景", render: x => h("span", null, h("strong", null, SCN[x.key] || say(x.key)), h("div", null, say(x.name)))},
          {key: "p", label: "主观概率权重", num: true, render: x => fmt.probability(x.probability)},
          {key: "story", label: "核心经营假设与外部环境", render: x => h("details", {class: "v2-more"}, h("summary", null, lead(say(x.narrative), 60)), ...ui.paragraphs(say(x.narrative)))},
          {key: "triggers", label: "关键触发信号与预警触发线", render: x => {
            const conditions = [...(x.triggers || []), ...(x.invalidators || [])];
            // The sentence built from the condition's fields (t.rule) leads;
            // the author's own label follows as the note (am 5.3).
            return conditions.length ? h("ul", null, conditions.map(t => h("li", null,
              t.rule ? say(t.rule) : `${t.kind === "invalidation" || t.kind === "invalidator" ? SCN.invalidator : SCN.trigger}：${say(t.display_label || t.label)}`,
              t.rule ? h("div", {class: "v2-note"}, say(t.display_label || t.label)) : null,
              t.observer_label ? h("div", {class: "v2-note"}, `判断依据：${say(t.observer_label)}`) : null))) : "—";
          }},
        ],
        rows: list.slice().sort((a, b) => (order[a.key] ?? 9) - (order[b.key] ?? 9)),
      }));
    }
    if (s.pending) el.append(ui.alert("有新的情景提案等待确认。", "info", " ", h("a", {href: href({page: "tasks"})}, "宏观与经营情景假设待确认")));
  });
  const live = c.scenarios?.in_force || [];
  if (!live.length) return;
  const key = companyKey(c);
  if (ctx.state.filters.edit === "probabilities") {
    const editor = scenarioEditor(ctx, c, {
      onClose: () => ctx.navigate({filters: {...ctx.state.filters, edit: ""}}, {replace: true}),
      onSaved: async ({failed}) => {
        // Saved: the URL loses ?edit and the section is redrawn from a fresh
        // read (updateCompany), with the receipt on it.  Partly saved: redraw
        // here with the form still open and the unsaved rows kept.
        if (!failed.length) { ctx.navigate({filters: {...ctx.state.filters, edit: ""}}, {replace: true}); return; }
        const ov = await load(ctx, OVERVIEW, {fresh: true});
        await scenariosSection(ctx, section, {company: findCompany(ov.value, ctx.state.company) || c});
        globalThis.document?.getElementById("scenario-receipt")?.focus?.();
      },
    });
    editorHolder.append(editor);
    if (focusEditor) setTimeout(() => { editor.scrollIntoView?.({block: "start"}); editor.querySelector("h3")?.focus(); }, 0);
  } else {
    editorHolder.append(h("div", {class: "v2-actions"},
      h("a", {class: "v2-btn v2-btn-secondary", href: href({page: "company", company: key, tab: "scenarios", filters: {edit: "probabilities"}})}, SCN.edit),
      h("span", {class: "v2-note"}, "拖动或输入新的权重，合计 100% 后整组一次保存；当前三情景组必须说明概率依据。")));
  }
}

/* --------------------------------------------------------- 回报归因 -- */

async function returnsSection(ctx, section, data) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const holder = h("div");
  fill(section, holder);
  await ctx.slot("company.returns", holder, {company: c, company_ref: c.company_ref}, el => {
    const ra = c.return_attribution;
    if (!ra) { el.append(honestState(ctx, {state: "还没有回报归因。", impact: "看不到回报来自盈利、股数还是估值倍数。",
      owner: "研究系统在行情、每股数据和模型就绪后计算。", action: "先看估值与回报部分。"})); return; }
    if (ra.headline) el.append(h("p", null, say(ra.headline)));
    for (const name of ["realised_trailing_year", "expected_driver_model", "expected_consensus", "expected_sell_side"]) {
      const l = returnLeg(c, name);
      if (!l) continue;
      const title = name === "realised_trailing_year" ? `${ATTR.title}（EPS 按财务期间对齐，不是当时可得的数据）` : say(l.label);
      el.append(h("h3", null, title, isEstimate(l) ? h("span", null, " ", ui.tag("粗算估算", "warn")) : null));
      if (l.status !== "available") { el.append(ui.empty(say(l.reason || "无法计算"))); continue; }
      // One unit declaration for the whole table (am 5.8): cells carry the
      // signed number only, never a repeated 「个百分点」.
      el.append(ui.table({
        caption: `总回报 ${fmt.change(l.total_return_percent)} ｜ ${ATTR.unit_note}`,
        columns: [
          {key: "label", label: ATTR.factor, render: x => factorName(ctx, x)},
          {key: "pp", label: ATTR.contribution, num: true, render: x => (x.contribution_pp == null ? h("span", {class: "v2-note"}, "未单独拆出") : signedPp(x.contribution_pp))},
        ],
        rows: l.components || [],
      }));
      if ((l.notes || []).length) el.append(h("ul", {class: "v2-note"}, l.notes.map(n => h("li", null, say(n)))));
    }
  });
}

/** A return factor's name in the registry's TSR words (am 5.8). */
const FACTOR_KEYS = [[/收入|revenue/i, "revenue"], [/利润率|margin/i, "margin"], [/股数|回购|稀释|share/i, "shares"],
                     [/倍数|估值|multiple|p\/e/i, "multiple"], [/股息|dividend/i, "dividend"], [/其他|残差|交叉|residual/i, "residual"]];
function factorName(ctx, x) {
  const named = (LABELS.attribution_component || {})[x.component];
  if (named) return named;
  const raw = ctx.say(x.label || "");
  const hit = FACTOR_KEYS.find(([re]) => re.test(raw) || re.test(String(x.key || "")));
  return hit ? ATTR[hit[1]] : raw;
}
/** "+2.4": the unit is declared once in the caption. */
function signedPp(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const t = (Math.round(n * 10) / 10).toFixed(1);
  return `${n > 0 ? "+" : ""}${t}`;
}

/** 「实际业绩对账：<期间> 营业收入录得 <金额>，较我们的预测（<模型版本>）偏离 −1.6%（低于预期）」 (am 5.6). */
export function reconciliationSentence(ctx, p, r) {
  const {fmt, say} = ctx;
  const revenue = (p.rows || []).find(x => /收入|revenue/i.test(String(x.label || "")));
  const amount = r.actual_label || (revenue ? say(revenue.text) : null);
  const d = Number(r.deviation_percent);
  const signed = Number.isFinite(d) ? `${d > 0 ? "+" : d < 0 ? "−" : ""}${fmt.number(Math.abs(d), 1)}%` : "—";
  const side = !Number.isFinite(d) ? "" : d > 0 ? "（高于预期）" : d < 0 ? "（低于预期）" : "（与预期一致）";
  const version = r.forecast_version_label || r.model_version_label || null;
  const basis = [version ? say(version) : null, r.forecast_label ? `预测值 ${say(r.forecast_label)}` : null].filter(Boolean).join("；");
  return `实际业绩对账：${say(r.period_label || p.period || "")} 营业收入${amount ? `录得 ${amount}，` : ""}较我们的预测${basis ? `（${basis}）` : ""}偏离 ${signed}${side}`
    + (r.status === "superseded" ? "；已依据正式 10-Q/10-K 申报数据完成最终校准对账" : "");
}

/* --------------------------------------------------------- 研究报告 -- */

function openReader(ctx, p) {
  const {h, ui, fmt, say} = ctx;
  ctx.side.open({heading: LABELS.report_state.reader.replace("{title}", productName(ctx, p)), render: el => {
    el.append(h("p", {class: "v2-note"}, `${say(p.version_label || "")} · ${fmt.date(p.created_at)} `, publicationTag(ctx, p)));
    const sections = p.sections || [];
    if (p.kind === "debate_map") {
      const groups = new Map();
      for (const s of sections) {
        const {question, role} = splitDebateTitle(s.title);
        if (!groups.has(question)) groups.set(question, []);
        groups.get(question).push({role: role || "研究内容", s});
      }
      for (const [question, parts] of groups) {
        el.append(h("h3", null, say(question)));
        for (const {role, s} of parts) el.append(h("h4", null, role), ...ui.paragraphs(s.display_body || say(s.body)));
      }
    } else {
      for (const s of sections) {
        el.append(h("h3", null, sectionTitle(s.display_title || s.title)), ...ui.paragraphs(s.display_body || say(s.body)));
        const gaps = s.display_gaps || [];
        if (gaps.filter(Boolean).length) el.append(h("details", {class: "v2-more"}, h("summary", null, "数据缺失与待求证事项"), h("ul", null, gaps.filter(Boolean).map(g => h("li", null, say(g))))));
        const srcs = s.sources || [];
        if (srcs.length) el.append(h("details", {class: "v2-more"}, h("summary", null, `来源（${srcs.length}）`), h("ol", {class: "v2-cite"}, srcs.map(x => h("li", null,
          h("span", {lang: untranslated(x.text) ? "en" : null}, x.text || "来源信息待补齐"), x.period ? ` · ${x.period}` : "")))));
      }
    }
  }});
}

async function reportsSection(ctx, section, data, researchLoad) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const r = await researchLoad;
  if (r.error) { fill(section, ui.errorBox(r.error)); return; }
  const products = r.value.products || [];
  const status = h("span", {class: "v2-note", role: "status"});
  const html = ui.button(REPORT.export, () => exportFile(ctx, c, "html", html, status), {variant: "secondary"});
  fill(section,
    products.length ? ui.table({
      caption: "本标的已归档的研究报告合辑；点「阅读」在右侧研报阅读器打开，关闭后回到这里",
      columns: [
        {key: "label", label: "报告", render: p => productName(ctx, p)},
        {key: "state", label: "状态", render: p => (p.status === "available" ? publicationTag(ctx, p) : h("span", {class: "v2-note"}, say(p.reason || "尚未生成")))},
        {key: "version", label: "版本", render: p => say(p.version_label || "—")},
        {key: "quality", label: "质量评审", render: p => p.quality?.label || "尚无本版本的质量评审记录"},
        {key: "at", label: "更新时间", render: p => (p.created_at ? fmt.date(p.created_at) : "—")},
        {key: "open", label: "操作", render: p => (p.status === "available" && (p.sections || []).length ? ui.button("阅读", () => openReader(ctx, p), {variant: "quiet", "aria-label": `阅读${productName(ctx, p)}`}) : "—")},
      ],
      rows: products,
    }) : honestState(ctx, {state: "还没有研究报告。", impact: "没有可以阅读或下载的报告。",
      owner: "研究系统按研究阶段依次撰写。", action: "先看证据和财务模型。"}),
    h("div", {class: "v2-actions"}, html, status),
    (r.value.legacy_gate_history || []).length ? h("details", {class: "v2-more"},
      h("summary", null, "历史复核副本"),
      ...(r.value.legacy_gate_history || []).map(item => h("div", null,
        h("p", null, `${fmt.date(item.created_at)} · ${item.label}`),
        item.replacement ? ui.button("阅读对应的正式复核记录", () => openReader(ctx, item.replacement), {variant: "quiet"}) : null,
        ui.technical(h("p", null, `旧记录：${item.version_ref}；正式记录：${item.superseded_by || "尚无"}`))))) : null,
    ui.note(REPORT.export_note));
}

V().registerPage("companies", {title: LABELS.nav.companies, nav: {label: LABELS.nav.companies, order: 20}, render: renderList, update: updateList});
V().registerPage("company", {title: LABELS.nav.companies, nav: null, parent: "companies", route: "company",
                             params: ["company", "tab"], render: renderCompany, update: updateCompany});
