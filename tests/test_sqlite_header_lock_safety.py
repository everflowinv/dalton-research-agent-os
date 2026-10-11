"""Actual cross-process POSIX lock and metadata-only header-cache coverage."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dalton_core import sqlite_header
from dalton_core.budget_policy_snapshot import read_checkpointed_budget_policy
from dalton_core.readonly_sqlite import connect_read_only

_PROBE = """import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 try:
  fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,int(sys.argv[3]),int(sys.argv[2]),os.SEEK_SET)
  print('available')
 except BlockingIOError: print('held')
finally: os.close(fd)
"""


class SQLiteHeaderLockSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'fixture.sqlite'
        with sqlite_header._HEADER_LOCK:
            sqlite_header._HEADERS.clear()

    def lock_status(self, start=1073741825, length=1):
        return subprocess.check_output(
            [sys.executable,'-I','-c',_PROBE,str(self.path),str(start),str(length)],
            text=True).strip()

    def test_reader_and_cached_header_preserve_reserved_writer_lock(self):
        writer = sqlite3.connect(self.path,isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('CREATE TABLE fixture(value)')
        writer.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.lock_status(),'held')
        run = subprocess.run
        with mock.patch.object(sqlite_header.subprocess,'run',wraps=run) as reads:
            reader = connect_read_only(self.path)
            self.assertEqual(reader.execute('SELECT count(*) FROM fixture').fetchone()[0],0)
            reader.close()
            header = sqlite_header.read_sqlite_header(self.path)
            self.assertEqual(header[:16],b'SQLite format 3\x00')
            self.assertEqual(reads.call_count,1)
        self.assertEqual(self.lock_status(),'held')
        writer.rollback()

    def test_budget_shm_refusal_preserves_existing_wal_reader_lock(self):
        writer = sqlite3.connect(self.path,isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE fixture(value)')
        self.assertEqual(self.lock_status(1073741826,510),'held')
        before = {p.name:p.stat().st_size for p in self.path.parent.iterdir()}
        with self.assertRaisesRegex(sqlite3.OperationalError,'absent WAL/SHM'):
            read_checkpointed_budget_policy(self.path,'not-a-policy')
        self.assertEqual(self.lock_status(1073741826,510),'held')
        self.assertEqual({p.name:p.stat().st_size for p in self.path.parent.iterdir()},before)

    def test_file_change_invalidates_cached_header(self):
        self.path.write_bytes(b'first'+b'0'*95)
        self.assertEqual(sqlite_header.read_sqlite_header(self.path)[:5],b'first')
        self.path.write_bytes(b'other'+b'1'*95)
        self.assertEqual(sqlite_header.read_sqlite_header(self.path)[:5],b'other')
        self.assertEqual(len(sqlite_header._HEADERS),1)

    def test_mutation_during_isolated_read_is_refused_not_cached(self):
        self.path.write_bytes(b'first'+b'0'*95)
        run = subprocess.run
        def change(*args,**kwargs):
            result=run(*args,**kwargs)
            self.path.write_bytes(b'other'+b'1'*95)
            return result
        with mock.patch.object(sqlite_header.subprocess,'run',side_effect=change):
            with self.assertRaisesRegex(sqlite3.OperationalError,'changed'):
                sqlite_header.read_sqlite_header(self.path)
        self.assertFalse(sqlite_header._HEADERS)

    def test_cache_has_a_fixed_bound_and_keeps_no_descriptors(self):
        run = subprocess.run
        with mock.patch.object(sqlite_header,'_MAX_HEADERS',2):
            for i in range(3):
                path=self.path.with_name(f'header-{i}')
                path.write_bytes(bytes([i])*100)
                self.assertEqual(sqlite_header.read_sqlite_header(path),bytes([i])*100)
        self.assertEqual(len(sqlite_header._HEADERS),2)
        self.assertTrue(all(isinstance(header,bytes) for header in sqlite_header._HEADERS.values()))
