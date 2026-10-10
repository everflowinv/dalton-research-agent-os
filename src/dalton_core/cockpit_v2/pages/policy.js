/* 研究设置 → 投资建议规则: the full editor (review P01-P06, S07).
   Reads GET /v1/cockpit/conviction-policy (?left=&right= for the comparison);
   publishes through the existing POST /v1/cockpit/conviction_policy with the
   version it was edited from (prior_version_ref), a required reason and a
   request id.  The server validates again; these checks only put the message
   next to the field before anything is sent. */

import {said} from "./common.js";
import {LABELS} from "../copy.js";
export const HORIZON_KEYS = Object.freeze(["3_6_months", "6_12_months", "1_3_years", "3_5_years"]);
const INT_FIELDS = {
  required_scenarios: [3, 3], min_invalidators: [1, 8], max_valuation_age_days: [1, 90], max_drafts_per_day: [0, 50],
};
export const REASON_MAX = 1000;

const decimal = text => /^\d+(\.\d+)?$/.test(String(text ?? "").trim());

/** Validate a form state (pure).  Returns {policy, errors}; errors are keyed
    by field (horizon fields as "<field>.<horizon>"). */
export function checkPolicy(values) {
  const v = values || {};
  const errors = {};
  const policy = {};
  policy.min_evidence_confidence = v.min_evidence_confidence;
  if (!["low", "medium", "high"].includes(v.min_evidence_confidence)) errors.min_evidence_confidence = `请选择${LABELS.policy.confidence}`;
  for (const [field, [lo, hi]] of Object.entries(INT_FIELDS)) {
    const text = String(v[field] ?? "").trim();
    if (!/^\d+$/.test(text) || Number(text) < lo || Number(text) > hi) errors[field] = `请填 ${lo}–${hi} 之间的整数`;
    else policy[field] = Number(text);
  }
  for (const field of ["allowed_horizons", "allowed_directions"]) {
    const list = Array.isArray(v[field]) ? v[field] : [];
    if (!list.length) errors[field] = "至少选择一项";
    policy[field] = list.slice();
  }
  if (policy.allowed_horizons.length!==1 || policy.allowed_horizons[0]!=="6_12_months") errors.allowed_horizons="新建议统一按价格日到12个月后估值";
  const ratio=String(v.min_upside_downside_ratio ?? "").trim();
  if (!decimal(ratio) || Number(ratio)<=0 || Number(ratio)>1000) errors.min_upside_downside_ratio="请填大于0且不超过1000的收益／损失倍数";
  else policy.min_upside_downside_ratio=ratio;
  for (const field of ["min_expected_return_percent", "same_direction_min_expected_return_percent"]) {
    policy[field] = {};
    for (const key of HORIZON_KEYS) {
      const text = String(v[field]?.[key] ?? "").trim();
      if (!decimal(text) || Number(text) > 1000) errors[`${field}.${key}`] = "请填 0–1000 之间的数字（百分比）";
      else policy[field][key] = text;
    }
  }
  for (const key of HORIZON_KEYS) {
    const a = policy.min_expected_return_percent[key], b = policy.same_direction_min_expected_return_percent[key];
    if (a != null && b != null && Number(b) < Number(a) && !errors[`same_direction_min_expected_return_percent.${key}`])
      errors[`same_direction_min_expected_return_percent.${key}`] = `与市场同向的门槛不能低于一般门槛（${a}%）`;
  }
  policy.thesis_scope = v.thesis_scope;
  if (!v.thesis_scope) errors.thesis_scope = "请选择";
  const cap = String(v.daily_drafting_cost_cap_usd ?? "").trim();
  if (!decimal(cap) || Number(cap) > 100) errors.daily_drafting_cost_cap_usd = "请填 0–100 之间的美元金额";
  else policy.daily_drafting_cost_cap_usd = cap;
  return {policy, errors, ok: !Object.keys(errors).length};
}

export function sameValues(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

/* 2026-10-09 (MUSE P0 #7, independent S08/S09, financial 4.14): three
   presets set the research thresholds; operating limits (起草次数, 起草费用,
   期限与方向的范围) are left as they are.  Every number stays editable under
   高级, each with what it means and its unit. */
export const PRESETS = Object.freeze([
  {key: "steady", label: "稳健", note: "证据要求高、回报门槛高，建议更少但更有把握。",
   values: {min_upside_downside_ratio:"3", min_evidence_confidence: "high", required_scenarios: "3", min_invalidators: "2", max_valuation_age_days: "14",
            min_expected_return_percent: {"3_6_months": "15", "6_12_months": "20", "1_3_years": "30", "3_5_years": "50"},
            same_direction_min_expected_return_percent: {"3_6_months": "20", "6_12_months": "25", "1_3_years": "40", "3_5_years": "60"}}},
  {key: "balanced", label: "平衡", note: "常用设置：中等证据要求，回报门槛适中。",
   values: {min_upside_downside_ratio:"2", min_evidence_confidence: "medium", required_scenarios: "3", min_invalidators: "1", max_valuation_age_days: "30",
            min_expected_return_percent: {"3_6_months": "10", "6_12_months": "15", "1_3_years": "25", "3_5_years": "40"},
            same_direction_min_expected_return_percent: {"3_6_months": "15", "6_12_months": "20", "1_3_years": "30", "3_5_years": "50"}}},
  {key: "aggressive", label: "进取", note: "门槛较低，更早给出建议；需要更频繁地复核。",
   values: {min_upside_downside_ratio:"2", min_evidence_confidence: "medium", required_scenarios: "3", min_invalidators: "1", max_valuation_age_days: "45",
            min_expected_return_percent: {"3_6_months": "5", "6_12_months": "10", "1_3_years": "15", "3_5_years": "25"},
            same_direction_min_expected_return_percent: {"3_6_months": "8", "6_12_months": "12", "1_3_years": "20", "3_5_years": "30"}}},
]);

/** Which preset a set of values matches exactly, or null (pure). */
export function matchPreset(values) {
  const v = values || {};
  const norm = x => JSON.stringify(x, (k, val) => (typeof val === "number" ? String(val) : val));
  return PRESETS.find(p => Object.entries(p.values).every(([k, want]) => norm(v[k]) === norm(want)))?.key || null;
}

/* What each threshold means, in research terms, with its unit (S08). */
const FIELD_HELP = {
  min_evidence_confidence: "底稿证据充分度准入门槛：低于这一档时，不起草投资建议。",
  thesis_scope: "投资建议针对的范围。",
  required_scenarios: "新建议固定三个互斥且穷尽的情景：悲观、基准、乐观；历史规则仍保留原数量。",
  min_upside_downside_ratio:"单位：倍。潜在上涨收益除以潜在下跌损失；必须同时有正收益和负损失，且不低于这一门槛，才起草建议。",
  min_invalidators: "单位：项。至少写明几个「出现就推翻观点」的证伪条件；可填 1–8。",
  max_valuation_age_days: "单位：天。估值数据超过这么多天就视为过期，需要更新后才起草；可填 1–90。",
  max_drafts_per_day: "单位：次/天。每天最多起草几份建议；0 表示暂停起草；可填 0–50。",
  daily_drafting_cost_cap_usd: "单位：美元/天。起草建议每天最多花多少；可填 0–100。",
  allowed_horizons: "新建议统一按最新价格日到12个月后估值。其他期限仅保留用于读取历史建议，不再用于新发布。",
  allowed_directions: "允许的建议方向。",
  min_expected_return_percent: "单位：%。最低预期回报率（Hurdle Rate）：预期回报至少达到这个水平，才给出建议（按持有期限分别设定）。",
  same_direction_min_expected_return_percent: "单位：%。和市场主流看法方向相同时，回报门槛要更高（不能低于上一行）。",
};

/** The editor; `payload` is the conviction-policy view. */
export function policyEditor(ctx, payload, {onPublished} = {}) {
  const {h, ui, fmt, say} = ctx;
  const form = payload.form || {};
  const labels = form.labels || {};
  const options = form.options || {};
  const base = String(form.prior_version_ref || "");
  const start = {...(form.values || {}),required_scenarios:3,allowed_horizons:["6_12_months"],min_upside_downside_ratio:form.values?.min_upside_downside_ratio || "2"};
  const held = ctx.drafts.getJson("policy:form");
  let notice = ui.note("本次编辑采用三情景、12个月及最低收益／损失比规则；原有历史记录和其他期限门槛仍保留。发布后才生效。");
  let values = JSON.parse(JSON.stringify(start));
  if (held && held.values) {
    if (held.base === base) { values = {...values, ...held.values}; notice = ui.alert("已恢复你上次没有发布的修改（尚未生效）。", "info"); }
    else { ctx.drafts.clear("policy:form"); notice = ui.alert("规则在你修改期间已经发布了新版本，之前未发布的修改已作废；请在下面的新版本上重新修改。"); }
  }
  const box = h("form", {class: "v2-panel", id: "policy-editor", novalidate: true, "aria-labelledby": "policy-editor-title"});
  const errs = new Map();
  const keyOf = new Map();   // input element -> the field it belongs to
  const errorEl = key => { const el = h("div", {class: "v2-field-error", id: `pol-${key.replace(/\./g, "-")}-err`, "aria-live": "polite"}); errs.set(key, el); return el; };
  const controls = new Map();
  const field = (key, control, help) => {
    const id = `pol-${key}`;
    control.setAttribute("id", id);
    control.setAttribute("aria-describedby", `${id}-err`);
    controls.set(key, control);
    keyOf.set(control, key);
    return h("div", {class: "v2-field"}, h("label", {for: id}, say(labels[key] || key)), help ? h("div", {class: "v2-help"}, help) : null, control, errorEl(key));
  };
  const select = (key, opts) => h("select", null, (opts || []).map(o => h("option", {value: o.value, selected: o.value === values[key] || null}, say(o.label))));
  const number = (key, unit) => h("input", {type: "text", inputmode: ["美元","倍"].includes(unit) ? "decimal" : "numeric", class: "v2-input", value: values[key] ?? "", size: "6"});
  const checks = (key, opts) => {
    const set = new Set(values[key] || []);
    const boxes = (opts || []).map(o => {
      const input = h("input", {type: "checkbox", value: o.value, id: `pol-${key}-${o.value}`});
      input.checked = set.has(o.value);
      if (key==="allowed_horizons") input.disabled=true;
      keyOf.set(input, key);
      return h("label", {class: "v2-check", for: input.id}, input, ` ${say(o.label)}`);
    });
    const fs = h("fieldset", {class: "v2-fieldset", id: `pol-${key}`, "aria-describedby": `pol-${key}-err`},
      h("legend", null, say(labels[key] || key)), h("div", {class: "v2-check-row"}, boxes), errorEl(key));
    controls.set(key, fs);
    return fs;
  };
  const horizonLabel = Object.fromEntries((options.allowed_horizons || []).map(o => [o.value, say(o.label)]));
  const thresholds = key => {
    const inputs = HORIZON_KEYS.map(k => {
      const id = `pol-${key}-${k}`;
      const input = h("input", {type: "text", inputmode: "decimal", class: "v2-input v2-num", id, size: "5",
        value: values[key]?.[k] ?? "", disabled:k!=="6_12_months", "aria-describedby": `pol-${key}-${k}-err`});
      controls.set(`${key}.${k}`, input);
      keyOf.set(input, `${key}.${k}`);
      return h("div", {class: "v2-field v2-field-inline"}, h("label", {for: id}, k === "6_12_months" ? "12个月（当前估值窗口）" : `${horizonLabel[k] || k}（历史规则）`),
        h("span", {class: "v2-input-pct"}, input, " %"), errorEl(`${key}.${k}`));
    });
    return h("fieldset", {class: "v2-fieldset"}, h("legend", null, say(labels[key] || key)), h("div", {class: "v2-grid v2-grid-tight"}, inputs));
  };
  const reason = ui.field({id: "policy-reason", label: "发布理由", required: true, maxLength: REASON_MAX,
    help: "为什么要改；会和这一版规则一起存档。", attrs: {rows: "2"}});
  ctx.drafts.bind(reason.input, "policy:reason");
  const status = h("div", {role: "status", "aria-live": "polite"});
  const publish = h("button", {type: "submit", class: "v2-btn"}, "发布新一版规则");
  const reset = ui.button("恢复为当前生效的规则", () => {
    ctx.drafts.clear("policy:form");
    write(start);
    status.replaceChildren(ui.receipt("表单已恢复为当前生效的规则；还没有发布任何修改。"));
  }, {variant: "quiet"});

  const read = () => {
    const out = {};
    for (const [key, el] of controls) {
      if (key.includes(".")) {
        const [f, k] = key.split(".");
        (out[f] ||= {})[k] = el.value;
      } else if (el.tagName === "FIELDSET") out[key] = [...el.querySelectorAll("input")].filter(i => i.checked).map(i => i.value);
      else out[key] = el.value;
    }
    return out;
  };
  const write = v => {
    for (const [key, el] of controls) {
      if (key.includes(".")) { const [f, k] = key.split("."); el.value = v[f]?.[k] ?? ""; }
      else if (el.tagName === "FIELDSET") { const set = new Set(v[key] || []); for (const i of [...el.querySelectorAll("input")]) i.checked = set.has(i.value); }
      else el.value = v[key] ?? "";
    }
    show({});
  };
  const show = errors => {
    for (const [key, el] of errs) {
      el.textContent = errors[key] || "";
      const control = controls.get(key);
      if (control) { if (errors[key]) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid"); }
    }
  };
  const persist = () => {
    const now = read();
    const {policy} = checkPolicy(now);
    if (sameValues(normalise(now), normalise(start))) ctx.drafts.clear("policy:form");
    else ctx.drafts.setJson("policy:form", {base, values: now});
    return policy;
  };
  const normalise = v => JSON.parse(JSON.stringify({...v, allowed_horizons: [...(v.allowed_horizons || [])].sort(), allowed_directions: [...(v.allowed_directions || [])].sort(),
    min_upside_downside_ratio: String(v.min_upside_downside_ratio ?? ""), required_scenarios: String(v.required_scenarios ?? ""), min_invalidators: String(v.min_invalidators ?? ""),
    max_valuation_age_days: String(v.max_valuation_age_days ?? ""), max_drafts_per_day: String(v.max_drafts_per_day ?? ""),
    daily_drafting_cost_cap_usd: String(v.daily_drafting_cost_cap_usd ?? "")}));

  // Presets fill the form; nothing is published until 「发布新一版规则」.
  const presetNote = h("p", {class: "v2-note", role: "status", "aria-live": "polite"});
  const markPreset = () => {
    const key = matchPreset(read());
    const hit = PRESETS.find(p => p.key === key);
    for (const b of presetButtons) b.setAttribute("aria-pressed", b.getAttribute("data-preset") === key ? "true" : "false");
    presetNote.textContent = hit ? `当前为「${hit.label}」：${hit.note}` : "当前为自定义设置（展开「高级」可以看到每一项）。";
  };
  const presetButtons = PRESETS.map(preset => {
    const b = h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "data-preset": preset.key, "aria-pressed": "false",
      "aria-label": `使用「${preset.label}」预设：${preset.note}`}, preset.label);
    b.addEventListener("click", () => {
      const now = read();
      write({...now, ...JSON.parse(JSON.stringify(preset.values))});
      persist();
      markPreset();
      status.replaceChildren(ui.receipt(`已按「${preset.label}」填好表单，尚未发布；确认后点「发布新一版规则」。`));
    });
    return b;
  });
  const advanced = h("details", {class: "v2-more", id: "policy-advanced"}, h("summary", null, "高级：逐项调整（共 15 项，含单位和说明）"),
    h("div", {class: "v2-grid"},
      field("min_evidence_confidence", select("min_evidence_confidence", options.min_evidence_confidence), FIELD_HELP.min_evidence_confidence),
      field("thesis_scope", select("thesis_scope", options.thesis_scope), FIELD_HELP.thesis_scope),
      field("required_scenarios", h("input",{type:"text",value:"3",readonly:true}), FIELD_HELP.required_scenarios),
      field("min_upside_downside_ratio", number("min_upside_downside_ratio","倍"), FIELD_HELP.min_upside_downside_ratio),
      field("min_invalidators", number("min_invalidators"), FIELD_HELP.min_invalidators),
      field("max_valuation_age_days", number("max_valuation_age_days"), FIELD_HELP.max_valuation_age_days),
      field("max_drafts_per_day", number("max_drafts_per_day"), FIELD_HELP.max_drafts_per_day),
      field("daily_drafting_cost_cap_usd", number("daily_drafting_cost_cap_usd", "美元"), FIELD_HELP.daily_drafting_cost_cap_usd)),
    ui.note(FIELD_HELP.allowed_horizons),
    checks("allowed_horizons", options.allowed_horizons),
    checks("allowed_directions", options.allowed_directions),
    ui.note(FIELD_HELP.min_expected_return_percent),
    thresholds("min_expected_return_percent"),
    ui.note(FIELD_HELP.same_direction_min_expected_return_percent),
    thresholds("same_direction_min_expected_return_percent"));
  box.append(h("h3", {id: "policy-editor-title", tabindex: "-1"}, `编辑${LABELS.policy.title}`),
    ui.note("这些投资纪律决定系统起草投资建议前必须满足的条件；发布后从下一次起草开始生效，已有建议不会被改写。"),
    notice,
    h("div", {class: "v2-presets", role: "group", "aria-label": "规则预设"}, presetButtons), presetNote,
    advanced,
    reason.wrap, h("div", {class: "v2-actions"}, publish, reset), status);
  markPreset();
  box.addEventListener("change", () => markPreset());
  // Inline validation: a field is checked when it is left (change), and a
  // shown error clears as soon as the value is right again.
  const recheck = target => {
    const key = keyOf.get(target);
    if (!key) return;
    const {errors} = checkPolicy(read());
    const el = errs.get(key);
    if (el && (errors[key] || el.textContent)) {
      el.textContent = errors[key] || "";
      const control = controls.get(key);
      if (errors[key]) control.setAttribute("aria-invalid", "true"); else control.removeAttribute("aria-invalid");
    }
  };
  box.addEventListener("input", e => { if (errs.get(keyOf.get(e.target))?.textContent) recheck(e.target); persist(); });
  box.addEventListener("change", e => { recheck(e.target); persist(); });
  box.addEventListener("submit", async e => {
    e.preventDefault();
    const now = read();
    const {policy, errors, ok} = checkPolicy(now);
    show(errors);
    const reasonOk = reason.check();
    if (!ok || !reasonOk) {
      const first = Object.keys(errors)[0];
      if (first) advanced.open = true;
      status.replaceChildren(ui.receipt(ok ? "请先写发布理由。" : "有几项规则没有填对，已在「高级」里标出。", "danger"));
      (first ? controls.get(first)?.querySelector?.("input") || controls.get(first) : reason.input)?.focus?.();
      return;
    }
    if (sameValues(normalise(now), normalise(start))) {
      status.replaceChildren(ui.receipt("规则和当前生效的一样，没有需要发布的修改。", "danger"));
      return;
    }
    publish.disabled = true;
    status.replaceChildren(ui.receipt("正在发布…"));
    const requestId = ctx.drafts.get("policy:request") || ctx.requestId();
    ctx.drafts.set("policy:request", requestId);
    try {
      const out = await ctx.api.post("/v1/cockpit/conviction_policy", {policy, reason: reason.input.value.trim(),
        prior_version_ref: form.prior_version_ref || null, request_id: requestId});
      for (const k of ["policy:form", "policy:reason", "policy:request"]) ctx.drafts.clear(k);
      reason.input.value = "";
      const message = say(out?.message || "已发布");
      status.replaceChildren(ui.receipt(`${message}（${fmt.datetime(new Date().toISOString())}）。新规则从下一次起草投资建议开始生效。`));
      ctx.announce && ctx.announce(message);
      onPublished && onPublished(out);
    } catch (err) {
      ctx.drafts.clear("policy:request");
      status.replaceChildren(ui.receipt(`没有发布：${said(err)}。你的修改还保留在表单里。`, "danger"));
    } finally { publish.disabled = false; }
  });
  return box;
}

/** Versions and the two-version comparison (#/settings?focus=rules&left=…&right=…). */
export function policyHistory(ctx, payload, {onCompare} = {}) {
  const {h, ui, fmt, say} = ctx;
  const out = h("div", {id: "policy-history"});
  const versions = payload.versions || [];
  out.append(h("h3", null, "已发布的版本"), versions.length
    ? ui.table({caption: "最新的在最上面", columns: [
        {key: "v", label: "版本", render: x => `第 ${x.version} 版`},
        {key: "at", label: "发布时间", render: x => fmt.datetime(x.created_at)},
        {key: "who", label: "发布人", render: x => say(x.who || "—")},
        {key: "reason", label: "理由", render: x => say(x.reason || "—")},
      ], rows: versions})
    : ui.empty(`还没有发布过任何版本：当前执行${LABELS.policy.default}。`));
  const choices = payload.choices || [];
  const cmp = payload.compare || {};
  const pick = (id, label, current) => {
    const f = ui.field({id, label, control: "select"});
    f.input.append(...choices.map(c => h("option", {value: c.key, selected: c.key === current || null}, say(c.label))));
    f.input.addEventListener("change", () => onCompare && onCompare(left.input.value, right.input.value));
    return f;
  };
  // S11 / financial 2.3: 基准版本 vs 对比版本, only when there is history.
  if (versions.length < 1) return out;
  const left = pick("policy-left", "基准版本（旧）", cmp.left), right = pick("policy-right", "对比版本（新）", cmp.right);
  const rows = cmp.rows || [];
  const changed = rows.filter(r => r.changed);
  out.append(h("details", {class: "v2-more", id: "policy-compare"}, h("summary", null, LABELS.policy.compare),
    h("div", {class: "v2-grid"}, left.wrap, right.wrap),
    rows.length ? ui.table({caption: changed.length ? `有 ${changed.length} 项变化，标为「有变化」` : "两个版本没有差别", columns: [
      {key: "label", label: "项目", render: r => say(r.label)},
      {key: "l", label: "基准版本", render: r => say(r.left_label)},
      {key: "r", label: "对比版本", render: r => say(r.right_label)},
      {key: "c", label: "变更明细", render: r => (r.changed ? ui.tag("有变化", "warn") : "—")},
    ], rows: changed.length ? changed : rows}) : ui.empty("没有可对比的内容。")));
  return out;
}
