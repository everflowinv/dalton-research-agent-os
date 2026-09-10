async (page) => {
  return await page.evaluate(async () => {
    const read = getJson, write = postJson;
    const item = {kind:"test",ref:"test:approval",hash:"hash:a",title:"离线审批测试",who:"QA",at:"2026-09-10T00:00:00Z",needs_rationale:true,actions:[{decision:"approve",label:"测试提交"},{decision:"reject",label:"不可用操作",disabled:true}]};
    let state = {count:1,as_of:"2026-09-10T00:00:00Z",items:[item]};
    getJson = async () => structuredClone(state);
    const checks = {};
    try {
      await loadApprovals();
      const area = document.querySelector("#approvals textarea");
      area.value="draft"; area.focus(); area.setSelectionRange(1,3);
      await loadApprovals();
      checks.refreshPreservesDraftAndFocus = area === document.querySelector("#approvals textarea") && area.value === "draft" && document.activeElement === area && area.selectionStart === 1;
      let rejectWrite;
      postJson = () => new Promise((_,reject)=>{rejectWrite=reject});
      document.querySelector("#approvals button").click();
      state.items[0].note="服务端说明更新";
      await loadApprovals();
      checks.pendingRefreshPreservesDisabled = [...document.querySelectorAll("#approvals button")].every(x=>x.disabled) && area === document.querySelector("#approvals textarea");
      rejectWrite(new Error("离线模拟失败")); await new Promise(r=>setTimeout(r,0));
      const buttons = document.querySelectorAll("#approvals button");
      checks.failureRetainsServerDisabledAction = !buttons[0].disabled && buttons[1].disabled;
      await loadApprovals();
      checks.changedDetailsKeepExactVersionDraft = document.querySelector("#approvals textarea").value === "draft";
      state.items[0].hash="hash:b";
      await loadApprovals();
      checks.newVersionDoesNotInheritDraft = document.querySelector("#approvals textarea").value === "";
      const pending=[];
      getJson = () => new Promise(resolve=>pending.push(resolve));
      const older=loadApprovals(), newer=loadApprovals();
      pending[1]({count:0,as_of:"2026-09-10T00:00:00Z",items:[]}); await newer;
      pending[0](state); await older;
      checks.staleResponseIgnored = !document.querySelector("#approvals textarea");
      return {checks,passed:Object.values(checks).every(Boolean),writesToLive:0};
    } finally {getJson=read;postJson=write;await loadApprovals();}
  });
}
