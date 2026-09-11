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

function selectedProviderFromResponse(response) {
  if (typeof response?.provider === "string" && PROVIDER_ID.test(response.provider)) {
    return response.provider;
  }
  try {
    const text = response?.result?.content?.[0]?.text;
    const payload = typeof text === "string" ? JSON.parse(text) : undefined;
    return typeof payload?.provider === "string" && PROVIDER_ID.test(payload.provider)
      ? payload.provider
      : undefined;
  } catch {
    return undefined;
  }
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
    this.hostConfig = hostConfig ?? (
      typeof runtime.config?.current === "function" ? undefined : runtime.config
    );
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
    let providerContext;
    try {
      providerContext = this.#providerContext();
    } catch (error) {
      const message = error instanceof ProtocolError
        ? error.message
        : "host web search provider configuration is unavailable";
      return this.#failure(
        request, contentHash(executionRequest), "fresh",
        "PROVIDER_CONTRACT_DRIFT", message,
      );
    }
    const { provider } = providerContext;
    // New clients send expectedProvider, so it is naturally part of their
    // signed identity. For legacy clients the broker adds the selected host
    // provider to its journal identity, preventing cross-provider replay.
    const identityRequest = request.expectedProvider === undefined
      ? { ...executionRequest, expectedProvider: provider }
      : executionRequest;
    const requestHash = contentHash(identityRequest);
    const legacyRequestHash = request.expectedProvider === undefined
      ? contentHash(executionRequest)
      : undefined;
    if (request.profileId !== this.config.profileId) {
      return this.#failure(request, requestHash, "fresh", "UNKNOWN_PROFILE", "profileId is not the configured search profile", provider);
    }
    if (request.timeoutMs > this.config.maxTimeoutMs) {
      return this.#failure(request, requestHash, "fresh", "PROFILE_LIMIT_EXCEEDED", "timeoutMs exceeds the configured maximum", provider);
    }
    if (request.expectedProvider !== undefined && request.expectedProvider !== provider) {
      return this.#failure(request, requestHash, "fresh", "PROVIDER_CONTRACT_DRIFT", "request provider guard differs from the active host provider", provider);
    }
    const live = this.inFlight.get(request.callRef);
    if (live) {
      if (live.requestHash !== requestHash || live.provider !== provider) {
        return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request", provider);
      }
      return this.#duplicate(await live.promise);
    }
    const persisted = this.journal.get(request.callRef);
    if (persisted) {
      const historicalMatch = legacyRequestHash !== undefined
        && persisted.requestHash === legacyRequestHash
        && persisted.state === "completed"
        && selectedProviderFromResponse(persisted.response) === provider;
      if (persisted.requestHash !== requestHash && !historicalMatch) {
        return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request", provider);
      }
      if (persisted.state === "completed") return this.#duplicate(persisted.response);
      return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "a prior host search may have run; automatic replay is blocked", provider);
    }
    if (replayOnly) {
      return this.#failure(request, requestHash, "fresh", "IDEMPOTENCY_MISS", "no durable search exists; replay-only request did not call the host", provider);
    }
    if (this.active + this.reserved >= this.config.maxConcurrent) {
      return this.#failure(request, requestHash, "fresh", "BUSY", "broker concurrency limit reached", provider);
    }
    this.reserved += 1;
    let claim;
    try {
      claim = await this.journal.claim(request.callRef, requestHash);
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "idempotency journal is unavailable", provider);
    } finally {
      this.reserved -= 1;
    }
    if (claim.status === "conflict") {
      return this.#failure(request, requestHash, "conflict", "IDEMPOTENCY_CONFLICT", "callRef was already used for another request", provider);
    }
    if (claim.status === "completed") return this.#duplicate(claim.record.response);
    if (claim.status === "pending") {
      return this.#failure(request, requestHash, "duplicate", "IDEMPOTENCY_INDETERMINATE", "a prior host search may have run; automatic replay is blocked", provider);
    }
    const promise = this.#searchAndPersist(request, requestHash, providerContext);
    this.inFlight.set(request.callRef, { requestHash, provider, promise });
    promise.finally(() => this.inFlight.delete(request.callRef)).catch(() => {});
    return promise;
  }

  #providerContext() {
    const current = this.runtime.config?.current;
    const hostConfig = typeof current === "function" ? current() : this.hostConfig;
    if (!hostConfig || typeof hostConfig !== "object" || Array.isArray(hostConfig)) {
      throw new ProtocolError("INVALID_CONFIG", "host runtime configuration is unavailable");
    }
    const configured = hostConfig.tools?.web?.search?.provider;
    const provider = configured === undefined ? this.config.expectedProvider : configured;
    if (typeof provider !== "string" || !PROVIDER_ID.test(provider)) {
      throw new ProtocolError("INVALID_CONFIG", "host web search provider is invalid");
    }
    if (typeof this.runtime.webSearch.listProviders === "function") {
      const available = this.runtime.webSearch.listProviders({ config: hostConfig });
      if (!Array.isArray(available) || !available.some((item) => item?.id === provider)) {
        throw new ProtocolError("INVALID_CONFIG", "host web search provider is unsupported");
      }
    }
    return Object.freeze({ hostConfig, provider });
  }

  async #searchAndPersist(request, requestHash, providerContext) {
    const response = await this.#search(request, requestHash, providerContext);
    try {
      await this.journal.complete(request.callRef, requestHash, response);
      return response;
    } catch {
      return this.#failure(request, requestHash, "fresh", "JOURNAL_UNAVAILABLE", "search result could not be committed to the idempotency journal", providerContext.provider);
    }
  }

  async #search(request, requestHash, providerContext) {
    this.active += 1;
    try {
      const args = {
        query: request.query,
        count: request.count,
        ...(request.dateAfter ? { date_after: request.dateAfter, date_before: request.dateBefore } : {}),
      };
      let payload;
      try {
        payload = await this.runtime.webSearch.search({
          config: providerContext.hostConfig,
          preferInputConfig: true,
          providerId: providerContext.provider,
          args,
        });
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
          providerContext.provider,
        );
      }
      if (!envelopeShape(payload)) {
        return this.#failure(request, requestHash, "fresh", "PROVIDER_CONTRACT_DRIFT", "host web search did not return a { provider, result } object", providerContext.provider);
      }
      const inner = payload.result;
      if (payload.provider !== providerContext.provider || inner.provider !== providerContext.provider) {
        // A silent provider swap would change the payload contract Dalton
        // pinned; refuse instead of handing over a different shape.
        return this.#failure(
          request,
          requestHash,
          "fresh",
          "PROVIDER_CONTRACT_DRIFT",
          "host web search used a provider other than the configured one",
          providerContext.provider,
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
        provider: providerContext.provider,
        providerRequestId: `provider-request:web-search-broker:${requestHash.slice(0, 32)}`,
        result: envelope,
      };
      const sealed = sealResponse(body);
      if (Buffer.byteLength(JSON.stringify(sealed), "utf8") > this.config.maxResponseBytes) {
        return this.#failure(request, requestHash, "fresh", "RESPONSE_TOO_LARGE", "host web search result exceeds the configured response limit", providerContext.provider);
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

  #failure(request, requestHash, idempotencyStatus, code, message, provider) {
    return sealResponse({
      schemaVersion: PROTOCOL_VERSION,
      brokerVersion: BROKER_VERSION,
      runtimeVersion: this.runtime.version ?? "unknown",
      ok: false,
      idempotencyStatus,
      callRef: request.callRef,
      requestHash,
      ...(provider ? { provider } : {}),
      error: { code, message },
    });
  }
}
