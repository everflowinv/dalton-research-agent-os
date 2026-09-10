import argparse, hashlib, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from scripts.build_sec_8k_discovery_proposal import build_candidate_plan, build_selector_proposal
from scripts.install_sec_8k_discovery_selection import run
ROOT=Path(__file__).parents[1]

def write(path,value): path.write_text(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n'); return hashlib.sha256(path.read_bytes()).hexdigest()
class OwnerInstallTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.root=Path(self.t.name); self.target=self.root/'target'
  self.active=self.root/'active.json'; self.active.write_bytes((ROOT/'deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json').read_bytes())
  active=json.loads(self.active.read_text()); self.active_hash=active['content_hash']
  candidate=build_candidate_plan(active,created_at='2026-09-10T00:00:00+00:00'); self.candidate=self.root/'candidate.json'; self.cs=write(self.candidate,candidate)
  selector=build_selector_proposal(candidate); self.selector=self.root/'selector.json'; self.ss=write(self.selector,selector)
  self.args=argparse.Namespace(command='prepare',active_plan=self.active,active_plan_hash=self.active_hash,candidate=self.candidate,candidate_sha256=self.cs,selector=self.selector,selector_sha256=self.ss,source_core=self.root/'core.sqlite',target_dir=self.target,actor=None,execute=False,service_stopped_ack=False)
  self.mission={'id':'mission:v13','content_hash':'m'*64,'mission_ref':active['mission_ref'],'universe':[{'company_ref':x} for x in candidate['companies']]}
 def tearDown(self): self.t.cleanup()
 def test_prepare_is_read_only_and_reports_exact_delta(self):
  with patch('scripts.install_sec_8k_discovery_selection.read_active_mission',return_value=self.mission): report=run(self.args)
  self.assertEqual(report['result'],'prepared_only; no files written'); self.assertFalse(self.target.exists()); self.assertEqual(report['delta']['added_specs'][0]['form'],'8-K'); self.assertFalse(report['artifact_acceptance'])
 def test_apply_requires_owner_ack_and_is_idempotent(self):
  self.args.command='apply'; self.args.actor='human:owner'; self.args.execute=True; self.args.service_stopped_ack=True
  with patch('scripts.install_sec_8k_discovery_selection.read_active_mission',return_value=self.mission):
   first=run(self.args); second=run(self.args)
  self.assertEqual(first['candidate']['write'],'created'); self.assertEqual(second['candidate']['write'],'identical'); self.assertEqual(json.loads((self.target/'sec-filings-plan-selection-v1.json').read_text())['status'],'approved')
 def test_tamper_stale_and_existing_different_target_refuse(self):
  with patch('scripts.install_sec_8k_discovery_selection.read_active_mission',return_value=self.mission):
   self.args.candidate_sha256='0'*64
   with self.assertRaisesRegex(ValueError,'sha256 mismatch'): run(self.args)
   self.args.candidate_sha256=self.cs; self.args.active_plan_hash='0'*64
   with self.assertRaisesRegex(ValueError,'original hash changed'): run(self.args)
   self.args.active_plan_hash=self.active_hash; self.target.mkdir(); (self.target/json.loads(self.selector.read_text())['plan_path']).write_text('different')
   with self.assertRaises(FileExistsError): run(self.args)
 def test_candidate_scope_change_refuses_even_when_rehashed(self):
  body=json.loads(self.candidate.read_text()); body['budget']['max_calls_24h']=51
  from dalton_core.store import content_hash
  body['content_hash']=content_hash({k:v for k,v in body.items() if k!='content_hash'}); self.args.candidate_sha256=write(self.candidate,body)
  selector=build_selector_proposal(body); self.args.selector_sha256=write(self.selector,selector)
  with patch('scripts.install_sec_8k_discovery_selection.read_active_mission',return_value=self.mission):
   with self.assertRaisesRegex(ValueError,'preserved plan scope'): run(self.args)
if __name__=='__main__': unittest.main()
