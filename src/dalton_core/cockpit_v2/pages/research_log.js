/* 研究总览 → 研究记录: two drawers that used to be panels of the old page.

   本周复盘 (#/overview?panel=reflection): GET /v1/cockpit/reflection -- where
   the week's research effort went, how earlier judgements held up, and the
   questions worth researching next (each can seed 调整研究方向).  Written by
   the server's templates from counted things, not by a model.

   研究轨迹 (#/overview?panel=trajectory&kind=call): GET /v1/cockpit/trajectory
   -- each step the system took (AI calls, research tasks, budget refusals),
   newest first, a page at a time.  Filtering is by kind; each row opens with
   a native disclosure.  Run failures and the failure ledger are the
   administrator's (/admin#health). */

export const KINDS = Object.freeze([
  {key: "", label: "全部"}, {key: "call", label: "AI 调用"}, {key: "work", label: "研究任务"}, {key: "budget", label: "预算"},
]);
const STATUS = {ok: "完成", skip: "跳过", run: "进行中", failed: "失败", warn: "超出预算未执行"};
const KIND_LABEL = {call: "AI 调用", work: "研究任务", budget: "预算"};
const TONE = {ok: "ok", failed: "danger", warn: "warn"};
const DETAIL = {claimed: "已开始处理", enqueued: "已排队", worker_completion: "处理结束"};

/** Rows of one kind ("" = all), newest first (pure). */
export function filterEvents(events, kind) {
  return (events || []).filter(e => !kind || e.kind === kind)
    .slice().sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));
}

/** Append a candidate question to the steer text without losing what is there (pure). */
export function withQuestion(existing, question) {
  const now = String(existing || "").trim(), q = String(question || "").trim();
  if (!q) return now;
  if (now.includes(q)) return now;
  return now ? `${now}\n${q}` : q;
}

export function reflectionView(ctx, r, {acceptance} = {}) {
  const {h, ui, fmt, say, href} = ctx;
  const box = h("div", {class: "v2-drawer"});
  if (acceptance && acceptance.available !== false && acceptance.note)
    box.append(ui.alert(`上周已完成研究的验收（${say(acceptance.week || "")}）：${say(acceptance.note)}`, "info"));
  if (!r || r.available === false) { box.append(ui.empty(say(r?.reason || "还没有每周复盘。"))); return box; }
  const o = r.owner || r;
  box.append(h("p", {class: "v2-note"}, [say(r.iso_week || ""), r.written_at ? `生成于 ${fmt.datetime(r.written_at)}` : null,
    r.version ? `第 ${r.version} 版` : null, r.historical ? "最近一份（不是本周新生成的）" : null,
    r.content_as_of ? `内容截至 ${fmt.datetime(r.content_as_of)}` : null].filter(Boolean).join(" · ")));
  box.append(h("h3", null, say(o.title || r.title || "这一周的研究")), ...ui.paragraphs(say(o.prose || r.prose || "")));
  const table = o.table || [];
  if (table.length) box.append(h("h3", null, "各项研究工作的花费"), ui.table({caption: "本周 AI 费用按工作类型分",
    columns: [
      {key: "pool", label: "工作", render: x => say(x.pool_label || x.pool || "其他研究工作")},
      {key: "cost", label: "费用", num: true, render: x => fmt.usd(x.cost_usd)},
      {key: "calls", label: "调用", num: true, render: x => `${fmt.number(x.calls)} 次`},
      {key: "share", label: "占本周", num: true, render: x => fmt.fraction(x.share_of_spend)},
    ], rows: table}));
  const jo = r.judgement_outcomes;
  if (jo) {
    box.append(h("h3", null, "历史判断跟踪"));
    if (jo.available === false) box.append(ui.empty(say(jo.reason || "还没有可以验证的判断。")));
    else {
      box.append(ui.table({caption: "累计与本周，按结果分", columns: [
        {key: "l", label: "结果", render: x => say(x.label)},
        {key: "t", label: "累计", num: true, render: x => fmt.number(x.to_date)},
        {key: "w", label: "本周", num: true, render: x => fmt.number(x.this_week)},
      ], rows: jo.rows || []}));
      if (jo.note) box.append(ui.note(say(jo.note)));
    }
  }
  const candidates = o.backlog_candidates || r.backlog_candidates || [];
  if (candidates.length) {
    box.append(h("h3", null, "下周可以研究的问题（候选）"),
      ui.note("这些只是候选，复盘本身不会改变研究方向。选一个写进「调整研究方向」后，仍要你确认方案才生效。"),
      h("ul", {class: "v2-list"}, candidates.map(c => h("li", {class: "v2-card"},
        h("p", null, say(c.question)), c.because ? h("p", {class: "v2-note"}, say(c.because)) : null,
        h("div", {class: "v2-actions"}, ui.button("写进「调整研究方向」", () => {
          ctx.drafts.set("steer:text", withQuestion(ctx.drafts.get("steer:text"), say(c.question)));
          ctx.navigate({page: "settings", filters: {focus: "steer"}});
        }, {variant: "quiet"}))))));
  }
  const suggestions = o.policy_suggestions || r.policy_suggestions || [];
  if (suggestions.length) box.append(h("h3", null, "后续研究建议"), h("ul", null, suggestions.map(x => h("li", null, say(x)))));
  if (o.authority_note || r.authority_note) box.append(ui.note(say(o.authority_note || r.authority_note)));
  box.append(h("p", null, h("a", {href: href({page: "settings", filters: {focus: "brief"}})}, "每周简报设置")));
  return box;
}

function eventRow(ctx, e) {
  const {h, ui, fmt, say} = ctx;
  return h("li", null, h("details", {class: "v2-more v2-traj"},
    h("summary", null, h("span", {class: "v2-num"}, fmt.datetime(e.at)), " ", ui.tag(KIND_LABEL[e.kind] || "其他"),
      " ", ui.tag(STATUS[e.status] || "状态未知", TONE[e.status] || null), " ", say(e.title || "").replace(/工单/g, "研究任务"), e.cost_usd ? ` · ${fmt.usd(e.cost_usd)}` : ""),
    ui.kv([["时间", fmt.datetime(e.at)], ["类型", KIND_LABEL[e.kind] || "其他"], ["结果", STATUS[e.status] || "状态未知"],
      e.actor ? ["环节", say(e.actor)] : null, e.detail ? ["说明", DETAIL[e.detail] || say(e.detail)] : null,
      e.cost_usd ? ["费用", fmt.usd(e.cost_usd)] : null].filter(Boolean))));
}

/** The trajectory drawer.  `state` = {events, older, as_of, kind}. */
export function trajectoryView(ctx, state, {onKind, onOlder} = {}) {
  const {h, ui, fmt} = ctx;
  const box = h("div", {class: "v2-drawer", "data-kind": state.kind || ""});
  const rows = filterEvents(state.events, state.kind);
  const calls = state.events.filter(e => e.kind === "call" && e.status === "ok").length;
  const cost = state.events.reduce((n, e) => n + (Number(e.cost_usd) || 0), 0);
  box.append(ui.note("系统最近每一步做了什么：哪项研究调用了 AI、研究任务排队和完成、超出预算未执行的请求。点开一行看详情。运行故障和失败台账在管理员页面。"),
    h("div", {class: "v2-toggle-row", role: "group", "aria-label": "按类型筛选研究轨迹"}, KINDS.map(k =>
      h("button", {type: "button", class: "v2-btn v2-btn-quiet v2-toggle", "data-k": k.key, "aria-pressed": (state.kind || "") === k.key ? "true" : "false",
        on: {click: () => onKind && onKind(k.key)}}, k.label))),
    h("p", {class: "v2-note", role: "status"}, `已读取 ${state.events.length} 条 · 其中 AI 调用完成 ${calls} 次 · 费用 ${fmt.usd(cost)} · 更新于 ${fmt.datetime(state.as_of)}`),
    rows.length ? h("ul", {class: "v2-list v2-traj-list"}, rows.map(e => eventRow(ctx, e)))
      : ui.empty(state.events.length ? "这一类还没有记录；可以加载更早的记录。" : "还没有研究轨迹。设定研究目标、系统开始工作后，这里会列出每一步。"));
  if (state.older) {
    const more = ui.button("加载更早的记录", async () => {
      more.disabled = true;
      try { await onOlder(); } catch (e) { box.append(ui.alert(`没有读到更早的记录：${e.display_message || "请稍后重试"}`, "danger")); }
      finally { more.disabled = false; }
    }, {variant: "secondary"});
    box.append(h("div", {class: "v2-actions"}, more));
  }
  return box;
}

/* ------------------------------------------------- drawer controller -- */

let trajectory = null;   // {events, older, as_of, kind}

/* The live URL filters (a page's ctx.state is the snapshot it rendered with). */
function currentFilters(ctx) {
  const live = globalThis.window?.DaltonV2?.state?.get?.();
  return (live && live.page === "overview" ? live.filters : ctx.state.filters) || {};
}

export async function openPanel(ctx, overview) {
  const {ui} = ctx;
  const panel = ctx.state.filters.panel;
  const close = () => {
    const filters = {...currentFilters(ctx)};
    delete filters.panel; delete filters.kind;
    ctx.navigate({filters}, {replace: true});
  };
  if (panel === "reflection") {
    ctx.side.open({heading: "本周复盘", returnLabel: "返回研究总览", onClose: close,
      render: el => el.append(ui.note("正在读取…"))});
    let r = null, error = null;
    try { r = await ctx.api("/v1/cockpit/reflection"); } catch (e) { error = e; }
    const body = ctx.side.body?.();
    if (!body || currentFilters(ctx).panel !== "reflection") return;
    body.replaceChildren(error ? ui.errorBox(error, () => openPanel(ctx, overview)) : reflectionView(ctx, r, {acceptance: overview?.ops?.acceptance}));
    return;
  }
  if (panel === "trajectory") {
    ctx.side.open({heading: "研究轨迹", returnLabel: "返回研究总览", onClose: () => { trajectory = null; close(); },
      render: el => el.append(ui.note("正在读取…"))});
    try {
      const r = await ctx.api("/v1/cockpit/trajectory", {fresh: true});
      trajectory = {events: r.events || [], older: r.older || null, as_of: r.as_of, kind: ctx.state.filters.kind || ""};
    } catch (e) {
      ctx.side.body?.()?.replaceChildren(ui.errorBox(e, () => openPanel(ctx, overview)));
      return;
    }
    drawTrajectory(ctx);
  }
}

function drawTrajectory(ctx) {
  const body = ctx.side.body?.();
  if (!body || !trajectory) return;
  body.replaceChildren(trajectoryView(ctx, trajectory, {
    onKind: key => {
      trajectory.kind = key;
      drawTrajectory(ctx);
      body.querySelector(`[data-k="${key}"]`)?.focus();
      ctx.setFilter("kind", key);
    },
    onOlder: async () => {
      const r = await ctx.api(`/v1/cockpit/trajectory?before=${encodeURIComponent(trajectory.older)}`, {fresh: true});
      const seen = new Set(trajectory.events.map(e => `${e.at}|${e.title}`));
      trajectory.events.push(...(r.events || []).filter(e => !seen.has(`${e.at}|${e.title}`)));
      trajectory.older = r.older || null;
      drawTrajectory(ctx);
    },
  }));
}

/** Overview update: the drawer follows ?panel= and ?kind= in place. */
export async function syncPanel(ctx, overview) {
  const now = ctx.state.filters.panel || "", before = ctx.prev.filters?.panel || "";
  if (now === before) {
    if (now === "trajectory" && trajectory && (ctx.state.filters.kind || "") !== trajectory.kind) {
      trajectory.kind = ctx.state.filters.kind || "";
      drawTrajectory(ctx);
    }
    return;
  }
  if (!now) { if (ctx.side.isOpen()) ctx.side.close(true); return; }
  await openPanel(ctx, overview);
}
