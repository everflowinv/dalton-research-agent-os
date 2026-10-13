import copy
import unittest
from pathlib import Path
from dalton_core.sec_native_statement_governance import preview,apply
from dalton_core.sec_financials_core import sec_financials_output_schema
from tests import test_sec_statement_amendment_replay as legacy

class NativeOwnerApprovalTests(unittest.TestCase):
    setUp=legacy.AmendmentTests.setUp
    workspace=legacy.MigrationTests.workspace

    def test_explicit_v6_owner_approval_retains_v5_no_service_or_preview_write(self):
        w=self.workspace();prior=self.newpath.read_bytes()
        result=preview(w,approved_by='human:synthetic-owner')
        self.assertEqual(result['status'],'eligible')
        self.assertEqual(result['record']['approved_by'],'human:synthetic-owner')
        self.assertEqual(result['record']['allowed_permissions'],self.new['allowed_permissions'])
        self.assertNotEqual(result['record']['expected_schema_hash'],self.new['expected_schema_hash'])
        self.assertFalse(Path(result['path']).exists());self.assertEqual(self.newpath.read_bytes(),prior)
        result=apply(w,approved_by='human:synthetic-owner',expected_binding=result['binding'])
        self.assertEqual(result['status'],'applied');self.assertEqual(self.newpath.read_bytes(),prior)
        self.assertEqual(result['service_actions'],0)
        self.assertEqual(apply(w,approved_by='human:synthetic-owner',expected_binding=result['binding'])['status'],'existing')
        self.assertIn('40-F',sec_financials_output_schema(version=6)['properties']['filings']['items']['properties']['form']['enum'])
        self.assertNotIn('40-F',sec_financials_output_schema(version=5)['properties']['filings']['items']['properties']['form']['enum'])

    def test_disabled_wrong_permission_actor_or_changed_managed_binding_refuses(self):
        w=self.workspace();p=preview(w,approved_by='human:synthetic-owner')
        wrong=copy.deepcopy(p['binding']);wrong['workspace_hash']='b'*64
        with self.assertRaises(ValueError):apply(w,approved_by='human:synthetic-owner',expected_binding=wrong)
        with self.assertRaises(ValueError):preview(w,approved_by='automation:unapproved')
        # Catalog fixtures use the exact declared external catalog path.
        import json
        body=json.loads(self.catalogpath.read_bytes());source=body['sources'][0];source['enabled']=False
        w=self.workspace(sources=[source])
        with self.assertRaises(ValueError):preview(w,approved_by='human:synthetic-owner')
        self.assertFalse((self.govdir/'sec-financial-statements-v6.json').exists())

    def test_installed_v5_selector_cas_moves_only_to_approved_v6_and_keeps_exact_backup(self):
        import plistlib,hashlib
        from dalton_core.sec_native_statement_writer_sync import preview as sync_preview,apply as sync_apply
        w=self.workspace();w.workspace_id='legacy'
        p=preview(w,approved_by='human:synthetic-owner')
        apply(w,approved_by='human:synthetic-owner',expected_binding=p['binding'])
        directory=self.root/'LaunchAgents';directory.mkdir()
        path=directory/'space.lumos.dalton.writer.plist'
        wire={'Label':'space.lumos.dalton.writer','ProgramArguments':['/private/synthetic/python','-m','dalton_core.writer_server',
            '--db',str(self.state/'core.sqlite'),'--statement-lane-governance',str(self.newpath)],'KeepAlive':True}
        path.write_bytes(plistlib.dumps(wire));before=path.read_bytes();oldgov=self.newpath.read_bytes()
        p=sync_preview(w,directory,approved_by='human:synthetic-owner')
        self.assertEqual(path.read_bytes(),before);self.assertFalse(p['service_reload_performed'])
        changed=copy.deepcopy(wire);changed['ProgramArguments'][-1]=str(self.govdir/'sec-financial-statements-v6.json')
        self.assertEqual(p['plist'],changed)
        with self.assertRaises(ValueError):sync_apply(w,directory,approved_by='human:synthetic-owner',expected_plist_sha256='a'*64)
        result=sync_apply(w,directory,approved_by='human:synthetic-owner',expected_plist_sha256=p['before_sha256'])
        self.assertEqual(plistlib.loads(path.read_bytes()),changed)
        self.assertEqual(Path(result['original_backup']).read_bytes(),before)
        self.assertEqual(self.newpath.read_bytes(),oldgov)
        self.assertFalse(result['service_reload_performed'])
        self.assertEqual(sync_preview(w,directory,approved_by='human:synthetic-owner')['status'],'existing')

    def test_writer_selector_missing_approval_wrong_program_or_other_scope_refuses(self):
        import plistlib
        from dalton_core.sec_native_statement_writer_sync import preview as sync_preview
        w=self.workspace();w.workspace_id='legacy'
        directory=self.root/'LaunchAgents';directory.mkdir();path=directory/'space.lumos.dalton.writer.plist'
        wire={'Label':'space.lumos.dalton.writer','ProgramArguments':['/private/synthetic/python','-m','dalton_core.writer_server',
            '--db',str(self.state/'core.sqlite'),'--statement-lane-governance',str(self.newpath)]}
        path.write_bytes(plistlib.dumps(wire))
        with self.assertRaisesRegex(ValueError,'already-applied'):sync_preview(w,directory,approved_by='human:synthetic-owner')
        p=preview(w,approved_by='human:synthetic-owner');apply(w,approved_by='human:synthetic-owner',expected_binding=p['binding'])
        for field in ('program','database','approval'):
            wrong=copy.deepcopy(wire)
            if field=='program':wrong['ProgramArguments'][2]='unrelated.module'
            elif field=='database':wrong['ProgramArguments'][4]='/private/other/core.sqlite'
            else:wrong['ProgramArguments'][6]='/private/other/v6.json'
            path.write_bytes(plistlib.dumps(wrong));before=path.read_bytes()
            with self.subTest(field=field),self.assertRaises(ValueError):sync_preview(w,directory,approved_by='human:synthetic-owner')
            self.assertEqual(path.read_bytes(),before)
