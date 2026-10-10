/* 待我处理: one list, sorted by impact and urgency, grouped by task kind (D01).
   Every card names the research object, the suggested decision, what happens
   if nothing is done, and an entry that completes the task -- or hands it to
   the administrator when only an administrator can (D02-D04). */

import {V, OVERVIEW, NEEDS_HUMAN, APPROVALS, autoReason, companies, companyKey, companyName, confirmAction, load, said} from "./common.js";
import {GROUPS, KINDS, foldTasks, kindOf, sortTasks, urgencyLabel} from "./task_kinds.js";
import {decisionBasis} from "./decision_basis.js";
import {serverEntry} from "./task_entry.js";
import {LABELS, UI} from "../copy.js";

const NAV = LABELS.nav, BTN = LABELS.decision_button, DOOR = LABELS.recovery_door;

export {GROUPS, KINDS, kindOf, sortTasks};

function objectOf(it, overview) {
  const d = it.detail || {};
  const c = d.company_ref ? companies(overview).find(x => x.company_ref === d.company_ref) : null;
  if (c) return {label: companyName(c), key: companyKey(c)};
  if (d.company) return {label: d.company, key: null};
  // Some items name the company only in their title: "ACN · Accenture 的…".
  const head = /^([A-Z][A-Z0-9.]{0,9}) · /.exec(String(it.title || ""));
  const byTicker = head ? companies(overview).find(x => x.ticker === head[1]) : null;
  if (byTicker) return {label: companyName(byTicker), key: companyKey(byTicker)};
  return {label: "本研究项目", key: null};
}

/** The company key a decision is about ("ACN · Accenture" -> "ACN"). */
function approvalObject(it) {
  const m = /^([A-Z][A-Z0-9.]{0,9})(?: · |$)/.exec(String(it.who || ""));
  return m ? m[1] : null;
}

/** The business summary an administrator needs (T08, financial 4.13): what,
    for whom, what it blocks -- no hashes.  The reference stays under the
    technical details, with 「复制给管理员的摘要」. */
export function adminSummary(ctx, it, object) {
  const {say} = ctx;
  const k = kindOf(it);
  const impact = (k.impact && k.impact(it.detail || {})) || say(it.consequence || "") || "暂无影响说明";
  return [`事项：${say(it.title)}`, `研究对象：${object?.label || "本研究项目"}`, `影响：${impact}`,
          `${BTN.suggest}：${k.suggest(it.detail || {})}`].join("\n");
}

function adminEntry(ctx, it, object) {
  const {h, ui} = ctx;
  const href = ctx.adminHref({task: it.ref, kind: it.kind});
  const summary = adminSummary(ctx, it, object);
  const status = h("span", {class: "v2-note", role: "status"});
  const copyButton = ui.button(BTN.handoff_copy, async () => {
    try { await globalThis.navigator.clipboard.writeText(`${summary}\n编号：${it.ref}`); status.textContent = "已复制，可以发给管理员。"; }
    catch (_e) { status.textContent = "无法自动复制，请选中上面的说明手动复制。"; }
  }, {variant: "quiet", "aria-label": `${BTN.handoff_copy}：${ctx.say(it.title)}`});
  return h("div", {class: "v2-handoff"},
    href ? h("a", {class: "v2-btn v2-btn-secondary", href, "aria-label": `${BTN.handoff}：${ctx.say(it.title)}`}, BTN.handoff) : null,
    h("details", {class: "v2-more"}, h("summary", null, "技术交办单内容"),
      h("p", {class: "v2-pre-line"}, summary),
      ui.note(href ? "管理员在「管理员设置」里处理这类事项；这里的说明可以直接转给他。" : "管理员界面尚未开放；把这段说明转给管理员即可。"),
      ui.technical(h("p", null, `编号：${it.ref}`), copyButton, status)));
}

/* 暂缓该专项课题 (stop-line): destructive, so a confirmation with its
   consequence and a required reason (coordinator decision 2026-10-09). */
function recoveryEntry(ctx, it, object, settle) {
  const {h, ui, say} = ctx;
  const d = it.detail || {};
  const what = say(it.title || "这项专项研究");
  const stop = ui.button(DOOR.withdraw, async () => {
    const answer = await confirmAction(ctx, {title: "暂缓当前专项课题？", tone: "danger", confirmLabel: "确认暂缓",
      body: ["暂缓后这项专项课题不再继续；现有研究底稿和记录保留，需要时可以重新开始。", `对象：${what}`],
      reason: {required: true, label: "理由（必填，会和操作一起存档）"}});
    if (!answer.confirmed) return;
    stop.disabled = true;
    try {
      const out = await ctx.api.post("/v1/cockpit/stop_line", {line_ref: d.admission_ref, reason: answer.reason, request_id: ctx.requestId()});
      const ok = ["stopped", "already_stopped"].includes(out.status);
      if (ok) settle("done", `${DOOR.withdraw_done}，记录已存档。`);
      else { settle("rejected", `暂缓操作未执行：${((out.receipt?.refs?.refused_because) || []).map(say).join("；") || "当前状态不允许暂缓"}`); stop.disabled = false; }
    } catch (e) { settle("rejected", `${said(e)}。`); stop.disabled = false; }
  }, {variant: "danger", "aria-label": `${DOOR.withdraw}：${what}`});
  return h("div", null, h("div", {class: "v2-actions"}, stop), adminEntry(ctx, it, object));
}

function entryFor(ctx, it, object, hasApprovals, settle) {
  const {h, href} = ctx;
  if (it.actionable === false && it.responsibility === "background") {
    return object.key ? h("a", {class: "v2-btn v2-btn-quiet", href: href({page: "company", company: object.key, tab: "view"})}, "查看当前研究与资料缺项") : null;
  }
  const own = serverEntry(ctx, it, {settle});  // 2026-10-09: the card's own completing action
  if (own) return own;
  const k = kindOf(it);
  const companyLink = object.key ? h("a", {class: "v2-btn v2-btn-quiet", href: href({page: "company", company: object.key, tab: k.group === "material" ? "risks" : "view"}),
                                           "aria-label": `查看 ${object.label} 的公司研究`}, "查看公司研究") : null;
  if (k.entry === "goal") return h("a", {class: "v2-btn", href: href({page: "settings", filters: {focus: "goal"}})}, "设定研究目标");
  if (k.entry === "approval") return hasApprovals
    ? ctx.ui.button(BTN.approve_entry, () => {
        // T09: land on this object's decision, not the top of the list.
        const doc = globalThis.document;
        const target = [...(doc.querySelectorAll?.("[data-approval-ref]") || [])].find(x => object.key && x.getAttribute("data-approval-object") === object.key)
          || doc.getElementById("approvals-title");
        target?.scrollIntoView?.({block: "start"}); target?.focus?.({preventScroll: true});
      }, {"aria-label": `${BTN.approve_entry}：${ctx.say(it.title)}`})
    : h("span", null, companyLink, ctx.ui.note(`这项决定的确认入口还在准备，点顶部的「${UI.refresh}」稍后再看。`));
  if (k.entry === "recovery") return it.detail?.admission_ref ? recoveryEntry(ctx, it, object, settle) : null;
  if (k.entry === "material") return h("div", {class: "v2-actions"}, companyLink, adminEntry(ctx, it, object));
  if (k.entry === "company") return companyLink;
  return adminEntry(ctx, it, object);
}

const SETTLED = {done: "已处理", queued: "已受理", rejected: "没有完成"};

/** Mark a card done / queued / rejected (T06) and let the page recount. */
function settler(ctx, card, holder, onSettled) {
  const {ui} = ctx;
  let counted = false;
  return (state, text) => {
    card.setAttribute("data-state", state);
    holder.replaceChildren(ui.receipt(`${SETTLED[state] || ""}：${text}`, state === "rejected" ? "danger" : null));
    if (state !== "rejected") {
      for (const b of card.querySelectorAll(".v2-actions button")) b.disabled = true;
      if (!counted) { counted = true; onSettled && onSettled(); }
    }
    ctx.announce && ctx.announce(`${SETTLED[state] || ""}：${text}`);
  };
}

function taskCard(ctx, it, overview, hasApprovals, onSettled) {
  const {h, ui, fmt, say} = ctx;
  const k = kindOf(it);
  const object = objectOf(it, overview);
  const d = it.detail || {};
  const systemOwned = it.actionable === false && Boolean(it.responsibility);
  const impact = systemOwned ? say(it.consequence || "") : (k.impact && k.impact(d)) || say(it.consequence || "");
  const suggestion = systemOwned ? say(it.action || it.why_blocked || "系统继续跟进。") : k.suggest(d);
  const card = h("li", {class: "v2-card", "data-kind": it.kind, "data-ref": it.ref},
    h("h3", null, say(it.title)),
    h("div", {class: "v2-card-meta"}, h("span", null, `研究对象：${object.label}`),
      ui.tag(systemOwned ? ({background: "后台跟进", administrator: "管理员维护", optional_feedback: "可选反馈"}[it.responsibility] || "系统跟进") : urgencyLabel(it), systemOwned ? null : it.urgency <= 2 ? "danger" : it.urgency <= 4 ? "warn" : null),
      it.at ? h("span", null, fmt.datetime(it.at)) : null),
    ui.kv([[systemOwned ? "后续进展" : BTN.suggest, suggestion], [systemOwned ? "对研究的影响" : "如果不处理", impact]]));
  if (k.group !== "service" && it.why_blocked) card.append(h("details", {class: "v2-more"}, h("summary", {"aria-label": `原因：${say(it.title)}`}, "原因"), h("p", null, say(it.why_blocked))));
  const holder = h("div", {role: "status", "aria-live": "polite"});
  const entry = entryFor(ctx, it, object, hasApprovals, settler(ctx, card, holder, onSettled));
  if (entry) card.append(h("div", {class: "v2-actions"}, entry));
  card.append(holder);
  return card;
}

/** The consequence of a decision, in words, for its confirmation (MUSE P0 #2). */
function consequenceOf(ctx, it, a) {
  const {say} = ctx;
  if (a.hint) return say(a.hint);
  if (["reject", "discard", "decline"].includes(a.decision)) return "退回说明：本轮草案将被归档，系统维持现有研判基准不变；补充新证据后可随时发起重新审议。";
  if (it.next_step) return `确认生效：${say(it.next_step)}`;
  return "确认生效：系统按你的决定继续，并生成新的研究版本。";
}

function approvalCard(ctx, it, onSettled) {
  const {h, ui, fmt, say} = ctx;
  const card = h("li", {class: "v2-card", "data-kind": "approval", "data-approval-ref": it.ref, tabindex: "-1",
                        "data-approval-object": approvalObject(it) || null},
    h("h3", null, say(it.title || "研究决定")),
    h("div", {class: "v2-card-meta"}, h("span", null, `研究对象：${say(it.who || "本研究项目")}`), h("span", null, fmt.datetime(it.latest_at || it.at))));
  if (it.summary) card.append(h("p", null, say(it.summary)));
  if (it.display_reason) card.append(h("p", {class: "v2-note"}, say(it.display_reason)));
  if (it.historical_paragraphs?.length) {
    const paragraphs = it.historical_paragraphs.map(p => h("li", null,
      h("strong", null, say(p.label)), "：", say(p.text)));
    card.append(h("details", null, h("summary", null, "历史定量段落与披露期间（原文保留）"),
      h("ul", {class: "v2-list"}, paragraphs)));
  }
  if (it.next_step && say(it.next_step) !== say(it.summary || "")) card.append(ui.kv([[it.decision_required === false ? "处理进展" : "确认生效", say(it.next_step)]]));
  const pairs = Object.entries(it.details || {}).filter(([key, v]) => (it.detail_labels || {})[key] && v != null && v !== "" && typeof v !== "object")
    .map(([key, v]) => [say(it.detail_labels[key]), /(_at|date|time)$/i.test(key) ? fmt.datetime(v) : say(String(v))]);
  if (pairs.length) card.append(ui.kv(pairs));
  // What the decision rests on (the investment call, the memo check, the review).
  card.append(...decisionBasis(ctx, it));
  let rationale = null;
  if (it.needs_rationale) {
    rationale = ui.field({id: `why-${String(it.ref || "").replace(/[^a-z0-9]/gi, "").slice(-24)}`, label: "补充说明（会随决定保存）", help: say(it.rationale_hint || "选填"), maxLength: 2000});
    ctx.drafts.bind(rationale.input, `approval:${it.kind}:${it.ref}`);
    card.append(rationale.wrap);
  }
  const result = h("div", {role: "status"});
  let due = null, closeEvidence = null;
  if (it.followup_fields) {
    due = ui.field({id: `due-${String(it.ref).slice(-24)}`, label: "处理期限", type: "date", value: it.due_on || ""});
    due.input.value = it.due_on || "";
    closeEvidence = h("select", {id: `evidence-${String(it.ref).slice(-24)}`, "aria-label": "关闭研究跟进事项所依据的已持有结果"},
      h("option", {value: ""}, "选择已持有的结果证据"),
      (it.close_evidence_choices || []).map((e, index) => h("option", {value: String(index)}, say(e.label))));
    card.append(due.wrap, h("label", null, "关闭结果证据", closeEvidence));
  }
  const actions = (it.actions || []);
  const primary = actions.findIndex(a => !["reject", "discard"].includes(a.decision) && !a.disabled);
  const buttons = actions.map((a, i) => {
    const variant = ["reject", "discard"].includes(a.decision) ? "danger" : i === primary ? "primary" : "secondary";
    const b = ui.button(say(a.label), async () => {
      // A decision is confirmed first, with what it changes (MUSE P0 #2, T10).
      const danger = ["reject", "discard", "decline"].includes(a.decision);
      const note = rationale ? rationale.input.value.trim() : "";
      const answer = await confirmAction(ctx, {title: `确认「${say(a.label)}」？`, tone: danger ? "danger" : "primary",
        confirmLabel: `确认${say(a.label)}`,
        body: [`决定对象：${say(it.title || "研究决定")}`, consequenceOf(ctx, it, a), note ? `补充说明：${note}` : null]});
      if (!answer.confirmed) return;
      buttons.forEach(x => { x.disabled = true; });
      result.replaceChildren(ui.receipt("正在保存你的决定…"));
      const body = {kind: it.kind, ref: it.ref, hash: it.hash, decision: a.decision, rationale: rationale ? rationale.input.value.trim() : "",
                    request_id: ctx.requestId()};
      if (it.evaluation_id) body.evaluation_id = it.evaluation_id;
      if (it.followup_fields) {
        body.due_on = due.input.value;
        const selected = closeEvidence.value === "" ? null : (it.close_evidence_choices || [])[Number(closeEvidence.value)];
        body.closure_evidence = selected ? [{kind: selected.kind, ref: selected.ref, hash: selected.hash}] : [];
      }
      try {
        const saved = await ctx.api.post("/v1/cockpit/decide", body);
        ctx.drafts.clear(`approval:${it.kind}:${it.ref}`);
        card.setAttribute("data-state", "done");
        const next = it.followup_fields ? (a.decision === "assign" ? "负责人和处理期限已保存。" : "决定和依据已保存；刷新后此事项会移出待办。") : "决定已保存，最新研究状态可在公司页查看。";
        result.replaceChildren(ui.receipt(`已处理：${say(a.label)}。${saved?.follow_up ? say(saved.follow_up) : next}`));
        onSettled && onSettled();
      } catch (e) {
        let confirmed = false;
        try {
          const q = new URLSearchParams({kind: it.kind, ref: it.ref, hash: it.hash, decision: a.decision, request_id: body.request_id});
          confirmed = (await ctx.api(`/v1/cockpit/decision-status?${q}`, {fresh: true})).decided === true;
        } catch (_e) { /* fall through */ }
        if (confirmed) { card.setAttribute("data-state", "done"); result.replaceChildren(ui.receipt(`已处理：${say(a.label)}。已核对决定保存成功。`)); onSettled && onSettled(); }
        else {
          result.replaceChildren(ui.receipt(e.display_message, "danger"));
          buttons.forEach((x, j) => { x.disabled = Boolean(actions[j].disabled); });
        }
      }
    }, {variant, disabled: a.disabled || null, title: a.hint ? say(a.hint) : null, "aria-label": `${say(a.label)}：${say(it.title || "研究决定")}`});
    return b;
  });
  card.append(h("div", {class: "v2-actions"}, buttons), result);
  return card;
}

async function render(root, ctx) {
  const {h, ui, fmt, say} = ctx;
  root.append(h("div", {class: "v2-page-head"}, h("h1", null, LABELS.page_title.tasks)));
  const [nh, ap, ov] = await Promise.all([load(ctx, NEEDS_HUMAN, {fresh: true}), load(ctx, APPROVALS, {fresh: true}), load(ctx, OVERVIEW)]);
  if (!ctx.alive()) return;
  let needs = nh.value;
  if (nh.error) {
    const last = ctx.drafts.getJson("tasks:last");
    root.append(ui.state({tone: "danger", status: last ? `待办暂时无法更新，以下为 ${fmt.datetime(last.as_of)} 的结果。` : "待办暂时无法读取，这不代表没有待办。",
      impact: "新出现的待办可能还没显示。", owner: "系统会自动恢复；持续出现请联系管理员。", next: "点「重试」重新读取。",
      actions: [{label: "重试", onClick: () => ctx.navigate({}, {replace: true})}]}));
    needs = last;
  } else ctx.drafts.setJson("tasks:last", {as_of: needs.as_of, items: needs.items || []});
  if (ap.error) root.append(ui.state({tone: "warn", status: "需要你确认的研究决定暂时读不到。", impact: "下面可能缺少研究决定。",
    owner: "系统会自动恢复。", next: UI.retryLater}));
  const overview = ov.value;
  const filterCompany = ctx.state.filters.company || "";
  // T09: the company filter applies to the decisions too.
  const approvals = (ap.value?.items || []).filter(it => !filterCompany || approvalObject(it) === filterCompany || !approvalObject(it));
  let items = sortTasks((needs?.items || []));
  // An aggregate recovery card repeats the per-task cards below it (D05).
  items = foldTasks(items);
  if (filterCompany) items = items.filter(x => objectOf(x, overview).key === filterCompany);

  const systemItems = items.filter(it => it.actionable === false && it.responsibility);
  items = items.filter(it => !systemItems.includes(it));
  const decisions = approvals.filter(it => it.decision_required !== false);
  const systemWork = approvals.filter(it => it.decision_required === false);
  let actionable = items.filter(x => x.actionable !== false && kindOf(x).group !== "processing").length + decisions.length;
  // T12: a part that could not be read is never counted as zero.
  const partial = Boolean(nh.error || ap.error);
  const sub = h("span", {class: "v2-sub", role: "status", "aria-live": "polite"});
  const writeCount = () => sub.replaceChildren(partial && !actionable ? "部分待办暂时读不到"
    : actionable ? `${actionable} 项需要处理${partial ? "（部分待办暂时读不到）" : ""}` : LABELS.shell.blocked_none,
    needs?.as_of ? ` · 更新于 ${fmt.datetime(needs.as_of)}` : "");
  writeCount();
  root.firstChild.append(sub);
  // T06: an action that completes a card brings the counts down at once.
  const onSettled = () => { actionable = Math.max(0, actionable - 1); writeCount(); if (!filterCompany) ctx.setBadge("tasks", actionable); };
  if (filterCompany) root.append(ui.alert(`只显示 ${filterCompany} 的事项。`, "info", " ", h("a", {href: ctx.href({filters: {}})}, "显示全部")));

  const grouping = ctx.state.filters.group === "urgency" ? "urgency" : "kind";
  const toggle = h("div", {class: "v2-actions", role: "group", "aria-label": "排列方式"},
    ...[["kind", "按任务种类分组"], ["urgency", "按紧急程度排列"]].map(([value, label]) =>
      ui.button(label, () => ctx.navigate({filters: {...ctx.state.filters, group: value}}, {replace: true}),
                {variant: grouping === value ? "primary" : "quiet", "aria-pressed": grouping === value ? "true" : "false"})));
  root.append(toggle);

  if (decisions.length) {
    root.append(ui.section("approvals", "需要你确认的研究决定", h("ul", {class: "v2-list"}, decisions.map(it => approvalCard(ctx, it, onSettled)))));
  }
  if (grouping === "urgency") {
    root.append(ui.section("all", "全部事项", items.length ? h("ul", {class: "v2-list"}, items.map(it => taskCard(ctx, it, overview, approvals.length > 0, onSettled))) : ui.empty("没有事项。")));
  } else {
    for (const g of GROUPS) {
      const own = items.filter(x => kindOf(x).group === g.id);
      if (!own.length) continue;
      root.append(ui.section(`group-${g.id}`, `${g.title}（${own.length}）`, h("ul", {class: "v2-list"}, own.map(it => taskCard(ctx, it, overview, approvals.length > 0, onSettled)))));
    }
  }
  for (const [role, title] of [["background", "后台推进"], ["administrator", "管理员诊断"], ["optional_feedback", "可选反馈"]]) {
    const rows = systemWork.filter(it => it.responsibility === role);
    const tasks = systemItems.filter(it => it.responsibility === role);
    if (rows.length || tasks.length) root.append(ui.section(`followups-${role}`, title, h("ul", {class: "v2-list"}, rows.map(it => approvalCard(ctx, it, null)), tasks.map(it => taskCard(ctx, it, overview, decisions.length > 0, null)))));
  }
  if (!items.length && !systemItems.length && !approvals.length && !nh.error) root.append(ui.state({status: `${LABELS.shell.blocked_none}。`,
    impact: "研究在按计划进行。", owner: "有新的决定或问题时会出现在这里，左侧导航会显示数字。",
    next: "可以去研究总览看最新判断。", actions: [{label: "打开研究总览", href: ctx.href({page: "overview"}), variant: "secondary"}]}));
  if (!filterCompany && !partial) ctx.setBadge("tasks", actionable);
  void say;
}

V().registerPage("tasks", {title: NAV.tasks, nav: {label: NAV.tasks, order: 40}, render});
