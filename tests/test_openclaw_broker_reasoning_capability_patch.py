import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from integrations.openclaw_host_patches.patch_broker_reasoning_capability import (
    BINDING_MARKERS, CALL_ANCHOR, EXECUTION_MARKERS, HELPER, HELPER_ANCHOR,
    PATCHED_CALL, RUNTIME_MARKERS, TRANSPORT_MARKERS, apply, render, target,
)


class BrokerReasoningCapabilityPatchTests(unittest.TestCase):
    def _root(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "dist").mkdir()
        (root / "package.json").write_text(json.dumps({"version": "2026.9.9"}))
        # These dependencies are contract fixtures, never imported/executed.
        (root / "dist/model-runtime-binding-test.mjs").write_text("\n".join(BINDING_MARKERS))
        (root / "dist/simple-completion-execution-test.mjs").write_text("\n".join(EXECUTION_MARKERS))
        ai = root / "node_modules/@openclaw/ai/dist"
        ai.mkdir(parents=True)
        (ai / "transports.mjs").write_text("\n".join(TRANSPORT_MARKERS))
        (root / "dist/runtime-llm.runtime-test.mjs").write_text(
            HELPER_ANCHOR + "\nreturn async (params, prepared) => {\n"
            + "\n".join(RUNTIME_MARKERS[:2]) + "\n"
            + "// " + RUNTIME_MARKERS[2] + "\n"
            + RUNTIME_MARKERS[3] + "\n" + CALL_ANCHOR + "\n});\n};\n}\n"
        )
        return temporary, root

    def _node(self, body):
        result = subprocess.run(["node", "--input-type=module", "-e", HELPER + body],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_apply_is_atomic_idempotent_and_syntax_valid(self):
        temporary, root = self._root()
        with temporary:
            self.assertTrue(apply(root, check=False))
            self.assertFalse(apply(root, check=False))
            self.assertFalse(apply(root, check=True))
            self.assertEqual(target(root).read_text().count(PATCHED_CALL), 1)
            self.assertEqual(subprocess.run(["node", "--check", str(target(root))],
                                           capture_output=True).returncode, 0)

    def test_check_is_read_only_when_missing(self):
        temporary, root = self._root()
        with temporary:
            before = target(root).read_bytes()
            with self.assertRaisesRegex(ValueError, "patch is missing"):
                apply(root, check=True)
            self.assertEqual(target(root).read_bytes(), before)

    def test_render_is_read_only(self):
        temporary, root = self._root()
        with temporary:
            before = target(root).read_bytes()
            _, original, candidate, changed = render(root)
            self.assertTrue(changed)
            self.assertEqual(original, before)
            self.assertIn(PATCHED_CALL, candidate)
            self.assertEqual(target(root).read_bytes(), before)

    def test_patched_call_uses_trusted_policy_and_explicit_thinking_only(self):
        temporary, root = self._root()
        with temporary:
            apply(root, check=False)
            path = target(root)
            path.write_text(path.read_text() + "\nexport {createRuntimeLlm};\n")
            script = '''
globalThis.caller={};
globalThis.normalizeRuntimeThinkingLevel=value=>value;
globalThis.resolvePluginPolicyId=authority=>authority.pluginIdForPolicy;
globalThis.completeWithPreparedSimpleCompletionModel=async params=>({flag:params.model.compat.supportsReasoningEffort??null,changed:params.model!==original});
const {createRuntimeLlm}=await import(process.argv[1]);
const original={api:"openai-completions",reasoning:true,compat:{supportedReasoningEfforts:["low"]}};
const results=[];
for(const [policy,params] of [["dalton-openclaw-model-broker",{thinkingLevel:"low"}],["other-plugin",{thinkingLevel:"low",pluginPolicyId:"dalton-openclaw-model-broker"}],["dalton-openclaw-model-broker",{reasoning:"low"}]]){
const run=createRuntimeLlm({authority:{pluginIdForPolicy:policy}});
const result=await run(params,{model:original,auth:{}});
results.push(result);
}
console.log(JSON.stringify(results));
'''
            # Synthetic host has the same trusted-policy/request/call anchors.
            # Return the instrumented completion result from its async callback.
            path.write_text(path.read_text().replace("\n};\n}", "\nreturn result;\n};\n}"))
            checked = subprocess.run(["node", "--input-type=module", "-e", script, str(path)],
                                     capture_output=True, text=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout), [
                {"flag": True, "changed": True},
                {"flag": None, "changed": False},
                {"flag": None, "changed": False},
            ])

    def test_unknown_host_version_refused(self):
        temporary, root = self._root()
        with temporary:
            (root / "package.json").write_text('{"version":"2026.9.10"}')
            with self.assertRaisesRegex(ValueError, "2026.9.9"):
                apply(root, check=False)

    def test_changed_binding_or_dispatch_contract_refused(self):
        for file in ("model-runtime-binding-test.mjs", "simple-completion-execution-test.mjs"):
            with self.subTest(file=file):
                temporary, root = self._root()
                with temporary:
                    (root / "dist" / file).write_text("changed contract")
                    with self.assertRaisesRegex(ValueError, "contract changed"):
                        apply(root, check=False)

    def test_changed_trusted_thinking_contract_refused(self):
        temporary, root = self._root()
        with temporary:
            path = target(root)
            path.write_text(path.read_text().replace(RUNTIME_MARKERS[1], "changed_policy();"))
            with self.assertRaisesRegex(ValueError, "trusted broker"):
                apply(root, check=False)

    def test_changed_provider_wrapper_contract_refused(self):
        temporary, root = self._root()
        with temporary:
            (root / "node_modules/@openclaw/ai/dist/transports.mjs").write_text("changed")
            with self.assertRaisesRegex(ValueError, "wrapper route contract changed"):
                apply(root, check=False)

    def test_partial_or_duplicate_patch_refused(self):
        for mode in ("helper_only", "call_only", "duplicate"):
            with self.subTest(mode=mode):
                temporary, root = self._root()
                with temporary:
                    path = target(root)
                    source = path.read_text()
                    if mode == "helper_only":
                        source = HELPER + source
                    elif mode == "call_only":
                        source = source.replace(CALL_ANCHOR, PATCHED_CALL)
                    else:
                        source = HELPER + HELPER + source.replace(CALL_ANCHOR, PATCHED_CALL)
                    path.write_text(source)
                    with self.assertRaisesRegex(ValueError, "partial|duplicated|changed"):
                        apply(root, check=False)

    def test_changed_call_anchor_refused(self):
        temporary, root = self._root()
        with temporary:
            path = target(root)
            path.write_text(path.read_text().replace(CALL_ANCHOR, "model: anotherModel,"))
            with self.assertRaisesRegex(ValueError, "call anchor changed"):
                apply(root, check=False)

    def test_invalid_javascript_does_not_replace_original(self):
        temporary, root = self._root()
        with temporary:
            path = target(root)
            path.write_text(path.read_text() + "invalid ??? syntax;")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "syntax validation"):
                apply(root, check=False)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list((root / "dist").glob(".runtime*"))), 0)

    def test_broker_explicit_levels_copy_only_missing_flag(self):
        result = self._node('''
const model = Object.freeze({id:"m",provider:"p",api:"openai-completions",reasoning:true,compat:Object.freeze({supportedReasoningEfforts:Object.freeze(["low","medium"]),reasoningEffortMap:{low:"low"},other:"kept"})});
const values=["low","medium"].map(level=>{
 const local=daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker",level);
 return {changed:local!==model,flag:local.compat.supportsReasoningEffort,kept:local.compat.other,map:local.compat.reasoningEffortMap===model.compat.reasoningEffortMap,declared:local.compat.supportedReasoningEfforts===model.compat.supportedReasoningEfforts};
});
console.log(JSON.stringify({values,originalMissing:model.compat.supportsReasoningEffort===undefined}));
''')
        self.assertTrue(result["originalMissing"])
        self.assertEqual(result["values"], [{"changed": True, "flag": True, "kept": "kept", "map": True, "declared": True}] * 2)

    def test_other_plugins_interactive_and_native_apis_are_unchanged(self):
        result = self._node('''
const model={id:"m",provider:"p",api:"openai-completions",reasoning:true,compat:{supportedReasoningEfforts:["low"]}};
const native={...model,api:"google-generative-ai"};
const absent={...model,compat:{}};
const broker="dalton-openclaw-model-broker";
console.log(JSON.stringify([
daltonBrokerModelForThinking(model,"some-other-plugin","low")===model,
daltonBrokerModelForThinking(model,undefined,"low")===model,
daltonBrokerModelForThinking(model,broker,undefined)===model,
daltonBrokerModelForThinking(native,broker,"low")===native,
daltonBrokerModelForThinking(absent,broker,"low")===absent
]));
''')
        self.assertEqual(result, [True] * 5)

    def test_false_and_unsupported_efforts_keep_original_mapping(self):
        result = self._node('''
const broker="dalton-openclaw-model-broker";
const cases=[{supportedReasoningEfforts:["low"],supportsReasoningEffort:false},{supportedReasoningEfforts:["high"]},{supportedReasoningEfforts:"low"},{supportedReasoningEfforts:[]},{}];
console.log(JSON.stringify(cases.map(compat=>{const model={api:"openai-completions",reasoning:true,compat}; return daltonBrokerModelForThinking(model,broker,"low")===model;})));
''')
        self.assertEqual(result, [True] * 5)

    def test_nonreasoning_models_keep_original_mapping(self):
        result = self._node('''
const cases=[false,undefined];
console.log(JSON.stringify(cases.map(reasoning=>{const model={api:"openai-completions",reasoning,compat:{supportedReasoningEfforts:["low"]}};return daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low")===model;})));
''')
        self.assertEqual(result, [True, True])

    def test_eligible_invalid_capability_flag_refused(self):
        result = self._node('''
const model={api:"openai-completions",reasoning:true,compat:{supportedReasoningEfforts:["low"],supportsReasoningEffort:null}};
try{daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low");console.log("false");}catch(e){console.log(JSON.stringify(e.message.includes("invalid reasoning capability flag")));}
''')
        self.assertTrue(result)

    def test_existing_true_model_is_unchanged(self):
        result = self._node('''
const model={api:"openai-completions",reasoning:true,compat:{supportedReasoningEfforts:["low"],supportsReasoningEffort:true}};
console.log(JSON.stringify(daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low")===model));
''')
        self.assertTrue(result)

    def test_prepared_transport_owner_runtime_symbols_and_prototype_are_preserved(self):
        result = self._node('''
const runtimeKey=Symbol("openclaw.modelLlmRuntime"), providerKey=Symbol("providerHandle"), customKey=Symbol("custom");
const prototype={boundType:"model"};
const model=Object.assign(Object.create(prototype),{api:"openai-completions",reasoning:true,provider:"p",id:"m",compat:{supportedReasoningEfforts:["low"]}});
const transport={api:"openai-completions",reasoning:true,provider:"p",id:"m",compat:{supportedReasoningEfforts:["low"]}};
const owner={run(){}},runtime={},provider={},custom={};
Object.defineProperty(transport,providerKey,{value:provider,enumerable:false});
Object.defineProperty(model,customKey,{value:custom,enumerable:false});
const binding=Object.freeze({runtime,completionOwner:owner,completionTransport:transport});
Object.defineProperty(model,runtimeKey,{value:binding,enumerable:false});
Object.freeze(model); Object.freeze(transport); Object.freeze(model.compat); Object.freeze(transport.compat);
const local=daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low");
console.log(JSON.stringify({prototype:Object.getPrototypeOf(local)===prototype,owner:local[runtimeKey].completionOwner===owner,runtime:local[runtimeKey].runtime===runtime,custom:local[customKey]===custom,provider:local[runtimeKey].completionTransport[providerKey]===provider,hidden:!Object.getOwnPropertyDescriptor(local,runtimeKey).enumerable,modelFlag:local.compat.supportsReasoningEffort,transportFlag:local[runtimeKey].completionTransport.compat.supportsReasoningEffort,originalModelMissing:model.compat.supportsReasoningEffort===undefined,originalTransportMissing:transport.compat.supportsReasoningEffort===undefined,bindingChanged:local[runtimeKey]!==binding}));
''')
        self.assertTrue(all(result.values()), result)

    def test_prepared_transport_false_and_unsupported_keep_mapping_route_conflict_refused(self):
        result = self._node('''
const broker="dalton-openclaw-model-broker", key=Symbol("openclaw.modelLlmRuntime");
const cases=[{compat:{supportsReasoningEffort:false}},{id:"other"},{compat:{supportedReasoningEfforts:["high"]}}];
console.log(JSON.stringify(cases.map(extra=>{
 const model={api:"openai-completions",reasoning:true,provider:"p",id:"m",compat:{supportedReasoningEfforts:["low"]}};
 const transport={api:model.api,provider:model.provider,id:model.id,...extra};
 Object.defineProperty(model,key,{value:{completionTransport:transport},enumerable:false});
 try{return daltonBrokerModelForThinking(model,broker,"low")===model;}catch(e){return extra.id==="other" && e.message.includes("route differs");}
})));
''')
        self.assertEqual(result, [True] * 3)

    def test_verified_host_api_wrapper_keeps_route_and_provider_bindings(self):
        result = self._node('''
const key=Symbol("openclaw.modelLlmRuntime"), providerHandle=Symbol("providerHandle");
const model={api:"openai-completions",reasoning:true,provider:"p",id:"m",baseUrl:"https://endpoint.test/v1",compat:{supportedReasoningEfforts:["low"]}};
const api="openclaw-provider-simple:"+[model.provider,model.id,model.api,model.baseUrl].map(encodeURIComponent).join(":")+":api-key::agent:research";
const transport={...model,api};const handle={};
Object.defineProperty(transport,providerHandle,{value:handle});
Object.defineProperty(model,key,{value:{completionTransport:transport}});
const local=daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low");
console.log(JSON.stringify({api:local[key].completionTransport.api===api,handle:local[key].completionTransport[providerHandle]===handle,flag:local[key].completionTransport.compat.supportsReasoningEffort,original:transport.compat.supportsReasoningEffort===undefined}));
''')
        self.assertTrue(all(result.values()), result)

    def test_host_wrapper_wrong_source_api_or_endpoint_refused(self):
        result = self._node('''
const key=Symbol("openclaw.modelLlmRuntime");
console.log(JSON.stringify(["wrong-api","wrong-endpoint"].map(mode=>{
const model={api:"openai-completions",reasoning:true,provider:"p",id:"m",baseUrl:"https://good.test",compat:{supportedReasoningEfforts:["low"]}};
const parts=[model.provider,model.id,mode==="wrong-api"?"openai-responses":model.api,mode==="wrong-endpoint"?"https://evil.test":model.baseUrl];
const transport={...model,api:"openclaw-provider-simple:"+parts.map(encodeURIComponent).join(":")+":api-key:"};
Object.defineProperty(model,key,{value:{completionTransport:transport}});
try{daltonBrokerModelForThinking(model,"dalton-openclaw-model-broker","low");return false;}catch(e){return e.message.includes("route differs");}
})));
''')
        self.assertEqual(result, [True, True])


if __name__ == "__main__":
    unittest.main()
