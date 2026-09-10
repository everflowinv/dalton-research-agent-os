"""P10x: reading order, multi-company attribution, batch bounds, broker provenance.

The live shape these were written against, on 2026-09-09: 454 acquired
documents, 447 of them already read to a terminal review state, an extraction
child reporting ``nothing_to_draft`` on 58 of 115 runs, and Accenture holding
five sell-side Claims against Cognizant's 149 -- not because the lane is slow
but because a five-vendor broker note is filed under whichever query returned
it, and because the industry, which has a checklist of its own, has never been
the subject of a single Claim.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dalton_core.document_provenance import (
    TIER_FILING,
    TIER_INDUSTRY,
    TIER_MANAGEMENT,
    TIER_NEWS,
    TIER_SELL_SIDE,
    broker_key,
    covered_subjects,
    evidence_value,
    independent_brokers,
    parse_search_metadata,
    provenance_record,
    subjects_for_statement,
    tier_for_spec,
)
from dalton_core.extraction_backlog import (
    DocumentProvenanceStore,
    ExtractionBacklogError,
    apply_schema,
    extraction_backlog,
    observed_yield,
)
from dalton_core.extraction_priority import (
    BEST_AGED_VALUE,
    MAX_WINDOWS_PER_TICK,
    STARVE_AFTER_DAYS,
    review_sort_key,
    safe_windows_per_tick,
    thinness_ranks,
    window_reservation_micros,
)

# The exact shape of one live AlphaEngine search response, trimmed to two
# results.  The first is the note that made the case: Wells Fargo, found by a
# Cognizant query, naming four covered vendors in its own company list.
LIVE_SEARCH = json.dumps({
    "jsonrpc": "2.0", "id": "dalton-868e1c9ac5c2fae2c3fa46c3",
    "result": {"content": [{"type": "text", "text": json.dumps({
        "query": "Cognizant CTSH research report",
        "results": [
            {
                "doc_id": "320000610220657",
                "title": "Payments, Processors, and IT Services: Wells Weekly Payments Pulse",
                "publish_time": "2026-08-23 08:00:00",
                "sources": ["Wells Fargo Securities, LLC"],
                "companies": [
                    "Paypal Holdings, Inc.", "Infosys Ltd",
                    "Cognizant Technology Solutions Corporation",
                    "EPAM Systems, Inc.", "Accenture PLC",
                ],
            },
            {
                "doc_id": "320000610154569",
                "title": "Cognizant: raising estimates",
                "publish_time": "2026-08-20 08:00:00",
                "sources": ["Wells Fargo Securities LLC"],
                "companies": ["Cognizant Technology Solutions Corporation"],
            },
        ],
    })}]},
}).encode("utf-8")

UNIVERSE = {
    "company:sec-cik:0001467373": "ACN",
    "company:sec-cik:0001058290": "CTSH",
    "company:sec-cik:0001352010": "EPAM",
    "company:sec-cik:0000051143": "IBM",
    "company:sec-cik:001688568": "DXC",
}
INDUSTRY = "industry:us-it-services"


class SearchMetadataTests(unittest.TestCase):
    def test_the_wire_named_the_broker_and_every_company(self):
        parsed = parse_search_metadata(LIVE_SEARCH)
        note = parsed["alphaengine-doc:320000610220657"]
        self.assertEqual(note["broker"], "Wells Fargo Securities, LLC")
        self.assertIn("Accenture PLC", note["named_companies"])
        self.assertIn("EPAM Systems, Inc.", note["named_companies"])

    def test_one_house_spelled_two_ways_is_one_source(self):
        parsed = parse_search_metadata(LIVE_SEARCH)
        first = parsed["alphaengine-doc:320000610220657"]["broker_key"]
        second = parsed["alphaengine-doc:320000610154569"]["broker_key"]
        self.assertEqual(first, second)
        self.assertEqual(first, "wells fargo")
        self.assertEqual(independent_brokers(list(parsed.values())),
                         ["Wells Fargo Securities, LLC"])

    def test_unreadable_bytes_yield_nothing_rather_than_raising(self):
        for raw in (b"\xff\xfe not json", b"{}", b'{"result": {}}', None, 7):
            self.assertEqual(parse_search_metadata(raw), {})

    def test_a_five_vendor_note_names_three_covered_companies(self):
        parsed = parse_search_metadata(LIVE_SEARCH)
        found = covered_subjects(
            parsed["alphaengine-doc:320000610220657"]["named_companies"], UNIVERSE)
        self.assertEqual(sorted(found), sorted([
            "company:sec-cik:0001467373", "company:sec-cik:0001058290",
            "company:sec-cik:0001352010",
        ]))

    def test_a_document_the_search_never_described_still_gets_its_tier(self):
        record = provenance_record(
            document_ref="alphaengine-doc:1", source_ref="source:alphaengine",
            spec_ref="sell-side-reports", metadata=None)
        self.assertEqual(record["provenance_tier"], TIER_SELL_SIDE)
        self.assertIsNone(record["broker"])
        self.assertFalse(record["metadata_seen"])
        self.assertEqual(record["named_companies"], [])
        # S2: coverage is the mission's answer, so it is never in the hash.
        self.assertNotIn("covered_subjects", record)


class TierLadderTests(unittest.TestCase):
    def test_the_owner_ladder_is_the_sort_order(self):
        ladder = [tier_for_spec(spec) for spec in (
            "annual-report-10k", "earnings-call-transcripts",
            "sell-side-reports", "sales-notes", "industry-demand",
            "management-changes")]
        self.assertEqual(ladder, [TIER_FILING, TIER_MANAGEMENT, TIER_SELL_SIDE,
                                  "sales_note", TIER_INDUSTRY, TIER_NEWS])
        self.assertEqual([evidence_value(t) for t in ladder],
                         sorted(evidence_value(t) for t in ladder))

    def test_an_unknown_kind_sorts_last_rather_than_being_promoted(self):
        self.assertEqual(tier_for_spec("something-new"), "other")
        self.assertGreater(evidence_value("other"), evidence_value(TIER_NEWS))

    def test_a_market_report_outranks_a_news_page(self):
        # S4: an industry report is where an industry Claim comes from, and
        # folding it into ``news`` both mis-ranked it and hid it.
        self.assertLess(evidence_value(TIER_INDUSTRY), evidence_value(TIER_NEWS))
        self.assertGreater(evidence_value(TIER_INDUSTRY), evidence_value(TIER_SELL_SIDE))

    def test_broker_key_drops_legal_dressing_but_not_identity(self):
        self.assertEqual(broker_key("TD Cowen"), "td cowen")
        self.assertEqual(broker_key("Deutsche Bank AG"), "deutsche bank")
        self.assertNotEqual(broker_key("TD Cowen"), broker_key("Wolfe Research"))


class ReadingOrderTests(unittest.TestCase):
    def setUp(self):
        self.specs = {
            "doc:acn-news": "management-changes",
            "doc:dxc-call": "earnings-call-transcripts",
            "doc:dxc-broker": "sell-side-reports",
            "doc:acn-broker": "sell-side-reports",
            "doc:ibm-10k": "annual-report-10k",
        }
        self.rank = {"ACN": 0, "DXC": 4}

    def _key(self, review_id, company, document, thin=None):
        return review_sort_key(
            {"review_id": review_id, "company_ref": company, "document_ref": document,
             "created_at": "2026-09-01T00:00:00+00:00"},
            spec_by_document=self.specs, company_rank=self.rank, thinness_rank=thin)

    def test_a_last_place_company_transcript_reads_before_a_first_place_news_page(self):
        # The old key sorted by company first, so ACN's news page beat DXC's
        # earnings call.  That is the acquisition rule, not the reading rule.
        call = self._key("r1", "DXC", "doc:dxc-call")
        news = self._key("r2", "ACN", "doc:acn-news")
        self.assertLess(call, news)

    def test_a_filing_reads_before_a_transcript_before_a_broker_note(self):
        order = sorted([
            self._key("r-broker", "DXC", "doc:dxc-broker"),
            self._key("r-10k", "ACN", "doc:ibm-10k"),
            self._key("r-call", "DXC", "doc:dxc-call"),
        ])
        self.assertEqual([key[0] for key in order],
                         [evidence_value(TIER_FILING), evidence_value(TIER_MANAGEMENT),
                          evidence_value(TIER_SELL_SIDE)])

    def test_between_two_broker_notes_the_thinner_company_reads_first(self):
        thin = thinness_ranks(
            {"ACN": {TIER_SELL_SIDE: 5}, "DXC": {TIER_SELL_SIDE: 149}},
            companies=["ACN", "DXC"])
        acn = self._key("r-acn", "ACN", "doc:acn-broker", thin)
        dxc = self._key("r-dxc", "DXC", "doc:dxc-broker", thin)
        self.assertLess(acn, dxc)
        # Company priority still breaks the tie when nothing is thinner.
        even = thinness_ranks({"ACN": {TIER_SELL_SIDE: 5}, "DXC": {TIER_SELL_SIDE: 5}},
                              companies=["ACN", "DXC"])
        self.assertLess(self._key("r-acn", "ACN", "doc:acn-broker", even)[2],
                        self._key("r-dxc", "DXC", "doc:dxc-broker", even)[2])

    def test_a_company_holding_nothing_of_a_tier_reads_first(self):
        thin = thinness_ranks({"ACN": {}, "DXC": {TIER_SELL_SIDE: 3}},
                              companies=["ACN", "DXC"])
        self.assertEqual(thin[("ACN", TIER_SELL_SIDE)], 0)
        self.assertEqual(thin[("DXC", TIER_SELL_SIDE)], 1)

    def test_the_key_is_defined_without_a_thinness_map(self):
        self.assertEqual(self._key("r", "ACN", "doc:acn-broker")[1], 0)


class AgingTests(unittest.TestCase):
    """S4: strict priority alone reads the bottom of the ladder never."""

    SPECS = {"doc:news": "management-changes", "doc:market": "industry-demand",
             "doc:call": "earnings-call-transcripts", "doc:10k": "annual-report-10k"}
    NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)

    def _key(self, document, age_days, *, now=None):
        created = (self.NOW - timedelta(days=age_days)).isoformat()
        return review_sort_key(
            {"review_id": f"r:{document}", "company_ref": "ACN",
             "document_ref": document, "created_at": created},
            spec_by_document=self.SPECS, company_rank={"ACN": 0}, now=now)

    def test_without_a_clock_the_order_is_pure_priority(self):
        self.assertEqual(self._key("doc:news", 400)[0], evidence_value(TIER_NEWS))

    def test_a_news_page_climbs_one_rung_per_waiting_period(self):
        fresh = self._key("doc:news", 0, now=self.NOW)[0]
        one = self._key("doc:news", STARVE_AFTER_DAYS, now=self.NOW)[0]
        two = self._key("doc:news", 2 * STARVE_AFTER_DAYS, now=self.NOW)[0]
        self.assertEqual(fresh, evidence_value(TIER_NEWS))
        self.assertEqual(one, fresh - 1)
        self.assertEqual(two, fresh - 2)

    def test_it_climbs_rather_than_jumps_and_never_passes_a_filing(self):
        ancient = self._key("doc:news", 3650, now=self.NOW)
        filing = self._key("doc:10k", 0, now=self.NOW)
        self.assertEqual(ancient[0], BEST_AGED_VALUE)
        self.assertLess(filing, ancient)
        self.assertEqual(filing[0], evidence_value(TIER_FILING))

    def test_a_starving_market_report_eventually_outruns_a_fresh_transcript(self):
        # The industry subject's Claims come from these, so starving them
        # starves the industry subject.
        market = self._key("doc:market", 6 * STARVE_AFTER_DAYS, now=self.NOW)
        call = self._key("doc:call", 0, now=self.NOW)
        self.assertLess(market, call)

    def test_an_unreadable_or_future_timestamp_ages_nothing(self):
        for created in ("", "not a date", (self.NOW + timedelta(days=9)).isoformat()):
            key = review_sort_key(
                {"review_id": "r", "company_ref": "ACN", "document_ref": "doc:news",
                 "created_at": created},
                spec_by_document=self.SPECS, company_rank={"ACN": 0}, now=self.NOW)
            self.assertEqual(key[0], evidence_value(TIER_NEWS), created)


class BatchBoundTests(unittest.TestCase):
    # The rate card the routing policy pins live: profile:deepseek-v4-flash.
    RATE = {"input_per_million_usd": 0.14, "output_per_million_usd": 0.28}
    # The WorkOrder bound document_extraction.build_work has always carried.
    WORK = {"max_input_tokens": 16000, "max_output_tokens": 3000, "max_cost_usd": 0.05}

    def test_a_window_can_cost_three_tenths_of_a_cent_not_five_cents(self):
        # 16000 * 0.14/1e6 + 3000 * 0.28/1e6 = 0.00308
        self.assertEqual(window_reservation_micros(self.RATE, self.WORK), 3080)
        self.assertLess(window_reservation_micros(self.RATE, self.WORK),
                        int(Decimal("0.05") * 1_000_000))

    def test_the_reservation_is_rounded_up_never_short(self):
        micros = window_reservation_micros(
            {"input_per_million_usd": 0.14, "output_per_million_usd": 0.28},
            {"max_input_tokens": 1, "max_output_tokens": 0})
        self.assertEqual(micros, 1)

    def test_an_unpriced_profile_is_refused_rather_than_treated_as_free(self):
        for card in ({}, {"input_per_million_usd": "free", "output_per_million_usd": 1},
                     {"input_per_million_usd": -1, "output_per_million_usd": 1}):
            with self.assertRaises(ValueError):
                window_reservation_micros(card, self.WORK)

    def test_the_bound_is_derived_from_the_day_caps_not_typed(self):
        # The live caps: $100/day, 9,000 paid calls, three passes per window.
        bound = safe_windows_per_tick(
            day_cap_micros=100_000_000, max_daily_paid_calls=9000,
            rate_card=self.RATE, work_budget=self.WORK, passes_per_window=3)
        self.assertEqual(bound["reservation_micros"], 3080)
        # 9000 * 0.5 / 3 / 288 = 5 windows; cost allows far more, so calls bind.
        self.assertEqual(bound["bound_by"], "paid_calls")
        self.assertEqual(bound["windows_by_paid_calls"], 5)
        self.assertEqual(bound["windows_per_tick"], 5)

    def test_the_prose_pass_alone_gets_a_much_larger_bound(self):
        bound = safe_windows_per_tick(
            day_cap_micros=100_000_000, max_daily_paid_calls=9000,
            rate_card=self.RATE, work_budget=self.WORK, passes_per_window=1)
        self.assertEqual(bound["windows_by_paid_calls"], 15)
        self.assertEqual(bound["windows_per_tick"], 15)

    def test_the_flat_five_cent_reservation_would_have_bound_the_day_cost(self):
        flat = safe_windows_per_tick(
            day_cap_micros=100_000_000, max_daily_paid_calls=9000,
            rate_card=self.RATE, work_budget=self.WORK, passes_per_window=1,
            reservation_micros=50_000)
        self.assertEqual(flat["bound_by"], "day_cost")
        self.assertEqual(flat["windows_by_day_cost"], 3)

    def test_the_derived_bound_is_clamped_into_the_launcher_range(self):
        big = safe_windows_per_tick(
            day_cap_micros=10_000_000_000, max_daily_paid_calls=10_000_000,
            rate_card=self.RATE, work_budget=self.WORK)
        self.assertEqual(big["windows_per_tick"], MAX_WINDOWS_PER_TICK)
        self.assertTrue(big["clamped"])
        tiny = safe_windows_per_tick(
            day_cap_micros=1, max_daily_paid_calls=0,
            rate_card=self.RATE, work_budget=self.WORK)
        self.assertEqual(tiny["windows_per_tick"], 1)

    def test_the_arithmetic_is_refused_rather_than_guessed(self):
        for kwargs in ({"ticks_per_day": 0}, {"lane_share": 0}, {"lane_share": 2},
                       {"passes_per_window": 0}, {"day_cap_micros": -1},
                       {"max_daily_paid_calls": -1}):
            with self.assertRaises(ValueError):
                safe_windows_per_tick(**{
                    "day_cap_micros": 100_000_000, "max_daily_paid_calls": 9000,
                    "rate_card": self.RATE, "work_budget": self.WORK, **kwargs})


class ReservationTests(unittest.TestCase):
    """B1: two cost paths reach one reservation, and the smaller one is a leak.

    Live, 41 extraction windows were charged on the route-estimate calculator
    rather than metered tokens: mean 4,471 micros, max 5,496, against a
    rate-card derivation of 3,080.  A reservation below the charge trips the
    overrun alert, skips ``settle()``, and leaves the hold open against the day
    cap for good.
    """

    RATE = {"input_per_million_usd": 0.14, "output_per_million_usd": 0.28}

    class _Work:
        budget = {"max_input_tokens": 16000, "max_output_tokens": 3000, "max_cost_usd": 0.05}

    def _profile(self, ref="model-profile-version:extraction:1"):
        return {"profile_version_ref": ref, "cost": dict(self.RATE)}

    def _route(self, estimate, ref="model-profile-version:extraction:1"):
        return {"candidate_snapshot": [
            {"profile_version_ref": ref, "eligible": True, "estimated_cost_usd": estimate}]}

    def test_the_route_estimate_wins_when_it_exceeds_the_rate_card(self):
        from dalton_core.document_extraction import RESERVATION_HEADROOM, reservation_micros

        # The live maximum on calculator:model-route-estimate:0.1.
        held = reservation_micros(self._Work, self._route("0.005496"), self._profile())
        self.assertEqual(held, 5496 * RESERVATION_HEADROOM)
        self.assertGreater(held, 5496)
        self.assertGreater(held, window_reservation_micros(self.RATE, self._Work.budget))

    def test_the_live_mean_estimate_is_covered_with_headroom(self):
        from dalton_core.document_extraction import reservation_micros

        held = reservation_micros(self._Work, self._route("0.004471"), self._profile())
        self.assertGreaterEqual(held, 4471 * 2)
        self.assertLessEqual(held, int(Decimal("0.05") * 1_000_000))

    def test_the_rate_card_is_the_floor_when_the_route_carries_no_estimate(self):
        from dalton_core.document_extraction import RESERVATION_HEADROOM, reservation_micros

        for route in ({}, {"candidate_snapshot": []},
                      {"candidate_snapshot": [{"profile_version_ref": "other",
                                               "eligible": True, "estimated_cost_usd": "9"}]},
                      {"candidate_snapshot": [{"profile_version_ref": "model-profile-version:extraction:1",
                                               "eligible": False, "estimated_cost_usd": "9"}]}):
            with self.subTest(route=route):
                self.assertEqual(reservation_micros(self._Work, route, self._profile()),
                                 3080 * RESERVATION_HEADROOM)

    def test_a_tiny_estimate_never_lowers_the_rate_card_floor(self):
        from dalton_core.document_extraction import RESERVATION_HEADROOM, reservation_micros

        self.assertEqual(reservation_micros(self._Work, self._route("0.000001"), self._profile()),
                         3080 * RESERVATION_HEADROOM)

    def test_the_contract_ceiling_is_still_the_roof(self):
        from dalton_core.document_extraction import reservation_micros

        ceiling = int(Decimal("0.05") * 1_000_000)
        self.assertEqual(reservation_micros(self._Work, self._route("1.00"), self._profile()), ceiling)

    def test_an_unpriced_profile_reserves_the_ceiling_rather_than_a_penny(self):
        from dalton_core.document_extraction import reservation_micros

        ceiling = int(Decimal("0.05") * 1_000_000)
        self.assertEqual(reservation_micros(self._Work, {}, {"profile_version_ref": "p", "cost": {}}),
                         ceiling)


class StatementSubjectTests(unittest.TestCase):
    EXTRA = {"company:sec-cik:0001467373": "ACN", "company:sec-cik:0001352010": "EPAM"}

    def _subjects(self, statement, **kwargs):
        return subjects_for_statement(
            statement, company_ref="company:sec-cik:0001058290",
            company_names_key="CTSH", industry_ref=INDUSTRY,
            extra_subjects=self.EXTRA, **kwargs)

    def test_a_statement_wholly_about_a_competitor_also_becomes_that_company_s(self):
        plan = self._subjects(
            "Wells Fargo said Accenture's bookings momentum is the strongest in the group.")
        self.assertEqual(plan["subjects"],
                         ["company:sec-cik:0001058290", "company:sec-cik:0001467373"])
        self.assertEqual(plan["basis"], "statement_names_other_subject")

    def test_a_comparison_naming_both_stays_with_the_review_company_alone(self):
        # B2, owner's decision: the sentence was drafted with Cognizant named
        # as the subject, and "prefers Cognizant to Accenture" is a statement
        # about how the analyst ranks them.  Minting it as an Accenture Claim
        # would put a sentence in Accenture's file that was never written
        # about Accenture.
        plan = self._subjects(
            "The analyst prefers Cognizant to Accenture on valuation grounds.")
        self.assertEqual(plan["subjects"], ["company:sec-cik:0001058290"])
        self.assertEqual(plan["basis"], "statement_names_review_company")

    def test_the_review_company_is_never_dropped(self):
        # B3: whatever else a statement is about, the window vouched for this
        # company, so removing it would be a silent retraction.
        for statement, industry in (
            ("Accenture's bookings momentum is strongest.", False),
            ("The analyst prefers Cognizant to Accenture.", False),
            ("Management expects margin expansion next year.", False),
            ("Buyers are shifting IT services spending.", True),
            ("", False),
        ):
            with self.subTest(statement=statement):
                plan = self._subjects(statement, industry_document=industry)
                self.assertIn("company:sec-cik:0001058290", plan["subjects"])

    def test_a_statement_naming_nobody_still_belongs_to_the_review_s_company(self):
        # This is what keeps the change additive: every Claim minted today is
        # still minted, against the same subject, under the same key.
        plan = self._subjects("Management expects margin expansion next year.")
        self.assertEqual(plan["subjects"], ["company:sec-cik:0001058290"])
        self.assertEqual(plan["basis"], "review_company")

    def test_an_industry_document_s_market_fact_also_becomes_an_industry_claim(self):
        plan = self._subjects(
            "Buyers are shifting IT services spending toward outcome-based contracts.",
            industry_document=True)
        self.assertEqual(plan["subjects"], ["company:sec-cik:0001058290", INDUSTRY])
        self.assertEqual(plan["basis"], "statement_names_industry")

    def test_the_same_sentence_in_a_transcript_stays_with_the_company(self):
        plan = self._subjects(
            "Buyers are shifting IT services spending toward outcome-based contracts.",
            industry_document=False)
        self.assertEqual(plan["subjects"], ["company:sec-cik:0001058290"])

    def test_an_industry_document_naming_the_company_is_still_the_company_s(self):
        plan = self._subjects(
            "Cognizant is winning share as IT services demand recovers.",
            industry_document=True)
        self.assertEqual(plan["subjects"], ["company:sec-cik:0001058290"])

    def test_the_old_claim_set_is_a_subset_of_the_new_one(self):
        # B3 re-baselined: replay a fixture queue of statements, mint the
        # subject each would have got before this change (always the review
        # company) and the subjects it gets now, and assert nothing was lost.
        queue = [
            "Accenture's bookings momentum is strongest in the group.",
            "The analyst prefers Cognizant to Accenture on valuation grounds.",
            "Management expects margin expansion next year.",
            "Buyers are shifting IT services spending toward outcome contracts.",
            "EPAM's Ukraine exposure remains the key debate.",
            "Cognizant is winning share as IT services demand recovers.",
        ]
        for industry in (False, True):
            before = {(index, "company:sec-cik:0001058290") for index in range(len(queue))}
            after = {(index, subject)
                     for index, statement in enumerate(queue)
                     for subject in self._subjects(statement, industry_document=industry)["subjects"]}
            self.assertTrue(before <= after, sorted(before - after))


def _ledger() -> sqlite3.Connection:
    """The three mission tables the backlog reads, and nothing else."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE coverage_mission_pointer(mission_ref TEXT, mission_version_id TEXT);"
        "CREATE TABLE coverage_mission_source_discoveries(record_id TEXT, spec_ref TEXT);"
        "CREATE TABLE coverage_mission_discovered_documents("
        " record_id TEXT, company_ref TEXT, source_ref TEXT, document_ref TEXT,"
        " discovery_ref TEXT, mission_version_ref TEXT, status TEXT);"
        "CREATE TABLE coverage_mission_document_reviews("
        " review_id TEXT, mission_version_ref TEXT, company_ref TEXT,"
        " discovered_document_ref TEXT, document_ref TEXT, state TEXT, rationale TEXT);"
    )
    connection.execute("INSERT INTO coverage_mission_pointer VALUES('m','v13')")
    apply_schema(connection)
    return connection


def _document(connection, *, doc, spec, company, status="acquired", version="v13"):
    discovery = f"disc:{doc}:{version}"
    connection.execute("INSERT INTO coverage_mission_source_discoveries VALUES(?,?)",
                       (discovery, spec))
    connection.execute(
        "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?,?,?)",
        (f"rec:{doc}:{version}", company, "source:alphaengine", doc, discovery, version, status))
    return f"rec:{doc}:{version}"


def _review(connection, *, record, doc, company, state, rationale="", version="v13"):
    connection.execute(
        "INSERT INTO coverage_mission_document_reviews VALUES(?,?,?,?,?,?,?)",
        (f"rev:{doc}:{version}", version, company, record, doc, state, rationale))


class ProvenanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = _ledger()
        self.store = DocumentProvenanceStore(self.connection)

    def test_the_broker_and_the_covered_companies_are_persisted(self):
        outcomes = self.store.record_search(
            raw=LIVE_SEARCH, source_ref="source:alphaengine",
            spec_by_document={"alphaengine-doc:320000610220657": "sell-side-reports",
                              "alphaengine-doc:320000610154569": "sell-side-reports"})
        self.assertEqual([o["status"] for o in outcomes], ["fresh", "fresh"])
        record = self.store.get("alphaengine-doc:320000610220657")
        self.assertEqual(record["broker"], "Wells Fargo Securities, LLC")
        self.assertEqual(record["sources"], "Wells Fargo Securities, LLC")
        self.assertEqual(record["provenance_tier"], TIER_SELL_SIDE)
        self.assertIn("company:sec-cik:0001467373",
                      self.store.subjects_for("alphaengine-doc:320000610220657", UNIVERSE))

    def test_coverage_is_derived_at_read_time_not_frozen_into_the_hash(self):
        # S2: a company admitted to the universe after the note was recorded is
        # still recognised, and the stored row -- which is append-only -- did
        # not have to change for that to be true.
        self.store.record_search(raw=LIVE_SEARCH, source_ref="source:alphaengine",
                                 spec_by_document={})
        doc = "alphaengine-doc:320000610220657"
        narrow = self.store.subjects_for(doc, {"company:sec-cik:0001058290": "CTSH"})
        wide = self.store.subjects_for(doc, UNIVERSE)
        self.assertEqual(narrow, ["company:sec-cik:0001058290"])
        self.assertIn("company:sec-cik:0001467373", wide)
        # Recording it again after coverage widened is still a duplicate.
        again = self.store.record_search(raw=LIVE_SEARCH, source_ref="source:alphaengine",
                                         spec_by_document={})
        self.assertEqual([o["status"] for o in again], ["duplicate", "duplicate"])

    def test_a_caller_that_tries_to_store_coverage_is_refused(self):
        base = provenance_record(document_ref="alphaengine-doc:1",
                                 source_ref="source:alphaengine",
                                 spec_ref="sell-side-reports", metadata={})
        with self.assertRaises(ExtractionBacklogError):
            self.store.record({**base, "covered_subjects": ["company:sec-cik:0001467373"]})

    def test_the_debate_map_reads_these_columns_under_the_names_it_looks_for(self):
        from dalton_core.debate_map_draft import _ATTRIBUTION_COLUMNS
        from dalton_core.extraction_backlog import document_attribution_rows

        self.store.record_search(raw=LIVE_SEARCH, source_ref="source:alphaengine",
                                 spec_by_document={})
        rows = document_attribution_rows(self.connection)
        entry = rows["alphaengine-doc:320000610220657"]
        self.assertEqual(entry["publisher"], "Wells Fargo Securities, LLC")
        self.assertTrue(set(entry) <= set(_ATTRIBUTION_COLUMNS))
        # The column names themselves are ones the ladder already looks for.
        stored = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(document_provenance_records)")}
        for field in ("publisher", "authors", "sources", "title"):
            self.assertTrue(set(_ATTRIBUTION_COLUMNS[field]) & stored, field)

    def test_recording_the_same_search_twice_writes_nothing_new(self):
        args = dict(raw=LIVE_SEARCH, source_ref="source:alphaengine", spec_by_document={})
        self.store.record_search(**args)
        again = self.store.record_search(**args)
        self.assertEqual([o["status"] for o in again], ["duplicate", "duplicate"])

    def test_a_second_reading_that_disagrees_is_a_conflict_not_an_overwrite(self):
        base = provenance_record(document_ref="alphaengine-doc:1",
                                 source_ref="source:alphaengine",
                                 spec_ref="sell-side-reports", metadata={"broker": "TD Cowen"})
        self.store.record(base)
        with self.assertRaises(ExtractionBacklogError):
            self.store.record({**base, "broker": "Wolfe Research"})

    def test_a_tier_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(ExtractionBacklogError):
            self.store.record({"document_ref": "d", "source_ref": "s",
                               "provenance_tier": "vibes"})

    def test_subjects_for_an_unrecorded_document_are_empty_not_guessed(self):
        self.assertEqual(self.store.subjects_for("alphaengine-doc:missing", UNIVERSE), [])


class BacklogProjectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = _ledger()
        self.acn = "company:sec-cik:0001467373"
        # Accenture's live sell-side shape: twelve documents, three acquired,
        # two of those read.  The DebateMap read this as 885 and was wrong by
        # two counting errors -- rows instead of documents, and the whole
        # mission instead of one company.
        read = _document(self.connection, doc="doc:acn-b1", spec="sell-side-reports", company=self.acn)
        _review(self.connection, record=read, doc="doc:acn-b1", company=self.acn,
                state="extraction_staged",
                rationale="ADR-0005 policy admission: 3 qualitative claim(s) admitted, 0 suggestion(s) refused")
        open_row = _document(self.connection, doc="doc:acn-b2", spec="sell-side-reports", company=self.acn)
        _review(self.connection, record=open_row, doc="doc:acn-b2", company=self.acn,
                state="awaiting_human_extraction")
        # Acquired long ago, its review left behind on mission version 9.
        _document(self.connection, doc="doc:acn-b3", spec="sell-side-reports",
                  company=self.acn, version="v9")
        _review(self.connection, record="rec:doc:acn-b3:v9", doc="doc:acn-b3",
                company=self.acn, state="awaiting_human_extraction", version="v9")
        # Found and never fetched: the AlphaEngine call cap, not this lane.
        for index in range(9):
            _document(self.connection, doc=f"doc:acn-d{index}", spec="sell-side-reports",
                      company=self.acn, status="discovered")

    def test_the_three_ways_a_document_waits_are_told_apart(self):
        backlog = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        tier = next(t for t in backlog["tiers"] if t["tier"] == TIER_SELL_SIDE)
        self.assertEqual(tier["already_read"], 1)
        self.assertEqual(tier["awaiting_extraction"], 1)
        self.assertEqual(tier["acquired_unqueued"], 1)
        self.assertEqual(tier["discovered"], 9)
        self.assertEqual(tier["queued_documents"], 11)

    def test_expected_claims_come_from_what_this_install_has_measured(self):
        backlog = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        tier = next(t for t in backlog["tiers"] if t["tier"] == TIER_SELL_SIDE)
        # One sell-side document was read and admitted three Claims.
        self.assertEqual(tier["yield_basis"], "observed")
        self.assertEqual(tier["expected_claims"], 33)

    def test_a_tier_nobody_has_read_says_so_instead_of_pretending(self):
        _document(self.connection, doc="doc:acn-call", spec="earnings-call-transcripts",
                  company=self.acn, status="discovered")
        backlog = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        tier = next(t for t in backlog["tiers"] if t["tier"] == TIER_MANAGEMENT)
        self.assertEqual(tier["yield_basis"], "default")

    def test_expected_cost_is_the_rate_card_worst_case_for_every_window(self):
        backlog = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        tier = next(t for t in backlog["tiers"] if t["tier"] == TIER_SELL_SIDE)
        self.assertEqual(tier["expected_windows"], 33)
        self.assertEqual(Decimal(tier["expected_cost_usd"]),
                         Decimal(33) * Decimal(3080) / Decimal(1_000_000))
        self.assertEqual(backlog["totals"]["queued_documents"], 11)

    def test_the_independence_ladder_counts_houses_not_documents(self):
        store = DocumentProvenanceStore(self.connection)
        for doc, broker in (("doc:acn-b1", "TD Cowen"), ("doc:acn-b2", "TD Cowen, LLC"),
                            ("doc:acn-b3", "Wolfe Research")):
            store.record(provenance_record(
                document_ref=doc, source_ref="source:alphaengine",
                spec_ref="sell-side-reports", metadata={
                    "broker": broker, "broker_key": broker_key(broker)}))
        backlog = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        self.assertEqual(backlog["documents_with_provenance"], 3)
        self.assertEqual(backlog["independent_brokers"], ["TD Cowen", "Wolfe Research"])

    def test_the_yield_counts_a_document_read_under_several_versions_once(self):
        _review(self.connection, record="rec:doc:acn-b1:v9", doc="doc:acn-b1",
                company=self.acn, state="extraction_staged", version="v9",
                rationale="ADR-0005 policy admission: 3 qualitative claim(s) admitted, 0 suggestion(s) refused")
        _document(self.connection, doc="doc:acn-b1", spec="sell-side-reports",
                  company=self.acn, version="v9")
        measured = observed_yield(self.connection)
        self.assertEqual(measured[TIER_SELL_SIDE]["documents_read"], 1)
        self.assertEqual(measured[TIER_SELL_SIDE]["claims_per_document"], Decimal("3.00"))

    def test_windows_per_document_says_whether_it_was_measured(self):
        # S3: only the dismissal sentence names a window count, so a tier
        # whose documents were all admitted has no window evidence and must
        # not wear the word "observed".
        measured = observed_yield(self.connection)
        self.assertEqual(measured[TIER_SELL_SIDE]["windows_basis"], "default")
        self.assertEqual(measured[TIER_SELL_SIDE]["windows_measured_on"], 0)
        record = _document(self.connection, doc="doc:acn-b9", spec="sell-side-reports",
                           company=self.acn)
        _review(self.connection, record=record, doc="doc:acn-b9", company=self.acn,
                state="dismissed",
                rationale="ADR-0005: mission automation found no admissible qualitative "
                          "statement in 6 window(s); 0 suggestion(s) refused")
        measured = observed_yield(self.connection)
        self.assertEqual(measured[TIER_SELL_SIDE]["windows_basis"], "observed")
        self.assertEqual(measured[TIER_SELL_SIDE]["windows_per_document"], 6)
        self.assertEqual(measured[TIER_SELL_SIDE]["windows_measured_on"], 1)

    def test_an_unpriced_backlog_reports_unknown_rather_than_free(self):
        backlog = extraction_backlog(self.connection, self.acn)
        self.assertEqual(backlog["totals"]["cost_basis"], "unknown")
        self.assertIsNone(backlog["totals"]["expected_cost_usd"])
        self.assertIsNone(backlog["totals"]["reservation_micros_per_window"])
        for tier in backlog["tiers"]:
            self.assertIsNone(tier["expected_cost_usd"])
        priced = extraction_backlog(self.connection, self.acn, reservation_micros=3080)
        self.assertEqual(priced["totals"]["cost_basis"], "rate_card_worst_case")
        self.assertIsNotNone(priced["totals"]["expected_cost_usd"])

    def test_a_subject_ref_is_required(self):
        with self.assertRaises(ExtractionBacklogError):
            extraction_backlog(self.connection, "")


if __name__ == "__main__":
    unittest.main()
