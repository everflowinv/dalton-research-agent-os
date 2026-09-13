from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.research_language_runtime import run

class FakeRouter:
    def __init__(self,path): pass
    def get_decision(self,ref): return {"selected_profile_version_ref":"profile:checker" if ref=="decision:checker" else "profile:brain"}
    def get_profile(self,ref):
        return ({"provider":"antigravity-cli-gateway","model":"gemini-3.8-flash"}
                if ref=="profile:checker" else {"provider":"openai","model":"brain"})
    def close(self): pass

class Model:
    def __init__(self,kind): self.kind=kind
    def call(self,**kw):
        if self.kind=="checker": payload={"overall":"可读", "suggestions":[]}; ref="decision:checker"
        else: payload={"decisions":[],"sections":[{"index":0,"title":"回答","body":"收入为 10 美元。","gaps":[]}]}; ref="decision:brain"
        return {"text":json.dumps(payload,ensure_ascii=False),"route_decision_ref":ref,
                "work_order_ref":"work:x","result_envelope_ref":"result:x","invocation_ref":"invoke:x","cost_micros":1,"replayed":False}

class RuntimeTests(unittest.TestCase):
    def test_two_calls_are_bound_and_artifacts_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); checker=root/'checker.json'; brain=root/'brain.json'
            checker.write_text(json.dumps({"model_router_db":str(root/'router.db')}));brain.write_text(json.dumps({"model_router_db":str(root/'router.db')}))
            made=[]
            def factory(config):
                kind="checker" if not made else "brain";made.append(kind);return Model(kind)
            args=dict(product={"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id="ask-1",checker_config=checker,brain_config=brain,scheduler_db=root/'scheduler.db',artifact_dir=root/'proof',model_factory=factory)
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): first=run(**args)
            self.assertEqual("ready_for_publication",first["status"]);self.assertEqual(["checker","brain"],made)
            self.assertEqual("antigravity-cli-gateway",first["runtime_identity"]["checker"]["provider"])
            made.clear()
            with patch('dalton_core.research_language_runtime.ModelRouter',FakeRouter): second=run(**args)
            self.assertEqual(first["artifact_sha256"],second["artifact_sha256"])

    def test_wrong_actual_checker_route_fails_closed(self):
        class Wrong(FakeRouter):
            def get_profile(self,ref): return {"provider":"openai","model":"gemini-3.8-flash"}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); paths=[]
            for name in ('c','b'):
                p=root/f'{name}.json';p.write_text(json.dumps({"model_router_db":str(root/'r')}));paths.append(p)
            made=[]
            def factory(config):
                kind='checker' if not made else 'brain';made.append(kind);return Model(kind)
            with patch('dalton_core.research_language_runtime.ModelRouter',Wrong):
                with self.assertRaisesRegex(ValueError,'身份不符合'):
                    run({"kind":"ask_answer","sections":[{"title":"回答","body":"收入为 10 美元。","gaps":[]}]},mission={},request_id='x',checker_config=paths[0],brain_config=paths[1],scheduler_db=root/'s',model_factory=factory)
if __name__=='__main__': unittest.main()
