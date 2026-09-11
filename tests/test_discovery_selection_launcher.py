import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.discovery_selection_launcher import DiscoverySelectionLauncher
from dalton_core.store import content_hash

class Process:
    pid=43210
    def __init__(self): self.code=None
    def poll(self): return self.code

class DiscoverySelectionLauncherTests(unittest.TestCase):
    def test_ticket_is_durable_and_empty_completion_advances_page(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); config=root/'model.json'; config.write_text('{}')
            process=Process()
            launcher=DiscoverySelectionLauncher(state_dir=root,model_config_path=config,
                                                  scheduler_db=root/'scheduler.sqlite')
            base={"schema_version":"0.1","contract_ref":"x","source_envelope_ref":"e",
                  "source_envelope_hash":"a"*64,"candidates":[]}
            view={**base,"content_hash":content_hash(base)}
            with patch('dalton_core.discovery_selection_launcher.subprocess.Popen',return_value=process):
                ticket=launcher.start(discovery_ref='discovery:one',view=view,mission_ref='mission:v1',
                    company={"company_ref":"c","name":"IBM","ticker":"IBM","aliases":[]},missing_periods=[])
            summary=launcher.root/ticket['id'].split(':')[1]/'summary.json'
            summary.write_text(json.dumps({"status":"succeeded","selection":{"selected":[]}}))
            process.code=0
            self.assertEqual(launcher.status(ticket['id'])['status'],'succeeded')
            self.assertEqual(launcher.completed_empty_discoveries(),['discovery:one'])

if __name__=='__main__': unittest.main()
