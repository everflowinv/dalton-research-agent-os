import {
  BROKER_VERSION,
  PROTOCOL_VERSION,
  ProtocolError,
  contentHash,
  sealResponse,
  validateRequest,
} from "./protocol.mjs";
import { MemoryIdempotencyJournal } from "./journal.mjs";

const DEFAULTS = Object.freeze({
  maxFrameBytes: 262_144,
  maxOutputBytes: 262_144,
  maxConcurrent: 2,
  idleTimeoutMs: 5_000,
  authMaxSkewMs: 30_000,
  journalTtlMs: 86_400_000,
  journalMaxRecords: 1_000,
  journalMaxBytes: 8_388_608,
});

function integer(value, name, fallback, min, max) {
  const selected = value ?? fallback;
  if (!Number.isSafeInteger(selected) || selected < min || selected > max) {
    throw new ProtocolError("INVALID_CONFIG", `${name} is outside its allowed range`);
  }
  return selected;
}

function exactObject(value, allowed, required, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError("INVALID_CONFIG", `${name} must be an object`);
  }
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  const missing = [...required].filter((key) => !(key in value));
  if (unknown.length || missing.length) {
    throw new ProtocolError("INVALID_CONFIG", `${name} has an invalid shape`);
  }
  return value;
}

function validateConfig(input) {
  const allowed = new Set([
    "dedicatedAgentId",
    "clientId",
    "socketName",
    "profiles",
    "maxFrameBytes",
    "maxOutputBytes",
    "maxConcurrent",
    "idleTimeoutMs",
    "authMaxSkewMs",
    "journalTtlMs",
    "journalMaxRecords",
    "journalMaxBytes",
  ]);
  const config = exactObject(input, allowed, new Set(["dedicatedAgentId", "clientId", "profiles"]), "config");
  if (typeof config.dedicatedAgentId !== "string" || !/^[A-Za-z0-9._-]+$/.test(config.dedicatedAgentId)) {
    throw new ProtocolError("INVALID_CONFIG", "dedicatedAgentId is invalid");
  }
  if (typeof config.clientId !== "string" || !/^client:[A-Za-z0-9._-]+$/.test(config.clientId)) {
    throw new ProtocolError("INVALID_CONFIG", "clientId is invalid");
  }
  const socketName = config.socketName ?? "dalton-model-broker.sock";
  if (typeof socketName !== "string" || !/^[A-Za-z0-9._-]+\.sock$/.test(socketName)) {
    throw new ProtocolError("INVALID_CONFIG", "socketName must be a safe .sock basename");
  }
  if (!Array.isArray(config.profiles) || config.profiles.length === 0) {
    throw new ProtocolError("INVALID_CONFIG", "profiles must be a non-empty array");
  }
  const profiles = new Map();
  for (const raw of config.profiles) {
    const profile = exactObject(
      raw,
      new Set(["id", "model", "maxTokens", "timeoutMs"]),
      new Set(["id", "model", "maxTokens", "timeoutMs"]),
      "profile",
    );
    if (typeof profile.id !== "string" || !/^profile:[A-Za-z0-9._-]+$/.test(profile.id)) {
      throw new ProtocolError("INVALID_CONFIG", "profile.id is invalid");
    }
    if (profiles.has(profile.id)) throw new ProtocolError("INVALID_CONFIG", "profile ids must be unique");
    if (typeof profile.model !== "string" || !/^[A-Za-z0-9._-]+\/[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(profile.model)) {
      throw new ProtocolError("INVALID_CONFIG", "profile.model must be an exact provider/model ref");
    }
    profiles.set(profile.id, Object.freeze({
      id: profile.id,
      model: profile.model,
      maxTokens: integer(profile.maxTokens, "profile.maxTokens", undefined, 1, 1_000_000),
      timeoutMs: integer(profile.timeoutMs, "profile.timeoutMs", undefined, 1, 600_000),
    }));
  }
  return Object.freeze({
    dedicatedAgentId: config.dedicatedAgentId,
    clientId: config.clientId,
    socketName,
    profiles,
    maxFrameBytes: integer(config.maxFrameBytes, "maxFrameBytes", DEFAULTS.maxFrameBytes, 1024, 1_048_576),
    maxOutputBytes: integer(config.maxOutputBytes, "maxOutputBytes", DEFAULTS.maxOutputBytes, 1, 1_048_576),
    maxConcurrent: integer(config.maxConcurrent, "maxConcurrent", DEFAULTS.maxConcurrent, 1, 32),
    idleTimeoutMs: integer(config.idleTimeoutMs, "idleTimeoutMs", DEFAULTS.idleTimeoutMs, 100, 60_000),
    authMaxSkewMs: integer(config.authMaxSkewMs, "authMaxSkewMs", DEFAULTS.authMaxSkewMs, 1_000, 300_000),
    journalTtlMs: integer(config.journalTtlMs, "journalTtlMs", DEFAULTS.journalTtlMs, 60_000, 604_800_000),
    journalMaxRecords: integer(config.journalMaxRecords, "journalMaxRecords", DEFAULTS.journalMaxRecords, 1, 10_000),
    journalMaxBytes: integer(config.journalMaxBytes, "journalMaxBytes", DEFAULTS.journalMaxBytes, 4_096, 67_108_864),
  });
}

function nullableUsageNumber(value, name) {
  if (value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new ProtocolError("INVALID_HOST_RESULT", `host returned invalid ${name}`);
  }
  return value;
}

function canonicalActualModel(provider, model) {
  const prefix = `${provider}/`;
  return `${provider}/${model.startsWith(prefix) ? model.slice(prefix.length) : model}`;
}

export class ModelBroker {
  constructor(runtime, rawConfig, { journal } = {}) {
    if (!runtime?.llm || typeof runtime.llm.complete !== "function") {
      throw new ProtocolError("INVALID_RUNTIME", "runtime.llm.complete is required");
    }
    this.runtime = runtime;
    this.config = validateConfig(rawConfig);
    this.journal = journal ?? new MemoryIdempotencyJournal({ ttlMs: this.config.journalTtlMs });
    this.active = 0;
    this.reserved = 0;
    this.inFlight = new Map();
  }

  get limits() {
    return {
      maxFrameBytes: this.config.maxFrameBytes,
      maxOutputBytes: this.config.maxOutputBytes,
      maxConcurrent: this.config.maxConcurrent,
      idleTimeoutMs: this.config.idleTimeoutMs,
    };
  }

  async handle(input) {
    const request = validateRequest(input, this.config.maxFrameBytes);
    // replayOnly is an authenticated transport instruction, not part of the
    // provider request identity.  It can read a durable journal result but
    // can never create a journal claim or call the host model on a miss.
    const { replayOnly = false, ...executionRequest } = request;
    const requestHash = contentHash(executionRequest);
    const live = this.inFlight.get(request.invocationId);
    if (live) {
      if (live.requestHash !== requestHash) return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "invocationId was already used for another request");
      const original = await live.promise;
      return this.#duplicate(original);
    }
    const persisted = this.journal.get(request.invocationId);
    if (persisted) {
      if (persisted.requestHash !== requestHash) return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "invocationId was already used for another request");
      if (persisted.state === "completed") return this.#duplicate(persisted.response);
      return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "prior host completion may have run; automatic replay is blocked");
    }
    if (replayOnly) {
      return this.#failure(request, requestHash, "fresh", "IDEMPOTENCY_MISS", "no durable completion exists; replay-only request did not call the host");
    }
    if (this.active + this.reserved >= this.config.maxConcurrent) {
      return this.#failure(request, requestHash, "fresh", "BUSY", "broker concurrency limit reached");
    }
    this.reserved += 1;
    let claim;
    try {
      claim = await this.journal.claim(request.invocationId, requestHash);
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "idempotency journal is unavailable");
    } finally {
      this.reserved -= 1;
    }
    if (claim.status === "conflict") return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "invocationId was already used for another request");
    if (claim.status === "completed") return this.#duplicate(claim.record.response);
    if (claim.status === "pending") return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "prior host completion may have run; automatic replay is blocked");
    const promise = this.#completeAndPersist(request, requestHash);
    this.inFlight.set(request.invocationId, { requestHash, promise });
    promise.finally(() => this.inFlight.delete(request.invocationId)).catch(() => {});
    return promise;
  }

  async #completeAndPersist(request, requestHash) {
    const response = await this.#complete(request, requestHash);
    try {
      await this.journal.complete(request.invocationId, requestHash, response);
      return response;
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "completion result could not be committed to the idempotency journal");
    }
  }

  #duplicate(original) {
    const { contentHash: _hash, ...body } = original;
    return sealResponse({ ...body, idempotencyStatus: "duplicate" });
  }

  async #complete(request, requestHash) {
    const profile = this.config.profiles.get(request.profileId);
    if (!profile || request.model !== profile.model) {
      return this.#failure(request, requestHash, "fresh", "MODEL_NOT_ALLOWED", "profile and exact model are not allowed");
    }
    if (request.maxTokens > profile.maxTokens || request.timeoutMs > profile.timeoutMs) {
      return this.#failure(request, requestHash, "fresh", "PROFILE_LIMIT_EXCEEDED", "request exceeds profile limits");
    }
    this.active += 1;
    const controller = new AbortController();
    let timer;
    try {
      const completion = Promise.resolve(this.runtime.llm.complete({
        messages: [{ role: "user", content: request.prompt }],
        purpose: "dalton.model-broker.complete",
        model: request.model,
        maxTokens: request.maxTokens,
        signal: controller.signal,
        agentId: this.config.dedicatedAgentId,
      }));
      completion.catch(() => {});
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new ProtocolError("TIMEOUT", "host completion exceeded request timeout"));
        }, request.timeoutMs);
      });
      const result = await Promise.race([completion, timeout]);
      return this.#success(request, requestHash, result);
    } catch (error) {
      const code = error instanceof ProtocolError ? error.code : "HOST_COMPLETION_FAILED";
      const message = error instanceof ProtocolError ? error.message : "host completion failed";
      return this.#failure(request, requestHash, "fresh", code, message);
    } finally {
      if (timer) clearTimeout(timer);
      this.active -= 1;
    }
  }

  #success(request, requestHash, result) {
    if (!result || typeof result !== "object") {
      throw new ProtocolError("INVALID_HOST_RESULT", "host returned an invalid result");
    }
    for (const field of ["text", "provider", "model", "agentId"]) {
      if (typeof result[field] !== "string" || result[field].length === 0) {
        throw new ProtocolError("INVALID_HOST_RESULT", `host returned invalid ${field}`);
      }
    }
    if (Buffer.byteLength(result.text, "utf8") > this.config.maxOutputBytes) {
      throw new ProtocolError("OUTPUT_TOO_LARGE", "host output exceeds the configured byte limit");
    }
    if (canonicalActualModel(result.provider, result.model) !== request.model || result.agentId !== this.config.dedicatedAgentId) {
      throw new ProtocolError("HOST_ATTRIBUTION_MISMATCH", "host attribution differs from the requested route");
    }
    const usage = result.usage ?? {};
    const normalizedUsage = {
      inputTokens: nullableUsageNumber(usage.inputTokens, "usage.inputTokens"),
      outputTokens: nullableUsageNumber(usage.outputTokens, "usage.outputTokens"),
      cacheReadTokens: nullableUsageNumber(usage.cacheReadTokens, "usage.cacheReadTokens"),
      cacheWriteTokens: nullableUsageNumber(usage.cacheWriteTokens, "usage.cacheWriteTokens"),
      totalTokens: nullableUsageNumber(usage.totalTokens, "usage.totalTokens"),
    };
    const costUsd = nullableUsageNumber(usage.costUsd, "usage.costUsd");
    return sealResponse({
      schemaVersion: PROTOCOL_VERSION,
      brokerVersion: BROKER_VERSION,
      runtimeVersion: typeof this.runtime.version === "string" ? this.runtime.version : "unknown",
      invocationId: request.invocationId,
      workOrderId: request.workOrderId,
      profileId: request.profileId,
      requestHash,
      idempotencyStatus: "fresh",
      ok: true,
      provider: result.provider,
      model: result.model,
      canonicalModel: canonicalActualModel(result.provider, result.model),
      agentId: result.agentId,
      text: result.text,
      usage: normalizedUsage,
      cost: { available: costUsd !== null, usd: costUsd },
      error: null,
    });
  }

  #failure(request, requestHash, idempotencyStatus, code, message) {
    return sealResponse({
      schemaVersion: PROTOCOL_VERSION,
      brokerVersion: BROKER_VERSION,
      runtimeVersion: typeof this.runtime.version === "string" ? this.runtime.version : "unknown",
      invocationId: request.invocationId,
      workOrderId: request.workOrderId,
      profileId: request.profileId,
      requestHash,
      idempotencyStatus,
      ok: false,
      provider: null,
      model: null,
      canonicalModel: null,
      agentId: null,
      text: null,
      usage: { inputTokens: null, outputTokens: null, cacheReadTokens: null, cacheWriteTokens: null, totalTokens: null },
      cost: { available: false, usd: null },
      error: { code, message },
    });
  }
}
