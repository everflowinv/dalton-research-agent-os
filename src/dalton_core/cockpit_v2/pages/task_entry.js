/* 待我处理: the completing action a card carries from the server (2026-10-09).

   Owner, 10-08: 「我根本不知道该怎么处理，没办法消除掉这个通知」.  Entry modes
   the server sends (needs_human.completion_entry):

   - "action"        one in-page action (重新生成中文定稿) with its payload;
   - "action_group"  several parked UI-text batches as one card: 「全部重新质检」,
                     「全部已知悉」 and each batch on its own when expanded;
   - "admin"         a held research task's recovery doors (恢复专项课题研究 /
                     扩大资料检索范围并重新获取 / 暂缓当前专项课题) plus 管理员设置 → 运行控制;
   - "acknowledge"   已知悉 / 暂不提醒 N 天 / 放弃这份资料.

   Reasons (2026-10-09 coordinator decision, financial review 痛点1, T05):
   non-destructive actions -- 已知悉, 暂不提醒, 重新生成中文定稿 -- are one
   click; a note is optional and collapsed, and when none is written the
   writer still records an auto-generated reason (「分析师在待我处理页…」).
   放弃这份资料 asks for confirmation with its consequence; the recovery
   doors (owner doors) keep a required reason in the confirmation dialog.
   Every result is read from the business status (T07): done, queued or
   rejected, and the card says which (T06). */

import {autoReason, confirmAction, said} from "./common.js";
import {LABELS} from "../copy.js";

const PAGE = `${LABELS.nav.tasks}页`;
const ACK = LABELS.ack, DOOR = LABELS.recovery_door;
const slug = text => String(text || "").replace(/[^a-z0-9]/gi, "").slice(-24) || "x";
let serial = 0;

/** The outcome of a write, from its business status (T07). */
export function outcomeOf(kind, out) {
  const status = String(out?.status ?? out?.result?.status ?? "");
  const state = String(out?.result?.state ?? "");
  if (kind === "ack") return ["recorded", "replayed"].includes(status) ? "done" : status ? "rejected" : "done";
  if (kind === "retry") return ["requested", "already_requested", "replayed", "pending"].includes(status) || !status ? "queued" : "rejected";
  if (kind === "door") {
    if (["queued", "running", "accepted", "requested"].includes(status) || ["queued", "running", "requested"].includes(state)) return "queued";
    if (status && /refus|reject|denied|not_/.test(status)) return "rejected";
    return "done";
  }
  return status && /refus|reject|denied|not_/.test(status) ? "rejected" : "done";
}

const STATE_WORDS = {done: "已处理", queued: "已受理，正在处理", rejected: "没有完成"};

/** Settle a card after its action: the state, the words, and the count. */
function settleWith(ctx, holder, settle) {
  const {ui} = ctx;
  return (state, text) => {
    if (settle) { settle(state, text); return; }
    holder.replaceChildren(ui.receipt(`${STATE_WORDS[state] || ""}：${text}`, state === "rejected" ? "danger" : null));
  };
}

/** The optional note shared by a card's light actions: collapsed. */
function noteField(ctx, key) {
  const {h, ui} = ctx;
  const field = ui.field({id: `note-${slug(key)}-${++serial}`, label: "补充说明（选填，会和操作一起存档）", maxLength: 600, attrs: {rows: "2"}});
  const box = h("details", {class: "v2-more"}, h("summary", null, "补充说明（选填）"), field.wrap);
  return {box, value: () => String(field.input.value || "").trim(), field};
}

function acknowledgeButtons(ctx, it, ack, {settle, note, label = ""}) {
  const {h, ui, say} = ctx;
  const what = label || say(it.title || "这张卡片");
  const post = body => ctx.api.post("/v1/cockpit/needs_human_ack", {
    card_ref: ack.card_ref, kind: ack.kind, condition_hash: ack.condition_hash, request_id: ctx.requestId(), ...body});
  const run = async (button, body, describe) => {
    button.disabled = true;
    try {
      const out = await post(body);
      settle(outcomeOf("ack", out), say(out?.message || describe));
    } catch (e) {
      settle("rejected", `${said(e)}。可以稍后再试。`);
      button.disabled = false;
    }
  };
  const nodes = [];
  for (const c of ack.choices || []) {
    if (c.choice === "noted") {
      const b = ui.button(ACK.noted, () => run(b, {choice: "noted", reason: note.value() || autoReason(PAGE, `标记${ACK.noted}`, what)},
        `${ACK.noted}：情况没有变化前不再提醒。`), {variant: "secondary", "aria-label": `${ACK.noted}：${what}`});
      nodes.push(b);
    } else if (c.choice === "snooze") {
      const days = h("select", {class: "v2-input", id: `days-${slug(it.ref)}-${++serial}`, "aria-label": `静音天数：${what}`},
        (c.days || [7]).map(d => h("option", {value: String(d), selected: d === 7 ? true : null}, `${d} 天`)));
      const b = ui.button(ACK.snooze, () => run(b, {choice: "snooze", snooze_days: Number(days.value),
        reason: note.value() || autoReason(PAGE, `选择${ACK.snooze} ${days.value} 天`, what)}, `已静音该提示 ${days.value} 天。`),
        {variant: "quiet", "aria-label": `${ACK.snooze}：${what}`});
      nodes.push(h("span", {class: "v2-inline"}, b, " ", days));
    } else if (c.choice === "approve_spend") {
      // Batch E: approve the extra material-acquisition spend the card names.
      const b = ui.button(ACK.approve_spend, async () => {
        const answer = await confirmAction(ctx, {title: `${ACK.approve_spend}？`, confirmLabel: "确认批准",
          body: [c.confirm || "批准后系统继续检索剩下的来源。"], reason: {required: false}});
        if (!answer.confirmed) return;
        run(b, {choice: "approve_spend", reason: answer.reason || note.value() || autoReason(PAGE, ACK.approve_spend, what)},
          "已批准追加材料获取额度，下一轮继续检索。");
      }, {"aria-label": `${ACK.approve_spend}：${what}`});
      nodes.push(b);
    } else if (c.choice === "abandon_document") {
      const b = ui.button(ACK.abandon_document, async () => {
        const answer = await confirmAction(ctx, {title: "忽略该缺失资料？", tone: "danger", confirmLabel: "确认忽略",
          body: ["系统不再尝试获取这份资料，采用现有数据继续推导；这张卡片也不再出现。", "已有的研究不受影响；以后需要时可以请管理员重新取回。"],
          reason: {required: false}});
        if (!answer.confirmed) return;
        run(b, {choice: "abandon_document", reason: answer.reason || note.value() || autoReason(PAGE, "忽略该缺失资料", what)},
          "已忽略该缺失资料，采用现有数据推导。");
      }, {variant: "danger", "aria-label": `${ACK.abandon_document}：${what}`});
      nodes.push(b);
    }
  }
  return nodes;
}

/** 重新生成中文定稿 for one payload: one click, no reason asked. */
async function retry(ctx, entry, payload, what) {
  return ctx.api.post(`/v1/cockpit/${encodeURIComponent(entry.action)}`,
    {...(payload || {}), reason: autoReason(PAGE, "要求重新生成中文定稿", what), request_id: ctx.requestId()});
}

function actionEntry(ctx, it, entry, settle) {
  const {h, ui, say} = ctx;
  const what = say(it.title || "");
  const note = noteField(ctx, it.ref);
  const go = ui.button(say(entry.label), async () => {
    go.disabled = true;
    try {
      const out = await retry(ctx, entry, entry.payload, what);
      settle(outcomeOf("retry", out), say(out?.message || "已受理，约 5 分钟内开始重新生成。"));
    } catch (e) { settle("rejected", `${said(e)}。可以稍后再试。`); go.disabled = false; }
  }, {"aria-label": `${say(entry.label)}：${what}`});
  const acks = entry.acknowledge ? acknowledgeButtons(ctx, it, entry.acknowledge, {settle, note}) : [];
  return h("div", null, h("div", {class: "v2-actions"}, go, ...acks), entry.acknowledge ? note.box : null,
           entry.note ? ui.note(say(entry.note)) : null);
}

function groupEntry(ctx, it, entry, settle) {
  const {h, ui, fmt, say} = ctx;
  const members = entry.members || [];
  const note = noteField(ctx, it.ref);
  const allRetry = ui.button(say(entry.label || "全部重新生成中文定稿"), async () => {
    allRetry.disabled = true; allAck.disabled = true;
    let ok = 0, failed = 0;
    for (const m of members) {
      if (!m.retry?.payload) continue;
      try { const out = await retry(ctx, {action: m.retry.action || entry.action}, m.retry.payload, say(m.title || ""));
            if (outcomeOf("retry", out) === "rejected") failed++; else ok++; }
      catch (_e) { failed++; }
    }
    if (failed && !ok) { settle("rejected", `${failed} 批都没有受理，请稍后再试。`); allRetry.disabled = false; allAck.disabled = false; }
    else settle("queued", `已受理 ${ok} 批，约 5 分钟内开始重新生成${failed ? `；另有 ${failed} 批没有受理，可展开逐批重试` : ""}。`);
  }, {"aria-label": `全部重新生成中文定稿：${members.length} 批中文译稿`});
  const allAck = ui.button(ACK.noted_all, async () => {
    allRetry.disabled = true; allAck.disabled = true;
    let ok = 0, failed = 0;
    for (const m of members) {
      const ack = m.acknowledge;
      if (!ack) continue;
      try {
        const out = await ctx.api.post("/v1/cockpit/needs_human_ack", {card_ref: ack.card_ref, kind: ack.kind, condition_hash: ack.condition_hash,
          choice: "noted", reason: note.value() || autoReason(PAGE, `批量标记${ACK.noted}`, say(m.title || "")), request_id: ctx.requestId()});
        if (outcomeOf("ack", out) === "rejected") failed++; else ok++;
      } catch (_e) { failed++; }
    }
    if (failed && !ok) { settle("rejected", "没有记下，请稍后再试。"); allRetry.disabled = false; allAck.disabled = false; }
    else settle("done", `${ACK.noted} ${ok} 批：情况没有变化前不再提醒${failed ? `；另有 ${failed} 批没有记下` : ""}。`);
  }, {variant: "secondary", "aria-label": `${ACK.noted_all}：${members.length} 批中文译稿`});
  const list = h("ul", {class: "v2-list"}, members.map((m, i) => {
    const holder = h("div", {role: "status"});
    const own = (state, text) => holder.replaceChildren(ui.receipt(`${STATE_WORDS[state]}：${text}`, state === "rejected" ? "danger" : null));
    const label = `第 ${i + 1} 批`;
    const one = m.retry?.payload ? ui.button("重新生成中文定稿", async () => {
      one.disabled = true;
      try { const out = await retry(ctx, {action: m.retry.action || entry.action}, m.retry.payload, label); own(outcomeOf("retry", out), say(out?.message || "已受理。")); }
      catch (e) { own("rejected", said(e)); one.disabled = false; }
    }, {variant: "secondary", "aria-label": `重新生成中文定稿：${label}中文译稿`}) : null;
    const acks = m.acknowledge ? acknowledgeButtons(ctx, {...it, ref: m.ref, title: label}, {...m.acknowledge, choices: (m.acknowledge.choices || []).filter(c => c.choice === "noted")},
      {settle: own, note, label}) : [];
    return h("li", {class: "v2-card"}, h("div", {class: "v2-card-meta"}, h("span", null, label),
      m.attempts ? h("span", null, `已连续 ${m.attempts} 次没有通过中文定稿校对`) : null, m.at ? h("span", null, fmt.datetime(m.at)) : null),
      h("div", {class: "v2-actions"}, one, ...acks), holder);
  }));
  return h("div", null, h("div", {class: "v2-actions"}, allRetry, allAck), note.box,
    h("details", {class: "v2-more"}, h("summary", null, `逐批查看（${members.length} 批）`), list),
    entry.note ? ui.note(say(entry.note)) : null);
}

function doorsEntry(ctx, it, entry, settle) {
  const {h, ui, say} = ctx;
  const what = say(it.title || "这项研究");
  const doors = (entry.actions || []).map(door => {
    // The registry's door words first (am 7.3): never 再买一次 / 放行 / 付费.
    const label = DOOR[door.door] || say(door.label);
    const b = ui.button(label, async () => {
      const cap = door.needs_cost_cap
        ? h("input", {type: "number", min: "0.01", max: "20", step: "0.01", id: `cap-${slug(it.ref)}-${++serial}`, class: "v2-input",
                      inputmode: "decimal", "aria-describedby": `cap-help-${serial}`})
        : null;
      const capBox = cap ? h("div", {class: "v2-field"}, h("label", {for: cap.id}, DOOR.cap_label),
        h("div", {class: "v2-help", id: `cap-help-${serial}`}, DOOR.cap_help), cap) : null;
      const answer = await confirmAction(ctx, {
        title: `${label}？`, tone: door.danger ? "danger" : "primary", confirmLabel: `确认${label.replace(/（.*$/, "")}`,
        body: [say(door.confirm || "").replace(/确认？$/, ""), `对象：${what}`].filter(Boolean),
        extra: capBox,
        reason: {required: true, label: "理由（必填，会和操作一起存档）"},
        validate: () => (cap && !(Number(cap.value) > 0 && Number(cap.value) <= 20) ? "请填写 0.01 到 20 美元之间的检索预算上限。" : ""),
      });
      if (!answer.confirmed) return;
      b.disabled = true;
      try {
        const out = await ctx.api.post("/v1/cockpit/owner_door", {door: door.door, admission_ref: door.admission_ref,
          ...(cap ? {max_cost_usd: Number(cap.value)} : {}), reason: answer.reason, request_id: ctx.requestId()});
        settle(outcomeOf("door", out), say(out?.message || "已提交。"));
      } catch (e) { settle("rejected", `${said(e)}。`); b.disabled = false; }
    }, {variant: door.danger ? "danger" : door.needs_cost_cap ? "secondary" : "primary", "aria-label": `${label}：${what}`});
    return b;
  });
  const admin = entry.href ? h("a", {class: "v2-btn v2-btn-quiet", href: entry.href}, say(entry.label)) : null;
  // 2026-10-09: not buttons -- the hand-off to the technical administrator,
  // and a task the system retries by itself, are said in words.
  const handoff = entry.handoff ? ui.note(`${DOOR.technical_help}：${say(entry.handoff.note || "")}`) : null;
  const automatic = entry.automatic_retry ? ui.note("系统会自动再试，不需要你操作；上面的按钮只在你不想再等时使用。") : null;
  return h("div", null, h("div", {class: "v2-actions"}, ...doors, admin), automatic, handoff,
    entry.note ? ui.note(say(entry.note)) : null);
}

/** The node for a card's server entry, or null when the page's own entry
    applies.  `settle(state, text)` (from the page) marks the card done /
    queued / rejected and updates the counts. */
export function serverEntry(ctx, it, {settle = null} = {}) {
  const {h, ui, say} = ctx;
  const entry = it.entry || {};
  const result = h("div", {role: "status"});
  const done = settleWith(ctx, result, settle);
  let node = null;
  if (entry.mode === "action" && entry.action) node = actionEntry(ctx, it, entry, done);
  else if (entry.mode === "action_group" && (entry.members || []).length) node = groupEntry(ctx, it, entry, done);
  else if (entry.mode === "admin" && (entry.actions || []).length) node = doorsEntry(ctx, it, entry, done);
  else if (entry.mode === "admin" && entry.href) {
    node = h("div", null, h("a", {class: "v2-btn v2-btn-secondary", href: entry.href}, say(entry.label)), entry.note ? ui.note(say(entry.note)) : null);
  } else if (entry.mode === "acknowledge" && (entry.choices || []).length) {
    const second = entry.secondary || {};
    const link = second.mode === "link" && /^https:\/\//.test(second.href || "")
      ? h("a", {class: "v2-btn v2-btn-quiet", href: second.href, target: "_blank", rel: "noopener noreferrer"}, say(second.label)) : null;
    const note = noteField(ctx, it.ref);
    node = h("div", null, h("div", {class: "v2-actions"}, ...acknowledgeButtons(ctx, it, entry, {settle: done, note}), link), note.box,
             entry.secondary_note ? ui.note(say(entry.secondary_note)) : null, entry.note ? ui.note(say(entry.note)) : null);
  }
  if (!node) return null;
  if (!settle) node.append(result);
  return node;
}
