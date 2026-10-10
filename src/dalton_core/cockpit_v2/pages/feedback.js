/* Feedback on one research output (review A03-A08, C10): the five verdicts the
   analyst journal takes, an optional note, and a receipt that stays on the
   object.  Writes go to the existing POST /v1/cockpit/feedback (the writer
   records them as the owner's human: principal); nothing here is new
   authority.  A binding is what the server attached to the object:
   {target_ref, target_hash, target_kind, company_ref?, enabled, entries}. */

import {said} from "./common.js";
export const VERDICTS = Object.freeze([
  {value: "read", label: "已阅读"},
  {value: "useful", label: "有参考价值"},
  {value: "needs_more_evidence", label: "证据不够"},
  {value: "disagree", label: "不同意这个结论"},
  {value: "revise", label: "需要重写"},
]);
export const NOTE_MAX = 4000;
const HASH = /^[0-9a-f]{64}$/;
let serial = 0;   // ids stay unique when one object is shown twice

/** The request body for one verdict, or an error sentence (pure). */
export function feedbackRequest(binding, verdict, note) {
  if (!binding || !binding.target_ref || !HASH.test(String(binding.target_hash || "")) || !binding.target_kind)
    return {error: "这条内容暂时不能反馈：缺少它的版本信息。请刷新页面后再试。"};
  if (!VERDICTS.some(v => v.value === verdict)) return {error: "请选择一种反馈。"};
  const text = String(note || "").trim();
  if (text.length > NOTE_MAX) return {error: `补充意见最多 ${NOTE_MAX} 个字，现在 ${text.length} 个。`};
  const body = {target_ref: binding.target_ref, target_hash: binding.target_hash,
                target_kind: binding.target_kind, verdict};
  if (binding.company_ref) body.company_ref = binding.company_ref;
  if (text) body.note = text;
  return {body};
}

/** The inline feedback block.  `title` names the object ("这份初步研究报告"). */
export function feedbackBar(ctx, binding, {title = "这条内容", id} = {}) {
  const {h, ui, fmt} = ctx;
  const box = h("div", {class: "v2-feedback"});
  if (!binding) return null;
  if (binding.enabled === false) {
    box.append(ui.note("这个研究项目还没有开通反馈记录，暂时不能在这里留反馈。"));
    return box;
  }
  const base = id || `fb-${String(binding.target_ref).replace(/[^A-Za-z0-9_-]/g, "").slice(-20)}-${++serial}`;
  const given = new Set((binding.entries || []).map(e => e.verdict));
  const groupLabel = `对${title}的反馈`;
  const note = ui.field({id: `${base}-note`, label: "补充意见（选填）", control: "textarea", maxLength: NOTE_MAX,
    help: "写下理由或希望补充的证据；和反馈一起存档。", attrs: {rows: "2", class: "v2-input-short"}});
  ctx.drafts.bind(note.input, `feedback:${binding.target_ref}`);
  const status = h("div", {role: "status", "aria-live": "polite"});
  const buttons = VERDICTS.map(v => h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle",
    "aria-pressed": given.has(v.value) ? "true" : "false", "data-verdict": v.value}, v.label));
  const send = async button => {
    const verdict = button.getAttribute("data-verdict");
    if (!note.check()) { note.input.focus(); return; }
    const req = feedbackRequest(binding, verdict, note.input.value);
    if (req.error) { status.replaceChildren(ui.receipt(req.error, "danger")); return; }
    for (const b of buttons) b.disabled = true;
    status.replaceChildren(ui.receipt("正在记下…"));
    try {
      const out = await ctx.api.post("/v1/cockpit/feedback", req.body);
      button.setAttribute("aria-pressed", "true");
      ctx.drafts.clear(`feedback:${binding.target_ref}`);
      note.input.value = "";
      const label = VERDICTS.find(v => v.value === verdict).label;
      status.replaceChildren(ui.receipt(out && out.duplicate
        ? `「${label}」之前已经记过了，这次没有重复记录。`
        : `已记下「${label}」${req.body.note ? "和你的补充意见" : ""}（${fmt.datetime(new Date().toISOString())}）。它会出现在每周复盘里；反馈不是审批，不会直接改变这条结论。`));
      ctx.announce && ctx.announce("反馈已记下");
    } catch (e) {
      status.replaceChildren(ui.receipt(`没有记下：${said(e)}。你的补充意见还保留在输入框里。`, "danger"));
    } finally { for (const b of buttons) b.disabled = false; }
  };
  for (const b of buttons) b.addEventListener("click", () => send(b));
  box.append(h("div", {class: "v2-toggle-row", role: "group", "aria-label": groupLabel}, buttons), note.wrap, status);
  if (given.size) box.append(ui.note(`你之前给过：${VERDICTS.filter(v => given.has(v.value)).map(v => v.label).join("、")}`));
  return box;
}

/** Collapsed form of the block, for long lists (one per evidence item). */
export function feedbackDisclosure(ctx, binding, options = {}) {
  const {h} = ctx;
  const bar = feedbackBar(ctx, binding, options);
  if (!bar) return null;
  const given = (binding.entries || []).length;
  const label = options.summary || "反馈";
  return h("details", {class: "v2-more v2-feedback-more"}, h("summary", null, given ? `${label}（已给 ${given} 次）` : label), bar);
}
