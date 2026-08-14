import { createHash } from "node:crypto";
import process from "node:process";

import { Agent } from "@earendil-works/pi-agent-core";
import { EventStream } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const PROTOCOL_VERSION = "0.1";

function canonicalObject(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalObject).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalObject(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function stableId(prefix, value) {
  return `${prefix}-${createHash("sha256").update(canonicalObject(value)).digest("hex").slice(0, 32)}`;
}

function requireObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object`);
  return value;
}

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);

try {
  const request = requireObject(JSON.parse(Buffer.concat(chunks).toString("utf8")), "request");
  if (Object.keys(request).sort().join(",") !== "protocol_version,runtime_profile,work_order") {
    throw new Error("unexpected request fields");
  }
  if (request.protocol_version !== PROTOCOL_VERSION) throw new Error("unsupported protocol version");
  const work = requireObject(request.work_order, "work_order");
  const profile = requireObject(request.runtime_profile, "runtime_profile");
  const records = work.metadata?.formatter_records;
  if (!Array.isArray(records) || records.some((record) => !record || typeof record !== "object" || Array.isArray(record))) {
    throw new Error("formatter_records must be an array of objects");
  }
  const capability = work.requested_capabilities?.[0];
  if (capability !== "format.records") throw new Error("unsupported capability");

  const loopEvents = [];
  let formatted = null;
  const tool = {
    name: "format_records",
    label: "Format records",
    description: "Canonicalize records as JSON Lines",
    parameters: Type.Object({ records: Type.Array(Type.Record(Type.String(), Type.Unknown())) }),
    async execute(_toolCallId, { records: toolRecords }) {
      formatted = toolRecords.map(canonicalObject).join("\n") + (toolRecords.length ? "\n" : "");
      return {
        content: [{ type: "text", text: formatted }],
        details: { formatted, record_count: toolRecords.length },
      };
    },
  };

  const model = {
    id: "dalton-pi-spike",
    name: "Dalton Pi Spike",
    api: "openai-responses",
    provider: "local-fixture",
    baseUrl: "https://example.invalid",
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 8192,
    maxTokens: 2048,
  };
  const usages = [
    { input: 17, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 22, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    { input: 8, output: 4, cacheRead: 0, cacheWrite: 0, totalTokens: 12, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
  ];
  let callIndex = 0;
  const streamFn = () => {
    const stream = new EventStream(
      (event) => event.type === "done" || event.type === "error",
      (event) => (event.type === "done" ? event.message : event.error),
    );
    queueMicrotask(() => {
      const toolTurn = callIndex === 0;
      const message = {
        role: "assistant",
        content: toolTurn
          ? [{ type: "toolCall", id: "format-1", name: "format_records", arguments: { records } }]
          : [{ type: "text", text: "FORMAT_OK" }],
        api: model.api,
        provider: model.provider,
        model: model.id,
        usage: usages[callIndex],
        stopReason: toolTurn ? "toolUse" : "stop",
        timestamp: Date.now(),
      };
      stream.push({ type: "done", reason: message.stopReason, message });
      callIndex += 1;
    });
    return stream;
  };

  const agent = new Agent({
    initialState: { systemPrompt: "Execute the assigned Dalton capability only.", model, tools: [tool] },
    streamFn,
    toolExecution: "sequential",
  });
  agent.subscribe((event) => {
    if (["turn_start", "turn_end", "tool_execution_start", "tool_execution_end"].includes(event.type)) {
      loopEvents.push(event.type);
    }
  });
  await agent.prompt(`work_order_id=${work.id}; capability=${capability}`);
  if (formatted === null) throw new Error("Pi agent loop did not execute formatter tool");

  const usage = {
    input_tokens: usages.reduce((sum, item) => sum + item.input, 0),
    output_tokens: usages.reduce((sum, item) => sum + item.output, 0),
    tokens: usages.reduce((sum, item) => sum + item.totalTokens, 0),
    cost: 0,
  };
  const invocationId = stableId("inv", { work: work.id, profile: profile.id, records });
  const artifactHash = createHash("sha256").update(formatted).digest("hex");
  const artifactRef = `artifact:formatter:${artifactHash}`;
  const usageRef = `usage:${invocationId}`;
  const invocation = {
    schema_version: work.schema_version,
    id: invocationId,
    created_at: work.created_at,
    work_order_ref: work.id,
    profile_ref: profile.id,
    granularity: "task",
    capability,
    provider: model.provider,
    model: model.id,
    model_family: "pi-agent-core-fixture",
    input_refs: work.input_refs ?? [],
    output_refs: [artifactRef],
    started_at: work.created_at,
    completed_at: work.created_at,
    usage,
    side_effects: [],
    runtime_ref: profile.id,
    actor_ref: "runtime:pi-agent-core",
    parent_ref: null,
    environment_hash: profile.environment_hash,
  };
  const result = {
    schema_version: work.schema_version,
    id: stableId("result", { invocation: invocationId, artifact: artifactHash }),
    created_at: work.created_at,
    work_order_ref: work.id,
    invocation_ref: invocationId,
    status: "completed",
    outputs: { format: "canonical-jsonl-v1", formatted, record_count: records.length, final_text: "FORMAT_OK" },
    actual_side_effects: [],
    usage_refs: [usageRef],
    artifact_refs: [artifactRef],
    error: null,
    metadata: { runtime: "pi-agent-core", version: "0.84.1", loop_events: loopEvents },
  };
  process.stdout.write(`${canonicalObject({ protocol_version: PROTOCOL_VERSION, invocation, result })}\n`);
} catch (error) {
  process.stderr.write(`Pi spike error: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
