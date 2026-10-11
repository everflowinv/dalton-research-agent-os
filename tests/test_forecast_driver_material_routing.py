import copy
import unittest
from types import SimpleNamespace
from contextlib import closing

from dalton_core import material_acquisition as ma, material_requests as mr
from dalton_core.forecast_driver_retrieval import LEGACY_DATA_ITEM, data_item, routing_view, terms, request_terms
from dalton_core.forecast_material_gaps import gap_plans
from tests import test_material_acquisition as fixture


class RecipeTests(unittest.TestCase):
    def request(self):
        return {"need": {"kind": "data", "data_item": LEGACY_DATA_ITEM,
                         "doc_kinds": ["sec_10k", "sec_8k", "transcript"]},
                "acceptance": {"model_gap_rule_ref": "rule:forecast-material-gaps:1"},
                "requester": {"lane": "company_model_forecast", "section": "economic_driver_history"},
                "content_hash": "original", "budget": {"max_attempts": 6}, "period": "2026-06-30"}

    def test_segment_recipe_preserves_exact_driver_dimensions_and_avoids_bookings(self):
        for company, label, member in [("company:ticker:googl", "Google Cloud", "example:CloudMember"),
                                       ("company:synthetic", "Equipment", "example:EquipmentMember")]:
            identity = {"statement": "income", "dimensions": {"example:SegmentAxis": member}}
            spec = {"company_ref": company, "revenue_drivers": [{"ref": "division", "kind": "segment",
                    "label": label, "basis_concept": "example:Sales", "basis_identity": identity}]}
            plan = gap_plans(spec, {"periods": ["2026-06-30"]},
                             record={"execution_contract": {"drivers": {"status": "unexecuted"}}})[0]
            self.assertEqual(plan["need"]["data_item"], data_item(["segment"]))
            self.assertIn("sec_10q", plan["need"]["doc_kinds"])
            self.assertNotIn("bookings", ma.data_item_terms(plan["need"]["data_item"]))
            details = plan["acceptance"]["economic_driver_identities"]
            self.assertEqual(details[0]["basis_identity"], identity)
            self.assertEqual(details[0]["label"], label)
            self.assertEqual(request_terms(plan)[0], label)
            queries = ma.build_queries({**plan, "subject": company},
                {"name": "Synthetic Issuer", "ticker": "SYN"})
            self.assertIn(label, queries[0])
            self.assertIn("source admission must pass", plan["acceptance"]["model_input_admission"])

    def test_contract_volume_price_keep_their_own_meaning(self):
        self.assertIn("bookings", terms(data_item(["contract_book"])))
        self.assertNotIn("segment", terms(data_item(["contract_book"])))
        self.assertIn("units sold", terms(data_item(["volume", "price"])))
        self.assertIn("average selling price", terms(data_item(["volume", "price"])))
        self.assertNotIn("bookings", terms(data_item(["volume", "price"])))

    def test_legacy_view_is_bounded_and_does_not_rewrite_identity_or_limits(self):
        old = self.request(); before = copy.deepcopy(old); new = routing_view(old)
        self.assertEqual(old, before)
        self.assertEqual(new["content_hash"], old["content_hash"])
        self.assertEqual(new["budget"], old["budget"])
        self.assertEqual(new["period"], old["period"])
        self.assertIn("sec_10q", new["need"]["doc_kinds"])
        self.assertEqual(ma.data_item_terms(new["need"]["data_item"])[0], "segment")
        self.assertIn("bookings", ma.data_item_terms(new["need"]["data_item"]))
        self.assertEqual(routing_view(new), new)

    def test_unrelated_requests_and_unknown_recipe_are_not_migrated(self):
        for field, value in [("acceptance", {}), ("requester", {"lane": "ask", "section": "economic_driver_history"}),
                             ("need", {"kind": "data", "data_item": "bookings", "doc_kinds": ["transcript"]}),
                             ("need", {"kind": "data", "data_item": "economic_driver_history:unknown"})]:
            old = self.request(); old[field] = value
            self.assertEqual(routing_view(old), old)
        self.assertIsNone(terms("economic_driver_history:segment,segment"))
        self.assertIsNone(terms("economic_driver_history:unknown"))
        self.assertIn("bookings", ma.data_item_terms("new bookings"))


class HeldOriginalTests(fixture.Fixture):
    def test_old_immutable_request_reads_held_10q_before_any_costed_source(self):
        for company, name in [(fixture.SYNW, "Synthetic Widgets"), (fixture.OTHER, "Other Gadgets")]:
            with self.subTest(company=company):
                doc = self.corpus.add("sec_10q", name + " quarterly filing",
                    name + " segments revenue (in millions): Equipment 40; Services 20; "
                    "reconciling items 2; total revenues 62. All are for June 30, 2026.",
                    date="2026-07-20", company=company)
                request = mr.open_request(self.state, requester={"lane": "company_model_forecast",
                    "product_ref": "spec:held", "section": "economic_driver_history"}, subject=company,
                    need={"kind": "data", "data_item": LEGACY_DATA_ITEM,
                          "doc_kinds": ["sec_10k", "sec_8k", "transcript"]}, period="2026-06-30",
                    acceptance={"min_passages": 1, "model_gap_rule_ref": "rule:forecast-material-gaps:1"},
                    source_priority=["indexed_corpus", "sec_edgar"], now=fixture.NOW)
                original = copy.deepcopy(mr.request(self.state, request["id"]))
                paid = SimpleNamespace(costed=True, applies=lambda _: True,
                    estimate_calls=lambda _: self.fail("held original must precede paid acquisition"))
                result = ma.serve(self.state, request["id"], now=fixture.NOW,
                    rungs={"indexed_corpus": ma.IndexedCorpusRung(), "sec_edgar": paid})
                self.assertEqual(result["outcome"], "found")
                current = mr.request(self.state, request["id"])
                for key in ("id", "content_hash", "need", "period", "budget", "deadline"):
                    self.assertEqual(current[key], original[key])
                self.assertEqual(current["attempts"] if "attempts" in current else current["attempt_count"], 1)
                # Material availability creates no numerical Figure or forecast.
                with closing(__import__('sqlite3').connect(self.state / "core.sqlite")) as c:
                    self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='forecast_model_versions'").fetchone())
                self.assertEqual(result["rungs"][0]["source"], "indexed_corpus")
                again = ma.serve(self.state, request["id"], now=fixture.NOW,
                    rungs={"indexed_corpus": ma.IndexedCorpusRung(), "sec_edgar": paid})
                self.assertEqual(again["outcome"], "cached")
                self.assertEqual(mr.request(self.state, request["id"])["attempt_count"], 1)

    def test_period_and_issuer_constraints_still_prevent_false_match(self):
        self.corpus.add("sec_10q", "Other Gadgets", "Other Gadgets segment revenues 100 in June 2026.",
                        date="2026-07-20", company=fixture.OTHER)
        request = RecipeTests().request(); request["subject"] = fixture.SYNW
        request["period"] = "as_of:2024-01-01"
        verdict = ma.evaluate_acceptance(self.state, routing_view(request), now=fixture.NOW)
        self.assertFalse(verdict["met"])
        self.corpus.add("sec_10q", "Synthetic Widgets", "Synthetic Widgets segment revenues 100 in June 2026.",
                        date="2026-07-20", company=fixture.SYNW)
        verdict = ma.evaluate_acceptance(self.state, routing_view(request), now=fixture.NOW)
        self.assertFalse(verdict["met"])
        self.assertTrue(verdict["partial"])
