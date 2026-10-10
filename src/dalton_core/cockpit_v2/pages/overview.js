/* 研究总览: research conclusions, important changes, my decisions (H01).

   2026-10-09 (independent §4 / §14, MUSE §2, financial 4.1):
   - the first screen answers four things: 最新判断 / 变化原因 / 影响公司 /
     下一步 (independent §1: "30 秒内知道…");
   - changes never print an internal reference (O03): a hash or ref becomes
     what it is in words; opinion changes come before new material (O04);
   - the comparison is one table read from the chart API's comparison payload
     -- the same ranking the chart uses -- and the fallback table renders that
     same payload (O01/O13);
   - only the current version of each thesis is a conclusion (P0, owner saw
     「需求正接近底部」 beside 「底部仍未确认」): 「行业论点」 and 「公司论点」
     hold current versions, superseded ones sit in a collapsed 「观点演变」
     marked 非当前, and the headline never reads a superseded version;
   - the administrator's failure ledger is not on this page (G02). */

import {foldTasks, sortTasks, kindOf, GROUPS, analystAction} from "./task_kinds.js";
import {V, OVERVIEW, NEEDS_HUMAN, companies, companyKey, companyName, compareRow,
        hasMission, isInternalRef, lead, load, periodSteps, refLabel, showText, tasksByCompany, thesisVersions} from "./common.js";
import {cnDate} from "./company_common.js";
import {evidenceButton} from "./company_view.js";
import {openPanel, syncPanel} from "./research_log.js";
import {comparisonSection, baselineContent} from "./company_compare.js";
import {LABELS, label} from "../copy.js";

const NAV = LABELS.nav, BTN = LABELS.decision_button;
/* A "reason" that only restates the decision (「维持现状」) is not a reason (am 3.1). */
const ECHO = /^(维持现状|维持现有判断|当前观点不变|维持观点|无变化|no[ _]change)[。.]?$/i;

/** An event's text, never an internal reference (O03 / C19). */
export function eventText(say, summary) {
  const t = String(summary || "").trim();
  if (!t) return "新增研究资料（内容解析中）";
  if (isInternalRef(t)) return refLabel(t);
  return say(t);
}

/** Changes worth a reader's attention, newest first (L01: research, not logs).
    `group` is "view" (opinion, scenario, price) or "material" (new filings,
    interviews, notes) -- O04: they are read separately. */
export function importantChanges(overview, ctx) {
  const {fmt, say} = ctx;
  const out = [];
  for (const c of companies(overview)) {
    for (const j of c.judgements?.latest || []) {
      if (j.action === "no_change" || j.decision === "NO_CHANGE") continue;
      out.push({at: j.at, company: c, kind: "观点调整", group: "view", text: say(j.decision_label || j.action_label || "研究判断有调整"),
                detail: j.because, tab: "events"});
    }
    for (const e of c.events?.latest || []) {
      if (e.kind === "calendar") continue;
      // The server's display summary first: a document title, never a ref (am 3.3).
      out.push({at: e.occurred_at, company: c, kind: say(e.kind_label || "新增资料"), group: "material",
                text: e.display_summary ? say(e.display_summary) : eventText(say, e.summary), tab: "events"});
    }
    const week = (c.market?.changes || []).find(x => x.days === 5 && x.status === "available");
    if (week && Math.abs(Number(week.percent)) >= 5)
      out.push({at: c.market.as_of, company: c, kind: "市场表现", group: "view",
                text: `近 5 个交易日累计${Number(week.percent) >= 0 ? "涨幅" : "跌幅"} ${fmt.change(week.percent)}`, tab: "valuation"});
    const s = c.scenarios || {};
    if (s.triggered) out.push({at: null, company: c, kind: "情景预警", group: "view", text: `情景触发信号已出现 ${s.triggered} 个`, tab: "scenarios"});
    if (s.invalidated) {
      // Name the condition that fired (am 3.3), not only a count.
      const fired = (s.in_force || []).flatMap(x => x.invalidators || []).find(t => t.status === "triggered");
      const what = fired ? say(fired.rule || fired.display_label || fired.label || "") : "";
      out.push({at: null, company: c, kind: "情景预警", group: "view",
                text: `${LABELS.scenario.invalidated}${what ? `：${what}` : `：${s.invalidated} 个条件`}`, tab: "scenarios"});
    }
  }
  // Research held up on an acquisition step comes first: it is why nothing new arrives.
  const blocked = companies(overview).flatMap(c => periodSteps(c, {blocking: true}).map(x =>
    ({at: null, company: c, kind: "资料收集中断", group: "view", text: `${say(x.label)}：${say(x.step)}`, tab: "risks"})));
  return [...blocked, ...out.sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")))];
}

function lastJudgement(overview, {changedOnly = false} = {}) {
  let latest = null;
  for (const c of companies(overview)) for (const j of c.judgements?.latest || []) {
    if (changedOnly && (j.action === "no_change" || j.decision === "NO_CHANGE")) continue;
    if (!latest || String(j.at) > String(latest.at)) latest = {...j, company: c};
  }
  return latest;
}

/** The four answers of the first screen (pure over the payloads). */
export function headline(ctx, overview, needs) {
  const {say, fmt} = ctx;
  const changed = lastJudgement(overview, {changedOnly: true});
  const latest = changed || lastJudgement(overview);
  // G10: a thesis with no reviewed Chinese is not the headline.  Only a
  // current version may be (P0): the industry thesis first, then a company's.
  const readable = t => /[㐀-鿿]/.test(say(t.display_summary || t.summary || ""));
  const current = thesisVersions(overview).current.filter(t => t.current !== false && t.stance !== "counter" && t.view_status !== "broken" && readable(t));
  const thesis = current.find(t => !t.company_ref) || current[0] || null;
  const view = changed
    ? `${companyName(changed.company)}：${say(changed.decision_label || changed.action_label || "研究判断有调整")}（${fmt.date(changed.at)}）`
    : thesis ? `${say(thesis.display_summary || thesis.summary)}${thesis.at ? `（${thesis.company_ref ? "公司" : "行业"}核心论点，截至${cnDate(thesis.at)}）` : ""}`
    : latest ? `${companyName(latest.company)}：维持基准判断（截至${cnDate(latest.at) || fmt.date(latest.at)}）`
    : "尚未建立正式投资观点（正在搜集公司报表与研报资料）。";
  // The reason belongs to the judgement it explains; with no change, the
  // latest review says why the view stands -- once, never 「维持观点：维持现状」.
  const reason = latest?.because ? lead(say(latest.because), 110) : "";
  const why = changed?.because ? lead(say(changed.because), 140)
    : latest ? `最近一次研判：${companyName(latest.company)}，${fmt.date(latest.at)}${reason && !ECHO.test(reason) ? `。依据：${reason}` : "。该次复核未调整判断。"}`
    : "尚无已完成的事件研判，暂不能判断近期经营变化对投资观点的影响。";
  const affected = [...new Set(importantChanges(overview, ctx).filter(x => x.group === "view").map(x => companyName(x.company)))];
  const items = needs ? sortTasks(foldTasks(needs.items || []).filter(analystAction)) : [];
  const catalysts = companies(overview).filter(c => c.catalyst?.expected_date).sort((a, b) => String(a.catalyst.expected_date).localeCompare(String(b.catalyst.expected_date)));
  const next = items.length ? `待审阅研究事项：共 ${items.length} 项重点决策待确认（首要：${say(items[0].title)}）`
    : catalysts.length ? `下一关键催化事件：${companyName(catalysts[0])} ${say(catalysts[0].catalyst.event_label || "下一事件")}，${fmt.date(catalysts[0].catalyst.expected_date)}${catalysts[0].catalyst.date_unconfirmed ? "（日期未确认）" : ""}`
    : LABELS.shell.blocked_none;
  return {view, why, affected: affected.length ? affected.join("、") : latest ? "当前研判记录未标注需要调整观点的公司。" : "尚无可用于判断影响范围的研判记录。", next};
}

const CONFIDENCE = {high: "高", medium: "中", low: "低（资料尚存缺口）"};

/** 「生效基准日：<date> · 研判版本：第 N 版 · <change_label>」 for a current version. */
export function thesisMeta(t) {
  return [t.at ? `生效基准日：${cnDate(t.at)}` : null, t.version ? `研判版本：第 ${t.version} 版` : null, t.change_label || null]
    .filter(Boolean).join(" · ");
}

/** Current theses only, industry then company; superseded versions folded
    under 「观点演变」 and marked 非当前 (P0 2026-10-09). */
export function renderTheses(ctx, el, overview) {
  const {h, ui} = ctx;
  const {current, history} = thesisVersions(overview);
  const disagreementPeer = t => [...current, ...history].find(x => x.version_ref === t.disagreement?.other_thesis_version_ref);
  const item = (t, {old = false} = {}) => h("li", {class: old ? "v2-history-item" : null},
    old ? ui.tag("历史归档", "warn") : null, old ? " " : null,
    ui.tag(t.view_status === "broken" ? "已被证据推翻" : t.stance === "counter" ? "反向观点" : t.stance === "house" ? "本所观点" : "历史口径观点", t.view_status === "broken" ? "warn" : ""), " ",
    showText(ctx, t.summary, t.display_summary), " ",
    ui.tag(`证据充分度：${CONFIDENCE[t.confidence] || "未注明"}`),
    h("p", {class: "v2-note"}, old
      ? [t.at ? `写于 ${cnDate(t.at)}` : null, t.version ? `第 ${t.version} 版` : null,
         t.superseded_at ? `${cnDate(t.superseded_at)}被修订取代` : "已被修订取代"].filter(Boolean).join(" · ")
      : thesisMeta(t)),
    t.disagreement ? h("details", {class: "v2-more"}, h("summary", null, "查看当前分歧与裁决安排"),
      h("p", null, ctx.say(t.disagreement.reason)),
      h("p", null, `复核截止日：${cnDate(t.disagreement.deadline)}；裁决人：${t.disagreement.arbiter_ref.replace(/^human:/, "")}`),
      h("p", null, disagreementPeer(t) ? `另一观点（第 ${disagreementPeer(t).version || "—"} 版）：${ctx.say(disagreementPeer(t).display_summary || disagreementPeer(t).summary)}` : "关联观点暂时无法读取。"),
      h("div", {class: "v2-actions"}, ...(t.disagreement.evidence_refs || []).map(ref => evidenceButton(ctx, ref))),
      h("details", {class: "technical"}, h("summary", null, "原始记录编号"), h("code", null, t.disagreement.other_thesis_version_ref))) : null);

  const industry = current.filter(t => !t.company_ref);
  if (industry.length) el.append(h("h3", null, "行业核心观点与宏观假设"), h("ul", {class: "v2-theses"}, industry.map(t => item(t))));
  const byCompany = new Map();
  for (const t of current.filter(t => t.company_ref)) {
    if (!byCompany.has(t.company_ref)) byCompany.set(t.company_ref, []);
    byCompany.get(t.company_ref).push(t);
  }
  if (byCompany.size) {
    el.append(h("h3", null, "公司核心论点"));
    for (const [ref, list] of byCompany) {
      const c = companies(overview).find(x => x.company_ref === ref);
      el.append(h("h4", null, c ? companyName(c) : "其他公司"), h("ul", {class: "v2-theses"}, list.map(t => item(t))));
    }
  }
  if (history.length) {
    const ordered = [...history].sort((a, b) => (a.company_ref ? 1 : 0) - (b.company_ref ? 1 : 0) || String(a.ref).localeCompare(String(b.ref)) || String(a.at || "").localeCompare(String(b.at || "")));
    el.append(h("details", {class: "v2-more", id: "thesis-evolution"},
      h("summary", null, `历史观点演变与分歧记录（${history.length} 条，已归档，仅供研判脉络对照）`),
      h("ul", {class: "v2-list"}, ordered.map(t => item(t, {old: true}))),
      ui.note("本区域记录随宏观环境与业绩期推移而修正的历史研判，用于复盘研判逻辑演进；当前决策请以上方生效观点为准。")));
  }
}

async function render(root, ctx) {
  const {h, ui, fmt, say, href} = ctx;
  const head = h("div", {class: "v2-page-head"}, h("h1", null, LABELS.page_title.overview));
  root.append(head);
  const [ov, nh] = await Promise.all([load(ctx, OVERVIEW), load(ctx, NEEDS_HUMAN)]);
  if (!ctx.alive()) return;
  if (ov.error) { root.append(ui.errorBox(ov.error, () => ctx.navigate({}, {replace: true}))); return; }
  const overview = ov.value;
  // A project name is the owner's own words: never rewritten.
  head.append(h("span", {class: "v2-sub"}, overview.workspace?.name || ""));

  if (!hasMission(overview)) {
    // O09: a project without a goal is 待开始, with one way to start.
    root.append(ui.section("start", "当前研究项目尚未设定研究目标",
      h("p", {class: "v2-lead"}, "请设定本项目重点覆盖的行业赛道、核心标的池及关键研究课题。系统将据此整理研究目标草案，经你确认后开始持续数据跟踪与模型测算。"),
      h("div", {class: "v2-actions"}, h("a", {class: "v2-btn", href: href({page: "settings", filters: {focus: "goal"}})}, "设定研究目标"))));
    if (ctx.state.filters.panel) openPanel(ctx, overview);
    return;
  }

  const goal = overview.goal;
  const about = h("details", {class: "v2-more"}, h("summary", null, `研究目标：${say(goal.display_title || goal.title)}`),
                  ...ui.paragraphs(say(goal.display_objective || goal.objective), ctx.seen));
  root.append(about);

  // 1. 研究结论: the four answers first, then the theses and each company.
  const conclusions = ui.section("conclusions", "研究结论");
  conclusions.className += " v2-hero";
  const top = headline(ctx, overview, nh.value);
  conclusions.append(h("dl", {class: "v2-kv v2-headline-kv"},
    h("dt", null, "核心投资判断"), h("dd", null, h("strong", null, top.view)),
    h("dt", null, "研判依据与主要变化"), h("dd", null, top.why),
    h("dt", null, "受影响标的"), h("dd", null, top.affected),
    h("dt", null, "待推进投研事项"), h("dd", null, top.next)));
  renderTheses(ctx, conclusions, overview);
  const tasks = tasksByCompany(nh.value);
  const rows = companies(overview).map(c => compareRow(c, ctx, tasks));
  conclusions.append(h("h3", null, "各标的当前研判"));
  conclusions.append(h("ul", {class: "v2-list v2-judgements"}, rows.map(r => {
    const j = r.judgement;
    // O07: no view is said once, plainly; 「维持」 only when there is a view.
    const viewText = r.view ? `基准情景：${baselineContent(r.view)}` : null;
    return h("li", {class: "v2-card"},
      h("h3", null, h("a", {href: href({page: "company", company: r.key}), "aria-label": `打开 ${r.name} 的公司研究`}, r.name)),
      h("p", null, viewText || h("span", {class: "v2-note"}, "尚未建立正式投资观点（正在搜集公司报表与研报资料）")),
      j && (r.view || j.action !== "no_change") ? h("p", {class: "v2-note"}, `最新研判（${fmt.date(j.at)}）：${say(j.decision_label || j.action_label)}。`,
        j.because ? h("details", {class: "v2-more"}, h("summary", {"aria-label": `${r.name} 这次研判的论证逻辑`}, "查看论证逻辑与核心假设"), h("p", null, say(j.because))) : null) : null);
  })));
  root.append(conclusions);

  // 2. 重要变化: opinion changes first, new material folded (O03/O04).
  const all = importantChanges(overview, ctx);
  const changes = all.filter(x => x.group === "view").slice(0, 6);
  const material = all.filter(x => x.group === "material");
  const changeSection = ui.section("changes", "近期重大异动与增量信息");
  const changeCard = x => h("li", {class: "v2-card"},
    h("div", {class: "v2-card-meta"}, ui.tag(x.kind), h("span", null, companyName(x.company)), x.at ? h("span", null, fmt.datetime(x.at)) : null),
    h("p", null, x.text, x.detail ? ` ${lead(say(x.detail), 140)}` : ""),
    h("a", {href: href({page: "company", company: companyKey(x.company), tab: x.tab}), "aria-label": `查看 ${companyName(x.company)} 的${x.kind}：${lead(x.text, 30)}`}, BTN.view_change));
  if (changes.length) changeSection.append(h("h3", null, "重大观点与股价异动"), h("ul", {class: "v2-list"}, changes.map(changeCard)));
  else {
    const last = lastJudgement(overview);
    changeSection.append(ui.empty(last
      ? `当前记录中暂无重大变化。最新研判：${companyName(last.company)}，${fmt.datetime(last.at)}，${say(last.decision_label || "")}。`
      : "尚无已完成的事件研判，暂不能确认是否出现影响投资观点的重大变化。"));
  }
  if (material.length) changeSection.append(h("details", {class: "v2-more"}, h("summary", null, `新入库事实纪要：已入库基础事实与研报摘录（共 ${material.length} 篇）`),
    h("ul", {class: "v2-list"}, material.slice(0, 20).map(changeCard))));
  root.append(changeSection);

  // 3. 待我决定
  renderDecisions(root, ctx, nh);

  // The headline block: the chart widget, or four tiles.
  const headerSlot = h("div", {class: "v2-headline"});
  root.append(headerSlot);
  await ctx.slot("overview.header", headerSlot, {overview, needsHuman: nh.value}, el => {
    const list = companies(overview);
    const reports = list.reduce((n, c) => n + (c.feedback?.total ? 1 : 0), 0);
    const actionable = nh.value ? sortTasks(foldTasks(nh.value.items || [])).filter(analystAction).length : null;
    el.append(h("div", {class: "v2-stats"}, [
      ["覆盖公司", `${list.length} 家`],
      [NAV.tasks, actionable == null ? "暂时读不到" : `${actionable} 项`],
      ["观点调整", `${all.filter(x => x.kind === "观点调整").length} 项`],
      ["已审阅的研究报告", `${reports} 家公司`],
    ].map(([label, value]) => h("div", {class: "v2-stat"}, h("span", {class: "v2-stat-n"}, value), h("span", {class: "v2-stat-l"}, label)))));
  });

  // 4. 公司比较: one ranking, from the comparison payload.
  const compare = ui.section("compare", LABELS.comparison.title);
  const holder = h("div");
  compare.append(holder);
  root.append(compare);
  // One comparison, one ranking rule: company_compare.js (O01/O13).
  await ctx.slot("overview.compare", holder, {overview, rows}, el => comparisonSection(ctx, el, {overview, needs: nh.value}));

  // 5. 研究记录: the weekly review and the step-by-step record open in the
  // side panel (stable URLs: ?panel=reflection / ?panel=trajectory).
  const keep = {...ctx.state.filters};
  delete keep.panel; delete keep.kind;
  const records = ui.section("records", "研究记录",
    h("ul", {class: "v2-list v2-records"},
      h("li", {class: "v2-card"}, h("h3", null, h("a", {href: href({filters: {...keep, panel: "reflection"}})}, "本周小结")),
        h("p", {class: "v2-note"}, "这一周的判断跟踪结果，以及下周值得研究的问题。"),
        overview.ops?.acceptance?.note ? h("p", {class: "v2-note"}, `上周已完成研究的验收：${say(overview.ops.acceptance.note)}`) : null),
      h("li", {class: "v2-card"}, h("h3", null, h("a", {href: href({filters: {...keep, panel: "trajectory"}})}, "研究过程记录")),
        h("p", {class: "v2-note"}, "系统最近读了什么、做了哪些研究，以及为什么。"))));
  root.append(records);
  if (ctx.state.filters.panel) openPanel(ctx, overview);
}

/* The verb a decision card offers (MUSE 第二批): what the reader will do. */
const VERBS = {decision: BTN.decision, material: BTN.material, service: BTN.service, processing: BTN.processing};

function renderDecisions(root, ctx, nh) {
  const {h, ui, fmt, say, href} = ctx;
  const section = ui.section("decide", "重点待办决策");
  if (nh.error) section.append(ui.state({tone: "danger", status: "待办暂时无法更新。", impact: "这里可能缺少新的待办。",
    owner: "系统会自动恢复。", next: `打开「${NAV.tasks}」查看上次结果。`, actions: [{label: `打开${NAV.tasks}`, href: href({page: "tasks"}), variant: "secondary"}]}));
  else {
    const items = sortTasks(foldTasks(nh.value.items || []).filter(analystAction));
    const groupTitle = id => (GROUPS.find(g => g.id === id) || {}).title || "";
    if (!items.length) section.append(ui.empty(`${LABELS.shell.blocked_none}。`));
    else {
      section.append(h("ul", {class: "v2-list"}, items.slice(0, 3).map(it => {
        const k = kindOf(it);
        const verb = it.kind === "publication_parked" ? "查看事项与进展" : VERBS[k.group] || "去处理";
        return h("li", {class: "v2-card"},
          h("div", {class: "v2-card-meta"}, ui.tag(groupTitle(k.group)), it.at ? h("span", null, fmt.datetime(it.at)) : null),
          h("p", null, say(it.title)), h("p", {class: "v2-note"}, `${BTN.suggest}：${k.suggest(it.detail || {})}`),
          h("div", {class: "v2-actions"}, h("a", {class: "v2-btn v2-btn-secondary", href: href({page: "tasks"}), "aria-label": `${verb}：${say(it.title)}`}, verb)));
      })));
      section.append(h("p", null, h("a", {href: href({page: "tasks"})}, label("decision_button", "all_tasks", {n: items.length}))));
    }
  }
  root.append(section);
}

async function update(ctx) {
  // The side panel follows ?panel= / ?kind= without rebuilding the page.
  const f = ctx.state.filters, p = ctx.prev.filters || {};
  const changed = [...new Set([...Object.keys(f), ...Object.keys(p)])].filter(k => f[k] !== p[k]);
  if (ctx.prev.page === "overview" && changed.length && changed.every(k => k === "panel" || k === "kind")) {
    const ov = await load(ctx, OVERVIEW);
    await syncPanel(ctx, ov.value);
    return true;
  }
  // Only the sort changed: redraw the comparison in place.
  if (!changed.every(k => k === "sort" || k === "dir")) return false;
  const holder = globalThis.document?.getElementById("compare");
  if (!holder || ctx.prev.page !== "overview") return false;
  const table = holder.querySelector("[data-slot-fallback]");
  if (!table) return false;
  const [ov, nh] = await Promise.all([load(ctx, OVERVIEW), load(ctx, NEEDS_HUMAN)]);
  await comparisonSection(ctx, table, {overview: ov.value, needs: nh.value});
  return true;
}

V().registerPage("overview", {title: NAV.overview, nav: {label: NAV.overview, order: 10}, render, update});
