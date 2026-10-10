/* 情景与触发: adjust the whole probability group (MUSE P0 #6, C23/C24,
   financial 4.9).

   Each scenario in force has a slider and a number box that move together,
   the total updates as you type, and -- when every kept scenario carries a
   target price or return -- the probability-weighted outcome updates too;
   otherwise the form says plainly that the scenarios are not valued yet (C09).
   Nothing is normalised behind the analyst's back; 「将剩余差额一键平衡至基准情景」 is
   an explicit button.

   Saving is ONE atomic request: POST /v2/data/scenarios/revise-group (the
   writer's revise_company_scenario_group) carries every scenario in force;
   either the whole group takes effect or none of it does.  The reason is
   required for current three-way partitions and must explain the evidence.
   Historical groups retain their earlier optional rationale. The request id is kept with
   the draft, so a retry after a dropped connection replays instead of
   writing twice.  The older per-scenario POST /v1/cockpit/scenario_revise
   stays on the server for compatibility; this form no longer uses it. */

import {said} from "./common.js";
import {LABELS} from "../copy.js";

export const KEY_LABELS = Object.freeze({bull: "乐观", base: "基准", bear: "悲观"});
const SCN = LABELS.scenario;
export const MIN_PCT = 1, MAX_PCT = 95, REASON_MAX = 600, MIN_KEPT = 2;
export const GROUP_PATH = "/v2/data/scenarios/revise-group";

export function percentOf(probability) {
  const n = Number(probability);
  return Number.isFinite(n) ? Math.round(n * 100) : null;
}

/** Check one state of the form (pure).
    rows: [{ref, label, key, current, value, retire}] -- value is what was typed. */
export function checkScenarioForm(rows, reason = "", requireReason = false) {
  const errors = {};
  let sum = 0, kept = 0;
  const changes = [];
  for (const r of rows) {
    if (r.retire) { changes.push({ref: r.ref, retire: true}); continue; }
    kept += 1;
    const text = String(r.value ?? "").trim();
    if (!/^\d{1,3}$/.test(text)) { errors[r.ref] = `请填 ${MIN_PCT} 到 ${MAX_PCT} 之间的整数百分比`; continue; }
    const n = Number(text);
    if (n < MIN_PCT || n > MAX_PCT) { errors[r.ref] = `每个情景的概率在 ${MIN_PCT}%–${MAX_PCT}% 之间`; continue; }
    sum += n;
    if (n !== r.current) changes.push({ref: r.ref, percent: n});
  }
  const valid = Object.keys(errors).length === 0;
  let formError = "";
  if (requireReason && (kept !== 3 || rows.some(r=>r.retire))) formError = "当前完整三情景组不能局部停用；请提交新的完整情景提案。";
  else if (kept < MIN_KEPT) formError = `至少保留 ${MIN_KEPT} 个情景；要整组作废，请在「${LABELS.nav.tasks}」里驳回这组情景。`;
  else if (valid && sum !== 100) formError = `保留的情景概率合计 ${sum}%，需要正好 100%（${sum > 100 ? `多了 ${sum - 100}` : `还差 ${100 - sum}`} 个百分点）。`;
  else if (valid && !changes.length) formError = "概率和现在一样，没有需要保存的修改。";
  const why = String(reason ?? "").trim();
  const reasonError = requireReason && !why ? "请说明哪些证据支持这次概率调整。" : why.length > REASON_MAX ? `理由最多 ${REASON_MAX} 个字，现在 ${why.length} 个。` : "";
  return {errors, sum: valid ? sum : null, kept, changes, formError, reasonError,
          ok: valid && !formError && !reasonError};
}

/** A one-line reason suggested from the changes (the writer records an
    automatic one of the same shape when the analyst leaves it empty). */
export function suggestedReason(rows, check) {
  const byRef = new Map(rows.map(r => [r.ref, r]));
  const moved = [], retired = [];
  for (const c of check.changes) {
    const r = byRef.get(c.ref);
    const name = KEY_LABELS[r?.key] || r?.label || "情景";
    if (c.retire) retired.push(name); else moved.push(`${name} ${r.current}%→${c.percent}%`);
  }
  const parts = [];
  if (moved.length) parts.push(`调整情景概率：${moved.join("；")}`);
  if (retired.length) parts.push(`${SCN.deactivate}：${retired.join("、")}`);
  return parts.join("；");
}

/** The probability-weighted outcome, or why there is none (C09).
    rows carry `target` (price) and/or `ret` (expected return %) per scenario. */
export function weightedOutcome(rows) {
  const kept = rows.filter(r => !r.retire);
  const pct = kept.map(r => Number(String(r.value ?? "").trim()));
  if (!kept.length || pct.some(n => !Number.isFinite(n))) return {status: "invalid"};
  const total = pct.reduce((a, b) => a + b, 0);
  const has = k => kept.every(r => r[k] !== null && r[k] !== undefined && r[k] !== "" && Number.isFinite(Number(r[k])));
  if (!has("target") && !has("ret")) return {status: "unvalued"};
  const weigh = k => kept.reduce((acc, r, i) => acc + Number(r[k]) * pct[i], 0) / (total || 1);
  return {status: "ok", total, target: has("target") ? weigh("target") : null, ret: has("ret") ? weigh("ret") : null};
}

/** The body of the one atomic POST (pure): every scenario in force, the
    probability as a fraction string so "1" never has to be guessed. */
export function groupBody(companyRef, scenarios, check, reason, requestId) {
  const byRef = new Map(check.changes.map(c => [c.ref, c]));
  return {
    company_ref: companyRef, reason: String(reason || "").trim(), request_id: requestId,
    changes: scenarios.map(s => {
      const c = byRef.get(s.scenario_ref);
      const retire = Boolean(c && c.retire);
      const percent = c && !retire ? c.percent : percentOf(s.probability);
      return {scenario_ref: s.scenario_ref, version_ref: s.version_ref, version_hash: s.version_hash, retire,
              probability: retire ? null : (percent / 100).toFixed(2)};
    }),
  };
}

const RECEIPTS = new Map();   // company_ref -> the last receipt, kept across re-renders

export function lastReceipt(companyRef) { return RECEIPTS.get(companyRef) || null; }

function numberOrNull(v) { const n = Number(v); return v === null || v === undefined || v === "" || !Number.isFinite(n) ? null : n; }

/** The editor.  onSaved() re-reads the company and redraws the section. */
export function scenarioEditor(ctx, company, {onSaved, onClose} = {}) {
  const {h, ui, fmt, say} = ctx;
  const order = {bull: 0, base: 1, bear: 2};
  const list = (company.scenarios?.in_force || []).slice().sort((a, b) => (order[a.key] ?? 9) - (order[b.key] ?? 9));
  const requireReason = list.some(s => s.condition_rules_ref === "rule:scenario-partition-12m:1");
  const draftKey = `scenario:${company.company_ref}`;
  const held = ctx.drafts.getJson(draftKey) || {};
  const heldValues = held.values || {};
  let rid = held.rid || null;
  const box = h("form", {class: "v2-panel", id: "scenario-editor", novalidate: true, "aria-labelledby": "scenario-editor-title"});
  const title = h("h3", {id: "scenario-editor-title", tabindex: "-1"}, SCN.edit);
  const restored = Object.keys(heldValues).some(ref => list.some(s => s.scenario_ref === ref));
  const inputs = new Map();
  const rowEls = list.map((s, i) => {
    const sid = `scen-${String(s.key || i).replace(/[^A-Za-z0-9_-]/g, "") || i}`;
    const current = percentOf(s.probability);
    const keep = heldValues[s.scenario_ref];
    const start = keep && keep.value != null ? String(keep.value) : String(current ?? "");
    const name = `${KEY_LABELS[s.key] || say(s.key)}：${say(s.name)}`;
    const input = h("input", {type: "text", inputmode: "numeric", class: "v2-input v2-num", id: sid, value: start,
      "aria-describedby": `${sid}-err`, size: "4", "aria-label": `${name} 的新概率（%）`});
    const slider = h("input", {type: "range", min: String(MIN_PCT), max: String(MAX_PCT), step: "1", class: "v2-range",
      id: `${sid}-range`, value: /^\d+$/.test(start) ? start : String(current ?? 0), "aria-label": `${name} 的新概率滑杆（%）`});
    const retire = h("input", {type: "checkbox", id: `${sid}-retire`, "aria-describedby": "scenario-retire-help"});
    retire.checked = !requireReason && Boolean(keep && keep.retire);
    retire.disabled = requireReason;
    const err = h("div", {class: "v2-field-error", id: `${sid}-err`, "aria-live": "polite"});
    inputs.set(s.scenario_ref, {s, current, input, slider, retire, err,
      target: numberOrNull(s.target_price ?? s.price_target), ret: numberOrNull(s.expected_return_percent ?? s.return_percent)});
    return h("tr", null,
      h("th", {scope: "row"}, h("strong", null, KEY_LABELS[s.key] || say(s.key)), h("div", {class: "v2-note"}, say(s.name))),
      h("td", {class: "v2-num"}, `${current ?? "—"}%`),
      h("td", {class: "v2-num"}, h("span", {class: "v2-input-pct"}, slider, " ", input, " %"), err),
      h("td", null, h("label", {class: "v2-check", for: retire.id}, retire, ` ${SCN.deactivate}`)));
  });
  const sumLine = h("p", {class: "v2-sum", role: "status", "aria-live": "polite"});
  const outcomeLine = h("p", {class: "v2-note", id: "scenario-outcome", "aria-live": "polite"});
  const reason = ui.field({id: "scenario-reason", label: requireReason ? "概率依据（必填）" : "理由（旧情景可选）", required: requireReason, maxLength: REASON_MAX,
    help: requireReason ? "说明支持新权重的证据、判断及不确定性；权重变化描述不能代替依据。" : "历史情景可留空，系统会记录权重变化。", attrs: {rows: "2"}});
  if (held.reason && !reason.input.value) reason.input.value = held.reason;
  const suggestion = h("p", {class: "v2-note", id: "scenario-suggestion"});
  const useSuggestion = ui.button("使用建议理由", () => { reason.input.value = suggestion.getAttribute("data-text") || ""; persist(); }, {variant: "quiet"});
  const formStatus = h("div", {role: "status", "aria-live": "polite"});
  const save = h("button", {type: "submit", class: "v2-btn"}, SCN.save);
  const close = ui.button("收起", () => onClose && onClose(), {variant: "quiet"});
  const balance = ui.button(SCN.rebalance, () => {
    const base = [...inputs.values()].find(x => x.s.key === "base" && !x.retire.checked);
    if (!base) return;
    const others = [...inputs.values()].filter(x => x !== base && !x.retire.checked)
      .reduce((acc, x) => acc + (Number(x.input.value) || 0), 0);
    const want = Math.max(MIN_PCT, Math.min(MAX_PCT, 100 - others));
    base.input.value = String(want); base.slider.value = String(want);
    refresh(); persist();
  }, {variant: "quiet"});
  const discard = ui.button(SCN.discard, () => {
    ctx.drafts.clear(draftKey);
    rid = null;
    for (const x of inputs.values()) { x.input.value = String(x.current ?? ""); x.slider.value = String(x.current ?? 0); x.retire.checked = false; }
    reason.input.value = "";
    refresh();
    formStatus.replaceChildren(ui.receipt("已还原初始配置，情景权重保持现状。"));
  }, {variant: "quiet"});

  const rows = () => [...inputs.values()].map(x => ({ref: x.s.scenario_ref, key: x.s.key, label: x.s.name, current: x.current,
                                                    value: x.input.value, retire: x.retire.checked, target: x.target, ret: x.ret}));
  const persist = () => {
    const values = {};
    for (const x of inputs.values()) {
      if (x.retire.checked || String(x.input.value).trim() !== String(x.current)) values[x.s.scenario_ref] = {value: x.input.value, retire: x.retire.checked};
    }
    if (!Object.keys(values).length && !reason.input.value.trim()) { ctx.drafts.clear(draftKey); return; }
    ctx.drafts.setJson(draftKey, {values, reason: reason.input.value, rid});
  };
  const refresh = () => {
    const all = rows();
    const check = checkScenarioForm(all, reason.input.value, requireReason);
    for (const x of inputs.values()) {
      const message = check.errors[x.s.scenario_ref] || "";
      x.err.textContent = message;
      if (message) x.input.setAttribute("aria-invalid", "true"); else x.input.removeAttribute("aria-invalid");
      x.input.disabled = x.retire.checked;
      x.slider.disabled = x.retire.checked;
    }
    sumLine.setAttribute("data-tone", check.sum === 100 ? "ok" : "warn");
    sumLine.textContent = check.sum == null ? "合计：有情景的概率还没填对。"
      : `保留的 ${check.kept} 个情景合计 ${check.sum}%${check.sum === 100 ? "，可以保存。" : `（需要 100%，${check.sum > 100 ? `多了 ${check.sum - 100}` : `还差 ${100 - check.sum}`} 个百分点）。`}`;
    const outcome = weightedOutcome(all);
    outcomeLine.textContent = outcome.status === "ok"
      ? `敏感性测算（调整情景概率）：${outcome.target != null ? `预期目标价约 ${fmt.number(outcome.target, 2)} 美元` : ""}${outcome.target != null && outcome.ret != null ? " ｜ " : ""}${outcome.ret != null ? `加权期望回报率约 ${outcome.ret >= 0 ? "+" : "−"}${fmt.number(Math.abs(outcome.ret), 1)}%` : ""}（随输入即时重算）。`
      : outcome.status === "unvalued" ? "各情景尚无目标价或回报区间，暂不能计算概率加权目标价；权重只表示可能性。"
      : "";
    const text = suggestedReason(all, check);
    suggestion.setAttribute("data-text", text);
    suggestion.textContent = text ? `${requireReason ? "本次权重变化（请另填证据依据）" : "建议理由"}：${text}` : "";
    useSuggestion.hidden = requireReason || !text;
    if (useSuggestion.style) useSuggestion.style.display = !requireReason && text ? "" : "none";
    return check;
  };
  for (const x of inputs.values()) {
    x.input.addEventListener("input", () => { if (/^\d+$/.test(String(x.input.value).trim())) x.slider.value = String(x.input.value).trim(); refresh(); persist(); });
    x.slider.addEventListener("input", () => { x.input.value = String(x.slider.value); refresh(); persist(); });
    x.retire.addEventListener("change", () => { refresh(); persist(); });
  }
  reason.input.addEventListener("input", persist);

  box.addEventListener("submit", async e => {
    e.preventDefault();
    const check = refresh();
    if (!check.ok) {
      const firstBad = [...inputs.values()].find(x => check.errors[x.s.scenario_ref]);
      if (check.reasonError) reason.setError(check.reasonError);
      formStatus.replaceChildren(ui.receipt(check.formError || check.reasonError || "请先改正标出的概率。", "danger"));
      (firstBad ? firstBad.input : check.reasonError ? reason.input : save).focus();
      return;
    }
    save.disabled = true;
    if (!rid) { rid = ctx.requestId(); persist(); }
    const why = reason.input.value.trim();
    formStatus.replaceChildren(ui.receipt(`正在保存 ${check.changes.length} 项权重修改…`));
    const at = fmt.datetime(new Date().toISOString());
    let failed = null, out = null, uncertain = false;
    try {
      out = await ctx.api.post("/v2/data/scenarios/revise-group", groupBody(company.company_ref, list, check, why, rid));
    } catch (err) { failed = said(err, "没有保存"); uncertain = err?.status === 0 || err?.status >= 500; }
    save.disabled = false;
    if (!failed) {
      ctx.drafts.clear(draftKey);
      rid = null;
      const recorded = String(out?.reason || why || suggestedReason(rows(), check)).replace(/[。.!！]+$/, "");
      const receiptText = say(out?.message || suggestedReason(rows(), check)).replace(/[。.!！]+$/, "");
      RECEIPTS.set(company.company_ref, {tone: null, fresh: true,
        text: `情景权重配置已成功更新存档（${at}）：${receiptText}。理由：${recorded}${out?.reason_auto ? "（自动记录）" : ""}。权重变化较大时，系统会同步上调对触发条件的监测频率。`});
    } else {
      persist();
      RECEIPTS.set(company.company_ref, {tone: "danger", fresh: true,
        text: uncertain ? `尚未确认保存结果：${failed}。你的修改已保留；请先刷新核对当前概率。`
          : `没有保存，整组都没有生效：${failed}。你的修改还留在表单里，可以改正后再保存。`});
    }
    const r = RECEIPTS.get(company.company_ref);
    formStatus.replaceChildren(ui.receipt(r.text, r.tone));
    ctx.announce && ctx.announce(failed ? (uncertain ? "尚未确认情景权重是否保存" : "情景权重没有保存") : "情景权重配置已保存");
    if (onSaved) await onSaved({done: failed ? [] : check.changes, failed: failed ? [failed] : []});
  });

  box.append(title,
    ui.note(`请按投资判断分配各情景权重（支持 ${MIN_PCT}%–${MAX_PCT}% 整数输入；情景合计需严格等于 100%，系统不做未经确认的自动归一；${requireReason ? "当前完整三情景组全部保留" : `历史组至少保留 ${MIN_KEPT} 个情景`}）。`),
    h("p", {class: "v2-note", id: "scenario-retire-help"},
      requireReason ? "完整三情景分区不能局部停用，调整条件需提出新的完整情景组。" : `「${SCN.deactivate}」：${SCN.deactivate_help}移出不能在这里撤销，需要时由新的情景提案重新提出。`),
    restored ? ui.alert("已恢复你上次没有保存的修改。", "info") : null,
    h("div", {class: "v2-table-wrap", role: "region", "aria-label": "情景概率", tabindex: "0"},
      h("table", {class: "v2-table"}, h("caption", null, "当前概率与新概率"),
        h("thead", null, h("tr", null, ["情景", "当前权重", "新权重", SCN.deactivate].map((t, i) => h("th", {scope: "col", class: i === 1 || i === 2 ? "v2-num" : null}, t)))),
        h("tbody", null, rowEls))),
    sumLine, outcomeLine, h("div", {class: "v2-actions"}, balance),
    reason.wrap, suggestion, h("div", {class: "v2-actions"}, useSuggestion),
    h("div", {class: "v2-actions"}, save, close, discard), formStatus);
  refresh();
  return box;
}
