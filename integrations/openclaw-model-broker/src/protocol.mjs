import { createHash } from "node:crypto";

export const PROTOCOL_VERSION = "0.1";
export const BROKER_VERSION = "0.1.0-spike.2";

const REQUIRED_REQUEST_KEYS = new Set([
  "schemaVersion",
  "invocationId",
  "workOrderId",
  "profileId",
  "model",
  "prompt",
  "maxTokens",
  "timeoutMs",
]);
const OPTIONAL_REQUEST_KEYS = new Set(["replayOnly", "requiredControls"]);
const REQUEST_KEYS = new Set([...REQUIRED_REQUEST_KEYS, ...OPTIONAL_REQUEST_KEYS]);
const REQUIRED_CONTROL_KEYS = new Set([
  "maxInputTokens",
  "maxOutputTokens",
  "maxTotalTokens",
  "maxCostUsd",
  "structuredOutput",
]);
const STRUCTURED_OUTPUT_KEYS = new Set(["schemaName", "schemaHash", "jsonSchema"]);

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

function requiredString(value, field, pattern) {
  if (typeof value !== "string" || value.length === 0 || (pattern && !pattern.test(value))) {
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

function positiveNumber(value, field) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new ProtocolError("INVALID_REQUEST", `${field} must be a positive finite number`);
  }
  return value;
}

function exactRequestObject(value, keys, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError("INVALID_REQUEST", `${field} must be an object`);
  }
  const actual = Object.keys(value);
  if (actual.length !== keys.size || actual.some((key) => !keys.has(key))) {
    throw new ProtocolError("INVALID_REQUEST", `${field} has an invalid shape`);
  }
  return value;
}

function validateRequiredControls(value, requestMaxTokens) {
  const raw = exactRequestObject(value, REQUIRED_CONTROL_KEYS, "requiredControls");
  const structured = exactRequestObject(
    raw.structuredOutput,
    STRUCTURED_OUTPUT_KEYS,
    "requiredControls.structuredOutput",
  );
  if (!structured.jsonSchema || typeof structured.jsonSchema !== "object" || Array.isArray(structured.jsonSchema)) {
    throw new ProtocolError("INVALID_REQUEST", "requiredControls structured output schema must be an object");
  }
  const schemaHash = requiredString(
    structured.schemaHash,
    "requiredControls.structuredOutput.schemaHash",
    /^[0-9a-f]{64}$/,
  );
  if (contentHash(structured.jsonSchema) !== schemaHash) {
    throw new ProtocolError("INVALID_REQUEST", "requiredControls structured output schema hash mismatch");
  }
  const controls = {
    maxInputTokens: positiveInteger(raw.maxInputTokens, "requiredControls.maxInputTokens"),
    maxOutputTokens: positiveInteger(raw.maxOutputTokens, "requiredControls.maxOutputTokens"),
    maxTotalTokens: positiveInteger(raw.maxTotalTokens, "requiredControls.maxTotalTokens"),
    maxCostUsd: positiveNumber(raw.maxCostUsd, "requiredControls.maxCostUsd"),
    structuredOutput: {
      schemaName: requiredString(
        structured.schemaName,
        "requiredControls.structuredOutput.schemaName",
        /^[A-Za-z][A-Za-z0-9._-]{0,127}$/,
      ),
      schemaHash,
      jsonSchema: structured.jsonSchema,
    },
  };
  if (controls.maxOutputTokens !== requestMaxTokens) {
    throw new ProtocolError("INVALID_REQUEST", "requiredControls output limit must equal maxTokens");
  }
  if (controls.maxTotalTokens < controls.maxInputTokens + controls.maxOutputTokens) {
    throw new ProtocolError("INVALID_REQUEST", "requiredControls total limit is smaller than input plus output limits");
  }
  return Object.freeze(controls);
}

export function validateRequest(input, maxFrameBytes) {
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
  const maxTokens = positiveInteger(input.maxTokens, "maxTokens");
  const request = Object.freeze({
    schemaVersion: PROTOCOL_VERSION,
    invocationId: requiredString(input.invocationId, "invocationId", /^invocation:[A-Za-z0-9._-]+$/),
    workOrderId: requiredString(input.workOrderId, "workOrderId", /^work:[A-Za-z0-9._-]+$/),
    profileId: requiredString(input.profileId, "profileId", /^profile:[A-Za-z0-9._-]+$/),
    model: requiredString(input.model, "model", /^[A-Za-z0-9._-]+\/[A-Za-z0-9][A-Za-z0-9._:/-]*$/),
    prompt: requiredString(input.prompt, "prompt"),
    maxTokens,
    timeoutMs: positiveInteger(input.timeoutMs, "timeoutMs"),
    ...(input.requiredControls === undefined ? {} : {
      requiredControls: validateRequiredControls(input.requiredControls, maxTokens),
    }),
    ...(input.replayOnly === true ? { replayOnly: true } : {}),
  });
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
