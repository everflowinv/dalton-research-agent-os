"""Cancellation never certifies partial bytes or bypasses retained proof."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from dalton_core import backup
from dalton_core.backup import BackupCancelled, DatabaseBackupManager
from dalton_core.service import DaltonService, ServiceConfig

class BackupCooperativeStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/'core.sqlite'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE rows(value TEXT)')
            c.executemany('INSERT INTO rows VALUES(?)', [(str(i)*100,) for i in range(5000)])
        self.stop = threading.Event()
        self.manager = DatabaseBackupManager(self.root/'backups', {'core':self.db})
        self.manager.snapshot('old')
        self.before = self.files()

    def files(self):
        return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def unchanged(self):
        self.assertEqual(self.files(), self.before)
        self.assertFalse([p for p in self.manager.backup_root.iterdir() if p.name.startswith('.')])

    def test_cancelled_hash_does_not_change_source_or_completed_snapshot(self):
        count = 0
        def request():
            nonlocal count
            count += 1
            return count == 3
        with self.assertRaises(BackupCancelled):
            backup._sha256(self.db, cancel_requested=request)
        self.assertEqual(count, 3)
        self.unchanged()

    def test_integrity_query_is_interrupted_and_never_claimed_ok(self):
        count = 0
        def request():
            nonlocal count
            count += 1
            return count >= 3
        with self.assertRaises(BackupCancelled) as caught:
            backup._integrity(self.db, cancel_requested=request)
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        self.unchanged()

    def test_interrupted_vacuum_does_not_fall_back_and_hidden_output_is_cleaned(self):
        original_connect = backup.sqlite3.connect
        copies = []
        def connect(*args, **kwargs):
            raw = original_connect(*args, **kwargs)
            class Proxy:
                def __getattr__(self, name): return getattr(raw, name)
                def execute(self, sql, *params):
                    if sql.startswith('VACUUM'):
                        self.stop_on_progress = True
                    return raw.execute(sql, *params)
                def set_progress_handler(self, handler, size):
                    if handler is not None:
                        def stop_then_check():
                            if getattr(self,'stop_on_progress',False):self_stop.set()
                            return handler()
                        raw.set_progress_handler(stop_then_check,size)
                    else:raw.set_progress_handler(None,0)
                def backup(self, *args, **kwargs):
                    copies.append(True);return raw.backup(*args, **kwargs)
            self_stop = self.stop
            return Proxy()
        self.manager.cancel_requested=self.stop.is_set
        with mock.patch.object(backup.sqlite3,'connect',side_effect=connect):
            with self.assertRaises(BackupCancelled):self.manager.snapshot('new')
        self.assertFalse(copies)
        self.unchanged()

    def test_fallback_copy_interrupts_at_page_boundary_and_cleans_hidden_output(self):
        original_connect = backup.sqlite3.connect
        pages = []
        def connect(*args, **kwargs):
            raw = original_connect(*args, **kwargs)
            if kwargs.get('uri'):
                class Proxy:
                    def __getattr__(self, name):return getattr(raw,name)
                    def execute(self,sql,*params):
                        if sql.startswith('VACUUM'):raise sqlite3.OperationalError('old SQLite syntax')
                        return raw.execute(sql,*params)
                    def backup(self,target,**options):
                        pages.append(options['pages']);progress=options['progress']
                        def stop_after_page(*values):
                            self_stop.set();progress(*values)
                        raw.backup(target,**{**options,'progress':stop_after_page})
                self_stop=self.stop
                return Proxy()
            return raw
        self.manager.cancel_requested=self.stop.is_set
        with mock.patch.object(backup.sqlite3,'connect',side_effect=connect):
            with self.assertRaises(BackupCancelled):self.manager.snapshot('new')
        self.assertEqual(pages,[256]);self.unchanged()

    def test_cancelled_new_copy_integrity_cleans_temp_and_preserves_old_snapshot(self):
        original=backup._integrity
        self.manager.cancel_requested=self.stop.is_set
        def stop_before_check(path,**kwargs):
            if path.parent.name.startswith('.'):
                self.stop.set()
            return original(path,**kwargs)
        with mock.patch.object(backup,'_integrity',side_effect=stop_before_check):
            with self.assertRaises(BackupCancelled):self.manager.snapshot('new')
        self.unchanged()

    def test_cancelled_new_copy_hash_cleans_temp_without_complete_manifest(self):
        original=backup._sha256
        self.manager.cancel_requested=self.stop.is_set
        def stop_before_hash(path,**kwargs):
            if path.parent.name.startswith('.'):
                self.stop.set()
            return original(path,**kwargs)
        with mock.patch.object(backup,'_sha256',side_effect=stop_before_hash):
            with self.assertRaises(BackupCancelled):self.manager.snapshot('new')
        self.unchanged()

    def test_cancelled_retained_integrity_does_not_delete_any_snapshot(self):
        self.manager.snapshot('newer');self.before=self.files()
        original=backup._integrity
        def stop_during_retained(path,**kwargs):
            self.stop.set();return original(path,**kwargs)
        self.manager.cancel_requested=self.stop.is_set
        with mock.patch.object(backup,'_integrity',side_effect=stop_during_retained):
            with self.assertRaises(BackupCancelled):self.manager.prune_verified(keep_latest=1)
        self.unchanged()

    def test_stop_at_delete_boundary_never_starts_rmtree(self):
        self.manager.snapshot('newer');self.before=self.files()
        self.manager.cancel_requested=self.stop.is_set
        def request(_path):self.stop.set();return []
        with mock.patch.object(backup,'_external_open_pids',side_effect=request), \
                mock.patch.object(backup.shutil,'rmtree',wraps=backup.shutil.rmtree) as remove:
            with self.assertRaises(BackupCancelled):self.manager.prune_verified(keep_latest=1)
        remove.assert_not_called();self.unchanged()

    def test_stop_during_rmtree_finishes_one_deletion_and_preserves_retained_snapshot(self):
        self.manager.snapshot('newer');self.before=self.files()
        retained={p:v for p,v in self.before.items() if p.startswith('backups/newer/')}
        self.manager.cancel_requested=self.stop.is_set
        remove=backup.shutil.rmtree
        def finish(path):
            self.stop.set();remove(path)
        with mock.patch.object(backup,'_external_open_pids',return_value=[]), \
                mock.patch.object(backup.shutil,'rmtree',side_effect=finish) as removing:
            with self.assertRaises(BackupCancelled):self.manager.prune_verified(keep_latest=1)
        self.assertEqual(removing.call_count,1)
        self.assertEqual({p:v for p,v in self.files().items()if p.startswith('backups/newer/')},retained)
        self.assertFalse((self.manager.backup_root/'old').exists())
        self.assertEqual(self.files()['core.sqlite'],self.before['core.sqlite'])

    def test_cancelled_snapshot_preprune_is_not_swallowed_and_does_not_start_copy(self):
        self.manager.keep_latest=2;self.manager.cancel_requested=self.stop.is_set
        original=backup._sha256
        def stop_in_prune(path,**kwargs):self.stop.set();return original(path,**kwargs)
        with mock.patch.object(backup,'_copy_database') as copy, \
                mock.patch.object(backup,'_sha256',side_effect=stop_in_prune):
            with self.assertRaises(BackupCancelled):self.manager.snapshot('new')
        copy.assert_not_called();self.unchanged()

    def test_latest_manifest_does_not_skip_cancelled_hash_as_corrupt_backup(self):
        self.manager.cancel_requested=self.stop.is_set
        original=backup._sha256
        def interrupted(path,**kwargs):self.stop.set();return original(path,**kwargs)
        with mock.patch.object(backup,'_sha256',side_effect=interrupted):
            with self.assertRaises(BackupCancelled):self.manager.latest_verified_manifest()
        self.unchanged()

    def test_service_stop_cancels_actual_backup_reader_and_reports_cancelled_not_success(self):
        config=ServiceConfig.from_mapping({'schema_version':'0.1','core_db':str(self.db),'scheduler_db':str(self.db),'projection_db':str(self.root/'projection.sqlite'),'model_router_db':None,'capability_catalog_db':None,'heartbeat_path':str(self.root/'heartbeat.json'),'writer_socket':str(self.root/'writer.sock'),'tick_seconds':1,'projection_min_interval_seconds':1,'plugin_retry_seconds':1,'plugins':[],'backup':{'enabled':True,'root':str(self.root/'backups'),'interval_seconds':86400}})
        service=DaltonService(config);entered=threading.Event();release=threading.Event();original=backup._sha256
        def hash_at_boundary(path,**kwargs):
            entered.set();self.assertTrue(release.wait(2));return original(path,**kwargs)
        with mock.patch.object(backup,'_sha256',side_effect=hash_at_boundary):
            service.start()
            with closing(sqlite3.connect(self.db)) as check:
                source_before=check.execute('SELECT rowid,value FROM rows ORDER BY rowid').fetchall()
            service._poll_backup();self.assertTrue(entered.wait(2))
            service.stop();release.set();service.close()
        self.assertEqual(service._backup_state['state'],'cancelled')
        self.assertEqual(service._backup_state['stop_reason'],'cooperative_backup_stop')
        self.assertIsNone(service._backup_state['last_success_at'])
        self.assertIsNone(service._backup_state['last_snapshot_id'])
        # Normal service.close can checkpoint its own pre-existing setup WAL;
        # compare authoritative row content, not the checkpoint's physical layout.
        with closing(sqlite3.connect(self.db)) as check:
            self.assertEqual(check.execute('SELECT rowid,value FROM rows ORDER BY rowid').fetchall(),source_before)
        self.assertEqual({str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()for p in (self.root/'backups').rglob('*')if p.is_file()}, {p:v for p,v in self.before.items()if p.startswith('backups/')})
