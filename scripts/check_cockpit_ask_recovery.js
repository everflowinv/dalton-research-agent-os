async (page) => {
  return await page.evaluate(async () => {
    const read=getJson,write=postJson,wait=waitJob;
    const result={answer:"离线回答已完成",citations:[],claims_considered:0,cost_usd:0,gaps:[]};
    const running={job_id:"offline:qa",status:"running",request:{question:"测试历史恢复"}};
    let items=[running],resolveAnswer;
    const checks={};
    getJson=async(path)=>path.includes("history?kind=ask")?{items:structuredClone(items)}:read(path);
    postJson=async()=>({job_id:"offline:qa"});
    waitJob=()=>new Promise(resolve=>{resolveAnswer=resolve});
    try{
      show("ask");await loadAskHistory();
      document.querySelector("#ask-input").value="测试新问题";
      const submit=document.querySelector("#ask-submit").onclick();
      await Promise.resolve();
      const before=document.querySelector("#chat .a");
      show("log");show("ask");await loadAskHistory();
      checks.switchPreservesActiveAnswer=before===document.querySelector("#chat .a");
      resolveAnswer(result);await submit;
      checks.resultVisibleAfterSwitch=before.isConnected&&before.textContent.includes(result.answer);
      await loadAskHistory();
      checks.pendingHistorySchedulesPoll=askHistoryTimer!==null&&document.querySelector("#chat").textContent.includes("还在处理");
      items=[{...running,status:"done",result}];
      await new Promise(resolve=>setTimeout(resolve,1950));
      checks.historyEventuallyShowsResult=document.querySelector("#chat").textContent.includes(result.answer);
      return {checks,passed:Object.values(checks).every(Boolean),writesToLive:0};
    }finally{clearTimeout(askHistoryTimer);getJson=read;postJson=write;waitJob=wait;askSubmitting=false;await loadAskHistory();}
  });
}
