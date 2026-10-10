/* The existing owner scope-expansion writer, with an explicit review step. */
const SOURCES = {"source:sec-edgar": "SEC 申报检索", "source:alphaengine": "研报与纪要检索", "source:web-search": "公开网页检索"};

export function coverageDraftView(ctx, r, onDone) {
  const {h, ui, fmt, say} = ctx, d = r.draft || {};
  const box = h("div", {class: "v2-panel", id: "coverage-draft"});
  box.append(h("h3", {tabindex: "-1"}, "覆盖名单变更预览"), h("p", null, say(d.summary || "请核对名单调整")),
    ui.kv([["变更理由", d.reason || "—"], ["依据版本", d.based_on?.version ? `第 ${d.based_on.version} 版研究任务` : "当前研究任务"]]));
  if ((d.additions || []).length) box.append(h("h4", null, "新增"), h("ul", null, d.additions.map(x => h("li", null,
    x.resolution === "resolved" ? `${x.ticker} · ${x.name || "名称未返回"} · 覆盖等级 ${x.coverage_tier}`
      : x.resolution === "already_covered" ? `${x.ticker}：已在覆盖名单中`
      : `${x.ticker}：${x.error || "尚未识别发行人"}`))));
  if ((d.removals || []).length) box.append(h("h4", null, "移除"), h("ul", null, d.removals.map(x => h("li", null, x.ticker || x.company_ref))));
  if ((d.universe_after || []).length) box.append(h("h4", null, "确认后的覆盖名单"), h("p", null, d.universe_after.map(x => x.ticker || x.company_ref).join("、")));
  if ((d.plans || []).length) box.append(h("h4", null, "资料检索与监控计划"), h("ul", null, d.plans.map(p => h("li", null,
    `${SOURCES[p.source_ref] || "资料检索"}：新增 ${(p.added || []).length} 家，移除 ${(p.removed || []).length} 家${p.empty ? "；调整后没有公司可检索" : ""}`))));
  const budget = d.budget || {}, shares = budget.coverage_pool_usd_per_company || {};
  if (budget.max_daily_cost_usd != null) box.append(h("h4", null, "预算影响"), h("p", null,
    `每日上限 ${fmt.usd(budget.max_daily_cost_usd)}；每家公司预算 ${fmt.usd(shares.before)} → ${fmt.usd(shares.after)}。${budget.note || ""}`));
  const unresolved = (d.additions || []).some(x => !["resolved", "already_covered"].includes(x.resolution));
  if ((d.warnings || []).length) box.append(ui.alert(d.warnings.map(say).join("；"), "warning"));
  const identities = (d.additions || []).filter(x => x.resolution === "resolved");
  if (identities.length) box.append(h("details", {class: "v2-more"}, h("summary", null, "发行人身份核对"),
    h("ul", null, identities.map(x => h("li", null, `${x.ticker} · SEC CIK ${x.cik}`)))));
  const result = h("div", {role: "status", "aria-live": "polite"});
  const uncertain = e => e?.status === 0 || e?.status >= 500;
  const holdForVerification = e => {
    ctx.drafts.setJson("coverage:draft", {...r, verification_needed: true});
    publish.disabled = discard.disabled = true;
    result.replaceChildren(ui.receipt(`尚未确认名单变更结果：${e.display_message || e.message}。草案和填写内容已保留，请先刷新核对名单版本与待确认记录。`, "warning"),
      ui.button("刷新核对名单", () => ctx.refresh?.()));
  };
  const publish = ui.button("确认调整覆盖名单", async () => {
    if (publish.disabled) return;
    publish.disabled = discard.disabled = true;result.replaceChildren(ui.receipt("正在发布名单变更…"));
    try {
      const out = await ctx.api.post("/v1/cockpit/publish", {draft_id: r.draft_id, draft_hash: r.draft_hash, request_id: ctx.requestId()});
      ctx.drafts.clear("coverage:draft");
      const errors = out.plan_errors || [];
      result.replaceChildren(ui.receipt(`覆盖名单已发布为第 ${out.version ?? "新"} 版${errors.length ? `；${errors.length} 项资料检索计划更新失败。` : "，检索和财报监控将按新名单执行。"}`, errors.length ? "warning" : "success"));
      if (errors.length) result.append(h("ul", null, errors.map(error => h("li", null, say(typeof error === "string" ? error : error.message || error.reason || error.error || "资料检索计划尚未更新")))),
        ui.note("名单变更已生效。请联系管理员修复这些检索计划，并在刷新后核对更新状态。"));
      onDone?.("published");
    } catch (e) {if (uncertain(e)) holdForVerification(e);else {result.replaceChildren(ui.receipt(e.display_message || e.message, "danger"));publish.disabled = unresolved;discard.disabled = false;}}
  });
  publish.disabled = unresolved || Boolean(r.verification_needed);
  const discard = ui.button("放弃名单变更", async () => {
    if (discard.disabled) return;
    publish.disabled = discard.disabled = true;
    try {
      await ctx.api.post("/v1/cockpit/decide", {kind: "draft:coverage", ref: r.draft_id, hash: r.draft_hash, decision: "discard", request_id: ctx.requestId()});
      ctx.drafts.clear("coverage:draft");result.replaceChildren(ui.receipt("名单变更已放弃，覆盖名单保持当前版本。"));onDone?.("discarded");
    } catch (e) {if (uncertain(e)) holdForVerification(e);else {result.replaceChildren(ui.receipt(e.display_message || e.message, "danger"));publish.disabled = unresolved;discard.disabled = false;}}
  }, {variant: "quiet"});
  if (r.verification_needed) {discard.disabled = true;result.append(ui.receipt("上次提交结果尚未确认，请先刷新核对名单版本。", "warning"), ui.button("刷新核对名单", () => ctx.refresh?.()));}
  box.append(ui.note("核对新增、移除和计划影响后确认，才会发布新版本。生成预览不调用 AI，也不会改变当前名单。"),
    h("div", {class: "v2-actions"}, publish, discard), result);
  return box;
}

export function coveragePanel(ctx, levers) {
  const {h, ui} = ctx, box = h("div", {class: "v2-panel", id: "coverage-editor"});
  if (!levers?.enabled || !levers.mission_ref) return ui.empty("已有研究任务后，可在这里调整覆盖名单。");
  const add = ui.field({id: "coverage-add", label: "新增股票代码", maxLength: 1000, help: "多个代码用逗号或空格分开。系统会核对发行人身份。"});
  const reason = ui.field({id: "coverage-reason", label: "名单变更理由", required: true, maxLength: 2000});
  ctx.drafts.bind(add.input, "coverage:add");ctx.drafts.bind(reason.input, "coverage:reason");
  const tier = h("select", {id: "coverage-tier"}, ["A", "B", "C"].map(value => h("option", {value, selected: value === "B"}, {A: "重点跟踪（A）", B: "常规跟踪（B）", C: "观察池（C）"}[value])));
  const removed = new Set(ctx.drafts.getJson("coverage:remove") || []);
  const choices = (levers.companies || []).map(c => {
    const input = h("input", {type: "checkbox", id: `coverage-remove-${c.ticker || c.company_ref.replace(/[^a-z0-9]/gi, "-")}`, value: c.company_ref, checked: removed.has(c.company_ref)});
    input.addEventListener("change", () => {input.checked ? removed.add(c.company_ref) : removed.delete(c.company_ref);ctx.drafts.setJson("coverage:remove", [...removed]);});
    return h("label", {for: input.id}, input, ` ${c.label || c.ticker}`);
  });
  const status = h("div", {role: "status", "aria-live": "polite"}), holder = h("div");
  const submit = ui.button("预览覆盖名单变更", null);submit.type = "submit";
  let pending = false;
  const show = r => {
    pending = true;submit.disabled = true;ctx.drafts.setJson("coverage:draft", r);
    holder.replaceChildren(coverageDraftView(ctx, r, outcome => {
      pending = false;submit.disabled = false;
      if (outcome === "published") {
        for (const key of ["coverage:add", "coverage:reason", "coverage:remove"]) ctx.drafts.clear(key);
        add.input.value = reason.input.value = "";removed.clear();choices.forEach(label => {label.querySelector("input").checked = false;});
        submit.disabled = true;if (ctx.refresh) setTimeout(() => ctx.refresh(), 1200);
      }
    }));
  };
  const form = h("form", {id: "coverage-form", novalidate: true}, h("h3", null, "调整覆盖名单"), add.wrap,
    h("label", {for: "coverage-tier"}, "新增公司的覆盖等级"), tier,
    h("fieldset", null, h("legend", null, "移除已有公司"), ...choices), reason.wrap,
    h("div", {class: "v2-actions"}, submit), status);
  form.addEventListener("submit", async e => {
    e.preventDefault();if (pending) return;
    if (!reason.check()) {reason.input.focus();return;}
    if (!add.input.value.trim() && !removed.size) {status.replaceChildren(ui.receipt("请填写新增股票代码，或勾选要移除的公司。", "danger"));add.input.focus();return;}
    submit.disabled = true;status.replaceChildren(ui.receipt("正在核对发行人并生成变更预览…"));
    try {
      const r = await ctx.api.post("/v1/cockpit/coverage_draft", {add: add.input.value.trim(), remove: [...removed],
        reason: reason.input.value.trim(), coverage_tier: tier.value, request_id: ctx.requestId()});
      show(r);status.replaceChildren(ui.receipt("变更预览已生成，请核对后确认。"));holder.querySelector("h3")?.focus();
    } catch (e) {
      if (e?.status === 0 || e?.status >= 500) {
        pending = true;
        status.replaceChildren(ui.receipt(`尚未确认草案是否生成：${e.display_message || e.message}。填写内容已保留，请先刷新核对待确认名单草案。`, "warning"), ui.button("刷新核对草案", () => ctx.refresh?.()));
      } else status.replaceChildren(ui.receipt(e.display_message || e.message, "danger"));
    }
    finally {submit.disabled = pending;}
  });
  box.append(form, holder);
  const open = (levers.coverage_drafts || []).find(r => r.status === "open");
  const saved = ctx.drafts.getJson("coverage:draft");
  const terminal = saved && (levers.coverage_drafts || []).some(r => r.draft_id === saved.draft_id && r.status !== "open");
  if (terminal) ctx.drafts.clear("coverage:draft");
  const held = open || (terminal ? null : saved);
  if (held?.draft_id && held?.draft_hash) show(held);
  return box;
}
