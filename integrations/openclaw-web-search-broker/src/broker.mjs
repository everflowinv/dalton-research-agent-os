import {
  BROKER_VERSION,
  PROTOCOL_VERSION,
  ProtocolError,
  contentHash,
  sealResponse,
  toolResultEnvelope,
  validateRequest,
} from "./protocol.mjs";
import { MemoryIdempotencyJournal } from "./journal.mjs";

const CLIENT_ID = /^client:[A-Za-z0-9._-]+$/;
const PROFILE_ID = /^profile:[A-Za-z0-9._-]+$/;
const SOCKET_NAME = /^[A-Za-z0-9._-]+\.sock$/;
const PROVIDER_ID = /^[a-z][a-z0-9-]{0,63}$/;

/** The host helper's documented return: an object wrapping a provider payload. */
function envelopeShape(value) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).length === 2
    && typeof value.provider === "string"
    && value.result
    && typeof value.result === "object"
    && !Array.isArray(value.result),
  );
}

function integer(value, field, { min, max, fallback }) {
  const resolved = value === undefined ? fallback : value;
  if (!Number.isSafeInteger(resolved) || resolved < min || resolved > max) {
    throw new ProtocolError("INVALID_CONFIG", `${field} is invalid`);
  }
  return resolved;
}

/**
 * Host-owned web search bridge for an external Dalton runtime.
 *
 * The broker owns provider selection and credentials: a client sends one
 * exact query and never an API key, provider, model, endpoint or header.  It
 * calls the shared ``api.runtime.webSearch.search`` helper, checks the
 * provider the host actually used against the configured expectation, and
 * returns the provider payload verbatim inside a tool-result envelope.
 * Nothing about the query or the results is logged.
 *
 * The helper returns ``{ provider, result }`` (verified against a real host
 * call on 2026-09-06); the broker unwraps it and forwards the inner provider
 * payload, so the client sees exactly one documented shape. Both the outer
 * and inner provider must equal the configured one.
 */
export class WebSearchBroker {
  constructor(runtime, rawConfig, { journal, hostConfig } = {}) {
    if (!runtime || typeof runtime !== "object") {
      throw new ProtocolError("INVALID_CONFIG", "plugin runtime is required");
    }
    if (typeof runtime.webSearch?.search !== "function") {
      throw new ProtocolError("INVALID_CONFIG", "host runtime does not expose webSearch.search");
    }
    const config = rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig) ? rawConfig : {};
    const clientId = config.clientId;
    if (typeof clientId !== "string" || !CLIENT_ID.test(clientId)) {
      throw new ProtocolError("INVALID_CONFIG", "clientId is invalid");
    }
    const profileId = config.profileId ?? "profile:web-search";
    if (typeof profileId !== "string" || !PROFILE_ID.test(profileId)) {
      throw new ProtocolError("INVALID_CONFIG", "profileId is invalid");
    }
    const expectedProvider = config.expectedProvider;
    if (typeof expectedProvider !== "string" || !PROVIDER_ID.test(expectedProvider)) {
      throw new ProtocolError("INVALID_CONFIG", "expectedProvider is invalid");
    }
    const socketName = config.socketName ?? "dalton-web-search-broker.sock";
    if (typeof socketName !== "string" || !SOCKET_NAME.test(socketName)) {
      throw new ProtocolError("INVALID_CONFIG", "socketName is invalid");
    }
    this.runtime = runtime;
    this.hostConfig = hostConfig ?? runtime.config ?? undefined;
    this.config = Object.freeze({
      clientId,
      profileId,
      expectedProvider,
      socketName,
      maxQueryChars: integer(config.maxQueryChars, "maxQueryChars", { min: 1, max: 4000, fallback: 400 }),
      maxCount: integer(config.maxCount, "maxCount", { min: 1, max: 50, fallback: 10 }),
      maxFrameBytes: integer(config.maxFrameBytes, "maxFrameBytes", { min: 1024, max: 1048576, fallback: 65536 }),
      maxResponseBytes: integer(config.maxResponseBytes, "maxResponseBytes", { min: 1024, max: 1048576, fallback: 262144 }),
      maxConcurrent: integer(config.maxConcurrent, "maxConcurrent", { min: 1, max: 8, fallback: 1 }),
      idleTimeoutMs: integer(config.idleTimeoutMs, "idleTimeoutMs", { min: 100, max: 60000, fallback: 5000 }),
      maxTimeoutMs: integer(config.maxTimeoutMs, "maxTimeoutMs", { min: 1000, max: 600000, fallback: 240000 }),
      authMaxSkewMs: integer(config.authMaxSkewMs, "authMaxSkewMs", { min: 1000, max: 300000, fallback: 30000 }),
      journalTtlMs: integer(config.journalTtlMs, "journalTtlMs", { min: 60000, max: 604800000, fallback: 86400000 }),
      journalMaxRecords: integer(config.journalMaxRecords, "journalMaxRecords", { min: 1, max: 10000, fallback: 1000 }),
      journalMaxBytes: integer(config.journalMaxBytes, "journalMaxBytes", { min: 4096, max: 67108864, fallback: 8388608 }),
    });
    this.journal = journal ?? new MemoryIdempotencyJournal({
      ttlMs: this.config.journalTtlMs,
      maxRecords: this.config.journalMaxRecords,
      maxBytes: this.config.journalMaxBytes,
    });
    this.inFlight = new Map();
    this.active = 0;
    this.reserved = 0;
  }

  get limits() {
    return Object.freeze({
      maxFrameBytes: this.config.maxFrameBytes,
      idleTimeoutMs: this.config.idleTimeoutMs,
    });
  }

  async handle(input) {
    const request = validateRequest(input, {
      maxFrameBytes: this.config.maxFrameBytes,
      maxQueryChars: this.config.maxQueryChars,
      maxCount: this.config.maxCount,
    });
    // replayOnly is an authenticated transport instruction, not part of the
    // search identity: it may read a durable result but never create a claim
    // or reach the host on a miss.
    const { replayOnly = false, ...executionRequest } = request;
    const requestHash = contentHash(executionRequest);
    if (request.profileId !== this.config.profileId) {
      return this.#failure(request, requestHash, "fresh", "UNKNOWN_PROFILE", "profileId is not the configured search profile");
    }
    if (request.timeoutMs > this.config.maxTimeoutMs) {
      return this.#failure(request, requestHash, "fresh", "PROFILE_LIMIT_EXCEEDED", "timeoutMs exceeds the configured maximum");
    }
    const live = this.inFlight.get(request.callRef);
    if (live) {
      if (live.requestHash !== requestHash) {
        return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request");
      }
      return this.#duplicate(await live.promise);
    }
    const persisted = this.journal.get(request.callRef);
    if (persisted) {
      if (persisted.requestHash !== requestHash) {
        return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request");
      }
      if (persisted.state === "completed") return this.#duplicate(persisted.response);
      return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "a prior host search may have run; automatic replay is blocked");
    }
    if (replayOnly) {
      return this.#failure(request, requestHash, "fresh", "IDEMPOTENCY_MISS", "no durable search exists; replay-only request did not call the host");
    }
    if (this.active + this.reserved >= this.config.maxConcurrent) {
      return this.#failure(request, requestHash, "fresh", "BUSY", "broker concurrency limit reached");
    }
    this.reserved += 1;
    let claim;
    try {
      claim = await this.journal.claim(request.callRef, requestHash);
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "idempotency journal is unavailable");
    } finally {
      this.reserved -= 1;
    }
    if (claim.status === "conflict") {
      return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request");
    }
    if (claim.status === "completed") return this.#duplicate(claim.record.response);
    if (claim.status === "pending") {
      return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "a prior host search may have run; automatic replay is blocked");
    }
    const promise = this.#searchAndPersist(request, requestHash);
    this.inFlight.set(request.callRef, { requestHash, promise });
    promise.finally(() => this.inFlight.delete(request.callRef)).catch(() => {});
    return promise;
  }

  async #searchAndPersist(request, requestHash) {
    const response = await this.#search(request, requestHash);
    try {
      await this.journal.complete(request.callRef, requestHash, response);
      return response;
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "search result could not be committed to the idempotency journal");
    }
  }

  async #search(request, requestHash) {
    this.active += 1;
    try {
      const args = {
        query: request.query,
        count: request.count,
        ...(request.dateAfter ? { date_after: request.dateAfter, date_before: request.dateBefore } : {}),
      };
      let payload;
      try {
        payload = await this.runtime.webSearch.search({ config: this.hostConfig, args });
      } catch (error) {
        const message = typeof error?.message === "string" ? error.message : "host web search failed";
        // Normalize punctuation so "GEMINI_API_KEY" and "api key" classify alike.
        const lowered = message.toLowerCase().replace(/[^a-z0-9]+/g, " ");
        const denied = ["api key", "unauthor", "permission", "forbidden", "credential"].some((token) => lowered.includes(token));
        const limited = ["rate limit", "quota", "429", "too many requests"].some((token) => lowered.includes(token));
        return this.#failure(
          request,
          requestHash,
          "fresh",
          denied ? "PROVIDER_PERMISSION_DENIED" : limited ? "PROVIDER_RATE_LIMITED" : "PROVIDER_ERROR",
          // The host message can quote provider text; keep it bounded and
          // never include the query or any result content.
          message.slice(0, 300),
        );
      }
      if (!envelopeShape(payload)) {
        return this.#failure(request, requestHash, "fresh", "PROVIDER_CONTRACT_DRIFT", "host web search did not return a { provider, result } object");
      }
      const inner = payload.result;
      if (payload.provider !== this.config.expectedProvider || inner.provider !== this.config.expectedProvider) {
        // A silent provider swap would change the payload contract Dalton
        // pinned; refuse instead of handing over a different shape.
        return this.#failure(
          request,
          requestHash,
          "fresh",
          "PROVIDER_CONTRACT_DRIFT",
          "host web search used a provider other than the configured one",
        );
      }
      const envelope = toolResultEnvelope(inner);
      const body = {
        schemaVersion: PROTOCOL_VERSION,
        brokerVersion: BROKER_VERSION,
        runtimeVersion: this.runtime.version ?? "unknown",
        ok: true,
        idempotencyStatus: "fresh",
        callRef: request.callRef,
        requestHash,
        providerRequestId: `provider-request:web-search-broker:${requestHash.slice(0, 32)}`,
        result: envelope,
      };
      const sealed = sealResponse(body);
      if (Buffer.byteLength(JSON.stringify(sealed), "utf8") > this.config.maxResponseBytes) {
        return this.#failure(request, requestHash, "fresh", "RESPONSE_TOO_LARGE", "host web search result exceeds the configured response limit");
      }
      return sealed;
    } finally {
      this.active -= 1;
    }
  }

  #duplicate(original) {
    const { contentHash: _hash, ...body } = original;
    return sealResponse({ ...body, idempotencyStatus: "duplicate" });
  }

  #failure(request, requestHash, idempotencyStatus, code, message) {
    return sealResponse({
      schemaVersion: PROTOCOL_VERSION,
      brokerVersion: BROKER_VERSION,
      runtimeVersion: this.runtime.version ?? "unknown",
      ok: false,
      idempotencyStatus,
      callRef: request.callRef,
      requestHash,
      error: { code, message },
    });
  }
}
