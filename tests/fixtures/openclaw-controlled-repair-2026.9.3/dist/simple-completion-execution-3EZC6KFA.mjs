import { i as isOpenAIProvider } from "./openai-routing-CPj7Gdxc.mjs";
import { d as resolveClaudeOpus5ModelIdentity, f as resolveClaudeSonnet5ModelIdentity } from "./anthropic-DfKaEfcc.mjs";
import "./src-DDmEryvj.mjs";
import { a as bindModelLlmRuntime, c as getModelLlmRuntime, n as completeSimple, s as getModelCompletionTransport } from "./stream-CzDmRkvi.mjs";
import { prepareModelForSimpleCompletion } from "@openclaw/ai/transports";
import { defaultApiRegistry } from "@openclaw/ai/internal/runtime";
import { reasoningTagTextPolicy, supportsOpenAIReasoningEffort } from "@openclaw/ai/internal/openai";
//#region src/agents/simple-completion-execution.ts
/** Executes an already-prepared model without importing model/auth preparation. */
async function completeWithPreparedSimpleCompletionModel(params) {
	const runtime = getModelLlmRuntime(params.model);
	const controlledTransport = params.options?.providerControls !== void 0;
	const boundCompletionTransport = getModelCompletionTransport(params.model);
	let completionModel = controlledTransport ? { ...params.model } : boundCompletionTransport ?? prepareModelForSimpleCompletion({
		apiRegistry: runtime?.registry ?? defaultApiRegistry,
		model: params.model,
		cfg: params.cfg
	});
	if (controlledTransport && getModelLlmRuntime(completionModel)) throw new Error("Controlled completion retained a host-bound transport");
	if (runtime && !controlledTransport) completionModel = bindModelLlmRuntime(completionModel, runtime);
	const { reasoning: rawReasoning, strictReasoningTags, ...options } = params.options ?? {};
	const reasoning = normalizeSimpleCompletionReasoning(rawReasoning, completionModel);
	const completionOptions = {
		...options,
		...reasoning ? { reasoning } : {},
		apiKey: params.auth.apiKey
	};
	if (strictReasoningTags) reasoningTagTextPolicy.markStrict(completionOptions);
	return await completeSimple(completionModel, params.context, completionOptions, params.assertCurrent);
}
function normalizeSimpleCompletionReasoning(reasoning, model) {
	switch (reasoning) {
		case void 0: return;
		case "off": return resolveClaudeSonnet5ModelIdentity(model) || resolveClaudeOpus5ModelIdentity(model) ? "off" : void 0;
		case "adaptive": return "medium";
		case "ultra":
		case "max": return isOpenAIProvider(model.provider) && supportsOpenAIReasoningEffort(model, "max") ? "max" : "xhigh";
		default: return reasoning;
	}
}
//#endregion
export { completeWithPreparedSimpleCompletionModel as t };
