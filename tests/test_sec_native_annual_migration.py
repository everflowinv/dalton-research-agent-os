import sqlite3
import unittest
from pathlib import Path
from dalton_core.sec_statement_form_schema import migrate as migrate_filings
from dalton_core.sec_native_statement_schema import migrate as migrate_dispatches

class NativeMigrationTests(unittest.TestCase):
    def fixture(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close)
        c.create_function('dalton_coverage_mission_authorized',0,lambda:1)
        c.executescript('CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY); INSERT INTO coverage_mission_versions VALUES("m1");')
        c.executescript((Path(__file__).parent/'fixtures/sec_native_annual_legacy_schema.sql').read_text())
        c.execute('INSERT INTO coverage_mission_statement_dispatches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('d1','m1','a'*64,'company:one','ONE','automation:test','10-K',1,0,'{}','b'*64,'succeeded','t1',None,'2026-01-01','2026-01-01'))
        c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('i1','d1','company:one','0000000123','Original issuer','0001234567-26-000001','10-K','2026-01-01',
             '2025-12-31',0,'[]','connector-governance:sec-financial-statements:v5','c'*64,'2026-01-01','d'*64))
        c.commit();c.execute('PRAGMA foreign_keys=ON')
        return c

    def test_owner_migration_keeps_every_original_rowid_byte_fk_trigger_and_is_idempotent(self):
        c=self.fixture();tables=('coverage_mission_statement_dispatches','coverage_mission_statement_filings')
        prior={t:c.execute('SELECT rowid,* FROM '+t).fetchall() for t in tables}
        triggers=c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
        for _ in range(2):migrate_filings(c);migrate_dispatches(c)
        self.assertEqual(prior,{t:c.execute('SELECT rowid,* FROM '+t).fetchall() for t in tables})
        self.assertEqual(triggers,c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall())
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertIn("'40-F'",c.execute("SELECT sql FROM sqlite_master WHERE name=?",(tables[0],)).fetchone()[0])

    def test_unknown_or_partial_index_shape_is_refused_before_originals_change(self):
        c=self.fixture();rows=c.execute('SELECT rowid,* FROM coverage_mission_statement_dispatches').fetchall()
        c.execute('CREATE INDEX unsupported_native_index ON coverage_mission_statement_dispatches(ticker)')
        with self.assertRaisesRegex(RuntimeError,'unknown dispatch index'):migrate_dispatches(c)
        self.assertEqual(rows,c.execute('SELECT rowid,* FROM coverage_mission_statement_dispatches').fetchall())
        c.execute('DROP INDEX unsupported_native_index')
        c.execute('ALTER TABLE coverage_mission_statement_dispatches ADD COLUMN unsupported TEXT')
        with self.assertRaisesRegex(RuntimeError,'unknown dispatch DDL'):migrate_dispatches(c)
        self.assertEqual(c.execute('SELECT request_hash FROM coverage_mission_statement_dispatches').fetchone()[0],'b'*64)

    def test_open_transaction_is_not_committed_by_migration_and_preview_does_not_write(self):
        c=self.fixture();before=c.total_changes
        migrate_filings(c,preview=True);migrate_dispatches(c,preview=True)
        self.assertEqual(c.total_changes,before)
        c.execute('BEGIN')
        with self.assertRaisesRegex(RuntimeError,'no open transaction'):migrate_filings(c)
        self.assertTrue(c.in_transaction);c.rollback()

if __name__=='__main__':unittest.main()
