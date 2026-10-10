/* 研究设置 → 研究范围 → 调整研究方向 (review S01/S02, Z05).
   Natural-language steering through the existing POST /v1/cockpit/steer
   (a paid AI call that returns a draft: questions to add or remove, a
   reworded goal, and what cannot be done here).  Nothing changes until the
   analyst confirms the draft (POST /v1/cockpit/publish) -- or discards it
   (POST /v1/cockpit/decide, kind draft:steer).  The text being written and the
   draft the server already priced survive refreshes and page changes. */

import {said} from "./common.js";
export const TEXT_MAX = 4000;
export const STATUS_LABELS = Object.freeze({open: "待确认", published: "已生效", discarded: "已放弃"});

/** A steer draft has something to publish (pure). */
export function hasChange(d) {
  return Boolean(d && ((d.add_questions || []).length || (d.remove_questions || []).length || d.changes?.objective));
}

export function steerDraftView(ctx, r, {onDone} = {}) {
  const {h, ui, say} = ctx;
  const d = r.draft || {};
  const box = h("div", {class: "v2-panel", id: "steer-draft"});
  const result = h("div", {role: "status", "aria-live": "polite"});
  box.append(h("h3", {tabindex: "-1"}, "研究范围变更草案"));
  if (d.understood_as) box.append(h("p", {class: "v2-lead"}, `我理解为：${say(d.understood_as)}`));
  if (d.summary) box.append(h("p", null, say(d.summary)));
  const item = (word, text, tone) => h("li", null, ui.tag(word, tone), " ", say(text));
  if ((d.add_questions || []).length) box.append(h("h4", null, "新增的研究问题"), h("ul", {class: "v2-diff"}, d.add_questions.map(q => item("新增", q, "ok"))));
  if ((d.remove_questions || []).length) box.append(h("h4", null, "去掉的研究问题"), h("ul", {class: "v2-diff"}, d.remove_questions.map(q => item("删除", q, "danger"))));
  if (d.changes?.objective) box.append(h("h4", null, "改写后的研究目标"), h("p", null, say(d.objective)));
  if ((d.not_possible || []).length) box.append(h("h4", null, "这次无法通过调整方向完成的"), h("ul", null, d.not_possible.map(x => h("li", null, say(x)))),
    ui.note("增减研究公司、资料来源或预算不在这里办理：覆盖名单和预算由管理员调整。"));
  if (!hasChange(d)) {
    box.append(ui.alert("这次没有形成可以发布的调整。请写明要新增的研究问题，或要怎样改研究目标。"));
    const discard = ui.button("放弃这份草案", () => decide(discard), {variant: "quiet"});
    box.append(h("div", {class: "v2-actions"}, discard), result);
  } else {
    const publish = ui.button("确认研究范围变更", async () => {
      publish.disabled = discard.disabled = true;
      result.replaceChildren(ui.receipt("正在发布…"));
      try {
        const out = await ctx.api.post("/v1/cockpit/publish", {draft_id: r.draft_id, draft_hash: r.draft_hash, request_id: ctx.requestId()});
        ctx.drafts.clear("steer:draft"); ctx.drafts.clear("steer:text");
        result.replaceChildren(ui.receipt(`已发布为第 ${out?.version ?? "新"} 版研究目标；系统从下一轮开始按新的研究问题阅读和提炼，已有结论保留。`));
        onDone && onDone("published");
      } catch (e) { result.replaceChildren(ui.receipt(`没有发布：${said(e)}。`, "danger")); publish.disabled = discard.disabled = false; }
    });
    const discard = ui.button("放弃这份草案", () => decide(discard, publish), {variant: "quiet"});
    box.append(ui.note("确认后发布为新一版研究目标；在此之前研究方向不会改变。"), h("div", {class: "v2-actions"}, publish, discard), result);
  }
  async function decide(button, other) {
    button.disabled = true; if (other) other.disabled = true;
    try {
      await ctx.api.post("/v1/cockpit/decide", {kind: "draft:steer", ref: r.draft_id, hash: r.draft_hash, decision: "discard", request_id: ctx.requestId()});
      ctx.drafts.clear("steer:draft");
      result.replaceChildren(ui.receipt("已放弃这份草案，研究方向不变。你写的调整要求还保留在输入框里。"));
      onDone && onDone("discarded");
    } catch (e) { result.replaceChildren(ui.receipt(`没有放弃：${said(e)}。`, "danger")); button.disabled = false; if (other) other.disabled = false; }
  }
  return box;
}

export function steerHistory(ctx, history) {
  const {h, ui, fmt, say} = ctx;
  const items = (history?.drafts || []).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const row = x => h("li", {class: "v2-card"},
    h("div", {class: "v2-card-meta"}, h("span", null, fmt.datetime(x.created_at)),
      ui.tag(STATUS_LABELS[x.status] || "状态暂不可读", x.status === "published" ? "ok" : x.status === "open" ? "warn" : null)),
    h("p", null, `你的要求：${x.input_text || "—"}`),
    x.draft?.understood_as || x.draft?.summary ? h("p", {class: "v2-note"}, say(x.draft.understood_as || x.draft.summary)) : null);
  if (!items.length) return ui.empty("还没有调整过研究方向。");
  return h("div", null, h("ul", {class: "v2-list"}, items.slice(0, 5).map(row)),
    items.length > 5 ? h("details", {class: "v2-more"}, h("summary", null, `更早的 ${items.length - 5} 条`), h("ul", {class: "v2-list"}, items.slice(5).map(row))) : null);
}

/** The steer form with its draft and history (#/settings?focus=steer). */
export function steerPanel(ctx, {goal, modelOk, modelReason, history}) {
  const {h, ui, fmt} = ctx;
  const panel = h("div", {class: "v2-panel", id: "steer"});
  const historyHolder = h("div", {id: "steer-history", class: "v2-block"});
  const drawHistory = value => historyHolder.replaceChildren(h("h4", null, "调整记录"), steerHistory(ctx, value));
  if (!goal) {
    panel.append(h("h3", null, "调整研究方向"), ui.empty("先设定研究目标；之后可以在这里用一句话调整研究方向。"));
    return panel;
  }
  const text = ui.field({id: "steer-text", label: "想怎样调整研究方向", required: true, maxLength: TEXT_MAX,
    help: "例如：本周加大对 IBM 软件和云业务的跟踪；暂时不看大众科技媒体的通稿。系统会整理成具体改动，你确认后才生效。"});
  ctx.drafts.bind(text.input, "steer:text");
  const status = h("div", {role: "status", "aria-live": "polite"});
  const draftHolder = h("div");
  const submit = h("button", {type: "submit", class: "v2-btn"}, "整理成调整方案");
  const replaceNote = h("p", {class: "v2-note", hidden: true}, "上面还有一份没有确认的草案；重新整理会替换它。");
  if (!modelOk) { submit.disabled = true; status.append(ui.alert(ctx.say(modelReason || "AI 服务暂时不可用，暂时不能整理调整方案。"))); }
  const form = h("form", {novalidate: true}, h("h3", null, "调整研究方向"), text.wrap,
    replaceNote, h("div", {class: "v2-actions"}, submit), status);
  const refreshHistory = async () => {
    try { drawHistory(await ctx.api("/v1/cockpit/history?kind=steer", {fresh: true})); } catch (_e) { /* the old list stays */ }
  };
  const showDraft = r => {
    const open = Boolean(r && r.draft);
    draftHolder.replaceChildren(open ? steerDraftView(ctx, r, {onDone: () => { replaceNote.hidden = true; refreshHistory(); }}) : null);
    replaceNote.hidden = !(open && hasChange(r.draft));
    if (open) ctx.drafts.setJson("steer:draft", r);
  };
  // A draft still open on the server comes back as it was, not regenerated.
  const openDraft = (history?.drafts || []).find(x => x.status === "open");
  const held = ctx.drafts.getJson("steer:draft");
  if (openDraft) {
    showDraft({draft: openDraft.draft, draft_id: openDraft.draft_id, draft_hash: openDraft.draft_hash});
    status.replaceChildren(ui.receipt(`你在 ${fmt.datetime(openDraft.created_at)} 整理的方案还没有确认（尚未生效）。`));
  } else if (held && held.draft_id && !(history?.drafts || []).some(x => x.draft_id === held.draft_id && x.status !== "open")) showDraft(held);
  else ctx.drafts.clear("steer:draft");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!text.check()) { text.input.focus(); return; }
    submit.disabled = true;
    status.replaceChildren(ui.receipt("正在整理具体改动，通常需要 20–60 秒…"));
    try {
      const job = await ctx.api.post("/v1/cockpit/steer", {text: text.input.value.trim(), request_id: ctx.requestId()});
      const r = await ctx.api.job(job.job_id);
      showDraft(r);
      status.replaceChildren(ui.receipt("方案已整理，请核对后确认。"));
      draftHolder.querySelector("h3")?.focus();
      refreshHistory();
    } catch (err) { status.replaceChildren(ui.receipt(`没有整理成方案：${said(err)}。你写的内容还在输入框里。`, "danger")); }
    finally { submit.disabled = !modelOk; }
  });
  drawHistory(history);
  panel.append(form, draftHolder, historyHolder);
  return panel;
}
