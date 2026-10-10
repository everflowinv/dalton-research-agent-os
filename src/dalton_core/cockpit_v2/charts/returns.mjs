// company.returns -- the return-contribution waterfall, one per leg, in
// percentage points. Realised (事后归因) and expected (估算) legs sit in separate
// sections and are never drawn on one axis: they are different questions.
//
// Form (dataviz): additive contributions to a total -> waterfall; the job is
// polarity (above/below zero), so the diverging pair, with signs printed.
import { el, fmtDate, metaLine, notes, card, emptyState, widthOf, tableTwin, signed } from "./core.mjs";
import { waterfall, waterfallKey } from "./waterfall.mjs";
import { forecastBadges, glossTerm } from "./labels.js";
import { LABELS } from "./vocabulary.js";

const ATTR = LABELS.attribution;
const TITLE = "股价总回报归因（TSR Attribution）";
const SHORT = {earnings_growth: "盈利增长驱动", share_count: ATTR.shares, multiple_change: ATTR.multiple,
  price_return: "= 价格回报", dividends: ATTR.dividend, total_return: "= 总回报"};
const PP = / ?(个百分点|pp)$/;

function legChart(leg, width, seen) {
  const rows = leg.steps.map((s) => ({label: s.label, short: SHORT[s.key] || s.label, type: s.type,
    start: s.start_pp, end: s.end_pp, display: String(s.display || "").replace(PP, ""), status: s.status, missing_reason: s.missing_reason}));
  const facts = [
    ["窗口", leg.window && leg.window.label],
    ["口径", leg.basis_label],
    ["预期来源", leg.source_label],
    ["股价", leg.price_start ? `${leg.price_start} → ${leg.price_end || (leg.price_target ? `目标 ${leg.price_target}` : "（未来）")} 美元` : null],
    ["每股收益", leg.eps_start ? `${leg.eps_start} → ${leg.eps_end || "—"} 美元/股` : null],
  ].filter(([, v]) => v);
  return el("article", {class: `dv2c-leg${leg.estimated ? " dv2c-leg-estimated" : ""}`},
    el("h4", {class: "dv2c-subtitle"}, leg.label, el("span", {class: "dv2c-total-figure", text: `总回报 ${leg.total_display ?? "—"}`}),
      forecastBadges(leg.badges, {seen})),
    leg.estimated ? el("p", {class: "dv2c-flag", text: leg.simplified_label || "估算，不是已实现结果"}) : el("p", {class: "dv2c-flag dv2c-flag-ex", text: "事后财务归因：不是当时可得的决策依据"}),
    el("dl", {class: "dv2c-facts"}, facts.map(([k, v]) => el("div", null, el("dt", {text: k}), el("dd", {text: v})))),
    waterfall({rows, width, tick: (t) => signed(t, Number.isInteger(t) ? 0 : 1, ""), ariaLabel: `${leg.label}：总回报 ${leg.total_display ?? "—"}，单位：百分点（pp）`}),
    leg.total_display == null && (leg.missing_reason || leg.reason) ? el("p", {class: "dv2c-hint", text: leg.missing_reason || leg.reason}) : null,
    leg.rounding_residual_pp ? el("p", {class: "dv2c-hint", text: `各项与价格回报的四舍五入差额 ${leg.rounding_residual_pp} pp`}) : null,
    (leg.notes || []).length ? el("ul", {class: "dv2c-notes"}, leg.notes.map((n) => el("li", {text: n}))) : null,
    tableTwin(`${leg.label} 分解`, [ATTR.factor, ATTR.contribution], leg.steps.map((s) => [s.label, String(s.display || "").replace(PP, "")])));
}

export function buildReturns(payload, container) {
  if (!payload || payload.status !== "ok") return emptyState(TITLE, payload);
  const width = widthOf(container);
  const seen = new Set();
  const groups = [["realised", "已实现（事后财务归因）"], ["expected", "预期（估算，未来）"]];
  const sections = groups.map(([group, heading]) => {
    const legs = payload.legs.filter((l) => l.group === group);
    if (!legs.length) return null;
    return el("section", {class: "dv2c-group-block"}, el("h4", {class: "dv2c-group-title", text: heading}),
      legs.map((leg) => leg.status === "ok" ? legChart(leg, width, seen)
        : el("article", {class: "dv2c-leg"}, el("h4", {class: "dv2c-subtitle", text: leg.label}),
          el("p", {class: "dv2c-empty-inline", text: `暂无：${leg.missing_reason}`}))));
  });
  return card(TITLE, payload, metaLine(payload, [["归因记录", fmtDate(payload.data_as_of)]]),
    el("p", {class: "dv2c-unitrow"}, "单位：", glossTerm("百分点", seen, "百分点（pp）"), "，柱顶数值均为对总回报的贡献（总回报为 %）。各段的窗口和", glossTerm("口径", seen), "不同，不能互相比较大小。"),
    waterfallKey("小计 / 总回报"), sections, notes(payload));
}
