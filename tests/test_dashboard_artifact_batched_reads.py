"""Exact artifact batching preserves both historical generations and all rows."""
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from dalton_core import dashboard_projector as projector
from dalton_core.projection_folds import clear_folds
from dalton_core.store import canonical_json, content_hash


class CountingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        self.calls.append((sql,tuple(parameters)))
        return super().execute(sql,parameters)


def database(path=':memory:'):
    connection=sqlite3.connect(path,factory=CountingConnection)
    connection.row_factory=sqlite3.Row;connection.calls=[]
    connection.execute('CREATE TABLE observability_artifact_version_index '
        '(version_id TEXT PRIMARY KEY,artifact_ref TEXT,version_number INTEGER,schema_version TEXT,created_at TEXT)')
    for table in ('observability_artifact_versions','observability_artifact_versions_v2'):
        connection.execute('CREATE TABLE '+table+'(version_id TEXT PRIMARY KEY,artifact_ref TEXT,version_number INTEGER,created_at TEXT,record_json TEXT)')
    return connection


def add(connection, ref, number=1, schema='0.1', version_ref=None):
    identifier=version_ref or ref+':'+str(number)
    wire={'id':identifier,'schema_version':schema,'artifact_ref':ref,'title':'资料 '+ref,
        'created_at':'2026-10-11T00:00:00+00:00','kind':'report','media_type':'text/plain','size_bytes':7,
        'work_order_ref':'work:'+ref,'artifact_content_hash':content_hash(ref),'access_class':'owner',
        'preview_status':'ready',**({'producer_invocation_ref':'invocation:old'}if schema=='0.1'
                                    else{'producer_execution_ref':'execution:new'})}
    wire['content_hash']=content_hash(wire)
    table='observability_artifact_versions'if schema=='0.1'else'observability_artifact_versions_v2'
    connection.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?)',(identifier,ref,number,wire['created_at'],canonical_json(wire)))
    connection.execute('INSERT INTO observability_artifact_version_index VALUES(?,?,?,?,?)',(identifier,ref,number,schema,wire['created_at']))
    return wire


def old_records(connection):
    """Original cold reduction/fill, retained solely as an equivalence oracle."""
    state={}
    for row in connection.execute('SELECT version_id,artifact_ref,version_number,schema_version,created_at FROM observability_artifact_version_index ORDER BY rowid'):
        projector._artifact_index_reduce(state,row)
    for ref,entry in state.items():
        table='observability_artifact_versions'if entry[2]=='0.1'else'observability_artifact_versions_v2'
        record=connection.execute('SELECT record_json FROM '+table+' WHERE version_id=?',(entry[1],)).fetchone()
        if record is None:raise projector.ProjectionSourceError('artifact generation index points to a missing authority row')
        wire=projector._json(record['record_json'],'artifact version')
        state[ref]=entry[:3]+({name:wire[name]for name in projector._ARTIFACT_FIELDS if name in wire},)
    return [state[ref][3]for ref in sorted(state)]


class ArtifactBatchTests(unittest.TestCase):
    def setUp(self):
        clear_folds();self.addCleanup(clear_folds)
        self.connection=database();self.addCleanup(self.connection.close)
        self.identity=('artifact-test-'+str(id(self.connection)),1,1)

    def read(self):return projector.DashboardProjector._latest_artifact_records(self.connection,self.identity)

    def payload_queries(self):
        return [(sql,args)for sql,args in self.connection.calls if 'SELECT version_id,record_json FROM observability_artifact_versions' in sql]

    def test_two_generations_across_batches_every_latest_row_byte_equal(self):
        for n in range(777):add(self.connection,f'artifact:{777-n:04}',schema='0.1'if n%2 else'0.2')
        add(self.connection,'artifact:0777',number=2,schema='0.2')
        expected=canonical_json(old_records(self.connection));self.connection.calls=[]
        actual=self.read();self.assertEqual(canonical_json(actual),expected)
        self.assertEqual(len(actual),777)
        queries=self.payload_queries();self.assertEqual(len(queries),8)
        self.assertTrue(all(1<=len(args)<=256 for _,args in queries))

    def test_duplicate_version_refs_follow_old_body_and_no_duplicate_parameters(self):
        add(self.connection,'artifact:a')
        self.connection.execute('INSERT INTO observability_artifact_version_index VALUES(?,?,?,?,?)',
            ('alias','artifact:b',1,'0.1','2026-10-11'))
        # A malformed historical index can point two artifact keys at one PK.
        # The performance-only change preserves the old reader's behavior.
        rows=[{'artifact_ref':'artifact:a','version_id':'artifact:a:1','version_number':1,'schema_version':'0.1','created_at':'x'},
              {'artifact_ref':'artifact:b','version_id':'artifact:a:1','version_number':1,'schema_version':'0.1','created_at':'x'}]
        state={}
        for row in rows:projector._artifact_index_reduce(state,row)
        class Fold:
            def read(inner,connection,identity,finish):
                finish(state);return type('Read',(),{'state':state})()
        self.connection.calls=[]
        with patch.object(projector,'_ARTIFACT_FOLD',Fold()):actual=self.read()
        self.assertEqual(actual[0],actual[1]);self.assertEqual(self.payload_queries()[0][1],('artifact:a:1',))

    def test_missing_record_rejects_drops_partial_fold_then_retry_is_complete(self):
        for n in range(260):add(self.connection,f'artifact:{n:04}')
        self.connection.execute('DELETE FROM observability_artifact_versions WHERE version_id=?',('artifact:0259:1',))
        for reader in (old_records,lambda c:self.read()):
            with self.assertRaisesRegex(projector.ProjectionSourceError,'missing authority row'):reader(self.connection)
        # Restore exact original without inserting a duplicate index row.
        other=database();wire=add(other,'artifact:0259');other.close()
        self.connection.execute('INSERT INTO observability_artifact_versions VALUES(?,?,?,?,?)',
            (wire['id'],wire['artifact_ref'],1,wire['created_at'],canonical_json(wire)))
        self.assertEqual(canonical_json(self.read()),canonical_json(old_records(self.connection)))

    def test_hash_fields_and_old_missing_optional_fields_remain_unchanged(self):
        wire=add(self.connection,'artifact:old');wire['content_hash']='0'*64
        wire['artifact_content_hash']='a'*64;wire.pop('producer_invocation_ref')
        self.connection.execute('UPDATE observability_artifact_versions SET record_json=?',(canonical_json(wire),))
        # This reader did not validate record hashes. Do not add a new rejection
        # or repair historical fields as part of the batching optimization.
        self.assertEqual(canonical_json(self.read()),canonical_json(old_records(self.connection)))
        self.assertEqual(self.read()[0]['artifact_content_hash'],'a'*64)
        self.assertNotIn('producer_invocation_ref',self.read()[0])

    def test_warm_read_fetches_only_changed_latest_not_old_payloads(self):
        for n in range(520):add(self.connection,f'artifact:{n:04}')
        self.read();self.connection.calls=[];self.read();self.assertEqual(self.payload_queries(),[])
        add(self.connection,'artifact:0500',number=2,schema='0.2');self.connection.calls=[]
        self.assertEqual(canonical_json(self.read()),canonical_json(old_records(self.connection)))
        self.assertEqual(self.payload_queries()[0][1],('artifact:0500:2',))
        self.assertEqual(len(self.payload_queries()),1)

    def test_invalid_json_keeps_original_refusal(self):
        wire=add(self.connection,'artifact:bad');self.connection.execute('UPDATE observability_artifact_versions SET record_json=?',('[]',))
        for reader in (old_records,lambda c:self.read()):
            with self.assertRaisesRegex(projector.ProjectionSourceError,'must be a JSON object'):reader(self.connection)

    def test_serial_batches_share_callers_read_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'artifacts.sqlite'
            reader=database(path);reader.execute('PRAGMA journal_mode=WAL')
            for n in range(300):add(reader,f'artifact:{n:04}')
            reader.commit()
            writer=sqlite3.connect(path)
            expected=canonical_json(old_records(reader))
            read_execute=reader.execute
            invoked=[]
            def execute(sql,parameters=()):
                if invoked==[True] and 'SELECT version_id,record_json FROM' in sql:
                    add(writer,'artifact:0299',number=2,schema='0.2');writer.commit()
                    invoked.append(True)
                result=read_execute(sql,parameters)
                if not invoked and 'SELECT version_id,record_json FROM' in sql:invoked.append(True)
                return result
            try:
                reader.execute=execute
                reader.execute('BEGIN')
                actual=projector.DashboardProjector._latest_artifact_records(reader,('snapshot-'+str(path),1,1))
                self.assertEqual(invoked,[True,True])
                self.assertEqual(canonical_json(actual),expected)
                reader.commit();reader.execute=read_execute
                latest=projector.DashboardProjector._latest_artifact_records(reader,('snapshot-'+str(path),1,1))
                self.assertEqual(canonical_json(latest),canonical_json(old_records(reader)))
                self.assertEqual(latest[-1]['producer_execution_ref'],'execution:new')
            finally:
                reader.close();writer.close()

    def test_pre_generation_index_legacy_branch_unchanged(self):
        add(self.connection,'artifact:a');add(self.connection,'artifact:a',number=2)
        self.connection.execute('DROP TABLE observability_artifact_versions_v2')
        self.connection.calls=[];actual=self.read()
        self.assertEqual(len(actual),1);self.assertEqual(actual[0]['id'],'artifact:a:2')
        self.assertEqual(self.payload_queries(),[])
