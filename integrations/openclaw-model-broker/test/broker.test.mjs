import assert from "node:assert/strict";
import test from "node:test";

import { ModelBroker } from "../src/broker.mjs";
import { MemoryIdempotencyJournal } from "../src/journal.mjs";
import { ProtocolError, contentHash, validateRequest } from "../src/protocol.mjs";

function config(overrides = {}) {
  return {
    dedicatedAgentId: "dalton-model-broker",
    clientId: "client:dalton-runtime",
    profiles: [
      { id: "profile:research", model: "openai/gpt-5.6", maxTokens: 1000, timeoutMs: 500 },
    ],
    maxFrameBytes: 4096,
    maxOutputBytes: 4096,
    maxConcurrent: 2,
    idleTimeoutMs: 500,
    ...overrides,
  };
}

function request(overrides = {}) {
  return {
    schemaVersion: "0.1",
    invocationId: "invocation:one",
    workOrderId: "work:one",
    profileId: "profile:research",
    model: "openai/gpt-5.6",
    prompt: "Summarize the verified filing evidence.",
    maxTokens: 256,
    timeoutMs: 100,
    ...overrides,
  };
}

function fakeRuntime(complete) {
  return { version: "2026.7.1", llm: { complete } };
}

function result(overrides = {}) {
  return {
    text: "Verified summary",
    provider: "openai",
    model: "gpt-5.6",
    agentId: "dalton-model-broker",
    usage: {
      inputTokens: 12,
      outputTokens: 3,
      cacheReadTokens: 2,
      totalTokens: 17,
      costUsd: 0.004,
    },
    ...overrides,
  };
}

function verifyHash(response) {
  const { contentHash: actual, ...body } = response;
  assert.equal(actual, contentHash(body));
}

test("calls only host-owned completion with a fixed agent and exact allowed model", async () => {
  const calls = [];
  const runtime = fakeRuntime(async (params) => {
    calls.push(params);
    return result();
  });
  const broker = new ModelBroker(runtime, config());
  const response = await broker.handle(request());

  assert.equal(response.ok, true);
  assert.equal(response.provider, "openai");
  assert.equal(response.model, "gpt-5.6");
  assert.equal(response.canonicalModel, "openai/gpt-5.6");
  assert.equal(response.agentId, "dalton-model-broker");
  assert.deepEqual(response.usage, {
    inputTokens: 12,
    outputTokens: 3,
    cacheReadTokens: 2,
    cacheWriteTokens: null,
    totalTokens: 17,
  });
  assert.deepEqual(response.cost, { available: true, usd: 0.004 });
  assert.equal(response.runtimeVersion, "2026.7.1");
  assert.equal(response.brokerVersion, "0.1.0-spike.1");
  verifyHash(response);

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].messages, [{ role: "user", content: request().prompt }]);
  assert.equal(calls[0].agentId, "dalton-model-broker");
  assert.equal(calls[0].model, "openai/gpt-5.6");
  assert.equal(calls[0].maxTokens, 256);
  for (const forbidden of ["baseUrl", "headers", "apiKey", "authProfileId"]) {
    assert.equal(forbidden in calls[0], false);
  }
});

test("closed request rejects all credential and transport authority fields", () => {
  for (const field of ["baseUrl", "header", "headers", "apiKey", "authProfile", "authProfileId", "agentId"]) {
    assert.throws(
      () => validateRequest({ ...request(), [field]: "attacker-value" }, 4096),
      (error) => error instanceof ProtocolError && error.code === "UNKNOWN_FIELD",
      field,
    );
  }
  assert.throws(() => validateRequest({ ...request(), prompt: "" }, 4096), ProtocolError);
  assert.throws(
    () => validateRequest({ ...request(), replayOnly: "yes" }, 4096),
    ProtocolError,
  );
});

test("invocation idempotency is fresh, duplicate, or conflict", async () => {
  let calls = 0;
  const broker = new ModelBroker(fakeRuntime(async () => {
    calls += 1;
    return result();
  }), config());

  const fresh = await broker.handle(request());
  const duplicate = await broker.handle(request());
  const conflict = await broker.handle(request({ prompt: "different input" }));
  assert.equal(fresh.idempotencyStatus, "fresh");
  assert.equal(duplicate.idempotencyStatus, "duplicate");
  assert.equal(conflict.idempotencyStatus, "conflict");
  assert.equal(conflict.error.code, "IDEMPOTENCY_CONFLICT");
  assert.equal(calls, 1);
  verifyHash(fresh);
  verifyHash(duplicate);
  verifyHash(conflict);
});

test("replay-only reads durable completion and never calls host on miss", async () => {
  let calls = 0;
  const broker = new ModelBroker(fakeRuntime(async () => {
    calls += 1;
    return result();
  }), config());

  const miss = await broker.handle(request({
    invocationId: "invocation:replay-miss",
    workOrderId: "work:replay-miss",
    replayOnly: true,
  }));
  assert.equal(miss.ok, false);
  assert.equal(miss.error.code, "IDEMPOTENCY_MISS");
  assert.equal(calls, 0);

  const fresh = await broker.handle(request());
  const replay = await broker.handle(request({ replayOnly: true }));
  assert.equal(fresh.ok, true);
  assert.equal(replay.ok, true);
  assert.equal(replay.idempotencyStatus, "duplicate");
  assert.equal(replay.text, fresh.text);
  assert.equal(replay.requestHash, fresh.requestHash);
  assert.equal(calls, 1);
  verifyHash(miss);
  verifyHash(replay);

  let release;
  let inFlightCalls = 0;
  const blocked = new Promise((resolve) => { release = resolve; });
  const inFlightBroker = new ModelBroker(fakeRuntime(async () => {
    inFlightCalls += 1;
    await blocked;
    return result();
  }), config());
  const originalPromise = inFlightBroker.handle(request());
  await new Promise((resolve) => setImmediate(resolve));
  const replayPromise = inFlightBroker.handle(request({ replayOnly: true }));
  release();
  const [original, inFlightReplay] = await Promise.all([originalPromise, replayPromise]);
  assert.equal(original.idempotencyStatus, "fresh");
  assert.equal(inFlightReplay.idempotencyStatus, "duplicate");
  assert.equal(inFlightReplay.text, original.text);
  assert.equal(inFlightCalls, 1);
});

test("profile, model, token, and timeout bounds fail closed", async () => {
  let calls = 0;
  const broker = new ModelBroker(fakeRuntime(async () => {
    calls += 1;
    return result();
  }), config());
  const cases = [
    request({ invocationId: "invocation:bad-profile", profileId: "profile:other" }),
    request({ invocationId: "invocation:bad-model", model: "openai/gpt-5.5" }),
    request({ invocationId: "invocation:tokens", maxTokens: 1001 }),
    request({ invocationId: "invocation:timeout", timeoutMs: 501 }),
  ];
  for (const item of cases) {
    const response = await broker.handle(item);
    assert.equal(response.ok, false);
  }
  assert.equal(calls, 0);
});

test("timeout aborts the host request and output/attribution mismatches fail closed", async () => {
  let observedSignal;
  const timeoutBroker = new ModelBroker(fakeRuntime(({ signal }) => {
    observedSignal = signal;
    return new Promise(() => {});
  }), config());
  const timeout = await timeoutBroker.handle(request({ timeoutMs: 10 }));
  assert.equal(timeout.error.code, "TIMEOUT");
  assert.equal(observedSignal.aborted, true);

  const outputBroker = new ModelBroker(
    fakeRuntime(async () => result({ text: "x".repeat(20) })),
    config({ maxOutputBytes: 10 }),
  );
  const output = await outputBroker.handle(request());
  assert.equal(output.error.code, "OUTPUT_TOO_LARGE");

  const routeBroker = new ModelBroker(
    fakeRuntime(async () => result({ provider: "anthropic", model: "claude-sonnet-4-6" })),
    config(),
  );
  const route = await routeBroker.handle(request());
  assert.equal(route.error.code, "HOST_ATTRIBUTION_MISMATCH");
});

test("concurrency limit rejects excess work without consuming its invocation id", async () => {
  let release;
  let calls = 0;
  const blocked = new Promise((resolve) => { release = resolve; });
  const broker = new ModelBroker(fakeRuntime(async () => {
    calls += 1;
    await blocked;
    return result();
  }), config({ maxConcurrent: 1 }));
  const firstPromise = broker.handle(request());
  await new Promise((resolve) => setImmediate(resolve));
  const secondRequest = request({ invocationId: "invocation:two", workOrderId: "work:two" });
  const busy = await broker.handle(secondRequest);
  assert.equal(busy.error.code, "BUSY");
  release();
  await firstPromise;
  const retry = await broker.handle(secondRequest);
  assert.equal(retry.ok, true);
  assert.equal(calls, 2);
});

test("cost unavailability is explicit and host failures never echo prompts", async () => {
  const secretPrompt = "PRIVATE-PROMPT-CONTENT";
  const broker = new ModelBroker(fakeRuntime(async () => {
    throw new Error(`provider exploded while handling ${secretPrompt}`);
  }), config());
  const response = await broker.handle(request({ prompt: secretPrompt }));
  assert.equal(response.ok, false);
  assert.equal(response.error.code, "HOST_COMPLETION_FAILED");
  assert.equal(JSON.stringify(response).includes(secretPrompt), false);
  assert.deepEqual(response.cost, { available: false, usd: null });
});

test("memory journal never turns expired pending uncertainty into a host replay", async () => {
  let now = 1_000;
  let calls = 0;
  const pendingRequest = request({ invocationId: "invocation:pending-memory" });
  const journal = new MemoryIdempotencyJournal({ ttlMs: 60_000, clock: () => now });
  await journal.claim(pendingRequest.invocationId, contentHash(pendingRequest));
  now += 600_000;
  const broker = new ModelBroker(fakeRuntime(async () => {
    calls += 1;
    return result();
  }), config(), { journal });
  const response = await broker.handle(pendingRequest);
  assert.equal(response.error.code, "IDEMPOTENCY_INDETERMINATE");
  assert.equal(response.idempotencyStatus, "duplicate");
  assert.equal(calls, 0);
  assert.equal(journal.get(pendingRequest.invocationId).state, "pending");
});
