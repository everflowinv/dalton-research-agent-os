/* 公司比较 -- ONE comparison for every surface (review O01/O13, P0).

   The company list, the overview's comparison widget (charts/comparison.mjs)
   and any fallback table read the same payload, GET /v2/data/charts/comparison
   (charts_api.COMPARISON_RULE: same basis and same period are ranked; another
   period is 不可比较 with its reason; missing values say what is missing; both
   rank last), and order rows with the same sortRows().  The sort lives in the
   URL (?sort=return|change|risk&dir=desc|asc); company names are links.

   For the overview (owned by another page module):
     import {comparisonSection} from "./company_compare.js";
     await comparisonSection(ctx, holder, {overview, needs});   // overview/needs optional
*/

import {companies, companyKey, load, tasksByCompany} from "./common.js";
import {comparisonTable, currentSort, SORT_COLUMN} from "../charts/comparison.mjs";
import {honestState, readFailure} from "./company_common.js";
import {LABELS} from "../copy.js";

const C = LABELS.comparison;

/** Strip only known display labels; official source names remain untouched. */
export function baselineContent(value) {
  return String(value ?? "").replace(/^(?:(?:排序基准|基准情景|基准)\s*[：:]\s*)+/, "");
}

export const COMPARISON_PATH = "/v2/data/charts/comparison";

/** The comparison read for the current URL sort. */
export function comparisonPath(filters = {}) {
  const q = new URLSearchParams();
  if (SORT_COLUMN[filters.sort]) q.set("sort", filters.sort);
  if (filters.dir === "asc" || filters.dir === "desc") q.set("dir", filters.dir);
  const text = q.toString();
  return text ? `${COMPARISON_PATH}?${text}` : COMPARISON_PATH;
}

/** The overview columns the comparison payload does not carry: the current
    view, the next market date and open tasks -- display only, never ranked. */
export function extraColumns(ctx, overview, needs) {
  const {h, say, fmt, href} = ctx;
  const byRef = new Map(companies(overview).map(c => [c.company_ref, c]));
  const tasks = tasksByCompany(needs);
  return [
    {label: C.view, render: r => {
      const c = byRef.get(r.company_ref);
      const base = (c?.scenarios?.in_force || []).find(s => s.key === "base");
      return base ? `基准情景：${baselineContent(say(base.name))}` : h("span", {class: "v2-note"}, LABELS.shell.no_view);
    }},
    {label: C.catalyst, render: r => {
      const cat = byRef.get(r.company_ref)?.catalyst;
      return cat ? `${say(cat.event_label || "下一事件")} ${fmt.date(cat.expected_date)}${cat.date_unconfirmed ? "（日期未确认）" : ""}` : "—";
    }},
    {label: C.tasks, render: r => {
      const n = tasks.get(r.company_ref) || 0;
      const c = byRef.get(r.company_ref);
      return n ? h("a", {href: href({page: "tasks", filters: {company: c ? companyKey(c) : r.ticker}})}, `${n} 项`) : "—";
    }},
  ];
}

/** Draw the comparison into `holder`.  `filter(row)` narrows the rows (the
    company list's search); ranking stays the payload's rule. */
export async function comparisonSection(ctx, holder, {overview = null, needs = null, filter = null, payload = null} = {}) {
  const {h} = ctx;
  const filters = ctx.state.filters || {};
  let value = payload;
  if (!value) {
    const res = await load(ctx, comparisonPath(filters));
    if (res.error) { holder.replaceChildren(readFailure(ctx, res.error, "标的横向比较数据")); return null; }
    value = res.value;
  }
  if (!value || value.status !== "ok") {
    holder.replaceChildren(honestState(ctx, {
      state: (value && value.empty_reason) || "还没有可以比较的公司。",
      impact: "公司比较和排名先不显示。",
      owner: "研究系统在覆盖名单和行情、回报数据就绪后自动生成。",
      action: "先设定研究目标并确认覆盖名单。",
      link: {href: ctx.href({page: "settings", filters: {focus: "goal"}}), text: "去设定研究目标"},
    }));
    return value;
  }
  const rows = filter ? value.rows.filter(filter) : value.rows;
  if (!rows.length) {
    holder.replaceChildren(honestState(ctx, {
      state: "没有符合查找条件的公司。", impact: "列表为空，不代表这些公司不存在。",
      owner: "—", action: "清除查找条件，或换一个公司名或代码。"}));
    return value;
  }
  const {sort, dir} = currentSort(value, {filters});
  const table = comparisonTable({...value, rows}, {
    sort, dir, seen: ctx.seen,
    href: row => ctx.href({page: "company", company: row.ticker || row.company_ref, tab: null, filters: {}}),
    onSort: (s, d) => ctx.navigate({filters: {...filters, sort: s, dir: d}}, {replace: true}),
    extraColumns: overview ? extraColumns(ctx, overview, needs) : [],
  });
  holder.replaceChildren(h("div", {class: "dv2c-root v2-compare"},
    h("p", {class: "v2-note"}, "排序基准：", baselineContent(value.rule ? value.rule.text : C.rule)),
    table));
  return value;
}
