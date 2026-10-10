// Profit bridge: revenue -> operating income -> pre-tax -> net income, one
// filed period and one period basis at a time (单季 / 完整财年 / 年初至今累计
// are never mixed). Every bar opens the statement line it came from.
//
// Form (dataviz): additive steps between filed anchors -> waterfall.
import { el, metaLine, notes, card, emptyState, widthOf, tableTwin, statusTag, clear, signed } from "./core.mjs";
import { waterfall, waterfallKey } from "./waterfall.mjs";
import { unitHeader } from "./labels.js";

const TITLE = "营业利润同比变动桥接分析（利润桥）";

function statementPanel(panel, step, host) {
  clear(panel);
  const s = step.statement;
  if (!s) {
    panel.appendChild(el("p", {text: `${step.short_label || step.label}（推算）：由「${step.derived_from || "上下两条申报行"}」相减得到，没有单独的报表行。`}));
    return;
  }
  const facts = [
    ["科目", s.concept_label || s.label_as_filed || "—"],
    ["报表原名", s.label_as_filed],
    ["申报原值", s.filed_value_display],
    ["期间", s.period],
    ["申报文件", [s.form, s.accession].filter(Boolean).join(" · ")],
    ["申报日", s.filed],
  ].filter(([, v]) => v);
  panel.appendChild(el("h4", {class: "dv2c-subtitle", text: `报表行：${step.short_label || step.label}`}));
  panel.appendChild(el("dl", {class: "dv2c-facts"}, facts.map(([k, v]) => el("div", null, el("dt", {text: k}), el("dd", {text: v})))));
  // The page opens the exact filed line and its original filing (C13): the
  // line id travels, not a text to search for.
  const detail = {ref: s.ref, line_id: s.line_id, concept: s.concept, accession: s.accession,
                  label: step.short_label || step.label};
  if (typeof CustomEvent === "function" && host && host.dispatchEvent) {
    host.dispatchEvent(new CustomEvent("dalton:statement-line", {bubbles: true, detail}));
  }
}

export function buildBridge(payload, container, options = {}) {
  if (!payload || payload.status !== "ok") return emptyState(TITLE, payload);
  const width = widthOf(container);
  const b = payload.bridge;
  const unit = b.unit;
  const divisor = Number(b.divisor) || 1;
  const clickable = b.steps.some((s) => s.statement);
  const panel = el("div", {class: "dv2c-panel", "aria-live": "polite"},
    el("p", {class: "dv2c-hint", text: clickable
      ? "带下划线和 › 的柱子可以点击（或用 Tab 选中后按回车）：点击查看报表行，并打开对应的原始申报。推算柱没有单独的报表行。"
      : "这一期的柱子都是推算值，没有可以打开的报表行。"}));
  const host = el("div");
  const rows = b.steps.map((s) => {
    const short = s.short_label || s.label;
    return {
      label: s.derived ? `${short}（推算：${s.derived_from || "相减得到"}）` : s.label,
      short: s.derived ? `${short}（推算）` : short,
      type: s.type === "anchor" ? "anchor" : "step", start: Number(s.start) / divisor, end: Number(s.end) / divisor,
      display: s.display, derived: s.derived, status: "ok",
      tipRows: s.statement ? [{label: s.statement.concept_label || s.statement.label_as_filed || ""},
                              {label: [s.statement.form, s.statement.filed ? `申报日 ${s.statement.filed}` : ""].filter(Boolean).join(" · ")},
                              {label: "点击查看报表行"}]
        : [{label: `推算：${s.derived_from || "由申报行相减"}`}],
      onClick: s.statement ? () => statementPanel(panel, s, host) : null,
    };
  });
  // period basis switch: one semantics at a time, then the period within it
  const controls = el("div", {class: "dv2c-controls"});
  if (options.reload) {
    const sem = el("div", {class: "dv2c-seg", role: "group", "aria-label": "期间口径"},
      (payload.semantics_options || []).map((o) => el("button", {type: "button", class: "dv2c-seg-btn",
        "aria-pressed": String(o.value === b.semantics), text: o.label,
        onclick: () => { const first = payload.options.find((x) => x.semantics === o.value); if (first) options.reload({bridge: first.ref}); }})));
    const same = payload.options.filter((o) => o.semantics === b.semantics);
    const pick = el("select", {class: "dv2c-select", "aria-label": "选择报告期"},
      same.map((o) => el("option", {value: o.ref, selected: o.ref === b.ref, text: `${o.fiscal_label || ""} · ${o.period}`})));
    pick.addEventListener("change", () => options.reload({bridge: pick.value}));
    controls.appendChild(sem);
    controls.appendChild(el("label", {class: "dv2c-control"}, "报告期 ", pick));
  }
  const checks = el("ul", {class: "dv2c-checks"}, (b.checks || []).map((c) => el("li", null,
    statusTag(c.status, c.status_label), ` ${c.label}`, c.residual_display && c.status !== "complete" ? `：差额 ${c.residual_display}` : "",
    c.missing && c.missing.length ? `（缺：${c.missing.join("、")}）` : "")));
  host.appendChild(waterfall({rows, width, tick: (t) => signed(t, Number.isInteger(t) ? 0 : 1, ""),
    ariaLabel: `${payload.company || ""} ${payload.period.label} 利润桥，单位${unit}`}));
  const twin = tableTwin("利润桥数据", ["项目", unitHeader(payload.unit_header || unit), "报表行"],
    b.steps.map((s) => [s.derived ? `${s.short_label || s.label}（推算）` : (s.short_label || s.label), s.display,
      s.statement ? [s.statement.concept_label || s.statement.label_as_filed, s.statement.form].filter(Boolean).join(" · ") : `推算：${s.derived_from || "相减"}`]));
  return card(TITLE, payload,
    metaLine(payload, [["勾稽", b.status_label]]),
    controls, waterfallKey("申报合计行（营业收入、营业利润、税前利润、净利润）"), host, checks, panel, twin,
    b.missing && b.missing.length ? el("p", {class: "dv2c-hint", text: `这一期缺的报表行：${b.missing.join("、")}`}) : null,
    notes(payload));
}
