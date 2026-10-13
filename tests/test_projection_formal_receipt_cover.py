"""Complete cold metadata coverage; timing never changes projection authority."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from dalton_core import dashboard_projector as projector
from dalton_core.projection_folds import AppendOnlyFold, clear_folds, source_identity
from dalton_core.migrations.projection_indexes import (
    FORMAL_RESULT_FOLD_COVER_INDEX, RESULT_ENVELOPE_FOLD_COVER_INDEX,
    SCHEDULER_PROJECTION_INDEXES, ensure_indexes, apply_projection_indexes,
)
from dalton_core.store import canonical_json

PAIRS = ((projector._FORMAL_RESULT_FOLD, FORMAL_RESULT_FOLD_COVER_INDEX),
         (projector._RESULT_ENVELOPE_FOLD, RESULT_ENVELOPE_FOLD_COVER_INDEX))
SECRET = 'SECRET-OLD-RESPONSE-DO-NOT-LOG'


class FormalReceiptCoverTests(unittest.TestCase):
    def setUp(self):
        clear_folds()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'scheduler.sqlite'
        self.c = sqlite3.connect(self.path, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.addCleanup(clear_folds)
        self.c.execute('CREATE TABLE scheduler_formal_results(work_order_id TEXT,result_record_id TEXT PRIMARY KEY,'
            'attempt_number INTEGER,result_envelope_id TEXT,result_envelope_json TEXT,terminal_state TEXT,created_at TEXT)')
        self.c.execute('CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT PRIMARY KEY,work_order_id TEXT,'
            'attempt_number INTEGER,result_envelope_json TEXT,created_at TEXT)')
        for index, identifier in enumerate(('z', 'a', 'middle')):
            self.insert(identifier, index)

    def insert(self, identifier, index):
        # Deliberately out of timestamp/PK order, with latest attempt ties.
        created = f'2026-10-12T00:00:0{2-index % 3}Z'
        self.c.execute('INSERT INTO scheduler_formal_results VALUES(?,?,?,?,?,?,?)',
            ('work:shared' if index < 2 else 'work:'+identifier, 'formal:'+identifier, index,
             'receipt:'+identifier, SECRET*2048, 'succeeded', created))
        self.c.execute('INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?,?)',
            ('receipt:'+identifier, 'work:shared' if index < 2 else 'work:'+identifier,
             index, SECRET*2048, created))

    def plain(self, fold):
        return AppendOnlyFold(fold.name+'.plain',fold.table,fold.columns,key=fold.key,
            init=fold.init,reduce=fold.reduce,snapshot=fold.snapshot,created=fold.created)

    def test_both_cold_covers_return_every_original_row_in_exact_order_without_response_column_reads(self):
        before = {fold.table:list(self.c.execute('SELECT * FROM '+fold.table+' ORDER BY rowid')) for fold,_ in PAIRS}
        for fold,spec in PAIRS:
            ensure_indexes(self.c,[spec])
            self.assertEqual(self.plain(fold).read(self.c,source_identity(self.path)),
                             fold.read(self.c,source_identity(self.path)))
            sql=fold._cold_select(self.c)
            self.assertTrue(any('COVERING INDEX '+spec.name in row[3]
                for row in self.c.execute('EXPLAIN QUERY PLAN '+sql,(0,))))
            root=self.c.execute('SELECT rootpage FROM sqlite_master WHERE name=?',(fold.table,)).fetchone()[0]
            opcodes=list(self.c.execute('EXPLAIN '+sql,(0,)))
            table_cursors={row[2] for row in opcodes if row[1]=='OpenRead' and row[3]==root}
            self.assertFalse(any(row[1]=='Column' and row[2] in table_cursors for row in opcodes))
            self.assertEqual(before[fold.table],list(self.c.execute('SELECT * FROM '+fold.table+' ORDER BY rowid')))

    def test_warm_append_uses_the_original_rowid_seek_and_exact_latest_reduction(self):
        for fold,spec in PAIRS:ensure_indexes(self.c,[spec]);fold.read(self.c,source_identity(self.path))
        self.insert('new',3)
        for fold,_ in PAIRS:
            trace=[];self.c.set_trace_callback(trace.append)
            result=fold.read(self.c,source_identity(self.path))
            self.assertFalse(result.fresh);self.assertEqual(result.added,1)
            selected=[q for q in trace if q.startswith('SELECT rowid AS')][-1]
            self.assertNotIn('INDEXED BY',selected)
            self.assertTrue(any('INTEGER PRIMARY KEY' in r[3]
                for r in self.c.execute('EXPLAIN QUERY PLAN '+selected)))
            self.assertEqual(result.state,self.plain(fold).read(self.c,source_identity(self.path)).state)

    def test_missing_cover_keeps_old_complete_source_query(self):
        for fold,_ in PAIRS:
            self.assertEqual(fold._cold_select(self.c),fold._select)
            self.assertEqual(self.plain(fold).read(self.c,source_identity(self.path)),
                             fold.read(self.c,source_identity(self.path)))

    def test_partial_wrong_order_and_unique_cover_are_rejected_not_trusted(self):
        for fold,spec in PAIRS:
            columns=spec.columns.split(',')
            for statement in (spec.statement+" WHERE work_order_id='foreign'",
                    spec.statement.replace(spec.columns,','.join(reversed(columns))),
                    spec.statement.replace('CREATE INDEX','CREATE UNIQUE INDEX')):
                with self.subTest(table=fold.table,statement=statement):
                    clear_folds();self.c.execute('DROP INDEX IF EXISTS '+spec.name)
                    self.c.execute(statement)
                    with self.assertRaisesRegex(ValueError,'complete fold source'):
                        fold.read(self.c,source_identity(self.path))

    def test_explicit_owner_migration_is_idempotent_and_worker_open_specs_do_not_expand(self):
        for _,spec in PAIRS:self.assertNotIn(spec.name,{s.name for s in SCHEDULER_PROJECTION_INDEXES})
        first=apply_projection_indexes(scheduler_db=self.path)
        created={r['name'] for r in first['scheduler']['created']}
        self.assertTrue({s.name for _,s in PAIRS}.issubset(created))
        second=apply_projection_indexes(scheduler_db=self.path)
        self.assertTrue({s.name for _,s in PAIRS}.issubset(second['scheduler']['present']))

    def test_full_projection_bytes_are_identical_with_and_without_both_indexes(self):
        from tests import test_dashboard_projector as fixture
        case=fixture.DashboardProjectorTests();case.setUp();self.addCleanup(case.tearDown)
        # Existing end-to-end authority fixture includes terminal result,
        # attempts, receipts, links, costs, artifacts and secret sentinels.
        clear_folds();before=canonical_json(case.projector().build_snapshot())
        apply_projection_indexes(scheduler_db=case.scheduler_path)
        clear_folds();after=canonical_json(case.projector().build_snapshot())
        self.assertEqual(before,after)
        self.assertNotIn(SECRET,after)

    def test_phase_timings_include_failure_progress_but_never_source_or_exception_text(self):
        log=io.StringIO()
        def reduce(state,row):
            if row['fold_rowid']==3:raise ValueError(SECRET)
            state.append(row['result_envelope_id'])
        fold=AppendOnlyFold('scheduler.timing-test','scheduler_result_envelopes',
            'result_envelope_id,created_at',key='result_envelope_id',init=list,reduce=reduce)
        with redirect_stderr(log):
            with self.assertRaisesRegex(ValueError,SECRET):fold.read(self.c,source_identity(self.path))
        events=[json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual([e['event'] for e in events],['projection_fold_started','projection_fold_failed'])
        self.assertEqual(events[-1]['rows'],2);self.assertTrue(events[-1]['fresh'])
        self.assertGreaterEqual(events[-1]['elapsed_seconds'],0)
        self.assertNotIn(SECRET,log.getvalue())
        for event in events:self.assertEqual(set(event),{'event','name','rows','fresh','elapsed_seconds'})
        # Existing failure drops the partial fold; a normal retry is complete.
        fold.reduce=lambda state,row:state.append(row['result_envelope_id'])
        log=io.StringIO()
        with redirect_stderr(log):result=fold.read(self.c,source_identity(self.path))
        self.assertTrue(result.fresh);self.assertEqual(result.count,3)
        self.assertEqual(json.loads(log.getvalue().splitlines()[-1])['rows'],3)

    def test_diagnostic_sink_failure_does_not_change_success_or_original_probe_error(self):
        fold=self.plain(PAIRS[1][0])
        with patch('dalton_core.projection_folds.sys.stderr.write',side_effect=BrokenPipeError('closed')):
            result=fold.read(self.c,source_identity(self.path))
        self.assertEqual(result.count,3)
        self.c.close();log=io.StringIO()
        with redirect_stderr(log):
            with self.assertRaises(sqlite3.ProgrammingError):fold.read(self.c,source_identity(self.path))
        self.assertEqual(json.loads(log.getvalue().splitlines()[-1])['event'],'projection_fold_failed')


if __name__=='__main__':unittest.main()
