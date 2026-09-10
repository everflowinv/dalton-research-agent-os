async (page) => {
  const writes = [];
  const listener = request => { if (request.method() !== "GET") writes.push(request.method()); };
  page.on("request", listener);
  const pattern = "**/v1/cockpit/overview";
  await page.route(pattern, route => route.fulfill({status: 200, contentType: "application/json",
    body: JSON.stringify({state: "awaiting_mission", goal: null,
      workspace: {mode: "isolated", slug: "analyst-a", workspace_id: "11111111-1111-4111-8111-111111111111"}})}));
  try {
    await page.goto("http://127.0.0.1:18894/");
    await page.getByText("为这位分析师设定研究目标", {exact: true}).waitFor();
    if (await page.title() !== "analyst-a · Dalton 研究台") throw new Error("workspace identity absent");
    if (await page.locator("#hero-models").isVisible()) throw new Error("empty workspace showed mission model action");
    if (await page.locator("#companies").isVisible()) throw new Error("empty workspace showed phantom progress");
    await page.unroute(pattern);
    await page.evaluate(() => loadOverview());
    await page.locator(".company-launch").first().waitFor();
    if (!(await page.locator("#hero-models").isVisible())) throw new Error("mission controls did not recover");
    if (writes.length) throw new Error("unexpected write request");
    return {passed: true, emptyWorkspace: true, identityVisible: true, activeMissionRecovered: true, writesToLive: 0,
      note: "Empty workspace API response is synthetic; active mission comes from the GET-only local QA bridge."};
  } finally {
    await page.unroute(pattern);
    page.off("request", listener);
  }
}
