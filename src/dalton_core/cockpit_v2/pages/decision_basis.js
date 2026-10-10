/* The reading a decision rests on, as the old cockpit showed it on each
   approval card: the investment call's three parts (证据置信度 / 情景与期望回报 /
   与市场看法), the memo verification record, the zero-based review, and an
   after-the-fact review (复盘).  Read-only; the decision itself stays
   POST /v1/cockpit/decide on the card. */

const PENDING_TEXT = "正文正在检查文字表达，完成后会显示。";
/* 2026-10-09 (financial 痛点5): a version whose review may still run is
   shown as its draft with this badge (final_text.show_draft), never blank. */
export const PENDING_BADGE = "初稿（待审核签发）";
const STATUS_WORDS = {pass: "通过", passed: "通过", ok: "通过", verified: "已核验", fail: "未通过", failed: "未通过",
  missing: "缺少", pending: "待处理", unknown: "未确认", complete: "完成", completed: "完成", skipped: "跳过"};

const pending = holder => holder?.final_text?.status === "pending" && holder?.final_text?.show_draft !== true;
const drafting = holder => holder?.final_text?.status === "pending" && holder?.final_text?.show_draft === true;
const text = (ctx, holder, value) => (typeof value !== "string" || !value ? value || "" : pending(holder) ? PENDING_TEXT : ctx.say(value));
const word = (ctx, v) => STATUS_WORDS[String(v || "").toLowerCase()] || (v ? ctx.say(String(v)) : "已记录");

/** One after-the-fact review: what we expected, what happened, why. */
export function reflectionCard(ctx, r, {as = "li"} = {}) {
  const {h, ui, fmt, say} = ctx;
  const isPending = pending(r);
  const draft = r.final_text?.status === "draft";
  const follow = [...(r.followup_tracking || []).map(f => `${say(f.source_label || "资料")}改为${say(f.interval_label || "新频率")}`),
                  ...(r.followup_research || []).map(f => text(ctx, r, f.question))].filter(Boolean);
  return h(as, {class: "v2-card"},
    h("div", {class: "v2-card-meta"}, r.at ? h("span", null, fmt.datetime(r.at)) : null, ui.tag(say(r.trigger_label || "复盘")),
      draft ? ui.tag(say(r.final_text.label || PENDING_BADGE), "warn") : null,
      drafting(r) ? ui.tag(say(r.final_text.badge || PENDING_BADGE), "warn") : null),
    isPending ? ui.note(PENDING_TEXT) : ui.kv([
      ["我们当时以为", text(ctx, r, r.what_we_expected) || "—"],
      ["实际发生的", text(ctx, r, r.what_happened) || "—"],
      r.why ? ["原因", text(ctx, r, r.why)] : null,
      r.market_view?.summary ? ["和市场相比", text(ctx, r, r.market_view.summary)] : null,
      r.convergence_pathway ? ["市场会怎样向我们靠拢", text(ctx, r, r.convergence_pathway)] : null,
    ].filter(Boolean)),
    (r.missed_debates || []).length && !isPending ? h("details", {class: "v2-more"}, h("summary", null, `可能漏掉的争论（${r.missed_debates.length}）`),
      h("ul", null, r.missed_debates.map(m => h("li", null, text(ctx, r, m.question))))) : null,
    follow.length && !isPending ? h("p", {class: "v2-note"}, `建议跟进：${follow.join("；")}`) : null,
    r.note ? ui.note(say(r.note)) : null);
}

function convictionBlock(ctx, c) {
  const {h, ui, say} = ctx;
  return h("details", {class: "v2-more", open: true}, h("summary", null, "投资建议的三部分依据"),
    h("h4", null, `① 底稿证据充分度：${say(c.evidence?.level_label || "未注明")}`),
    c.evidence?.statement ? h("p", null, say(c.evidence.statement)) : null,
    c.evidence?.rests_on ? ui.note(`依据：${say(c.evidence.rests_on)}`) : null,
    h("h4", null, "② 情景与期望回报"),
    (c.scenarios || []).length ? ui.table({caption: "每个情景的概率、股价回报和触发条件", columns: [
      {key: "n", label: "情景", render: s => `${say(s.name)}${s.held ? "（已采纳的情景）" : ""}`},
      {key: "p", label: "概率", num: true, render: s => say(String(s.probability ?? "—"))},
      {key: "r", label: "股价回报", num: true, render: s => say(String(s.payoff ?? "—"))},
      {key: "t", label: "触发条件", render: s => say(s.trigger || "—")},
    ], rows: c.scenarios}) : ui.empty("没有列出情景。"),
    c.expected_return ? h("p", null, h("strong", null, say(c.expected_return.line || "")), c.expected_return.stock ? `（股价期望变动 ${say(c.expected_return.stock)}）` : "") : null,
    h("h4", null, `③ 与市场看法：${say(c.alignment?.label || "未注明")}`),
    c.alignment?.market_view ? h("p", null, `市场看法：${say(c.alignment.market_view)}`) : null,
    c.alignment?.where_market_is_wrong ? h("p", null, `市场错在哪：${say(c.alignment.where_market_is_wrong)}`) : null,
    (c.invalidators || []).length ? [h("h4", null, "失效条件"), h("ul", null, c.invalidators.map(x => h("li", null, say(x || ""))))] : null,
    ui.note(`估值快照日期 ${c.valuation_as_of || "—"} · 依据：${say(c.policy?.label || "投资建议规则")}`));
}

function memoBlock(ctx, m) {
  const {h, ui, say} = ctx;
  const groups = m.producer_groups || [], checks = m.checks || [], formal = m.formal_evidence || {};
  return h("details", {class: "v2-more"}, h("summary", null, "投资备忘录的核验依据"),
    h("h4", null, `生成记录（${groups.length}）`),
    groups.length ? h("ul", null, groups.map((g, i) => h("li", null, `第 ${i + 1} 组：${say(g.purpose_label || "已记录")} · ${word(ctx, g.status || g.result)}`))) : ui.alert("缺少生成记录。"),
    ...(m.key_questions || []).map((q, i) => h("div", {class: "v2-panel"},
      h("h4", null, `${i + 1}. ${say(q.question_label || q.question || "核验问题")}`),
      h("p", null, q.answer ? say(q.answer) : "（没有答案）"),
      ui.note([q.unknown ? "明确未知" : q.answer ? "已回答" : "未回答", q.falsifier ? `证伪条件：${say(q.falsifier)}` : ""].filter(Boolean).join(" · ")))),
    h("h4", null, "结构检查"),
    checks.length ? h("ul", null, checks.map(x => h("li", null, `${say(x.label || "核验项目")}：${word(ctx, x.status || x.result)}${x.display_reason ? ` · ${say(x.display_reason)}` : ""}`)))
      : ui.alert("缺少结构检查记录。"),
    ui.alert(`独立模型证据：${formal.status === "verified" ? "已核验" : "未完成核验"}${formal.display_reason || formal.reason ? ` · ${say(formal.display_reason || formal.reason)}` : ""}`,
      formal.status === "verified" ? "info" : "warn"),
    (m.gaps || []).length ? [h("h4", null, "资料缺口"), h("ul", null, m.gaps.map(x => h("li", null, typeof x === "string" ? say(x) : "有一项结构化资料缺口")))] : null,
    ui.note(m.human_signature_required ? "需要人工签署" : "无需人工签署"));
}

function zeroBaseBlock(ctx, it) {
  const {h, ui, say} = ctx;
  const z = it.zero_base_review;
  const box = h("details", {class: "v2-more"}, h("summary", null, "完整复盘依据"));
  if (pending(it)) { box.append(ui.note(PENDING_TEXT)); return box; }
  if (drafting(it)) box.append(ui.tag(say(it.final_text.badge || PENDING_BADGE), "warn"));
  box.append(h("h4", null, say(z.title || "从零复盘")));
  for (const s of z.sections || []) box.append(h("h5", null, say(s.heading || "")), ...ui.paragraphs(say(String(s.body || "").replace(/^(\s*-\s*)thesis-version:[0-9a-f]{32}(?=（)/gm, "$1"))));
  if (z.authority_note) box.append(ui.note(say(z.authority_note)));
  return box;
}

/** Everything an approval card carries beyond its summary and simple fields. */
export function decisionBasis(ctx, it) {
  const {ui, say} = ctx;
  const out = [];
  if (it.prior_review_note) out.push(ui.alert(`上一版审阅意见：${say(it.prior_review_note)}`, "info"));
  if (it.change_note) out.push(ui.note(`本版改了什么：${say(it.change_note)}`));
  if (it.conviction_v2) out.push(convictionBlock(ctx, it.conviction_v2));
  if (it.memo_review) out.push(memoBlock(ctx, it.memo_review));
  if (it.zero_base_review) out.push(zeroBaseBlock(ctx, it));
  if (it.reflection) out.push(ctx.h("div", null, ctx.h("h4", null, "相关复盘"), reflectionCard(ctx, it.reflection, {as: "div"})));
  return out;
}
