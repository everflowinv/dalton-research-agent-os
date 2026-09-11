import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import ALPHAENGINE_PLAN_SELECTOR, render
from dalton_core.mission_source_discovery import load_discovery_plan
from dalton_core.store import content_hash


class AlphaEnginePlanSelectionTests(unittest.TestCase):
    def test_hash_bound_selection_survives_render_and_drift_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); state=root/'state'; plans=state/'discovery-plans'
            plans.mkdir(parents=True)
            source=Path(__file__).resolve().parents[1]/'deploy/phase9/p9d-us-it-services-discovery-plan-v2-candidate.json'
            plan=load_discovery_plan(source); selected=plans/'alphaengine-v2.json'
            selected.write_bytes(source.read_bytes())
            body={"schema_version":"alphaengine-discovery-plan-selection-0.1",
                  "id":"alphaengine-plan-selection:test","status":"approved",
                  "source_ref":"source:alphaengine","plan_ref":plan['id'],
                  "plan_hash":plan['content_hash'],"plan_path":selected.name}
            selector={**body,"content_hash":content_hash(body)}
            (plans/ALPHAENGINE_PLAN_SELECTOR).write_text(json.dumps(selector))
            for _ in range(2):
                paths=render(root/'agents',root/'bin',state,root/'absent.json',root/'logs')
                args=plistlib.loads(Path(paths['writer']).read_bytes())['ProgramArguments']
                self.assertEqual(args[args.index('--alphaengine-discovery-plan')+1],str(selected))
            changed=json.loads(selected.read_text()); changed['budget']['max_calls_24h']+=1
            changed['content_hash']=content_hash({k:v for k,v in changed.items() if k!='content_hash'})
            selected.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError,'ref, hash, and source'):
                render(root/'agents',root/'bin',state,root/'absent.json',root/'logs')


if __name__=='__main__': unittest.main()
