from __future__ import annotations
import sqlite3,tempfile,unittest
from pathlib import Path
from dalton_core.projection_folds import AppendOnlyFold,clear_folds,source_identity
from dalton_core.migrations.projection_indexes import CONNECTOR_FOLD_COVER_INDEXES,ensure_indexes

class FirstCoverTests(unittest.TestCase):
    def setUp(self):
        clear_folds(); self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'source.sqlite'
        self.c=sqlite3.connect(self.path,isolation_level=None);self.c.row_factory=sqlite3.Row
        self.c.execute('CREATE TABLE facts(id TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL,unused_json TEXT NOT NULL)')
        self.c.executemany('INSERT INTO facts VALUES(?,?,?,?)', [('z','r','2026-10-10T00:00:00Z','x'*32768),('a','r','2026-10-10T00:01:00Z','y'*32768)])
    def tearDown(self):self.c.close();self.tmp.cleanup();clear_folds()
    def fold(self,name='covered',index='cover'):
        return AppendOnlyFold(name,'facts','id, role, created_at',key='id',init=list,
                              reduce=lambda state,row:state.append(tuple(row)),cold_cover_index=index)
    def test_cover_first_only_then_real_append_uses_rowid_seek(self):
        self.c.execute('CREATE INDEX cover ON facts(id,role,created_at)');trace=[];self.c.set_trace_callback(trace.append)
        covered=self.fold();a=covered.read(self.c,source_identity(self.path));self.assertTrue(a.fresh)
        self.assertEqual([r[1] for r in a.state],['z','a'])
        selected=[q for q in trace if q.startswith('SELECT rowid AS')][-1];self.assertIn('INDEXED BY cover',selected)
        self.assertTrue(any('COVERING INDEX cover' in r[3] for r in self.c.execute('EXPLAIN QUERY PLAN '+selected)))
        trace.clear();self.c.execute("INSERT INTO facts VALUES('m','r','2026-10-10T00:02:00Z','new')")
        b=covered.read(self.c,source_identity(self.path));self.assertFalse(b.fresh);self.assertEqual(b.added,1)
        self.assertEqual([r[1] for r in b.state],['z','a','m']);selected=[q for q in trace if q.startswith('SELECT rowid AS')][-1]
        self.assertNotIn('INDEXED BY',selected);self.assertTrue(any('INTEGER PRIMARY KEY' in r[3] for r in self.c.execute('EXPLAIN QUERY PLAN '+selected)))
        self.assertEqual(covered.read(self.c,source_identity(self.path)).added,0)
    def test_missing_index_and_default_keep_old_query_and_exact_state(self):
        trace=[];self.c.set_trace_callback(trace.append);a=self.fold().read(self.c,source_identity(self.path));b=self.fold('plain',None).read(self.c,source_identity(self.path))
        self.assertEqual(a,b);self.assertFalse(any('INDEXED BY' in q for q in trace))
    def test_partial_or_wrong_index_is_not_complete_source(self):
        for sql in ["CREATE INDEX cover ON facts(id,role,created_at) WHERE role='other'",'CREATE INDEX cover ON facts(id)']:
            with self.subTest(sql=sql):
                clear_folds();self.c.execute('DROP INDEX IF EXISTS cover');self.c.execute(sql)
                with self.assertRaisesRegex(ValueError,'complete fold'):self.fold().read(self.c,source_identity(self.path))
    def test_restore_guard_cold_rebuilds_with_cover(self):
        self.c.execute('CREATE INDEX cover ON facts(id,role,created_at)');fold=self.fold();fold.read(self.c,source_identity(self.path))
        self.c.execute("UPDATE facts SET id='restored' WHERE rowid=2");trace=[];self.c.set_trace_callback(trace.append)
        result=fold.read(self.c,source_identity(self.path));self.assertTrue(result.fresh);self.assertEqual([r[1] for r in result.state],['z','restored'])
        self.assertTrue(any('INDEXED BY cover' in q for q in trace))
    def test_all_four_registry_indexes_actual_cover_and_idempotence(self):
        for spec in CONNECTOR_FOLD_COVER_INDEXES:
            cols=[v.strip() for v in spec.columns.split(',')]
            self.c.execute(f"CREATE TABLE {spec.table} ("+','.join(v+' TEXT' for v in cols)+', unused_json TEXT)')
            rows=[tuple(f'{label}-{i}' for label in cols)+('private body'*200,) for i in range(3)]
            self.c.executemany(f"INSERT INTO {spec.table} VALUES("+','.join('?' for _ in rows[0])+')',rows)
        first=ensure_indexes(self.c,CONNECTOR_FOLD_COVER_INDEXES);self.assertEqual(len(first['created']),4);self.assertFalse(first['deferred'])
        second=ensure_indexes(self.c,CONNECTOR_FOLD_COVER_INDEXES);self.assertEqual(len(second['present']),4)
        for spec in CONNECTOR_FOLD_COVER_INDEXES:
            key=spec.columns.split(',')[0].strip()
            args=dict(table=spec.table,columns=spec.columns,key=key,init=list,reduce=lambda st,row:st.append(tuple(row)))
            a=AppendOnlyFold(spec.table+'plain',**args).read(self.c,source_identity(self.path))
            b=AppendOnlyFold(spec.table+'cover',**args,cold_cover_index=spec.name).read(self.c,source_identity(self.path))
            self.assertEqual(a,b)
            self.assertIn(spec.name, AppendOnlyFold('probe',**args,cold_cover_index=spec.name)._cold_select(self.c))

if __name__=='__main__':unittest.main()
