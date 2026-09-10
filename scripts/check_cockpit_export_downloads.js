async (page) => {
  const requests = [];
  let fail = false;
  const listener = request => {
    if (request.method() !== "GET") requests.push(request.method());
  };
  page.on("request", listener);
  const pattern = "**/v1/cockpit/export?**";
  await page.route(pattern, async route => {
    const format = (route.request().url().match(/[?&]format=(html|xlsx)(?:&|$)/) || [])[1];
    await route.fulfill({
      status: fail ? 400 : 200,
      contentType: "application/json",
      body: JSON.stringify(fail ? {message: "公司模型尚未形成"} : {
        filename: `QA-research.${format}`,
        media_type: format === "html" ? "text/html" : "application/octet-stream",
        content_base64: "UXVhbGl0eSBhc3N1cmFuY2UgZml4dHVyZQ==",
      }),
    });
  });
  try {
    await page.locator(".company-launch").first().click();
    await page.getByRole("tab", {name: "产物", exact: true}).click();
    const filenames = [];
    for (const label of ["下载 HTML 报告", "下载 Excel 模型"]) {
      const button = page.getByRole("button", {name: label, exact: true});
      const [download] = await Promise.all([
        page.waitForEvent("download"), button.click(),
      ]);
      filenames.push(download.suggestedFilename());
      if (!(await button.isEnabled())) throw new Error("download button stayed disabled");
    }
    fail = true;
    await page.getByRole("button", {name: "下载 Excel 模型", exact: true}).click();
    await page.getByRole("status").filter({hasText: "公司模型尚未形成"}).waitFor();
    const recovered = await page.getByRole("button", {name: "下载 Excel 模型", exact: true}).isEnabled();
    const passed = recovered && filenames.join(",") === "QA-research.html,QA-research.xlsx" && requests.length === 0;
    if (!passed) throw new Error("download regression failed");
    return {passed, filenames, failureVisible: true, buttonRecovered: recovered,
      writesToLive: requests.length, note: "Export bodies mocked; actual exporters tested against real Core fixtures."};
  } finally {
    await page.unroute(pattern);
    page.off("request", listener);
  }
}
