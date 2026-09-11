"""Scratch copies preserve room for the live host, including during SQLite backup."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import rehearse_deploy as rehearsal


class RehearsalSpaceTests(unittest.TestCase):
    def test_low_space_rejects_before_creating_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / 'live'
            (live / 'config').mkdir(parents=True)
            (live / 'config/service.json').write_text('{}')
            target = root / 'scratch'
            run = rehearsal.Rehearsal(live, target, openclaw_config=root/'openclaw.json',
                scratch_reserve_bytes=100, log=lambda _: None)
            with patch.object(rehearsal.shutil, 'disk_usage', return_value=SimpleNamespace(free=103)):
                with self.assertRaises(rehearsal.RehearsalDiskSpaceError):
                    run.copy_state()
            self.assertFalse(target.exists())

    def test_configurable_reserve_and_exact_boundary(self):
        with patch.dict(os.environ, {'DALTON_REHEARSAL_RESERVE_BYTES': '777'}):
            self.assertEqual(rehearsal.rehearsal_reserve_bytes(), 777)
            self.assertEqual(rehearsal.rehearsal_reserve_bytes(123), 123)
        with patch.object(rehearsal.shutil, 'disk_usage', return_value=SimpleNamespace(free=120)):
            result = rehearsal.check_rehearsal_space(Path('/private/tmp'), reserve_bytes=100, incoming_bytes=20)
            self.assertEqual(result['required_bytes'], 120)
            with self.assertRaises(rehearsal.RehearsalDiskSpaceError):
                rehearsal.check_rehearsal_space(Path('/private/tmp'), reserve_bytes=100, incoming_bytes=21)
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                rehearsal.rehearsal_reserve_bytes(value)

    def test_sqlite_backup_aborts_when_live_reserve_is_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source.sqlite'; dest = root/'scratch.sqlite'
            with closing(sqlite3.connect(source)) as c:
                c.execute('CREATE TABLE payload(value BLOB)')
                c.executemany('INSERT INTO payload VALUES(zeroblob(8192))', [()]*100)
                c.commit()
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            calls = []
            def check():
                calls.append(True)
                if len(calls) == 2:
                    raise rehearsal.RehearsalDiskSpaceError('reserve consumed by another task')
            with self.assertRaises(rehearsal.RehearsalDiskSpaceError):
                rehearsal.backup_sqlite(source, dest, progress_check=check)
            self.assertEqual(len(calls), 2)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            with closing(sqlite3.connect(source)) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM payload').fetchone()[0], 100)
            # The aborted destination handle is closed and can be removed.
            dest.unlink()

    def test_sqlite_sizing_includes_pending_wal_and_tree_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.sqlite';source.write_bytes(b'x'*10)
            Path(str(source)+'-wal').write_bytes(b'w'*7)
            self.assertEqual(rehearsal.copy_item_bytes(rehearsal.CopyItem(source,root/'dest','sqlite')),17)
            tree=root/'tree';tree.mkdir();(tree/'keep.json').write_bytes(b'123')
            (tree/'.hidden').write_bytes(b'12345');(tree/'x.lock').write_bytes(b'12345')
            self.assertEqual(rehearsal.copy_item_bytes(rehearsal.CopyItem(tree,root/'dest','tree')),3)

    def test_space_failure_is_fatal_even_for_informational_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            run=rehearsal.Rehearsal(root/'live',root/'scratch',openclaw_config=root/'cfg',
                scratch_reserve_bytes=100,log=lambda _:None)
            called=[]
            with patch.object(rehearsal.shutil,'disk_usage',return_value=SimpleNamespace(free=99)):
                first=run.step('precheck',lambda:(called.append(True) or ('ok',[])))
                second=run.step('next',lambda:('ok',[]))
            self.assertFalse(first.ok);self.assertTrue(second.skipped);self.assertEqual(called,[])
