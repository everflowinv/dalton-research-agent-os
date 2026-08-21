import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { ModelBroker } from "../src/broker.mjs";
import { loadOrCreateSecret, signRequest } from "../src/auth.mjs";
import { FileIdempotencyJournal } from "../src/journal.mjs";
import { ProtocolError, contentHash, sealResponse } from "../src/protocol.mjs";
import { BrokerServer } from "../src/server.mjs";
import { createPluginDefinition } from "../src/plugin-definition.mjs";

const config = {
  dedicatedAgentId: "dalton-model-broker",
  clientId: "client:dalton-runtime",
  socketName: "broker.sock",
  profiles: [{ id: "profile:research", model: "openai/gpt-5.6", maxTokens: 100, timeoutMs: 500 }],
  maxFrameBytes: 2048,
  maxOutputBytes: 1024,
  maxConcurrent: 1,
  idleTimeoutMs: 200,
};

const request = {
  schemaVersion: "0.1",
  invocationId: "invocation:uds",
  workOrderId: "work:uds",
  profileId: "profile:research",
  model: "openai/gpt-5.6",
  prompt: "UDS request prompt",
  maxTokens: 50,
  timeoutMs: 100,
};

function runtime() {
  return {
    version: "2026.7.1",
    llm: {
      async complete() {
        return {
          text: "UDS response",
          provider: "openai",
          model: "gpt-5.6",
          agentId: "dalton-model-broker",
          usage: { inputTokens: 2, outputTokens: 2 },
        };
      },
    },
  };
}

function exchange(socketPath, frame) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath);
    let data = "";
    socket.setEncoding("utf8");
    socket.on("data", (chunk) => { data += chunk; });
    socket.on("end", () => resolve(JSON.parse(data)));
    socket.on("error", reject);
    socket.write(frame);
  });
}

function authenticated(core, secret, nonce = "1".repeat(32), timestampMs = Date.now()) {
  return signRequest(core, { secret, clientId: config.clientId, timestampMs, nonce });
}

test("UDS accepts one closed JSONL frame and is owner-only", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-broker-test-"));
  const server = new BrokerServer(new ModelBroker(runtime(), config));
  try {
    const socketPath = await server.start(stateDir);
    const { secret } = await loadOrCreateSecret(stateDir, config.socketName);
    assert.equal((await stat(socketPath)).mode & 0o777, 0o600);
    const response = await exchange(socketPath, `${JSON.stringify(authenticated(request, secret))}\n`);
    assert.equal(response.ok, true);
    const { contentHash: actual, ...body } = response;
    assert.equal(actual, contentHash(body));

    const bad = await exchange(socketPath, `${JSON.stringify(authenticated(request, secret, "2".repeat(32)))}\n{}\n`);
    assert.equal(bad.error.code, "INVALID_FRAME");
    const huge = await exchange(socketPath, `${"x".repeat(2050)}\n`);
    assert.equal(huge.error.code, "FRAME_TOO_LARGE");
  } finally {
    await server.stop();
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("UDS keeps an authenticated socket open while the model call is in flight", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-broker-slow-test-"));
  const slowConfig = {
    ...config,
    idleTimeoutMs: 100,
    profiles: [{ ...config.profiles[0], timeoutMs: 500 }],
  };
  const slowRuntime = runtime();
  slowRuntime.llm.complete = async () => {
    await new Promise((resolve) => setTimeout(resolve, 175));
    return {
      text: "slow but valid",
      provider: "openai",
      model: "gpt-5.6",
      agentId: "dalton-model-broker",
      usage: { inputTokens: 2, outputTokens: 3 },
    };
  };
  const server = new BrokerServer(new ModelBroker(slowRuntime, slowConfig));
  try {
    const socketPath = await server.start(stateDir);
    const { secret } = await loadOrCreateSecret(stateDir, slowConfig.socketName);
    const core = { ...request, invocationId: "invocation:slow", timeoutMs: 300 };
    const signed = signRequest(core, {
      secret,
      clientId: slowConfig.clientId,
      timestampMs: Date.now(),
      nonce: "b".repeat(32),
    });
    const response = await exchange(socketPath, `${JSON.stringify(signed)}\n`);
    assert.equal(response.ok, true);
    assert.equal(response.text, "slow but valid");
  } finally {
    await server.stop();
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("UDS rejects missing, wrong, replayed, expired, and duplicate-key authentication", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-auth-test-"));
  const server = new BrokerServer(new ModelBroker(runtime(), config));
  try {
    const socketPath = await server.start(stateDir);
    const { secret } = await loadOrCreateSecret(stateDir, config.socketName);
    const missing = await exchange(socketPath, `${JSON.stringify(request)}\n`);
    assert.equal(missing.error.code, "AUTH_REQUIRED");

    const wrong = authenticated(request, "f".repeat(64), "3".repeat(32));
    assert.equal((await exchange(socketPath, `${JSON.stringify(wrong)}\n`)).error.code, "AUTH_INVALID");

    const signed = authenticated(request, secret, "4".repeat(32));
    const first = await exchange(socketPath, `${JSON.stringify(signed)}\n`);
    assert.equal(first.ok, true);
    assert.equal((await exchange(socketPath, `${JSON.stringify(signed)}\n`)).error.code, "AUTH_REPLAY");

    const expired = authenticated(request, secret, "5".repeat(32), Date.now() - 60_000);
    assert.equal((await exchange(socketPath, `${JSON.stringify(expired)}\n`)).error.code, "AUTH_EXPIRED");

    const duplicateTop = JSON.stringify(authenticated(request, secret, "6".repeat(32))).replace(
      '"schemaVersion":"0.1"',
      '"schemaVersion":"0.1","schemaVersion":"0.1"',
    );
    assert.equal((await exchange(socketPath, `${duplicateTop}\n`)).error.code, "DUPLICATE_KEY");
    const valid = authenticated(request, secret, "7".repeat(32));
    const duplicateAuth = JSON.stringify(valid).replace(
      '"clientId":"client:dalton-runtime"',
      '"clientId":"client:dalton-runtime","clientId":"client:dalton-runtime"',
    );
    assert.equal((await exchange(socketPath, `${duplicateAuth}\n`)).error.code, "DUPLICATE_KEY");
  } finally {
    await server.stop();
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("owner-only journal survives restart and blocks replay or conflict before host", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-journal-test-"));
  let firstCalls = 0;
  const firstRuntime = runtime();
  firstRuntime.llm.complete = async () => { firstCalls += 1; return {
    text: "persisted response", provider: "openai", model: "gpt-5.6",
    agentId: "dalton-model-broker", usage: { inputTokens: 1, outputTokens: 2 },
  }; };
  const firstServer = new BrokerServer(new ModelBroker(firstRuntime, config));
  try {
    const socketPath = await firstServer.start(stateDir);
    const { secret } = await loadOrCreateSecret(stateDir, config.socketName);
    const fresh = await exchange(socketPath, `${JSON.stringify(authenticated(request, secret, "8".repeat(32)))}\n`);
    assert.equal(fresh.idempotencyStatus, "fresh");
    assert.equal(firstCalls, 1);
    await firstServer.stop();

    let restartCalls = 0;
    const restartRuntime = runtime();
    restartRuntime.llm.complete = async () => { restartCalls += 1; throw new Error("must not run"); };
    const restartServer = new BrokerServer(new ModelBroker(restartRuntime, config));
    const restartedPath = await restartServer.start(stateDir);
    const duplicate = await exchange(restartedPath, `${JSON.stringify(authenticated(request, secret, "9".repeat(32)))}\n`);
    assert.equal(duplicate.idempotencyStatus, "duplicate");
    assert.equal(duplicate.text, "persisted response");
    assert.equal(restartCalls, 0);
    const replayMissCore = {
      ...request,
      invocationId: "invocation:replay-only-miss",
      workOrderId: "work:replay-only-miss",
      replayOnly: true,
    };
    const replayMiss = await exchange(
      restartedPath,
      `${JSON.stringify(authenticated(replayMissCore, secret, "b".repeat(32)))}\n`,
    );
    assert.equal(replayMiss.error.code, "IDEMPOTENCY_MISS");
    assert.equal(restartCalls, 0);
    const conflictCore = { ...request, prompt: "changed prompt" };
    const conflict = await exchange(restartedPath, `${JSON.stringify(authenticated(conflictCore, secret, "a".repeat(32)))}\n`);
    assert.equal(conflict.idempotencyStatus, "conflict");
    assert.equal(restartCalls, 0);

    const journalPath = path.join(stateDir, `${config.socketName}.journal.json`);
    const keyPath = path.join(stateDir, `${config.socketName}.key`);
    assert.equal((await stat(journalPath)).mode & 0o777, 0o600);
    assert.equal((await stat(keyPath)).mode & 0o777, 0o600);
    const journalText = await readFile(journalPath, "utf8");
    assert.equal(journalText.includes(request.prompt), false);
    assert.equal(journalText.includes(secret), false);
    await restartServer.stop();
  } finally {
    await firstServer.stop();
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("pending crash record is indeterminate, corrupt journal fails closed, and bounds prune", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-journal-hostile-"));
  const journalOptions = { ttlMs: 60_000, maxRecords: 1, maxBytes: 4096, clock: () => 1000 };
  try {
    const journal = await FileIdempotencyJournal.open(stateDir, "hostile.sock", journalOptions);
    const pendingRequest = { ...request, invocationId: "invocation:pending" };
    await journal.claim("invocation:pending", contentHash(pendingRequest));
    const reopened = await FileIdempotencyJournal.open(stateDir, "hostile.sock", journalOptions);
    const broker = new ModelBroker(runtime(), config, { journal: reopened });
    const indeterminate = await broker.handle(pendingRequest);
    assert.equal(indeterminate.error.code, "IDEMPOTENCY_INDETERMINATE");

    await assert.rejects(
      reopened.claim("invocation:capacity", "2".repeat(64)),
      (error) => error instanceof ProtocolError && error.code === "JOURNAL_CAPACITY",
    );

    const bytesDir = await mkdtemp(path.join(os.tmpdir(), "dalton-journal-bytes-"));
    try {
      const bytesJournal = await FileIdempotencyJournal.open(bytesDir, "bytes.sock", {
        ttlMs: 60_000, maxRecords: 2, maxBytes: 700, clock: () => 1000,
      });
      await bytesJournal.claim("invocation:bytes", "5".repeat(64));
      const oversizedResponse = sealResponse({
        schemaVersion: "0.1", invocationId: "invocation:bytes", ok: true, text: "x".repeat(1000),
      });
      await assert.rejects(
        bytesJournal.complete("invocation:bytes", "5".repeat(64), oversizedResponse),
        (error) => error instanceof ProtocolError && error.code === "JOURNAL_CAPACITY",
      );
      const bytesReopened = await FileIdempotencyJournal.open(bytesDir, "bytes.sock", {
        ttlMs: 60_000, maxRecords: 2, maxBytes: 700, clock: () => 1000,
      });
      assert.equal(bytesReopened.get("invocation:bytes").state, "pending");
    } finally {
      await rm(bytesDir, { recursive: true, force: true });
    }

    let now = 1_000;
    const ttlDir = await mkdtemp(path.join(os.tmpdir(), "dalton-journal-ttl-"));
    try {
      const ttlJournal = await FileIdempotencyJournal.open(ttlDir, "ttl.sock", {
        ttlMs: 60_000, maxRecords: 2, maxBytes: 4096, clock: () => now,
      });
      const ttlRequest = { ...request, invocationId: "invocation:ttl" };
      const ttlHash = contentHash(ttlRequest);
      await ttlJournal.claim("invocation:ttl", ttlHash);
      now += 60_001;
      const retained = await ttlJournal.claim("invocation:ttl", ttlHash);
      assert.equal(retained.status, "pending");
      const ttlReopened = await FileIdempotencyJournal.open(ttlDir, "ttl.sock", {
        ttlMs: 60_000, maxRecords: 2, maxBytes: 4096, clock: () => now,
      });
      let calls = 0;
      const guardedRuntime = runtime();
      guardedRuntime.llm.complete = async () => { calls += 1; return {}; };
      const ttlBroker = new ModelBroker(guardedRuntime, config, { journal: ttlReopened });
      const blocked = await ttlBroker.handle(ttlRequest);
      assert.equal(blocked.error.code, "IDEMPOTENCY_INDETERMINATE");
      assert.equal(calls, 0);
    } finally {
      await rm(ttlDir, { recursive: true, force: true });
    }

    const corruptDir = await mkdtemp(path.join(os.tmpdir(), "dalton-journal-corrupt-"));
    try {
      const corruptPath = path.join(corruptDir, "bad.sock.journal.json");
      await writeFile(corruptPath, '{"schemaVersion":"0.1","records":[', { mode: 0o600 });
      await chmod(corruptPath, 0o600);
      await assert.rejects(
        FileIdempotencyJournal.open(corruptDir, "bad.sock", journalOptions),
        (error) => error instanceof ProtocolError && error.code === "INVALID_JOURNAL",
      );
    } finally {
      await rm(corruptDir, { recursive: true, force: true });
    }
  } finally {
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("plugin registers one service and logs no prompt content", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-plugin-test-"));
  const logs = [];
  let service;
  const api = {
    runtime: runtime(),
    pluginConfig: config,
    registerService(value) { service = value; },
  };
  try {
    createPluginDefinition().register(api);
    assert.equal(service.id, "dalton-openclaw-model-broker");
    const ctx = {
      stateDir,
      logger: { info(message, metadata) { logs.push({ message, metadata }); } },
    };
    await service.start(ctx);
    await service.stop(ctx);
    assert.equal(JSON.stringify(logs).includes(request.prompt), false);
    assert.equal(JSON.stringify(logs).includes("apiKey"), false);
  } finally {
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("source has no Dalton authority imports, model CLI args, or secret-bearing fields", async () => {
  const root = path.resolve(import.meta.dirname, "..");
  const files = ["index.mjs", "src/auth.mjs", "src/broker.mjs", "src/journal.mjs", "src/server.mjs", "src/protocol.mjs", "src/plugin-definition.mjs"];
  const source = (await Promise.all(files.map((file) => readFile(path.join(root, file), "utf8")))).join("\n");
  for (const forbidden of [
    "DaltonStore", "writerToken", "writer_token", "coverage.db", "sqlite3", "child_process",
    "api.runtime.config", "loadConfig", "writeConfig", "authProfileId:", "baseUrl:", "apiKey:",
  ]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
  assert.match(source, /api\.runtime|runtime\.llm\.complete/);
});
