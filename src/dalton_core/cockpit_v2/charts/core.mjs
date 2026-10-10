// Cockpit v2 charts: shared pieces. Plain DOM + SVG, no library, no network
// except the cockpit's own /v2/data/charts/* routes.
//
// Every untrusted string (company names, line labels, reasons) goes in through
// textContent / createTextNode -- never innerHTML.

export const SVG_NS = "http://www.w3.org/2000/svg";
export const DATA_BASE = "/v2/data/charts/";

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

export function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.setAttribute("class", String(value));
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function num(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function fmtDate(value) {
  if (!value) return "—";
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}年${Number(m[2])}月${Number(m[3])}日` : String(value);
}

// A signed number for axis ticks and tooltips: the sign is always printed.
export function signed(value, digits = 1, suffix = "") {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  const shown = Math.abs(n).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
  if (Number(n.toFixed(digits)) > 0) return `+${shown}${suffix}`;
  if (Number(n.toFixed(digits)) < 0) return `−${shown}${suffix}`;
  return `${shown}${suffix}`;
}

export function plain(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
}

// Clean axis ticks: 1 / 2 / 2.5 / 5 x 10^n.
export function ticks(min, max, count = 5) {
  if (!(Number.isFinite(min) && Number.isFinite(max))) return [];
  if (min === max) { const pad = Math.abs(min) * 0.1 || 1; min -= pad; max += pad; }
  const span = max - min;
  const raw = span / Math.max(1, count);
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * power).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) out.push(Number(v.toFixed(10)));
  if (out[out.length - 1] < max) out.push(Number((out[out.length - 1] + step).toFixed(10)));
  return out;
}

export function scale(d0, d1, r0, r1) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}

// The drawing width inside a card (the card's padding and border subtracted),
// so SVG text renders at its stated pixel size instead of being scaled.
export function widthOf(container, fallback = 680) {
  const w = container && container.clientWidth;
  return Math.max(300, Math.min(1160, (w || fallback) - 42));
}

// The meta line every chart carries: as-of, currency, unit, period, source.
export function metaLine(payload, extra = []) {
  const parts = [];
  if (payload.data_as_of) parts.push(["数据截至", fmtDate(payload.data_as_of)]);
  if (payload.currency) parts.push(["币种", payload.currency === "USD" ? "美元（USD）" : payload.currency]);
  if (payload.unit) parts.push(["单位", payload.unit]);
  if (payload.period && payload.period.label) parts.push(["期间", payload.period.label]);
  for (const item of extra) if (item && item[1]) parts.push(item);
  return el("dl", {class: "dv2c-meta"}, parts.map(([k, v]) => el("div", null, el("dt", {text: k}), el("dd", {text: v}))));
}

export function notes(payload) {
  const list = (payload.notes || []).filter(Boolean);
  if (!list.length) return null;
  return el("ul", {class: "dv2c-notes"}, list.map((t) => el("li", {text: t})));
}

export function card(title, payload, ...body) {
  return el("section", {class: "dv2c-card", "data-kind": payload && payload.kind},
    el("header", {class: "dv2c-head"}, el("h3", {class: "dv2c-title", text: title}),
      payload && payload.company ? el("span", {class: "dv2c-company", text: payload.company}) : null),
    ...body);
}

// The honest empty state: what is missing and why, from the stage ledger.
export function emptyState(title, payload) {
  return card(title, payload,
    el("div", {class: "dv2c-empty", role: "status"},
      el("p", {class: "dv2c-empty-lead", text: "暂无数据"}),
      el("p", {text: (payload && payload.empty_reason) || "这张图还没有数据。"})));
}

export function errorState(title, message) {
  return card(title, null, el("div", {class: "dv2c-empty", role: "alert"},
    el("p", {class: "dv2c-empty-lead", text: "读取失败"}), el("p", {text: message})));
}

// A missing value: a dash a screen reader can explain, never a blank or a zero.
export function missing(reason) {
  return el("span", {class: "dv2c-missing", title: reason || "缺失", "aria-label": `缺失：${reason || "原因未记录"}`, text: "—"});
}

// Status with icon + label; colour never carries the meaning alone.
const STATUS = {
  triggered: ["▲", "warning"], not_triggered: ["○", "neutral"], unobservable: ["?", "neutral"],
  unobserved: ["·", "neutral"], complete: ["✓", "good"], residual: ["!", "warning"],
  missing_anchor: ["!", "serious"], missing_rows: ["!", "serious"],
};
export function statusTag(status, label) {
  const [icon, tone] = STATUS[status] || ["·", "neutral"];
  return el("span", {class: `dv2c-status dv2c-status-${tone}`},
    el("span", {class: "dv2c-status-icon", "aria-hidden": "true", text: icon}), el("span", {text: label || status || "未知"}));
}

// One tooltip per chart; values lead, labels follow. Text only.
export function tooltip(host) {
  const tip = el("div", {class: "dv2c-tip", role: "tooltip", hidden: true});
  host.appendChild(tip);
  return {
    show(x, y, rows) {
      clear(tip);
      for (const row of rows) {
        const line = el("div", {class: "dv2c-tip-row"});
        if (row.key) line.appendChild(el("span", {class: `dv2c-key dv2c-key-${row.key}`, "aria-hidden": "true"}));
        if (row.value !== undefined) line.appendChild(el("strong", {text: row.value}));
        if (row.label) line.appendChild(el("span", {class: "dv2c-tip-label", text: row.label}));
        tip.appendChild(line);
      }
      tip.hidden = false;
      tip.removeAttribute("hidden");
      const hostWidth = host.clientWidth || 640;
      tip.style.left = `${Math.max(4, Math.min(x + 12, hostWidth - 220))}px`;
      tip.style.top = `${Math.max(4, y - 8)}px`;
    },
    hide() { tip.hidden = true; tip.setAttribute("hidden", ""); },
  };
}

// Every chart has a table twin -- the WCAG-clean equivalent.
export function tableTwin(caption, headers, rows) {
  const table = el("table", {class: "dv2c-table"},
    el("caption", {class: "dv2c-sr", text: caption}),
    el("thead", null, el("tr", null, headers.map((h, i) => el("th", {scope: "col", class: i ? "num" : null, text: h})))),
    el("tbody", null, rows.map((row) => el("tr", null, row.map((cell, i) =>
      i === 0 ? el("th", {scope: "row", text: cell}) :
        el("td", {class: "num"}, cell === null || cell === undefined ? missing() :
          (typeof cell === "object" ? cell : String(cell))))))));
  return el("details", {class: "dv2c-twin"}, el("summary", {text: "查看数据表"}), el("div", {class: "dv2c-scroll"}, table));
}

export async function fetchChart(kind, params, options = {}) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") query.set(k, v);
  const url = `${options.base || DATA_BASE}${kind}${query.toString() ? `?${query}` : ""}`;
  const getter = options.fetchJson || (async (u) => {
    const response = await fetch(u, {credentials: "same-origin", headers: {Accept: "application/json"}});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || body.error || `读取失败（${response.status}）`);
    return body;
  });
  return getter(url);
}

// Refetch keeps the frame: the previous render stays, dimmed, until the new one lands.
export async function mount(container, loader, build, title) {
  container.classList && container.classList.add("dv2c-root");
  container.setAttribute("aria-busy", "true");
  if (container.firstChild) container.classList.add("dv2c-loading");
  try {
    const payload = await loader();
    const node = build(payload, container);
    clear(container);
    container.appendChild(node);
    return payload;
  } catch (error) {
    clear(container);
    container.appendChild(errorState(title, (error && error.message) || "读取失败，请稍后重试"));
    return null;
  } finally {
    container.classList && container.classList.remove("dv2c-loading");
    container.removeAttribute("aria-busy");
  }
}

export function companyOf(context) {
  if (typeof context === "string") return context;
  if (!context) return null;
  return context.company_ref || context.company || context.companyRef || (context.dataset && context.dataset.company) || null;
}
