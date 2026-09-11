async (page) => {
  return await page.evaluate(async () => {
  const checks = {};
  const research = {
    products: [{
      kind: "dossier", label: "公司档案", status: "available",
      version_ref: "dossier:v3", created_at: "2026-09-10T00:00:00Z",
      mission_binding: "current",
      completeness: {status: "partial", drafted_units: 3, total_units: 12},
      sections: [{title: "业务", body: "已发布正文", gaps: [],
        sources: [{kind: "claim", ref: "claim:v8", text: "公司披露"}]}],
    }, {kind: "investment_memo", label: "投资备忘录", status: "missing",
      reason: "等待模型出口门", gaps: [], sections: []},
    {kind: "investment_memo", label: "待审备忘录", status: "available",
      version_ref: "memo:v2", mission_binding: "current",
      approval: {status: "pending_human_decision"}, sections: []},
    {kind: "investment_memo", label: "历史备忘录", status: "available",
      version_ref: "memo:v1", mission_binding: "historical",
      approval: {status: "historical"}, sections: []},
    {kind: "investment_memo", label: "已批准备忘录", status: "available",
      version_ref: "memo:v3", mission_binding: "current",
      approval: {status: "approved", actor_ref: "human:reviewer",
        decision_record_ref: "decision:exact-memo-v3"}, sections: []}],
  };
  const originalGet = getJson;
  getJson = async path => path.startsWith("/v1/cockpit/research") ? research : originalGet(path);
  const target = document.querySelector(".company-launch");
  target.focus();
  openCompany({
    ticker: "QA", name: "交互检查", company_ref: "company:QA",
    stage: "公司模型", stage_status: "进行中", stage_ref: "company_model",
    note: "", market: null, checklist: [],
    progress: {found: 1, read: 1, waiting: 0},
    claims: {total: 1, latest: []}, figures: {total: 0, latest: []},
    invariants: {}, reflections: [], research_tasks: [], cadence: [],
  }, target);
  try {
    const tabs = [...document.querySelectorAll(".detail-tab")];
    tabs[4].click();
    await new Promise(resolve => setTimeout(resolve, 0));
    checks.structuredSource = document.querySelector("#reader-sheet").innerText
      .includes("claim · claim:v8 · 公司披露");
    checks.missingReason = document.querySelector("#reader-sheet").innerText
      .includes("等待模型出口门");
    checks.noObjectLeak = !document.querySelector("#reader-sheet").innerText
      .includes("[object Object]");
    const productText = document.querySelector("#reader-sheet").innerText;
    checks.partialDossier = productText.includes("部分档案，仍在起草：3/12 单元")
      && productText.includes("起草进度不代表资料已更新或研究质量已验收");
    checks.missionVersionVisible = productText.includes("历史研究任务版本")
      && productText.includes("当前研究任务版本");
    checks.approvalNotPublication = productText.includes("等待人工审批")
      && productText.includes("历史版本，当前审批不适用")
      && productText.includes("人工审批已通过 · decision:exact-memo-v3 · human:reviewer");
    const probeProducts = document.createElement("div");
    const originalProducts = research.products;
    research.products = [{kind: "dossier", status: "available", label: "公司档案",
      mission_binding: "current", version_ref: "dossier:all",
      completeness: {status: "all_units_drafted", drafted_units: 12, total_units: 12},
      sections: []}];
    await loadResearchProducts("company:QA",probeProducts);
    checks.allUnitsNotQuality = probeProducts.textContent.includes("所有单元已起草：12/12 单元")
      && probeProducts.textContent.includes("起草进度不代表资料已更新或研究质量已验收");
    research.products = [{kind: "investment_memo", status: "available", label: "投资备忘录",
      mission_binding: "current", version_ref: "memo:rejected",
      approval: {status: "rejected"}, sections: []}];
    await loadResearchProducts("company:QA",probeProducts);
    checks.rejectedMemo = probeProducts.textContent.includes("人工审批未通过")
      && !probeProducts.textContent.includes("人工审批已通过");
    research.products[0].approval = {status: "pending_human_decision",
      reason: "current stage decision belongs to another memo version"};
    await loadResearchProducts("company:QA",probeProducts);
    checks.wrongMemoReason = probeProducts.textContent.includes("等待人工审批")
      && probeProducts.textContent.includes("current stage decision belongs to another memo version")
      && !probeProducts.textContent.includes("人工审批已通过");
    research.products = originalProducts;
    tabs[0].focus();
    tabs[0].dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true}));
    checks.arrowTabs = tabs[1].getAttribute("aria-selected") === "true"
      && document.activeElement === tabs[1];
    closeReader();
    checks.closeRestoresFocus = document.activeElement === target;
    const width = document.documentElement.scrollWidth;
    checks.noDocumentOverflow = width === window.innerWidth;
    const failingGet = getJson;
    getJson = async () => { throw new Error("offline reader check"); };
    const probe = document.createElement("button");
    probe.textContent = "reader focus probe";
    document.body.append(probe);
    const readers = [openSources, openModels, openReflection,
      () => openClaims("company:QA"), () => openModel("company:QA"),
      () => openDoc("doc:QA"), openOps];
    checks.allReadersRestoreFocus = true;
    for (const open of readers) {
      probe.focus();
      await Promise.resolve(open());
      await new Promise(resolve => setTimeout(resolve, 0));
      closeReader();
      checks.allReadersRestoreFocus &&= document.activeElement === probe;
    }
    probe.remove();
    getJson = failingGet;
    return {checks, passed: Object.values(checks).every(Boolean), writesToLive: 0};
  } finally {
    getJson = originalGet;
    closeReader();
  }
  });
}
