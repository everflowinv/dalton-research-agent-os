"""Exact input bytes, strong context refusals, and per-build read lifetime."""
import unittest
from unittest import mock
from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.store import canonical_json
from dalton_core.statement_context import StatementContextAuthority, StatementContextError
from tests import test_company_model_forecast as forecast_fixture
from tests import test_ar_segment_bridge_identity as context_fixture
from tests.test_company_model_inputs import _spec

class RequestSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture=forecast_fixture.LaneStateTests('test_the_first_run_publishes_a_model_and_its_forecast_lines')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.missions=self.fixture.missions
        self.spec=self.missions.latest_company_model_spec(forecast_fixture.ACN)

    def test_exact_table_one_read_per_filing_and_transaction_unchanged(self):
        with mock.patch('dalton_core.company_model_inputs._input_statement_reader', side_effect=lambda x:x):
            old=build_model_inputs(self.missions,self.spec)
        before=self.fixture.store.connection.in_transaction
        with mock.patch.object(self.missions,'statement_lines',wraps=self.missions.statement_lines) as reads:
            new=build_model_inputs(self.missions,self.spec)
        self.assertEqual(canonical_json(old),canonical_json(new))
        self.assertEqual(reads.call_count,len(self.missions.statement_filings(forecast_fixture.ACN)))
        self.assertEqual(self.fixture.store.connection.in_transaction,before)

    def test_existing_transaction_remains_owned_by_caller(self):
        c=self.fixture.store.connection
        c.execute('BEGIN')
        try:
            build_model_inputs(self.missions,self.spec)
            self.assertTrue(c.in_transaction)
        finally:c.rollback()

    def test_next_build_sees_new_quarter_and_reads_again(self):
        old=build_model_inputs(self.missions,self.spec)
        self.fixture.file_quarters([('2026-06-01','2026-08-31')],
            {forecast_fixture.REVENUE_CONCEPT:['2000000000'],forecast_fixture.COST_CONCEPT:['1000000000']})
        with mock.patch.object(self.missions,'statement_lines',wraps=self.missions.statement_lines) as reads:
            new=build_model_inputs(self.missions,self.spec)
        self.assertIn('2026-08-31',new['periods'])
        self.assertNotEqual(canonical_json(old),canonical_json(new))
        self.assertEqual(reads.call_count,len(self.missions.statement_filings(forecast_fixture.ACN)))

class StrongContextTests(unittest.TestCase):
    def setUp(self):
        f=context_fixture.ContextBackfillTests('test_context_overlay_is_exact_append_only_and_preserves_old_proof_resolution')
        f.setUp();self.addCleanup(f.doCleanups);f.raw_parse()
        legacy=context_fixture.line();legacy.pop('dimensions')
        filing,_=f.ingest([legacy]);self.fixture=f
        self.contexts=StatementContextAuthority(f.store)
        self.context=self.contexts.backfill(f.state_dir,filing['ingest_id'])
        self.spec=_spec(drivers=[{'ref':'operating','label':'Operating','kind':'mix',
            'basis_concept':'us-gaap:OperatingIncomeLoss','unit':'USD','because':'Synthetic'}],expenses=[])
        self.spec['company_ref']=context_fixture.COMPANY

    def test_context_strongly_verified_once_and_table_exact(self):
        with mock.patch('dalton_core.company_model_inputs._input_statement_reader', side_effect=lambda x:x):
            old=build_model_inputs(self.fixture.authority,self.spec)
        import dalton_core.statement_context as c
        with mock.patch.object(c,'context_record',wraps=c.context_record) as reads:
            new=build_model_inputs(self.fixture.authority,self.spec)
        self.assertEqual(canonical_json(old),canonical_json(new));self.assertEqual(reads.call_count,1)

    def test_corrupt_context_still_refuses_next_call(self):
        build_model_inputs(self.fixture.authority,self.spec)
        c=self.fixture.store.connection
        c.execute('DROP TRIGGER statement_context_no_update')
        c.execute("UPDATE statement_context_versions SET record_json='{}'")
        with self.assertRaises(StatementContextError):build_model_inputs(self.fixture.authority,self.spec)

    def test_new_exact_context_is_not_cached_across_calls(self):
        import dalton_core.statement_context as c
        with mock.patch.object(c,'context_record',wraps=c.context_record) as reads:
            build_model_inputs(self.fixture.authority,self.spec)
            with mock.patch.object(c,'normalizer_hash',return_value='d'*64):
                newer=self.contexts.backfill(self.fixture.state_dir,self.context['ingest_id'])
            self.assertNotEqual(newer['version_id'],self.context['version_id'])
            build_model_inputs(self.fixture.authority,self.spec)
        self.assertEqual([call.args[1] for call in reads.call_args_list],
                         [self.context['version_id'],newer['version_id']])
