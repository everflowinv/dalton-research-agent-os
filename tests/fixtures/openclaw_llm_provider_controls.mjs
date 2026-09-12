import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const openClawRoot = process.argv[2];
if (!openClawRoot) {
  throw new Error("usage: node patch/test_openclaw_llm_provider_controls.mjs OPENCLAW_ROOT");
}

const aiRoot = join(openClawRoot, "node_modules", "@openclaw", "ai", "dist");
const { completeSimple, defaultApiRegistry } = await import(
  pathToFileURL(join(aiRoot, "internal", "runtime.mjs")).href
);
const { registerBuiltInApiProviders } = await import(
  pathToFileURL(join(aiRoot, "providers.mjs")).href
);
const runtimeFiles = (await import("node:fs/promises"))
  .readdir(join(openClawRoot, "dist"));
const runtimeFile = (await runtimeFiles).find((name) => /^runtime-llm\.runtime-.*\.m?js$/.test(name));
if (!runtimeFile) throw new Error("patched runtime-llm bundle was not found");
const { createRuntimeLlm } = await import(
  pathToFileURL(join(openClawRoot, "dist", runtimeFile)).href
);

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]));
  }
  return value;
}

function hash(value) {
  return createHash("sha256").update(JSON.stringify(canonicalize(value))).digest("hex");
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

registerBuiltInApiProviders(defaultApiRegistry);
const requests = [];
let countedInputTokens = 12;
const server = http.createServer(async (request, response) => {
  const body = await readJson(request);
  requests.push({ url: request.url, body });
  if (request.url === "/v1/responses/input_tokens") {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ object: "response.input_tokens", input_tokens: countedInputTokens }));
    return;
  }
  if (request.url === "/v1/responses") {
    response.writeHead(400, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: { message: "fake provider stop after payload capture" } }));
    return;
  }
  if (request.url?.includes(":countTokens")) {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ totalTokens: countedInputTokens }));
    return;
  }
  if (request.url?.includes(":streamGenerateContent")) {
    if (request.url?.includes("gemini-3.7-flash")) {
      assert.equal(body.generationConfig.thinkingConfig.thinkingLevel, "LOW");
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end(`data: ${JSON.stringify({
        candidates: [{
          content: { parts: [{ text: "HELLO" }] },
          finishReason: "STOP",
        }],
        usageMetadata: {
          promptTokenCount: 5,
          candidatesTokenCount: 1,
          totalTokenCount: 6,
        },
      })}\n\n`);
      return;
    }
    response.writeHead(400, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: { message: "fake provider stop after payload capture" } }));
    return;
  }
  response.writeHead(404).end();
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));

try {
  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}/v1`;
  const model = {
    id: "fake-model",
    name: "Fake OpenAI Responses",
    api: "openai-responses",
    provider: "openai",
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 1, output: 1, cacheRead: 1, cacheWrite: 1 },
    contextWindow: 1000,
    maxTokens: 100,
  };
  const schema = {
    type: "object",
    additionalProperties: false,
    required: ["verdict"],
    properties: { verdict: { type: "string", enum: ["pass", "reject"] } },
  };
  const now = Date.now();
  const rateCard = {
    model: "openai/fake-model",
    serviceTier: "default",
    inputUsdPerMillion: "1.00",
    cachedInputUsdPerMillion: "1.00",
    cacheWriteUsdPerMillion: "1.00",
    outputUsdPerMillion: "2.00",
    verifiedAt: new Date(now - 60_000).toISOString(),
    expiresAt: new Date(now + 86_400_000).toISOString(),
  };
  const controls = {
    mode: "openai-responses-input-count-v1",
    maxInputTokens: 20,
    maxOutputTokens: 10,
    maxTotalTokens: 30,
    maxCostUsd: 0.001,
    structuredOutput: {
      schemaName: "dalton_verifier",
      schemaHash: hash(schema),
      jsonSchema: schema,
    },
    rateCard,
  };

  let proof;
  await completeSimple(model, {
    messages: [{ role: "user", content: "Verify the evidence.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    maxRetries: 0,
    providerControls: controls,
    onProviderControlProof: (value) => { proof = value; },
  }).catch(() => {});
  assert.deepEqual(requests.map((item) => item.url), [
    "/v1/responses/input_tokens",
    "/v1/responses",
  ]);
  for (const body of requests.map((item) => item.body)) {
    assert.equal(body.text.format.type, "json_schema");
    assert.equal(body.text.format.strict, true);
    assert.deepEqual(body.text.format.schema, schema);
  }
  assert.equal(requests[1].body.max_output_tokens, 10);
  assert.equal(requests[1].body.service_tier, "default");
  assert.equal(proof.rateCardHash, hash(rateCard));
  assert.equal(proof.worstCaseCostUsd, "0.000032");

  requests.length = 0;
  countedInputTokens = 21;
  await completeSimple(model, {
    messages: [{ role: "user", content: "Too large.", timestamp: 0 }],
  }, { apiKey: "fake-key", maxRetries: 0, providerControls: controls }).catch(() => {});
  assert.deepEqual(requests.map((item) => item.url), ["/v1/responses/input_tokens"]);

  requests.length = 0;
  countedInputTokens = 12;
  await completeSimple(model, {
    messages: [{ role: "user", content: "Too expensive.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxRetries: 0,
    providerControls: { ...controls, maxCostUsd: 0.000001 },
  }).catch(() => {});
  assert.deepEqual(requests.map((item) => item.url), ["/v1/responses/input_tokens"]);

  requests.length = 0;
  const cfg = {
    models: { providers: { openai: {
      baseUrl,
      apiKey: "fake-key",
      api: "openai-chatgpt-responses",
      models: [
        { ...model, api: undefined, id: "fake-chatgpt-model", provider: undefined },
        {
          ...model,
          api: "openai-responses",
          id: "fake-runtime-model",
          provider: undefined,
          reasoning: true,
        },
      ],
    } } },
    agents: {
      defaults: {
        model: { primary: "openai/fake-chatgpt-model" },
        models: {
          "openai/fake-chatgpt-model": {},
          "openai/fake-runtime-model": {},
        },
      },
      list: [{ id: "dalton-model-broker", model: "openai/fake-chatgpt-model" }],
    },
  };
  const runtime = createRuntimeLlm({
    getConfig: () => cfg,
    authority: {
      caller: { kind: "plugin", id: "dalton-openclaw-model-broker" },
      allowComplete: true,
      allowModelOverride: true,
      allowedModels: ["openai/fake-chatgpt-model", "openai/fake-runtime-model"],
      allowAgentIdOverride: true,
    },
    logger: { debug() {}, info() {}, warn() {}, error() {} },
  });
  assert.equal(runtime.capabilities.providerControls.transport, "openai/openai-responses");
  assert.deepEqual(runtime.capabilities.thinkingLevel.levels, [
    "off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max",
  ]);
  await assert.rejects(
    runtime.complete({
      messages: [{ role: "user", content: "must not reach provider" }],
      model: "openai/fake-chatgpt-model",
      maxTokens: 10,
      agentId: "dalton-model-broker",
      providerControls: controls,
    }),
    (error) => {
      assert.equal(error?.code, "REQUIRED_CONTROLS_UNAVAILABLE");
      assert.equal(
        error?.message,
        "Plugin LLM completion failed: selected endpoint cannot enforce provider max_output_tokens.",
      );
      return true;
    },
  );
  assert.equal(requests.length, 0);

  await assert.rejects(
    runtime.complete({
      messages: [{ role: "user", content: "must reject before provider" }],
      model: "openai/fake-runtime-model",
      maxTokens: 10,
      thinkingLevel: "ultra",
      agentId: "dalton-model-broker",
    }),
    /thinkingLevel is unsupported/,
  );
  assert.equal(requests.length, 0);

  await runtime.complete({
    messages: [{ role: "user", content: "capture exact reasoning" }],
    model: "openai/fake-runtime-model",
    maxTokens: 10,
    thinkingLevel: "high",
    agentId: "dalton-model-broker",
  }).catch(() => {});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/v1/responses");
  assert.equal(requests[0].body.reasoning.effort, "high");
  requests.length = 0;

  const googleModel = {
    id: "fake-google",
    name: "Fake Google Generative AI",
    api: "google-generative-ai",
    provider: "google",
    baseUrl: `http://127.0.0.1:${address.port}/v1beta`,
    reasoning: false,
    input: ["text"],
    cost: { input: 4, output: 18, cacheRead: 4, cacheWrite: 4 },
    contextWindow: 1000,
    maxTokens: 100,
  };
  const googleRateCard = {
    model: "google/fake-google",
    serviceTier: "default",
    inputUsdPerMillion: "4.00",
    cachedInputUsdPerMillion: "4.00",
    cacheWriteUsdPerMillion: "4.00",
    outputUsdPerMillion: "18.00",
    verifiedAt: new Date(now - 60_000).toISOString(),
    expiresAt: new Date(now + 86_400_000).toISOString(),
  };
  const googleControls = {
    ...controls,
    mode: "google-generative-ai-count-tokens-v1",
    rateCard: googleRateCard,
  };
  requests.length = 0;
  countedInputTokens = 12;
  let googleProof;
  await completeSimple(googleModel, {
    messages: [{ role: "user", content: "Verify the evidence.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: googleControls,
    onProviderControlProof: (value) => { googleProof = value; },
  }).catch(() => {});
  assert.equal(requests.length, 2);
  assert.match(requests[0].url, /:countTokens/);
  assert.match(requests[1].url, /:streamGenerateContent/);
  assert.equal(requests[1].body.generationConfig.maxOutputTokens, 10);
  assert.equal(requests[1].body.generationConfig.responseMimeType, "application/json");
  assert.deepEqual(requests[1].body.generationConfig.responseJsonSchema, schema);
  assert.equal(googleProof.mode, "google-generative-ai-count-tokens-v1");
  assert.equal(googleProof.rateCardHash, hash(googleRateCard));
  assert.equal(googleProof.worstCaseCostUsd, "0.000228");

  requests.length = 0;
  const unsupportedSchema = {
    ...schema,
    properties: {
      ...schema.properties,
      verdict: { const: "pass" },
    },
  };
  await completeSimple(googleModel, {
    messages: [{ role: "user", content: "Reject before provider.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: {
      ...googleControls,
      structuredOutput: {
        ...googleControls.structuredOutput,
        schemaHash: hash(unsupportedSchema),
        jsonSchema: unsupportedSchema,
      },
    },
  }).catch(() => {});
  assert.equal(requests.length, 0);

  requests.length = 0;
  countedInputTokens = 12;
  let thinkingProof;
  await completeSimple(googleModel, {
    messages: [{ role: "user", content: "Verify with pinned low thinking.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: { ...googleControls, thinkingLevel: "low" },
    onProviderControlProof: (value) => { thinkingProof = value; },
  }).catch(() => {});
  assert.equal(requests.length, 2);
  assert.match(requests[0].url, /:countTokens/);
  assert.match(requests[1].url, /:streamGenerateContent/);
  assert.equal(requests[1].body.generationConfig.thinkingConfig.thinkingLevel, "LOW");
  assert.equal(thinkingProof.thinkingLevel, "low");
  assert.equal(thinkingProof.worstCaseCostUsd, "0.000228");

  requests.length = 0;
  await completeSimple(googleModel, {
    messages: [{ role: "user", content: "Reject uncalibrated level.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: { ...googleControls, thinkingLevel: "high" },
  }).catch(() => {});
  assert.equal(requests.length, 0);

  requests.length = 0;
  await completeSimple({ ...googleModel, id: "gemini-3.7-flash", reasoning: true }, {
    messages: [{ role: "user", content: "Caller thinking intent cannot override the control.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    thinking: { enabled: true, level: "high" },
    providerControls: {
      ...googleControls,
      thinkingLevel: "low",
      rateCard: { ...googleRateCard, model: "google/gemini-3.7-flash" },
    },
  }).catch(() => {});
  assert.equal(requests.length, 2);
  assert.deepEqual(
    requests[1].body.generationConfig.thinkingConfig,
    { thinkingLevel: "LOW" },
  );

  requests.length = 0;
  let reasoningProof;
  const reasoningGoogleModel = { ...googleModel, id: "gemini-3.7-flash", reasoning: true };
  await completeSimple(reasoningGoogleModel, {
    messages: [{ role: "user", content: "Reasoning model broker path.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: {
      ...googleControls,
      thinkingLevel: "low",
      rateCard: { ...googleRateCard, model: "google/gemini-3.7-flash" },
    },
    onProviderControlProof: (value) => { reasoningProof = value; },
  }).catch(() => {});
  assert.equal(requests.length, 2);
  assert.deepEqual(
    requests[1].body.generationConfig.thinkingConfig,
    { thinkingLevel: "LOW" },
  );
  assert.equal(reasoningProof.thinkingLevel, "low");

  requests.length = 0;
  let legacyProof;
  await completeSimple(googleModel, {
    messages: [{ role: "user", content: "Controls without thinking stay unchanged.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 10,
    providerControls: googleControls,
    onProviderControlProof: (value) => { legacyProof = value; },
  }).catch(() => {});
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.generationConfig.thinkingConfig, void 0);
  assert.equal("thinkingLevel" in legacyProof, false);

  assert.deepEqual(runtime.capabilities.providerControls.modes, [
    "openai-responses-input-count-v1",
    "google-generative-ai-count-tokens-v1",
  ]);
  assert.equal(
    runtime.capabilities.providerControls.transports["google-generative-ai-count-tokens-v1"],
    "google/google-generative-ai",
  );

  requests.length = 0;
  const geminiFlashModel = {
    ...googleModel,
    id: "gemini-3.7-flash",
    name: "Gemini 3.7 Flash",
    reasoning: true,
  };
  const flashResult = await completeSimple(geminiFlashModel, {
    messages: [{ role: "user", content: "Return HELLO.", timestamp: 0 }],
  }, {
    apiKey: "fake-key",
    maxTokens: 100,
    maxRetries: 0,
  });
  assert.equal(flashResult.content.find((item) => item.type === "text")?.text, "HELLO");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.generationConfig.thinkingConfig.thinkingLevel, "LOW");

  console.log(JSON.stringify({
    ok: true,
    admitted: { inputCountCalls: 1, modelCalls: 1 },
    inputLimit: { inputCountCalls: 1, modelCalls: 0 },
    costLimit: { inputCountCalls: 1, modelCalls: 0 },
    unsupportedTransport: { providerCalls: 0 },
    runtimeThinkingLevel: { providerCalls: 1, providerThinking: "high" },
    googleAdmitted: { inputCountCalls: 1, modelCalls: 1 },
    googleUnsupportedSchema: { providerCalls: 0 },
    googleThinkingLevel: { providerCalls: 1, providerThinking: "LOW", proofThinking: "low" },
    googleThinkingLevelUnsupported: { providerCalls: 0 },
    googleThinkingLevelCallerIntent: { providerCalls: 1, providerThinking: "LOW" },
    googleControlsLegacy: { providerCalls: 1, proofThinking: "absent" },
    geminiFlashLowThinking: { providerCalls: 1, text: "HELLO" },
    paidCalls: 0,
  }));
} finally {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
