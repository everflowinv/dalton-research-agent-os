import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";
import test from "node:test";

import { WebSearchBroker } from "../src/broker.mjs";
import { loadOrCreateSecret, signRequest } from "../src/auth.mjs";
import { BrokerServer } from "../src/server.mjs";
import { createPluginDefinition } from "../src/plugin-definition.mjs";
import { BROKER_VERSION } from "../src/protocol.mjs";

const CONFIG = {
  clientId: "client:dalton-core",
  expectedProvider: "gemini",
  socketName: "search.sock",
  maxFrameBytes: 4096,
  idleTimeoutMs: 300,
};

const REQUEST = {
  schemaVersion: "0.1",
  callRef: "credential-use:web-search:uds",
  profileId: "profile:web-search",
  query: "Accenture AI demand",
  count: 3,
  timeoutMs: 5000,
};

function payload(query = REQUEST.query) {
  return {
    kind: "answer",
    query,
    provider: "gemini",
    tookMs: 4,
    externalContent: { untrusted: true, source: "web_search", provider: "gemini", wrapped: true },
    content: "UNTRUSTED synthesis",
    citations: [{ url: "https://example.com/a", title: "A" }],
  };
}

function runtime(handler = () => payload()) {
  const calls = [];
  return {
    version: "2026.9.1",
    webSearch: { async search(input) { calls.push(input); return { provider: "gemini", result: handler(input) }; } },
    calls,
  };
}

async function withServer(handler, run, config = CONFIG) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-search-broker-"));
  const host = runtime(handler);
  const broker = new WebSearchBroker(host, config, { hostConfig: {} });
  const server = new BrokerServer(broker);
  const socketPath = await server.start(stateDir);
  const { secret } = await loadOrCreateSecret(stateDir, config.socketName);
  try {
    return await run({ socketPath, secret, stateDir, host, broker });
  } finally {
    await server.stop();
    await rm(stateDir, { recursive: true, force: true });
  }
}

/**
 * Write one frame and keep the client half open.  The server replies with
 * ``socket.end(...)``; a client that half-closes first would make the server
 * socket auto-end before the asynchronous reply is written.
 */
function send(socketPath, frame) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(socketPath);
    let data = "";
    socket.on("connect", () => socket.write(frame));
    socket.on("data", (chunk) => { data += chunk.toString("utf8"); });
    socket.on("end", () => resolve(data));
    socket.on("error", reject);
  });
}

function signed(secret, overrides = {}) {
  return signRequest({ ...REQUEST, ...overrides }, {
    secret,
    clientId: CONFIG.clientId,
    timestampMs: Date.now(),
    nonce: randomBytes(16).toString("hex"),
  });
}

test("the socket is owner-only and serves one authenticated JSONL frame", async () => {
  await withServer(undefined, async ({ socketPath, secret, host }) => {
    const info = await stat(socketPath);
    assert.equal(info.mode & 0o777, 0o600);
    const raw = await send(socketPath, `${JSON.stringify(signed(secret))}\n`);
    assert.ok(raw.endsWith("\n"));
    const response = JSON.parse(raw);
    assert.equal(response.ok, true);
    assert.equal(response.callRef, REQUEST.callRef);
    assert.deepEqual(JSON.parse(response.result.content[0].text), payload());
    assert.equal(host.calls.length, 1);
  });
});

test("missing, forged, replayed and expired authentication never reach the host", async () => {
  await withServer(undefined, async ({ socketPath, secret, host }) => {
    const unauthenticated = JSON.parse(await send(socketPath, `${JSON.stringify(REQUEST)}\n`));
    assert.equal(unauthenticated.ok, false);

    const forged = signed(secret);
    forged.auth.mac = "0".repeat(64);
    assert.equal(JSON.parse(await send(socketPath, `${JSON.stringify(forged)}\n`)).ok, false);

    const wrongClient = signRequest(REQUEST, {
      secret, clientId: "client:someone-else", timestampMs: Date.now(), nonce: randomBytes(16).toString("hex"),
    });
    assert.equal(JSON.parse(await send(socketPath, `${JSON.stringify(wrongClient)}\n`)).ok, false);

    const expired = signRequest(REQUEST, {
      secret, clientId: CONFIG.clientId, timestampMs: Date.now() - 3_600_000, nonce: randomBytes(16).toString("hex"),
    });
    assert.equal(JSON.parse(await send(socketPath, `${JSON.stringify(expired)}\n`)).ok, false);

    const once = signed(secret);
    assert.equal(JSON.parse(await send(socketPath, `${JSON.stringify(once)}\n`)).ok, true);
    const replay = JSON.parse(await send(socketPath, `${JSON.stringify(once)}\n`));
    assert.equal(replay.ok, false);
    assert.equal(host.calls.length, 1, "only the first authenticated frame may reach the host");
  });
});

test("oversized and malformed frames are refused before authentication", async () => {
  await withServer(undefined, async ({ socketPath, secret, host }) => {
    const huge = JSON.parse(await send(socketPath, `${"x".repeat(CONFIG.maxFrameBytes + 8)}\n`));
    assert.equal(huge.error.code, "FRAME_TOO_LARGE");
    assert.equal(JSON.parse(await send(socketPath, "not json\n")).ok, false);
    const duplicate = `{"schemaVersion":"0.1","schemaVersion":"0.1"}\n`;
    assert.equal(JSON.parse(await send(socketPath, duplicate)).error.code, "DUPLICATE_KEY");
    assert.equal(host.calls.length, 0);
  });
});

test("the authentication key is created owner-only and reused", async () => {
  await withServer(undefined, async ({ stateDir, secret }) => {
    const keyPath = path.join(stateDir, `${CONFIG.socketName}.key`);
    const info = await stat(keyPath);
    assert.equal(info.mode & 0o777, 0o600);
    assert.equal((await readFile(keyPath, "utf8")).trim(), secret);
  });
});

test("the plugin registers one service and logs no query or result content", async () => {
  const definition = createPluginDefinition();
  assert.equal(definition.id, "dalton-openclaw-web-search-broker");
  const services = [];
  const logged = [];
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-search-plugin-"));
  try {
    definition.register({
      runtime: runtime(),
      config: {},
      pluginConfig: { ...CONFIG, socketName: "plugin.sock" },
      registerService: (service) => services.push(service),
    });
    assert.equal(services.length, 1);
    const ctx = { stateDir, logger: { info: (message, fields) => logged.push({ message, fields }) } };
    await services[0].start(ctx);
    await services[0].stop(ctx);
    const text = JSON.stringify(logged);
    assert.ok(!text.includes(REQUEST.query));
    assert.ok(!text.includes("UNTRUSTED synthesis"));
    assert.ok(text.includes("plugin.sock"));
  } finally {
    await rm(stateDir, { recursive: true, force: true });
  }
});

test("package, plugin manifest and response contract identify the same broker version", async () => {
  const packageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
  const pluginManifest = JSON.parse(await readFile(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  assert.equal(packageJson.version, BROKER_VERSION);
  assert.equal(pluginManifest.version, BROKER_VERSION);
});

test("the plugin source holds no Dalton authority, no provider transport and no credential read", async () => {
  const sources = ["broker.mjs", "protocol.mjs", "server.mjs", "auth.mjs", "journal.mjs", "plugin-definition.mjs"];
  for (const name of sources) {
    const text = await readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
    // No Dalton database or authority, and no direct provider transport: the
    // broker only ever calls the host's own webSearch helper.
    for (const banned of ["dalton_core", "core.sqlite", "process.env", "child_process", "node:https", "generativelanguage"]) {
      assert.ok(!text.includes(banned), `${name} must not reference ${banned}`);
    }
  }
  const broker = await readFile(new URL("../src/broker.mjs", import.meta.url), "utf8");
  assert.ok(broker.includes("runtime.webSearch.search"), "the broker must call the host web search helper");
  // The journal refuses to persist credential-bearing fields.
  const journal = await readFile(new URL("../src/journal.mjs", import.meta.url), "utf8");
  for (const guarded of ["apiKey", "credential", "headers", "baseUrl"]) {
    assert.ok(journal.includes(guarded), `the journal must refuse ${guarded} fields`);
  }
});

test("a populated journal reloads across a restart with this broker's keys", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dalton-search-journal-"));
  try {
    const config = { ...CONFIG, socketName: "reload.sock" };
    const first = new WebSearchBroker(runtime(), config, { hostConfig: {} });
    const serverA = new BrokerServer(first);
    await serverA.start(stateDir);
    const { secret } = await loadOrCreateSecret(stateDir, config.socketName);
    const frame = `${JSON.stringify(signRequest(REQUEST, {
      secret, clientId: config.clientId, timestampMs: Date.now(), nonce: randomBytes(16).toString("hex"),
    }))}\n`;
    const socketPath = path.join(stateDir, config.socketName);
    assert.equal(JSON.parse(await send(socketPath, frame)).ok, true);
    await serverA.stop();

    // Restart against the same state directory: the journal written above
    // must load, and the recorded call must replay without a host search.
    const host = runtime();
    const second = new WebSearchBroker(host, config, { hostConfig: {} });
    const serverB = new BrokerServer(second);
    await serverB.start(stateDir);
    try {
      const replay = JSON.parse(await send(socketPath, `${JSON.stringify(signRequest(REQUEST, {
        secret, clientId: config.clientId, timestampMs: Date.now(), nonce: randomBytes(16).toString("hex"),
      }))}\n`));
      assert.equal(replay.ok, true);
      assert.equal(replay.idempotencyStatus, "duplicate");
      assert.equal(host.calls.length, 0, "a reloaded journal must not call the host again");
    } finally {
      await serverB.stop();
    }
  } finally {
    await rm(stateDir, { recursive: true, force: true });
  }
});
