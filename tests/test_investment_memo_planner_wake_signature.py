"""A bounded indexed hint cannot stand in for the strong memo reader."""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.investment_memo_planner_feedback import signature,_read,_SIGNATURE_SQL,_SIGNATURE_RANGE

class MemoPlannerWakeSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'scheduler.sqlite'
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE scheduler_formal_results(work_order_id TEXT UNIQUE, result_envelope_json TEXT)')
            c.execute('CREATE TABLE scheduler_work_orders(work_order_id TEXT UNIQUE,work_order_json TEXT)')
            c.executemany('INSERT INTO scheduler_work_orders VALUES(?,?)',
                [(f'work:cockpit-other-{i:032x}','not-json-'+'x'*2000)for i in range(2000)])
            c.execute('INSERT INTO scheduler_formal_results VALUES(?,?)',
                ('work:cockpit-investment_memo-'+'a'*32,'not-authority'))

    @contextlib.contextmanager
    def deny_payloads(self,path):
        c=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        def guard(op,table,column,db,trigger):
            if op==sqlite3.SQLITE_READ and (table=='scheduler_work_orders' or column=='result_envelope_json'):
                self.denied.append((table,column));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(guard)
        try:yield c
        finally:c.close()

    def test_indexed_formal_only_no_work_or_response_payload_even_with_large_unrelated_history(self):
        self.denied=[]
        with patch('dalton_core.planner_paid_output_recovery._authority_connection',self.deny_payloads):
            value=signature(self.root)
        self.assertNotIn(value,('unavailable','not_installed'));self.assertEqual(self.denied,[])
        with sqlite3.connect(self.path)as c:
            plans=[r[3]for r in c.execute('EXPLAIN QUERY PLAN '+_SIGNATURE_SQL,_SIGNATURE_RANGE)]
        self.assertTrue(any('COVERING INDEX' in p and 'SEARCH' in p for p in plans))
        self.assertFalse(any('SCAN' in p for p in plans))

    def test_new_formal_success_or_failure_changes_hint_but_unrelated_purpose_does_not(self):
        first=signature(self.root)
        with sqlite3.connect(self.path)as c:
            c.execute('INSERT INTO scheduler_formal_results VALUES(?,?)',('work:cockpit-investment_memo_verifier-'+'b'*32,'failed'))
            c.execute('INSERT INTO scheduler_formal_results VALUES(?,?)',('work:cockpit-other-'+'c'*32,'succeeded'))
        self.assertEqual(signature(self.root),first)
        with sqlite3.connect(self.path)as c:
            c.execute('INSERT INTO scheduler_formal_results VALUES(?,?)',('work:cockpit-investment_memo-'+'d'*32,'failed'))
        second=signature(self.root);self.assertNotEqual(second,first)
        with sqlite3.connect(self.path)as c:
            c.execute('INSERT INTO scheduler_formal_results VALUES(?,?)',('work:cockpit-investment_memo-'+'e'*32,'succeeded'))
        self.assertNotEqual(signature(self.root),second)

    def test_hint_with_forged_namespace_is_never_a_verified_gap(self):
        value=signature(self.root);self.assertNotEqual(value,'unavailable')
        self.assertEqual(_read(None,state_dir=self.root,mission={},company_ref='company:synthetic',config={},playbook={}),
                         {'status':'not_recorded','items':[]})

    def test_missing_wrong_or_unindexed_schema_remains_unavailable(self):
        for schema in ('CREATE TABLE unrelated(x TEXT)',
                       'CREATE TABLE scheduler_formal_results(wrong TEXT)',
                       'CREATE TABLE scheduler_formal_results(work_order_id INTEGER UNIQUE)',
                       'CREATE TABLE scheduler_formal_results(work_order_id TEXT)',
                       'CREATE TABLE scheduler_formal_results(work_order_id TEXT,result_envelope_json TEXT)'):
            with self.subTest(schema=schema),tempfile.TemporaryDirectory() as raw:
                with sqlite3.connect(Path(raw)/'scheduler.sqlite')as c:c.execute(schema)
                self.assertEqual(signature(raw),'unavailable')
        with tempfile.TemporaryDirectory()as raw:self.assertEqual(signature(raw),'not_installed')

    def test_partial_or_composite_index_cannot_trigger_fallback_table_scan(self):
        for index in ('CREATE UNIQUE INDEX bad ON scheduler_formal_results(work_order_id) WHERE work_order_id IS NOT NULL',
                      'CREATE UNIQUE INDEX bad ON scheduler_formal_results(work_order_id,other)'):
            with self.subTest(index=index),tempfile.TemporaryDirectory()as raw:
                with sqlite3.connect(Path(raw)/'scheduler.sqlite')as c:
                    c.execute('CREATE TABLE scheduler_formal_results(work_order_id TEXT,other TEXT)');c.execute(index)
                self.assertEqual(signature(raw),'unavailable')

    def test_cold_reader_error_remains_unknown_without_creating_authority(self):
        with patch('dalton_core.planner_paid_output_recovery._authority_connection',side_effect=sqlite3.OperationalError('interrupted')):
            self.assertEqual(signature(self.root),'unavailable')
