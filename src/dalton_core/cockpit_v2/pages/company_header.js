/* 公司页页头 (financial review 4.2, MUSE 4, batch ah): name + ticker (never a
   CIK), the last price with its date, the changes with their sign, and the
   key multiples -- the same name the company list and every chart card print. */

import {companyLabel, cnDate, signedPercent, grouped} from "./company_common.js";
import {METRIC_LABELS} from "../copy.js";

const HEADER_METRICS = ["trailing_pe", "forward_pe", "price_to_sales", "ev_to_ebitda", "fcf_yield"];
const SHORT_METRIC = {trailing_pe: "市盈率（过去四季）", forward_pe: "预期市盈率", price_to_sales: "市销率",
                      ev_to_ebitda: "EV/EBITDA", fcf_yield: "自由现金流收益率"};

/** The multiples the header shows, each available or with why not. */
export function headerMultiples(valuation) {
  const metrics = (valuation && valuation.metrics) || [];
  return HEADER_METRICS.map(key => metrics.find(m => m.metric === key)).filter(Boolean).map(m => {
    const label = SHORT_METRIC[m.metric] || METRIC_LABELS[m.metric] || "估值倍数";
    if (m.status !== "available" || m.value == null) return {label, value: null, missing: /ebitda/i.test(m.metric || "") ? "数据未补齐（缺经调 EBITDA）" : /fcf/i.test(m.metric || "") ? "数据未补齐（缺自由现金流）" : "数据未补齐（缺计算所需科目）"};
    const n = Number(m.value);
    const value = m.unit === "fraction" ? signedPercent(n * 100, 1).replace(/^\+/, "") : `${grouped(n, 1)} 倍`;
    return {label, value, percentile: m.percentile != null ? Number(m.percentile) : null};
  });
}

export function companyHeader(ctx, c) {
  const {h} = ctx;
  const m = c.market || {};
  const name = companyLabel(c);
  const changes = (m.changes || []).filter(x => x.status === "available" && x.percent != null);
  const priceLine = m.close != null
    ? h("p", {class: "v2-quote"},
        h("span", {class: "v2-quote-price v2-num"}, `${grouped(m.close)} ${m.currency === "USD" || !m.currency ? "美元" : m.currency}`),
        " ",
        ...changes.map(x => h("span", {class: `v2-quote-change v2-num ${Number(x.percent) >= 0 ? "v2-up" : "v2-down"}`},
          `${x.label || `近${x.days}个交易日`} ${signedPercent(x.percent)}`)),
        m.change_percent != null ? h("span", {class: `v2-quote-change v2-num ${Number(m.change_percent) >= 0 ? "v2-up" : "v2-down"}`},
          `近一年 ${signedPercent(m.change_percent)}`) : null,
        h("span", {class: "v2-note"}, `${m.provisional ? "盘中即时行情（收盘后更新为收盘价）" : "收盘行情（美东时间）"}，${cnDate(m.as_of) || "日期未记录"}`))
    : h("p", {class: "v2-quote v2-note"}, "还没有行情：价格数据接入后显示收盘价和涨跌。");
  const multiples = headerMultiples(c.valuation);
  const stage = c.stage ? ctx.say(c.stage) : "";
  const stageStatus = c.stage_status ? ctx.say(c.stage_status) : "";
  const stageLine = stageStatus && stageStatus !== stage ? `${stage} · ${stageStatus}` : stage;
  const cap = c.valuation?.basis?.market_cap;
  const capText = cap != null && Number.isFinite(Number(cap)) ? ctx.fmt.money(cap) : null;
  const multiplesLine = multiples.length || capText
    ? h("ul", {class: "v2-quote-multiples", "aria-label": "关键估值倍数"},
        capText ? h("li", null, h("span", {class: "v2-note"}, "市值 "), h("strong", {class: "v2-num"}, capText)) : null,
        ...multiples.map(x => h("li", null, h("span", {class: "v2-note"}, `${x.label} `),
          x.value ? h("strong", {class: "v2-num"}, x.value) : h("span", {class: "v2-note"}, x.missing))))
    : null;
  return h("div", {class: "v2-page-head v2-company-head"},
    h("h1", null, name),
    priceLine, multiplesLine,
    c.stage ? h("p", {class: "v2-note"}, `投研阶段：${stageLine}`) : null);
}
