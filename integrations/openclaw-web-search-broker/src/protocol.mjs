import { createHash } from "node:crypto";

export const PROTOCOL_VERSION = "0.1";
export const BROKER_VERSION = "0.1.0-spike.1";
export const TOOL_NAME = "web_search";

const REQUIRED_REQUEST_KEYS = new Set([
  "schemaVersion",
  "callRef",
  "profileId",
  "query",
  "count",
  "timeoutMs",
]);
const OPTIONAL_REQUEST_KEYS = new Set(["dateAfter", "dateBefore", "replayOnly"]);
const REQUEST_KEYS = new Set([...REQUIRED_REQUEST_KEYS, ...OPTIONAL_REQUEST_KEYS]);
const DATE = /^\d{4}-\d{2}-\d{2}$/;

export class ProtocolError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
  }
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

export function canonicalJson(value) {
  return JSON.stringify(canonicalize(value));
}

export function strictJsonParse(text) {
  if (typeof text !== "string") throw new ProtocolError("INVALID_JSON", "JSON frame must be text");
  let index = 0;
  const whitespace = () => { while (/[ \t\r\n]/.test(text[index] ?? "")) index += 1; };
  const fail = () => { throw new ProtocolError("INVALID_JSON", "request is not strict valid JSON"); };
  const parseString = () => {
    if (text[index] !== '"') fail();
    const start = index++;
    while (index < text.length) {
      const char = text[index++];
      if (char === '"') {
        try { return JSON.parse(text.slice(start, index)); } catch { fail(); }
      }
      if (char === "\\") {
        if (index >= text.length) fail();
        const escaped = text[index++];
        if (escaped === "u") {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(index, index + 4))) fail();
          index += 4;
        } else if (!'"\\/bfnrt'.includes(escaped)) fail();
      } else if (char.charCodeAt(0) < 0x20) fail();
    }
    fail();
  };
  const parseValue = () => {
    whitespace();
    const char = text[index];
    if (char === '"') return parseString();
    if (char === "{") {
      index += 1;
      whitespace();
      const result = Object.create(null);
      const keys = new Set();
      if (text[index] === "}") { index += 1; return result; }
      while (index < text.length) {
        whitespace();
        const key = parseString();
        if (keys.has(key)) throw new ProtocolError("DUPLICATE_KEY", "JSON objects must not contain duplicate keys");
        keys.add(key);
        whitespace();
        if (text[index++] !== ":") fail();
        result[key] = parseValue();
        whitespace();
        const separator = text[index++];
        if (separator === "}") return result;
        if (separator !== ",") fail();
      }
      fail();
    }
    if (char === "[") {
      index += 1;
      whitespace();
      const result = [];
      if (text[index] === "]") { index += 1; return result; }
      while (index < text.length) {
        result.push(parseValue());
        whitespace();
        const separator = text[index++];
        if (separator === "]") return result;
        if (separator !== ",") fail();
      }
      fail();
    }
    for (const [literal, value] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(literal, index)) { index += literal.length; return value; }
    }
    const match = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!match) fail();
    index += match[0].length;
    const number = Number(match[0]);
    if (!Number.isFinite(number)) fail();
    return number;
  };
  const result = parseValue();
  whitespace();
  if (index !== text.length) fail();
  return result;
}

export function contentHash(value) {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function requiredString(value, field, pattern, maxLength) {
  if (
    typeof value !== "string"
    || value.length === 0
    || (pattern && !pattern.test(value))
    || (maxLength && value.length > maxLength)
  ) {
    throw new ProtocolError("INVALID_REQUEST", `${field} is invalid`);
  }
  return value;
}

function positiveInteger(value, field) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new ProtocolError("INVALID_REQUEST", `${field} must be a positive integer`);
  }
  return value;
}

/**
 * Validate one search request frame.
 *
 * The caller supplies the exact query the host will run; the broker never
 * rewrites, expands or re-ranks it.  ``callRef`` is Dalton's credential-use
 * ref for one physical attempt, so it is a precise idempotency key: a retry
 * on a new attempt is a new key, which is correct because it is a new paid
 * call.
 */
export function validateRequest(input, { maxFrameBytes, maxQueryChars, maxCount }) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ProtocolError("INVALID_REQUEST", "request must be an object");
  }
  const unknown = Object.keys(input).filter((key) => !REQUEST_KEYS.has(key));
  const missing = [...REQUIRED_REQUEST_KEYS].filter((key) => !(key in input));
  if (unknown.length > 0) {
    throw new ProtocolError("UNKNOWN_FIELD", `request contains unknown fields: ${unknown.sort().join(",")}`);
  }
  if (missing.length > 0) {
    throw new ProtocolError("MISSING_FIELD", `request is missing fields: ${missing.sort().join(",")}`);
  }
  if (input.schemaVersion !== PROTOCOL_VERSION) {
    throw new ProtocolError("UNSUPPORTED_VERSION", "schemaVersion is not supported");
  }
  if ("replayOnly" in input && typeof input.replayOnly !== "boolean") {
    throw new ProtocolError("INVALID_REQUEST", "replayOnly must be boolean");
  }
  const count = positiveInteger(input.count, "count");
  if (count > maxCount) {
    throw new ProtocolError("INVALID_REQUEST", "count exceeds the configured maximum");
  }
  const hasAfter = "dateAfter" in input;
  const hasBefore = "dateBefore" in input;
  if (hasAfter !== hasBefore) {
    throw new ProtocolError("INVALID_REQUEST", "dateAfter and dateBefore must be supplied together");
  }
  const request = Object.freeze({
    schemaVersion: PROTOCOL_VERSION,
    callRef: requiredString(input.callRef, "callRef", /^credential-use:[A-Za-z0-9._:-]+$/, 200),
    profileId: requiredString(input.profileId, "profileId", /^profile:[A-Za-z0-9._-]+$/, 200),
    query: requiredString(input.query, "query", undefined, maxQueryChars),
    count,
    timeoutMs: positiveInteger(input.timeoutMs, "timeoutMs"),
    ...(hasAfter
      ? {
        dateAfter: requiredString(input.dateAfter, "dateAfter", DATE),
        dateBefore: requiredString(input.dateBefore, "dateBefore", DATE),
      }
      : {}),
    ...(input.replayOnly === true ? { replayOnly: true } : {}),
  });
  if (request.dateAfter && request.dateAfter > request.dateBefore) {
    throw new ProtocolError("INVALID_REQUEST", "dateAfter must not be later than dateBefore");
  }
  if (Buffer.byteLength(canonicalJson(request), "utf8") > maxFrameBytes) {
    throw new ProtocolError("FRAME_TOO_LARGE", "request exceeds the configured frame limit");
  }
  return request;
}

export function sealResponse(responseWithoutHash) {
  const hash = contentHash(responseWithoutHash);
  return Object.freeze({ ...responseWithoutHash, contentHash: hash });
}

export function protocolFailure(code, message, runtimeVersion = "unknown") {
  return sealResponse({
    schemaVersion: PROTOCOL_VERSION,
    brokerVersion: BROKER_VERSION,
    runtimeVersion,
    ok: false,
    error: { code, message },
  });
}

/**
 * Wrap one provider payload in the tool-result envelope Dalton's frozen
 * public-web adapter already parses, so the stored raw artifact stays
 * self-describing without a second parsing contract.
 */
export function toolResultEnvelope(payload) {
  return { content: [{ type: "text", text: canonicalJson(payload) }] };
}
