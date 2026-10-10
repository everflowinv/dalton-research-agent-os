"""Honor declared reasoning effort for explicit Dalton broker requests only.

OpenClaw 2026.9.9 can infer supportsReasoningEffort=False for a compatible
endpoint even when the selected descriptor declares supportedReasoningEfforts.
This patch supplies the missing flag on request-local model/transport copies.
Only reasoning models with a matching nonempty declared effort list qualify.
Explicit False, unsupported efforts, other plugins and interactive defaults keep
the original runtime mapping. Prepared lifecycle/provider bindings stay attached.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

SUPPORTED_VERSIONS = ("2026.9.9",)
HELPER_ANCHOR = "function createRuntimeLlm(options = {}) {"
CALL_ANCHOR = "\t\t\t\t\t\t\tmodel: prepared.model,\n\t\t\t\t\t\t\tauth: prepared.auth,"
PATCHED_CALL = CALL_ANCHOR.replace(
    "model: prepared.model,",
    "model: daltonBrokerModelForThinking(prepared.model, pluginPolicyId, requestedThinking),",
)
HELPER = '''// Dalton broker explicit reasoning capability, request-local v1.
function daltonBrokerModelForThinking(model, pluginPolicyId, requestedThinking) {
    if (pluginPolicyId !== "dalton-openclaw-model-broker" || requestedThinking === undefined || model.api !== "openai-completions" || model.reasoning !== true) return model;
    const declared = model.compat?.supportedReasoningEfforts;
    if (!Array.isArray(declared) || declared.length === 0 || !declared.includes(requestedThinking) || model.compat?.supportsReasoningEffort === false) return model;
    const refuse = (message) => { throw new Error(`Dalton broker reasoning capability: ${message}`); };
    const withCapability = (selected) => {
        const compat = selected.compat;
        if (compat?.supportsReasoningEffort === true) return selected;
        if (compat?.supportsReasoningEffort !== undefined) refuse("selected model has an invalid reasoning capability flag");
        const compatDescriptors = Object.getOwnPropertyDescriptors(compat ?? {});
        compatDescriptors.supportsReasoningEffort = {value: true, enumerable: true, writable: true, configurable: true};
        const copy = Object.create(Object.getPrototypeOf(compat ?? {}), compatDescriptors);
        const descriptors = Object.getOwnPropertyDescriptors(selected);
        descriptors.compat = {value: copy, enumerable: descriptors.compat?.enumerable ?? true, writable: true, configurable: true};
        return Object.create(Object.getPrototypeOf(selected), descriptors);
    };
    let result = withCapability(model);
    // OpenClaw's prepared transport is non-enumerably bound. Copy its capability
    // too: completion dispatch prefers that transport over the descriptor model.
    const symbols = Object.getOwnPropertySymbols(model).filter((key) => key.description === "openclaw.modelLlmRuntime");
    if (symbols.length > 1) refuse("prepared runtime binding is ambiguous");
    if (symbols.length === 1) {
        const key = symbols[0];
        const binding = model[key];
        if (binding?.completionTransport) {
            const transport = binding.completionTransport;
            // 2026.9.9 wraps provider hooks in a registered API whose first four
            // encoded parts still prove provider, model, source API and endpoint.
            const wrapperPrefix = "openclaw-provider-simple:" + [model.provider, model.id, model.api, model.baseUrl || "default"].map(encodeURIComponent).join(":") + ":";
            const sameApi = transport.api === model.api || (typeof transport.api === "string" && transport.api.startsWith(wrapperPrefix));
            if (!sameApi || transport.provider !== model.provider || transport.id !== model.id || transport.baseUrl !== model.baseUrl) refuse("prepared transport route differs from the selected model");
            if (transport.compat?.supportsReasoningEffort === false) return model;
            if (transport.compat?.supportedReasoningEfforts !== undefined && (!Array.isArray(transport.compat.supportedReasoningEfforts) || !transport.compat.supportedReasoningEfforts.includes(requestedThinking))) return model;
            const localTransport = withCapability(transport);
            if (localTransport !== transport) {
                const bindingDescriptors = Object.getOwnPropertyDescriptors(binding);
                bindingDescriptors.completionTransport = {...bindingDescriptors.completionTransport, value: localTransport};
                const localBinding = Object.create(Object.getPrototypeOf(binding), bindingDescriptors);
                const descriptors = Object.getOwnPropertyDescriptors(result);
                descriptors[key] = {...descriptors[key], value: localBinding};
                result = Object.create(Object.getPrototypeOf(result), descriptors);
            }
        }
    }
    return result;
}
'''

BINDING_MARKERS = (
    'const MODEL_LLM_RUNTIME = Symbol("openclaw.modelLlmRuntime");',
    'Object.defineProperty(bound, MODEL_LLM_RUNTIME, {',
    'enumerable: false',
    'function bindModelLlmRuntime(model, runtime, completionTransport)',
    'completionOwner: getModelCompletionOwner(model)',
    'return model[MODEL_LLM_RUNTIME]?.completionTransport;',
)
EXECUTION_MARKERS = (
    'const boundCompletionTransport = getModelCompletionTransport(params.model);',
    'boundCompletionTransport ?? prepareModelForSimpleCompletion({',
)
TRANSPORT_MARKERS = (
    'const PROVIDER_SIMPLE_COMPLETION_API_PREFIX = "openclaw-provider-simple:";',
    'function resolveProviderSimpleCompletionApi(model, auth, agentId)',
    'model.baseUrl || "default"',
    'if (auth) parts.push(auth.mode, auth.authFlow ?? "");',
    'return `${PROVIDER_SIMPLE_COMPLETION_API_PREFIX}${parts.map((part) => encodeURIComponent(part)).join(":")}`;',
    'const registeredStreamFn = (runtimeModel, context, options) => streamFn(projectModel(runtimeModel, { api: hookSourceApi }), context, options);',
)
RUNTIME_MARKERS = (
    'const requestedThinking = normalizeRuntimeThinkingLevel(params.thinkingLevel);',
    'const pluginPolicyId = resolvePluginPolicyId(options.authority, caller);',
    'const prepared = _usingCtx$1.a(preparation);',
    'const result = await completeWithPreparedSimpleCompletionModel({',
)


def target(root: Path) -> Path:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") not in SUPPORTED_VERSIONS:
        raise ValueError("broker reasoning patch supports OpenClaw 2026.9.9")
    matches = sorted((root / "dist").glob("runtime-llm.runtime-*.mjs"))
    if len(matches) != 1:
        raise ValueError(f"expected one runtime LLM bundle, found {len(matches)}")
    return matches[0]


def _assert_contract(root: Path, pattern: str, markers: tuple[str, ...]) -> None:
    matches = sorted((root / "dist").glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"expected one {pattern} contract bundle, found {len(matches)}")
    source = matches[0].read_text(encoding="utf-8")
    if any(marker not in source for marker in markers):
        raise ValueError(f"OpenClaw {pattern} binding/dispatch contract changed")


def render(root: Path) -> tuple[Path, bytes, str, bool]:
    path = target(root)
    original = path.read_bytes()
    source = original.decode("utf-8")
    _assert_contract(root, "model-runtime-binding-*.mjs", BINDING_MARKERS)
    _assert_contract(root, "simple-completion-execution-*.mjs", EXECUTION_MARKERS)
    transport_source = (root / "node_modules/@openclaw/ai/dist/transports.mjs").read_text(encoding="utf-8")
    if any(marker not in transport_source for marker in TRANSPORT_MARKERS):
        raise ValueError("OpenClaw prepared provider wrapper route contract changed")
    if any(source.count(marker) != 1 for marker in RUNTIME_MARKERS):
        raise ValueError("OpenClaw trusted broker thinking/selection contract changed")
    if source.count(HELPER_ANCHOR) != 1:
        raise ValueError("OpenClaw runtime helper anchor changed")
    if source.count(HELPER) == 1 and source.count(PATCHED_CALL) == 1:
        if source.count(CALL_ANCHOR) or source.count("function daltonBrokerModelForThinking(") != 1:
            raise ValueError("broker reasoning patch is duplicated or ambiguous")
        return path, original, source, False
    if "daltonBrokerModelForThinking" in source:
        raise ValueError("broker reasoning patch is partial or changed")
    if source.count(CALL_ANCHOR) != 1:
        raise ValueError("OpenClaw prepared completion call anchor changed")
    return path, original, source.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1).replace(CALL_ANCHOR, PATCHED_CALL, 1), True


def apply(root: Path, *, check: bool) -> bool:
    path, original, source, changed = render(root)
    if not changed:
        return False
    if check:
        raise ValueError("broker reasoning capability patch is missing")
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".mjs",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as handle:
            candidate = Path(handle.name)
            handle.write(source)
        checked = subprocess.run(["node", "--check", str(candidate)], capture_output=True, check=False)
        if checked.returncode:
            raise ValueError("patched runtime LLM bundle failed syntax validation")
        if path.read_bytes() != original:
            raise ValueError("OpenClaw runtime bundle changed during validation")
        os.chmod(candidate, path.stat().st_mode)
        os.replace(candidate, path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openclaw-root", type=Path)
    parser.add_argument("--apply", action="store_true", help="explicitly replace the host bundle; default is read-only check")
    args = parser.parse_args()
    try:
        root_arg = args.openclaw_root or os.environ.get("OPENCLAW_INSTALL_ROOT")
        if not root_arg:
            raise ValueError("--openclaw-root or OPENCLAW_INSTALL_ROOT is required")
        changed = apply(Path(root_arg).resolve(), check=not args.apply)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print("PATCH_CHANGED" if changed else "OK broker reasoning capability")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
