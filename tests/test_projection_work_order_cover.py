"""Cold projection reads every WorkOrder identity/capability without prompt I/O."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from dalton_core.dashboard_projector import _WORK_ORDER_FOLD, _WORK_ORDER_COLUMNS, _work_order_entry
from dalton_core.projection_folds import AppendOnlyFold, clear_folds, source_identity
from dalton_core.migrations.projection_indexes import (
    WORK_ORDER_FOLD_COVER_INDEX as SPEC, SCHEDULER_PROJECTION_INDEXES,
    ensure_indexes, apply_projection_indexes,
)
from tests import test_scheduler as fixture


def expression_cover_probe():
    """Independent engine capability, not the production fold/index SQL."""
    with sqlite3.connect(':memory:') as probe:
        probe.execute('CREATE TABLE capability_probe(identity TEXT,payload TEXT)')
        payload=json.dumps({'capabilities':['read','核验'], 'body':'x'*20000},ensure_ascii=False)
        probe.execute('INSERT INTO capability_probe VALUES(?,?)',('synthetic',payload))
        probe.execute("CREATE INDEX expression_probe ON capability_probe(identity,json_extract(payload,'$.capabilities'))")
        sql="SELECT identity,json_extract(payload,'$.capabilities') FROM capability_probe INDEXED BY expression_probe"
        values=probe.execute(sql).fetchall()
        opcodes=probe.execute('EXPLAIN '+sql).fetchall()
        rootpage=probe.execute("SELECT rootpage FROM sqlite_master WHERE name='capability_probe'").fetchone()[0]
        cursors={r[2] for r in opcodes if r[1]=='OpenRead' and r[3]==rootpage}
        raw_reads=[r for r in opcodes if r[1]=='Column' and r[2] in cursors]
        return values,raw_reads


class WorkOrderCoverTests(unittest.TestCase):
    def setUp(self):
        clear_folds()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'authority.sqlite'
        self.c = sqlite3.connect(self.path, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.addCleanup(clear_folds)
        self.c.execute('CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,created_at TEXT,work_order_json TEXT)')
        for ref, caps in (('z', ['read_original', '核验数字']), ('a', []), ('middle', ['formatter'])):
            self.insert(ref, caps)

    def insert(self, ref, caps):
        work = fixture.work_order('work:' + ref)
        work['requested_capabilities'] = caps
        work['question'] = 'unread historic prompt ' * 5000
        self.c.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?)',
                       (work['id'], work['created_at'], json.dumps(work, ensure_ascii=False)))

    def plain(self):
        return AppendOnlyFold('scheduler.plain.workorders', 'scheduler_work_orders', _WORK_ORDER_COLUMNS,
            key='work_order_id', init=dict,
            reduce=lambda state,row:state.__setitem__(row['work_order_id'], _work_order_entry(row)))

    def test_first_read_full_expression_cover_has_same_complete_state(self):
        before = list(self.c.execute('SELECT work_order_id,created_at,work_order_json FROM scheduler_work_orders ORDER BY rowid'))
        created = ensure_indexes(self.c, [SPEC])
        self.assertEqual([x['name'] for x in created['created']], [SPEC.name])
        old = self.plain().read(self.c, source_identity(self.path))
        new = _WORK_ORDER_FOLD.read(self.c, source_identity(self.path))
        self.assertEqual(old, new)
        self.assertEqual(list(new.state), ['work:z', 'work:a', 'work:middle'])
        sql = _WORK_ORDER_FOLD._cold_select(self.c)
        plan = [r[3] for r in self.c.execute('EXPLAIN QUERY PLAN ' + sql, (0,))]
        self.assertTrue(any('USING INDEX ' + SPEC.name in p or 'USING COVERING INDEX ' + SPEC.name in p for p in plan), plan)
        self.assertEqual(before, list(self.c.execute('SELECT work_order_id,created_at,work_order_json FROM scheduler_work_orders ORDER BY rowid')))

    def test_expression_cover_probe_preserves_values_on_every_engine(self):
        values,_=expression_cover_probe()
        self.assertEqual(len(values),1)
        self.assertEqual(values[0][0],'synthetic')
        self.assertEqual(json.loads(values[0][1]),['read','核验'])

    def test_supported_engine_reads_no_historic_json_column(self):
        _,raw_reads=expression_cover_probe()
        if raw_reads:
            self.skipTest(f'SQLite {sqlite3.sqlite_version}: independent expression-cover probe rereads the source table; cold-cover optimization unavailable')
        ensure_indexes(self.c,[SPEC])
        sql=_WORK_ORDER_FOLD._cold_select(self.c)
        # Expression indexes may open a deferred table cursor; prove no Column
        # reads that cursor, so no historic work_order_json page is fetched.
        opcodes = list(self.c.execute('EXPLAIN ' + sql, (0,)))
        rootpage = self.c.execute("SELECT rootpage FROM sqlite_master WHERE name='scheduler_work_orders'").fetchone()[0]
        table_cursors = {r[2] for r in opcodes if r[1]=='OpenRead' and r[3]==rootpage}
        self.assertFalse(any(r[1]=='Column' and r[2] in table_cursors for r in opcodes))

    def test_warm_append_retains_exact_rowid_seek(self):
        ensure_indexes(self.c, [SPEC])
        first = _WORK_ORDER_FOLD.read(self.c, source_identity(self.path))
        trace = []
        self.c.set_trace_callback(trace.append)
        self.insert('new', ['search'])
        next_read = _WORK_ORDER_FOLD.read(self.c, source_identity(self.path))
        self.assertFalse(next_read.fresh)
        self.assertEqual(next_read.added, 1)
        self.assertEqual(next_read.count, first.count + 1)
        sql = [q for q in trace if q.startswith('SELECT rowid AS')][-1]
        self.assertNotIn('INDEXED BY', sql)
        self.assertTrue(any('INTEGER PRIMARY KEY' in r[3] for r in self.c.execute('EXPLAIN QUERY PLAN ' + sql)))
        self.assertEqual(next_read.state['work:new']['wire']['requested_capabilities'], ['search'])

    def test_old_authority_without_index_preserves_complete_fallback(self):
        trace=[]; self.c.set_trace_callback(trace.append)
        self.assertEqual(self.plain().read(self.c, source_identity(self.path)),
                         _WORK_ORDER_FOLD.read(self.c, source_identity(self.path)))
        self.assertFalse(any('INDEXED BY' in q for q in trace))

    def test_expression_identity_and_partial_source_are_not_trusted(self):
        for statement in (
            SPEC.statement.replace('$.requested_capabilities', '$.input_refs'),
            SPEC.statement + " WHERE work_order_id='work:a'",
            SPEC.statement.replace('CREATE INDEX', 'CREATE UNIQUE INDEX'),
        ):
            with self.subTest(statement=statement):
                clear_folds(); self.c.execute('DROP INDEX IF EXISTS ' + SPEC.name)
                self.c.execute(statement)
                with self.assertRaisesRegex(ValueError, 'complete fold source'):
                    _WORK_ORDER_FOLD.read(self.c, source_identity(self.path))

    def test_index_not_built_by_ordinary_worker_open_spec_list(self):
        self.assertNotIn(SPEC.name, {spec.name for spec in SCHEDULER_PROJECTION_INDEXES})
        # Use the actual existing explicit migration entry point; absent unrelated
        # tables defer as before, while this real table is fully indexed once.
        first = apply_projection_indexes(scheduler_db=self.path)
        self.assertIn(SPEC.name, [r['name'] for r in first['scheduler']['created']])
        second = apply_projection_indexes(scheduler_db=self.path)
        self.assertIn(SPEC.name, second['scheduler']['present'])

    def test_locked_build_defers_then_can_be_reentered_without_losing_history(self):
        other = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other.close)
        other.execute('BEGIN IMMEDIATE')
        report = ensure_indexes(self.c, [SPEC], busy_timeout_ms=1)
        self.assertIn(SPEC.name, report['deferred'])
        self.assertEqual(_WORK_ORDER_FOLD.read(self.c, source_identity(self.path)).count, 3)
        other.rollback()
        self.assertEqual(ensure_indexes(self.c, [SPEC])['deferred'], {})
        clear_folds()
        self.assertEqual(_WORK_ORDER_FOLD.read(self.c, source_identity(self.path)),
                         self.plain().read(self.c, source_identity(self.path)))

    def test_same_fold_does_not_mix_different_authority_files(self):
        ensure_indexes(self.c, [SPEC]); a=_WORK_ORDER_FOLD.read(self.c, source_identity(self.path))
        second_path=Path(self.tmp.name)/'other.sqlite'
        other=sqlite3.connect(second_path,isolation_level=None);other.row_factory=sqlite3.Row
        self.addCleanup(other.close)
        other.execute('CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,created_at TEXT,work_order_json TEXT)')
        work=fixture.work_order('work:other');other.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?)',(work['id'],work['created_at'],json.dumps(work)))
        ensure_indexes(other,[SPEC]); b=_WORK_ORDER_FOLD.read(other,source_identity(second_path))
        self.assertEqual(b.count,1);self.assertEqual(list(b.state),['work:other'])
        self.assertEqual(_WORK_ORDER_FOLD.read(self.c,source_identity(self.path)).state,a.state)
