import copy
import unittest
from dalton_core.sec_interim_history_plan import build_history_plan
from dalton_core.sec_plan_specs import effective_sec_plan
from tests import test_sec_filings_index_discovery as fixture

class HistoryPlanTests(unittest.TestCase):
    def test_explicit_new_plan_keeps_company_scope_domestic_specs_and_counting_cap(self):
        old = fixture.sec_plan(); untouched = copy.deepcopy(old)
        new = build_history_plan(old, created_at='2026-10-13T15:00:00+00:00')
        self.assertEqual(old, untouched)
        self.assertEqual(new['companies'], old['companies'])
        self.assertEqual(new['budget'], old['budget'])
        self.assertEqual(new['mission_ref'], old['mission_ref'])
        self.assertEqual(new['specs'][:-1], old['specs'])
        actual = next(s for s in effective_sec_plan(new)['specs'] if s['spec_ref']=='foreign-disclosure-6k')
        prior = next(s for s in effective_sec_plan(old)['specs'] if s['spec_ref']=='foreign-disclosure-6k')
        self.assertEqual(actual, {**prior, 'lookback_days':1095})
        self.assertNotEqual(new['content_hash'], old['content_hash'])

    def test_unrelated_source_invalid_window_and_implicit_nonincrease_refused(self):
        for days in (90, 0, 3651, True):
            with self.assertRaises(ValueError): build_history_plan(fixture.sec_plan(), created_at='2026-10-13T15:00:00+00:00', lookback_days=days)

    def test_existing_explicit_foreign_spec_has_one_replacement_and_preserves_other_fields(self):
        old = effective_sec_plan(fixture.sec_plan())
        new = build_history_plan(old, created_at='2026-10-13T15:00:00+00:00')
        self.assertEqual(len(new['specs']), len(old['specs']))
        for before, after in zip(old['specs'], new['specs']):
            self.assertEqual(after, {**before,'lookback_days':1095} if before['spec_ref']=='foreign-disclosure-6k' else before)
