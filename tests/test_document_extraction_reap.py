"""Exit facts harvested without spawning, killing, or rewriting summary/fees."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.document_extraction_launcher import DocumentExtractionLauncher

class Process:
 pid=12345
 def __init__(self):self.code=None;self.kills=0
 def poll(self):return self.code
 def terminate(self):self.kills+=1
 def kill(self):self.kills+=1
 def wait(self,timeout=None):return self.code

class ExtractionReapTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);config=root/'cfg.json';config.write_text('{}');self.launcher=DocumentExtractionLauncher(state_dir=root,model_config_path=config);self.process=Process()
  with patch('dalton_core.document_extraction_launcher.subprocess.Popen',return_value=self.process):self.ticket=self.launcher.start()
  self.path=self.launcher._ticket_path(self.ticket['id']);self.summary=self.path.with_name('summary.json');self.summary.write_text('{"status":"failed","cost_micros":1234}')
 def test_reap_live_child_leaves_everything_unchanged(self):
  before=self.path.read_bytes()
  with patch('dalton_core.document_extraction_launcher.subprocess.Popen',side_effect=AssertionError('must not dispatch')):self.assertEqual(self.launcher.reap(),[])
  self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.process.kills,0)
 def test_reap_exited_child_settles_once_without_new_dispatch_or_summary_change(self):
  sha=hashlib.sha256(self.summary.read_bytes()).hexdigest();self.process.code=1
  with patch('dalton_core.document_extraction_launcher.subprocess.Popen',side_effect=AssertionError('must not dispatch')):self.assertEqual(self.launcher.reap(),[self.ticket['id']])
  record=json.loads(self.path.read_text());self.assertEqual(record['status'],'failed');self.assertEqual(record['exit_code'],1);self.assertIsNotNone(record['completed_at']);self.assertEqual(self.process.kills,0);self.assertEqual(hashlib.sha256(self.summary.read_bytes()).hexdigest(),sha)
  saved=self.path.read_bytes();self.assertEqual(self.launcher.reap(),[]);self.assertEqual(self.path.read_bytes(),saved)
 def test_close_harvests_finished_handle_without_killing(self):
  self.process.code=0;self.launcher.close();record=json.loads(self.path.read_text());self.assertEqual(record['status'],'succeeded');self.assertEqual(self.process.kills,0)
 def test_new_start_harvests_previous_exit_before_replacing_handle(self):
  self.process.code=0
  with patch('dalton_core.document_extraction_launcher.subprocess.Popen',return_value=Process()):self.launcher.start()
  self.assertEqual(json.loads(self.path.read_text())['status'],'succeeded')

 def test_already_terminal_ticket_is_not_rewritten(self):
  self.process.code=0;record=json.loads(self.path.read_text());record.update(status='failed',exit_code=17,completed_at='2026-01-01T00:00:00+00:00');self.path.write_text(json.dumps(record));before=self.path.read_bytes()
  self.assertEqual(self.launcher.reap(),[self.ticket['id']]);self.assertEqual(self.path.read_bytes(),before)
 def test_unreadable_ticket_does_not_abort_close_or_trigger_new_dispatch(self):
  self.process.code=0;self.path.write_text('{broken');self.launcher.close();self.assertEqual(self.path.read_text(),'{broken');self.assertEqual(self.process.kills,0)
