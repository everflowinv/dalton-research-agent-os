import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_native_statement_membership as fixtures
from tests import test_sec_native_filing_units as units
from tests import test_sec_filings_index_discovery as roots
from tests import test_mission_model_spec_lane as lane_fixtures
from dalton_core import statement_context as contexts
from dalton_core.sec_native_filing_units import prepare_model_unit_inputs
from dalton_core.native_model_context_refresh import scope, previous_refusal
from dalton_core.store import content_hash


class NativePendingRefreshTests(unittest.TestCase):
    setUp = fixtures.MembershipPipelineTests.setUp
    run_once = fixtures.MembershipPipelineTests.run_once
    adapter = fixtures.MembershipPipelineTests.adapter
    root = fixtures.MembershipPipelineTests.root
    profile = fixtures.MembershipPipelineTests.profile
    body = fixtures.MembershipPipelineTests.body
    admitted = fixtures.MembershipPipelineTests.admitted
    captured = fixtures.MembershipPipelineTests.captured
    captured_opaque = fixtures.MembershipPipelineTests.captured_opaque
    summary = fixtures.MembershipPipelineTests.summary
    stored = fixtures.MembershipPipelineTests.stored

    def old_context(self, missions, filing):
        with patch.object(contexts, '_native_unit_normalizer_hash', return_value='a'*64), patch(
                'dalton_core.native_statement_membership.supplement', side_effect=lambda c,s,f,p,rows:(rows,None)):
            return contexts.StatementContextAuthority(self.core).backfill(self.state, filing['ingest_id'], discover_companyfacts=False)

    def test_resolved_units_stale_context_appends_source_membership_and_old_proof_survives(self):
        m, f = self.stored(); old = self.old_context(m, f)
        self.assertTrue(all(x['unit']=='USD' for x in old['lines']))
        before = copy.deepcopy(contexts.base_statement_rows(m.connection, f['ingest_id']))
        refreshed = prepare_model_unit_inputs(m, self.state, f['company_ref'])
        self.assertTrue(refreshed['refreshed']); self.assertEqual(refreshed['unresolved'], [])
        latest = contexts.context_record(m.connection, contexts._latest(m.connection, f['ingest_id']))
        self.assertNotEqual(latest['version_id'], old['version_id'])
        self.assertEqual(latest['normalizer_hash'], contexts._native_unit_normalizer_hash())
        self.assertEqual(len(latest['lines']), len(old['lines'])+1)
        self.assertEqual(contexts.context_record(m.connection, old['version_id']), old)
        self.assertEqual(contexts.base_statement_rows(m.connection, f['ingest_id']), before)
        changes = m.connection.total_changes
        with patch.object(contexts.StatementContextAuthority, 'backfill', side_effect=AssertionError('same rule must not replay')):
            self.assertEqual(prepare_model_unit_inputs(m, self.state, f['company_ref']), {'refreshed':False,'unresolved':[]})
        self.assertEqual(m.connection.total_changes, changes)

    def test_two_companies_native_annual_current_rules_are_not_ticker_specific(self):
        for ref, cik in [('company:sec-cik:0001467373','0001467373'),('company:sec-cik:0001058290','0001058290')]:
            with self.subTest(company=ref), patch.object(roots, 'ACN', ref), patch.object(roots, 'CIK', cik):
                case = units.NativeUnitPipelineTests('test_native_annual_durable_source_context_read_and_model_units_without_quarters')
                case.setUp(); self.addCleanup(case.doCleanups)
                m, filing = case.stored(annual='20-F')
                with patch.object(contexts, '_native_unit_normalizer_hash', return_value='b'*64):
                    old = contexts.StatementContextAuthority(case.core).backfill(case.state, filing['ingest_id'], discover_companyfacts=False)
                result = prepare_model_unit_inputs(m, case.state, ref)
                self.assertTrue(result['refreshed']); self.assertEqual(result['unresolved'], [])
                self.assertEqual(contexts.context_record(m.connection, old['version_id']), old)
                self.assertEqual(prepare_model_unit_inputs(m, case.state, ref), {'refreshed':False,'unresolved':[]})

    def test_source_refusal_persists_then_same_source_skips_and_arrival_retries_without_replacing_old_available(self):
        m, f = self.stored(); old = self.old_context(m, f)
        from dalton_core.raw_spool import RawSpoolReader
        from dalton_core.acquired_sec_filings import _spool_bytes
        rawref = f['source_record_refs'][0]
        raw, problem = _spool_bytes(self.state, rawref[9:]); self.assertIsNone(problem)
        reader = RawSpoolReader(self.state/'connector-spool')
        path = reader._objects/rawref[9:11]/rawref[9:]
        path.unlink()
        with patch.object(contexts, 'prepare_context', wraps=contexts.prepare_context) as prepare:
            first = prepare_model_unit_inputs(m, self.state, f['company_ref'])
            self.assertFalse(first['context_refresh_refusals'][0]['reused'])
            self.assertEqual(contexts._latest(m.connection, f['ingest_id']), old['version_id'])
            second = prepare_model_unit_inputs(m, self.state, f['company_ref'])
            self.assertTrue(second['context_refresh_refusals'][0]['reused']); self.assertEqual(prepare.call_count, 1)
            path.write_bytes(raw)
            third = prepare_model_unit_inputs(m, self.state, f['company_ref'])
            self.assertNotIn('context_refresh_refusals', third); self.assertTrue(third['refreshed'])
            self.assertEqual(prepare.call_count, 2)
        self.assertNotEqual(contexts._latest(m.connection, f['ingest_id']), old['version_id'])
        self.assertEqual(contexts.context_record(m.connection, old['version_id']), old)

    def test_scope_binds_original_evidence_locator_hash_context_and_rule(self):
        m, f = self.stored(); self.old_context(m, f)
        original = scope(m.connection, self.state, f)
        self.assertTrue(original['native_evidence']['document_set']['originals'])
        with patch.object(contexts, '_native_unit_normalizer_hash', return_value='c'*64):
            self.assertNotEqual(scope(m.connection, self.state, f)['content_hash'], original['content_hash'])
        changed = dict(f); changed['content_hash']='d'*64
        self.assertNotEqual(scope(m.connection, self.state, changed)['content_hash'], original['content_hash'])
        contexts.StatementContextAuthority(self.core).backfill(self.state, f['ingest_id'], discover_companyfacts=False)
        self.assertNotEqual(scope(m.connection, self.state, f)['content_hash'], original['content_hash'])

    def test_stale_tampered_context_projection_cannot_trigger_authority_write(self):
        m, f = self.stored(); self.old_context(m, f)
        proxy = SimpleNamespace(statement_filings=m.statement_filings, statement_lines=m.statement_lines, store=m.store)
        class Connection:
            def execute(self, sql, args=()):
                result = m.connection.execute(sql, args)
                if sql.startswith('SELECT record_json,content_hash FROM statement_context_versions'):
                    row = dict(result.fetchone()); body=json.loads(row['record_json']);body['normalizer_hash']='e'*64
                    row['record_json']=json.dumps(body)
                    return SimpleNamespace(fetchone=lambda: row)
                return result
        proxy.connection = Connection()
        changes=m.connection.total_changes
        with self.assertRaisesRegex(ValueError,'projection hash'):
            prepare_model_unit_inputs(proxy, self.state, f['company_ref'])
        self.assertEqual(m.connection.total_changes, changes)

    def test_lane_refreshes_pending_company_before_hold_then_rechooses_once_without_paid_authority(self):
        m, f = self.stored(); old = self.old_context(m, f)
        from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator
        launcher = lane_fixtures.FakeLauncher(); launcher.state_dir=self.state
        mission={'id':'mission:test','universe':[{'company_ref':f['company_ref']}],'autonomy':{}}
        lane=MissionModelSpecLaneCoordinator(missions=m,launcher=launcher,mission=lambda:mission)
        seen=[]
        def choose(*args,**kwargs):
            ref=contexts._latest(m.connection,f['ingest_id']); seen.append(ref)
            return f['company_ref'], {'state_hash':content_hash(ref)}
        with patch('dalton_core.mission_model_spec_lane.choose_company',side_effect=choose), patch(
                'dalton_core.mission_model_spec_lane.current_permission',side_effect=ValueError('no current paid permission')):
            with self.assertRaisesRegex(ValueError,'no current paid permission'):
                lane.dispatch_once()
        self.assertEqual(len(seen),2);self.assertEqual(seen[0],old['version_id']);self.assertNotEqual(seen[1],seen[0])
        self.assertEqual(launcher.started,[])
        self.assertEqual(prepare_model_unit_inputs(m,self.state,f['company_ref']),{'refreshed':False,'unresolved':[]})

    def test_default_backfill_has_no_refresh_cache_metadata(self):
        m, f=self.stored()
        record=contexts.StatementContextAuthority(self.core).backfill(self.state,f['ingest_id'],discover_companyfacts=False)
        self.assertNotIn('model_refresh_scope',record)
        self.assertEqual(contexts.context_record(m.connection,record['version_id']),record)

    def test_unknown_native_source_bytes_cannot_inherit_exact_prefight_alias(self):
        import dalton_core.sec_native_filing_units as native
        before = contexts._native_unit_normalizer_hash()
        reader = Path.read_bytes
        def altered(path):
            value = reader(path)
            return value + b'\n# unknown numeric source modification\n' if path == Path(native.__file__) else value
        with patch.object(Path, 'read_bytes', altered):
            self.assertNotEqual(contexts._native_unit_normalizer_hash(), before)

    def test_refusal_cache_tampering_is_not_reused(self):
        m,f=self.stored();self.old_context(m,f)
        wanted=scope(m.connection,self.state,f)
        identity={'schema_version':'0.1','ingest_id':f['ingest_id'],'company_ref':f['company_ref'],
            'filing_content_hash':f['content_hash'],'status':'refused','reason':'source missing','model_refresh_scope':wanted}
        record={**identity,'version_id':'statement-context:'+content_hash(identity)[:32]}
        record['content_hash']=content_hash(record)
        row={'record_json':json.dumps(record),'content_hash':record['content_hash'],
            'version_id':record['version_id'],'ingest_id':f['ingest_id']}
        class Connection:
            def execute(self,sql,args=()):
                if sql.startswith('SELECT * FROM statement_context_versions'):
                    return SimpleNamespace(fetchall=lambda:[row])
                return m.connection.execute(sql,args)
        with self.assertRaisesRegex(ValueError,'refusal cache'):
            previous_refusal(Connection(),wanted)
