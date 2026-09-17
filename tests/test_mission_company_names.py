"""The per-mission company name table, and the refusal it replaced.

The bug this fixes had a misleading shape: a hyperscaler workspace's feed lanes
reported ``MSFT has no company names to match a subject line against`` every
tick, which reads as a fact about the research and was in fact a missing row in
``document_subject.COMPANY_NAMES`` -- a five-row dict the workspace had no way
to edit. So these tests are mostly about *where the answer comes from*, in
order, and about the legacy Core continuing to get the answer it always got.
"""

from __future__ import annotations

import unittest

from dalton_core.document_subject import (
    COMPANY_NAMES,
    document_names_subject,
    earnings_call_names_issuer,
    subject_label,
    subject_names,
)
from dalton_core.mission_company_names import (
    MissionCompanyNamesError,
    mission_name_table,
    names_from_plan,
    require_named,
    resolve_universe_names,
    unnamed_tickers,
)

HYPERSCALERS = [
    {"company_ref": "company:ticker:amzn", "ticker": "AMZN"},
    {"company_ref": "company:ticker:googl", "ticker": "GOOGL"},
    {"company_ref": "company:ticker:meta", "ticker": "META"},
    {"company_ref": "company:ticker:msft", "ticker": "MSFT"},
]
PLAN = {
    "companies": {
        "company:ticker:amzn": {"search_terms": "AMZN", "names": ["Amazon.com", "Amazon"]},
        "company:ticker:googl": {"search_terms": "GOOGL", "names": ["Alphabet", "Google"]},
        "company:ticker:meta": {"search_terms": "META", "names": ["Meta Platforms"]},
        "company:ticker:msft": {"search_terms": "MSFT", "names": ["Microsoft"]},
    },
}


class NameTableTests(unittest.TestCase):
    def test_the_four_hyperscalers_get_usable_subject_terms(self):
        table = mission_name_table(HYPERSCALERS, PLAN)
        self.assertEqual(unnamed_tickers(table), [])
        self.assertEqual(subject_names("MSFT", table), ("Microsoft", "MSFT"))
        self.assertEqual(subject_names("GOOGL", table), ("Alphabet", "Google", "GOOGL"))
        self.assertIn("Microsoft", subject_label("MSFT", table))
        named = document_names_subject(
            "Microsoft Azure capacity is tightening again", "MSFT", table)
        self.assertTrue(named["checked"])
        self.assertTrue(named["names_subject"])
        self.assertEqual(named["matched"], ["Microsoft"])

    def test_a_document_about_another_covered_company_is_not_attributed(self):
        table = mission_name_table(HYPERSCALERS, PLAN)
        self.assertFalse(document_names_subject(
            "Alphabet's cloud backlog", "MSFT", table)["names_subject"])

    def test_the_mission_member_s_own_name_outranks_the_plan(self):
        universe = [{"company_ref": "company:ticker:msft", "ticker": "MSFT",
                     "name": "Microsoft Corporation"}]
        table = mission_name_table(universe, PLAN)
        self.assertEqual(table["MSFT"], ("Microsoft Corporation", "MSFT"))

    def test_the_plan_outranks_the_resolver_and_the_packaged_dict(self):
        universe = [{"company_ref": "company:sec-cik:0001467373", "ticker": "ACN"}]
        plan = {"companies": {"company:sec-cik:0001467373": {
            "search_terms": "ACN", "names": ["Accenture plc"]}}}
        self.assertEqual(mission_name_table(universe, plan)["ACN"],
                         ("Accenture plc", "ACN"))
        # And with no plan, the legacy Core's packaged answer is unchanged.
        self.assertEqual(mission_name_table(universe)["ACN"], ("Accenture", "ACN"))

    def test_the_ticker_is_always_appended_but_never_counts_as_a_name(self):
        table = mission_name_table([{"company_ref": "c:zzz", "ticker": "ZZZZ"}])
        self.assertEqual(table["ZZZZ"], ("ZZZZ",))
        self.assertEqual(unnamed_tickers(table), ["ZZZZ"])
        with self.assertRaises(MissionCompanyNamesError) as caught:
            require_named(table, where="the feed plan")
        self.assertIn("ZZZZ", str(caught.exception))
        self.assertIn("the feed plan", str(caught.exception))

    def test_a_short_ticker_is_not_added_as_a_name_of_its_own(self):
        """Two letters hit inside ordinary prose; the existing floor stands."""

        table = mission_name_table([{"company_ref": "c:hp", "ticker": "HP"}])
        self.assertEqual(table["HP"], ())

    def test_names_from_a_plan_without_names_is_empty_not_wrong(self):
        legacy = {"companies": {"company:sec-cik:0001467373": {"search_terms": "ACN"}}}
        self.assertEqual(names_from_plan(legacy), {})
        self.assertEqual(names_from_plan(None), {})

    def test_the_resolver_fills_only_what_is_missing_and_never_raises(self):
        calls: list[str] = []

        def resolve(ticker: str) -> dict[str, str]:
            calls.append(ticker)
            if ticker == "GOOGL":
                raise RuntimeError("SEC is unreachable")
            return {"ticker": ticker, "cik": "1", "name": f"{ticker} Inc"}

        found = resolve_universe_names(HYPERSCALERS, resolve=resolve)
        self.assertEqual(sorted(calls), ["AMZN", "GOOGL", "META", "MSFT"])
        self.assertEqual(found["MSFT"], ("MSFT Inc",))
        self.assertNotIn("GOOGL", found)
        # A ticker the packaged dict already knows is never asked about.
        self.assertEqual(resolve_universe_names(
            [{"company_ref": "c:acn", "ticker": "ACN"}], resolve=resolve).get("ACN"),
            None)

    def test_no_resolver_is_not_an_error(self):
        self.assertEqual(resolve_universe_names(HYPERSCALERS), {})


class LegacyFallbackTests(unittest.TestCase):
    """The legacy Core passes no table and must get exactly its old answers."""

    def test_packaged_answers_are_unchanged_without_a_table(self):
        self.assertEqual(subject_names("ACN"), ("Accenture", "ACN"))
        self.assertEqual(subject_names("IBM"),
                         COMPANY_NAMES["IBM"])
        self.assertEqual(subject_names("MSFT"), ("MSFT",))
        self.assertIn("IT services", subject_names("industry:us-it-services"))

    def test_an_industry_may_be_renamed_by_the_mission_too(self):
        table = {"industry:us-hyperscalers": ("云计算基础设施", "hyperscaler")}
        self.assertEqual(subject_names("industry:us-hyperscalers", table),
                         ("云计算基础设施", "hyperscaler"))
        self.assertEqual(subject_names("industry:us-hyperscalers"), ())

    def test_issuer_ambiguity_is_judged_against_the_mission_s_own_issuers(self):
        """Two covered issuers in one title is ambiguous; one is not.

        Without a table this read the packaged five, so on a hyperscaler
        mission an "Amazon and Microsoft" title looked unambiguous -- the
        check silently stopped working for every company it did not know.
        """

        table = mission_name_table(HYPERSCALERS, PLAN)
        one = earnings_call_names_issuer(
            "Microsoft Q1 2026 Earnings Call Transcript", "MSFT", table)
        self.assertTrue(one["names_issuer"])
        both = earnings_call_names_issuer(
            "Microsoft and Alphabet Q1 2026 Earnings Call", "MSFT", table)
        self.assertFalse(both["names_issuer"])
        self.assertEqual(both["ambiguous_with"], ["Alphabet"])
        self.assertTrue(earnings_call_names_issuer(
            "Accenture Q1 2026 Earnings Call Transcript", "ACN")["names_issuer"])



class FeedLaneIntegrationTests(unittest.TestCase):
    """The lane that used to refuse: it now runs, on the mission's own names."""

    def setUp(self):
        from dalton_core.workspace_lane_parity import build_mission_feed_plan

        self.mission = {
            "mission_ref": "coverage-mission:ws-hyperscalers",
            "industry_ref": "industry:美国-hyperscaler-云计算与-ai-基础设施",
            "title": "美国 Hyperscaler 初次覆盖",
            "objective": "Understand AI infrastructure capital intensity.",
            "budget": {"max_alphaengine_calls_24h": 20, "max_daily_paid_calls": 100},
            "universe": HYPERSCALERS,
        }
        self.plan = build_mission_feed_plan(
            self.mission,
            company_names={"AMZN": ["Amazon.com"], "GOOGL": ["Alphabet"],
                           "META": ["Meta Platforms"], "MSFT": ["Microsoft"]})

    def test_the_generated_plan_carries_the_names_and_still_validates(self):
        from dalton_core.mission_feed_lane import validate_feed_discovery_plan

        validate_feed_discovery_plan(self.plan)
        self.assertEqual(self.plan["companies"]["company:ticker:msft"]["names"],
                         ["Microsoft", "MSFT"])

    def test_universe_terms_no_longer_refuses_the_hyperscalers(self):
        from dalton_core.mission_feed_lane import _universe_terms, plan_company_names

        table = plan_company_names(HYPERSCALERS, self.plan)
        self.assertEqual(
            [ticker for _ref, ticker in _universe_terms(HYPERSCALERS, table)],
            ["AMZN", "GOOGL", "META", "MSFT"])

    def test_universe_terms_still_refuses_a_company_nobody_named(self):
        from dalton_core.mission_feed_lane import FeedLaneRejected, _universe_terms

        with self.assertRaises(FeedLaneRejected) as caught:
            _universe_terms([{"company_ref": "c:zzz", "ticker": "ZZZZ"}])
        self.assertIn("feed discovery plan", str(caught.exception))

    def test_a_body_is_attributed_to_the_company_its_words_name(self):
        from dalton_core.mission_feed_lane import attribute_body, plan_company_names

        table = plan_company_names(HYPERSCALERS, self.plan)
        result = attribute_body(
            "Microsoft guided Azure growth down two points; Alphabet did not.",
            HYPERSCALERS, self.plan, names=table)
        self.assertEqual(result["outcome"], "company")
        self.assertEqual(result["company_refs"],
                         ["company:ticker:googl", "company:ticker:msft"])

    def test_a_body_naming_nobody_is_dropped_with_its_reason(self):
        from dalton_core.mission_feed_lane import attribute_body, plan_company_names

        table = plan_company_names(HYPERSCALERS, self.plan)
        result = attribute_body("European white goods shipments fell.",
                                HYPERSCALERS, self.plan, names=table)
        self.assertEqual(result["outcome"], "dropped")
        self.assertTrue(result["reason"])

    def test_the_legacy_plan_and_universe_still_work_untouched(self):
        from dalton_core.mission_feed_lane import attribute_notes

        universe = [{"company_ref": "company:sec-cik:0001467373", "ticker": "ACN"}]
        attributed = attribute_notes(
            [{"note_id": "n1", "subject": "Accenture bookings"}], universe)
        self.assertEqual(attributed["by_company"],
                         {"company:sec-cik:0001467373": ["n1"]})


if __name__ == "__main__":
    unittest.main()
