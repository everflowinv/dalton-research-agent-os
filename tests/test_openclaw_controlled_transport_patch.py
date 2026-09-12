from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from integrations.openclaw_host_patches.patch_controlled_completion_transport import (
    ORIGINAL, ORIGINAL_BIND, PATCHED, PATCHED_BIND, SUPPORTED_VERSION, apply,
)
from integrations.openclaw_host_patches.patch_provider_output_control_endpoint import (
    apply as check_provider_output_endpoint,
)


MODULE = r'''
let unsafeNetworkCalls = 0;
let safeCountCalls = 0;
let safeGenerateCalls = 0;
const RUNTIME = Symbol("runtime");
function getModelLlmRuntime(model) { return model[RUNTIME]; }
function getModelCompletionTransport() {
  return { runtime: { completeSimple: async () => { unsafeNetworkCalls += 1; return {}; } } };
}
function prepareModelForSimpleCompletion({model}) { return model; }
function bindModelLlmRuntime(model, runtime) {
  const copy={...model}; Object.defineProperty(copy,RUNTIME,{value:runtime}); return copy;
}
const defaultApiRegistry = {};
async function completeSimple(model, context, options) {
  const runtime=getModelLlmRuntime(model);
  if (runtime) return runtime.completeSimple(model, context, options);
  if (!options.providerControls) throw new Error("controls missing");
  safeCountCalls += 1;
  if (options.providerControls.rejectAfterCount) throw new Error("admission refused");
  safeGenerateCalls += 1;
  options.onProviderControlProof?.({mode: options.providerControls.mode});
  return {content: [{type:"text", text:"{}"}]};
}
function normalizeSimpleCompletionReasoning(value) { return value; }
async function completeWithPreparedSimpleCompletionModel(params) {
 const runtime = getModelLlmRuntime(params.model);
ORIGINAL_SELECTION
BIND_SELECTION
 const {reasoning: rawReasoning, ...options} = params.options ?? {};
 const reasoning = normalizeSimpleCompletionReasoning(rawReasoning, completionModel);
 return await completeSimple(completionModel, params.context, {...options, ...(reasoning ? {reasoning} : {})});
}
const model = {};
Object.defineProperty(model,RUNTIME,{value:{registry:{},completeSimple:async()=>{
 unsafeNetworkCalls += 1; return {};
}}});
let proof;
await completeWithPreparedSimpleCompletionModel({model, context:{}, cfg:{}, options:{
 providerControls:{mode:"google-generative-ai-count-tokens-v1"},
 onProviderControlProof:value=>{proof=value;}
}});
try { await completeWithPreparedSimpleCompletionModel({model, context:{}, cfg:{}, options:{
 providerControls:{mode:"google-generative-ai-count-tokens-v1", rejectAfterCount:true}
}}); } catch {}
console.log(JSON.stringify({unsafeNetworkCalls,safeCountCalls,safeGenerateCalls,proof}));
'''


class ControlledTransportPatchTests(unittest.TestCase):
    def fixture(self, root: Path, *, version: str = SUPPORTED_VERSION) -> Path:
        (root / "dist").mkdir(parents=True)
        (root / "package.json").write_text(
            json.dumps({"version": version}), encoding="utf-8")
        path = root / "dist" / "simple-completion-execution-fixture.mjs"
        source = MODULE.replace("ORIGINAL_SELECTION", ORIGINAL)
        path.write_text(source.replace("BIND_SELECTION", ORIGINAL_BIND), encoding="utf-8")
        return path

    def test_controlled_request_bypasses_bound_transport_and_fails_before_generate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = self.fixture(root)
            self.assertTrue(apply(root, check=False))
            self.assertIn(PATCHED, module.read_text(encoding="utf-8"))
            self.assertIn(PATCHED_BIND, module.read_text(encoding="utf-8"))
            result = subprocess.run(
                ["node", str(module)], text=True, capture_output=True, check=True)
            observed = json.loads(result.stdout)
            self.assertEqual(observed["unsafeNetworkCalls"], 0)
            self.assertEqual(observed["safeCountCalls"], 2)
            self.assertEqual(observed["safeGenerateCalls"], 1)
            self.assertEqual(
                observed["proof"]["mode"],
                "google-generative-ai-count-tokens-v1",
            )

    def test_patch_is_idempotent_and_check_detects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertTrue(apply(root, check=False))
            self.assertFalse(apply(root, check=False))
            self.assertFalse(apply(root, check=True))

    def test_cli_accepts_patch_runner_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            script = Path(__file__).parents[1] / "integrations" / (
                "openclaw_host_patches/patch_controlled_completion_transport.py")
            env = {**__import__("os").environ,
                   "OPENCLAW_INSTALL_ROOT": str(root)}
            applied = subprocess.run(
                ["python3", str(script)], env=env, text=True,
                capture_output=True, check=False)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            checked = subprocess.run(
                ["python3", str(script), "--check"], env=env, text=True,
                capture_output=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_an_unreviewed_openclaw_version_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, version="2026.9.4")
            with self.assertRaisesRegex(ValueError, "supports OpenClaw"):
                apply(root, check=False)

    def test_partial_and_duplicate_anchors_are_refused(self):
        for suffix in (PATCHED, PATCHED + "\n" + PATCHED_BIND):
            with self.subTest(suffix=suffix[:20]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.fixture(root)
                path.write_text(path.read_text(encoding="utf-8") + suffix,
                                encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "partial|duplicate"):
                    apply(root, check=True)

    def test_copied_installed_module_uses_native_seam_for_controls(self):
        installed = sorted(Path.home().glob(
            ".openclaw/tools/node-*/lib/node_modules/openclaw/dist/"
            "simple-completion-execution-*.mjs"
        ))
        if not installed:
            self.skipTest("installed OpenClaw completion module unavailable")
        source = installed[-1].read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "package.json").write_text(
                json.dumps({"version": SUPPORTED_VERSION}), encoding="utf-8")
            module = root / "dist" / installed[-1].name
            module.write_text(source, encoding="utf-8")
            apply(root, check=False)
            patched = module.read_text(encoding="utf-8")
            region = patched.index("//#region")
            support = root / "dist" / "support.mjs"
            support.write_text(r'''
const binding = Symbol("binding");
export const observed = { unsafe: 0, count: 0, generate: 0, proof: null };
export const defaultApiRegistry = {};
export const reasoningTagTextPolicy = { markStrict() {} };
export const isOpenAIProvider = () => false;
export const supportsOpenAIReasoningEffort = () => false;
export const resolveClaudeOpus5ModelIdentity = () => false;
export const resolveClaudeSonnet5ModelIdentity = () => false;
export function getModelLlmRuntime(model) { return model[binding]?.runtime; }
export function getModelCompletionTransport(model) { return model[binding]?.transport; }
export function bindModelLlmRuntime(model, runtime, transport) {
 const copy={...model}; Object.defineProperty(copy,binding,{value:{runtime,transport}}); return copy;
}
export function prepareModelForSimpleCompletion({model}) { return model; }
export async function completeSimple(model, _context, options) {
 const held=getModelLlmRuntime(model);
 if (held) return held.completeSimple();
 observed.count += 1;
 if (options.providerControls.rejectAfterCount) throw new Error("admission refused");
 observed.generate += 1;
 observed.proof={mode:options.providerControls.mode};
 options.onProviderControlProof?.(observed.proof);
 return {content:[{type:"text",text:"{}"}]};
}
export function controlledModel() {
 const model={provider:"google"};
 return bindModelLlmRuntime(model,{registry:{},completeSimple:async()=>{observed.unsafe+=1;return{};}},{provider:"unsafe"});
}
''', encoding="utf-8")
            imports = '''import { observed, defaultApiRegistry, reasoningTagTextPolicy,
 isOpenAIProvider, supportsOpenAIReasoningEffort,
 resolveClaudeOpus5ModelIdentity, resolveClaudeSonnet5ModelIdentity,
 getModelLlmRuntime, getModelCompletionTransport, bindModelLlmRuntime,
 prepareModelForSimpleCompletion, completeSimple, controlledModel } from "./support.mjs";\n'''
            module.write_text(imports + patched[region:], encoding="utf-8")
            runner = root / "dist" / "run.mjs"
            runner.write_text(f'''
import {{t as complete}} from "./{module.name}";
import {{observed,controlledModel}} from "./support.mjs";
const model=controlledModel(); let proof;
await complete({{model,auth:{{apiKey:"fake"}},cfg:{{}},context:{{}},options:{{providerControls:{{mode:"google-generative-ai-count-tokens-v1"}},onProviderControlProof:v=>proof=v}}}});
try {{ await complete({{model,auth:{{apiKey:"fake"}},cfg:{{}},context:{{}},options:{{providerControls:{{mode:"google-generative-ai-count-tokens-v1",rejectAfterCount:true}}}}}}); }} catch {{}}
await complete({{model,auth:{{apiKey:"fake"}},cfg:{{}},context:{{}},options:{{}}}});
console.log(JSON.stringify({{...observed,proof}}));
''', encoding="utf-8")
            result = subprocess.run(
                ["node", "--permission", "--allow-fs-read=*", str(runner)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(observed["unsafe"], 1)
            self.assertEqual(observed["count"], 2)
            self.assertEqual(observed["generate"], 1)
            self.assertEqual(observed["proof"]["mode"],
                             "google-generative-ai-count-tokens-v1")

    def test_installed_control_patches_match_repo_owned_contracts(self):
        roots = sorted(Path.home().glob(
            ".openclaw/tools/node-*/lib/node_modules/openclaw/package.json"))
        if not roots:
            self.skipTest("installed OpenClaw unavailable")
        root = roots[-1].parent
        # Both installed bytes and behavior are checked against repository-owned
        # contracts.  The behavioral harness uses only loopback mock transports;
        # it performs no paid or external provider call.
        self.assertFalse(apply(root, check=True))
        self.assertFalse(check_provider_output_endpoint(root, check=True))
        runner = Path(__file__).parent / "fixtures" / (
            "openclaw_llm_provider_controls.mjs")
        result = subprocess.run(
            ["node", str(runner), str(root)], text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"])
        self.assertEqual(report["googleAdmitted"],
                         {"inputCountCalls": 1, "modelCalls": 1})
        self.assertEqual(report["googleUnsupportedSchema"], {"providerCalls": 0})
        self.assertEqual(report["paidCalls"], 0)
