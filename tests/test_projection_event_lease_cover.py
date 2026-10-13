"""Cold scheduler metadata reads preserve full history and warm append semantics."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core import dashboard_projector as projector
from dalton_core.projection_folds import AppendOnlyFold, clear_folds, source_identity
from dalton_core.migrations.projection_indexes import (
    ATTEMPT_EVENT_FOLD_COVER_INDEX, LEASE_FOLD_COVER_INDEX,
    SCHEDULER_PROJECTION_INDEXES, ensure_indexes, apply_projection_indexes,
)
from dalton_core.store import canonical_json

PAIRS = ((projector._ATTEMPT_EVENT_FOLD, ATTEMPT_EVENT_FOLD_COVER_INDEX),
         (projector._LEASE_FOLD, LEASE_FOLD_COVER_INDEX))


class EventLeaseCoverTests(unittest.TestCase):
    def setUp(self):
        clear_folds(); self.addCleanup(clear_folds)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'scheduler.sqlite'
        self.c = sqlite3.connect(self.path, isolation_level=None)
        self.c.row_factory = sqlite3.Row; self.addCleanup(self.c.close)
        self.c.execute('CREATE TABLE scheduler_attempt_events(event_seq INTEGER PRIMARY KEY AUTOINCREMENT,'
            'work_order_id TEXT,state TEXT,attempt_number INTEGER,event_id TEXT,created_at TEXT,reason TEXT)')
        self.c.execute('CREATE TABLE scheduler_leases(lease_revision_id TEXT,work_order_id TEXT,'
            'attempt_number INTEGER,lease_version INTEGER,expires_at TEXT,created_at TEXT,lease_token_hash TEXT)')
        for i, name in enumerate(('z', 'a', 'middle')): self.insert(i, name)

    def insert(self, i, name):
        created = f'2026-10-12T00:00:0{2-i%3}Z'
        work = 'work:shared' if i < 2 else 'work:'+name
        # Timestamp, PK and rowid deliberately disagree. Huge unused reason
        # data represents a cold table-page read that metadata does not need.
        self.c.execute('INSERT INTO scheduler_attempt_events VALUES(?,?,?,?,?,?,?)',
            (None,work,'leased' if i%2 else 'ready',i%2+1,'event:'+name,created,'unused'*8192))
        self.c.execute('INSERT INTO scheduler_leases VALUES(?,?,?,?,?,?,?)',
            ('lease:'+name,work,1,i+1,'2026-10-13',created,'TOKEN-NOT-PROJECTION'))

    @staticmethod
    def plain(fold):
        return AppendOnlyFold(fold.name+'.plain',fold.table,fold.columns,key=fold.key,
            init=fold.init,reduce=fold.reduce,snapshot=fold.snapshot,created=fold.created)

    def test_cold_plan_reads_only_cover_and_preserves_exact_rowid_order(self):
        for fold,spec in PAIRS:
            original = [tuple(r) for r in self.c.execute(fold._select,(0,))]
            ensure_indexes(self.c,[spec]); query=fold._cold_select(self.c)
            self.assertEqual(original,[tuple(r) for r in self.c.execute(query,(0,))])
            self.assertEqual(self.plain(fold).read(self.c,source_identity(self.path)),
                             fold.read(self.c,source_identity(self.path)))
            self.assertTrue(any('COVERING INDEX '+spec.name in r[3]
                for r in self.c.execute('EXPLAIN QUERY PLAN '+query,(0,))))
            root=self.c.execute('SELECT rootpage FROM sqlite_master WHERE name=?',(fold.table,)).fetchone()[0]
            ops=list(self.c.execute('EXPLAIN '+query,(0,)))
            cursors={r[2] for r in ops if r[1]=='OpenRead' and r[3]==root}
            self.assertFalse(any(r[1]=='Column' and r[2] in cursors for r in ops))

    def test_append_and_cold_restart_match_original_latest_semantics(self):
        for fold,spec in PAIRS: ensure_indexes(self.c,[spec]); fold.read(self.c,source_identity(self.path))
        self.insert(3,'new')
        for fold,_ in PAIRS:
            trace=[];self.c.set_trace_callback(trace.append)
            got=fold.read(self.c,source_identity(self.path));self.c.set_trace_callback(None)
            self.assertFalse(got.fresh);self.assertEqual(got.added,1)
            self.assertNotIn('INDEXED BY',[q for q in trace if q.startswith('SELECT rowid AS')][-1])
            self.assertEqual(got.state,self.plain(fold).read(self.c,source_identity(self.path)).state)
        clear_folds()
        for fold,_ in PAIRS:
            restored=fold.read(self.c,source_identity(self.path))
            self.assertTrue(restored.fresh);self.assertEqual(restored.count,4)
            self.assertEqual(restored.state,self.plain(fold).read(self.c,source_identity(self.path)).state)

    def test_absent_indexes_keep_original_full_queries(self):
        for fold,_ in PAIRS:
            self.assertEqual(fold._cold_select(self.c),fold._select)
            self.assertEqual(self.plain(fold).read(self.c,source_identity(self.path)),
                             fold.read(self.c,source_identity(self.path)))

    def test_partial_wrong_columns_and_wrong_owner_cannot_hide_history(self):
        for fold,spec in PAIRS:
            statements=(spec.statement+" WHERE work_order_id='missing'",
                        spec.statement.replace(spec.columns,'work_order_id,created_at'),
                        spec.statement.replace('ON '+spec.table+'(', 'ON foreign_table('))
            self.c.execute('CREATE TABLE IF NOT EXISTS foreign_table('+spec.columns.replace(',',' TEXT,')+' TEXT)')
            for sql in statements:
                with self.subTest(table=fold.table,sql=sql):
                    clear_folds();self.c.execute('DROP INDEX IF EXISTS '+spec.name);self.c.execute(sql)
                    with self.assertRaisesRegex(ValueError,'complete fold source'):
                        fold.read(self.c,source_identity(self.path))
            self.c.execute('DROP INDEX '+spec.name); self.c.execute('DROP TABLE foreign_table')

    def test_owner_migration_idempotent_and_not_added_to_worker_open(self):
        names={s.name for _,s in PAIRS}
        self.assertFalse(names & {s.name for s in SCHEDULER_PROJECTION_INDEXES})
        first=apply_projection_indexes(scheduler_db=self.path)
        self.assertTrue(names <= {r['name'] for r in first['scheduler']['created']})
        second=apply_projection_indexes(scheduler_db=self.path)
        self.assertTrue(names <= set(second['scheduler']['present']))

    def test_full_projection_bytes_equal_before_and_after_official_migration(self):
        from tests import test_dashboard_projector as fixture
        case=fixture.DashboardProjectorTests();case.setUp();self.addCleanup(case.tearDown)
        clear_folds(); before=canonical_json(case.projector().build_snapshot())
        apply_projection_indexes(scheduler_db=case.scheduler_path)
        clear_folds(); after=canonical_json(case.projector().build_snapshot())
        self.assertEqual(before,after)


if __name__=='__main__':unittest.main()
