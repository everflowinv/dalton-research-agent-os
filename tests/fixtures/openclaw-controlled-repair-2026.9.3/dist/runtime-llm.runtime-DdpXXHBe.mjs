import { c as asFiniteNumberInRange, s as asFiniteNumber } from "./number-coercion-CLj0HTDM.mjs";
import "./src-DqBJ2aW2.mjs";
import { l as normalizeOptionalString } from "./string-coerce-CIXf7egm.mjs";
import { n as normalizeAgentId } from "./agent-id-GA8mwdTG.mjs";
import "./session-key-DKGa_yQH.mjs";
import { a as getChildLogger } from "./logger-BT9Zboly.mjs";
import { I as markHostPluginUsageDiagnosticEvent, f as isDiagnosticsEnabled, o as emitTrustedDiagnosticEvent } from "./diagnostic-events-DVk335S_.mjs";
import { l as normalizePluginsConfig } from "./config-state-BshQs2ME.mjs";
import { t as modelKey } from "./model-key-2xbDA5NJ.mjs";
import { s as normalizeModelRef } from "./model-ref-shared-BaD6pVOB.mjs";
import { r as buildConfiguredModelCatalog } from "./model-selection-shared-T-i8X-YJ.mjs";
import { t as splitTrailingAuthProfile } from "./model-ref-profile-BIKs-96s.mjs";
import { a as getPluginRuntimeGatewayRequestScope } from "./gateway-request-scope-ByAkGT2-.mjs";
import "./logging-qzYeujDH.mjs";
import { c as normalizeUsage } from "./usage-BpC2Ujh-.mjs";
import { c as resolveThinkingProfile } from "./thinking-DXBjerSL.mjs";
import { o as resolveEffectiveAgentRuntime } from "./thinking-runtime-C3esvTaK.mjs";
import { a as resolveModelCostConfig, n as estimateUsageCost, t as estimateAggregateUsageCost } from "./usage-format-DWHUXcFg.mjs";
import { t as compileModelAllowlist } from "./model-allowlist-ChW3lzbE.mjs";
//#region src/plugins/runtime/runtime-llm-error.ts
function createLlmCompleteError(code, message, cause) {
	return Object.assign(new Error(message, cause === void 0 ? void 0 : { cause }), {
		name: "LlmCompleteError",
		code
	});
}
//#endregion
//#region src/plugins/runtime/runtime-llm-isolated.ts
const MAX_TIMER_DELAY_MS = 2147483647;
function requireIsolatedUserPrompt(params) {
	if (params.execution?.mode !== "isolated-agent-runtime" || !Array.isArray(params.messages) || params.messages.length !== 1 || params.messages[0]?.role !== "user" || typeof params.messages[0].content !== "string") throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", "Isolated agent-runtime completion requires exactly one user message; pass system instructions through systemPrompt.");
	return params.messages[0].content;
}
function isIsolatedAgentRuntimeRequest(params) {
	return params.execution?.mode === "isolated-agent-runtime";
}
function assertSupportedExecutionMode(params) {
	const execution = params.execution;
	if (execution === void 0) return;
	if (!execution || typeof execution !== "object" || Array.isArray(execution) || execution.mode !== "isolated-agent-runtime") throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", "Plugin LLM completion execution.mode must be \"isolated-agent-runtime\" when execution is provided.");
}
function resolveIsolatedTimeoutMs(value) {
	if (value === void 0) return 3e4;
	const timeoutMs = asFiniteNumber(value);
	if (timeoutMs === void 0 || !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_TIMER_DELAY_MS) throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", `Isolated agent-runtime completion timeoutMs must be an integer from 1 through ${MAX_TIMER_DELAY_MS}.`);
	return timeoutMs;
}
function assertIsolatedReasoningSupported(params) {
	if (params.reasoning === void 0) return;
	const catalog = buildConfiguredModelCatalog({ cfg: params.cfg });
	const profile = resolveThinkingProfile({
		provider: params.provider,
		model: params.model,
		agentRuntime: resolveEffectiveAgentRuntime({
			cfg: params.cfg,
			agentId: params.agentId,
			provider: params.provider,
			modelId: params.model
		}),
		...catalog.length > 0 ? { catalog } : {}
	});
	if (profile.levels.some((level) => level.id === params.reasoning)) return;
	throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", `Thinking level "${params.reasoning}" is not supported for ${params.provider}/${params.model}. Use one of: ${profile.levels.map((level) => level.label).join(", ")}.`);
}
async function runIsolatedAgentRuntimeCompletion(params) {
	const prompt = requireIsolatedUserPrompt(params.request);
	const timeoutMs = resolveIsolatedTimeoutMs(params.request.execution.timeoutMs);
	assertIsolatedReasoningSupported({
		cfg: params.cfg,
		agentId: params.agentId,
		provider: params.provider,
		model: params.model,
		reasoning: params.request.reasoning
	});
	const controller = new AbortController();
	let timedOut = false;
	const abortFromCaller = () => controller.abort(params.request.signal?.reason);
	if (params.request.signal?.aborted) throw createLlmCompleteError("LLM_COMPLETION_ABORTED", "Plugin LLM completion was aborted.");
	params.request.signal?.addEventListener("abort", abortFromCaller, { once: true });
	const timer = setTimeout(() => {
		timedOut = true;
		controller.abort(/* @__PURE__ */ new Error(`Isolated completion timed out after ${timeoutMs}ms.`));
	}, timeoutMs);
	timer.unref?.();
	let rejectOnAbort;
	const abortPromise = new Promise((_resolve, reject) => {
		rejectOnAbort = () => {
			const reason = controller.signal.reason;
			reject(reason instanceof Error ? reason : /* @__PURE__ */ new Error("Isolated completion was aborted."));
		};
		controller.signal.addEventListener("abort", rejectOnAbort, { once: true });
	});
	try {
		const operation = (async () => {
			const { runIsolatedCompletion } = await import("./isolated-completion-BlK5S39e.mjs");
			return await runIsolatedCompletion({
				config: params.cfg,
				provider: params.provider,
				model: params.model,
				authProfileId: params.authProfileId,
				agentId: params.agentId,
				systemPrompt: params.request.systemPrompt ?? "",
				prompt,
				timeoutMs,
				abortSignal: controller.signal,
				thinkLevel: params.request.reasoning,
				streamParams: {
					maxTokens: asFiniteNumber(params.request.maxTokens),
					temperature: asFiniteNumber(params.request.temperature)
				}
			});
		})();
		return await Promise.race([operation, abortPromise]);
	} catch (error) {
		if (timedOut) throw createLlmCompleteError("LLM_COMPLETION_TIMEOUT", `Plugin LLM completion timed out after ${timeoutMs}ms.`, error);
		if (params.request.signal?.aborted) throw createLlmCompleteError("LLM_COMPLETION_ABORTED", "Plugin LLM completion was aborted.", error);
		const isolatedError = error;
		if (isolatedError.code === "unsupported") throw createLlmCompleteError("LLM_ISOLATED_UNSUPPORTED", typeof isolatedError.message === "string" ? isolatedError.message : "Configured agent runtime does not support isolated completion.", error);
		if (isolatedError.code === "runtime-unavailable") throw createLlmCompleteError("LLM_RUNTIME_UNAVAILABLE", typeof isolatedError.message === "string" ? isolatedError.message : "Configured agent runtime is unavailable.", error);
		if (isolatedError.code === "input-rejected") throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", typeof isolatedError.message === "string" ? isolatedError.message : "Isolated completion input was rejected.", error);
		if (isolatedError.code === "output-rejected") throw createLlmCompleteError("LLM_COMPLETION_OUTPUT_REJECTED", typeof isolatedError.message === "string" ? isolatedError.message : "Isolated completion output was rejected.", error);
		throw createLlmCompleteError("LLM_COMPLETION_FAILED", "Plugin LLM completion failed.", error);
	} finally {
		clearTimeout(timer);
		if (rejectOnAbort) controller.signal.removeEventListener("abort", rejectOnAbort);
		params.request.signal?.removeEventListener("abort", abortFromCaller);
	}
}
//#endregion
//#region src/plugins/runtime/runtime-llm.runtime.ts
const defaultLogger = getChildLogger({ capability: "runtime.llm" });
function toRuntimeLogger(logger) {
	return {
		debug: (message, meta) => logger.debug?.(meta, message),
		info: (message, meta) => logger.info(meta, message),
		warn: (message, meta) => logger.warn(meta, message),
		error: (message, meta) => logger.error(meta, message)
	};
}
function normalizeCaller(caller, fallback) {
	const source = caller ?? fallback;
	if (!source) return { kind: "unknown" };
	return {
		kind: source.kind,
		...normalizeOptionalString(source.id) ? { id: source.id.trim() } : {},
		...normalizeOptionalString(source.name) ? { name: source.name.trim() } : {}
	};
}
function resolveTrustedCaller(authority) {
	if (authority?.caller?.kind === "context-engine") return normalizeCaller(authority.caller);
	const scope = getPluginRuntimeGatewayRequestScope();
	const scopedPluginId = normalizeOptionalString(scope?.pluginId);
	if (scopedPluginId) return {
		kind: "plugin",
		id: scopedPluginId
	};
	return normalizeCaller(authority?.caller);
}
function resolveRuntimeConfig(options) {
	const cfg = options.getConfig?.();
	if (!cfg) throw new Error("Plugin LLM completion requires an injected runtime config scope.");
	return cfg;
}
async function resolveAgentId(params) {
	const authorityAgentIdRaw = normalizeOptionalString(params.authority?.agentId);
	const requestedAgentIdRaw = normalizeOptionalString(params.request.agentId);
	const authorityAgentId = authorityAgentIdRaw ? normalizeAgentId(authorityAgentIdRaw) : void 0;
	const requestedAgentId = requestedAgentIdRaw ? normalizeAgentId(requestedAgentIdRaw) : void 0;
	if (params.authority?.requiresBoundAgent && !authorityAgentId) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion is not bound to an active session agent.");
	if (authorityAgentId) {
		if (requestedAgentId && requestedAgentId !== authorityAgentId && !params.allowAgentIdOverride) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion cannot override the active session agent.");
		return authorityAgentId;
	}
	if (requestedAgentId) {
		if (!params.allowAgentIdOverride) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion cannot override the target agent.");
		return requestedAgentId;
	}
	const { resolveAmbientOwnerAgentId } = await import("./agent-scope-BXrHg6e5.mjs");
	return resolveAmbientOwnerAgentId(params.cfg);
}
function buildSystemPrompt(params) {
	const segments = [normalizeOptionalString(params.systemPrompt), ...params.messages.filter((message) => message.role === "system").map((message) => normalizeOptionalString(message.content))].filter((segment) => Boolean(segment));
	return segments.length > 0 ? segments.join("\n\n") : void 0;
}
function buildMessages(params) {
	const now = Date.now();
	return params.request.messages.filter((message) => message.role !== "system").map((message) => message.role === "user" ? {
		role: "user",
		content: message.content,
		timestamp: now
	} : {
		role: "assistant",
		content: [{
			type: "text",
			text: message.content
		}],
		api: params.api,
		provider: params.provider,
		model: params.model,
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				total: 0
			}
		},
		stopReason: "stop",
		timestamp: now
	});
}
function readFiniteNonNegativeNumber(value) {
	return asFiniteNumberInRange(value, { min: 0 });
}
function readExplicitCostUsd(raw) {
	if (!raw || typeof raw !== "object" || Array.isArray(raw)) return;
	const cost = raw.cost;
	if (typeof cost === "number") return readFiniteNonNegativeNumber(cost);
	if (!cost || typeof cost !== "object" || Array.isArray(cost)) return;
	return readFiniteNonNegativeNumber(cost.totalUsd) ?? readFiniteNonNegativeNumber(cost.total);
}
function finalizePluginLlmCompletion(params) {
	const normalized = normalizeUsage(params.rawUsage);
	const costConfig = resolveModelCostConfig({
		provider: params.result.provider,
		model: params.result.model,
		config: params.cfg
	});
	const estimateCost = params.result.execution.mode === "direct-provider" ? estimateUsageCost : estimateAggregateUsageCost;
	const costUsd = readExplicitCostUsd(params.rawUsage) ?? estimateCost({
		usage: normalized,
		cost: costConfig
	});
	const usage = {
		...normalized?.input !== void 0 ? { inputTokens: normalized.input } : {},
		...normalized?.output !== void 0 ? { outputTokens: normalized.output } : {},
		...normalized?.cacheRead !== void 0 ? { cacheReadTokens: normalized.cacheRead } : {},
		...normalized?.cacheWrite !== void 0 ? { cacheWriteTokens: normalized.cacheWrite } : {},
		...normalized?.total !== void 0 ? { totalTokens: normalized.total } : {},
		...costUsd !== void 0 ? { costUsd } : {}
	};
	(params.logger ?? toRuntimeLogger(defaultLogger)).info("plugin llm completion", {
		caller: params.result.audit.caller,
		purpose: params.result.audit.purpose,
		sessionKey: params.result.audit.sessionKey,
		agentId: params.result.agentId,
		provider: params.result.provider,
		model: params.result.model,
		executionMode: params.result.execution.mode,
		executionOwner: params.result.execution.owner,
		usage
	});
	const input = normalized?.input ?? 0;
	const output = normalized?.output ?? 0;
	const cacheRead = normalized?.cacheRead ?? 0;
	const cacheWrite = normalized?.cacheWrite ?? 0;
	const promptTokens = input + cacheRead + cacheWrite;
	const total = normalized?.total ?? promptTokens + output;
	const hasPositiveUsage = [
		input,
		output,
		cacheRead,
		cacheWrite,
		total,
		usage.costUsd
	].some((value) => typeof value === "number" && Number.isFinite(value) && value > 0);
	if (params.suppressUsage !== true && isDiagnosticsEnabled(params.cfg) && hasPositiveUsage) emitTrustedDiagnosticEvent(markHostPluginUsageDiagnosticEvent({
		type: "model.usage",
		...params.result.audit.sessionKey ? { sessionKey: params.result.audit.sessionKey } : {},
		agentId: params.result.agentId,
		provider: params.result.provider,
		model: params.result.model,
		usage: {
			input,
			output,
			cacheRead,
			cacheWrite,
			promptTokens,
			total
		},
		...usage.costUsd !== void 0 ? { costUsd: usage.costUsd } : {}
	}, params.hostPluginId));
	return {
		...params.result,
		usage
	};
}
function buildPolicyFromEntry(entry) {
	return {
		allowAgentIdOverride: entry.allowAgentIdOverride === true,
		allowModelOverride: entry.allowModelOverride === true,
		allowAuthProfileOverride: entry.allowAuthProfileOverride === true,
		overrideModels: compileModelAllowlist({
			configured: entry.hasAllowedModelsConfig === true,
			values: entry.allowedModels,
			formatKey: modelKey
		}),
		completionModels: compileModelAllowlist({
			configured: entry.hasAllowedCompletionModelsConfig === true,
			values: entry.allowedCompletionModels,
			formatKey: modelKey
		})
	};
}
function resolvePluginPolicyId(authority, caller) {
	const authorityPluginId = normalizeOptionalString(authority?.pluginIdForPolicy);
	if (authorityPluginId) return authorityPluginId;
	if (caller.kind !== "plugin") return;
	return normalizeOptionalString(caller.id);
}
function resolvePluginLlmPolicy(cfg, pluginId) {
	if (!pluginId) return;
	const entry = normalizePluginsConfig(cfg.plugins).entries[pluginId]?.llm;
	return entry ? buildPolicyFromEntry(entry) : void 0;
}
function resolveAuthorityModelPolicy(authority) {
	if (authority?.allowAgentIdOverride !== true && authority?.allowModelOverride !== true && authority?.allowAuthProfileOverride !== true && authority?.allowedModels === void 0 && authority?.allowedCompletionModels === void 0) return;
	return buildPolicyFromEntry({
		allowAgentIdOverride: authority.allowAgentIdOverride,
		allowModelOverride: authority.allowModelOverride,
		allowAuthProfileOverride: authority.allowAuthProfileOverride,
		hasAllowedModelsConfig: authority.allowedModels !== void 0,
		allowedModels: authority.allowedModels,
		hasAllowedCompletionModelsConfig: authority.allowedCompletionModels !== void 0,
		allowedCompletionModels: authority.allowedCompletionModels
	});
}
function assertAllowedAuthProfileOverride(params) {
	if (!params.authProfileId) return;
	if (params.authorityPolicy?.allowAuthProfileOverride === true || params.pluginPolicy?.allowAuthProfileOverride === true) return;
	throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion cannot override the auth profile. Enable plugins.entries.<id>.llm.allowAuthProfileOverride to authorize it.");
}
function assertModelAllowed(params) {
	const allowlist = params.kind === "override" ? params.policy?.overrideModels : params.policy?.completionModels;
	if (!allowlist?.configured || allowlist.allowAny) return;
	const target = params.kind === "override" ? "model override" : "model";
	if (allowlist.models.size === 0) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", `Plugin LLM completion ${target} allowlist has no valid models.`);
	if (!params.resolvedModelRef) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", `Plugin LLM completion ${target} allowlist requires a resolvable provider/model target.`);
	if (!allowlist.models.has(params.resolvedModelRef)) {
		const owner = params.policyOwnerPluginId ? ` for plugin "${params.policyOwnerPluginId}"` : "";
		const usage = params.kind === "completion" ? " for completions" : "";
		throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", `Plugin LLM completion ${target} "${params.resolvedModelRef}" is not allowlisted${usage}${owner}.`);
	}
}
function assertAllowedModelOverride(params) {
	if (params.authorityPolicy?.allowModelOverride !== true && params.pluginPolicy?.allowModelOverride !== true) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion cannot override the target model.");
	assertModelAllowed({
		kind: "override",
		resolvedModelRef: params.resolvedModelRef,
		policy: params.authorityPolicy
	});
	assertModelAllowed({
		kind: "override",
		resolvedModelRef: params.resolvedModelRef,
		policy: params.pluginPolicy,
		policyOwnerPluginId: params.pluginPolicyId
	});
}
/**
* Create the host-owned generic LLM completion runtime for trusted plugin callers.
*/
const PROVIDER_CONTROLS_CAPABILITY = Object.freeze({
    version: "0.1",
    modes: Object.freeze(["openai-responses-input-count-v1", "google-generative-ai-count-tokens-v1"]),
    structuredOutput: "json-schema-strict",
    inputTokens: "provider-count-endpoint",
    outputTokens: "provider-max-output-tokens",
    totalTokens: "count-plus-output-reservation",
    cost: "expiring-rate-card-reservation",
    transport: "openai/openai-responses",
    transports: Object.freeze({
        "openai-responses-input-count-v1": "openai/openai-responses",
        "google-generative-ai-count-tokens-v1": "google/google-generative-ai"
    })
});
const THINKING_LEVEL_CAPABILITY = Object.freeze({version: "0.1", levels: Object.freeze(["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"])});
const DALTON_LLM_CAPABILITIES = Object.freeze({providerControls: PROVIDER_CONTROLS_CAPABILITY, thinkingLevel: THINKING_LEVEL_CAPABILITY});
function normalizeRuntimeThinkingLevel(value) {
    if (value === void 0) return void 0;
    if (typeof value !== "string" || !THINKING_LEVEL_CAPABILITY.levels.includes(value)) throw new Error("Plugin LLM completion thinkingLevel is unsupported.");
    return value;
}
function createRuntimeLlm(options = {}) {
	const logger = options.logger ?? toRuntimeLogger(defaultLogger);
	return { capabilities: DALTON_LLM_CAPABILITIES, complete: async (params) => {
		const caller = resolveTrustedCaller(options.authority);
		if (options.authority?.allowComplete === false) {
			const reason = options.authority.denyReason ?? "capability denied";
			logger.warn("plugin llm completion denied", {
				caller,
				purpose: params.purpose,
				reason
			});
			throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", `Plugin LLM completion denied: ${reason}`);
		}
		assertSupportedExecutionMode(params);
        if (params.providerControls && params.execution !== void 0) throw new Error("Provider controls require direct-provider execution.");
        const requestedThinking = normalizeRuntimeThinkingLevel(params.thinkingLevel);
        if (requestedThinking !== void 0 && params.reasoning !== void 0 && params.reasoning !== requestedThinking) throw new Error("Conflicting thinkingLevel and reasoning.");
        if (requestedThinking !== void 0) params = {...params, reasoning: requestedThinking};
		const [{ prepareSimpleCompletionModelForAgent, completeWithPreparedSimpleCompletionModel, resolveSimpleCompletionSelectionForAgent }, cfg] = await Promise.all([import("./simple-completion-runtime-BOVBy4or.mjs"), Promise.resolve(resolveRuntimeConfig(options))]);
		const pluginPolicyId = resolvePluginPolicyId(options.authority, caller);
		const pluginPolicy = resolvePluginLlmPolicy(cfg, pluginPolicyId);
		const authorityPolicy = resolveAuthorityModelPolicy(options.authority);
		const preferredProfile = normalizeOptionalString(options.authority?.preferredProfile);
		const audit = {
			caller,
			...params.purpose ? { purpose: params.purpose } : {},
			...options.authority?.sessionKey ? { sessionKey: options.authority.sessionKey } : {}
		};
		const agentId = await resolveAgentId({
			request: params,
			cfg,
			authority: options.authority,
			allowAgentIdOverride: options.authority?.allowAgentIdOverride === false ? false : authorityPolicy?.allowAgentIdOverride === true || pluginPolicy?.allowAgentIdOverride === true
		});
		const requestedModel = normalizeOptionalString(params.model);
		const requestedModelProfile = requestedModel ? normalizeOptionalString(splitTrailingAuthProfile(requestedModel).profile) : void 0;
		const selection = resolveSimpleCompletionSelectionForAgent({
			cfg,
			agentId,
			modelRef: requestedModel
		});
		if (!selection) throw createLlmCompleteError("LLM_COMPLETION_FAILED", `No model configured for agent ${agentId}.`);
		const normalizedSelection = normalizeModelRef(selection.provider, selection.modelId);
		const resolvedModelRef = modelKey(normalizedSelection.provider, normalizedSelection.model);
		assertModelAllowed({
			kind: "completion",
			resolvedModelRef,
			policy: authorityPolicy
		});
		assertModelAllowed({
			kind: "completion",
			resolvedModelRef,
			policy: pluginPolicy,
			policyOwnerPluginId: pluginPolicyId
		});
		if (requestedModel) assertAllowedModelOverride({
			resolvedModelRef,
			pluginPolicyId,
			authorityPolicy,
			pluginPolicy
		});
		const isolatedRequest = isIsolatedAgentRuntimeRequest(params);
		const executionProfile = isolatedRequest ? normalizeOptionalString(params.execution.authProfileId) : void 0;
		const modelProfile = normalizeOptionalString(selection.profileId);
		if (executionProfile && requestedModelProfile && executionProfile !== requestedModelProfile) throw createLlmCompleteError("LLM_ISOLATED_INPUT_REJECTED", "Isolated completion received conflicting auth profiles in model and execution.authProfileId.");
		if (isolatedRequest) {
			assertAllowedAuthProfileOverride({
				authProfileId: executionProfile ?? requestedModelProfile,
				authorityPolicy,
				pluginPolicy
			});
			const result = await runIsolatedAgentRuntimeCompletion({
				request: params,
				cfg,
				agentId,
				provider: selection.provider,
				model: selection.modelId,
				authProfileId: executionProfile ?? requestedModelProfile ?? preferredProfile ?? modelProfile
			});
			return finalizePluginLlmCompletion({
				cfg,
				hostPluginId: pluginPolicyId,
				rawUsage: result.usage,
				logger,
				result: {
					text: result.text,
					provider: result.provider,
					model: result.model,
					agentId,
					execution: {
						mode: params.execution.mode,
						owner: result.owner
					},
					audit
				}
			});
		}
		const prepared = await prepareSimpleCompletionModelForAgent({
			cfg,
			agentId,
			modelRef: params.model,
			preferredProfile,
			allowBundledStaticCatalogFallback: true,
			allowMissingApiKeyModes: ["aws-sdk"],
			skipAgentDiscovery: true
		});
		if ("error" in prepared) throw new Error(`Plugin LLM completion failed: ${prepared.error}`);
        if (params.providerControls) {
            const expected = PROVIDER_CONTROLS_CAPABILITY.transports[params.providerControls.mode];
            if (!expected) throw new Error("Plugin LLM completion failed: provider control mode is unsupported.");
            if (expected !== `${prepared.model.provider}/${prepared.model.api}`) throw new Error(params.providerControls.mode === "openai-responses-input-count-v1" ? "Plugin LLM completion failed: provider controls require native OpenAI Responses." : "Plugin LLM completion failed: provider controls require native Google Generative AI.");
        }
		const context = {
			systemPrompt: buildSystemPrompt(params),
			messages: buildMessages({
				request: params,
				provider: prepared.model.provider,
				model: prepared.model.id,
				api: prepared.model.api
			})
		};
		let providerControlProof;
		const result = await completeWithPreparedSimpleCompletionModel({
			model: prepared.model,
			auth: prepared.auth,
			cfg,
			context,
			options: {
				maxTokens: asFiniteNumber(params.maxTokens),
				temperature: asFiniteNumber(params.temperature),
				...params.reasoning !== void 0 ? { reasoning: params.reasoning } : {},
                signal: params.signal,
                providerControls: params.providerControls,
                maxRetries: params.providerControls ? 0 : void 0,
                onProviderControlProof: params.providerControls ? (proof) => { providerControlProof = proof; } : void 0
			}
		});
		if (params.providerControls && !providerControlProof) throw new Error("Plugin LLM completion failed: provider controls were not enforced by the selected transport.");
		const text = result.content.filter((c) => c.type === "text").map((c) => c.text).join("");
		return finalizePluginLlmCompletion({
			cfg,
			hostPluginId: pluginPolicyId,
			suppressUsage: !text.trim() || ![
				"stop",
				"length",
				"toolUse"
			].includes(result.stopReason),
			rawUsage: result.usage,
			logger,
			result: {
				text,
				...providerControlProof ? { providerControlProof } : {},
				provider: prepared.selection.provider,
				model: prepared.selection.modelId,
				agentId,
				execution: {
					mode: "direct-provider",
					owner: {
						kind: "provider",
						id: prepared.selection.provider
					}
				},
				audit
			}
		});
	} };
}
//#endregion
export { createRuntimeLlm, finalizePluginLlmCompletion };
