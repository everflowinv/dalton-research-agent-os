"""Selection and publication agree on admitted domestic periodic amendments."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.company_model_forecast import forecast_reporting_boundary, pending_action


class AmendedReportingBoundaryTests(unittest.TestCase):
    def boundary(self, company, filings):
        requests = []
        def read(ref):
            requests.append(ref)
            return filings
        with patch('dalton_core.forecast_assumption_sources.preliminary_quarter', return_value=None):
            result = forecast_reporting_boundary(SimpleNamespace(statement_filings=read),
                                                 None, company, table={})
        self.assertEqual(requests, [company])
        return result

    def test_two_companies_amendment_only_annual_is_annual_authority(self):
        for company in ('company:materials', 'company:services'):
            filings = [{'form':'10-K/A','report_date':'2025-12-31'},
                       {'form':'10-Q','report_date':'2026-03-31'}]
            before = copy.deepcopy(filings)
            self.assertEqual(self.boundary(company,filings),
                {'latest_actual_end':'2026-03-31','fiscal_year_end':'2025-12-31','preliminary':None})
            self.assertEqual(filings,before)

    def test_quarter_amendment_advances_actual_without_becoming_annual(self):
        self.assertEqual(self.boundary('company:other',[
            {'form':'10-K','report_date':'2025-12-31'},
            {'form':'10-Q/A','report_date':'2026-06-30'}]),
            {'latest_actual_end':'2026-06-30','fiscal_year_end':'2025-12-31','preliminary':None})
        self.assertIsNone(self.boundary('company:other',[
            {'form':'10-Q/A','report_date':'2026-06-30'}])['fiscal_year_end'])

    def test_original_forms_stay_exact_and_unrelated_forms_do_not_fake_periods(self):
        original = [{'form':'10-K','report_date':'2025-12-31'},
                    {'form':'10-Q','report_date':'2026-03-31'}]
        expected = {'latest_actual_end':'2026-03-31','fiscal_year_end':'2025-12-31','preliminary':None}
        self.assertEqual(self.boundary('company:other',original),expected)
        self.assertEqual(self.boundary('company:other',original+[
            {'form':'6-K','report_date':'2027-06-30'},
            {'form':'8-K','report_date':'2027-09-30'},
            {'form':'10-K/A','report_date':None}]),expected)

    def test_completed_amendment_model_is_not_a_permanent_rules_upgrade(self):
        from tests.test_company_model_forecast import LaneStateTests, ACN
        from dalton_core.company_model_inputs import build_model_inputs
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        case = LaneStateTests('runTest'); case.setUp(); self.addCleanup(case.doCleanups)
        published = case.child()
        model = ForecastModelAuthority(case.store).model(published['model_version_ref'])
        spec = case.missions.latest_company_model_spec(ACN)
        table = build_model_inputs(case.missions,spec)
        # Source shape uses an admitted amendment's own report date, exactly
        # as the publisher's 10-K/10-K-A annual SQL does. No source is renamed.
        end = model['forecast_policy']['fiscal_year_end']
        boundary = self.boundary(ACN,[{'form':'10-K/A','report_date':end}])
        before = copy.deepcopy(model)
        self.assertIsNone(pending_action(model,spec,table,reporting_boundary=boundary))
        self.assertEqual(model,before)
        without_amendment = {**boundary,'fiscal_year_end':None}
        self.assertEqual(pending_action(model,spec,table,reporting_boundary=without_amendment),'rules_upgrade')
