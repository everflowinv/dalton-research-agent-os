async (page) => {
  return await page.evaluate(async () => {
    const read = getJson, render = renderOverview;
    const requests = [], rendered = [];
    let resolveRequest, rejectRequest;
    getJson = async path => {
      if (path !== "/v1/cockpit/overview") return read(path);
      requests.push(path);
      return await new Promise((resolve, reject) => {
        resolveRequest = resolve; rejectRequest = reject;
      });
    };
    renderOverview = value => rendered.push(value.marker);
    try {
      const first = loadOverview(), concurrent = loadOverview();
      const oneWhilePending = requests.length === 1;
      resolveRequest({marker: "first"});
      await Promise.all([first, concurrent]);
      const afterSuccess = loadOverview();
      const retriesAfterSuccess = requests.length === 2;
      resolveRequest({marker: "second"}); await afterSuccess;
      const failing = loadOverview();
      const thirdStarted = requests.length === 3;
      rejectRequest(new Error("offline delayed failure")); await failing;
      const afterFailure = loadOverview();
      const retriesAfterFailure = requests.length === 4;
      resolveRequest({marker: "recovered"}); await afterFailure;
      const checks = {oneWhilePending, retriesAfterSuccess, thirdStarted,
                      retriesAfterFailure,
                      rendersSuccessfulResponses: rendered.join(",") === "first,second,recovered"};
      return {checks, requestCount: requests.length,
              passed: Object.values(checks).every(Boolean), writesToLive: 0};
    } finally {
      getJson = read; renderOverview = render;
    }
  });
}
