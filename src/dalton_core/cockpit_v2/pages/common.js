/* Shared reading of the /v1/cockpit payloads for the analyst pages, and the
   shared pieces other pages (company, charts, administrator) reuse:
   confirmDialog, glossSegments, collapsibleToc.  The company comparison has one
   implementation: company_compare.js (comparisonSection). */

import {confirmDialog} from "../shell.js";
import {glossSegments} from "../copy.js";

export {confirmDialog, glossSegments};

export const V = () => globalThis.window.DaltonV2;

export const OVERVIEW = "/v1/cockpit/overview";
export const NEEDS_HUMAN = "/v1/cockpit/needs-human";
export const APPROVALS = "/v1/cockpit/approvals";

export function companyKey(c) { return (c && (c.ticker || c.company_ref)) || ""; }

/** "Accenture ACN"; the ticker once when there is no other name (C02). */
export function companyName(c) {
  if (!c) return "未知公司";
  const ticker = c.ticker || "", name = (c.name || "").trim();
  if (!name || name === ticker) return ticker || c.company_ref || "未知公司";
  return ticker ? `${name} ${ticker}` : name;
}

export function companies(overview) { return Array.isArray(overview?.companies) ? overview.companies : []; }

/** Every thesis version split into the current one per thesis and the
    superseded rest (2026-10-09 P0: two contradictory versions side by side).
    The server's ``theses_current`` is the guard when present; older payloads
    without ``current`` mark the last row of each ref current. */
export function thesisVersions(overview) {
  const all = Array.isArray(overview?.theses) ? overview.theses : [];
  const last = new Map();
  all.forEach((t, i) => last.set(t.ref, i));
  const marked = all.map((t, i) => ({...t, current: typeof t.current === "boolean" ? t.current : last.get(t.ref) === i}));
  const guard = Array.isArray(overview?.theses_current) ? overview.theses_current.filter(t => t.current !== false) : null;
  const current = guard ? guard.map(t => ({...t, current: true})) : marked.filter(t => t.current);
  const key = t => `${t.ref}|${t.version_ref || t.version || ""}`;
  const shown = new Set(current.map(key));
  return {current, history: marked.filter(t => !t.current && !shown.has(key(t)))};
}

export function findCompany(overview, key) {
  if (!key) return null;
  const upper = String(key).toUpperCase();
  return companies(overview).find(c => (c.ticker || "").toUpperCase() === upper || c.company_ref === key) || null;
}

/** Checklist items whose acquisition step the server spelled out
    (checklist[].period_step, e.g. a source's daily quota and when it
    resumes).  `blocking` keeps only items not yet complete. */
export function periodSteps(c, {blocking = false} = {}) {
  return (Array.isArray(c?.checklist) ? c.checklist : [])
    .filter(x => x && typeof x.period_step === "string" && x.period_step.trim())
    .filter(x => !blocking || x.status !== "complete")
    .map(x => ({label: x.label || x.item_ref || "资料", note: x.note || "", step: x.period_step.trim(), status: x.status}));
}

export function hasMission(overview) { return Boolean(overview && overview.goal); }

const leg = (c, name) => (c?.return_attribution?.returns?.legs || []).find(l => l.leg === name) || null;
export const returnLeg = leg;

/** True when a return leg rests on a stated simplification (R01). */
export function isEstimate(l) {
  return Boolean(l && (l.notes || []).some(n => /代理口径|持平|简化|不假设/.test(String(n))));
}

/* The three comparisons the review asks for (H03 / section 7.2), each either a
   number on a stated basis or "cannot compare" with the reason. */
export function compareRow(c, ctx, tasksByCompany = new Map()) {
  const {fmt, say} = ctx;
  const expected = leg(c, "expected_driver_model");
  const ret = expected && expected.status === "available" && expected.total_return_percent != null
    ? {value: Number(expected.total_return_percent), text: fmt.change(expected.total_return_percent),
       note: `${fmt.date(expected.window?.start)} 至 ${fmt.date(expected.window?.end)}${expected.contradiction ? "；" + say(expected.contradiction) : ""}`,
       estimate: isEstimate(expected), contradiction: expected.contradiction || null}
    : {value: null, reason: expected ? say(expected.reason || "驱动模型尚未算出预期回报") : "尚无驱动模型预期回报"};
  const month = (c.market?.changes || []).find(x => x.horizon === "30D" || x.days === 30);
  const change = month && month.status === "available" && month.percent != null
    ? {value: Number(month.percent), text: fmt.change(month.percent), note: `${say(month.label || "近 30 个交易日")}，截至 ${fmt.date(c.market?.as_of)}`}
    : {value: null, reason: month ? say(month.reason || "股价数据不足") : "尚无股价数据"};
  const scenarios = c.scenarios?.in_force || [];
  const bear = scenarios.find(s => s.key === "bear");
  const risk = bear && bear.probability != null
    ? {value: Number(bear.probability), text: fmt.probability(bear.probability),
       note: `发生概率 · ${bear.target_return_percent != null ? `潜在跌幅 ${fmt.change(bear.target_return_percent)}` : "潜在跌幅待测算"}`}
    : {value: null, reason: scenarios.length ? "情景中没有悲观情景" : "尚未建立情景"};
  const base = scenarios.find(s => s.key === "base");
  const judgement = (c.judgements?.latest || [])[0] || null;
  return {
    company: c, key: companyKey(c), name: companyName(c),
    view: base ? say(base.name) : null,
    judgement,
    ret, change, risk,
    next: c.catalyst ? `${say(c.catalyst.event_label || "下一事件")} ${fmt.date(c.catalyst.expected_date)}${c.catalyst.date_unconfirmed ? "（日期未确认）" : ""}` : null,
    tasks: tasksByCompany.get(c.company_ref) || 0,
  };
}

export function tasksByCompany(needs) {
  const map = new Map();
  for (const it of needs?.items || []) {
    const ref = it.detail?.company_ref;
    if (ref && it.actionable !== false) map.set(ref, (map.get(ref) || 0) + 1);
  }
  return map;
}

/** A load that fails is shown as such, never as "nothing" (D08). */
export async function load(ctx, path, options) {
  try { return {value: await ctx.api(path, options), error: null}; }
  catch (error) { return {value: null, error}; }
}

/** Short first sentence(s) of a long text, for summaries. */
export function lead(text, max = 160) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  const stop = Math.max(cut.lastIndexOf("。"), cut.lastIndexOf("；"));
  return (stop > max * 0.4 ? cut.slice(0, stop + 1) : cut) + "…";
}

/** English-only text the server has no reviewed translation for. */
export function untranslated(text) {
  const t = String(text || "");
  return t.length > 0 && !/[㐀-鿿]/.test(t) && /[A-Za-z]{3,}/.test(t);
}

/** Show a server string: reviewed translation / display field, copy rules;
    English with no translation is labelled as the original. */
export function showText(ctx, raw, display) {
  const {h, say} = ctx;
  // A server display field is final text, but still read in the registry's
  // words (batch am: machine numbers, banned terms).
  const shown = display ? (ctx.copy ? ctx.copy(display) : display) : say(raw);
  if (untranslated(shown)) return h("span", null, h("span", {class: "v2-tag"}, "原文（英文）"), " ", h("span", {lang: "en"}, shown));
  return h("span", null, shown);
}

/** A server message without its closing punctuation, to quote inside a sentence. */
export function said(err, fallback = "请稍后重试") {
  return String((err && err.display_message) || fallback).trim().replace(/[。．.！!；;，,\s]+$/u, "");
}


/* ---------------------------------------------------------- 2026-10-09 -- */

/** Confirm through the page's ui (MUSE P0 #2); see shell confirmDialog. */
export function confirmAction(ctx, options) { return ctx.ui.confirm(options); }

/** An internal reference (hash, ref, accession code) that must not be the
    text an analyst reads (O03 / C19 / financial 5.2). */
export function isInternalRef(text) {
  const t = String(text || "").trim();
  return /^[a-z][a-z0-9-]*(?::[A-Za-z0-9._-]+)+$/.test(t) || /sha256:[0-9a-f]{6,}/.test(t) || /^[0-9a-f]{32,64}$/.test(t);
}

/** What an internal reference is, in words, when nothing better is known. */
export function refLabel(text) {
  const t = String(text || "");
  if (/^sec:filing:/.test(t)) return "新增 SEC 官方申报（内容解析中）";
  if (/transcript|earnings-call/i.test(t)) return "新增电话会纪要（内容解析中）";
  if (/guidepoint|expert/i.test(t)) return "新增专家访谈纪要（内容解析中）";
  if (/alphaengine|broker|research-note/i.test(t)) return "新增券商研报摘录（内容解析中）";
  if (/public-web|web-url|news/i.test(t)) return "新增公开网页资料（内容解析中）";
  if (/^calendar:/.test(t)) return "催化事件日程更新";
  return "新增研究资料（内容解析中）";
}

/** The reason an analyst action records when the analyst wrote none: the
    action and where it was taken, in words (reasons are optional for
    non-destructive actions; the writer still stores one). */
export function autoReason(page, action, detail = "") {
  return `分析师在${page}${action}${detail ? `：${detail}` : ""}`.slice(0, 600);
}

/** Make a page's table of contents collapsible (G04): adds the toggle, keeps
    the choice per browser.  `toc` is the .v2-toc element. */
export function collapsibleToc(ctx, toc, {key = "company"} = {}) {
  const {h} = ctx;
  const storeKey = `dalton.v2.toc-collapsed:${key}`;
  toc.classList?.add("v2-toc--collapsible");
  const button = h("button", {type: "button", class: "v2-toc-toggle", "aria-expanded": "true"},
    h("span", {"aria-hidden": "true"}, "☰"), h("span", {class: "v2-toc-toggle-text"}, "收起目录"));
  const set = on => {
    toc.setAttribute("data-collapsed", on ? "true" : "false");
    button.setAttribute("aria-expanded", on ? "false" : "true");
    button.setAttribute("aria-label", on ? "展开目录" : "收起目录");
    button.lastChild.textContent = on ? "展开目录" : "收起目录";
    try { globalThis.localStorage?.setItem(storeKey, on ? "1" : "0"); } catch (_e) { /* private mode */ }
  };
  button.addEventListener("click", () => set(toc.getAttribute("data-collapsed") !== "true"));
  toc.prepend(button);
  let held = false;
  try { held = globalThis.localStorage?.getItem(storeKey) === "1"; } catch (_e) { held = false; }
  set(held);
  return button;
}
