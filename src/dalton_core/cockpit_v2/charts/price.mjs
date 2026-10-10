// company.summary -- stat tile (latest close + signed changes) with a price
// sparkline and filing markers, then the key-metric mini table.
//
// Form (dataviz): a single current value plus its trend is a stat tile, not a
// chart; the sparkline is the tile's trend. One series -> no legend box.
import {
  el, svg, num, fmtDate, ticks, scale, widthOf, metaLine, notes, card, emptyState,
  missing, tooltip, tableTwin,
} from "./core.mjs";

const TITLE = "股价与关键指标";

function sparkline(payload, width) {
  const points = (payload.points || []).map((p) => ({...p, y: num(p.close)})).filter((p) => p.y !== null);
  const height = 150, padL = 48, padR = 12, padT = 10, padB = 26;
  const values = points.map((p) => p.y);
  const yt = ticks(Math.min(...values), Math.max(...values), 3);
  const y = scale(yt[0], yt[yt.length - 1], height - padB, padT);
  const x = scale(0, Math.max(1, points.length - 1), padL, width - padR);
  const root = svg("svg", {class: "dv2c-svg", width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `${payload.company || ""}收盘价走势，${fmtDate(payload.period && payload.period.start)}至${fmtDate(payload.data_as_of)}`});
  for (const t of yt) {
    root.appendChild(svg("line", {class: "dv2c-grid", x1: padL, x2: width - padR, y1: y(t), y2: y(t)}));
    root.appendChild(svg("text", {class: "dv2c-tick", x: padL - 6, y: y(t) + 4, "text-anchor": "end", text: t.toFixed(0)}));
  }
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
  root.appendChild(svg("path", {class: "dv2c-area", d: `${line}L${x(points.length - 1).toFixed(1)},${height - padB}L${padL},${height - padB}Z`}));
  root.appendChild(svg("path", {class: "dv2c-line", d: line}));
  // filing markers along the baseline: event identity is in the tooltip and table
  const index = new Map(points.map((p, i) => [p.date, i]));
  const nearest = (day) => { let best = 0; points.forEach((p, i) => { if (p.date <= day) best = i; }); return best; };
  for (const event of payload.events || []) {
    const i = index.has(event.date) ? index.get(event.date) : nearest(event.date);
    root.appendChild(svg("rect", {class: "dv2c-event", x: x(i) - 1, y: height - padB + 2, width: 2, height: 6}));
  }
  const last = points[points.length - 1];
  if (last) root.appendChild(svg("circle", {class: "dv2c-dot", cx: x(points.length - 1), cy: y(last.y), r: 4}));
  root.appendChild(svg("text", {class: "dv2c-tick", x: padL, y: height - 4, text: fmtDate(points[0] && points[0].date)}));
  root.appendChild(svg("text", {class: "dv2c-tick", x: width - padR, y: height - 4, "text-anchor": "end", text: fmtDate(last && last.date)}));
  // crosshair: snaps to the nearest trading day; keyboard arrows move it
  const cross = svg("line", {class: "dv2c-cross", y1: padT, y2: height - padB, x1: -10, x2: -10});
  const hit = svg("rect", {class: "dv2c-hit", x: padL, y: 0, width: width - padL - padR, height, tabindex: 0,
    "aria-label": "用左右方向键逐日查看收盘价"});
  root.appendChild(cross);
  root.appendChild(hit);
  return {root, points, x, y, hit, cross};
}

// options.compact: the company page header already prints the price, the
// signed changes and the multiples (G05), so the card shows the chart only.
// options.onRange(range): a 3个月 / 6个月 / 1年 / 3年 switch (C05).
export function buildPriceSummary(payload, container, options = {}) {
  if (!payload || payload.status !== "ok") return emptyState(TITLE, payload);
  const compact = Boolean(options.compact);
  const width = widthOf(container);
  const latest = payload.latest || {};
  const tile = el("div", {class: "dv2c-tile"},
    el("div", {class: "dv2c-tile-label", text: `${latest.basis_label || "收盘价"} · ${fmtDate(latest.date)}`}),
    el("div", {class: "dv2c-tile-value", text: latest.display || "—"}),
    el("ul", {class: "dv2c-changes"}, (payload.changes || []).map((c) => el("li", null,
      el("span", {class: "dv2c-change-label", text: c.label}),
      c.status === "ok" ? el("strong", {class: "dv2c-num", title: `${c.basis}，自 ${fmtDate(c.since)}`, text: c.display}) : missing(c.missing_reason)))));
  const chart = el("div", {class: "dv2c-chart"});
  const spark = sparkline(payload, compact || width < 720 ? width : width - 276);
  chart.appendChild(spark.root);
  const tip = tooltip(chart);
  const events = new Map();
  for (const e of payload.events || []) events.set(e.date, [...(events.get(e.date) || []), e]);
  let cursor = spark.points.length - 1;
  const show = (i) => {
    cursor = Math.max(0, Math.min(spark.points.length - 1, i));
    const p = spark.points[cursor];
    spark.cross.setAttribute("x1", spark.x(cursor)); spark.cross.setAttribute("x2", spark.x(cursor));
    const rows = [{key: "s1", value: `${Number(p.close).toFixed(2)} 美元`, label: `${fmtDate(p.date)}${p.provisional ? "（盘中）" : " 收盘"}`}];
    for (const e of events.get(p.date) || []) rows.push({value: e.label, label: `报告期 ${fmtDate(e.report_date)}`});
    tip.show(spark.x(cursor), spark.y(p.y), rows);
  };
  spark.hit.addEventListener("pointermove", (ev) => {
    const box = spark.root.getBoundingClientRect ? spark.root.getBoundingClientRect() : {left: 0};
    const px = ev.clientX - box.left;
    let best = 0, gap = Infinity;
    spark.points.forEach((_, i) => { const d = Math.abs(spark.x(i) - px); if (d < gap) { gap = d; best = i; } });
    show(best);
  });
  spark.hit.addEventListener("pointerleave", () => tip.hide());
  spark.hit.addEventListener("focus", () => show(cursor));
  spark.hit.addEventListener("blur", () => tip.hide());
  spark.hit.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { show(cursor - 1); ev.preventDefault && ev.preventDefault(); }
    if (ev.key === "ArrowRight") { show(cursor + 1); ev.preventDefault && ev.preventDefault(); }
  });
  const eventNote = (payload.events || []).length
    ? el("p", {class: "dv2c-hint", text: `底部短竖线为申报日（共 ${payload.events.length} 次：${[...new Set(payload.events.map((e) => e.form))].join("、")}），悬停可看`})
    : el("p", {class: "dv2c-hint", text: "这段期间没有记录到申报事件"});
  const metrics = payload.metrics || [];
  const metricTable = metrics.length ? el("table", {class: "dv2c-table dv2c-mini"},
    el("caption", {text: `关键指标（估值快照 ${fmtDate(payload.metrics_as_of)}）`}),
    el("thead", null, el("tr", null, el("th", {scope: "col", text: "指标"}), el("th", {scope: "col", class: "num", text: "数值"}),
      el("th", {scope: "col", class: "num", text: "历史分位"}), el("th", {scope: "col", text: "说明"}))),
    el("tbody", null, metrics.map((m) => el("tr", null,
      el("th", {scope: "row", text: m.label}),
      el("td", {class: "num"}, m.display ? m.display : missing(m.missing_reason)),
      el("td", {class: "num"}, m.percentile_display ? m.percentile_display : missing("没有分位数据")),
      el("td", {class: "dv2c-note-cell", text: m.missing_reason || m.note || m.percentile_basis_label || ""})))))
    : el("p", {class: "dv2c-hint", text: payload.metrics_missing_reason || "还没有估值指标"});
  const twin = tableTwin("每日收盘价", ["日期", "收盘价（美元）"],
    (payload.points || []).slice().reverse().map((p) => [fmtDate(p.date), Number(p.close).toFixed(2)]));
  const ranges = (payload.range_options || []).length && options.onRange
    ? el("div", {class: "dv2c-seg", role: "group", "aria-label": "股价区间"}, payload.range_options.map((o) =>
        el("button", {type: "button", class: "dv2c-seg-btn", "aria-pressed": String(o.value === payload.range),
          text: o.label, onclick: () => options.onRange(o.value)})))
    : null;
  return card(compact ? "股价走势" : TITLE, payload,
    metaLine(payload, [["来源", payload.source_label], ["区间", `${plain0(payload.range_low)} – ${plain0(payload.range_high)} 美元`]]),
    ranges ? el("div", {class: "dv2c-controls"}, ranges) : null,
    compact ? chart : el("div", {class: "dv2c-tile-row"}, tile, chart), eventNote, compact ? null : metricTable, twin, notes(payload));
}

function plain0(v) { return v === null || v === undefined ? "—" : String(v); }
