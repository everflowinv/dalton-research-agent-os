"""Execution guards for the public synthetic withdrawal fixtures.

The real named-selection CLI is exercised against isolated SQLite. Shell
execution substitutes only its interpreter transport and records exact args;
there is no socket, credential lease, external service, or live state.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import claim_admission_cli as cli
from dalton_core.store import content_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
REF_RE = re.compile(r'claim-version:[0-9a-f]{64}')


class NamedFixtureCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.refs = REF_RE.findall((FIXTURES/'claim-scope-withdrawal/withdraw.sh').read_text())[:3]
        self.hashes = {}
        with sqlite3.connect(self.state/'core.sqlite') as c:
            c.execute('CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY,claim_json TEXT,content_hash TEXT)')
            for ref in self.refs:
                body = {'subject_ref':'company:synthetic:northvale', 'normalized_statement':'Synthetic finding '+ref[-8:]}
                self.hashes[ref] = content_hash(body)
                c.execute('INSERT INTO claim_versions VALUES(?,?,?)', (ref,json.dumps(body),self.hashes[ref]))
        self.arguments = ['retire','--state-dir',str(self.state),'--reason','Synthetic reviewed mismatch']
        for ref in self.refs:self.arguments += ['--claim-version-ref',ref]
        self.arguments += ['--expect-selection',cli.selection_hash(self.refs)]

    def invoke(self, extra=()):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):cli.main([*self.arguments,*extra])
        return json.loads(output.getvalue())

    def test_named_selection_dry_run_keeps_original_bytes_and_never_uses_writer(self):
        before=(self.state/'core.sqlite').read_bytes()
        with patch('dalton_core.governance_cli.ephemeral_call') as writer:
            result=self.invoke()
        writer.assert_not_called()
        self.assertEqual('dry_run',result['status'])
        self.assertEqual(cli.selection_hash(self.refs),result['selection_sha256'])
        self.assertEqual(set(self.refs),{row['claim_version_ref'] for row in result['claims']})
        self.assertEqual(before,(self.state/'core.sqlite').read_bytes())

    def test_wrong_selection_and_nonhuman_apply_are_rejected_before_writer(self):
        with patch('dalton_core.governance_cli.ephemeral_call') as writer:
            with self.assertRaises(SystemExit):self.invoke(['--expect-selection','0'*64,'--apply','--actor','human:fixture-owner'])
            with self.assertRaises(SystemExit):self.invoke(['--apply','--actor','automation:fixture'])
        writer.assert_not_called()

    def test_explicit_apply_forwards_only_exact_selected_refs_and_original_hashes(self):
        with patch('dalton_core.governance_cli.ephemeral_call',return_value={'status':'fresh'}) as writer:
            result=self.invoke(['--apply','--actor','human:fixture-owner'])
        self.assertEqual('applied',result['status']);self.assertEqual(3,writer.call_count)
        self.assertEqual(set(self.refs),{call.kwargs['params']['claim_version_ref'] for call in writer.call_args_list})
        for call in writer.call_args_list:
            kw=call.kwargs;self.assertEqual('retire_claim_by_hand',kw['operation'])
            self.assertEqual('human:fixture-owner',kw['actor_ref'])
            self.assertEqual(self.hashes[kw['params']['claim_version_ref']],kw['params']['claim_version_hash'])


@unittest.skipUnless(shutil.which('zsh'),'zsh is unavailable')
class ShellFixtureExecutionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.scratch=Path(temp.name)
        self.log=self.scratch/'calls.jsonl'
        self.env=os.environ.copy()
        # No inherited state/interpreter defaults can select a real system.
        for key in ('PY','STATE_A','STATE_B','WORKSPACE_MANIFEST','RELEASE_MANIFEST','DALTON_WORKSPACE_MANIFEST'):
            self.env.pop(key,None)
        self.interpreter=self.scratch/'fixture-interpreter'
        self.interpreter.write_text('#!'+sys.executable+'\n'+'''import hashlib,json,os,sys
args=sys.argv[1:]
if args and args[0]=='-c':
 print(json.load(sys.stdin).get('selection_sha256',''));raise SystemExit(0)
if args and args[0].endswith('.py'):
 print(json.dumps({'status':'readonly_fixture_replay'}));raise SystemExit(0)
assert args[:3]==['-m','dalton_core.claim_admission_cli','retire']
refs=[args[n+1] for n,a in enumerate(args) if a=='--claim-version-ref']
digest=hashlib.sha256('\\n'.join(sorted(set(refs))).encode()).hexdigest()
with open(os.environ['FIXTURE_LOG'],'a') as f:
 f.write(json.dumps({'args':args,'refs':refs,'manifest':os.environ.get('DALTON_WORKSPACE_MANIFEST')})+'\\n')
print(json.dumps({'selection_sha256':'0'*64 if os.environ.get('FIXTURE_BAD_SELECTION') else digest,'count':len(refs)}))
''')
        self.interpreter.chmod(0o700)
        self.env.update(PY=str(self.interpreter),STATE_A=str(self.scratch/'state alpha'),
                        STATE_B=str(self.scratch/'state beta'),WORKSPACE_MANIFEST=str(self.scratch/'workspace.json'),FIXTURE_LOG=str(self.log))

    def run_fixture(self,name,arguments=(),**overrides):
        if self.log.exists():self.log.unlink()
        result=subprocess.run(['zsh',str(FIXTURES/name/'withdraw.sh'),*arguments],env={**self.env,**overrides},capture_output=True,text=True)
        calls=[json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result,calls

    def test_default_dry_run_excludes_optional_groups_and_never_applies(self):
        for name,count in [('claim-scope-withdrawal',5),('equity-component-withdrawal',4)]:
            with self.subTest(name=name):
                result,calls=self.run_fixture(name)
                self.assertEqual(0,result.returncode,result.stderr);self.assertEqual(count,len(calls))
                self.assertTrue(all('--apply' not in call['args'] for call in calls))

    def test_optional_group_only_applies_after_both_reviewed_hashes_match(self):
        for name,group in [('claim-scope-withdrawal','X'),('equity-component-withdrawal','C')]:
            with self.subTest(name=name):
                result,calls=self.run_fixture(name,('apply',group))
                self.assertEqual(0,result.returncode,result.stderr);self.assertEqual(4,len(calls))
                for before,after in [(calls[0],calls[1]),(calls[2],calls[3])]:
                    self.assertNotIn('--apply',before['args']);self.assertIn('--apply',after['args'])
                    self.assertEqual(before['refs'],after['refs'])
                    self.assertIn('--expect-selection',after['args'])
                    self.assertEqual('human:owner',after['args'][-1])
                self.assertIsNone(calls[0]['manifest'])
                self.assertEqual(self.env['WORKSPACE_MANIFEST'],calls[2]['manifest'])

    def test_old_cli_ignoring_selection_cannot_trigger_apply_after_bad_preview(self):
        for name,group in [('claim-scope-withdrawal','X'),('equity-component-withdrawal','C')]:
            with self.subTest(name=name):
                result,calls=self.run_fixture(name,('apply',group),FIXTURE_BAD_SELECTION='1')
                self.assertNotEqual(0,result.returncode);self.assertEqual(2,len(calls))
                self.assertTrue(all('--apply' not in call['args'] for call in calls))

    def test_nonzero_or_signal_apply_failure_remains_failure_after_cleanup(self):
        original=self.interpreter.read_text()
        failures={
            'nonzero': "raise SystemExit(23)",
            'signal': "import signal;os.kill(os.getpid(),signal.SIGTERM)",
        }
        for failure,action in failures.items():
            body=original.replace('args=sys.argv[1:]\n',
                "args=sys.argv[1:]\nif '--apply' in args:\n "+action+"\n",1)
            self.interpreter.write_text(body)
            for name,group in [('claim-scope-withdrawal','X'),('equity-component-withdrawal','C')]:
                with self.subTest(failure=failure,name=name):
                    result,calls=self.run_fixture(name,('apply',group))
                    self.assertNotEqual(0,result.returncode,result.stdout)
                    self.assertEqual(2,len(calls))
                    self.assertTrue(all('--apply' not in call['args'] for call in calls))
                    self.assertEqual(2,result.stderr.count('apply 子命令失败'))
        self.interpreter.write_text(original)

    def test_missing_explicit_state_inputs_refuse_without_discovering_host_defaults(self):
        env=self.env.copy();env.pop('STATE_A')
        for name in ('claim-scope-withdrawal','equity-component-withdrawal'):
            with self.subTest(name=name):
                result=subprocess.run(['zsh',str(FIXTURES/name/'withdraw.sh')],env=env,capture_output=True,text=True)
                self.assertNotEqual(0,result.returncode);self.assertIn('STATE_A',result.stderr)

    def test_explicit_release_manifest_resolves_only_its_isolated_interpreter(self):
        release=self.scratch/'synthetic release'
        (release/'bin').mkdir(parents=True)
        (release/'bin/python').symlink_to(self.interpreter)
        manifest=self.scratch/'release.json'
        manifest.write_text(json.dumps({'release_path':str(release)}))
        env=self.env.copy();env.pop('PY')
        env['RELEASE_MANIFEST']=str(manifest)
        for name,count in [('claim-scope-withdrawal',5),('equity-component-withdrawal',4)]:
            with self.subTest(name=name):
                if self.log.exists():self.log.unlink()
                result=subprocess.run(['zsh',str(FIXTURES/name/'withdraw.sh')],env=env,capture_output=True,text=True)
                self.assertEqual(0,result.returncode,result.stderr)
                calls=[json.loads(line) for line in self.log.read_text().splitlines()]
                self.assertEqual(count,len(calls))
                self.assertTrue(all('--apply' not in call['args'] for call in calls))


if __name__=='__main__':unittest.main()
