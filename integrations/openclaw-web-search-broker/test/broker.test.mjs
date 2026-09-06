import assert from "node:assert/strict";
import test from "node:test";

import { WebSearchBroker } from "../src/broker.mjs";
import { MemoryIdempotencyJournal } from "../src/journal.mjs";
import { PROTOCOL_VERSION, canonicalJson, contentHash } from "../src/protocol.mjs";

const CONFIG = { clientId: "client:dalton-core", expectedProvider: "gemini", maxCount: 10 };

function geminiPayload(query = "Accenture AI demand") {
  return {
    query,
    provider: "gemini",
    model: "gemini-2.5-flash",
    tookMs: 12,
    externalContent: { untrusted: true, source: "web_search", provider: "gemini", wrapped: true },
    content: "UNTRUSTED synthesis",
    citations: [{ url: "https://example.com/a", title: "A" }],
  };
}

function fakeRuntime(handler, { version = "2026.9.1" } = {}) {
  const calls = [];
  return {
    version,
    webSearch: {
      async search(input) {
        calls.push(input);
        return handler(input, calls.length);
      },
    },
    calls,
  };
}

function request(overrides = {}) {
  return {
    schemaVersion: PROTOCOL_VERSION,
    callRef: "credential-use:web-search:1",
    profileId: "profile:web-search",
    query: "Accenture AI demand",
    count: 5,
    timeoutMs: 30000,
    ...overrides,
  };
}

/** A refused request either throws ProtocolError or returns ok:false. */
async function assertRefused(broker, value, label) {
  let response;
  try {
    response = await broker.handle(value);
  } catch (error) {
    assert.equal(error.name, "ProtocolError", label);
    return;
  }
  assert.equal(response.ok, false, `${label} should be refused`);
}

function newBroker(runtime, config = CONFIG) {
  return new WebSearchBroker(runtime, config, { journal: new MemoryIdempotencyJournal({}), hostConfig: { host: true } });
}

test("a search returns the provider payload verbatim in a tool-result envelope", async () => {
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = newBroker(runtime);
  const response = await broker.handle(request());
  assert.equal(response.ok, true);
  assert.equal(response.idempotencyStatus, "fresh");
  assert.equal(response.callRef, "credential-use:web-search:1");
  const { contentHash: sealed, ...body } = response;
  assert.equal(sealed, contentHash(body));
  const text = response.result.content[0].text;
  assert.deepEqual(JSON.parse(text), geminiPayload());
  assert.equal(text, canonicalJson(geminiPayload()));
  // The host receives the exact query and count, and the host's own config.
  assert.deepEqual(runtime.calls[0].args, { query: "Accenture AI demand", count: 5 });
  assert.deepEqual(runtime.calls[0].config, { host: true });
});

test("an explicit date window is forwarded and must be complete and ordered", async () => {
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = newBroker(runtime);
  const response = await broker.handle(request({ dateAfter: "2026-08-01", dateBefore: "2026-09-06" }));
  assert.equal(response.ok, true);
  assert.deepEqual(runtime.calls[0].args, {
    query: "Accenture AI demand", count: 5, date_after: "2026-08-01", date_before: "2026-09-06",
  });
  for (const bad of [
    { dateAfter: "2026-08-01" },
    { dateBefore: "2026-09-06" },
    { dateAfter: "2026-09-07", dateBefore: "2026-09-06" },
    { dateAfter: "not-a-date", dateBefore: "2026-09-06" },
  ]) {
    await assertRefused(newBroker(fakeRuntime(() => geminiPayload())), request({ ...bad, callRef: "credential-use:web-search:2" }), JSON.stringify(bad));
  }
});

test("closed request shape, limits and unknown profiles are refused before the host", async () => {
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = newBroker(runtime);
  const cases = [
    ["unknown field", request({ apiKey: "leak", callRef: "credential-use:web-search:c0" })],
    ["count above the cap", request({ count: 99, callRef: "credential-use:web-search:c1" })],
    ["zero count", request({ count: 0, callRef: "credential-use:web-search:c2" })],
    ["empty query", request({ query: "", callRef: "credential-use:web-search:c3" })],
    ["unknown profile", request({ profileId: "profile:other", callRef: "credential-use:web-search:c4" })],
    ["timeout above the cap", request({ timeoutMs: 900000, callRef: "credential-use:web-search:c5" })],
    ["unsupported schema", request({ schemaVersion: "9.9", callRef: "credential-use:web-search:c6" })],
    ["callRef outside the credential-use namespace", request({ callRef: "invocation:not-a-credential-use" })],
    ["query above the char cap", request({ query: "x".repeat(401), callRef: "credential-use:web-search:c8" })],
  ];
  for (const [label, value] of cases) {
    await assertRefused(broker, value, label);
  }
  assert.equal(runtime.calls.length, 0, "no refused request may reach the host");
});

test("a provider swap or non-object payload is contract drift, not a result", async () => {
  for (const payload of [{ ...geminiPayload(), provider: "brave" }, "text", null, ["a"]]) {
    const runtime = fakeRuntime(() => payload);
    const response = await newBroker(runtime).handle(request());
    assert.equal(response.ok, false);
    assert.equal(response.error.code, "PROVIDER_CONTRACT_DRIFT");
  }
});

test("host failures are classified and never echo the query", async () => {
  const cases = [
    ["missing GEMINI_API_KEY for provider", "PROVIDER_PERMISSION_DENIED"],
    ["429 too many requests", "PROVIDER_RATE_LIMITED"],
    ["upstream exploded", "PROVIDER_ERROR"],
  ];
  for (const [message, code] of cases) {
    const runtime = fakeRuntime(() => { throw new Error(message); });
    const response = await newBroker(runtime).handle(request({ query: "secret-query-token" }));
    assert.equal(response.ok, false);
    assert.equal(response.error.code, code);
    assert.ok(!JSON.stringify(response).includes("secret-query-token"));
  }
});

test("the same callRef replays the stored result and never calls the host twice", async () => {
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = newBroker(runtime);
  const first = await broker.handle(request());
  const second = await broker.handle(request());
  assert.equal(first.idempotencyStatus, "fresh");
  assert.equal(second.idempotencyStatus, "duplicate");
  assert.equal(second.result.content[0].text, first.result.content[0].text);
  assert.equal(runtime.calls.length, 1);
  // A different query under the same callRef is a conflict, not a new search.
  const conflict = await broker.handle(request({ query: "another query" }));
  assert.equal(conflict.ok, false);
  assert.equal(conflict.error.code, "IDEMPOTENCY_CONFLICT");
  assert.equal(runtime.calls.length, 1);
});

test("replayOnly reads a durable result but never reaches the host on a miss", async () => {
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = newBroker(runtime);
  const miss = await broker.handle(request({ replayOnly: true }));
  assert.equal(miss.ok, false);
  assert.equal(miss.error.code, "IDEMPOTENCY_MISS");
  assert.equal(runtime.calls.length, 0);
  await broker.handle(request());
  const hit = await broker.handle(request({ replayOnly: true }));
  assert.equal(hit.ok, true);
  assert.equal(hit.idempotencyStatus, "duplicate");
  assert.equal(runtime.calls.length, 1);
});

test("a pending journal claim blocks automatic replay", async () => {
  const journal = new MemoryIdempotencyJournal({});
  const runtime = fakeRuntime(() => geminiPayload());
  const broker = new WebSearchBroker(runtime, CONFIG, { journal });
  await journal.claim("credential-use:web-search:1", "0".repeat(64));
  const response = await broker.handle(request());
  assert.equal(response.ok, false);
  assert.equal(response.error.code, "IDEMPOTENCY_CONFLICT");
  assert.equal(runtime.calls.length, 0);
});

test("a runtime without webSearch.search is refused at construction", () => {
  assert.throws(() => new WebSearchBroker({ version: "1" }, CONFIG), /webSearch\.search/);
  assert.throws(() => new WebSearchBroker(fakeRuntime(() => geminiPayload()), { ...CONFIG, clientId: "nope" }), /clientId/);
  assert.throws(() => new WebSearchBroker(fakeRuntime(() => geminiPayload()), { ...CONFIG, expectedProvider: "" }), /expectedProvider/);
});
