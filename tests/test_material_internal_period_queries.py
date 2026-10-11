"""Internal period identities must stay out of public search keywords."""
import copy
import unittest
from dalton_core import material_acquisition as ma

NOW = "2026-10-11T05:30:00+00:00"

class MaterialInternalPeriodQueryTests(unittest.TestCase):
    def test_event_namespaces_are_not_public_periods(self):
        for period in ("event:research-event:" + "9c01" * 8,
                       "event_document:doc:report-1",
                       "event_document:research-event:event-1"):
            with self.subTest(period=period):
                self.assertEqual(ma.period_window(period, now=NOW),
                                 {"date_from": None, "date_to": None, "label": None,
                                  "tokens": [], "quarter_end": None})

    def test_queries_omit_identity_but_request_identity_is_unchanged(self):
        for period in ("event:research-event:" + "9c01" * 8, "event_document:doc:report-1"):
            with self.subTest(period=period):
                request = {"id": "material-request:exact", "period": period,
                           "subject": "company:ticker:epam",
                           "need": {"kind": "data", "data_item": "10-Q cost of revenues"}}
                before = copy.deepcopy(request)
                queries = ma.build_queries(request, {"name": "EPAM Systems", "ticker": "EPAM"}, now=NOW)
                self.assertEqual(queries[0], "EPAM Systems 10-Q cost of revenues")
                self.assertTrue(all("event:" not in q and "event_document:" not in q for q in queries))
                self.assertEqual(request, before)

    def test_explicit_dates_and_fiscal_terms_still_constrain_queries(self):
        window = ma.period_window("as_of:2026-10-09", now=NOW)
        self.assertEqual(window["date_to"], "2026-10-09")
        self.assertEqual(window["date_from"], "2026-06-11")
        fiscal = ma.period_window("FY2026-Q4", now=NOW)
        self.assertEqual(fiscal["tokens"][0], "Q4 2026")
        request = {"subject": "company:ticker:epam", "period": "FY2026-Q4",
                   "need": {"kind": "data", "data_item": "cost of revenues"}}
        self.assertIn("Q4 2026", ma.build_queries(request, {"name": "EPAM Systems"}, now=NOW)[0])

    def test_other_text_and_empty_namespace_are_not_silently_stripped(self):
        for period in ("FY2025", "year ended December 31, 2025", "event:", "event_document:"):
            with self.subTest(period=period):
                self.assertEqual(ma.period_window(period, now=NOW)["label"], period)
