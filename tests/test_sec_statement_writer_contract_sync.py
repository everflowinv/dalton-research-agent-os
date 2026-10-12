from __future__ import annotations
import hashlib,json,plistlib,unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_sec_statement_amendment_replay as fixtures
from dalton_core.sec_statement_form_migration import apply as migrate_governance
from dalton_core.sec_statement_writer_contract_sync import preview,apply,FLAG

class WriterContractSyncTests(unittest.TestCase):
    setUp=fixtures.AmendmentTests.setUp
    workspace=fixtures.MigrationTests.workspace

    def installed(self, *, legacy=False, flag=None):
        self.newpath.unlink();w=self.workspace();w.slug='ws-synthetic'
        if legacy:w.workspace_id='legacy'
        migrate_governance(w)
        self.launch=self.root/'LaunchAgents';self.launch.mkdir()
        self.label='space.lumos.dalton.writer' if legacy else 'space.lumos.dalton.workspace.ws-synthetic.writer'
        self.plist=self.launch/(self.label+'.plist')
        self.wire={'Label':self.label,'ProgramArguments':['/frozen/bin/dalton-writer','--db',str(self.state/'core.sqlite'),FLAG,str(flag or self.oldpath),'--model-config','frozen-model.json','--owner-call-cap','50'],
                   'KeepAlive':True,'EnvironmentVariables':{'SAFE':'original'},'StandardErrorPath':'existing.stderr'}
        self.plist.write_bytes(plistlib.dumps(self.wire,sort_keys=True));return w

    def test_workspace_and_legacy_change_only_one_exact_flag_with_backup_and_cas(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                if legacy:
                    import shutil
                    shutil.rmtree(self.launch);self.newpath.unlink(missing_ok=True)
                    # installed() starts with an existing separately approved v5.
                    self.newpath.write_text(json.dumps(self.new))
                w=self.installed(legacy=legacy);before=self.plist.read_bytes();p=preview(w,self.launch)
                self.assertEqual(p['status'],'eligible');self.assertEqual(self.plist.read_bytes(),before)
                with patch('subprocess.Popen',side_effect=AssertionError('never reload or call provider')),patch('subprocess.run',side_effect=AssertionError('never reload')):
                    result=apply(w,self.launch,expected_plist_sha256=p['before_sha256'])
                self.assertEqual(result['status'],'applied');self.assertFalse(result['service_reload_performed'])
                expected={**self.wire,'ProgramArguments':list(self.wire['ProgramArguments'])};expected['ProgramArguments'][expected['ProgramArguments'].index(FLAG)+1]=str(self.newpath)
                self.assertEqual(plistlib.loads(self.plist.read_bytes()),expected)
                self.assertEqual(Path(result['original_backup']).read_bytes(),before)
                current=hashlib.sha256(self.plist.read_bytes()).hexdigest()
                self.assertEqual(apply(w,self.launch,expected_plist_sha256=current)['status'],'existing')
                self.assertEqual(self.oldpath.read_text(),json.dumps(self.old))

    def test_changed_plist_since_preview_refused_before_write(self):
        w=self.installed();p=preview(w,self.launch);self.wire['KeepAlive']=False
        self.plist.write_bytes(plistlib.dumps(self.wire));before=self.plist.read_bytes()
        with self.assertRaisesRegex(ValueError,'changed since approval'):apply(w,self.launch,expected_plist_sha256=p['before_sha256'])
        self.assertEqual(self.plist.read_bytes(),before)

    def test_disabled_and_independent_flags_preserved(self):
        w=self.installed();self.wire['ProgramArguments']=['writer','--db',str(self.state/'core.sqlite')]
        self.plist.write_bytes(plistlib.dumps(self.wire));self.assertEqual(preview(w,self.launch)['status'],'skipped')
        self.wire['ProgramArguments']+= [FLAG,str(self.root/'independent-v4.json')]
        self.plist.write_bytes(plistlib.dumps(self.wire));self.assertEqual(preview(w,self.launch)['status'],'skipped')

    def test_wrong_core_duplicate_flag_or_label_refused(self):
        w=self.installed()
        for wire in ({**self.wire,'Label':'foreign'}, {**self.wire,'ProgramArguments':self.wire['ProgramArguments']+[FLAG,str(self.oldpath)]},
                     {**self.wire,'ProgramArguments':['writer','--db','/foreign/core.sqlite',FLAG,str(self.oldpath)]}):
            self.plist.write_bytes(plistlib.dumps(wire))
            with self.assertRaises(ValueError):preview(w,self.launch)

    def test_approval_or_source_change_not_migrated(self):
        w=self.installed();before=self.plist.read_bytes();self.newpath.write_text(json.dumps(self.new))
        self.assertEqual(preview(w,self.launch)['status'],'skipped');self.assertEqual(self.plist.read_bytes(),before)

    def test_missing_applied_v5_never_publishes_preview_only_approval(self):
        w=self.installed();self.newpath.unlink();self.assertEqual(preview(w,self.launch)['status'],'skipped');self.assertFalse(self.newpath.exists())
