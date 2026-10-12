from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.connector_governance import ConnectorGovernance, build_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.sec_financials_core import KIND, sec_financials_output_schema
from dalton_core.sec_financials_cli import build_parser, run
from dalton_core.sec_financials_launcher import SecFinancialsLauncher
from dalton_core.sec_statement_cached_reparse import original, prepare, request_path, validate_request
from dalton_core.sec_statement_form_migration import ACTOR, preview, apply
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_sec_financials_cli import fixture_parse

# Exact pre-amendment DDL captured read-only from the deployed legacy Core.
# Keep independent of migrate() and the new schema so prefix errors cannot
# manufacture a falsely "legacy" fixture. SQL SHA256: b76f39ef45822789c236600e0e76510e0cb8f5d802d1062b25da7df21d1e1d63
CAPTURED_LEGACY_FILINGS_DDL = "CREATE TABLE coverage_mission_statement_filings (\n    ingest_id TEXT PRIMARY KEY,\n    dispatch_id TEXT NOT NULL\n        REFERENCES coverage_mission_statement_dispatches(dispatch_id),\n    company_ref TEXT NOT NULL,\n    cik TEXT NOT NULL,\n    entity_name TEXT NOT NULL,\n    accession TEXT NOT NULL,\n    form TEXT NOT NULL CHECK(form IN ('10-Q','10-K')),\n    filed TEXT NOT NULL,\n    report_date TEXT NOT NULL,\n    line_count INTEGER NOT NULL,\n    source_record_refs_json TEXT NOT NULL,\n    governance_ref TEXT NOT NULL,\n    governance_hash TEXT NOT NULL,\n    recorded_at TEXT NOT NULL,\n    content_hash TEXT NOT NULL,\n    -- A filing is ingested once per company. A second run of the same 10-Q is\n    -- the same filing, not a second set of 495 lines.\n    UNIQUE(company_ref, accession)\n)"

COMPANY = 'company:sec-cik:0001467373'

class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir()
        self.govdir = self.state / 'connector-governance'; self.govdir.mkdir()
        self.old = build_governance_record(KIND, approved_by='human:synthetic-owner', status='approved',
                 effective_from='2026-01-01T00:00:00+00:00', version=4)
        self.oldpath = self.govdir / 'sec-financial-statements-v4.json'
        self.oldpath.write_text(json.dumps(self.old))
        self.new = build_governance_record(KIND, approved_by='human:synthetic-owner', status='approved',
                 effective_from='2026-01-01T00:00:00+00:00', version=5)
        self.newpath = self.govdir / 'sec-financial-statements-v5.json'
        self.newpath.write_text(json.dumps(self.new))

    def captured(self):
        raw = fixture_parse(cik='1467373', entity_name='Synthetic Services Corporation')
        f = raw['filings'][0]
        # SEC's accession prefix is the submitting agent, not necessarily issuer.
        f.update(accession='0001234567-26-000046', form='10-Q/A')
        return raw

    def args(self, gov, *, replay=None):
        flags = ['--state-dir', str(self.state), '--governance', str(gov),
            '--ticker', 'ACN', '--form', '10-Q', '--limit', '8',
            '--summary-dir', str(self.out), '--quiet']
        return build_parser().parse_args(flags + (['--replay-request', str(replay)] if replay else ['--allow-network']))

    def failure(self):
        digest = hashlib.sha256(f'sec-financials-run|ACN|10-Q|8|{self.old["content_hash"]}'.encode()).hexdigest()[:24]
        self.ref = 'sec-financials-run:' + digest
        self.out = self.state / 'sec-financials-runs' / digest; self.out.mkdir(parents=True)
        args = self.args(self.oldpath)
        with patch('dalton_core.sec_financials_cli.parse_live', return_value=self.captured()) as fetch:
            summary = run(args)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(summary['status'], 'failed')
        self.reason = summary['failure_reason']
        self.ticket = {'id': self.ref, 'status':'failed', 'pid':-1, 'ticker':'ACN', 'form':'10-Q', 'limit':8,
            'governance_ref':self.old['id'], 'governance_hash':self.old['content_hash'],
            'transport':'public-https', 'command':['python','-m','dalton_core.sec_financials_cli',
              '--governance',str(self.oldpath),'--ticker','ACN','--form','10-Q','--limit','8','--allow-network']}
        (self.out / 'ticket.json').write_text(json.dumps(self.ticket))
        return summary

    def mission(self):
        self.store = DaltonStore(self.state / 'core.sqlite'); self.addCleanup(self.store.close)
        methods = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(methods)
        self.missions.create_mission(params.pop('mission_ref'), **params)
        authorization = self.missions.sec_lane_authorization_for_company(COMPANY)
        for n in range(8):
            d = self.missions.queue_statement_dispatch(authorization=authorization, form='10-Q', filing_limit=8,
                                                       retry_salt=f'old-contract-{n}')
            self.missions.mark_statement_dispatch_launched(d['dispatch_id'], self.ref)
            self.missions.settle_statement_dispatch(d['dispatch_id'], outcome='failed', failure_reason=self.reason)
        self.launcher = SecFinancialsLauncher(state_dir=self.state, governance_path=self.newpath,
                                              scheduling_forms=('10-Q',))

    def test_old_contracts_closed_and_v5_only_adds_exact_amended_forms(self):
        for version in (2,3,4):
            schema = sec_financials_output_schema(version=version)
            self.assertEqual(schema['properties']['filings']['items']['properties']['form']['enum'], ['10-Q','10-K'])
        schema = sec_financials_output_schema(version=5)
        self.assertEqual(schema['properties']['filings']['items']['properties']['form']['enum'], ['10-Q','10-K','10-Q/A','10-K/A'])
        from dalton_core.authority_resolver import _schema_matches
        self.failure()
        with patch('dalton_core.sec_financials_cli.parse_live', return_value=self.captured()):
            result = run(self.args(self.newpath))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['observation']['filings'][0]['form'], '10-Q/A')
        bad = copy.deepcopy(result['observation']); bad['filings'][0]['form']='6-K'
        with self.assertRaises(Exception): _schema_matches(bad, schema, 'output')

    def test_exact_cached_agent_filing_replays_through_real_cli_and_records(self):
        oldsummary = self.failure(); self.mission()
        before = [dict(r) for r in self.missions.connection.execute('SELECT * FROM coverage_mission_statement_dispatches')]
        oldout = self.out
        ticket_bytes = (oldout / 'ticket.json').read_bytes(); summary_bytes = (oldout / 'summary.json').read_bytes()
        proof, raw = original(self.state, self.ref)
        self.assertEqual(proof['issuer_cik'], '0001467373')
        self.assertNotEqual(raw['filings'][0]['accession'][:10], proof['issuer_cik'])
        d = prepare(self.missions, self.launcher, company_ref=COMPANY, form='10-Q')
        self.assertEqual(d['status_marker'], 'fresh')
        path = request_path(self.state, d['dispatch_id'])
        self.out = self.root / 'local-output'; self.out.mkdir()
        with patch('dalton_core.sec_financials_cli.parse_live') as network:
            result = run(self.args(self.newpath, replay=path))
        network.assert_not_called()
        self.assertEqual(result['status'], 'succeeded', result['failure_reason'])
        self.assertEqual(result['artifact']['content_hash'], oldsummary['artifact']['content_hash'])
        self.missions.mark_statement_dispatch_launched(d['dispatch_id'], 'sec-financials-run:'+'b'*24)
        recorded = self.missions.record_statement_observation(dispatch_id=d['dispatch_id'],
            observation=result['observation'], governance_ref=self.new['id'], governance_hash=self.new['content_hash'],
            governance_proof=self.new)
        self.missions.settle_statement_dispatch(d['dispatch_id'], outcome='succeeded')
        self.assertEqual(self.missions.statement_filings(COMPANY)[0]['form'], '10-Q/A')
        after = [dict(r) for r in self.missions.connection.execute('SELECT * FROM coverage_mission_statement_dispatches LIMIT 8')]
        self.assertEqual(before, after)
        self.assertEqual((oldout / 'ticket.json').read_bytes(), ticket_bytes)
        self.assertEqual((oldout / 'summary.json').read_bytes(), summary_bytes)
        # Original summary is distinct from new local child output in production.
        self.assertEqual(hashlib.sha256(summary_bytes).hexdigest(), proof['summary_sha256'])

    def test_tampered_missing_unknown_or_foreign_original_never_fetches(self):
        self.failure(); self.mission()
        oldpath = self.out / 'ticket.json'; frozen = oldpath.read_bytes()
        for status in ('running','orphaned','unknown','succeeded'):
            bad={**self.ticket,'status':status}; oldpath.write_text(json.dumps(bad))
            with self.assertRaises(ValueError): prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        oldpath.write_bytes(frozen)
        d=prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        path=request_path(self.state,d['dispatch_id'])
        saved=json.loads(path.read_bytes()); saved['authorization']['ticker']='OTHER'
        saved['content_hash']=content_hash({k:v for k,v in saved.items() if k!='content_hash'})
        path.write_text(json.dumps(saved))
        with patch('dalton_core.sec_financials_cli.parse_live') as network:
            result=run(self.args(self.newpath,replay=path))
        network.assert_not_called(); self.assertEqual(result['status'],'failed')


    def test_foreign_issuer_current_token_refuses_before_queue_or_child(self):
        raw=self.captured(); raw['cik']='1352010'; raw['entity_name']='Different Synthetic Issuer'
        with patch.object(self, 'captured', return_value=raw): self.failure()
        self.mission()
        before=self.missions.connection.execute('SELECT count(*) FROM coverage_mission_statement_dispatches').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'company authorization'):
            prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        self.assertEqual(self.missions.connection.execute('SELECT count(*) FROM coverage_mission_statement_dispatches').fetchone()[0],before)

    def test_one_fixed_local_child_even_after_terminal_failure(self):
        self.failure(); self.mission()
        d=prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        path=request_path(self.state,d['dispatch_id'])
        class Process:
            pid=-1
            def poll(self): return None
        with patch('dalton_core.lane_child_launcher.subprocess.Popen', return_value=Process()) as spawn:
            first=self.launcher.start_reparse(replay_request_path=path,missions=self.missions,actor_ref=d['actor_ref'])
            second=self.launcher.start_reparse(replay_request_path=path,missions=self.missions,actor_ref=d['actor_ref'])
            self.assertEqual(first['id'],second['id']); self.assertEqual(spawn.call_count,1)
            self.assertIn('--replay-request', first['command']); self.assertNotIn('--allow-network',first['command'])
        self.launcher._children.clear(); self.launcher._current=None
        p=self.launcher._ticket_path(first['id']); saved=json.loads(p.read_bytes()); saved['status']='failed'; p.write_text(json.dumps(saved))
        with patch('dalton_core.lane_child_launcher.subprocess.Popen') as spawn:
            result=self.launcher.start_reparse(replay_request_path=path,missions=self.missions,actor_ref=d['actor_ref'])
        spawn.assert_not_called(); self.assertEqual(result['status'],'failed')

    def test_missing_companion_remains_identifiable_local_not_network(self):
        from dalton_core.sec_statement_cached_reparse import is_reparse_dispatch
        self.failure(); self.mission()
        d=prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        request_path(self.state,d['dispatch_id']).unlink()
        row=self.missions._statement_dispatch(d['dispatch_id'])
        self.assertTrue(is_reparse_dispatch(self.missions,row))
        with patch('dalton_core.lane_child_launcher.subprocess.Popen') as spawn:
            with self.assertRaises((ValueError,Exception)):
                self.launcher.start_reparse(replay_request_path=request_path(self.state,d['dispatch_id']),missions=self.missions,actor_ref=d['actor_ref'])
        spawn.assert_not_called()

    def test_latest_amended_disclosure_preserves_identity_and_refuses_ambiguous_unit(self):
        from dalton_core.company_model_series import _latest_by_period
        row={'period_start':'2026-04-01','period_end':'2026-06-30','value':'100','unit':'USD',
             'filed':'2026-08-01','accession':'0001234567-26-000001','filing_form':'10-Q'}
        amendment={**row,'value':'110','filed':'2026-08-08','accession':'0001234567-26-000002','filing_form':'10-Q/A'}
        best, ambiguous=_latest_by_period([amendment,row])
        value=next(iter(best.values())); self.assertEqual(str(value['value']),'110'); self.assertEqual(value['form'],'10-Q/A')
        self.assertEqual(ambiguous,[])
        best, ambiguous=_latest_by_period([amendment,{**amendment,'unit':'EUR'}])
        self.assertTrue(ambiguous)

class MigrationTests(unittest.TestCase):
    setUp = AmendmentTests.setUp
    # Only migration cases: shared fixture methods without rerunning the above.
    def workspace(self, sources=None):
        source={'id':'source:synthetic-sec','connector_ref':'connector:sec-edgar',
            'capability_id':'capability:dalton:connector:sec-edgar','auth_mode':'none',
            'allowed_hosts':['data.sec.gov','www.sec.gov'],'allowed_operations':['list_filings','get_company_facts'],
            'credential_slot_refs':[], 'transport':{'config_path':None,'endpoint_ref':'transport:public-http:0.1',
                'kind':'connector','socket_path':None}}
        body={'schema_version':'dalton-shared-connection-catalog-0.1','models':[],
              'sources':[source] if sources is None else sources}
        catalog={**body,'content_hash':content_hash(body)}
        self.catalogpath=self.root / 'catalog.json'; self.catalogpath.write_text(json.dumps(catalog))
        root=self.root/'workspace'; root.mkdir(exist_ok=True)
        (root/'display.json').write_text(json.dumps({'shared_connection_catalog':{
            'path':str(self.catalogpath),'content_hash':catalog['content_hash'],'model_count':0,'source_count':len(body['sources'])}}))
        return SimpleNamespace(workspace_root=root,state_dir=self.state,lifecycle='active',workspace_id='ws:synthetic',
            content_hash='a'*64, shared_readonly_paths=(self.catalogpath,))

    def test_migration_exact_approval_permissions_idempotent_and_honest_actor(self):
        self.newpath.unlink(); w=self.workspace(); prior=self.oldpath.read_bytes()
        result=preview(w); self.assertEqual(result['status'],'eligible'); self.assertFalse(self.newpath.exists())
        record=result['record']; self.assertEqual(record['approved_by'],ACTOR)
        self.assertEqual(record['allowed_permissions'],self.old['allowed_permissions'])
        result=apply(w); self.assertEqual(result['status'],'applied')
        self.assertEqual(apply(w)['status'],'existing'); self.assertEqual(self.oldpath.read_bytes(),prior)
        self.assertEqual(self.newpath.stat().st_mode & 0o777,0o600)
        self.assertEqual(ConnectorGovernance.load(self.newpath).wire,result['record'])

    def test_disabled_expired_or_unapproved_source_is_not_migrated(self):
        self.newpath.unlink(); self.assertEqual(preview(self.workspace(sources=[]))['status'],'skipped')
        w=self.workspace(); expired={**self.old,'effective_until':'2026-01-02T00:00:00+00:00'}
        expired['content_hash']=content_hash({k:v for k,v in expired.items() if k!='content_hash'})
        self.oldpath.write_text(json.dumps(expired)); self.assertEqual(preview(w)['status'],'skipped')

    def test_source_permissions_actor_and_existing_binding_tampering_refused(self):
        self.newpath.unlink(); w=self.workspace(); record=preview(w)['record']
        for mutate in ('actor','source','permissions'):
            bad=copy.deepcopy(record)
            if mutate=='actor': bad['approved_by']='automation:other'
            elif mutate=='source': bad['compatibility_migration']['source_binding']['source']['transport']['endpoint_ref']='other'
            else: bad['allowed_permissions']['filesystem_read'].append('unapproved-path')
            bad['content_hash']=content_hash({k:v for k,v in bad.items() if k!='content_hash'})
            with self.assertRaises(Exception): ConnectorGovernance(bad)
        apply(w)
        source=copy.deepcopy(record['compatibility_migration']['source_binding']['source']); source['id']='different-profile'
        with self.assertRaises(ValueError): preview(self.workspace(sources=[source]))

class ConsumerAndMigrationTests(unittest.TestCase):
    setUp=AmendmentTests.setUp
    captured=AmendmentTests.captured
    args=AmendmentTests.args
    failure=AmendmentTests.failure
    mission=AmendmentTests.mission

    def ingest(self, filings):
        d=self.missions.queue_statement_dispatch(authorization=self.missions.sec_lane_authorization_for_company(COMPANY),
            form='10-Q',filing_limit=8,retry_salt='consumer:'+content_hash(filings)[:24])
        self.missions.mark_statement_dispatch_launched(d['dispatch_id'],'sec-financials-run:'+'c'*24)
        self.missions.record_statement_observation(dispatch_id=d['dispatch_id'],observation={
            'schema_version':'0.1','cik':'0001467373','entity_name':'Synthetic Services Corporation',
            'filings':filings,'source_record_refs':['raw-sink:'+'c'*64],'next_cursor':None,'provider_status':200},
            governance_ref=self.new['id'],governance_hash=self.new['content_hash'],governance_proof=self.new)
        self.missions.settle_statement_dispatch(d['dispatch_id'],outcome='succeeded')

    def test_real_state_window_and_inputs_select_amendment_not_double_count(self):
        from tests.test_company_model_state import _line
        from tests.test_company_model_inputs import _spec
        from dalton_core.company_model_state import build_company_model_state
        from dalton_core.company_model_inputs import build_model_inputs
        self.failure(); self.mission()
        dates=[('2024-07-01','2024-09-30'),('2024-10-01','2024-12-31'),
            ('2025-01-01','2025-03-31'),('2025-04-01','2025-06-30'),
            ('2025-07-01','2025-09-30'),('2025-10-01','2025-12-31'),
            ('2026-01-01','2026-03-31'),('2026-04-01','2026-06-30')]
        filings=[]
        for n,(start,end) in enumerate(dates):
            filings.append({'accession':f'0001234567-26-{n:06d}','form':'10-Q','report_date':end,'filed':end,
                'lines':[_line('us-gaap:Revenues',period_start=start,period_end=end,value=str(100+n))]})
        self.ingest(filings)
        amended=copy.deepcopy(filings[-1]); amended.update(accession='0001234567-26-999999',form='10-Q/A',filed='2026-08-08')
        amended['lines'][0]['value']='220'; self.ingest([amended])
        state=build_company_model_state(self.missions,COMPANY,ticker='ACN')
        self.assertEqual(len(state['filings']),8)
        self.assertIn(filings[0]['accession'],[f['accession'] for f in state['filings']])
        self.assertNotIn(filings[-1]['accession'],[f['accession'] for f in state['filings']])
        table=build_model_inputs(self.missions,_spec())
        line=next(r for r in table['filed_lines'] if r['concept']=='us-gaap:Revenues')
        self.assertEqual(line['cells']['2026-06-30']['value'],'220')
        self.assertEqual(line['cells']['2026-06-30']['source_accessions'],[amended['accession']])
        self.assertEqual(line['cells']['2026-03-31']['value'],'106')
        self.assertEqual(len(self.missions.statement_filings(COMPANY)),9)
        self.assertEqual(self.missions.statement_coverage(COMPANY)['held_by_form']['10-Q'],8)

    def legacy(self):
        from tests.test_mission_statement_lane import _observation
        from dalton_core.sec_statement_form_schema import TABLE
        self.failure(); self.mission(); self.ingest(_observation()['filings'])
        c=self.missions.connection
        ddl=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(TABLE,)).fetchone()[0]
        triggers=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(TABLE,)).fetchall()
        columns=','.join(r[1] for r in c.execute(f'PRAGMA table_info({TABLE})'))
        c.execute('PRAGMA foreign_keys=OFF'); c.execute('PRAGMA legacy_alter_table=ON'); c.execute('BEGIN IMMEDIATE')
        c.execute(CAPTURED_LEGACY_FILINGS_DDL.replace(TABLE,TABLE+'_old_fixture',1))
        c.execute(f'INSERT INTO {TABLE}_old_fixture(rowid,{columns}) SELECT rowid,{columns} FROM {TABLE}')
        c.execute(f'DROP TABLE {TABLE}'); c.execute(f'ALTER TABLE {TABLE}_old_fixture RENAME TO {TABLE}')
        for sql, in triggers:c.execute(sql)
        c.commit(); c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA legacy_alter_table=OFF')
        return c

    def test_exact_legacy_schema_migration_preserves_rows_rowids_hashes_and_fk(self):
        from dalton_core.sec_statement_form_schema import migrate,TABLE
        c=self.legacy(); before=c.execute(f'SELECT rowid,* FROM {TABLE}').fetchall()
        lines=c.execute('SELECT rowid,* FROM coverage_mission_statement_lines').fetchall()
        migrate(c); migrate(c)
        self.assertEqual([tuple(r) for r in before],[tuple(r) for r in c.execute(f'SELECT rowid,* FROM {TABLE}')])
        self.assertEqual([tuple(r) for r in lines],[tuple(r) for r in c.execute('SELECT rowid,* FROM coverage_mission_statement_lines')])
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(c.execute('PRAGMA legacy_alter_table').fetchone()[0],0)
        self.assertEqual(c.execute('PRAGMA foreign_key_check(coverage_mission_statement_lines)').fetchall(),[])
        with self.assertRaises(Exception):c.execute(f'UPDATE {TABLE} SET content_hash=?',('0'*64,))
        c.rollback()

    def test_real_old_ddl_and_repeated_authority_open_keep_exact_four_forms(self):
        from dalton_core.sec_statement_form_schema import TABLE
        c = self.legacy()
        before = [tuple(r) for r in c.execute(f'SELECT rowid,* FROM {TABLE}')]
        self.assertIn("CHECK(form IN ('10-Q','10-K'))", c.execute(
            'SELECT sql FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()[0])
        CoverageMissionAuthority(self.store)
        first = c.execute('SELECT sql FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()[0]
        self.assertIn("CHECK(form IN ('10-Q','10-K','10-Q/A','10-K/A'))", first)
        self.assertEqual(first.count("'10-Q/A'"), 1)
        self.assertEqual(first.count("'10-K/A'"), 1)
        CoverageMissionAuthority(self.store)
        self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',
                                  (TABLE,)).fetchone()[0], first)
        self.assertEqual([tuple(r) for r in c.execute(f'SELECT rowid,* FROM {TABLE}')], before)

    def test_mid_migration_failure_rolls_back_and_unknown_shape_refuses(self):
        from dalton_core.sec_statement_form_schema import migrate,TABLE
        c=self.legacy(); before=[tuple(r) for r in c.execute(f'SELECT rowid,* FROM {TABLE}')]
        ddl=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(TABLE,)).fetchone()[0]
        class Proxy:
            def __getattr__(self,name):return getattr(c,name)
            def execute(self,sql,*args):
                if sql==f'DROP TABLE {TABLE}':raise OSError('forced migration interruption')
                return c.execute(sql,*args)
        with self.assertRaises(OSError):migrate(Proxy())
        self.assertEqual(c.execute('SELECT sql FROM sqlite_master WHERE name=?',(TABLE,)).fetchone()[0],ddl)
        self.assertEqual([tuple(r) for r in c.execute(f'SELECT rowid,* FROM {TABLE}')],before)
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(TABLE+'_amended_contract',)).fetchone())
        c.execute(f'CREATE INDEX unknown_owner_index ON {TABLE}(filed)')
        with self.assertRaisesRegex(RuntimeError,'unknown trigger/index'):migrate(c)

    def test_missing_companion_after_many_later_failures_still_cannot_be_http(self):
        from dalton_core.sec_statement_cached_reparse import is_reparse_dispatch
        self.failure(); self.mission(); d=prepare(self.missions,self.launcher,company_ref=COMPANY,form='10-Q')
        for n in range(9):
            other=self.missions.queue_statement_dispatch(authorization=self.missions.sec_lane_authorization_for_company(COMPANY),
                form='10-Q',retry_salt=f'later-failure-{n}')
            self.missions.mark_statement_dispatch_launched(other['dispatch_id'],'sec-financials-run:'+'d'*24)
            self.missions.settle_statement_dispatch(other['dispatch_id'],outcome='failed',failure_reason='unrelated later failure')
        request_path(self.state,d['dispatch_id']).unlink()
        self.assertTrue(is_reparse_dispatch(self.missions,self.missions._statement_dispatch(d['dispatch_id'])))
        with patch('dalton_core.lane_child_launcher.subprocess.Popen') as provider:
            with self.assertRaises(Exception):self.launcher.start_reparse(replay_request_path=request_path(self.state,d['dispatch_id']),missions=self.missions,actor_ref=d['actor_ref'])
        provider.assert_not_called()

    def test_material_only_unit_source_alias_exact_and_financial_changes_invalidate(self):
        from dalton_core import sec_filing_units,statement_context
        source=Path(sec_filing_units.__file__).read_bytes(); sha=hashlib.sha256(source).hexdigest()
        self.assertIn(sha,statement_context._UNIT_MATERIAL_ONLY_SOURCE_ALIAS)
        original_source=source.replace(b"filing['form'] in {'10-K', '10-K/A'} else 'sec_10q'",b"filing['form']=='10-K' else 'sec_10q'")
        self.assertEqual(hashlib.sha256(original_source).hexdigest(),statement_context._UNIT_MATERIAL_ONLY_SOURCE_ALIAS[sha])
        mutated=source.replace(b'RULE_REF=',b'RULE_REF =',1)
        self.assertNotEqual(source,mutated)
        self.assertNotIn(hashlib.sha256(mutated).hexdigest(),statement_context._UNIT_MATERIAL_ONLY_SOURCE_ALIAS)


    def test_amended_only_yoy_lane_waits_without_faking_regular_or_queue(self):
        from dalton_core.mission_sec_quarters import MissionSecQuartersCoordinator
        from tests.test_mission_sec_quarters import _entry
        from tests.test_company_model_state import _line
        self.failure(); self.mission()
        self.ingest([{'accession':'0001234567-26-999999','form':'10-Q/A','filed':'2026-08-08',
            'report_date':'2026-06-30','lines':[_line('us-gaap:Revenues')]}])
        coordinator=MissionSecQuartersCoordinator(store=self.store,missions=self.missions,state_dir=self.state,
            checklist=lambda:[_entry(0)],governance_check=lambda:None)
        before=self.missions.connection.execute('SELECT count(*) FROM coverage_mission_sec_dispatches').fetchone()[0]
        result=coordinator.dispatch_once()
        self.assertEqual(result.get('queued',[]),[])
        self.assertTrue(any('同比契约尚不支持' in str(r.get('reason')) for r in result['skipped']))
        self.assertEqual(self.missions.connection.execute('SELECT count(*) FROM coverage_mission_sec_dispatches').fetchone()[0],before)
        old={'end':'2026-06-30','form':'10-Q','filed':'2026-07-01','accession':'0001234567-26-000001',
             'start':'2026-04-01','fourth_quarter':None}
        amended={**old,'form':'10-Q/A','filed':'2026-08-08','accession':'0001234567-26-999999','start':None}
        self.assertEqual(coordinator._recent_quarters([old,amended])[0]['form'],'10-Q/A')
