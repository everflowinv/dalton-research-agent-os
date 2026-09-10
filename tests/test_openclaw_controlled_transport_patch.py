from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from integrations.openclaw_host_patches.patch_controlled_completion_transport import (
    ORIGINAL, ORIGINAL_BIND, PATCHED, PATCHED_BIND, SUPPORTED_VERSION, apply,
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

    def test_an_unreviewed_openclaw_version_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, version="2026.9.4")
            with self.assertRaisesRegex(ValueError, "supports OpenClaw"):
                apply(root, check=False)
