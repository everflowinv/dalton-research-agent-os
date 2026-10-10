// Display words shared by the chart widgets and the company pages (batch ah,
// 2026-10-09 analyst UX reviews).  Not the shell's copy table: these are the
// finance terms the reviews asked to explain on first use (MUSE 3 / 9 / 10.5)
// and the labels a forecast carries next to its number (C07 / C08 / G11).
//
// Plain DOM through `document`, so a chart module (its own el()) and a page
// (ctx.h) can both use it.  Every string goes in through textContent.

import { LABELS } from "./vocabulary.js";

// The glossary of the ? popovers comes from the terminology registry
// (src/dalton_core/terminology.py, namespace "term_gloss"; batch am).
export const TERMS = LABELS.term_gloss;

//: The one money rule of the company page's tables (MUSE 10.2): one unit per
//: table, printed once in the header ("金额：亿美元"), thousands separators,
//: two decimals; never per cell, never an unscaled integer or a long float.
export const MONEY_RULE = "金额单位每张表统一，写在表头；按表内最大金额取“亿美元”或“万美元”，保留两位小数并加千分位。";

export function unitHeader(unit) {
  const text = String(unit || "").trim();
  return text.startsWith("金额") ? text : `金额：${text || "美元"}`;
}

let popId = 0;

function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.setAttribute("class", cls);
  if (text != null) n.textContent = String(text);
  return n;
}

/** A finance term with its explanation one press away, the first time it
    appears on a page (`seen` is the page's Set); later uses are plain text.
    Keyboard: the ? button toggles, Escape closes and returns focus. */
export function glossTerm(term, seen, shown, {iconOnly = false} = {}) {
  const label = shown || term;
  const gloss = TERMS[term];
  if (!gloss || (seen && seen.has(`term:${term}`))) return document.createTextNode(iconOnly ? "" : label);
  if (seen) seen.add(`term:${term}`);
  popId += 1;
  const id = `dv2c-term-${popId}`;
  const wrap = node("span", "dv2c-term");
  const button = node("button", "dv2c-term-btn", "?");
  button.setAttribute("type", "button");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", id);
  button.setAttribute("aria-label", `解释：${term}`);
  const pop = node("span", "dv2c-term-pop", gloss);
  pop.setAttribute("id", id);
  pop.setAttribute("role", "note");
  pop.hidden = true;
  pop.setAttribute("hidden", "");
  const set = open => {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    pop.hidden = !open;
    if (open) pop.removeAttribute("hidden"); else pop.setAttribute("hidden", "");
  };
  button.addEventListener("click", e => { if (e && e.stopPropagation) e.stopPropagation(); set(pop.hidden); });
  wrap.addEventListener("keydown", e => {
    if (e.key === "Escape" && !pop.hidden) { if (e.preventDefault) e.preventDefault(); set(false); button.focus && button.focus(); }
  });
  for (const child of [...(iconOnly ? [] : [node("span", "dv2c-term-word", label)]), button, pop]) wrap.appendChild(child);
  return wrap;
}

/** The labels a forward number carries beside it: 预测生成于… / 已到期未更新 /
    预测期已开始 / 简化估算（假设…）.  `badges` come from the server. */
export function forecastBadges(badges, {seen} = {}) {
  const list = (badges || []).filter(b => b && b.text);
  if (!list.length) return null;
  const wrap = node("span", "dv2c-badges");
  for (const b of list) {
    const badge = node("span", `dv2c-flagbadge dv2c-flagbadge-${b.tone === "warn" ? "warn" : "info"}`);
    badge.appendChild(/^(简化|粗算)估算$/.test(b.text) ? glossTerm("简化估算", seen, "粗算估算") : document.createTextNode(b.text));
    if (b.detail) {
      badge.setAttribute("title", b.detail);
      badge.appendChild(node("span", "dv2c-flagbadge-detail", `：${b.detail}`));
    }
    wrap.appendChild(badge);
  }
  return wrap;
}

/** "Accenture ACN": the one company name every surface prints. */
export function companyLabel(c) {
  if (!c) return "未知公司";
  if (c.display_name) return c.display_name;
  const ticker = String(c.ticker || "").trim(), name = String(c.name || "").trim();
  if (!name || name === ticker || /^\d{6,10}$/.test(name)) return ticker || "未登记代码的公司";
  return ticker ? `${name} ${ticker}` : name;
}
