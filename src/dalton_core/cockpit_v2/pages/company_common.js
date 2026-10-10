/* Shared pieces of the company pages (batch ah, 2026-10-09 analyst reviews).

   honestState(): every empty or unreadable section says the same four things
   (review G12): 当前状态 → 影响 → 谁处理 → 你可以做什么 -- never a bare "暂无",
   never a blank that could read as zero. */

export {companyLabel, glossTerm, forecastBadges, unitHeader, MONEY_RULE} from "../charts/labels.js";

/** {state, impact, owner, action, link: {href, text}} -> one block. */
export function honestState(ctx, {state, impact, owner, action, link = null, tone = "info"}) {
  const {h} = ctx;
  const rows = [["当前状态", state], ["影响", impact], ["谁处理", owner], ["你可以", action]].filter(([, v]) => v);
  return h("div", {class: "v2-honest", "data-tone": tone, role: "status"},
    h("dl", {class: "v2-honest-list"}, ...rows.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v)))),
    link ? h("p", null, h("a", {href: link.href}, link.text)) : null);
}

/** The server's G12 envelope ({empty: {state, impact, owner, action}}) or a reason string. */
export function honestFromPayload(ctx, payload, fallback) {
  const e = payload && payload.empty;
  if (e && (e.state || e.impact)) return honestState(ctx, e);
  const reason = (payload && (payload.empty_reason || payload.display_message)) || null;
  return honestState(ctx, {...fallback, state: reason || fallback.state});
}

/** An unreadable read, in the same four parts. */
export function readFailure(ctx, err, what) {
  const message = String((err && err.display_message) || "暂时读不到").replace(/[。.\s]+$/u, "");
  return honestState(ctx, {
    state: `${what}暂时读不到（${message}）。`,
    impact: "这一部分先不显示，其他部分照常可用；这不代表没有数据。",
    owner: "系统会在你下次打开或刷新本页时重新读取；一直读不到时由管理员排查。",
    action: "稍后刷新本页。",
    tone: "warn",
  });
}

/** "2026年10月8日" from an ISO date or timestamp. */
export function cnDate(value) {
  const m = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}年${Number(m[2])}月${Number(m[3])}日` : "";
}

/** A signed percent with the sign always printed (+2.3% / −1.4% / 0.0%). */
export function signedPercent(value, digits = 1) {
  const n = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isFinite(n)) return "—";
  const shown = Math.abs(n).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const zero = Number(n.toFixed(digits)) === 0;
  return zero ? `${shown}%` : `${n > 0 ? "+" : "−"}${shown}%`;
}

/** Amounts in the one money rule: thousands separators, two decimals. */
export function grouped(value, digits = 2) {
  const n = Number(value);
  if (value === null || value === undefined || value === "" || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
}
