// company.valuation -- bear / base / bull side by side with probability bars.
// company.scenarios -- every trigger and invalidator with its latest status.
//
// Form (dataviz): three rows of mixed text + one magnitude -> a table; the one
// magnitude (probability) gets an in-cell bar in a single hue (one series, so
// no legend box). The number is always printed beside the bar.
import { el, fmtDate, metaLine, notes, card, emptyState, missing, statusTag } from "./core.mjs";

const VALUATION_TITLE = "估值情景";
const STATUS_TITLE = "情景与触发条件";

function stateBanner(payload) {
  return el("p", {class: `dv2c-lead dv2c-state-${payload.state}`},
    el("strong", {text: payload.state_label}), payload.headline ? `：${payload.headline}` : "");
}

function probabilityBar(scenario) {
  const pct = Math.max(0, Math.min(100, Number(scenario.probability_pp) || 0));
  return el("div", {class: "dv2c-pbar", role: "img", "aria-label": `概率 ${scenario.probability_display}`},
    el("span", {class: "dv2c-pbar-track"}, el("span", {class: "dv2c-pbar-fill", style: `width:${pct}%`})),
    el("strong", {class: "dv2c-num", text: scenario.probability_display}));
}

function conditionCounts(scenario) {
  const by = (kind, status) => scenario.conditions.filter((c) => c.kind === kind && c.status === status).length;
  const triggers = scenario.conditions.filter((c) => c.kind === "trigger").length;
  const invalidators = scenario.conditions.filter((c) => c.kind === "invalidator").length;
  return `触发信号 ${by("trigger", "triggered")}/${triggers} 已出现；预警触发线 ${by("invalidator", "triggered")}/${invalidators} 已触发`;
}

export function buildValuation(payload) {
  if (!payload || payload.status !== "ok") return emptyState(VALUATION_TITLE, payload);
  const rows = payload.scenarios.map((s) => el("tr", null,
    el("th", {scope: "row"}, el("span", {class: "dv2c-badge", text: s.key_label}), el("div", {class: "dv2c-sname", text: s.name})),
    el("td", {class: "dv2c-pcell"}, probabilityBar(s)),
    el("td", null, el("details", {class: "dv2c-narr"}, el("summary", {text: s.linked_drivers.length ? `关键驱动：${s.linked_drivers.join("、")}` : "展开情景叙述"}), el("p", {text: s.narrative || "未写叙述"}))),
    el("td", null, s.target_label ? s.target_label : missing(s.target_missing_reason), s.target_label ? null : el("div", {class: "dv2c-hint", text: "未给出目标价或回报区间"})),
    el("td", {text: s.horizon_label}),
    el("td", {class: "dv2c-hint", text: conditionCounts(s)})));
  const sum = el("tr", {class: "dv2c-sum"},
    el("th", {scope: "row", text: "概率合计"}),
    el("td", {class: "dv2c-pcell"}, el("strong", {class: "dv2c-num", text: payload.probability_sum_display}),
      el("span", {class: "dv2c-hint", text: payload.probability_sum_ok ? " 合计为 100%" : " 合计不等于 100%（未归一化）"})),
    el("td", {colspan: 4}));
  const table = el("table", {class: "dv2c-table dv2c-scen"},
    el("caption", {text: "多情景推演对照（悲观 / 基准 / 乐观）"}),
    el("thead", null, el("tr", null, ["情景", "概率", "关键假设", "目标价 / 回报区间", "期限", "条件状态"].map((h) => el("th", {scope: "col", text: h})))),
    el("tbody", null, rows, sum));
  return card(VALUATION_TITLE, payload, metaLine(payload), stateBanner(payload),
    el("div", {class: "dv2c-scroll"}, table), notes(payload));
}

export function buildScenarioStatus(payload) {
  if (!payload || payload.status !== "ok") return emptyState(STATUS_TITLE, payload);
  const body = [];
  for (const s of payload.scenarios) {
    body.push(el("tr", {class: "dv2c-section"}, el("th", {scope: "rowgroup", colspan: 7},
      `${s.key_label}情景（${s.probability_display}）：${s.name || ""}`)));
    for (const c of s.conditions) {
      body.push(el("tr", null,
        el("td", {text: c.kind_label}),
        el("th", {scope: "row", title: c.rule_technical || ""}, el("div", {text: c.rule}), c.label && c.label !== c.rule ? el("div", {class: "dv2c-hint", text: c.label}) : null),
        el("td", {class: "num"}, c.value_label ? c.value_label : missing(c.reason_label || "还没有可读的数据"),
          c.value_as_of ? el("div", {class: "dv2c-hint", text: `截至 ${fmtDate(c.value_as_of)}`}) : null),
        el("td", {class: "num"}, c.threshold_label || "—"),
        el("td", null, c.distance_label || el("span", {class: "dv2c-hint", text: c.status === "triggered" ? "已满足" : "—"})),
        el("td", null, statusTag(c.status, c.status_label), c.reason_label ? el("div", {class: "dv2c-hint", text: c.reason_label}) : null),
        el("td", {text: c.observer_label || "—"})));
    }
  }
  const table = el("table", {class: "dv2c-table"},
    el("caption", {text: "每个情景的触发信号与预警触发线，按最新观察"}),
    el("thead", null, el("tr", null, ["类型", "条件（期限）", "最新值", "阈值", "距阈值", "状态", "观察方"].map((h, i) => el("th", {scope: "col", class: i === 2 || i === 3 ? "num" : null, text: h})))),
    el("tbody", null, body));
  const summary = el("p", {class: "dv2c-hint", text: `已出现的触发信号 ${payload.triggered || 0} 个；已触发的预警线（核心假设证伪预警） ${payload.invalidated || 0} 个；概率合计 ${payload.probability_sum_display}`});
  return card(STATUS_TITLE, payload, metaLine(payload), stateBanner(payload), summary,
    el("div", {class: "dv2c-scroll"}, table), notes(payload));
}
