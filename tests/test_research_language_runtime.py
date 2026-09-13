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
if __name__=='__main__': unittest.main()
