/* 研究设置: only research scope, cost and conclusion rules (section 9.1).
   AI services, connectors, credentials and diagnostics belong to the
   administrator interface, linked through the settings.admin_link slot. */

import {workspaceCreatePanel} from "./workspace_create.js";
import {V, OVERVIEW, load} from "./common.js";
import {steerPanel} from "./steer.js";
import {policyEditor, policyHistory} from "./policy.js";
import {templatesPanel} from "./templates.js";
import {weeklyBriefSection} from "./weekly_brief.js";
import {coveragePanel} from "./coverage.js";
import {LABELS, UI} from "../copy.js";

const POLICY = LABELS.policy;

/* 2026-10-09 (independent S01 / MUSE §7 / financial 4.14): the page has its
   own table of contents; each part is a section with a stable ?focus= URL. */
export const SETTINGS_SECTIONS = [
  ["scope", "研究课题与覆盖池"], ["cost", "投研预算"], ["rules", POLICY.title], ["templates", LABELS.page_title.templates],
  ["brief", "每周投研周报"], ["help", "帮助"],
];

const TIERS = {A: "重点跟踪（A）", B: "常规跟踪（B）", C: "观察池（C）"};
const SOURCE_LABELS = LABELS.research_source;

// Localize only the old generated defaults at display time. Saved proposal
// bytes and the owner's own wording remain the review/publish authority.
export function goalQuestionDisplay(value) {
  const text = typeof value === "string" ? value : value?.display_text || value?.text || value?.question || "";
  const prefix = "What evidence would confirm or reject this objective: ";
  if (text.startsWith(prefix)) return "哪些证据能够支持或推翻这一研究目标：" + text.slice(prefix.length);
  return ({"Which causal drivers explain the differences across the selected companies?": "哪些业务驱动因素能够解释所选公司的差异？",
    "What observable events would falsify the leading view?": "哪些可观察事件会推翻当前主要判断？"})[text] || text;
}
const ISSUES = {"industry requires review": "请补充研究的行业或领域",
  "at least one company and ticker require review": "建议名单尚未完成身份核对",
  "named company identities require verification": "指定公司的身份核对尚未完成，暂时保留原范围",
  "source plan requires review": "资料来源尚未准备好", "bounded budget defaults require review": "研究预算尚未准备好"};

/** One goal draft, whichever of the three shapes the server returned. */
export function draftView(ctx, r, onDone) {
  const {h, ui, fmt, say} = ctx;
  const d = r.draft || {};
  const box = h("div", {class: "v2-panel", id: "goal-draft"});
  const result = h("div", {role: "status"});
  const actions = () => {
    const publish = ui.button("确认覆盖范围并启动研究", async () => {
      publish.disabled = discard.disabled = true;
      result.replaceChildren(ui.receipt("正在发布…"));
      try {
        const out = await ctx.api.post("/v1/cockpit/publish", {draft_id: r.draft_id, draft_hash: r.draft_hash, request_id: ctx.requestId()});
        ctx.drafts.clear("goal:draft"); ctx.drafts.clear("goal:text");
        result.replaceChildren(ui.receipt(`已发布为第 ${out.version ?? "新"} 版研究目标，系统从下一轮开始按新目标搜集和阅读。`));
        onDone && onDone("published");
      } catch (e) { result.replaceChildren(ui.receipt(e.display_message, "danger")); publish.disabled = discard.disabled = false; }
    });
    const discard = ui.button("放弃这份草稿", async () => {
      publish.disabled = discard.disabled = true;
      try {
        await ctx.api.post("/v1/cockpit/decide", {kind: "draft:goal", ref: r.draft_id, hash: r.draft_hash, decision: "discard", request_id: ctx.requestId()});
        ctx.drafts.clear("goal:draft");
        result.replaceChildren(ui.receipt("已放弃这份草稿，当前研究目标不变。"));
        onDone && onDone("discarded");
      } catch (e) { result.replaceChildren(ui.receipt(e.display_message, "danger")); publish.disabled = discard.disabled = false; }
    }, {variant: "quiet"});
    return h("div", {class: "v2-actions"}, publish, discard);
  };
  if (d.schema_version === "workspace-first-mission-draft-0.1") {
    const m = d.mission_body || {};
    box.append(h("h3", null, "研究目标与覆盖范围草案"),
      ui.kv([["研究目标", say(m.title)], ["说明", say(m.objective)],
             ["研究公司", (m.universe || []).map(c => c.ticker).join("、") || "—"],
             ["资料来源", (m.source_plan || []).map(s => SOURCE_LABELS[s.source_ref] || "其他资料来源").join("、") || "—"],
             ["每日授权预算上限", m.budget ? fmt.usd(m.budget.max_daily_cost_usd) : "—"]]),
      h("h4", null, "研究问题"), h("ul", null, (m.research_questions || []).map(q => h("li", null, say(goalQuestionDisplay(q))))));
    if ((d.company_recommendations || []).length) box.append(
      h("h4", null, "建议覆盖名单"),
      h("ul", null, d.company_recommendations.map(c => h("li", null,
        `${c.name || c.suggested_name || c.ticker}${c.ticker ? `（${c.ticker}）` : ""}${c.included ? " · 身份已核对" : c.requested ? " · 指定公司待核对" : " · 暂未纳入建议名单"}`,
        c.industry_role ? h("p", null, say(c.industry_role)) : null,
        c.reason ? h("p", null, "建议覆盖理由：" + say(c.reason)) : null,
        c.identity_error ? h("p", null, say(c.identity_error)) : null,
        c.included && /^\d{10}$/.test(c.cik || "") ? h("a", {href: `https://www.sec.gov/edgar/browse/?CIK=${c.cik}`, target: "_blank", rel: "noopener"}, "公司资料") : null))));
    if (d.setup_state === "ready_for_confirmation") box.append(ui.note("确认后开始搜集、阅读和分析；关闭浏览器后研究会继续。"), actions());
    else box.append(h("h4", null, (d.company_recommendations || []).length ? "公司资料核对尚未完成" : "研究目标还需明确"), h("ul", null, (d.review_issues || []).map(x => h("li", null, ISSUES[x] || "请补充目标中的研究范围"))),
                    ui.note((d.company_recommendations || []).length ? "原目标已保存，可稍后重新整理并继续核对公司资料。" : "请说明想研究的行业、市场或核心问题，系统会据此建议覆盖名单。"));
  } else if (d.schema_version === "workspace-initial-goal-draft-0.1") {
    box.append(h("h3", null, "目标草稿已保存"), ui.kv([["标题", say(d.title)], ["目标", say(d.objective)]]),
      ui.note("保存草稿不会启动研究。补齐研究公司、资料范围、预算和研究规则后，系统会请你确认并开始。"));
  } else {
    box.append(h("h3", null, "研究目标草案"), d.summary ? h("p", null, say(d.summary)) : null,
      ui.kv([["标题", say(d.title)], ["目标", say(d.objective)]]),
      h("h4", null, "研究问题"), h("ul", null, (d.research_questions || []).map(q => h("li", null, say(goalQuestionDisplay(q))))));
    if ((d.suggested_companies || []).length) box.append(h("h4", null, "目标里提到的公司"),
      h("ul", null, d.suggested_companies.map(x => h("li", null, `${x.ticker}${x.already_covered ? "（已在覆盖名单）" : "（不在覆盖名单）"}${x.reason ? `：${say(x.reason)}` : ""}`))),
      ui.note("确认目标不会改变覆盖名单；增减研究公司需另外调整覆盖名单。"));
    box.append(ui.note("确认后发布为新一版研究目标；已有结论保留。"), actions());
  }
  box.append(result);
  return box;
}

/* Where ?focus=<name> lands (each a stable URL): the field to type in, or
   the heading of the part asked for. */
const FOCUS = {new_project: "new-project-title", goal: "goal-text", steer: "steer-text", rules: "policy-editor-title", templates: "templates-title",
               brief: "brief-title", plan: "plan-title", help: "help-title", cost: "cost-title", scope: "scope-title"};

function focusTarget(ctx) {
  const doc = globalThis.document;
  const name = ctx.state.filters.focus;
  if (!name || !doc) return;
  const el = doc.getElementById(FOCUS[name] || "") || doc.getElementById(`${name}-title`) || doc.getElementById(name);
  if (!el) return;
  el.scrollIntoView?.({block: "center"});
  el.focus?.();
}

function policyQuery(filters) {
  const q = new URLSearchParams();
  if (filters.left) q.set("left", filters.left);
  if (filters.right) q.set("right", filters.right);
  return `/v1/cockpit/conviction-policy${q.toString() ? `?${q}` : ""}`;
}

async function render(root, ctx) {
  const {h, ui, fmt, say} = ctx;
  root.append(h("div", {class: "v2-page-head"}, h("h1", null, LABELS.page_title.settings),
    h("span", {class: "v2-sub"}, `研究课题与覆盖池、投研预算、${POLICY.title}、财务模型导出模板和每周投研周报`)));
  root.append(h("nav", {class: "v2-subnav", "aria-label": "研究设置目录"},
    SETTINGS_SECTIONS.map(([id, label]) => h("a", {href: ctx.href({filters: {focus: id}})}, label))));
  const [ov, levers, policy, goals, steers, templates, brief, coverage] = await Promise.all([load(ctx, OVERVIEW), load(ctx, "/v1/cockpit/levers"),
    load(ctx, policyQuery(ctx.state.filters)), load(ctx, "/v1/cockpit/history?kind=goal", {fresh: true}),
    load(ctx, "/v1/cockpit/history?kind=steer", {fresh: true}), load(ctx, "/v1/cockpit/fund-templates"),
    load(ctx, "/v1/cockpit/weekly-brief", {fresh: true}), load(ctx, "/v1/cockpit/history?kind=coverage", {fresh: true})]);
  if (!ctx.alive()) return;
  if (ov.error) { root.append(ui.errorBox(ov.error, () => ctx.navigate({}, {replace: true}))); return; }
  const o = ov.value, goal = o.goal;

  // 1. 研究范围
  const scope = ui.section("scope", "研究课题与覆盖池管理");
  const workspaceCatalog = await load(ctx, "/v1/cockpit/workspaces");
  if (workspaceCatalog.value?.enabled) scope.append(workspaceCreatePanel(ctx, workspaceCatalog.value));
  if (goal) {
    scope.append(ui.kv([["研究目标", say(goal.display_title || goal.title)], ["版本", `第 ${goal.version} 版 · 发布于 ${fmt.date(goal.published_at)}`]]),
      h("details", {class: "v2-more"}, h("summary", null, "现行投研任务书与核心研究课题"), ...ui.paragraphs(say(goal.display_objective || goal.objective)),
        h("ol", null, (goal.research_questions || []).map(q => h("li", null, say(goalQuestionDisplay(q)))))));
    const versions = goal.history || [];
    if (versions.length > 1) scope.append(h("details", {class: "v2-more", id: "goal-versions"}, h("summary", null, `研究目标的历史版本（${versions.length}）`),
      ui.table({caption: "每次确认目标或研究范围变更都会生成一个新版本；最新的在最上面",
        columns: [
          {key: "v", label: "版本", render: x => `第 ${x.version} 版`},
          {key: "at", label: "发布时间", render: x => fmt.datetime(x.created_at)},
          {key: "t", label: "标题", render: x => say(x.title)},
          {key: "q", label: "研究问题", num: true, render: x => `${(x.research_questions || []).length} 个`},
          {key: "u", label: "研究公司", render: x => (x.universe || []).join("、") || "—"},
        ], rows: versions.slice().sort((a, b) => Number(b.version) - Number(a.version))})));
  } else scope.append(h("p", {class: "v2-lead"}, "先写下要研究什么、覆盖哪些公司，即可开始。"));
  // What the system plans to do next, and why (the old 研究计划 block).
  const plan = o.plan;
  if (plan) scope.append(h("h3", {id: "plan-title", tabindex: "-1"}, "投研跟踪推进日程"),
    plan.assessment ? h("p", {class: "v2-note"}, "以下评估对应当前材料清单与工作安排；财务模型和研究报告的完成情况请查看公司研究页。") : null,
    plan.assessment ? h("p", null, say(plan.assessment)) : null,
    (plan.directives || []).length ? h("ol", {class: "v2-list"}, plan.directives.map(d => h("li", {class: "v2-card"},
      h("div", {class: "v2-card-meta"}, ui.tag(say(d.action_label || ""), d.action === "stop" ? "warn" : null), h("span", null, `${say(d.subject || "")} · ${say(d.item || "")}`)),
      d.reason ? h("p", {class: "v2-note"}, say(d.reason)) : null))) : null,
    (plan.inquiries || []).length ? h("details", {class: "v2-more"}, h("summary", null, `待核实的问题（${plan.inquiries.length}）`),
      h("ul", null, plan.inquiries.map(q => h("li", null, `${say(q.subject || "")}：${say(q.question || "")}`, q.wants ? h("div", {class: "v2-note"}, `需要核实：${say(q.wants)}`) : null)))) : null,
    ui.note(`本跟踪日程于 ${fmt.datetime(plan.decided_at)} 依据最新财报披露期与研究目标自动编排${plan.stopped ? `；${plan.stopped} 项建议暂停` : ""}。如需调整重点研究方向，请在下方提交「${LABELS.page_title.steer}」。`));
  const list = levers.value?.companies || [];
  if (list.length) scope.append(h("h3", null, "覆盖标的名单"), ui.table({
    caption: "覆盖标的名单包含重点跟踪（A）、常规跟踪（B）及观察池（C）；覆盖等级决定研究深度与资料更新频率，增减标的会联动调整检索与财报监控计划",
    columns: [
      {key: "label", label: "公司", render: c => say(c.label)},
      {key: "tier", label: "覆盖等级", render: c => TIERS[c.coverage_tier] || c.coverage_tier || "—"},
      {key: "state", label: "状态", render: c => (c.paused ? ui.tag(say(c.paused_label || "已暂停"), "warn") : c.prioritized ? ui.tag("已提高更新频率", "ok") : "正常跟踪")},
    ],
    rows: list,
  }));
  else if (levers.error) scope.append(ui.errorBox(levers.error));
  if (goal) scope.append(coveragePanel(ctx, {...levers.value, coverage_drafts: coverage.value?.drafts || []}));

  // The goal editor: persistent label, inline validation, a draft that is
  // never lost (sessionStorage) and a pending server draft restored, not
  // regenerated at a second cost (H06).
  const editor = h("div", {class: "v2-panel"});
  const text = ui.field({id: "goal-text", label: goal ? "修订投研核心目标与考察范围" : "研究目标", required: true, maxLength: 4000,
    help: goal ? "请写下重点调整的子赛道或需重点求证的假设。新增或移除公司请使用上方「调整覆盖名单」。这里生成的任务书草案经审阅后生效。"
               : "写下要研究的行业、公司范围和想回答的问题，例如：美国云计算基础设施公司未来四个季度的资本开支和收入增长。"});
  ctx.drafts.bind(text.input, "goal:text");
  const status = h("div", {role: "status", "aria-live": "polite"});
  const draftHolder = h("div");
  const submit = ui.button(goal ? "生成任务书修订草案" : "整理成研究目标草案", null);
  submit.type = "submit";
  const modelOk = o.model_available?.available !== false;
  if (!modelOk) { submit.disabled = true; status.append(ui.alert(say(o.model_available.reason || "AI 服务暂时不可用。"))); }
  const form = h("form", {novalidate: true}, h("h3", null, goal ? "调整研究目标" : "设定研究目标"), text.wrap,
    h("div", {class: "v2-actions"}, submit), status);
  editor.append(form, draftHolder);
  scope.append(editor);
  // Natural-language steering of the research direction (the old 策略指引 page).
  scope.append(steerPanel(ctx, {goal, modelOk, modelReason: o.model_available?.reason, history: steers.value}));
  if (steers.error && goal) scope.append(ui.alert(`调整记录暂时无法读取：${steers.error.display_message}`, "danger"));
  root.append(scope);
  const showDraft = r => {
    // S06: a published or discarded goal redraws the page in place, keeping
    // what is typed elsewhere and the reading position.
    draftHolder.replaceChildren(draftView(ctx, r, outcome => {
      goals.value = null;
      if (outcome === "published" && ctx.refresh) setTimeout(() => ctx.refresh(), 1200);
    }));
    ctx.drafts.setJson("goal:draft", r);
  };
  const open = (goals.value?.drafts || []).find(x => x.status === "open");
  const held = ctx.drafts.getJson("goal:draft");
  if (open) {
    if (!text.input.value) text.input.value = open.input_text || open.draft?.mission_body?.objective || open.draft?.objective || "";
    showDraft({draft: open.draft, draft_id: open.draft_id, draft_hash: open.draft_hash});
  }
  else if (held && held.draft_id && !(goals.value?.drafts || []).some(x => x.draft_id === held.draft_id && x.status !== "open")) showDraft(held);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (!text.check()) { text.input.focus(); return; }
    submit.disabled = true;
    status.replaceChildren(ui.receipt("正在整理，通常需要 20–60 秒…"));
    try {
      const job = await ctx.api.post("/v1/cockpit/goal", {text: text.input.value.trim(), request_id: ctx.requestId()});
      const r = await ctx.api.job(job.job_id);
      showDraft(r);
      status.replaceChildren(ui.receipt("草案已整理，请确认。"));
      draftHolder.querySelector("h3")?.setAttribute("tabindex", "-1");
      draftHolder.querySelector("h3")?.focus();
    } catch (err) { status.replaceChildren(ui.receipt(err.display_message, "danger")); }
    finally { submit.disabled = !modelOk; }
  });

  // 2. 费用
  const cost = ui.section("cost", "投研预算与算力额度");
  const b = o.budgets || {};
  const mc = b.model_calls, shared = b.shared_daily;
  if (mc || shared) {
    cost.append(ui.kv([
      mc ? ["本研究项目今日已用额度", `${fmt.usd(mc.cost_usd)} ｜ 每日授权预算上限 ${fmt.usd(mc.cost_cap_usd)}${Number(mc.cost_cap_usd) > 0 ? `（已用 ${fmt.number(Number(mc.cost_usd) / Number(mc.cost_cap_usd) * 100, 1)}%）` : ""}`] : null,
      shared ? ["全部研究项目合并预算池", `已用 ${fmt.usd(shared.cost_usd)} ｜ 总授权 ${fmt.usd(shared.cost_cap_usd)}`] : null,
    ].filter(Boolean)));
    if (shared?.note) cost.append(ui.note(say(shared.note)));
    cost.append(ui.note("算力预算统一在此汇总，日常提问与研报撰写过程中不打扰操作。如需扩充每日预算池，请联系系统管理员。"));
  } else cost.append(goal ? ui.state({status: "预算数据暂时读不到。", impact: "看不到今天的预算使用；研究照常进行。",
      owner: "系统会自动恢复。", next: UI.retryLater})
    : ui.empty("设定研究目标后，这里会显示每日预算使用和上限。"));
  root.append(cost);

  // 3. 投资建议规则: what is in force, the editor, the versions and a comparison.
  const rules = ui.section("rules", POLICY.title);
  const rulesBody = h("div", {id: "rules-body"});
  rules.append(rulesBody);
  drawRules(ctx, rulesBody, policy);
  root.append(rules);

  // 4. 报告模板
  const tpl = ui.section("templates", LABELS.page_title.templates);
  if (templates.value && templates.value.enabled !== false) {
    const keyOf = ref => { const c = (o.companies || []).find(x => x.company_ref === ref); return c ? (c.ticker || c.company_ref) : ref; };
    tpl.append(templatesPanel(ctx, templates.value, {companyKey: keyOf}));
  } else if (templates.error) tpl.append(ui.errorBox(templates.error));
  else tpl.append(ui.empty(say(templates.value?.reason || "这个研究项目还不能使用基金 Excel 模板。")));
  root.append(tpl);

  // 5. 每周简报 -- inside the workbench (W01): time, destination, next send, history.
  const briefSection = ui.section("brief", LABELS.page_title.weekly_brief,
    h("p", null, "有行业研究证据包时，研究周报汇总研究变化，可按配置发送；尚未建立证据包时，研究进展周报汇总已登记的进展与资料缺项，保存在工作台查阅。"));
  const briefHolder = h("div");
  const drawBrief = view => briefHolder.replaceChildren(view.error ? ui.errorBox(view.error, async () => drawBrief(await load(ctx, "/v1/cockpit/weekly-brief", {fresh: true})))
    : weeklyBriefSection(ctx, view.value, {onSaved: async () => drawBrief(await load(ctx, "/v1/cockpit/weekly-brief", {fresh: true}))}));
  drawBrief(brief);
  briefSection.append(briefHolder);
  root.append(briefSection);

  // 6. 帮助: the old interface (G01) and the administrator's settings (G02).
  const help = ui.section("help", "帮助",
    h("h3", null, "历史界面（旧版）"),
    h("p", null, "旧版工作台只保留查阅历史记录的用途，术语和状态含义与这里不同；日常研究请在这里完成。"),
    h("div", {class: "v2-actions"}, h("a", {class: "v2-btn v2-btn-quiet", href: "/legacy"}, "历史界面（旧版）")),
    h("h3", null, "管理员设置"));
  const holder = h("div");
  help.append(holder);
  root.append(help);
  await ctx.slot("settings.admin_link", holder, {overview: o}, el => {
    const href = ctx.adminHref();
    if (href) el.append(h("p", null, "AI 服务、资料来源、预算上限和运行诊断由管理员在这里维护。"),
      h("a", {class: "v2-btn v2-btn-secondary", href}, "打开管理员界面"));
    else el.append(h("p", null, "AI 服务、资料来源和运行诊断由管理员维护；管理员界面尚未开放，需要时请联系管理员。"));
  });
  if (ctx.state.filters.focus) setTimeout(() => focusTarget(ctx), 0);
}

let policyReceipt = null;   // the last publish, kept on the rules section across redraws

function drawRules(ctx, el, policy) {
  const {h, ui, say, fmt} = ctx;
  if (policy.error) { el.replaceChildren(ui.errorBox(policy.error)); return; }
  const v = policy.value || {};
  if (!v.effective) { el.replaceChildren(ui.empty(`尚未设置${POLICY.title}。`)); return; }
  const eff = v.effective;
  const fields = eff.fields || [];
  const receipt = policyReceipt ? h("div", {id: "policy-receipt", tabindex: "-1"}, ui.receipt(policyReceipt)) : null;
  el.replaceChildren(receipt,
    h("p", null, `当前执行标准：${say(eff.source_label || "")}${eff.version ? ` · 第 ${eff.version} 版` : ""}`),
    v.explainer ? ui.note(say(v.explainer)) : null,
    ui.kv(fields.slice(0, 6).map(f => [say(f.label), say(String(f.value ?? "—"))])),
    fields.length > 6 ? h("details", {class: "v2-more"}, h("summary", null, "全部规则"),
      ui.kv(fields.slice(6).map(f => [say(f.label), say(String(f.value ?? "—"))]))) : null,
    v.form ? policyEditor(ctx, v, {onPublished: async out => {
      policyReceipt = `${say(out?.message || "已发布")}（${fmt.datetime(new Date().toISOString())}）。新规则从下一次起草投资建议开始生效，已有建议不会被改写。`;
      const fresh = await load(ctx, policyQuery(ctx.state.filters), {fresh: true});
      drawRules(ctx, el, fresh);
      globalThis.document?.getElementById("policy-receipt")?.focus?.();
    }}) : null,
    h("div", {id: "policy-history-holder"}, policyHistory(ctx, v, {onCompare: (left, right) => ctx.navigate(
      {filters: {...ctx.state.filters, focus: "rules", left, right}}, {replace: true})})));
}

/* Same page, other filters: the policy comparison and ?focus= are handled in
   place, so a half-written form elsewhere on the page is never rebuilt. */
async function update(ctx) {
  const doc = globalThis.document;
  const f = ctx.state.filters, p = ctx.prev.filters || {};
  const changed = new Set([...Object.keys(f), ...Object.keys(p)].filter(k => f[k] !== p[k]));
  if (![...changed].every(k => ["left", "right", "focus"].includes(k))) return false;
  if (changed.has("left") || changed.has("right")) {
    const holder = doc?.getElementById("policy-history-holder");
    if (!holder) return false;
    const policy = await load(ctx, policyQuery(f));
    if (policy.error) { holder.replaceChildren(ctx.ui.errorBox(policy.error)); return true; }
    holder.replaceChildren(policyHistory(ctx, policy.value, {onCompare: (left, right) => ctx.navigate(
      {filters: {...ctx.state.filters, focus: "rules", left, right}}, {replace: true})}));
    return true;
  }
  if (changed.has("focus")) focusTarget(ctx);
  return true;
}

V().registerPage("settings", {title: LABELS.nav.settings, nav: {label: LABELS.nav.settings, order: 50}, render, update});
