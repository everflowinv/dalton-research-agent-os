import { lstat, mkdir, rm, chmod } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { ProtocolError, canonicalJson, protocolFailure, strictJsonParse } from "./protocol.mjs";
import { RequestAuthenticator, loadOrCreateSecret } from "./auth.mjs";
import { FileIdempotencyJournal } from "./journal.mjs";

async function removeStaleSocket(socketPath) {
  try {
    const stat = await lstat(socketPath);
    if (!stat.isSocket()) throw new ProtocolError("UNSAFE_SOCKET_PATH", "refusing to replace a non-socket path");
    await rm(socketPath);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

export class BrokerServer {
  constructor(broker) {
    this.broker = broker;
    this.server = null;
    this.socketPath = null;
    this.connections = new Set();
  }

  async start(stateDir) {
    if (this.server) throw new Error("broker server is already running");
    await mkdir(stateDir, { recursive: true, mode: 0o700 });
    const { secret } = await loadOrCreateSecret(stateDir, this.broker.config.socketName);
    this.authenticator = new RequestAuthenticator({
      secret,
      clientId: this.broker.config.clientId,
      maxSkewMs: this.broker.config.authMaxSkewMs,
    });
    this.broker.journal = await FileIdempotencyJournal.open(stateDir, this.broker.config.socketName, {
      ttlMs: this.broker.config.journalTtlMs,
      maxRecords: this.broker.config.journalMaxRecords,
      maxBytes: this.broker.config.journalMaxBytes,
    });
    this.socketPath = path.join(stateDir, this.broker.config.socketName);
    await removeStaleSocket(this.socketPath);
    this.server = net.createServer((socket) => this.#accept(socket));
    await new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(this.socketPath, resolve);
    });
    await chmod(this.socketPath, 0o600);
    return this.socketPath;
  }

  async stop() {
    if (!this.server) return;
    for (const socket of this.connections) socket.destroy();
    const server = this.server;
    this.server = null;
    await new Promise((resolve) => server.close(resolve));
    if (this.socketPath) await removeStaleSocket(this.socketPath);
    this.socketPath = null;
  }

  #accept(socket) {
    this.connections.add(socket);
    socket.setTimeout(this.broker.limits.idleTimeoutMs, () => socket.destroy());
    let buffer = Buffer.alloc(0);
    let handled = false;
    socket.on("data", async (chunk) => {
      if (handled) return;
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > this.broker.limits.maxFrameBytes + 1) {
        handled = true;
        this.#reply(socket, protocolFailure("FRAME_TOO_LARGE", "request exceeds the configured frame limit", this.broker.runtime.version));
        return;
      }
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      handled = true;
      if (newline !== buffer.length - 1 || newline === 0) {
        this.#reply(socket, protocolFailure("INVALID_FRAME", "exactly one non-empty JSONL frame is required", this.broker.runtime.version));
        return;
      }
      try {
        const request = strictJsonParse(buffer.subarray(0, newline).toString("utf8"));
        const authenticated = this.authenticator.verify(request);
        this.#reply(socket, await this.broker.handle(authenticated));
      } catch (error) {
        const code = error instanceof ProtocolError ? error.code : "INVALID_JSON";
        const message = error instanceof ProtocolError ? error.message : "request is not valid JSON";
        this.#reply(socket, protocolFailure(code, message, this.broker.runtime.version));
      }
    });
    socket.on("close", () => this.connections.delete(socket));
    socket.on("error", () => this.connections.delete(socket));
  }

  #reply(socket, payload) {
    socket.end(`${canonicalJson(payload)}\n`);
  }
}
