"""Configured raw roots and exact archive parents survive every ordinary boundary."""
import json,unittest
from datetime import date,datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock
from dalton_core import material_acquisition as ma
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.material_targeted_sec import targeted_sec_parameters,TargetedSecError
from dalton_core.mission_source_discovery import MissionSourceDiscoveryCoordinator,discovery_query_hash
from dalton_core.raw_spool import RawSpoolReader
from dalton_core.sec_filings_index_cli import run_discovery
from dalton_core.sec_submissions_pages import verify_archive_parent
from dalton_core.store import DaltonStore,canonical_json,content_hash
from dalton_core.migrations.projection_indexes import apply_projection_indexes
from tests import test_sec_submissions_archive_pages as archive
from tests import test_sec_filings_index_discovery as fixture

class ArchiveParentRoutingTests(unittest.TestCase):
    setUp=fixture.SecFilingsIndexDiscoveryTests.setUp
    setup_query=archive.SecArchivePagesTests.setup_query

    def root(self):
        self.setup_query();self.configured=self.state/'private-sec-originals'
        result=self.page(self.binding,None)
        self.assertEqual(result['status'],'succeeded',result['failure_reason'])
        self.first=result
        self.parent={'discovery_ref':result['discovery_ref'],'discovery_hash':result['discovery_hash'],
            'envelope_ref':result['index']['source_envelope_ref'],'envelope_hash':result['index']['source_envelope_hash']}
        self.core=DaltonStore(str(self.state/'core.sqlite'));self.addCleanup(self.core.close)
        return result

    def page(self,binding,cursor):
        return run_discovery(state_dir=self.state,governance=self.governance,plan=self.plan,company_ref=fixture.ACN,
            spec_ref='annual-report-10k',requested_by=self.principal,mission_version_ref=self.mission['id'],
            mission_version_hash=self.mission['content_hash'],as_of=date(2026,9,8),
            summary_dir=self.state/('child-archive'if cursor else'child-root'),adapter=self.adapter,
            targeted_binding=binding,cursor=cursor,spool_dir=self.configured)

    def archive_params(self,**kwargs):
        return targeted_sec_parameters(self.state,plan=self.plan,company_ref=fixture.ACN,spec_ref='annual-report-10k',
            as_of=date(2026,9,8),mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            query_ref=self.query['query_id'],cursor=self.first['next_cursor'],spool_dir=self.configured,
            archive_parent=self.parent,**kwargs)

    def test_configured_originals_exact_binding_child_and_settlement(self):
        self.root();params,b=self.archive_params()
        self.assertEqual(b['archive_parent'],self.parent)
        result=self.page(b,self.first['next_cursor'])
        self.assertEqual((result['status'],result['provider_calls']),('succeeded',1),result['failure_reason'])
        self.assertEqual(result['targeted_binding'],b)
        missions=Mock();dispatch={'dispatch_id':'fixture','ticket_ref':'fixture','company_ref':fixture.ACN,
            'spec_ref':'annual-report-10k','query_hash':result['query_hash'],'mission_version_ref':self.mission['id'],
            'mission_version_hash':self.mission['content_hash']}
        missions.open_discovery_dispatches.return_value=[dispatch]
        missions.settle_discovery_dispatch.side_effect=lambda ref,**kw:{'status':kw['status']}
        launcher=Mock(state_dir=self.state,spool_dir=self.configured)
        launcher.status.return_value={'status':'succeeded','id':'fixture','targeted_binding':b,
            'cursor':self.first['next_cursor'],'as_of':'2026-09-08','summary':result}
        co=SimpleNamespace(search_launcher=launcher,missions=missions,source_ref='source:sec-edgar',plan=self.plan)
        self.assertEqual(MissionSourceDiscoveryCoordinator.settle_dispatches(co)[0]['status'],'succeeded')
        with self.assertRaisesRegex(TargetedSecError,'RawSpoolError'):
            targeted_sec_parameters(self.state,plan=self.plan,company_ref=fixture.ACN,spec_ref='annual-report-10k',
                as_of=date(2026,9,8),mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
                binding=b,cursor=self.first['next_cursor'])

    def test_coordinator_derives_exact_parent_and_honors_configured_spool(self):
        self.root();missions=CoverageMissionAuthority(self.core)
        auth=missions.authorize_source_discovery(company_ref=fixture.ACN,source_ref='source:sec-edgar',requested_by=self.principal,
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'])
        d=missions.record_discovery_dispatch(authorization=auth,discovery_plan_ref=self.plan['id'],
            discovery_plan_hash=self.plan['content_hash'],spec_ref='annual-report-10k',query_hash=self.first['query_hash'],
            ticket_ref='sec-filings-index:rootfixture')
        missions.settle_discovery_dispatch(d['dispatch_id'],status='succeeded')
        ma.record_targeted_event(self.state,self.query['query_id'],status='launched',kind='archive_ready',
            ticket_ref='sec-filings-index:rootfixture',dispatch_ref=d['dispatch_id'])
        launcher=Mock(state_dir=self.state,spool_dir=self.configured);launcher.running.return_value=False
        launcher.status.return_value={'status':'succeeded','id':'sec-filings-index:rootfixture','cursor':None,
            'targeted_binding':self.binding,'as_of':'2026-09-08','mission_version_ref':self.mission['id'],
            'mission_version_hash':self.mission['content_hash'],'summary':self.first}
        launcher.start.return_value={'id':'sec-filings-index:archivefixture'}
        co=SimpleNamespace(search_launcher=launcher,missions=missions,store=self.core,source_ref='source:sec-edgar',plan=self.plan,
            clock=lambda:datetime(2026,9,8,tzinfo=timezone.utc),_health_hold=lambda:None,_budget=lambda cap:{'remaining':1},
            _missing_periods=lambda *a:[])
        result=MissionSourceDiscoveryCoordinator.launch_targeted_discovery(co)
        self.assertEqual(result['status'],'launched',result)
        self.assertEqual(launcher.start.call_args.kwargs['targeted_binding']['archive_parent'],self.parent)

    def test_actual_launcher_revalidates_exact_binding_before_spawn(self):
        from dalton_core import mission_source_discovery as module
        from dalton_core.mission_source_discovery import SecFilingsIndexLauncher,DiscoveryLaunchRejected
        from unittest.mock import patch
        import subprocess
        self.root();params,b=self.archive_params()
        gp=self.state/'governance.json';gp.write_text(json.dumps(self.governance.wire))
        pp=self.state/'plan.json';pp.write_text(json.dumps(self.plan))
        auth=CoverageMissionAuthority(self.core).authorize_source_discovery(company_ref=fixture.ACN,
            source_ref='source:sec-edgar',requested_by=self.principal,mission_version_ref=self.mission['id'],
            mission_version_hash=self.mission['content_hash'])
        launcher=SecFilingsIndexLauncher(state_dir=self.state,governance_path=gp,plan_path=pp,spool_dir=self.configured)
        # Actual launch resolves the code-owned extended plan; freeze that
        # same effective authority, as the production coordinator does.
        _,b=targeted_sec_parameters(self.state,plan=launcher.load_plan(),company_ref=fixture.ACN,
            spec_ref='annual-report-10k',as_of=date(2026,9,8),mission_version_ref=self.mission['id'],
            mission_version_hash=self.mission['content_hash'],query_ref=self.query['query_id'],
            cursor=self.first['next_cursor'],spool_dir=self.configured,archive_parent=self.parent)
        spawn=Mock(return_value=SimpleNamespace(pid=12345,poll=lambda:None))
        with patch.object(module,'subprocess',SimpleNamespace(Popen=spawn,DEVNULL=subprocess.DEVNULL,STDOUT=subprocess.STDOUT)):
            forged={**b,'archive_parent':{**b['archive_parent'],'envelope_hash':'0'*64}}
            with self.assertRaises(DiscoveryLaunchRejected):launcher.start(authorization=auth,
                spec_ref='annual-report-10k',as_of=date(2026,9,8),targeted_binding=forged,cursor=self.first['next_cursor'])
            spawn.assert_not_called()
            ticket=launcher.start(authorization=auth,spec_ref='annual-report-10k',as_of=date(2026,9,8),
                targeted_binding=b,cursor=self.first['next_cursor'])
        self.assertEqual(ticket['targeted_binding'],b)
        args=spawn.call_args.args[0];self.assertEqual(args[args.index('--spool-dir')+1],str(self.configured))
        self.assertEqual(json.loads(args[args.index('--targeted-binding')+1]),b)

    def test_same_index_name_owned_by_another_table_is_deferred_unchanged(self):
        self.root();c=self.core.connection
        c.execute('DROP INDEX IF EXISTS idx_connector_call_query_hash')
        c.execute('CREATE TABLE index_owner_counterexample(value TEXT)')
        c.execute('CREATE INDEX idx_connector_call_query_hash ON index_owner_counterexample(value)')
        before=c.execute("SELECT tbl_name,sql FROM sqlite_master WHERE name='idx_connector_call_query_hash'").fetchone()
        report=apply_projection_indexes(core_db=self.state/'core.sqlite')
        self.assertIn('wrong shape',report['core']['deferred']['idx_connector_call_query_hash'])
        self.assertNotIn('idx_connector_call_query_hash',report['core']['present'])
        after=c.execute("SELECT tbl_name,sql FROM sqlite_master WHERE name='idx_connector_call_query_hash'").fetchone()
        self.assertEqual(tuple(before),tuple(after))

    def test_legacy_limit_and_lazy_parent_validation_preserve_first_match(self):
        self.root();params,b=self.archive_params();c=self.core.connection
        columns=[row[1] for row in c.execute('PRAGMA table_info(connector_source_envelopes)')]
        template=dict(c.execute('SELECT * FROM connector_source_envelopes WHERE source_envelope_id=?',
            (self.parent['envelope_ref'],)).fetchone())
        values=[]
        for i in range(12):
            row={**template,'source_envelope_id':f'unrelated-parent:{i}',
                'created_at':'2000-01-01T00:00:00+00:00','record_json':json.dumps({'id':f'bad:{i}'})}
            values.append(tuple(row[k] for k in columns))
        with self.core._transaction():
            c.executemany('INSERT INTO connector_source_envelopes('+','.join(columns)+') VALUES('+','.join('?'for k in columns)+')',values)
        old='SELECT e.source_envelope_id FROM connector_source_envelopes e JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref WHERE c.query_hash=? ORDER BY e.created_at DESC LIMIT 8'
        query=json.loads(self.first['next_cursor'])['root_query_hash']
        expected=c.execute(old,(query,)).fetchall()
        self.assertEqual(len(expected),8)
        self.assertEqual(expected[0][0],self.parent['envelope_ref'])
        # The first valid original succeeds without materializing later
        # corrupt rows, just as the previous per-candidate loop did.
        record=verify_archive_parent(c,RawSpoolReader(self.configured),params)
        self.assertEqual(record['id'],expected[0][0])

    def test_wrong_parent_query_source_hash_and_company_do_not_authorize_page(self):
        self.root()
        for key in ['discovery_hash','envelope_hash','envelope_ref']:
            old=self.parent;self.parent={**old,key:'0'*64}
            with self.assertRaises(ValueError):self.archive_params()
            self.parent=old
        for field,value in [('source_ref','source:web-search'),('query_hash','0'*64),('company_ref','company:other'),('mission_version_ref','mission:other')]:
            original=self.core.connection.execute('SELECT record_json FROM coverage_mission_source_discoveries WHERE record_id=?',(self.parent['discovery_ref'],)).fetchone()[0]
            self.core.connection.execute('DROP TRIGGER IF EXISTS coverage_mission_source_discoveries_no_update')
            self.core.connection.execute('PRAGMA foreign_keys=OFF')
            wire=json.loads(original);forged={**wire,field:value};forged.pop('content_hash');forged['content_hash']=content_hash(forged)
            old=self.parent;self.parent={**old,'discovery_hash':forged['content_hash']}
            self.core.connection.execute('UPDATE coverage_mission_source_discoveries SET '+field+'=?,record_json=?,content_hash=? WHERE record_id=?',(value,canonical_json(forged),forged['content_hash'],old['discovery_ref']))
            with self.assertRaises(ValueError):self.archive_params()
            self.core.connection.execute('UPDATE coverage_mission_source_discoveries SET '+field+'=?,record_json=?,content_hash=? WHERE record_id=?',(wire[field],original,wire['content_hash'],old['discovery_ref']))
            self.parent=old
            self.core.connection.execute('PRAGMA foreign_keys=ON')

    def test_missing_and_corrupt_original_are_explicit_single_request_refusals(self):
        self.root();e=self.core.connection.execute('SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?',(self.parent['envelope_ref'],)).fetchone()[0]
        digest=json.loads(e)['raw_response_hash'];reader=RawSpoolReader(self.configured)
        path=reader._objects/digest[:2]/digest;raw=path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(TargetedSecError,'RawSpoolError'):self.archive_params()
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(TargetedSecError,'SecPageError'):self.archive_params()
        path.write_bytes(raw)
        self.assertEqual(self.archive_params()[1]['archive_parent'],self.parent)

    def test_old_lookup_bytes_equal_indexed_and_exact_parent_ignores_unrelated_history(self):
        self.root();params,b=self.archive_params();c=self.core.connection
        oldsql='SELECT e.source_envelope_id FROM connector_source_envelopes e JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref WHERE c.query_hash=? ORDER BY e.created_at DESC LIMIT 8'
        query=json.loads(self.first['next_cursor'])['root_query_hash']
        # Unrelated wide historic call payloads must not be materialized by
        # either the exact PK path or the indexed compatibility lookup.
        columns=[row[1] for row in c.execute('PRAGMA table_info(connector_call_specs)')]
        template=dict(c.execute('SELECT * FROM connector_call_specs LIMIT 1').fetchone())
        values=[]
        for i in range(2000):
            row={**template,'call_spec_id':f'unrelated:{i}','query_hash':f'{i:064x}',
                'record_json':json.dumps({'id':f'unrelated:{i}','padding':'x'*2048})}
            values.append(tuple(row[k] for k in columns))
        with self.core._transaction():
            c.executemany('INSERT INTO connector_call_specs('+','.join(columns)+') VALUES('+','.join('?' for k in columns)+')',values)
        old=[tuple(r)for r in c.execute(oldsql,(query,))]
        apply_projection_indexes(core_db=self.state/'core.sqlite')
        new=verify_archive_parent(c,RawSpoolReader(self.configured),params)
        exact=verify_archive_parent(c,RawSpoolReader(self.configured),params,parent_binding=self.parent)
        self.assertEqual(new,exact);self.assertEqual(old,[(new['id'],)])
        trace=[];c.set_trace_callback(trace.append)
        verify_archive_parent(c,RawSpoolReader(self.configured),params,parent_binding=self.parent)
        c.set_trace_callback(None)
        self.assertFalse(any('JOIN connector_invocations' in q for q in trace))
        plan=[r['detail']for r in c.execute('EXPLAIN QUERY PLAN SELECT e.source_envelope_id FROM connector_source_envelopes e JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref WHERE c.query_hash=? ORDER BY e.created_at DESC LIMIT 8',(query,))]
        self.assertTrue(any('idx_connector_call_query_hash' in d for d in plan),plan)
        self.assertFalse(any(d.startswith('SCAN ') for d in plan),plan)
        first=apply_projection_indexes(core_db=self.state/'core.sqlite');second=apply_projection_indexes(core_db=self.state/'core.sqlite')
        self.assertEqual(first,second)
        c.execute('DROP INDEX idx_connector_call_query_hash');c.execute('CREATE INDEX idx_connector_call_query_hash ON connector_call_specs(operation)')
        report=apply_projection_indexes(core_db=self.state/'core.sqlite')
        self.assertIn('wrong',json.dumps(report).lower())

if __name__=='__main__':unittest.main()
