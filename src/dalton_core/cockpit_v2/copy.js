/* Cockpit v2 copy (ADR-0016; batch am 2026-10-09).

   Every table here comes from the one terminology registry,
   src/dalton_core/terminology.py, through the generated module
   charts/vocabulary.js -- never edit a label here; edit the registry and run
   `python -m dalton_core.terminology --write` (tests/test_terminology.py fails
   when the two differ).  The server applies the same rows with
   terminology.scrub(), so server and page say the same words.

   COPY_TABLE carries every row of docs/reports/cockpit-analyst-ux-review-
   2026-10-08.md section 8 verbatim (current wording, suggested wording, note),
   batch ah's rows and batch am's (the 10-09 text evaluation, chapter 10), plus
   how v2 applies each:
     rules   -- replacements copy() makes on text the server wrote;
     mode    -- "replace" (rules apply), "keep" (deliberately unchanged),
                "format" (done by DaltonV2.fmt), "structure" (done by the page
                layout), "remove" (the sentence is dropped from analyst pages),
                "page" (the page never offers that wording / control).
   copy() is idempotent.  Finance terms (EPS, ROIC, CapEx, 10-K ...) are kept
   and glossed on first use per page (GLOSSARY / glossSegments). */

import {LABELS, COPY_TABLE_DATA, BANNED_TERMS} from "./charts/vocabulary.js";

export {LABELS, BANNED_TERMS};

/* US-GAAP concepts the server may still name (financial 5.3). */
export const CONCEPT_LABELS = LABELS.statement_line;

/* A lookup replacement: {template: "{1}（{2}）", lookup: {"1": namespace}} --
   each group looked up in the registry; an unknown one leaves the match. */
const lookup = replace => (whole, ...groups) => {
  let out = replace.template;
  for (const [group, namespace] of Object.entries(replace.lookup)) {
    const shown = (LABELS[namespace] || {})[groups[Number(group) - 1] ?? ""];
    if (shown == null) return whole;
    out = out.split(`{${group}}`).join(shown);
  }
  return out;
};

const toRule = ([find, replace]) => [
  typeof find === "string" ? find : new RegExp(find.re, find.flags || ""),
  replace && typeof replace === "object" && replace.lookup ? lookup(replace) : replace,
];

export const COPY_TABLE = COPY_TABLE_DATA.map(row => ({...row, rules: row.rules.map(toRule)}));

/* Finance terms analysts know: kept, glossed once per page. */
export const GLOSSARY = LABELS.glossary;

/* Valuation metric names the server still sends in English (M07). */
export const METRIC_LABELS = LABELS.metric;

/** "{n} 项" templates of the registry, filled. */
export function fill(template, values = {}) {
  return String(template ?? "").replace(/\{(\w+)\}/g, (whole, key) => (key in values ? String(values[key]) : whole));
}

/** One registry label: label("comparison", "bear"), with {name} values. */
export function label(namespace, key, values) {
  const text = (LABELS[namespace] || {})[key];
  return text == null ? null : fill(text, values);
}

/** A metric's Chinese name; an unknown key never shows raw (G10). */
export function metricLabel(key, fallback) {
  if (key && METRIC_LABELS[key]) return METRIC_LABELS[key];
  const text = typeof fallback === "string" ? copy(fallback) : "";
  return text && /[㐀-鿿]/.test(text) ? text : "暂无中文名称的指标";
}

/** A closed-vocabulary value shown by its label; an unknown value is
    「状态未知」 and the code goes to the technical details (G10). */
export function enumText(labels, value) {
  const key = value == null ? "" : String(value);
  if (labels && Object.prototype.hasOwnProperty.call(labels, key)) return {text: labels[key], known: true, code: key};
  return {text: "状态未知", known: false, code: key || "（空）"};
}

/* Debate-map sections arrive as "<question> · <side>" (O05). */
export const DEBATE_ROLES = LABELS.debate_role;

/* The shell's own words, in one place (the registry's "shell" / "nav"). */
const S = LABELS.shell;
export const UI = {
  product: S.product,
  nav: LABELS.nav,
  project: S.project,
  // 2026-10-09 (MUSE P0 #4, G06; am 2.1): plain words with the impact.
  serviceStarting: S.service_starting,
  serviceStartingHelp: S.service_starting_help,
  serviceOnline: S.service_online,
  serviceOnlineSince: ago => fill(S.service_online_since, {ago}),
  serviceOnlineHelp: S.service_online_help,
  serviceOffline: S.service_offline,
  serviceOfflineHelp: since => fill(S.service_offline_help, {since}),
  serviceDegraded: (n, linked) => fill(linked ? S.service_degraded : S.service_degraded_plain, {n}),
  serviceDegradedHelp: (ago, lanes) => fill(S.service_degraded_help, {ago, lanes}),
  serviceUnknown: S.service_unknown,
  awaitingMission: S.service_awaiting_mission,
  awaitingMissionHelp: S.service_awaiting_mission_help,
  blocked: n => (n ? fill(S.blocked, {n}) : S.blocked_none),
  blockedUnknown: S.blocked_unknown,
  badge: n => fill(S.badge, {n}),
  updated: at => fill(S.updated, {at}),
  refresh: S.refresh,
  refreshing: S.refreshing,
  refreshed: S.refreshed,
  fresh: S.fresh,
  freshAction: S.fresh_action,
  closePanel: S.close_panel,
  backTo: title => fill(S.back_to, {title}),
  readFailed: S.read_failed,
  noView: S.no_view,
  cannotCompare: S.cannot_compare,
  backToCompany: S.back_to_company,
  tryNew: S.try_new,
  technical: S.technical,
  retryLater: S.retry_later,
};

const RULES = COPY_TABLE.flatMap(row => row.rules);

/* Machine-precision numbers as a reader says them (batch am; the twin of
   numeric_display.humanize_machine_numbers -- tests/test_terminology.py
   checks they agree): 「0.577264（即 57.73%）」 -> 「57.73%」; more than four
   decimals rounded; seven-digit-plus integers scaled to 万 / 亿.  Identifiers
   (accession numbers, refs, CIKs, eight-digit dates) are left alone. */
const RATIO_GLOSS = /(?<![A-Za-z0-9_.,])-?\d+\.\d{4,}\s*[（(]\s*即\s*(-?\d+(?:\.\d+)?)\s*%\s*[）)]/g;
const MACHINE_NUMBER = /(?<![A-Za-z0-9_.,:/\-])(-?(?:[1-9]\d{0,2}(?:,\d{3}){2,}|[1-9]\d{6,}|\d+\.\d{5,}|\d{5,}\.\d+))(?![A-Za-z0-9_.,:/\-])(\s*[万亿千百])?(\s*(?:美元|USD\b))?/g;

function roundHalfUpText(value, places) {
  const sign = value < 0 ? "-" : "";
  const n = Math.abs(value);
  const shifted = String(n).includes("e") ? n * 10 ** places : Number(`${n}e${places}`);
  const scaled = Math.round(shifted);
  const text = (scaled / 10 ** places).toFixed(places);
  return Number(text) === 0 ? text : sign + text;
}
function exponent(n) { return Math.floor(Math.log10(Math.abs(n))); }
function scaledAmount(value, divisor, label) {
  const v = value / divisor;
  const places = Math.max(0, Math.min(2, 3 - exponent(v)));
  return `${roundHalfUpText(v, places)} ${label}`;
}
function dateLike(digits) {
  if (digits.length !== 8 || !["19", "20"].includes(digits.slice(0, 2))) return false;
  const m = Number(digits.slice(4, 6)), d = Number(digits.slice(6, 8));
  return m >= 1 && m <= 12 && d >= 1 && d <= 31;
}

/** Machine-precision numbers in one display fragment, as a reader says them. */
export function humanizeNumbers(text) {
  if (typeof text !== "string" || !text) return text;
  const out = text.replace(RATIO_GLOSS, (whole, pct) => `${pct}%`);
  return out.replace(MACHINE_NUMBER, (whole, raw, scale, unit) => {
    if (scale) return whole;
    const digits = raw.replace(/^-/, "").replace(/,/g, "");
    const intPart = digits.split(".")[0];
    if (!digits.includes(".") && dateLike(digits)) return whole;
    const value = Number(raw.replace(/,/g, ""));
    const money = unit ? "美元" : "";
    const magnitude = Math.abs(value);
    if (magnitude >= 1e8 && (intPart.length >= 7 || raw.includes(","))) return scaledAmount(value, 1e8, `亿${money}`);
    if (magnitude >= 1e4 && intPart.length >= 5) return scaledAmount(value, 1e4, `万${money}`);
    if (!digits.includes(".")) return whole;
    const shown = magnitude >= 1 ? roundHalfUpText(value, 2)
      : magnitude === 0 ? "0" : roundHalfUpText(value, Math.max(0, 3 - exponent(magnitude)));
    return shown + (unit || "");
  });
}

/** Apply the section-8 replacements to one server-written string. */
export function copy(text) {
  if (typeof text !== "string" || !text) return text;
  // Text inside 「」 is a name someone gave (a project, a question) or a
  // quotation; it is never rewritten.
  return text.split(/(「[^」]*」)/).map(part => {
    if (part.startsWith("「") && part.endsWith("」")) return part;
    let out = part;
    for (const [find, replace] of RULES) {
      if (typeof find === "string") out = out.split(find).join(replace);
      else out = out.replace(find, replace);
    }
    return humanizeNumbers(out);
  }).join("");
}

/** Split "<question> · <side>" into its parts; side is relabelled. */
export function splitDebateTitle(title) {
  const text = String(title || "");
  const at = text.lastIndexOf(" · ");
  if (at < 0) return {question: text, role: null};
  const side = text.slice(at + 3).trim();
  if (!(side in DEBATE_ROLES)) return {question: text, role: null};
  return {question: text.slice(0, at).trim(), role: DEBATE_ROLES[side]};
}

const TERM_PATTERN = new RegExp(
  "(" + Object.keys(GLOSSARY).sort((a, b) => b.length - a.length)
    .map(t => t.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")).join("|") + ")(?![A-Za-z0-9])", "g");

/** Split text into plain and glossed parts; `seen` (a Set) spans one page. */
export function glossSegments(text, seen) {
  const out = [];
  const value = String(text ?? "");
  let last = 0;
  for (const match of value.matchAll(TERM_PATTERN)) {
    const term = match[1], at = match.index;
    if (at > 0 && /[A-Za-z0-9]/.test(value[at - 1])) continue;
    if (seen && seen.has(term)) continue;
    if (at > last) out.push({text: value.slice(last, at)});
    out.push({text: term, term, gloss: GLOSSARY[term]});
    if (seen) seen.add(term);
    last = at + term.length;
  }
  if (last < value.length) out.push({text: value.slice(last)});
  return out;
}

/* Report section keys the server still sends raw (O05: real topics). */
export const SECTION_TITLES = LABELS.section_title;

export function sectionTitle(title) {
  const t = String(title || "");
  if (t in SECTION_TITLES) return SECTION_TITLES[t];
  const chain = /^causal_chain:(\d+)$/.exec(t);
  if (chain) return `核心争议议题 ${Number(chain[1]) + 1}`;
  return copy(t);
}
