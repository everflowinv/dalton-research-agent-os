// Cockpit v2 financial charts -- the entry module.
//
//   <link rel="stylesheet" href="/v2/assets/charts/charts.css">
//   <script type="module" src="/v2/assets/charts/charts.mjs"></script>
//
// On load it registers one widget per slot through
// window.DaltonV2.registerWidget(slot, render) when the shell provides it.
// render(container, context) -> Promise; context is a company ref string or
// {company_ref | company, fetchJson?, base?}. Without the shell, import the
// renderers below and call them directly.
import { fetchChart, mount, companyOf, el, emptyState } from "./core.mjs";
import { buildPriceSummary } from "./price.mjs";
import { buildModel } from "./model.mjs";
import { buildValuation, buildScenarioStatus } from "./scenarios.mjs";
import { buildReturns } from "./returns.mjs";
import { buildBridge } from "./bridge.mjs";
import { buildComparison, SORT_COLUMN } from "./comparison.mjs";

export { buildPriceSummary, buildModel, buildValuation, buildScenarioStatus, buildReturns, buildBridge, buildComparison };

function options(context) {
  return context && typeof context === "object" ? {fetchJson: context.fetchJson, base: context.base} : {};
}

function needCompany(container, context, title) {
  const company = companyOf(context);
  if (!company) {
    while (container.firstChild) container.removeChild(container.firstChild);
    container.appendChild(emptyState(title, {empty_reason: "没有指定公司。"}));
  }
  return company;
}

export function renderSummary(container, context) {
  const company = needCompany(container, context, "股价与关键指标");
  if (!company) return Promise.resolve(null);
  const compact = Boolean(context && context.header_shown);
  const load = (range) => mount(container, () => fetchChart("price", {company, range}, options(context)),
    (payload, host) => buildPriceSummary(payload, host, {compact, onRange: load}), "股价与关键指标");
  return load(context && context.range);
}

export function renderModel(container, context) {
  const company = needCompany(container, context, "财务模型");
  if (!company) return Promise.resolve(null);
  return mount(container, () => fetchChart("model", {company}, options(context)), buildModel, "财务模型");
}

export function renderValuation(container, context) {
  const company = needCompany(container, context, "估值情景");
  if (!company) return Promise.resolve(null);
  return mount(container, () => fetchChart("scenarios", {company}, options(context)), buildValuation, "估值情景");
}

export function renderScenarios(container, context) {
  const company = needCompany(container, context, "情景与触发条件");
  if (!company) return Promise.resolve(null);
  return mount(container, () => fetchChart("scenarios", {company}, options(context)), buildScenarioStatus, "情景与触发条件");
}

// 回报归因 tab: the return waterfall, then the profit bridge below it.
export function renderReturns(container, context) {
  const company = needCompany(container, context, "回报归因");
  if (!company) return Promise.resolve(null);
  const top = el("div", {class: "dv2c-slot"});
  const bottom = el("div", {class: "dv2c-slot"});
  while (container.firstChild) container.removeChild(container.firstChild);
  container.appendChild(top);
  container.appendChild(bottom);
  const opts = options(context);
  const loadBridge = (params = {}) => mount(bottom, () => fetchChart("bridge", {company, ...params}, opts),
    (payload, host) => buildBridge(payload, host, {reload: loadBridge}), "利润桥");
  return Promise.all([
    mount(top, () => fetchChart("returns", {company}, opts), buildReturns, "回报归因"),
    loadBridge(),
  ]);
}

export function renderBridge(container, context) {
  const company = needCompany(container, context, "利润桥");
  if (!company) return Promise.resolve(null);
  const opts = options(context);
  const load = (params = {}) => mount(container, () => fetchChart("bridge", {company, ...params}, opts),
    (payload, host) => buildBridge(payload, host, {reload: load}), "利润桥");
  return load();
}

// The shell passes its page context third: the sort lives in the URL
// (?sort=&dir=, review O13), company names link to their pages.
export function comparisonState(context, shell) {
  const filters = (shell && shell.state && shell.state.filters) || {};
  const state = {filters, seen: shell && shell.seen};
  if (context && typeof context === "object" && context.sort) state.sort = context.sort;
  if (shell && typeof shell.href === "function") {
    state.href = (row) => shell.href({page: "company", company: row.ticker || row.company_ref, tab: null, filters: {}});
  }
  if (shell && typeof shell.navigate === "function") {
    state.onSort = (sort, dir) => shell.navigate({filters: {...filters, sort, dir}}, {replace: true});
  }
  return state;
}

export function renderComparison(container, context, shell) {
  const state = comparisonState(context, shell);
  const sort = [state.sort, state.filters.sort].find((s) => SORT_COLUMN[s]);
  const dir = ["asc", "desc"].includes(state.filters.dir) ? state.filters.dir : undefined;
  return mount(container, () => fetchChart("comparison", {sort, dir}, options(context)),
    (payload, host) => buildComparison(payload, host, state), "覆盖公司对比");
}

export const SLOTS = {
  "company.summary": renderSummary,
  "company.model": renderModel,
  "company.valuation": renderValuation,
  "company.returns": renderReturns,
  "company.scenarios": renderScenarios,
  // The overview owns its single comparison table, with view/catalyst columns.
  // Keep its headline slot for the coverage and research summary tiles.
};

export function install(api) {
  const target = api || (typeof window !== "undefined" ? window.DaltonV2 : undefined);
  if (!target || typeof target.registerWidget !== "function") return false;
  for (const [slot, render] of Object.entries(SLOTS)) target.registerWidget(slot, render);
  return true;
}

if (typeof window !== "undefined") {
  window.DaltonV2Charts = {SLOTS, install, renderSummary, renderModel, renderValuation, renderScenarios,
    renderReturns, renderBridge, renderComparison};
  if (!install()) {
    // The shell may load after this module; it can call DaltonV2Charts.install() itself,
    // or we retry once the document has finished loading.
    if (typeof document !== "undefined" && document.readyState !== "complete" && window.addEventListener) {
      window.addEventListener("load", () => install(), {once: true});
    }
  }
}
