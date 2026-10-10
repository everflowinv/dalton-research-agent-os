// A horizontal waterfall: categories down the side (their names are long
// Chinese account names), one shared value axis, bars float from the running
// total. Polarity is the colour job (diverging poles blue/red, totals neutral),
// and the sign is always printed beside every bar -- colour never carries it.
import { el, svg, num, ticks, scale, tooltip } from "./core.mjs";

export function waterfall(spec) {
  const rows = spec.rows.map((r) => ({...r, a: num(r.start), b: num(r.end)}));
  const width = spec.width;
  const rowH = 32, bar = 16, padT = 26, padB = 8;
  const labelW = Math.min(240, Math.max(140, Math.floor(width * 0.34)));
  const valueW = 112;
  const plotL = labelW + 8, plotR = width - valueW;
  const height = padT + rows.length * rowH + padB;
  const values = rows.flatMap((r) => [r.a, r.b]).filter((v) => v !== null);
  const xt = ticks(Math.min(0, ...values), Math.max(0, ...values), 4);
  const x = scale(xt[0], xt[xt.length - 1], plotL, plotR);
  const box = el("div", {class: "dv2c-chart"});
  const root = svg("svg", {class: "dv2c-svg", width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": spec.ariaLabel});
  for (const t of xt) {
    root.appendChild(svg("line", {class: t === 0 ? "dv2c-baseline" : "dv2c-grid", x1: x(t), x2: x(t), y1: padT - 4, y2: height - padB}));
    root.appendChild(svg("text", {class: "dv2c-tick", x: x(t), y: padT - 10, "text-anchor": "middle", text: spec.tick(t)}));
  }
  const tip = tooltip(box);
  rows.forEach((r, i) => {
    const top = padT + i * rowH;
    const cy = top + rowH / 2;
    // A bar that opens something says so: underlined label and a chevron
    // (MUSE 9: visible affordance, not only role=button).
    root.appendChild(svg("text", {class: `dv2c-rowlabel${r.type !== "step" ? " dv2c-rowlabel-total" : ""}${r.onClick ? " dv2c-rowlabel-link" : ""}`,
      x: labelW, y: cy + 5, "text-anchor": "end", text: `${r.short || r.label}${r.onClick ? " ›" : ""}`}));
    if (r.status === "missing" || r.a === null || r.b === null) {
      root.appendChild(svg("text", {class: "dv2c-label dv2c-muted", x: x(0) + 6, y: cy + 5, text: `缺失：${r.missing_reason || "未计入"}`}));
      return;
    }
    const lo = Math.min(r.a, r.b), hi = Math.max(r.a, r.b);
    const tone = r.type === "step" ? (r.b - r.a >= 0 ? "pos" : "neg") : "total";
    const w = Math.max(2, x(hi) - x(lo));
    root.appendChild(svg("rect", {class: `dv2c-bar dv2c-${tone}${r.derived ? " dv2c-derived" : ""}`, x: x(lo), y: cy - bar / 2, width: w, height: bar, rx: 2}));
    if (i + 1 < rows.length && rows[i + 1].type === "step" && rows[i + 1].a !== null) {
      root.appendChild(svg("line", {class: "dv2c-connector", x1: x(r.b), x2: x(r.b), y1: cy + bar / 2, y2: cy + rowH - bar / 2}));
    }
    const text = r.display || "";
    const right = x(hi) + 6;
    root.appendChild(svg("text", {class: "dv2c-label dv2c-num", x: right, y: cy + 5, text}));
    const hit = svg("rect", {class: `dv2c-hit${r.onClick ? " dv2c-hit-link" : ""}`, x: 0, y: top, width, height: rowH, tabindex: 0,
      role: r.onClick ? "button" : null,
      "aria-label": `${r.label}：${text}${r.derived ? "（推算）" : ""}${r.onClick ? "。按回车查看报表行与原始申报" : ""}`});
    const show = () => tip.show(Math.min(right, width - 230), top, [
      {key: tone === "pos" ? "pos" : tone === "neg" ? "neg" : "total", value: text, label: r.label},
      ...(r.tipRows || []),
    ]);
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", () => tip.hide());
    hit.addEventListener("blur", () => tip.hide());
    if (r.onClick) {
      hit.addEventListener("click", () => r.onClick(r));
      hit.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault && ev.preventDefault(); r.onClick(r); } });
    }
    root.appendChild(hit);
  });
  box.appendChild(root);
  return box;
}

export function waterfallKey(totalLabel) {
  return el("ul", {class: "dv2c-key-list", "aria-label": "图例"},
    el("li", null, el("span", {class: "dv2c-swatch dv2c-pos", "aria-hidden": "true"}), "增加（+）"),
    el("li", null, el("span", {class: "dv2c-swatch dv2c-neg", "aria-hidden": "true"}), "减少（−）"),
    el("li", null, el("span", {class: "dv2c-swatch dv2c-total", "aria-hidden": "true"}), totalLabel));
}
