from __future__ import annotations
import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest import mock
from dalton_core.backup import DatabaseBackupManager
from dalton_core.service import DaltonService,ServiceConfig
from dalton_core.store import DaltonStore
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
import dalton_core.backup as backup

class AsyncBackupCadenceTests(unittest.TestCase):
    def fixture(self,root):
        core=root/'core.sqlite';sched=root/'scheduler.sqlite'
        with DaltonStore(core) as store:ObservabilityStore(store)
        Scheduler(sched).close()
        manager=DatabaseBackupManager(root/'backups',{'core':core,'scheduler':sched})
        manifest=manager.snapshot()
        config=ServiceConfig.from_mapping({'schema_version':'0.1','core_db':str(core),'scheduler_db':str(sched),'projection_db':str(root/'projection.sqlite'),'model_router_db':None,'capability_catalog_db':None,'heartbeat_path':str(root/'heartbeat.json'),'writer_socket':str(root/'writer.sock'),'tick_seconds':1,'projection_min_interval_seconds':1,'plugin_retry_seconds':1,'plugins':[],'backup':{'enabled':True,'root':str(root/'backups'),'interval_seconds':86400}})
        return config,manifest

    def test_blocked_real_hash_does_not_block_constructor_start_or_ticks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);config,manifest=self.fixture(root);entered=threading.Event();release=threading.Event();original=backup._sha256
            def blocked(path, **kwargs):
                entered.set();self.assertTrue(release.wait(5));return original(path, **kwargs)
            with mock.patch.object(backup,'_sha256',side_effect=blocked):
                service=DaltonService(config)
                self.assertFalse(entered.is_set())
                with mock.patch.object(service._backup,'snapshot') as snapshot:
                    try:
                        service.start();service._poll_backup();self.assertTrue(entered.wait(2))
                        first_future=service._backup_future
                        first=service.run_once(wait_for_projection=False)
                        second=service.run_once(wait_for_projection=False)
                        self.assertEqual(first['backup']['state'],'restoring');self.assertEqual(second['backup']['state'],'restoring')
                        self.assertNotEqual(first['last_tick_at'],second['last_tick_at'])
                        self.assertIs(service._backup_future,first_future);snapshot.assert_not_called()
                        release.set();first_future.result(2);service._poll_backup(allow_launch=False)
                        self.assertEqual(service._backup_state['state'],'ready')
                        self.assertEqual(service._backup_state['last_success_at'],manifest['created_at'])
                        self.assertEqual(service._backup_state['last_snapshot_id'],manifest['snapshot_id'])
                        service._poll_backup();snapshot.assert_not_called()
                    finally:release.set();service.close()

    def test_failed_restore_is_error_and_does_not_create_snapshot_or_fake_success(self):
        with tempfile.TemporaryDirectory() as d:
            config,_=self.fixture(Path(d));service=DaltonService(config)
            with mock.patch.object(service._backup,'latest_verified_manifest',side_effect=OSError('hash read failed')) as restore, mock.patch.object(service._backup,'snapshot') as snapshot:
                try:
                    service.start();service._poll_backup()
                    while not service._backup_future.done():time.sleep(.001)
                    service._poll_backup(allow_launch=False)
                    self.assertEqual(service._backup_state['state'],'error');self.assertIn('hash read failed',service._backup_state['last_error']);self.assertIsNone(service._backup_state['last_success_at'])
                    self.assertFalse(service._backup_cadence_restored);service._poll_backup();snapshot.assert_not_called();self.assertEqual(restore.call_count,1)
                finally:service.close()

    def test_stop_cancels_running_hash_verification_without_new_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            config,_=self.fixture(Path(d));service=DaltonService(config);entered=threading.Event();release=threading.Event();finished=threading.Event()
            original=service._backup.latest_verified_manifest
            def verify():entered.set();release.wait(5);return original()
            with mock.patch.object(service._backup,'latest_verified_manifest',side_effect=verify),mock.patch.object(service._backup,'snapshot') as snapshot:
                service.start();service._poll_backup();self.assertTrue(entered.wait(2));service.stop()
                closer=threading.Thread(target=lambda:(service.close(),finished.set()));closer.start()
                try:
                    self.assertFalse(finished.wait(.05));snapshot.assert_not_called()
                    release.set();closer.join(3);self.assertTrue(finished.is_set());snapshot.assert_not_called()
                    self.assertFalse(service._backup_cadence_restored)
                    self.assertEqual(service._backup_state['state'], 'cancelled')
                    self.assertIsNone(service._backup_state['last_success_at'])
                finally:release.set();closer.join(3)

    def test_stop_during_due_or_missing_verification_never_starts_snapshot(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                config, manifest = self.fixture(Path(d))
                due = {**manifest, "created_at": "2000-01-01T00:00:00+00:00"}
                service = DaltonService(config)
                entered = threading.Event(); release = threading.Event()
                def verify():
                    entered.set(); release.wait(5)
                    return None if missing else due
                with mock.patch.object(service._backup, 'latest_verified_manifest', side_effect=verify), mock.patch.object(service._backup, 'snapshot') as snapshot:
                    service.start(); service._poll_backup()
                    self.assertTrue(entered.wait(2)); service.stop(); release.set()
                    service.close()
                    service._poll_backup()
                    snapshot.assert_not_called()
                    self.assertTrue(service._backup_cadence_restored)
                    self.assertEqual(service._backup_state['state'], 'pending')

    def test_due_or_missing_only_launches_once_on_next_allowed_poll(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                config, manifest = self.fixture(Path(d))
                due = {**manifest, "created_at": "2000-01-01T00:00:00+00:00"}
                service = DaltonService(config)
                entered = threading.Event(); release = threading.Event()
                def snapshot():
                    entered.set(); release.wait(5)
                    return {"snapshot_id": "new"}
                with mock.patch.object(service._backup, 'latest_verified_manifest', return_value=None if missing else due), mock.patch.object(service._backup, 'snapshot', side_effect=snapshot) as save:
                    try:
                        service.start(); service._poll_backup()
                        service._backup_future.result(2)
                        service._poll_backup(allow_launch=False)
                        save.assert_not_called()
                        service._poll_backup(); self.assertTrue(entered.wait(2))
                        service._poll_backup(); self.assertEqual(save.call_count, 1)
                        release.set(); service._backup_future.result(2)
                        service._poll_backup(allow_launch=False)
                        service._poll_backup(); self.assertEqual(save.call_count, 1)
                    finally:
                        release.set(); service.close()
