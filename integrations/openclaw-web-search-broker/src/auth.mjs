import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { lstat, open } from "node:fs/promises";
import path from "node:path";

import { ProtocolError, canonicalJson } from "./protocol.mjs";

const AUTH_KEYS = new Set(["scheme", "clientId", "timestampMs", "nonce", "mac"]);
const HEX_64 = /^[0-9a-f]{64}$/;
const NONCE = /^[0-9a-f]{32}$/;

function assertOwnerOnlyRegularFile(stat, label) {
  if (!stat.isFile()) throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be a regular file`);
  if ((stat.mode & 0o077) !== 0) throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be owner-only`);
  if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
    throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be owned by the broker user`);
  }
}

export function authenticationPayload(request) {
  const { auth, ...core } = request;
  const { mac: _mac, ...unsignedAuth } = auth;
  return { ...core, auth: unsignedAuth };
}

export function computeMac(secret, request) {
  return createHmac("sha256", secret).update(canonicalJson(authenticationPayload(request)), "utf8").digest("hex");
}

export function signRequest(coreRequest, { secret, clientId, timestampMs, nonce }) {
  const request = {
    ...coreRequest,
    auth: {
      scheme: "hmac-sha256-v1",
      clientId,
      timestampMs,
      nonce,
      mac: "0".repeat(64),
    },
  };
  request.auth.mac = computeMac(secret, request);
  return request;
}

export async function loadOrCreateSecret(stateDir, socketName) {
  const secretPath = path.join(stateDir, `${socketName}.key`);
  let handle;
  try {
    handle = await open(secretPath, "wx", 0o600);
    const secret = randomBytes(32).toString("hex");
    await handle.writeFile(`${secret}\n`, { encoding: "utf8" });
    await handle.sync();
    await handle.close();
    handle = undefined;
    return { secretPath, secret };
  } catch (error) {
    await handle?.close().catch(() => {});
    if (error?.code !== "EEXIST") throw error;
  }
  const stat = await lstat(secretPath);
  assertOwnerOnlyRegularFile(stat, "broker authentication key");
  const reader = await open(secretPath, "r");
  try {
    const secret = (await reader.readFile("utf8")).trim();
    if (!HEX_64.test(secret)) throw new ProtocolError("INVALID_STATE_FILE", "broker authentication key is invalid");
    return { secretPath, secret };
  } finally {
    await reader.close();
  }
}

export class RequestAuthenticator {
  constructor({ secret, clientId, maxSkewMs, maxNonces = 4096, clock = Date.now }) {
    if (!HEX_64.test(secret)) throw new ProtocolError("INVALID_CONFIG", "authentication secret is invalid");
    if (typeof clientId !== "string" || !/^client:[A-Za-z0-9._-]+$/.test(clientId)) {
      throw new ProtocolError("INVALID_CONFIG", "clientId is invalid");
    }
    if (!Number.isSafeInteger(maxSkewMs) || maxSkewMs < 1000 || maxSkewMs > 300_000) {
      throw new ProtocolError("INVALID_CONFIG", "authMaxSkewMs is invalid");
    }
    this.secret = secret;
    this.clientId = clientId;
    this.maxSkewMs = maxSkewMs;
    this.maxNonces = maxNonces;
    this.clock = clock;
    this.nonces = new Map();
  }

  verify(request) {
    if (!request || typeof request !== "object" || Array.isArray(request)) {
      throw new ProtocolError("AUTH_REQUIRED", "authenticated request is required");
    }
    const auth = request.auth;
    if (!auth || typeof auth !== "object" || Array.isArray(auth)) {
      throw new ProtocolError("AUTH_REQUIRED", "request authentication is required");
    }
    const unknown = Object.keys(auth).filter((key) => !AUTH_KEYS.has(key));
    const missing = [...AUTH_KEYS].filter((key) => !(key in auth));
    if (unknown.length || missing.length) throw new ProtocolError("AUTH_INVALID", "authentication envelope is invalid");
    if (auth.scheme !== "hmac-sha256-v1" || auth.clientId !== this.clientId) {
      throw new ProtocolError("AUTH_INVALID", "authentication envelope is invalid");
    }
    if (!Number.isSafeInteger(auth.timestampMs) || !NONCE.test(auth.nonce) || !HEX_64.test(auth.mac)) {
      throw new ProtocolError("AUTH_INVALID", "authentication envelope is invalid");
    }
    const now = this.clock();
    if (!Number.isSafeInteger(now) || Math.abs(now - auth.timestampMs) > this.maxSkewMs) {
      throw new ProtocolError("AUTH_EXPIRED", "authentication timestamp is outside the allowed window");
    }
    this.#prune(now);
    if (this.nonces.has(auth.nonce)) throw new ProtocolError("AUTH_REPLAY", "authentication nonce was already used");
    const expected = Buffer.from(computeMac(this.secret, request), "hex");
    const actual = Buffer.from(auth.mac, "hex");
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
      throw new ProtocolError("AUTH_INVALID", "authentication envelope is invalid");
    }
    this.nonces.set(auth.nonce, now + this.maxSkewMs);
    while (this.nonces.size > this.maxNonces) this.nonces.delete(this.nonces.keys().next().value);
    const { auth: _auth, ...core } = request;
    return core;
  }

  #prune(now) {
    for (const [nonce, expiresAt] of this.nonces) {
      if (expiresAt < now) this.nonces.delete(nonce);
    }
  }
}
