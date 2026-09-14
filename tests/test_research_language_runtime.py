from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.research_language_runtime import run
from dalton_core.store import content_hash

class FakeRouter:
    def __init__(self,path): pass
    def get_decision(self,ref): return {"selected_profile_version_ref":"profile:checker" if ref=="decision:checker" else ("profile:brain" if ref=="decision:brain" else ("profile:fidelity" if ref=="decision:fidelity" else "profile:producer"))}
    def get_profile(self,ref):
        return ({"provider":"antigravity-cli-gateway","model":"gemini-3.8-flash","family":"gemini"}
                if ref=="profile:checker" else ({"provider":"openai","model":"brain","family":"gpt"} if ref=="profile:brain" else ({"provider":"google","model":"verifier","family":"claude"} if ref=="profile:fidelity" else {"provider":"deepseek","model":"producer","family":"deepseek"})))
    def close(self): pass

class Model:
    def __init__(self,kind): self.kind=kind
    def call(self,**kw):
        if self.kind=="checker": payload={"overall":"可读", "suggestions":[]}; ref="decision:checker"
        elif self.kind=="brain": payload={"decisions":[],"sections":[{"index":0,"title":"回答","body":"收入为 10 美元。","gaps":[]}]}; ref="decision:brain"
        else: payload={"verdict":"pass","faithful":True,"no_new_facts":True,"meaning_preserved":True,"findings":[]}; ref="decision:fidelity"
        return {"text":json.dumps(payload,ensure_ascii=False),"route_decision_ref":ref,
                "work_order_ref":"work:x","result_envelope_ref":"result:x","invocation_ref":"invoke:x","cost_micros":1,"replayed":False}

class RuntimeTests(unittest.TestCase):
    def resumable_args(self, root, factory, *, request_id="resume"):
        paths={}
        for kind in ('checker','brain','fidelity'):
            path=root/f'{kind}.json';path.write_text(json.dumps({
                "model_router_db":str(root/'router.db'),"fixture_kind":kind}))
            paths[kind]=path
        return dict(product={"kind":"ask_answer","sections":[{
            "title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},
            request_id=request_id,checker_config=paths['checker'],brain_config=paths['brain'],
            verifier_config=paths['fidelity'],producer_route_decision_ref='decision:producer',
            scheduler_db=root/'scheduler.db',artifact_dir=root/'proof',model_factory=factory)

    def test_two_calls_are_bound_and_artifacts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); checker=root/'checker.json'; brain=root/'brain.json'
            checker.write_text(json.dumps({"model_router_db":str(root/'router.db')}));brain.write_text(json.dumps({"model_router_db":str(root/'router.db')}))
            made=[]
            def factory(config):
                kind=('checker' if len(made)==0 else ('brain' if len(made)==1 else 'fidelity'));made.append(kind);return Model(kind)
            args=dict(product={"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id="ask-1",checker_config=checker,brain_config=brain,verifier_config=brain,producer_route_decision_ref='decision:producer',scheduler_db=root/'scheduler.db',artifact_dir=root/'proof',model_factory=factory)
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): first=run(**args)
            self.assertEqual("ready_for_publication",first["status"]);self.assertEqual(["checker","brain","fidelity"],made)
            self.assertEqual(3,first["review_cost_micros"])
            self.assertFalse(first["replayed"])
            self.assertTrue(first["fidelity_verification"]["independence"]["independent"])
            self.assertEqual("antigravity-cli-gateway",first["runtime_identity"]["checker"]["provider"])
            made.clear()
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): second=run(**args)
            self.assertEqual(first["artifact_sha256"],second["artifact_sha256"])
            self.assertTrue(second["artifact_replayed"])
            self.assertFalse(second["replayed"])
            self.assertEqual(second["content_hash"],content_hash({k:v for k,v in second.items() if k not in {"content_hash","artifact_ref","artifact_sha256","artifact_replayed"}}))

    def test_nonempty_fidelity_findings_are_persisted_as_pending(self):
        class FindingModel(Model):
            def call(self,**kw):
                value=super().call(**kw)
                if self.kind=="fidelity":
                    value["text"]=json.dumps({"verdict":"pass","faithful":True,
                        "no_new_facts":True,"meaning_preserved":True,
                        "findings":["仍有一项语义风险"]},ensure_ascii=False)
                return value
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); configs=[]
            for name in ('c','b','v'):
                p=root/f'{name}.json';p.write_text(json.dumps({"model_router_db":str(root/'r')}));configs.append(p)
            made=[]
            def factory(config):
                kind=('checker' if len(made)==0 else ('brain' if len(made)==1 else 'fidelity'));made.append(kind);return FindingModel(kind)
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):
                result=run({"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='pending',checker_config=configs[0],brain_config=configs[1],verifier_config=configs[2],producer_route_decision_ref='decision:producer',scheduler_db=root/'s',artifact_dir=root/'proof',model_factory=factory)
            self.assertEqual("pending_fidelity_review",result["status"])
            self.assertTrue((root/'proof'/f'{result["artifact_ref"]}.md').is_file())
            self.assertTrue((root/'proof'/f'{result["artifact_ref"]}.json').is_file())

    def test_same_family_verifier_is_persisted_as_pending(self):
        class Clash(FakeRouter):
            def get_profile(self,ref):
                value=super().get_profile(ref)
                if ref=="profile:fidelity": value={**value,"family":"gpt"}
                return value
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); configs=[]
            for name in ('c','b','v'):
                p=root/f'{name}.json';p.write_text(json.dumps({"model_router_db":str(root/'r')}));configs.append(p)
            made=[]
            def factory(config):
                kind=('checker' if len(made)==0 else ('brain' if len(made)==1 else 'fidelity'));made.append(kind);return Model(kind)
            with patch('dalton_core.research_language_runtime.ModelRouter',Clash):
                result=run({"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='clash',checker_config=configs[0],brain_config=configs[1],verifier_config=configs[2],producer_route_decision_ref='decision:producer',scheduler_db=root/'s',artifact_dir=root/'proof',model_factory=factory)
            self.assertEqual("pending_fidelity_review",result["status"])
            self.assertFalse(result["fidelity_verification"]["independence"]["independent"])

    def test_wrong_actual_checker_route_fails_closed(self):
        class Wrong(FakeRouter):
            def get_profile(self,ref): return {"provider":"openai","model":"gemini-3.8-flash","family":"gemini"}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); paths=[]
            for name in ('c','b','v'):
                p=root/f'{name}.json';p.write_text(json.dumps({"model_router_db":str(root/'r')}));paths.append(p)
            made=[]
            def factory(config):
                kind=('checker' if len(made)==0 else ('brain' if len(made)==1 else 'fidelity'));made.append(kind);return Model(kind)
            with patch('dalton_core.research_language_runtime.ModelRouter',Wrong):
                with self.assertRaisesRegex(ValueError,'身份不符合'):
                    run({"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='x',checker_config=paths[0],brain_config=paths[1],verifier_config=paths[2],producer_route_decision_ref='decision:producer',scheduler_db=root/'s',model_factory=factory)

    def test_fidelity_failure_retries_only_fidelity_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);calls=[];fail=[True]
            class Routed(Model):
                def call(self,**kw):
                    calls.append(self.kind)
                    if self.kind=='fidelity' and fail:
                        fail.pop();raise RuntimeError('temporary outage')
                    return super().call(**kw)
            args=self.resumable_args(root,lambda cfg:Routed(cfg['fixture_kind']))
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):
                first=run(**args);second=run(**args)
            self.assertEqual('pending_fidelity_review',first['status'])
            self.assertEqual('ready_for_publication',second['status'])
            self.assertEqual(['checker','brain','fidelity','fidelity'],calls)
            self.assertTrue(list((root/'proof'/'failures').glob('*.fidelity.*.json')))
            self.assertTrue((root/'proof'/f'{second["artifact_ref"]}.json').is_file())

    def test_brain_transport_failure_reuses_checker_then_finishes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);calls=[];failed=False
            class Routed(Model):
                def call(self,**kw):
                    nonlocal failed
                    calls.append(self.kind)
                    if self.kind=='brain' and not failed:
                        failed=True;raise RuntimeError('temporary brain outage')
                    return super().call(**kw)
            args=self.resumable_args(root,lambda cfg:Routed(cfg['fixture_kind']),request_id='brain-resume')
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):
                first=run(**args);second=run(**args)
            self.assertEqual('pending_brain_revision',first['status'])
            self.assertEqual('ready_for_publication',second['status'])
            self.assertEqual(['checker','brain','brain','fidelity'],calls)

    def test_source_or_stage_config_change_cannot_reuse_cached_calls(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);calls=[]
            class Routed(Model):
                def call(self,**kw):calls.append(self.kind);return super().call(**kw)
            args=self.resumable_args(root,lambda cfg:Routed(cfg['fixture_kind']),request_id='binding')
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):run(**args)
            checker=json.loads(args['checker_config'].read_text());checker['changed']=True
            args['checker_config'].write_text(json.dumps(checker))
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):run(**args)
            changed=dict(args);changed['product']={**args['product'],"version_ref":"other"}
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):run(**changed)
            self.assertEqual(3,calls.count('checker'))
            self.assertEqual(3,calls.count('brain'))

    def test_verifier_config_change_reuses_style_but_not_semantic_result(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);calls=[]
            class Routed(Model):
                def call(self,**kw):calls.append(self.kind);return super().call(**kw)
            args=self.resumable_args(root,lambda cfg:Routed(cfg['fixture_kind']),request_id='verifier-change')
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):run(**args)
            verifier=json.loads(args['verifier_config'].read_text());verifier['policy_version']='new'
            args['verifier_config'].write_text(json.dumps(verifier))
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):second=run(**args)
            self.assertEqual('ready_for_publication',second['status'])
            self.assertEqual(['checker','brain','fidelity','fidelity'],calls)
if __name__=='__main__': unittest.main()

class EofRuntimeTests(unittest.TestCase):
    def test_future_brain_eof_closure_is_disclosed_and_passes_existing_validation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); paths=[]
            for kind in ('checker','brain','fidelity'):
                p=root/f'{kind}.json'; p.write_text(json.dumps({"model_router_db":str(root/'r'),"fixture_kind":kind})); paths.append(p)
            class Truncated(Model):
                def call(self, **kw):
                    value=super().call(**kw)
                    if self.kind == 'brain': value['text']=value['text'][:-2]
                    return value
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter):
                result=run(product={"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='eof',checker_config=paths[0],brain_config=paths[1],verifier_config=paths[2],producer_route_decision_ref='decision:producer',scheduler_db=root/'s',artifact_dir=root/'proof',model_factory=lambda cfg:Truncated(cfg['fixture_kind']))
            self.assertEqual(result['status'],'ready_for_publication')
            normalization=result['call_evidence']['brain']['stage_output_normalization']
            self.assertEqual(normalization['mode'],'eof_container_closure')
            self.assertEqual(normalization['suffix'],']}')

class TerminalRecoveryTests(unittest.TestCase):
    def test_terminal_recovery_reuses_checker_and_brain_envelope_then_only_calls_fidelity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths=[]
            for kind in ('checker','brain','fidelity'):
                p=root/f'{kind}.json';p.write_text(json.dumps({"model_router_db":str(root/'r'),"fixture_kind":kind}));paths.append(p)
            calls=[]
            class Broken(Model):
                def call(self,**kw):
                    calls.append(self.kind)
                    value=super().call(**kw)
                    if self.kind=='brain': value['text']='{"decisions":[],"sections":[{"index":0,"title":"回答'
                    return value
            args=dict(product={"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='recover-terminal',checker_config=paths[0],brain_config=paths[1],verifier_config=paths[2],producer_route_decision_ref='decision:producer',scheduler_db=root/'s',artifact_dir=root/'proof',model_factory=lambda cfg:Broken(cfg['fixture_kind']))
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): first=run(**args)
            self.assertEqual(first['status'],'pending_brain_revision')
            raw=json.dumps({"decisions":[],"sections":[{"index":0,"title":"回答","body":"收入为 10 美元。","gaps":[]}]},ensure_ascii=False)[:-2]
            args['brain_recovery']={"result_envelope_ref":"result:x","raw_text":raw,"raw_sha256":__import__('hashlib').sha256(raw.encode()).hexdigest()}
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): second=run(**args)
            self.assertEqual(second['status'],'ready_for_publication')
            self.assertEqual(calls,['checker','brain','fidelity'])
            self.assertEqual(second['call_evidence']['brain']['stage_output_normalization']['suffix'],']}')
            self.assertTrue(list((root/'proof').glob('*.brain-terminal-*.json')))
