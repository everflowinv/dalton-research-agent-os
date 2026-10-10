"""The zero-byte WAL policy snapshot never skips live WAL or file guards."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.budget_policy_snapshot import read_checkpointed_budget_policy
from dalton_core.readonly_sqlite import connect_cold_wal_snapshot
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore, ThesisImpactBudgetConflict


class CheckpointedBudgetPolicyTests(unittest.TestCase):
    def fixture(self, empty_wal=True):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'budget.sqlite'
        with ThesisImpactBudgetStore(path) as store:
            expected=store.register_policy(policy_version_id='policy:synthetic',day_cap_micros=1000000)
            expected=store.policy('policy:synthetic')
        self.assertFalse(Path(str(path)+'-shm').exists())
        if empty_wal:Path(str(path)+'-wal').write_bytes(b'')
        return path,expected

    def fingerprint(self,path):
        return {p.name:(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns,p.stat().st_ctime_ns)
                for p in path.parent.iterdir() if p.is_file()}

    def test_zero_wal_policy_read_is_exact_and_leaves_every_file_unchanged(self):
        path,expected=self.fixture();before=self.fingerprint(path)
        self.assertEqual(read_checkpointed_budget_policy(path,'policy:synthetic'),expected)
        self.assertEqual(self.fingerprint(path),before)
        self.assertFalse(Path(str(path)+'-shm').exists())
        # Existing global cold reader still insists both sidecars are absent.
        with self.assertRaisesRegex(sqlite3.OperationalError,'absent WAL/SHM'):
            with connect_cold_wal_snapshot(path):pass

    def test_absent_sidecars_policy_read_remains_exact_and_nonprovisioning(self):
        path,expected=self.fixture(empty_wal=False);before=self.fingerprint(path)
        self.assertEqual(read_checkpointed_budget_policy(path,'policy:synthetic'),expected)
        self.assertEqual(self.fingerprint(path),before)

    def test_nonzero_wal_or_present_shm_refuse_without_reading_policy(self):
        for sidecar in ('-wal','-shm'):
            with self.subTest(sidecar=sidecar):
                path,_=self.fixture();Path(str(path)+sidecar).write_bytes(b'writer-pages')
                with patch.object(ThesisImpactBudgetStore,'policy_from_connection',side_effect=AssertionError('unsafe immutable read')):
                    with self.assertRaises(sqlite3.OperationalError):read_checkpointed_budget_policy(path,'policy:synthetic')

    def test_source_identity_and_sidecar_changes_during_read_refuse(self):
        for change in ('main','wal_replace','wal_growth','shm','wal_absent'):
            with self.subTest(change=change):
                path,_=self.fixture();wal=Path(str(path)+'-wal')
                real=ThesisImpactBudgetStore.policy_from_connection
                def changed(connection,ref):
                    result=real(connection,ref)
                    if change=='main':path.write_bytes(path.read_bytes()+b'changed')
                    elif change=='wal_replace':
                        replacement=path.parent/'replacement';replacement.write_bytes(b'');replacement.replace(wal)
                    elif change=='wal_growth':wal.write_bytes(b'committed pages')
                    elif change=='shm':Path(str(path)+'-shm').write_bytes(b'writer-shm')
                    elif change=='wal_absent':wal.unlink()
                    return result
                with patch.object(ThesisImpactBudgetStore,'policy_from_connection',side_effect=changed):
                    with self.assertRaises(sqlite3.OperationalError):read_checkpointed_budget_policy(path,'policy:synthetic')

    def test_snapshot_connection_is_query_only_and_canonical_policy_validation_still_runs(self):
        path,expected=self.fixture();real=ThesisImpactBudgetStore.policy_from_connection
        def inspect(connection,ref):
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0],1)
            with self.assertRaises(sqlite3.OperationalError):connection.execute("DELETE FROM thesis_impact_budget_policies")
            return real(connection,ref)
        with patch.object(ThesisImpactBudgetStore,'policy_from_connection',side_effect=inspect):
            self.assertEqual(read_checkpointed_budget_policy(path,'policy:synthetic'),expected)
        with self.assertRaises(ThesisImpactBudgetConflict):read_checkpointed_budget_policy(path,'policy:missing')
