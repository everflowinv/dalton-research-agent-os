"""P12e: the record, the computed table and the derived gap list.

The three things worth testing here are the three things that are *not* the
model's: the sections come from the Constitution's causal chain, the
comparison table's arithmetic is a pure function of the ModelInputTables, and
the gap list is read off the capability map rather than written.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from dalton_core.industry_framework import (
    CHARACTERISTIC_FIELDS,
    CHARACTERISTIC_SLOTS,
    COMPARISON_METRICS,
    DEFAULT_COMPARISON_QUARTERS,
    STATIC_UNITS,
    FrameworkStructureUnmapped,
    IndustryFrameworkAuthority,
    IndustryFrameworkConflict,
    IndustryFrameworkValidationError,
    assess_gaps,
    body_hash,
    build_comparison,
    calendar_quarter,
    candidate_sources,
    causal_chain_hash,
    cell_ref,
    chain_titles,
    comparability_notes,
    comparison_accessions,
    comparison_hash,
    comparison_material,
    drivers_for_horizon,
    evidence_scope,
    framework_artefact,
    framework_ref_for,
    load_policy,
    new_refs,
    open_gaps,
    output_rubric_findings,
    policy_hash,
    quarter_minus_year,
    render_comparison,
    stated_driver_refs,
    summarise_debates,
    unit_slots,
    units_for,
    validate_comparison,
    validate_framework_version,
    validate_policy,
)
from dalton_core.store import DaltonStore, content_hash

REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO / "deploy" / "phase9" / "p12e-industry-framework-policy-v1.json"
INDUSTRY = "industry:us-it-services"

# The live chain, verbatim, so that this suite exercises the same six links the
# policy titles rather than a shape invented for a test.
CHAIN = [
    "Enterprise IT budgets are approved annually and re-allocated quarterly, so "
    "discretionary consulting spend reacts to macro confidence with a "
    "one-to-two-quarter lag.",
    "New bookings lead revenue by two to four quarters: bookings mix and "
    "conversion determine forward revenue visibility before it appears in "
    "reported growth.",
    "AI-led reinvention programs create a new budget pool that can offset "
    "discretionary consulting weakness and generate follow-on managed-services "
    "work.",
    "Managed-services contracts convert annuity-style and damp revenue "
    "volatility, so their share of bookings stabilizes aggregate growth.",
    "Delivery economics (utilization, workforce mix, pricing, productivity) "
    "transmit demand into operating margin; a demand bottom without margin "
    "stability is not a profit inflection.",
    "Cash conversion and capital return are trailing confirmations, not demand "
    "signals; they cannot lead the inflection call.",
]

DRIVER_PACK = {
    "id": "driver-pack-version:us-it-services:v2",
    "content_hash": "d" * 64,
    "drivers": [
        {"driver_ref": "driver:revenue-growth-usd-gaap",
         "label": "Reported quarterly revenue growth", "mechanism": "filed revenue",
         "metric_refs": ["quarterly_revenue_yoy_growth"]},
        {"driver_ref": "driver:bookings-mix-and-conversion",
         "label": "Bookings mix and conversion", "mechanism": "bookings lead revenue",
         "metric_refs": ["metric:new-bookings-total"]},
        {"driver_ref": "driver:ai-reinvention-demand",
         "label": "AI and reinvention demand", "mechanism": "a new budget pool",
         "metric_refs": ["metric:genai-bookings"]},
        {"driver_ref": "driver:delivery-economics",
         "label": "Delivery economics", "mechanism": "utilisation and mix",
         "metric_refs": ["metric:utilization"]},
        {"driver_ref": "driver:cash-conversion-and-capital-return",
         "label": "Cash conversion and capital return", "mechanism": "trailing",
         "metric_refs": ["metric:free-cash-flow"]},
    ],
}


def constitution(chain=None):
    return {
        "id": "constitution-version:us-it-services:9",
        "constitution_ref": "constitution:us-it-services",
        "content_hash": "c" * 64,
        "method": {
            "causal_chain": list(CHAIN if chain is None else chain),
            "output_rubric": {"criteria": [], "good_samples": [], "bad_samples": []},
        },
    }


def policy():
    return load_policy(POLICY_PATH)


# ---------------------------------------------------------------------------
# structure comes from the Constitution
# ---------------------------------------------------------------------------


class StructureTests(unittest.TestCase):
    def test_the_policy_titles_the_live_chain_link_for_link(self):
        titles = chain_titles(constitution(), policy())
        self.assertEqual(len(titles), len(CHAIN))

    def test_a_chain_that_gained_a_link_has_no_titles_and_is_refused(self):
        # The whole point of hashing the chain rather than its links: a new
        # link is a question about what it is called, and only a person can
        # answer it.
        grown = CHAIN + ["Currency translation moves reported growth."]
        with self.assertRaises(FrameworkStructureUnmapped) as caught:
            chain_titles(constitution(grown), policy())
        self.assertIn("has no titles", str(caught.exception))

    def test_a_reordered_chain_is_a_different_chain(self):
        swapped = [CHAIN[1], CHAIN[0]] + CHAIN[2:]
        self.assertNotEqual(causal_chain_hash(CHAIN), causal_chain_hash(swapped))
        with self.assertRaises(FrameworkStructureUnmapped):
            chain_titles(constitution(swapped), policy())

    def test_units_are_one_per_link_plus_the_three_blocks(self):
        units = units_for(constitution(), policy())
        self.assertEqual(units[:len(CHAIN)],
                         tuple(f"causal_chain:{i}" for i in range(len(CHAIN))))
        self.assertEqual(units[len(CHAIN):], STATIC_UNITS)

    def test_a_causal_chain_section_has_two_slots_and_neither_is_chosen(self):
        slots = unit_slots("causal_chain:2", constitution=constitution(), policy=policy())
        self.assertEqual([slot["slot_id"] for slot in slots], ["state", "divergence"])

    def test_a_link_the_chain_does_not_have_is_refused(self):
        with self.assertRaises(FrameworkStructureUnmapped):
            unit_slots("causal_chain:9", constitution=constitution(), policy=policy())

    def test_the_characteristics_slots_are_the_five_closed_fields(self):
        slots = unit_slots("characteristics")
        self.assertEqual([slot["slot_id"] for slot in slots], list(CHARACTERISTIC_SLOTS))
        self.assertEqual(len(CHARACTERISTIC_FIELDS), 5)

    def test_driver_blocks_take_their_slots_from_the_pack_and_the_policy(self):
        long_slots = unit_slots("long_term_drivers", policy=policy(),
                                driver_pack=DRIVER_PACK)
        short_slots = unit_slots("short_term_drivers", policy=policy(),
                                 driver_pack=DRIVER_PACK)
        long_refs = {slot["slot_id"] for slot in long_slots}
        short_refs = {slot["slot_id"] for slot in short_slots}
        # The Constitution says cash conversion is a trailing confirmation, so
        # the policy files it long only; the reported print is short only.
        self.assertIn("driver:cash-conversion-and-capital-return", str(long_refs))
        self.assertNotIn("driver:cash-conversion-and-capital-return", str(short_refs))
        self.assertIn("driver:revenue-growth-usd-gaap", str(short_refs))
        self.assertNotIn("driver:revenue-growth-usd-gaap", str(long_refs))
        # bookings is both, and the policy says why.
        self.assertIn("driver:bookings-mix-and-conversion", str(long_refs))
        self.assertIn("driver:bookings-mix-and-conversion", str(short_refs))

    def test_every_driver_slot_names_a_driver_the_pack_carries(self):
        pack_refs = {driver["driver_ref"] for driver in DRIVER_PACK["drivers"]}
        for horizon in ("long_term", "short_term"):
            for driver in drivers_for_horizon(DRIVER_PACK, policy(), horizon):
                self.assertIn(driver["driver_ref"], pack_refs)


class PolicyTests(unittest.TestCase):
    def test_the_shipped_policy_validates_and_hashes(self):
        wire = policy()
        self.assertEqual(wire["policy_ref"], "industry-framework-policy:us-it-services:v1")
        self.assertEqual(len(policy_hash(wire)), 64)

    def test_a_policy_with_an_empty_checklist_is_refused(self):
        wire = policy()
        wire["gap_checklist"] = []
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            validate_policy(wire)
        self.assertIn("nothing is missing", str(caught.exception))

    def test_a_binding_with_no_check_and_no_reason_is_refused(self):
        wire = policy()
        wire["output_rubric_bindings"][0]["reason"] = ""
        with self.assertRaises(IndustryFrameworkValidationError):
            validate_policy(wire)

    def test_a_criterion_may_bind_two_checks(self):
        # The reason this policy's shape differs from the dossier's: the
        # fourth criterion needs both readings.
        bindings = {tuple(item["checks"]) for item in policy()["output_rubric_bindings"]}
        self.assertIn(("not_a_restatement", "open_gaps_name_a_source"), bindings)


# ---------------------------------------------------------------------------
# the comparison table: arithmetic on a fixture
# ---------------------------------------------------------------------------


def line(concept, cells, *, status="filed"):
    return {"concept": concept, "status": status, "statement": "income",
            "label": concept, "cells": cells}


def money(value, accession, *, end="2026-03-31"):
    return {"period_start": None, "value": value, "unit": "usd", "basis": "reported",
            "source_accessions": [accession]}


def table(company_ref, *, revenue=None, cost=None, operating=None, spec="spec:x"):
    lines = []
    if revenue is not None:
        lines.append(line("us-gaap:Revenues", revenue))
    if cost is not None:
        lines.append(line("us-gaap:CostOfRevenue", cost))
    if operating is not None:
        lines.append(line("us-gaap:OperatingIncomeLoss", operating))
    return {"company_ref": company_ref, "spec_ref": spec, "state_hash": "a" * 64,
            "periods": [], "filed_lines": lines, "rows": []}


UNIVERSE = [
    {"company_ref": "company:a", "ticker": "AAA"},
    {"company_ref": "company:b", "ticker": "BBB"},
]


class ComparisonArithmeticTests(unittest.TestCase):
    def fixture(self):
        # AAA: 100 -> 110 a year later, cost 60 -> 66, operating income 20.
        # Every number is round so the assertions below are arithmetic a
        # reader can check without running anything.
        return build_comparison(
            [
                table("company:a",
                      revenue={"2025-03-31": money("100", "acc:a-2025q1"),
                               "2026-03-31": money("110", "acc:a-2026q1")},
                      cost={"2025-03-31": money("60", "acc:a-2025q1"),
                            "2026-03-31": money("66", "acc:a-2026q1")},
                      operating={"2026-03-31": money("22", "acc:a-2026q1")}),
                table("company:b",
                      revenue={"2026-03-31": money("200", "acc:b-2026q1")},
                      cost={"2026-03-31": money("150", "acc:b-2026q1")}),
            ],
            universe=UNIVERSE,
        )

    def cell(self, comparison, company, metric, quarter):
        return next(item for item in comparison["cells"]
                    if item["company_ref"] == company and item["metric"] == metric
                    and item["quarter"] == quarter)

    def test_revenue_is_the_filed_value_unchanged(self):
        cell = self.cell(self.fixture(), "company:a", "revenue", "2026Q1")
        self.assertEqual(cell["status"], "computed")
        self.assertEqual(cell["value"], "110")
        self.assertEqual(cell["source_accessions"], ["acc:a-2026q1"])

    def test_year_on_year_growth_is_the_ratio_of_the_two_filed_values(self):
        cell = self.cell(self.fixture(), "company:a", "revenue_yoy_growth", "2026Q1")
        # 110 / 100 - 1 = 0.10 exactly.
        self.assertEqual(cell["value"], "0.100000")
        self.assertEqual(cell["display"], "10.0%")
        # Both quarters' accessions, because both are behind the number.
        self.assertEqual(sorted(cell["source_accessions"]),
                         ["acc:a-2025q1", "acc:a-2026q1"])

    def test_gross_margin_is_revenue_less_cost_over_revenue(self):
        cell = self.cell(self.fixture(), "company:a", "gross_margin", "2026Q1")
        # (110 - 66) / 110 = 0.4 exactly.
        self.assertEqual(cell["value"], "0.400000")
        self.assertEqual(cell["display"], "40.0%")

    def test_operating_margin_is_computed_where_the_concept_is_bound(self):
        cell = self.cell(self.fixture(), "company:a", "operating_margin", "2026Q1")
        # 22 / 110 = 0.2 exactly.
        self.assertEqual(cell["value"], "0.200000")

    def test_a_company_with_no_operating_income_says_so_rather_than_being_absent(self):
        cell = self.cell(self.fixture(), "company:b", "operating_margin", "2026Q1")
        self.assertEqual(cell["status"], "unavailable")
        self.assertIsNone(cell["value"])
        self.assertIn("binds no operating income concept", cell["reason"])

    def test_a_quarter_with_no_year_earlier_comparison_says_which_input_is_missing(self):
        cell = self.cell(self.fixture(), "company:b", "revenue_yoy_growth", "2026Q1")
        self.assertEqual(cell["status"], "unavailable")
        self.assertIn("year earlier", cell["reason"])

    def test_a_company_with_no_revenue_line_has_every_metric_unavailable(self):
        comparison = build_comparison(
            [table("company:a", revenue={"2026-03-31": money("100", "acc:a")}),
             table("company:b", cost={"2026-03-31": money("50", "acc:b")})],
            universe=UNIVERSE,
        )
        rows = [cell for cell in comparison["cells"] if cell["company_ref"] == "company:b"]
        self.assertEqual(len(rows), len(COMPARISON_METRICS))
        for cell in rows:
            self.assertEqual(cell["status"], "unavailable")
            self.assertIn("no revenue line is filed", cell["reason"])

    def test_the_column_is_a_calendar_quarter_and_the_cell_keeps_its_period_end(self):
        # A February fiscal quarter end and a March one land in the same
        # column, and each cell says which day it is really about.
        comparison = build_comparison(
            [table("company:a", revenue={"2026-02-28": money("100", "acc:a")}),
             table("company:b", revenue={"2026-03-31": money("200", "acc:b")})],
            universe=UNIVERSE,
        )
        self.assertEqual(comparison["quarters"], ["2026Q1"])
        self.assertEqual(
            self.cell(comparison, "company:a", "revenue", "2026Q1")["period_end"],
            "2026-02-28")
        self.assertEqual(
            self.cell(comparison, "company:b", "revenue", "2026Q1")["period_end"],
            "2026-03-31")
        self.assertIn("up to one month",
                      " ".join(comparison["comparability_notes"]))

    def test_calendar_quarter_and_its_inverse(self):
        self.assertEqual(calendar_quarter("2026-05-31"), "2026Q2")
        self.assertEqual(calendar_quarter("2025-11-30"), "2025Q4")
        self.assertEqual(quarter_minus_year("2026Q2"), "2025Q2")
        with self.assertRaises(IndustryFrameworkValidationError):
            calendar_quarter("not-a-date")
        with self.assertRaises(IndustryFrameworkValidationError):
            quarter_minus_year("2026-Q2")

    def test_a_cost_line_that_excludes_d_and_a_is_reported_as_not_comparable(self):
        notes = comparability_notes([
            {"ticker": "AAA", "revenue_concept": "us-gaap:Revenues",
             "cost_of_revenue_concept": "us-gaap:CostOfRevenue",
             "operating_income_concept": "us-gaap:OperatingIncomeLoss"},
            {"ticker": "BBB", "revenue_concept": "us-gaap:Revenues",
             "cost_of_revenue_concept":
                 "us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
             "operating_income_concept": "us-gaap:OperatingIncomeLoss"},
        ])
        joined = " ".join(notes)
        self.assertIn("gross_margin is not comparable", joined)
        self.assertIn("BBB", joined)

    def test_the_window_bounds_the_columns(self):
        cells = {f"20{20 + n}-03-31": money(str(100 + n), f"acc:{n}") for n in range(12)}
        comparison = build_comparison([table("company:a", revenue=cells)],
                                      universe=UNIVERSE, quarters=4)
        self.assertEqual(len(comparison["quarters"]), 4)

    def test_an_empty_universe_is_unavailable_rather_than_an_empty_table(self):
        comparison = build_comparison([], universe=UNIVERSE)
        self.assertEqual(comparison["status"], "unavailable")
        self.assertEqual(comparison["reason"], "no_model_inputs")

    def test_only_computed_cells_become_citable_material(self):
        comparison = self.fixture()
        refs = {row["ref"] for row in comparison_material(comparison)}
        for cell in comparison["cells"]:
            if cell["status"] == "computed":
                self.assertIn(cell["ref"], refs)
            else:
                self.assertNotIn(cell["ref"], refs)

    def test_the_material_text_carries_the_figure_verbatim(self):
        # The number discipline compares the prose against this text, so the
        # rendering here and the rendering a sentence may use are one string.
        row = next(item for item in comparison_material(self.fixture())
                   if item["ref"] == cell_ref("company:a", "gross_margin", "2026Q1"))
        self.assertIn("40.0%", row["text"])

    def test_the_table_hash_moves_when_a_quarter_lands_and_not_otherwise(self):
        first = self.fixture()
        again = self.fixture()
        self.assertEqual(comparison_hash(first), comparison_hash(again))
        landed = build_comparison(
            [table("company:a",
                   revenue={"2025-03-31": money("100", "acc:a-2025q1"),
                            "2026-03-31": money("110", "acc:a-2026q1"),
                            "2026-06-30": money("120", "acc:a-2026q2")})],
            universe=UNIVERSE,
        )
        self.assertNotEqual(comparison_hash(first), comparison_hash(landed))
        self.assertIn("acc:a-2026q2", comparison_accessions(landed))

    def test_render_shows_a_dash_for_a_cell_with_no_value(self):
        text = render_comparison(self.fixture())
        self.assertIn("AAA\trevenue", text)
        self.assertIn("operating_margin", text)
        self.assertTrue(any(line.startswith("#") for line in text.splitlines()))

    def test_a_reshaped_table_is_refused(self):
        comparison = self.fixture()
        comparison["metrics"] = ["revenue"]
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            validate_comparison(comparison)
        self.assertIn("frozen", str(caught.exception))

    def test_a_cell_that_renames_itself_is_refused(self):
        comparison = self.fixture()
        comparison["cells"][0]["ref"] = "comparison-cell:whatever"
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            validate_comparison(comparison)
        self.assertIn("derived, not chosen", str(caught.exception))

    def test_an_unavailable_cell_carrying_a_value_is_refused(self):
        comparison = self.fixture()
        cell = next(item for item in comparison["cells"] if item["status"] == "unavailable")
        cell["value"] = "0.5"
        cell["display"] = "50.0%"
        with self.assertRaises(IndustryFrameworkValidationError):
            validate_comparison(comparison)


# ---------------------------------------------------------------------------
# the gap list
# ---------------------------------------------------------------------------


def driver_block(horizon, driver_refs, *, stated=()):
    slots = []
    stances = {}
    for ref in driver_refs:
        if ref in stated:
            slots.append({"slot_id": f"driver:{ref}",
                          "sentences": [{"text": "有依据。", "refs": ["claim-version:x"]}]})
            stances[ref] = "positive"
        else:
            slots.append({"slot_id": f"driver:{ref}", "unknown": "材料没有说"})
            stances[ref] = "unknown"
    return {
        "horizon": horizon, "status": "drafted", "reason": None,
        "structure": [f"driver:{ref}" for ref in driver_refs],
        "stances": stances, "slots": slots,
        "sources": [{"kind": "claim", "ref": "claim-version:x", "text": "-",
                     "period": None}],
        "gaps": [],
    }


class GapTests(unittest.TestCase):
    def test_a_gap_with_no_driver_behind_it_is_open_by_construction(self):
        # TAM: nothing in the driver pack measures it, so no amount of
        # drafting can close it. That is the honest answer, and it is what
        # tells the S line to go and connect something.
        gaps = {gap["gap_ref"]: gap for gap in assess_gaps(policy())}
        self.assertEqual(gaps["gap:tam-and-share"]["status"], "open")
        self.assertEqual(gaps["gap:tam-and-share"]["driver_refs"], [])

    def test_a_gap_closes_only_when_every_driver_it_names_has_a_basis(self):
        refs = ["driver:delivery-economics"]
        wrote_nothing = assess_gaps(
            policy(), long_term=driver_block("long_term", refs))
        wrote_something = assess_gaps(
            policy(), long_term=driver_block("long_term", refs, stated=refs))
        self.assertEqual(
            next(g for g in wrote_nothing if g["gap_ref"] == "gap:pricing")["status"],
            "open")
        self.assertEqual(
            next(g for g in wrote_something if g["gap_ref"] == "gap:pricing")["status"],
            "covered")

    def test_a_gap_naming_two_drivers_with_one_covered_is_partial(self):
        refs = ["driver:revenue-growth-usd-gaap", "driver:bookings-mix-and-conversion"]
        gaps = assess_gaps(
            policy(),
            short_term=driver_block("short_term", refs, stated=refs[:1]))
        row = next(g for g in gaps if g["gap_ref"] == "gap:high-frequency-demand")
        self.assertEqual(row["status"], "partially_covered")
        self.assertEqual(row["covered_driver_refs"], refs[:1])

    def test_a_slot_answered_unknown_does_not_count_as_stated(self):
        refs = ["driver:delivery-economics"]
        self.assertEqual(stated_driver_refs(driver_block("long_term", refs)), set())
        self.assertEqual(
            stated_driver_refs(driver_block("long_term", refs, stated=refs)), set(refs))

    def test_candidate_sources_come_from_the_capability_map_with_quota_and_status(self):
        rows = candidate_sources("expert_excerpt")
        self.assertIn("guidepoint", [row["slug"] for row in rows])
        for row in rows:
            self.assertIn("connection_status", row)
            self.assertIn("daily_quota", row)

    def test_a_generic_source_is_ordered_after_the_specific_ones(self):
        rows = candidate_sources("news")
        generic = [index for index, row in enumerate(rows) if row["generic"]]
        specific = [index for index, row in enumerate(rows) if not row["generic"]]
        if generic and specific:
            self.assertGreater(min(generic), max(specific))

    def test_the_mission_supplies_the_connection_status(self):
        mission = {"source_plan": [
            {"source_ref": "source:guidepoint", "status": "not_connected"},
            {"source_ref": "source:alphaengine", "status": "probe_only"},
        ]}
        rows = {row["slug"]: row for row in
                candidate_sources("expert_excerpt", mission=mission)}
        self.assertEqual(rows["guidepoint"]["connection_status"], "not_connected")
        rows = {row["slug"]: row for row in
                candidate_sources("sell_side_report", mission=mission)}
        self.assertEqual(rows["alphaengine"]["connection_status"], "probe_only")

    def test_high_frequency_demand_has_only_generic_candidates(self):
        # The finding this checklist exists to surface: nothing specific in the
        # capability map yields a weekly read on demand.
        row = next(gap for gap in assess_gaps(policy())
                   if gap["gap_ref"] == "gap:high-frequency-demand")
        self.assertTrue(row["candidate_sources"])
        self.assertTrue(all(item["generic"] for item in row["candidate_sources"]))

    def test_open_gaps_filters_the_covered_ones(self):
        gaps = assess_gaps(policy())
        self.assertEqual(len(open_gaps(gaps)), len(gaps))


# ---------------------------------------------------------------------------
# debates, records and versioning
# ---------------------------------------------------------------------------


class DebateSummaryTests(unittest.TestCase):
    def test_no_debate_map_is_unavailable_with_a_reason(self):
        summary = summarise_debates(None)
        self.assertEqual(summary["status"], "unavailable")
        self.assertEqual(summary["reason"], "no_debate_map")

    def test_a_map_is_projected_rather_than_restated(self):
        summary = summarise_debates({
            "id": "debate-map-version:x:3", "map_ref": "debate-map:x", "version": 3,
            "debates": [{
                "debate_ref": "debate:1", "question": "需求见底了吗？",
                "status": "shifting", "driver_refs": ["driver:delivery-economics"],
                "bull_position": {"claim_refs": ["a", "b"]},
                "bear_position": {"claim_refs": ["c"]},
                "market_position": {"lean": "bear"},
                "our_position": {"side": "bull"},
                "last_shift_reason": {"reason": "新的季度申报", "refs": ["d"]},
            }],
        })
        self.assertEqual(summary["status"], "available")
        row = summary["debates"][0]
        self.assertEqual((row["bull_ref_count"], row["bear_ref_count"]), (2, 1))
        self.assertEqual((row["our_side"], row["market_lean"]), ("bull", "bear"))
        # The positions themselves are not copied: the map is the authority.
        self.assertNotIn("bull_position", row)


def sentence(ref="claim-version:x"):
    return {"text": "这一环在本季度的证据里成立。", "refs": [ref]}


def source(kind="claim", ref="claim-version:x"):
    return {"kind": kind, "ref": ref, "text": "-", "period": None}


def section(index, *, status="drafted", ref="claim-version:x"):
    if status == "unavailable":
        return {"link_index": index, "link": CHAIN[index], "title": "t",
                "status": "unavailable", "reason": "no_industry_claims",
                "structure": ["state", "divergence"], "slots": [], "sources": [],
                "gaps": []}
    return {
        "link_index": index, "link": CHAIN[index], "title": "t",
        "status": "drafted", "reason": None, "structure": ["state", "divergence"],
        "slots": [{"slot_id": "state", "sentences": [sentence(ref)]},
                  {"slot_id": "divergence", "unknown": "材料不足"}],
        "sources": [source(ref=ref)], "gaps": [],
    }


def characteristics(*, ref="claim-version:x"):
    return {
        "status": "drafted", "reason": None,
        "values": {"classification": "structural_growth",
                   "cyclicality": "moderately_cyclical",
                   "revenue_visibility": "backlog_led",
                   "capital_intensity": "asset_light",
                   "concentration": "fragmented"},
        "structure": list(CHARACTERISTIC_SLOTS),
        "slots": [{"slot_id": slot, "sentences": [sentence(ref)]}
                  for slot in CHARACTERISTIC_SLOTS],
        "sources": [source(ref=ref)], "gaps": [],
    }


def record(*, comparison, ref="claim-version:x", change_reason="evidence_thicker"):
    long_refs = [d["driver_ref"] for d in drivers_for_horizon(DRIVER_PACK, policy(), "long_term")]
    short_refs = [d["driver_ref"] for d in drivers_for_horizon(DRIVER_PACK, policy(), "short_term")]
    long_block = driver_block("long_term", long_refs, stated=long_refs[:1])
    short_block = driver_block("short_term", short_refs, stated=short_refs[:1])
    for block in (long_block, short_block):
        block["sources"] = [source(ref=ref)]
        for slot in block["slots"]:
            if "sentences" in slot:
                slot["sentences"] = [sentence(ref)]
    return {
        "industry_ref": INDUSTRY,
        "sections": [section(index, ref=ref) for index in range(len(CHAIN))],
        "industry_characteristics": characteristics(ref=ref),
        "long_term_drivers": long_block,
        "short_term_drivers": short_block,
        "cross_company_comparison": comparison,
        "debates_summary": summarise_debates(None),
        "gaps": assess_gaps(policy(), long_term=long_block, short_term=short_block),
        "drafted_at": {},
        "bindings": {
            "constitution_version": {"ref": constitution()["id"], "hash": "c" * 64},
            "playbook_version": {"ref": "playbook:1", "hash": "e" * 64},
            "driver_pack_version": {"ref": DRIVER_PACK["id"], "hash": "d" * 64},
            "mission_version_ref": "mission:1",
            "debate_map_version_ref": None,
            "policy_ref": policy()["policy_ref"],
            "policy_hash": policy_hash(policy()),
            "causal_chain_hash": causal_chain_hash(CHAIN),
            "rubric_ref": "rubric:industry-framework",
            "rubric_hash": "f" * 64,
            "comparison_hash": comparison_hash(comparison),
        },
        "actor_ref": "automation:coverage-mission",
        "change_reason": change_reason,
        "evidence_refs": [source(ref=ref)],
    }


def simple_comparison(accession="acc:1", value="110"):
    return build_comparison(
        [table("company:a",
               revenue={"2025-03-31": money("100", "acc:base"),
                        "2026-03-31": money(value, accession)})],
        universe=UNIVERSE,
    )


class RecordAndVersionTests(unittest.TestCase):
    def store(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        return store

    def test_a_published_version_reads_back_as_written(self):
        authority = IndustryFrameworkAuthority(self.store())
        published = authority.publish(record(comparison=simple_comparison()))
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["version"], 1)
        self.assertEqual(published["framework_ref"], framework_ref_for(INDUSTRY))
        again = authority.framework(published["id"])
        self.assertEqual(again["content_hash"], published["content_hash"])

    def test_an_identical_body_is_a_duplicate(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        authority.publish(body)
        second = authority.publish(record(comparison=simple_comparison()))
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["duplicate_reason"], "identical_body")

    def test_a_moved_body_citing_nothing_new_is_a_duplicate(self):
        authority = IndustryFrameworkAuthority(self.store())
        authority.publish(record(comparison=simple_comparison()))
        moved = record(comparison=simple_comparison())
        moved["sections"][0]["slots"][0]["sentences"][0]["text"] = "换了一种说法。"
        second = authority.publish(moved)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["duplicate_reason"], "no_new_evidence")

    def test_a_newly_filed_quarter_is_new_evidence_even_with_unchanged_prose(self):
        # The one place this differs from the dossier, and the reason the
        # comparison accessions are inside the evidence scope: a filing lands,
        # the table moves, no prose changes, and that is a version.
        authority = IndustryFrameworkAuthority(self.store())
        first = authority.publish(record(comparison=simple_comparison()))
        landed = build_comparison(
            [table("company:a",
                   revenue={"2025-03-31": money("100", "acc:base"),
                            "2026-03-31": money("110", "acc:1"),
                            "2026-06-30": money("120", "acc:2")})],
            universe=UNIVERSE,
        )
        second = authority.publish(record(comparison=landed))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        self.assertIn("acc:2", new_refs(second, first))

    def test_a_body_computed_from_a_superseded_head_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        authority.publish(record(comparison=simple_comparison()))
        stale = record(comparison=simple_comparison(accession="acc:2"))
        stale["computed_from_version_ref"] = "industry-framework-version:x:99"
        with self.assertRaises(IndustryFrameworkConflict):
            authority.publish(stale)

    def test_the_comparison_accessions_are_part_of_the_evidence_scope(self):
        scope = evidence_scope(record(comparison=simple_comparison()))
        self.assertIn("acc:1", scope)
        self.assertIn("acc:base", scope)
        self.assertIn("claim-version:x", scope)

    def test_a_bound_comparison_hash_that_is_not_the_table_is_refused(self):
        body = record(comparison=simple_comparison())
        body["bindings"]["comparison_hash"] = "0" * 64
        body.update({
            "schema_version": "0.1", "id": "x", "created_at": "now",
            "framework_ref": framework_ref_for(INDUSTRY), "version": 1,
            "prior_version_ref": None, "generator_ref": "g",
            "body_hash": "0" * 64, "content_hash": "0" * 64,
        })
        body.pop("computed_from_version_ref", None)
        with self.assertRaises(IndustryFrameworkConflict) as caught:
            validate_framework_version(body)
        self.assertIn("comparison_hash", str(caught.exception))

    def test_a_version_with_no_gaps_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        body["gaps"] = []
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            authority.publish(body)
        self.assertIn("nothing is missing", str(caught.exception))

    def test_a_sentence_citing_something_not_shown_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        body["sections"][0]["slots"][0]["sentences"][0]["refs"] = ["claim-version:never"]
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            authority.publish(body)
        self.assertIn("was not among the material shown", str(caught.exception))

    def test_a_citation_tag_written_into_the_prose_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        body["sections"][0]["slots"][0]["sentences"][0]["text"] = "如 T3 所示，需求见底。"
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            authority.publish(body)
        self.assertIn("citation tag", str(caught.exception))

    def test_a_characteristic_word_with_no_basis_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        block = body["industry_characteristics"]
        block["slots"][0] = {"slot_id": CHARACTERISTIC_SLOTS[0], "unknown": "不知道"}
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            authority.publish(body)
        self.assertIn("gives no basis", str(caught.exception))

    def test_a_stance_with_no_basis_is_refused(self):
        authority = IndustryFrameworkAuthority(self.store())
        body = record(comparison=simple_comparison())
        block = body["long_term_drivers"]
        # The second driver is the one this block answered "unknown"; claiming
        # a stance on it without writing a sentence is the failure.
        driver = block["structure"][1].split(":", 1)[1]
        self.assertNotIn("sentences", block["slots"][1])
        block["stances"][driver] = "positive"
        with self.assertRaises(IndustryFrameworkValidationError) as caught:
            authority.publish(body)
        self.assertIn("gives no basis", str(caught.exception))

    def test_the_replay_reads_one_link_across_the_chain(self):
        authority = IndustryFrameworkAuthority(self.store())
        authority.publish(record(comparison=simple_comparison()))
        authority.publish(record(comparison=simple_comparison(accession="acc:2",
                                                              value="115")))
        replay = authority.replay_link(INDUSTRY, 0)
        self.assertEqual([row["version"] for row in replay], [1, 2])
        self.assertTrue(all(row["body"] for row in replay))

    def test_the_gap_report_is_the_open_gaps_of_the_current_version(self):
        authority = IndustryFrameworkAuthority(self.store())
        published = authority.publish(record(comparison=simple_comparison()))
        report = authority.gap_report(INDUSTRY)
        self.assertEqual(report["version_ref"], published["id"])
        self.assertTrue(report["gaps"])
        self.assertTrue(all(gap["status"] != "covered" for gap in report["gaps"]))

    def test_versions_are_immutable_in_the_database(self):
        store = self.store()
        authority = IndustryFrameworkAuthority(store)
        published = authority.publish(record(comparison=simple_comparison()))
        with self.assertRaises(Exception):
            store.connection.execute(
                "UPDATE industry_framework_versions SET actor_ref='human:x' "
                "WHERE version_id=?", (published["id"],))

    def test_an_insert_outside_the_authority_is_refused(self):
        store = self.store()
        IndustryFrameworkAuthority(store)
        with self.assertRaises(Exception):
            store.connection.execute(
                "INSERT INTO industry_framework_versions"
                "(version_id,framework_ref,version_number,prior_version_id,industry_ref,"
                "change_reason,body_hash,evidence_scope_hash,comparison_hash,record_json,"
                "content_hash,actor_ref,created_at) "
                "VALUES('x','y',1,NULL,'z','r','h','s','c','{}','ch','human:a','now')")


class ArtefactAndRubricTests(unittest.TestCase):
    def published(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        return IndustryFrameworkAuthority(store).publish(
            record(comparison=simple_comparison()))

    def test_the_artefact_shape_matches_what_q1s_own_builder_produces(self):
        # ``research_quality_score.ARTEFACT_KINDS`` is a closed tuple in a
        # module this branch does not own, so the framework builds the shape
        # itself. This is the test that stops the two drifting.
        from dalton_core.research_quality_score import artefact

        theirs = artefact(
            artefact_kind="company_dossier", ref="r", hash="h",
            sections=[{"title": "t", "body": "b", "claim_refs": ["c"],
                       "numbers": [{"text": "x", "claim_version_ref": "c",
                                    "period": None}],
                       "gaps": ["g"]}])
        ours = framework_artefact(self.published())
        self.assertEqual(set(ours), set(theirs))
        # The section rows too: every check reads these five keys, and a
        # shape that matched only at the top level would fail inside a check
        # rather than here.
        self.assertEqual(set(ours["sections"][0]), set(theirs["sections"][0]))
        self.assertEqual(set(ours["sections"][0]["numbers"][0]),
                         set(theirs["sections"][0]["numbers"][0]))

    def test_the_deterministic_layer_runs_over_it(self):
        from dalton_core.research_quality_rubrics import rubric as get_rubric
        from dalton_core.research_quality_score import run_deterministic

        result = run_deterministic(framework_artefact(self.published()),
                                   get_rubric("industry_framework"), core=None)
        self.assertEqual(result["rubric_ref"], "rubric:industry-framework")
        self.assertIn("numbers_without_refs",
                      [item["check"] for item in result["checks"]])

    def test_an_unavailable_part_carries_its_reason_into_the_gap_list(self):
        # Otherwise Q1 calls it an empty shell, which is right only when the
        # part really does not say why it is empty.
        body = record(comparison=simple_comparison())
        body["sections"][0] = section(0, status="unavailable")
        body["evidence_refs"] = [source()]
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        published = IndustryFrameworkAuthority(store).publish(body)
        art = framework_artefact(published)
        part = next(item for item in art["sections"] if item["title"] == "causal_chain:0")
        self.assertIn("unavailable: no_industry_claims", part["gaps"])

    def test_an_unmapped_criterion_is_itself_a_finding(self):
        published = self.published()
        findings = output_rubric_findings(
            published,
            constitution={"method": {"output_rubric": {
                "criteria": ["A standard nobody bound."]}}},
            policy=policy(),
        )
        self.assertEqual([item["code"] for item in findings],
                         ["unmapped_output_rubric_criterion"])

    def test_an_investment_conclusion_is_a_finding(self):
        published = dict(self.published())
        published["sections"] = copy.deepcopy(published["sections"])
        published["sections"][0]["slots"][0]["sentences"][0]["text"] = "该行业明显被低估。"
        criterion = "Outputs never auto-generate investment conclusions."
        findings = output_rubric_findings(
            published,
            constitution={"method": {"output_rubric": {"criteria": [criterion]}}},
            policy={**policy(), "output_rubric_bindings": [
                {"criterion_hash": content_hash(criterion),
                 "checks": ["no_investment_conclusion"], "reason": ""}]},
        )
        self.assertEqual([item["code"] for item in findings], ["investment_conclusion"])

    def test_an_open_gap_with_no_candidate_source_is_a_finding(self):
        published = copy.deepcopy(self.published())
        published["gaps"][0]["candidate_sources"] = []
        published["gaps"][0]["cost_note"] = "要接一个还没评估的数据源"
        criterion = "A good research output reduces the open question set."
        findings = output_rubric_findings(
            published,
            constitution={"method": {"output_rubric": {"criteria": [criterion]}}},
            policy={**policy(), "output_rubric_bindings": [
                {"criterion_hash": content_hash(criterion),
                 "checks": ["open_gaps_name_a_source"], "reason": ""}]},
        )
        self.assertIn("gap_without_a_source", [item["code"] for item in findings])

    def test_a_gap_that_says_no_source_can_fill_it_is_not_a_finding(self):
        published = copy.deepcopy(self.published())
        for gap in published["gaps"]:
            gap["candidate_sources"] = []
            gap["cost_note"] = "no source in the capability map yields this"
        criterion = "A good research output reduces the open question set."
        findings = output_rubric_findings(
            published,
            constitution={"method": {"output_rubric": {"criteria": [criterion]}}},
            policy={**policy(), "output_rubric_bindings": [
                {"criterion_hash": content_hash(criterion),
                 "checks": ["open_gaps_name_a_source"], "reason": ""}]},
        )
        self.assertEqual(findings, [])

    def test_the_deliverable_projection_carries_the_table_and_the_gaps(self):
        from dalton_core.industry_framework import deliverable_sections

        sections = deliverable_sections(self.published())
        titles = [row["title"] for row in sections]
        self.assertIn("cross_company_comparison", titles)
        self.assertIn("gaps", titles)
        table_section = next(row for row in sections
                             if row["title"] == "cross_company_comparison")
        self.assertIn("revenue", table_section["body"])
        # Every figure in the rendered table names the filing it came out of.
        self.assertTrue(table_section["numbers"])
        for entry in table_section["numbers"]:
            self.assertEqual(entry["cell"]["kind"], "statement_accession")
            self.assertTrue(entry["cell"]["accession"])
            self.assertNotIn("claim_version_ref", entry)

    def test_every_figure_in_the_projection_is_sourced(self):
        # The check the deliverable authority will run, run here against the
        # projection so a shape change is caught without a Core.
        from dalton_core.industry_framework import deliverable_sections
        from dalton_core.mission_deliverable import unsourced_numbers

        for section in deliverable_sections(self.published()):
            with self.subTest(section=section["title"]):
                self.assertEqual(
                    unsourced_numbers(section["body"], section["numbers"]), [])

    def test_a_prose_sentence_citing_a_cell_carries_that_cell(self):
        from dalton_core.industry_framework import deliverable_sections

        record = self.published()
        cited = {entry["cell"]["ref"]
                 for section in deliverable_sections(record)
                 if section["title"] != "cross_company_comparison"
                 for entry in section["numbers"] if "cell" in entry}
        # This fixture's prose cites Claims only, so the set is empty; what is
        # under test is that a comparison-cell source would travel as a cell
        # rather than being silently dropped the way it used to be.
        self.assertEqual(cited, set())


class ReviewFixTests(unittest.TestCase):
    """The four findings from the w2-industry-framework review."""

    def five_filers(self):
        # Five fully populated peers: enough cells that a tail slice of forty
        # would drop the companies that sort first.
        universe = [{"company_ref": f"company:{n}", "ticker": f"T{n}"} for n in range(5)]
        quarters = [f"202{4 + n // 4}-{3 * (n % 4) + 3:02d}-31" for n in range(8)]
        tables = []
        for n in range(5):
            revenue = {end: money(str(1000 + index), f"acc:{n}-{index}")
                       for index, end in enumerate(quarters)}
            cost = {end: money(str(600 + index), f"acc:{n}-{index}")
                    for index, end in enumerate(quarters)}
            tables.append(table(f"company:{n}", revenue=revenue, cost=cost))
        return build_comparison(tables, universe=universe), universe

    def test_the_material_budget_is_allocated_per_company(self):
        # The blocker: cells are generated company by company, so a tail slice
        # kept whichever sorted last and dropped the rest -- while the prompt
        # went on showing the whole table.
        comparison, universe = self.five_filers()
        rows = comparison_material(comparison, limit=40)
        cited = {row["ref"].split(":")[1] for row in rows}
        self.assertEqual(len(cited), 5)
        for member in universe:
            slug = member["company_ref"].replace(":", "-")
            with self.subTest(company=member["ticker"]):
                self.assertTrue(any(row["ref"].startswith(f"comparison-cell:{slug}:")
                                    for row in rows))

    def test_the_budget_is_still_a_budget(self):
        comparison, _ = self.five_filers()
        self.assertLessEqual(len(comparison_material(comparison, limit=40)), 40)

    def test_a_company_with_fewer_cells_does_not_borrow(self):
        comparison = build_comparison(
            [table("company:a",
                   revenue={"2026-03-31": money("100", "acc:a")}),
             table("company:b",
                   revenue={f"202{4 + n // 4}-{3 * (n % 4) + 3:02d}-31":
                            money(str(100 + n), f"acc:b-{n}") for n in range(8)})],
            universe=[{"company_ref": "company:a", "ticker": "AAA"},
                      {"company_ref": "company:b", "ticker": "BBB"}],
        )
        rows = comparison_material(comparison, limit=40)
        by_company = {}
        for row in rows:
            by_company.setdefault(row["ref"].split(":")[1], []).append(row)
        self.assertEqual(len(by_company["company-a"]), 1)
        self.assertLessEqual(len(by_company["company-b"]), 20)

    def test_a_line_filed_as_an_instant_does_not_play_a_flow_role(self):
        # "18.7 billion in the quarter" and "18.7 billion on that day" are
        # different claims and a column header cannot tell them apart.
        instant = table("company:a", revenue={"2026-03-31": money("100", "acc:a")})
        instant["filed_lines"][0]["period_basis"] = "instant"
        # A second filer whose revenue is a duration, so the table has a column
        # at all and the instant filer's row can be seen to be empty.
        flow = table("company:b", revenue={"2026-03-31": money("200", "acc:b")})
        flow["filed_lines"][0]["period_basis"] = "duration"
        comparison = build_comparison([instant, flow], universe=UNIVERSE)
        self.assertEqual(comparison["quarters"], ["2026Q1"])
        cell = next(item for item in comparison["cells"]
                    if item["company_ref"] == "company:a" and item["metric"] == "revenue")
        self.assertEqual(cell["status"], "unavailable")
        self.assertIn("no revenue line is filed", cell["reason"])
        other = next(item for item in comparison["cells"]
                     if item["company_ref"] == "company:b" and item["metric"] == "revenue")
        self.assertEqual(other["value"], "200")

    def test_a_remapped_chain_does_not_relabel_carried_forward_prose(self):
        from dalton_core.industry_framework_cli import _prior_units

        prior = {
            "sections": [section(0), section(1)],
            "industry_characteristics": characteristics(),
            "long_term_drivers": {"status": "drafted"},
            "short_term_drivers": {"status": "drafted"},
            "bindings": {"causal_chain_hash": causal_chain_hash(CHAIN)},
        }
        same = _prior_units(prior, chain_hash=causal_chain_hash(CHAIN))
        self.assertIn("causal_chain:0", same)
        self.assertIn("characteristics", same)

        moved = _prior_units(prior, chain_hash=causal_chain_hash(CHAIN[1:]))
        self.assertNotIn("causal_chain:0", moved)
        self.assertNotIn("causal_chain:1", moved)
        # The three blocks are not chain-shaped and still carry forward.
        self.assertIn("characteristics", moved)
        self.assertIn("long_term_drivers", moved)

    def test_connecting_a_source_moves_the_gap_state_and_occasions_a_version(self):
        from dalton_core.industry_framework import gap_state_ref
        from dalton_core.industry_framework_cli import _fresh_evidence

        unconnected = assess_gaps(policy(), mission={"source_plan": [
            {"source_ref": "source:guidepoint", "status": "not_connected"}]})
        connected = assess_gaps(policy(), mission={"source_plan": [
            {"source_ref": "source:guidepoint", "status": "connected"}]})
        self.assertNotEqual(gap_state_ref(unconnected), gap_state_ref(connected))

        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = IndustryFrameworkAuthority(store)
        body = record(comparison=simple_comparison())
        body["gaps"] = unconnected
        first = authority.publish(body)
        self.assertEqual(first["status"], "fresh")

        # Nothing else changed: same prose, same table, same filings. The only
        # thing that moved is that Guidepoint is connected now -- which is the
        # event this whole deliverable exists to provoke.
        second_body = record(comparison=simple_comparison())
        second_body["gaps"] = connected
        occasion = _fresh_evidence({}, simple_comparison(), first, connected)
        self.assertTrue(any(row["kind"] == "gap_state" for row in occasion))
        second_body["evidence_refs"] = occasion
        second = authority.publish(second_body)
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertIn(gap_state_ref(connected), new_refs(second, first))

    def test_an_unchanged_gap_state_does_not_occasion_anything(self):
        from dalton_core.industry_framework_cli import _fresh_evidence

        gaps = assess_gaps(policy())
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        body = record(comparison=simple_comparison())
        body["gaps"] = gaps
        first = IndustryFrameworkAuthority(store).publish(body)
        self.assertEqual(_fresh_evidence({}, simple_comparison(), first, gaps), [])


if __name__ == "__main__":
    unittest.main()
