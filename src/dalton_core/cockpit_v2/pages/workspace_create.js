/* Analyst entry shared by Settings and the project switcher. */
export function workspaceCreatePanel(ctx, catalog = {}) {
  const {h, ui} = ctx;
  const name = h("input", {maxlength: "80", "aria-label": "新项目名称"});
  const goal = h("textarea", {rows: "3", maxlength: "2000", "aria-label": "初始研究目标（可选）"});
  const status = h("div", {role: "status", "aria-live": "polite"});
  let request = ctx.requestId();
  const create = ui.button("创建并进入研究项目", async () => {
    if (!name.value.trim()) { status.replaceChildren(ui.receipt("请填写项目名称", "danger")); return; }
    create.disabled = true;
    status.replaceChildren(ui.receipt("正在准备项目空间、初始化研究状态并启动服务…"));
    let timer = setInterval(async () => {
      try {
        const list = await ctx.api("/v1/cockpit/workspaces", {fresh: true});
        const row = (list.items || []).find(x => x.name === name.value.trim());
        const phase = {allocating_port: "正在分配项目空间…", initializing: "正在初始化研究状态…", starting_services: "正在启动研究服务…", checking_readiness: "正在检查研究服务是否就绪…", ready: "项目已准备好…"};
        if (row?.progress) status.replaceChildren(ui.receipt(phase[row.progress] || "正在准备项目…"));
      } catch (_) { /* the create receipt remains authoritative */ }
    }, 3000);
    try {
      const result = await ctx.api.post("/v1/cockpit/workspace_create", {name: name.value.trim(), initial_goal: goal.value.trim(), request_id: request});
      const workspace = result.workspace;
      if (workspace?.url && result.status === "running") globalThis.location.href = workspace.url.replace(/\/+$/, "") + "/#/overview";
      else status.replaceChildren(ui.receipt("项目尚未准备完成，可以重试同一次创建请求", "warn"));
    } catch (error) { status.replaceChildren(ui.receipt(error.display_message || error.message, "danger")); }
    finally { clearInterval(timer); create.disabled = false; }
  });
  if (catalog.can_create === false) create.disabled = true;
  return h("details", {id: "new-project-title", open: ctx.state?.filters?.focus === "new_project" || null},
    h("summary", null, "新建研究项目…"),
    h("label", null, "项目名称", name), h("label", null, "初始研究目标（可选）", goal),
    ui.note("请明确行业和股票代码（如 $ASML）；范围完整时创建即发布首版研究目标并开始研究，信息不足时保存草稿并请你补充。每人每天默认最多创建三个项目。"),
    catalog.can_create === false ? ui.note(catalog.create_disabled_reason || "当前暂不能新建项目，请联系管理员查看权限或每日额度") : null, create, status);
}
