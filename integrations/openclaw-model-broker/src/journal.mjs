import { randomBytes } from "node:crypto";
import { lstat, mkdir, open, rename, stat } from "node:fs/promises";
import path from "node:path";

import { PROTOCOL_VERSION, ProtocolError, canonicalJson, contentHash } from "./protocol.mjs";

const JOURNAL_VERSION = "0.1";
const HASH = /^[0-9a-f]{64}$/;

function ownerOnlyRegular(statValue, label) {
  if (!statValue.isFile()) throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be a regular file`);
  if ((statValue.mode & 0o077) !== 0) throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be owner-only`);
  if (typeof process.getuid === "function" && statValue.uid !== process.getuid()) {
    throw new ProtocolError("UNSAFE_STATE_FILE", `${label} must be owned by the broker user`);
  }
}

function validateRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) throw new ProtocolError("INVALID_JOURNAL", "journal record is invalid");
  const allowed = new Set(["invocationId", "requestHash", "state", "createdAtMs", "expiresAtMs", "response"]);
  if (Object.keys(record).some((key) => !allowed.has(key))) throw new ProtocolError("INVALID_JOURNAL", "journal record shape is invalid");
  if (typeof record.invocationId !== "string" || !/^invocation:[A-Za-z0-9._-]+$/.test(record.invocationId)) {
    throw new ProtocolError("INVALID_JOURNAL", "journal invocation id is invalid");
  }
  if (!HASH.test(record.requestHash) || !["pending", "completed"].includes(record.state)) {
    throw new ProtocolError("INVALID_JOURNAL", "journal record metadata is invalid");
  }
  if (!Number.isSafeInteger(record.createdAtMs) || !Number.isSafeInteger(record.expiresAtMs) || record.expiresAtMs <= record.createdAtMs) {
    throw new ProtocolError("INVALID_JOURNAL", "journal record timestamps are invalid");
  }
  if (record.state === "pending" && record.response !== null) throw new ProtocolError("INVALID_JOURNAL", "pending journal record has a response");
  if (record.state === "completed") {
    if (!record.response || typeof record.response !== "object" || Array.isArray(record.response)) {
      throw new ProtocolError("INVALID_JOURNAL", "completed journal record lacks a response");
    }
    const { contentHash: actual, ...body } = record.response;
    if (!HASH.test(actual) || contentHash(body) !== actual) {
      throw new ProtocolError("INVALID_JOURNAL", "persisted response hash mismatch");
    }
    const serialized = canonicalJson(record.response);
    for (const forbidden of ["prompt", "apiKey", "authProfile", "credential", "headers", "baseUrl"]) {
      if (serialized.includes(`\"${forbidden}\"`)) throw new ProtocolError("INVALID_JOURNAL", "persisted response contains forbidden authority data");
    }
  }
  return record;
}

export class MemoryIdempotencyJournal {
  constructor({ ttlMs = 86_400_000, clock = Date.now } = {}) {
    this.ttlMs = ttlMs;
    this.clock = clock;
    this.records = new Map();
  }

  async claim(invocationId, requestHash) {
    this.prune();
    const prior = this.records.get(invocationId);
    if (prior) {
      if (prior.requestHash !== requestHash) return { status: "conflict", record: prior };
      return { status: prior.state, record: prior };
    }
    const now = this.clock();
    const record = { invocationId, requestHash, state: "pending", createdAtMs: now, expiresAtMs: now + this.ttlMs, response: null };
    this.records.set(invocationId, record);
    return { status: "fresh", record };
  }

  async complete(invocationId, requestHash, response) {
    const record = this.records.get(invocationId);
    if (!record || record.requestHash !== requestHash || record.state !== "pending") {
      throw new ProtocolError("JOURNAL_CONFLICT", "cannot complete an unclaimed invocation");
    }
    const completed = { ...record, state: "completed", response };
    this.records.set(invocationId, completed);
    return completed;
  }

  get(invocationId) {
    this.prune();
    return this.records.get(invocationId) ?? null;
  }

  prune() {
    const now = this.clock();
    // A pending row means the host call may already have executed.  Time can
    // never turn that uncertainty into permission to call the host again.
    // Pending rows require a separate operator resolution boundary and are
    // intentionally immune to automatic TTL and capacity pruning.
    for (const [id, record] of this.records) {
      if (record.state === "completed" && record.expiresAtMs <= now) this.records.delete(id);
    }
  }
}

export class FileIdempotencyJournal extends MemoryIdempotencyJournal {
  static async open(stateDir, socketName, options) {
    await mkdir(stateDir, { recursive: true, mode: 0o700 });
    const journal = new FileIdempotencyJournal(path.join(stateDir, `${socketName}.journal.json`), options);
    await journal.#load();
    return journal;
  }

  constructor(filePath, { ttlMs, maxRecords, maxBytes, clock = Date.now }) {
    super({ ttlMs, clock });
    this.filePath = filePath;
    this.maxRecords = maxRecords;
    this.maxBytes = maxBytes;
    this.queue = Promise.resolve();
  }

  async claim(invocationId, requestHash) {
    return this.#locked(async () => {
      const result = await super.claim(invocationId, requestHash);
      if (result.status === "fresh") {
        try {
          await this.#persist(invocationId);
        } catch (error) {
          this.records.delete(invocationId);
          throw error;
        }
      }
      return result;
    });
  }

  async complete(invocationId, requestHash, response) {
    return this.#locked(async () => {
      const previous = this.records.get(invocationId);
      const result = await super.complete(invocationId, requestHash, response);
      try {
        await this.#persist(invocationId);
      } catch (error) {
        if (previous) this.records.set(invocationId, previous);
        throw error;
      }
      return result;
    });
  }

  async #locked(operation) {
    const run = this.queue.then(operation, operation);
    this.queue = run.then(() => undefined, () => undefined);
    return run;
  }

  async #load() {
    let fileStat;
    try {
      fileStat = await lstat(this.filePath);
    } catch (error) {
      if (error?.code === "ENOENT") return;
      throw error;
    }
    ownerOnlyRegular(fileStat, "idempotency journal");
    if (fileStat.size > this.maxBytes) throw new ProtocolError("JOURNAL_TOO_LARGE", "idempotency journal exceeds its configured limit");
    const handle = await open(this.filePath, "r");
    let raw;
    try {
      raw = await handle.readFile("utf8");
    } finally {
      await handle.close();
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new ProtocolError("INVALID_JOURNAL", "idempotency journal is not valid JSON");
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || parsed.schemaVersion !== JOURNAL_VERSION || !Array.isArray(parsed.records) || Object.keys(parsed).some((key) => !["schemaVersion", "records"].includes(key))) {
      throw new ProtocolError("INVALID_JOURNAL", "idempotency journal envelope is invalid");
    }
    if (parsed.records.length > this.maxRecords) throw new ProtocolError("JOURNAL_TOO_LARGE", "idempotency journal has too many records");
    for (const rawRecord of parsed.records) {
      const record = validateRecord(rawRecord);
      if (this.records.has(record.invocationId)) throw new ProtocolError("INVALID_JOURNAL", "journal has duplicate invocation ids");
      this.records.set(record.invocationId, record);
    }
    this.prune();
  }

  #boundedRecords(pinnedInvocationId) {
    this.prune();
    let records = [...this.records.values()].sort((a, b) => a.createdAtMs - b.createdAtMs || a.invocationId.localeCompare(b.invocationId));
    while (records.length > this.maxRecords) {
      const index = records.findIndex((record) => record.state === "completed" && record.invocationId !== pinnedInvocationId);
      if (index < 0) throw new ProtocolError("JOURNAL_CAPACITY", "pending invocations exhaust journal capacity");
      this.records.delete(records[index].invocationId);
      records.splice(index, 1);
    }
    let wire = { schemaVersion: JOURNAL_VERSION, records };
    while (Buffer.byteLength(canonicalJson(wire), "utf8") > this.maxBytes) {
      const index = records.findIndex((record) => record.state === "completed" && record.invocationId !== pinnedInvocationId);
      if (index < 0) throw new ProtocolError("JOURNAL_CAPACITY", "pending invocations exhaust journal byte limit");
      this.records.delete(records[index].invocationId);
      records.splice(index, 1);
      wire = { schemaVersion: JOURNAL_VERSION, records };
    }
    return wire;
  }

  async #persist(pinnedInvocationId) {
    const wire = this.#boundedRecords(pinnedInvocationId);
    const bytes = `${canonicalJson(wire)}\n`;
    const directory = path.dirname(this.filePath);
    const temporary = `${this.filePath}.tmp-${process.pid}-${randomBytes(8).toString("hex")}`;
    const handle = await open(temporary, "wx", 0o600);
    try {
      await handle.writeFile(bytes, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, this.filePath);
    const finalStat = await stat(this.filePath);
    ownerOnlyRegular(finalStat, "idempotency journal");
    try {
      const dirHandle = await open(directory, "r");
      try {
        await dirHandle.sync();
      } finally {
        await dirHandle.close();
      }
    } catch (error) {
      if (process.platform !== "win32") throw error;
    }
  }
}
