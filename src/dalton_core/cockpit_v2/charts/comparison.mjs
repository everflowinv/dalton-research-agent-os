// overview.header -- the coverage comparison table: the ONE comparison rule of
// GET /v2/data/charts/comparison (charts_api.COMPARISON_RULE, review O01/O13).
// Each column states its one reference period; a value on another period is
// 不可比较 with its reason and is never ranked.  The company list and the
// shell's fallback table rank with sortRows() below, so every surface orders
// the same companies the same way.
//
// Form (dataviz): many companies x several measures of different units -> a
// table, not a chart (more classes than colour can carry).
import { el, fmtDate, metaLine, notes, card, emptyState, missing, num } from "./core.mjs";
import { glossTerm } from "./labels.js";
import { LABELS } from "./vocabulary.js";

const C = LABELS.comparison;
const TITLE = C.title;
export const SORT_COLUMN = Object.freeze({return: "expected", change: "change", risk: "risk"});

/** The server's rank_rows(): comparable values by value (desc by default),
    then 不可比较, then missing; ties and the rest by ticker. */
export function sortRows(rows, sort, dir = "desc") {
  const key = SORT_COLUMN[sort] || "expected";
  const sign = dir === "asc" ? 1 : -1;
  const tier = (c) => (c && c.value !== null && c.value !== undefined ? (c.comparable ? 0 : 1) : 2);
  return rows.slice().sort((a, b) => {
    const ca = a.cells[key], cb = b.cells[key];
    const ta = tier(ca), tb = tier(cb);
    if (ta !== tb) return ta - tb;
    if (ta === 0) {
      const va = num(ca.value), vb = num(cb.value);
      if (va !== vb) return (va - vb) * sign;
    }
    return String(a.ticker || "").localeCompare(String(b.ticker || ""));
  });
}

/** The current sort from the widget's state, the URL filters, or the payload. */
export function currentSort(payload, state = {}) {
  const filters = state.filters || {};
  const sort = state.sort || (SORT_COLUMN[filters.sort] ? filters.sort : null) || payload.sort || "return";
  const dir = state.dir || (filters.dir === "asc" || filters.dir === "desc" ? filters.dir : null) || payload.dir || "desc";
  return {sort, dir};
}

export function cell(c, seen, key = null) {
  if (!c || c.value === null || c.value === undefined) {
    // A forward return the model has not produced yet says so (am 4.1).
    return el("td", {class: "num"}, key === "expected" ? el("span", {class: "dv2c-missing", text: C.model_pending})
      : c?.missing_reason ? el("span", {class: "dv2c-missing", text: "—", "aria-hidden": "true"}) : missing(),
      c && c.missing_reason ? el("div", {class: "dv2c-hint", text: c.missing_reason}) : null);
  }
  if (!c.comparable) {
    return el("td", {class: "num dv2c-incomparable"},
      el("span", {class: "dv2c-num", text: c.display}),
      el("div", null, el("span", {class: "dv2c-badge"}, glossTerm("口径暂不可比", seen, C.not_comparable))),
      el("div", {class: "dv2c-hint", text: c.incomparable_reason || `期间：${c.basis_label}`}));
  }
  // A compact table: the forecast's labels as one short line under the number
  // (C07/C08); the details are in the cell's title and on the company page.
  const flags = (c.badges || []).filter((b) => b && b.text);
  const flagLine = flags.length ? el("div", {class: "dv2c-hint dv2c-flagline", title: flags.map((b) => b.detail || b.text).join("；")},
    flags.map((b, i) => [i ? " · " : "", /^(简化|粗算)估算$/.test(b.text) ? glossTerm("简化估算", seen, "粗算估算") : b.text])) : null;
  return el("td", {class: "num"}, el("span", {class: "dv2c-num", text: c.display}), flagLine,
    c.downside_label ? el("div", {class: "dv2c-hint", text: c.downside_label}) : null,
    c.invalidated_label ? el("div", {class: "dv2c-hint", text: c.invalidated_label}) : null);
}

function columnHead(column, sort, dir, onSort, seen) {
  const sorted = column.sort && column.sort === sort;
  const words = column.label;
  const help = column.key === "risk" ? glossTerm("下行风险情景", null, column.label, {iconOnly: true}) : null;
  const label = el("span", null, el("span", null, words),
    el("small", {class: "dv2c-colbasis", text: column.basis?.basis_label ? `${column.key === "risk" ? "口径" : "对比期间"}：${column.basis.basis_label}` : "对比期间待确定"}));
  const ariaSort = column.sort ? (sorted ? (dir === "asc" ? "ascending" : "descending") : "none") : null;
  if (!column.sort) return el("th", {scope: "col", class: "num"}, label, help);
  const next = sorted && dir === "desc" ? "asc" : "desc";
  return el("th", {scope: "col", class: "num", "aria-sort": ariaSort},
    el("button", {type: "button", class: "dv2c-sort", "aria-pressed": String(Boolean(sorted)),
      "aria-label": `${column.label}，${sorted ? (dir === "desc" ? "当前从高到低，点击改为从低到高" : "当前从低到高，点击改为从高到低") : "点击按这一列从高到低排序"}`,
      onclick: () => onSort(column.sort, sorted ? next : "desc")},
      label, el("span", {class: "dv2c-sort-mark", "aria-hidden": "true", text: sorted ? (dir === "desc" ? " ▼" : " ▲") : " ▽"})), help);
}

/** One row's company cell: a link to its page when the page gives an href. */
function companyCell(row, href) {
  const name = href ? el("a", {href: href(row), text: row.company}) : el("span", {text: row.company});
  return el("th", {scope: "row"}, el("div", null, name), el("div", {class: "dv2c-hint", text: row.stage}));
}

export function comparisonTable(payload, {sort, dir, onSort, href, seen, extraColumns = []} = {}) {
  const columns = payload.columns || [];
  const head = el("tr", null, el("th", {scope: "col", text: C.company}),
    columns.map((c) => columnHead(c, sort, dir, onSort, seen)),
    extraColumns.map((c) => el("th", {scope: "col", text: c.label})));
  const rows = sortRows(payload.rows || [], sort, dir).map((r) => el("tr", {"data-company": r.company_ref},
    companyCell(r, href), columns.map((c) => cell(r.cells[c.key], seen, c.key)),
    extraColumns.map((c) => el("td", null, c.render(r)))));
  const option = (payload.sort_options || []).find((o) => o.value === sort) || {};
  return el("div", {class: "dv2c-scroll"}, el("table", {class: "dv2c-table dv2c-cmp"},
    el("caption", {text: `${option.label || ""}${dir === "asc" ? "（从低到高）" : "（从高到低）"}；${(payload.rule && payload.rule.label) || "统计窗口一致才排名"}，口径暂不可比与缺数的排在最后`}),
    el("thead", null, head), el("tbody", null, rows)));
}

export function buildComparison(payload, container, state = {}) {
  if (!payload || payload.status !== "ok") return emptyState(TITLE, payload);
  let {sort, dir} = currentSort(payload, state);
  const seen = state.seen || new Set();
  const wrap = el("div");
  const draw = () => {
    while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
    wrap.appendChild(comparisonTable(payload, {sort, dir, seen, href: state.href,
      onSort: (s, d) => { sort = s; dir = d; if (state.onSort) state.onSort(s, d); draw(); }}));
  };
  draw();
  return card(TITLE, payload, metaLine(payload, [["收盘日", fmtDate(payload.data_as_of)]]),
    el("p", {class: "dv2c-hint"}, "排序基准：", String(payload.rule?.text || C.rule).replace(/^排序基准[：:]\s*/, ""), " ", glossTerm("口径", seen)),
    wrap, notes({notes: (payload.notes || []).filter((n) => !payload.rule || n !== payload.rule.text)}));
}
