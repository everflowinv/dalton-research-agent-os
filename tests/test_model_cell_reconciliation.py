import unittest
from dalton_core.model_cell_reconciliation import compare_cells, ModelCellReconciliationAuthority, read_existing
from dalton_core.store import DaltonStore


def model(created="2026-07-01T00:00:00Z"):
    return {"id":"forecast-model-version:synthetic:1","content_hash":"b"*64,"company_ref":"company:synthetic","created_at":created,
            "forecast_periods":[{"end":"2026-09-30"}],"financial_statement_structure":{"lines":[{"ref":"sales","concept":"synthetic:Sales","statement":"income"}]},
            "drivers":[{"ref":"driver:sales","statement_identity":{"statement":"income","dimensions":{"synthetic:BusinessAxis":"synthetic:AlphaMember"}}}],
            "assumptions":[{"ref":"assumption:sales","provenance":{"rule_ref":"rule:synthetic:capacity-times-price"}}],
            "results":[{"ref":"result:sales","role":"revenue","driver_ref":"driver:sales","unit":"usd","cells":[{
                "kind":"estimate","period":{"start":"2026-07-01","end":"2026-09-30"},"status":"computed","ref":"result:sales@2026-09-30","value":"100","assumption_refs":["assumption:sales"]}]}]}


def actual(value="103",member="synthetic:AlphaMember",available="2026-10-10"):
    return {"concept":"synthetic:Sales","statement":"income","dimensions":{"synthetic:BusinessAxis":member},
            "period_start":"2026-07-01","period_end":"2026-09-30","value":value,"unit":"usd","basis":"filed","available_on":available,
            "refs":[{"line_id":"synthetic-context-line:1"}],"source_hash":"a"*64}

EVENTS=[{"period_end":"2026-09-30","available_on":"2026-10-10","source_ref":"filing:synthetic","source_hash":"c"*64}]


class ReconciliationTests(unittest.TestCase):
    def test_every_cell_binds_identity_assumptions_method_and_threshold_without_editing_model(self):
        source=model();record=compare_cells(source,[actual()],EVENTS)[0]
        self.assertEqual(record['status'],'compared')
        self.assertEqual(record['deviation_percent'],'3.00')
        self.assertTrue(record['redraft_required'])
        self.assertEqual(record['method_refs'],['rule:synthetic:capacity-times-price'])
        self.assertEqual(source['results'][0]['cells'][0]['value'],'100')
        within=compare_cells(source,[actual('100.5')],EVENTS)[0]
        self.assertEqual(within['band'],'within_tolerance')

    def test_equal_amount_cannot_resolve_a_wrong_member_and_missing_is_not_a_zero(self):
        record=compare_cells(model(),[actual(member='synthetic:BetaMember')],EVENTS)[0]
        self.assertEqual(record['status'],'unavailable')
        self.assertEqual(record['reason'],'exact_period_identity_unit_actual_missing')
        self.assertIsNone(record['actual'])
        self.assertNotIn('deviation_percent',record)

    def test_late_restatement_cannot_make_a_post_actual_model_into_a_prior_forecast(self):
        record=compare_cells(model('2026-11-01T00:00:00Z'),[actual(available='2027-02-01')],EVENTS)[0]
        self.assertEqual(record['status'],'unavailable')
        self.assertEqual(record['reason'],'forecast_not_known_before_actual_publication')
        self.assertEqual(record['first_actual_available_on'],'2026-10-10')

    def test_read_only_missing_ledger_has_no_migration(self):
        store=DaltonStore(':memory:');self.addCleanup(store.close)
        self.assertEqual(read_existing(store.connection,'company:synthetic'),[])
        self.assertIsNone(store.connection.execute("SELECT name FROM sqlite_master WHERE name='forecast_model_cell_reconciliations'").fetchone())

    def test_append_only_authority_is_idempotent_and_does_not_change_forecast(self):
        from unittest.mock import patch
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        from tests.test_model_forecast_driver import model as forecast_body
        store=DaltonStore(':memory:');self.addCleanup(store.close)
        models=ForecastModelAuthority(store);held=models.publish(forecast_body())
        cell=held['results'][0]['cells'][0]
        a={**actual(member='synthetic:AlphaMember',available='2027-12-01'),'concept':'us-gaap:Revenues','dimensions':{},'unit':held['results'][0]['unit'],
           'period_start':cell['period']['start'],'period_end':cell['period']['end'],'value':cell['value']}
        event={**EVENTS[0],'period_end':cell['period']['end'],'available_on':'2027-12-01'}
        authority=ModelCellReconciliationAuthority(store)
        with patch('dalton_core.model_cell_reconciliation.collect_actuals',return_value=([a],[event])):
            records=authority.refresh(held['company_ref']);count=store.connection.execute('SELECT COUNT(*) FROM forecast_model_cell_reconciliations').fetchone()[0]
            authority.refresh(held['company_ref'])
        self.assertTrue(records)
        self.assertEqual(count,len(read_existing(store.connection,held['company_ref'])))
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM forecast_model_cell_reconciliations').fetchone()[0],count)
        self.assertEqual(models.model(held['id'])['content_hash'],held['content_hash'])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute('DELETE FROM forecast_model_cell_reconciliations')
