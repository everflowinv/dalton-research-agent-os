// company.model -- revenue and margin, history vs forecast, then the real
// financial table (semantic <table>, sticky line names, right-aligned figures).
//
// Form (dataviz): change over time -> line. Revenue (亿美元) and margin (%) have
// different scales, so they are two charts (small multiples), never one chart
// with two y-axes. One series per chart -> no legend box; a key explains the
// marker/line encodings (disclosed / derived / estimate).
import {
  el, svg, num, fmtDate, ticks, scale, widthOf, metaLine, notes, card, emptyState,
  missing, tooltip,
} from "./core.mjs";
import { unitHeader } from "./labels.js";

const TITLE = "财务模型";
const BASIS_LABEL = {
  reported: "已披露", derived: "推导（累计值相减）", derived_formula: "推导（模型结构公式）",
  estimate: "预测（估算）", actual: "实际披露（预测期）",
  preliminary: "初步实际（8-K），待 10-Q/10-K 核对",
};

// 方法 column: how a forecast row is produced (驱动 / 占收入比 / 公式 / 延续 /
// 一次性归零 / 不可预测+原因); the reason and the assumption source sit in the
// cell's title.
export function methodCell(row) {
  const method = row.method || null;
  if (!method) return el("td", {class: "dv2c-method", text: "—"});
  const title = [method.reason, row.source ? `来源：${row.source}` : null].filter(Boolean).join("；");
  const unavailable = method.code === "unavailable";
  return el("td", {class: `dv2c-method${unavailable ? " dv2c-method-na" : ""}`, title: title || method.label},
    method.label,
    unavailable && method.reason ? el("span", {class: "dv2c-method-reason", text: `：${method.reason}`}) : null);
}

export function lineChart(spec) {
  const {points, columns, boundary, width, unitLabel, title, format} = spec;
  const height = 200, padL = 56, padR = 16, padT = 26, padB = 30;
  const values = points.filter((p) => p.v !== null).map((p) => p.v);
  const box = el("div", {class: "dv2c-chart"});
  box.appendChild(el("h4", {class: "dv2c-subtitle", text: `${title}（${unitLabel}）`}));
  if (!values.length) {
    box.appendChild(el("p", {class: "dv2c-hint", text: spec.emptyReason || "没有可画的数据"}));
    return box;
  }
  let lo = Math.min(...values), hi = Math.max(...values);
  if (spec.zeroBased && lo > 0) lo = 0;
  const yt = ticks(lo, hi, 4);
  const y = scale(yt[0], yt[yt.length - 1], height - padB, padT);
  const band = (width - padL - padR) / Math.max(1, columns.length);
  const x = (i) => padL + band * (i + 0.5);
  const root = svg("svg", {class: "dv2c-svg", width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `${title}：${columns.length} 个季度，其中已披露 ${boundary} 个、预测 ${columns.length - boundary} 个`});
  if (boundary < columns.length) {
    const bx = padL + band * boundary;
    root.appendChild(svg("rect", {class: "dv2c-forecast-wash", x: bx, y: padT - 18, width: width - padR - bx, height: height - padB - padT + 18}));
  }
  for (const t of yt) {
    root.appendChild(svg("line", {class: "dv2c-grid", x1: padL, x2: width - padR, y1: y(t), y2: y(t)}));
    root.appendChild(svg("text", {class: "dv2c-tick", x: padL - 6, y: y(t) + 4, "text-anchor": "end", text: format(t)}));
  }
  const every = Math.max(1, Math.ceil(columns.length / Math.max(2, Math.floor((width - padL) / 54))));
  columns.forEach((c, i) => {
    const lastFits = (columns.length - 1) % every === 0 || (columns.length - 1) - Math.floor((columns.length - 1) / every) * every >= Math.ceil(every / 2) + 1;
    if (i % every === 0 || (i === columns.length - 1 && lastFits)) {
      root.appendChild(svg("text", {class: "dv2c-tick", x: x(i), y: height - 10, "text-anchor": "middle", text: c.label}));
    }
  });
  if (boundary > 0 && boundary < columns.length) {
    const bx = padL + band * boundary;
    root.appendChild(svg("line", {class: "dv2c-boundary", x1: bx, x2: bx, y1: padT - 18, y2: height - padB}));
    root.appendChild(svg("text", {class: "dv2c-zone", x: bx - 6, y: padT - 6, "text-anchor": "end", text: "已披露历史"}));
    root.appendChild(svg("text", {class: "dv2c-zone", x: bx + 6, y: padT - 6, text: "预测（估算）"}));
  }
  // solid history, dashed forecast; a missing value breaks the line (never a zero)
  const segment = (from, to, cls) => {
    let d = "", pen = false;
    for (let i = from; i <= to; i++) {
      const p = points[i];
      if (!p || p.v === null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`;
      pen = true;
    }
    if (d) root.appendChild(svg("path", {class: cls, d}));
  };
  segment(0, boundary - 1, "dv2c-line");
  segment(Math.max(0, boundary - 1), columns.length - 1, "dv2c-line dv2c-line-forecast");
  points.forEach((p, i) => {
    if (p.v === null) return;
    const hollow = p.basis !== "reported" && p.basis !== "actual";
    root.appendChild(svg("circle", {class: hollow ? "dv2c-dot dv2c-dot-hollow" : "dv2c-dot", cx: x(i), cy: y(p.v), r: 4}));
  });
  const lastHistory = [...points.slice(0, boundary)].reverse().find((p) => p.v !== null);
  const lastForecast = [...points].reverse().find((p, j) => p.v !== null && points.length - 1 - j >= boundary);
  for (const p of [lastHistory, lastForecast]) {
    if (!p) continue;
    const i = points.indexOf(p);
    root.appendChild(svg("text", {class: "dv2c-label", x: x(i), y: y(p.v) - 10, "text-anchor": i > columns.length - 2 ? "end" : "middle", text: p.display || format(p.v)}));
  }
  const tip = tooltip(box);
  columns.forEach((c, i) => {
    const p = points[i];
    const hit = svg("rect", {class: "dv2c-hit", x: padL + band * i, y: 0, width: band, height, tabindex: 0,
      "aria-label": `${c.title}：${p && p.v !== null ? p.display : "缺失"}`});
    const show = () => tip.show(x(i), p && p.v !== null ? y(p.v) : padT, [
      {key: "s1", value: p && p.v !== null ? p.display : "—", label: c.title},
      {label: p && p.v !== null ? BASIS_LABEL[p.basis] || "" : (p && p.reason) || "缺失"},
    ]);
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", () => tip.hide());
    hit.addEventListener("blur", () => tip.hide());
    root.appendChild(hit);
  });
  box.appendChild(root);
  return box;
}

function encodingKey() {
  return el("ul", {class: "dv2c-key-list", "aria-label": "图例"},
    el("li", null, el("span", {class: "dv2c-swatch-line", "aria-hidden": "true"}), "实线：已披露历史"),
    el("li", null, el("span", {class: "dv2c-swatch-line dv2c-swatch-dash", "aria-hidden": "true"}), "虚线：预测（估算）"),
    el("li", null, el("span", {class: "dv2c-swatch-dot", "aria-hidden": "true"}), "实心点：申报原值"),
    el("li", null, el("span", {class: "dv2c-swatch-dot dv2c-swatch-hollow", "aria-hidden": "true"}), "空心点：推导或预测"));
}

function seriesPoints(series) {
  return (series.points || []).map((p) => ({...p, v: num(p.plot_value !== undefined ? p.plot_value : p.value)}));
}

export function buildFinancialTable(payload) {
  const columns = payload.columns || [];
  const boundary = payload.boundary_index || 0;
  const prelim = columns.filter((c) => c.kind === "preliminary").length;
  const history = boundary - prelim;
  const head1 = el("tr", null, el("th", {scope: "col", class: "dv2c-stick", rowspan: 2, text: `科目（${unitHeader(payload.unit_header || payload.unit)}）`}),
    el("th", {scope: "col", rowspan: 2, class: "dv2c-method-head", text: "方法"}),
    history ? el("th", {scope: "colgroup", colspan: history, class: "dv2c-group", text: "已披露历史"}) : null,
    prelim ? el("th", {scope: "colgroup", colspan: prelim, class: "dv2c-group dv2c-group-prelim", text: "初步实际（8-K）"}) : null,
    columns.length > boundary ? el("th", {scope: "colgroup", colspan: columns.length - boundary, class: "dv2c-group dv2c-group-fc dv2c-fc-start", text: "预测（估算） →"}) : null);
  const head2 = el("tr", null, columns.map((c, i) => el("th", {scope: "col", class: `num${i === boundary ? " dv2c-fc-start" : ""}${c.kind === "forecast" ? " dv2c-fc" : ""}${c.kind === "preliminary" ? " dv2c-prelim" : ""}`, title: c.title, text: c.label})));
  const body = [];
  const footnotes = [];
  for (const section of payload.sections || []) {
    body.push(el("tr", {class: "dv2c-section"}, el("th", {scope: "rowgroup", colspan: columns.length + 2, class: "dv2c-stick", text: section.label})));
    for (const row of (payload.rows || []).filter((r) => r.section === section.key)) {
      const reasons = new Set();
      const cells = row.cells.map((cell, i) => {
        const cls = `num${i === boundary ? " dv2c-fc-start" : ""}${columns[i] && columns[i].kind === "forecast" ? " dv2c-fc" : ""}${columns[i] && columns[i].kind === "preliminary" ? " dv2c-prelim" : ""}`;
        if (cell.status !== "ok") {
          if (cell.reason) reasons.add(cell.reason);
          return el("td", {class: cls}, missing(cell.reason));
        }
        const derived = cell.basis === "derived" || cell.basis === "derived_formula";
        const title = `${columns[i].title} · ${BASIS_LABEL[cell.basis] || ""}${cell.prior_estimate_display ? ` · 当时预测 ${cell.prior_estimate_display}` : ""}${cell.model_estimate_display ? ` · 模型估计 ${cell.model_estimate_display}` : ""}`;
        return el("td", {class: cls, title}, cell.display, derived ? el("span", {class: "dv2c-mark", "aria-label": "推导值", text: "*"}) : null);
      });
      const unit = row.unit_kind === "amount" ? "" : `（${row.unit_label}）`;
      body.push(el("tr", null, el("th", {scope: "row", class: "dv2c-stick", text: `${row.label}${unit}`}), methodCell(row), cells));
      if (reasons.size) footnotes.push([row.label, [...reasons]]);
    }
  }
  const table = el("table", {class: "dv2c-table dv2c-fin"},
    el("caption", {text: `${payload.company || ""} 驱动模型 第 ${payload.version} 版 · ${payload.period ? payload.period.label : ""}`}),
    el("thead", null, head1, head2), el("tbody", null, body));
  const gaps = footnotes.length ? el("details", {class: "dv2c-gaps"},
    el("summary", {text: `缺失说明（${footnotes.length} 行有“—”）`}),
    el("ul", null, footnotes.map(([label, reasons]) => el("li", null, el("strong", {text: label}), `：${reasons.join("；")}`)))) : null;
  const completeness = payload.completeness && payload.completeness.by_statement
    ? el("p", {class: "dv2c-hint dv2c-completeness", text: `预测完整度：${payload.completeness.label}`}) : null;
  return el("div", null,
    el("p", {class: "dv2c-unitrow", text: `${unitHeader(payload.unit_header || payload.unit)}（全表统一）；股数：百万股；每股：美元/股；比率：%`}),
    completeness,
    el("div", {class: "dv2c-scroll dv2c-fin-wrap", tabindex: 0, role: "region", "aria-label": "财务模型表格，首列与表头固定，可横向和纵向滚动"}, table),
    el("p", {class: "dv2c-hint", text: "* 推导值：由累计披露值相减，或按模型已校验的结构公式由已披露行计算。“—”为缺失，悬停可见原因。"}), gaps);
}

export function buildModel(payload, container) {
  if (!payload || payload.status !== "ok") return emptyState(TITLE, payload);
  const width = widthOf(container);
  const chartWidth = width >= 700 ? Math.floor((width - 16) / 2) : width;
  const columns = payload.columns || [];
  const boundary = payload.boundary_index || 0;
  const revenue = payload.series && payload.series.revenue;
  const margins = (payload.series && payload.series.margins) || [];
  const charts = el("div", {class: "dv2c-multiples"});
  if (revenue) {
    charts.appendChild(lineChart({points: seriesPoints(revenue), columns, boundary, width: chartWidth,
      unitLabel: revenue.unit || payload.unit, title: "营业收入", zeroBased: true,
      emptyReason: revenue.missing_reason, format: (v) => Number(v).toLocaleString("en-US", {maximumFractionDigits: Math.abs(v) < 10 ? 1 : 0})}));
  }
  const marginBox = el("div");
  const drawMargin = (index) => {
    while (marginBox.firstChild) marginBox.removeChild(marginBox.firstChild);
    const m = margins[index];
    if (!m) { marginBox.appendChild(el("p", {class: "dv2c-hint", text: payload.series.margins_missing_reason || "没有可计算的利润率"})); return; }
    marginBox.appendChild(lineChart({points: seriesPoints(m), columns, boundary, width: chartWidth, unitLabel: "%",
      title: m.label, format: (v) => `${Number(v).toFixed(0)}%`,
      emptyReason: "缺少计算所需的收入或利润数据"}));
    marginBox.appendChild(el("p", {class: "dv2c-hint", text: `口径：${m.formula}；已披露 ${m.history_points} 期、预测 ${m.forecast_points} 期有值`}));
  };
  if (margins.length > 1) {
    const select = el("select", {class: "dv2c-select", "aria-label": "选择利润率口径"},
      margins.map((m, i) => el("option", {value: i, text: m.label})));
    select.addEventListener("change", () => drawMargin(Number(select.value)));
    charts.appendChild(el("div", null, el("label", {class: "dv2c-control"}, "利润率口径 ", select), marginBox));
  } else {
    charts.appendChild(marginBox);
  }
  drawMargin(0);
  // What is usable / not, the version history and the export sit outside this
  // widget on the company page (C15/C18), so the card does not repeat them.
  return card(TITLE, payload,
    metaLine(payload, [["模型版本", `第 ${payload.version} 版（预测生成于${fmtDate(payload.model_created_at)}）`]]),
    encodingKey(), charts, buildFinancialTable(payload), notes(payload));
}
