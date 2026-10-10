import unittest
from datetime import datetime, timezone
from dalton_core.store import DaltonStore
from dalton_core.research_lifecycle import catalyst_status, observation_points
from dalton_core.zero_base_review import ZeroBaseReviewAuthority, build_review_body, review_state, inputs_hash
from tests.test_zero_base_review import context, answer, MISSION
from tests.test_earnings_season import PreviewHarness, AUTOMATION, preview


class ReviewExpiryTests(unittest.TestCase):
    def test_deadline_within_current_month_is_stale_and_requeues_without_contract_change(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = ZeroBaseReviewAuthority(store, clock=lambda: "2026-07-12T09:00:00+00:00")
        ctx = context(as_of="2026-07-12", period_label="2026-07")
        body = build_review_body(ctx, {**answer(), "model": None})
        held = authority.record(body, actor_ref="automation:coverage-mission")
        state = review_state(store.connection, mission_ref=MISSION["mission_ref"],
                             company_refs=["company:ACN"], now=datetime(2026,7,21,tzinfo=timezone.utc))[0]
        self.assertTrue(state["due"])
        self.assertEqual(state["review_status"], "stale")
        self.assertEqual(state["period_label"], "2026-07")
        prior = {"ref": held["id"], "next_verification": answer()["next_verification"]}
        self.assertNotEqual(inputs_hash(ctx), inputs_hash(context(as_of="2026-07-21", prior_review=prior)))
        from dalton_core.product_consistency import evaluate_bindings
        item = next(r for r in evaluate_bindings(store.connection, "company:ACN", today="2026-07-21")
                    if r["kind"] == "zero_base_review")
        self.assertEqual(item["binding_status"], "stale_binding")

    def test_passed_confirmed_and_estimated_dates_do_not_claim_an_event_occurred(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        entry = {"entry_ref": "catalyst-entry:synthetic", "expected_date": "2026-06-01",
                 "confidence": "confirmed", "sources": [{"observed_at": "2026-05-15T10:00:00Z"}]}
        confirmed = catalyst_status(store.connection, "company:synthetic", entry, "2026-06-02")
        estimated = catalyst_status(store.connection, "company:synthetic", {**entry,"confidence":"estimated"}, "2026-06-02")
        self.assertEqual(confirmed["status"], "missed")
        self.assertEqual(estimated["status"], "unconfirmed")
        self.assertFalse(estimated["may_trigger_judgement"])
        self.assertEqual(confirmed["result_refs"], [])


class PreviewObservationTests(PreviewHarness):
    def test_source_bound_points_are_consumed_only_for_exact_current_occurrence(self):
        draft = {"status": "drafted", "model": {"invocation_ref": "invocation:1"},
                 **preview.validate_preview_output(self.answer(), self.context)}
        held = preview.publish_preview(self.deliverables, context=self.context, draft=draft,
                                       mission=self.mission, playbook=self.playbook, actor_ref=AUTOMATION)
        occurrence = self.context["occurrence"]
        points = observation_points(self.store.connection, occurrence["company_ref"], occurrence, "2026-09-09")
        self.assertEqual(points["observations_status"], "available")
        self.assertEqual(points["key_observations"][0]["preview_version_ref"], held["id"])
        self.assertEqual(points["key_observations"][0]["refs"], ["thesis-version:1"])
        moved = observation_points(self.store.connection, occurrence["company_ref"],
                                   {**occurrence,"expected_date":"2026-12-01"}, "2026-09-09")
        self.assertEqual(moved["observations_status"], "missing")
        passed = observation_points(self.store.connection, occurrence["company_ref"], occurrence, "2027-01-01")
        self.assertEqual(passed["observations_status"], "missing")

    def test_uncited_observation_is_refused(self):
        output = self.answer(what_to_watch=[{"question":"订单如何变化", "why":"验证收入", "refs":[]}])
        with self.assertRaises(ValueError):
            preview.validate_preview_output(output, self.context)
