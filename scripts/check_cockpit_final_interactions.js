async (page) => {
  return await page.evaluate(async () => {
  const checks = {};
  const research = {
    products: [{
      kind: "dossier", label: "公司档案", status: "available",
      version_ref: "dossier:v3", created_at: "2026-09-10T00:00:00Z",
      sections: [{title: "业务", body: "已发布正文", gaps: [],
        sources: [{kind: "claim", ref: "claim:v8", text: "公司披露"}]}],
    }, {kind: "investment_memo", label: "投资备忘录", status: "missing",
      reason: "等待模型出口门", gaps: [], sections: []}],
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
