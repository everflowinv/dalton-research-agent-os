// Excerpt of OpenClaw 2026.9.6 dist/runtime-llm.runtime-CRhsHHlN.mjs as seen by the
// repo host patches (after patch_openclaw_202609_llm, before provider_failure_bridge
// and provider_output_control_endpoint). Only the completion body is kept.
function createLlmCompleteError(code, message, cause) {
	return Object.assign(new Error(message, cause === void 0 ? void 0 : { cause }), {
		name: "LlmCompleteError",
		code
	});
}
async function completeExcerpt(params, cfg, agentId, pluginPolicyId, logger, audit, preferredProfile, requestedModelProfile) {
		const callerResult = createDeferredCore();
		captureAsyncWorkTracker()(async () => {
			try {
				var _usingCtx$1 = _usingCtx();
				const preparation = await acquireSimpleCompletionModelForAgent({
					cfg,
					agentId,
					modelRef: params.model,
					preferredProfile,
					...requestedModelProfile ? { bindAuthOwner: true } : {},
					allowBundledStaticCatalogFallback: true,
					allowMissingApiKeyModes: ["aws-sdk"],
					skipAgentDiscovery: true,
					signal: params.signal
				});
				if ("error" in preparation) throw new Error(`Plugin LLM completion failed: ${preparation.error}`);
				const prepared = _usingCtx$1.a(preparation);
        if (params.providerControls) {
            const expected = PROVIDER_CONTROLS_CAPABILITY.transports[params.providerControls.mode];
            if (!expected) throw new Error("Plugin LLM completion failed: provider control mode is unsupported.");
            if (expected !== `${prepared.model.provider}/${prepared.model.api}`) throw new Error(params.providerControls.mode === "openai-responses-input-count-v1" ? "Plugin LLM completion failed: provider controls require native OpenAI Responses." : "Plugin LLM completion failed: provider controls require native Google Generative AI.");
        }
				const work = new AsyncWorkScope();
				try {
					callerResult.resolve(await work.track(async () => {
						if (params.requiredAuthMode && prepared.auth.mode !== params.requiredAuthMode) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion selected a credential with the wrong authentication mode.");
						if (requestedModelProfile && prepared.auth.profileId !== requestedModelProfile) throw createLlmCompleteError("LLM_COMPLETION_NOT_AUTHORIZED", "Plugin LLM completion selected a different authentication profile.");
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
								...params.responseFormat !== void 0 ? { responseFormat: params.responseFormat } : {},
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
								responseModel: result.responseModel,
								stopReason: result.stopReason,
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
					}));
				} catch (error) {
					callerResult.reject(error);
				} finally {
					await work.drain();
				}
			} catch (_) {
				_usingCtx$1.e = _;
			} finally {
				await _usingCtx$1.d();
			}
		}).catch((error) => callerResult.reject(error));
		return await callerResult.promise;
}
export { completeExcerpt };
