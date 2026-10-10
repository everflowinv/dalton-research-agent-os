/* 财务模型 -- a real grid everywhere (MUSE P0 #5, financial 痛点 4, C15/C17/C18).

   The chart module's widget (charts/model.mjs buildModel) draws the lines and
   the grid; without it the page draws the same grid (buildFinancialTable)
   from the same payload, GET /v2/data/charts/model -- never the old
   <pre class="v2-pre"> text.  Frozen first column and header, thousands
   separators, one unit per table ("金额：亿美元"), Chinese line names.

   What is product content stays outside the widget slot, so a chart taking
   the slot never hides it (C18): what is usable and what is not (C15), the
   version history, the results the model could not compute (grouped by
   reason), and the export -- which says plainly when there is nothing to
   export (C17). */

import {LABELS} from "../copy.js";
import {load} from "./common.js";
import {buildFinancialTable} from "../charts/model.mjs";
import {cnDate, honestFromPayload, honestState, readFailure} from "./company_common.js";

export const MODEL_CHART_PATH = "/v2/data/charts/model";

const KEY_LINES = [
  ["revenue", "营业收入"], ["operating_income", "营业利润"], ["net_income", "净利润"],
  ["diluted_eps", "稀释每股收益"], ["operating_cash_flow", "经营现金流"],
];
const STATEMENTS = [["income", "利润表"], ["balance", "资产负债表"], ["cash", "现金流量表"]];
export const NO_MODEL_EXPORT = "尚未生成完整财务模型：当前缺少关键财务假设，无法导出有效 Excel 测算底稿。";

/** True when the grid has forecasts an export could carry (am 5.5). */
export function hasForecasts(payload) {
  if (!payload || payload.status !== "ok") return false;
  const forecastAt = (payload.columns || []).some(col => col.kind === "forecast");
  const computed = payload.completeness?.results_computed;
  return forecastAt && (computed == null || computed > 0);
}

/** 「三大表预测完备度：利润表 12/15 · 资产负债表 未建立 · 现金流量表 3/8 ｜ 营业收入、营业利润预测已就绪 ｜ 净利润、稀释每股收益待补齐」
    from the grid payload's completeness (am 5.5). */
export function usableSummary(payload) {
  if (!payload || payload.status !== "ok") return null;
  const forecastAt = new Set((payload.columns || []).map((c, i) => (c.kind === "forecast" ? i : -1)).filter(i => i >= 0));
  const ok = [], missing = [];
  for (const [role, label] of KEY_LINES) {
    const norm = x => String(x || "").replace(/-/g, "_");
    const rows = (payload.rows || []).filter(r => norm(r.role) === role || norm(r.line_ref) === role
      || (role === "net_income" && norm(r.role) === "parent_net_income")
      || (role === "diluted_eps" && /eps/i.test(`${r.role || ""} ${r.line_ref || ""}`)));
    if (!rows.length) { missing.push(label); continue; }
    const usable = rows.some(r => (r.cells || []).some((cell, i) => forecastAt.has(i) && cell.status === "ok"));
    (usable ? ok : missing).push(label);
  }
  const by = payload.completeness?.by_statement || {};
  const statements = STATEMENTS.map(([key, label]) => (by[key] ? `${label} ${by[key].computed}/${by[key].total}` : `${label} 未建立`)).join(" · ");
  return {ok, missing,
          text: [`三大表预测完备度：${statements}`,
                 ok.length ? `${ok.join("、")}预测已就绪` : "核心盈利预测尚未算出",
                 missing.length ? `${missing.join("、")}待补齐` : null].filter(Boolean).join(" ｜ ")};
}

/** The results the model could not compute, grouped by why. */
export function blockedByReason(payload) {
  const groups = new Map();
  for (const b of payload?.completeness?.results_blocked || []) {
    const reason = String(b.reason || "原因未记录").replace(/[。.\s]+$/u, "");
    if (!groups.has(reason)) groups.set(reason, []);
    groups.get(reason).push(b.label);
  }
  return [...groups.entries()].map(([reason, labels]) => ({reason, labels}));
}

async function exportFile(ctx, c, format, button, status, template = null) {
  button.disabled = true; status.textContent = "正在生成文件…";
  try {
    const q = new URLSearchParams({company: c.company_ref, format});
    if (template) q.set("template", template);
    const out = await ctx.api(`/v1/cockpit/export?${q}`, {fresh: true, timeoutMs: 120000});
    ctx.ui.download(out);
    status.textContent = `已下载 ${out.filename}${template ? "（本次临时使用所选模板，没有改变这家公司的默认模板）" : ""}`;
  } catch (e) { status.textContent = e.display_message; }
  finally { button.disabled = false; }
}
export {exportFile};

/* 导出财务模型 with the fund template it uses.  Choosing a company default is a
   non-destructive choice: the reason is optional (a suggested one is filled in
   when left empty, so the record still says why). */
async function modelExport(ctx, el, c, chart) {
  const {h, ui, say} = ctx;
  const status = h("span", {class: "v2-note", role: "status"});
  // No forecasts, no export (am 5.5): an Excel of empty forecast columns is
  // worse than none.
  const hasModel = hasForecasts(chart);
  if (!hasModel) {
    const xlsx = ui.button("下载财务预测底稿（.xlsx）", () => {}, {variant: "secondary"});
    xlsx.disabled = true;
    el.append(h("h3", {id: "model-export"}, LABELS.page_title.model_export),
      honestState(ctx, {state: NO_MODEL_EXPORT, impact: "导出按钮暂时不可用。",
        owner: "研究系统在模型规格确定、预测算出后自动生成模型。", action: "先阅读研究报告；模型生成后这里会出现导出。"}),
      h("div", {class: "v2-actions"}, xlsx));
    return;
  }
  const blocked = chart.completeness?.results_blocked || [];
  const tpl = await load(ctx, "/v1/cockpit/fund-templates");
  const view = tpl.value && tpl.value.enabled !== false ? tpl.value : null;
  const mine = (view?.companies || []).find(x => x.company_ref === c.company_ref) || null;
  const templates = view?.templates || [];
  const select = ui.field({id: "model-template", label: "财务模型套用模板", control: "select",
    help: mine ? `这家公司现在使用：${say(mine.template_label)}（${say(mine.basis_label || "")}）` : "按本项目的默认模板导出"});
  select.input.append(...templates.map(t => h("option", {value: t.version_ref, selected: mine && t.version_ref === mine.template_version_ref ? true : null},
    `${say(t.label)}${t.is_latest ? "" : "（旧版本）"}`)));
  const chosen = () => (mine && select.input.value && select.input.value !== mine.template_version_ref ? select.input.value : null);
  const xlsx = ui.button("下载财务预测底稿（.xlsx）", () => exportFile(ctx, c, "xlsx", xlsx, status, chosen()), {variant: "secondary"});
  el.append(h("h3", {id: "model-export"}, LABELS.page_title.model_export),
    blocked.length ? ui.alert(`限制提示：导出底稿只含已测算出的预测；还有 ${blocked.length} 项科目尚存空缺（文件中同样标为缺失），不能作为完整估值模型使用。`, "warn")
      : ui.note("导出底稿包含当前版本的全部预测。"),
    templates.length > 1 && mine ? select.wrap : null,
    h("div", {class: "v2-actions"}, xlsx, status));
  if (!(templates.length > 1 && mine)) {
    if (tpl.error) el.append(ui.note("模板列表暂时读不到，按本项目的默认模板导出。"));
    return;
  }
  const reason = ui.field({id: "model-template-reason", label: "理由（可选）", required: false, maxLength: 300, control: "input",
    help: "可以不填；不填时记录为「在公司页把导出模板改为所选模板」。"});
  ctx.drafts.bind(reason.input, `template:${c.company_ref}:reason`);
  const result = h("div", {role: "status", "aria-live": "polite"});
  const choose = async (versionRef, button) => {
    const t = templates.find(x => x.version_ref === versionRef);
    const why = reason.input.value.trim() || (versionRef ? `在公司页把导出模板改为「${say(t?.label || "所选模板")}」` : "在公司页恢复按行业或项目默认模板导出");
    button.disabled = true;
    result.replaceChildren(ui.receipt("正在记录…"));
    try {
      const out = await ctx.api.post("/v1/cockpit/fund_template_select", {scope_kind: "company", scope_ref: c.company_ref,
        template_version_ref: versionRef || null, template_version_hash: t ? t.version_hash : null,
        reason: why, request_id: ctx.requestId()});
      ctx.drafts.clear(`template:${c.company_ref}:reason`);
      reason.input.value = "";
      result.replaceChildren(ui.receipt(versionRef
        ? `${say(out?.message || "已记录")}：模板配置已生效，该标的以后默认按「${say(t?.label || "所选模板")}」的公式与版式导出。`
        : `${say(out?.message || "已记录")}：这家公司恢复按行业或项目默认模板导出。`));
    } catch (e) { result.replaceChildren(ui.receipt(e.display_message, "danger")); }
    finally { button.disabled = false; }
  };
  const setDefault = ui.button("设为这家公司的默认模板", () => choose(select.input.value, setDefault), {variant: "quiet"});
  const reset = mine.company_choice_ref ? ui.button("改回行业或项目默认模板", () => choose(null, reset), {variant: "quiet"}) : null;
  el.append(h("details", {class: "v2-more", id: "model-template-default"}, h("summary", null, "为该标的配置专用 Excel 模板"),
    reason.wrap, h("div", {class: "v2-actions"}, setDefault, reset), result,
    ui.note(`项目默认和按行业指定的模板在「${LABELS.nav.settings}」的「${LABELS.page_title.templates}」里查看；上传新模板也在那里。`)));
}

export async function modelSection(ctx, section, data, modelLoad, fill) {
  const {h, ui, fmt, say} = ctx;
  const c = data.company;
  const [res, chartRes] = await Promise.all([modelLoad, load(ctx, `${MODEL_CHART_PATH}?company=${encodeURIComponent(c.company_ref)}`)]);
  data.model = res.value;
  const chart = chartRes.value;
  const summary = h("div", {class: "v2-model-summary"});
  const holder = h("div");
  const after = h("div");
  const exportBox = h("div", {class: "v2-panel"});
  fill(section, summary, holder, after, exportBox);
  // 1. what is usable (C15), with the version and when it was generated
  if (chart && chart.status === "ok") {
    const usable = usableSummary(chart);
    summary.append(
      h("p", {class: "v2-lead"}, usable.text),
      h("p", {class: "v2-note"}, `财务模型基准版本：第 ${chart.version ?? "—"} 版 ｜ 预测生成基准日：${cnDate(chart.model_created_at) || "—"} ｜ 预测颗粒度：${chart.period?.label || "—"}`));
  } else if (chartRes.error && res.error) {
    summary.append(readFailure(ctx, chartRes.error, "财务模型"));
  } else if (chart && chart.status !== "ok") {
    summary.append(honestFromPayload(ctx, chart, {state: say(c.model_empty_label || "预测底稿：三大表盈利预测编制中。"),
      impact: "没有预测表和导出文件。", owner: "研究系统在模型规格确定后自动建模。", action: "先阅读研究报告。"}));
  }
  // 1b. batch an: per-rule invariant coverage of the current version; a rule
  // with nothing to check reads 未覆盖, never 通过.
  const coverage = Object.values(c.invariant_coverage || {});
  if (coverage.length) summary.append(h("details", {class: "v2-more", id: "model-invariant-coverage"},
    h("summary", null, "模型一致性检查覆盖情况"),
    ...coverage.map(item => h("div", null,
      h("p", null, h("strong", null, say(item.output_kind_label || "")), "：", say(item.display_summary || "")),
      h("ul", null, ...(item.rules || []).map(r => h("li", null, `${say(r.label)}：`,
        ui.tag(say(r.display_status), r.coverage === "pass" ? "ok" : "warn"),
        r.checked ? `（检查 ${r.checked} 项）` : "")))))));
  const staleModel = (c.consistency?.stale || []).filter(x => ["forecast_line", "sensitivity_projection", "annual_projection", "invariant_verdict"].includes(x.kind));
  if (staleModel.length) summary.append(ui.alert(`${staleModel.length} 份模型派生结果基于旧版模型，已标为已过期，不作为当前结果。`, "warn"));
  // 2. the grid: the widget, else the same grid from the same payload
  const exporting = modelExport(ctx, exportBox, c, chart).catch(e => { console.warn(e); exportBox.remove(); });
  // No model: the four-part state above says so once; no empty chart card under it.
  if (chart && chart.status !== "ok" && !chartRes.error) holder.remove();
  else await ctx.slot("company.model", holder, {company: c, company_ref: c.company_ref, model: res.value}, el => {
    if (!chart || chart.status !== "ok") return;
    el.append(h("div", {class: "dv2c-root"}, buildFinancialTable(chart)));
  });
  // 3. outside the slot: version history and what could not be computed
  const groups = blockedByReason(chart);
  if (groups.length) after.append(h("details", {class: "v2-more"},
    h("summary", null, `尚未完成测算的预测科目与原因（共 ${chart.completeness.results_blocked.length} 项，暂未计入正式财务底稿）`),
    h("ul", null, ...groups.map(g => h("li", null, h("strong", null, g.labels.join("、")), `：${g.reason}`)))));
  const history = res.value?.history || [];
  if (history.length > 1) after.append(h("details", {class: "v2-more"}, h("summary", null, `财务模型版本记录（${history.length} 版）`),
    h("ul", null, ...history.map(x => h("li", null, `第 ${x.version} 版 · ${fmt.datetime(x.created_at)} · ${say(x.change_reason_label || "")}`)))));
  await exporting;
  if (!summary.childNodes.length && res.error) summary.append(readFailure(ctx, res.error, "财务模型"));

}
