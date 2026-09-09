"""Q2: the weekly-brief rubric, its capability gate, and the golden set.

Two of this rubric's ten criteria cannot be graded yet, and the interesting
part of the design is that this is a *fact about the Core* rather than a
reading of the document.  So the gate is a deterministic check, its answer is
``not_applicable_yet`` with a reason instead of a zero, and the tests below
pin both halves: a Core with neither layer withholds two criteria and says
why; a Core with both grants them and the rubric becomes fully gradeable
without a single word of it changing.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from dalton_core.research_quality_rubrics import (
    WEEKLY_BRIEF,
    WEEKLY_BRIEF_SECTIONS,
    rubric as get_rubric,
)
from dalton_core.research_quality_score import (
    CHECKS,
    REF_MARKER,
    WEEKLY_BRIEF_CAPABILITY_SOURCES,
    artefact_from_weekly_brief,
    run_deterministic,
    weekly_brief_capabilities,
)
from dalton_core.weekly_brief import ISSUE_SECTIONS

GOLDEN = Path(__file__).resolve().parent / "golden" / "weekly-brief"


def cases() -> list[dict]:
    found = []
    for path in sorted(GOLDEN.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["path"] = path
        found.append(payload)
    return found


class RubricContentTests(unittest.TestCase):
    def test_it_covers_the_six_things_the_weekly_meeting_asks_for(self):
        # P15c: 上周价格表现与归因、观点变化、debate 转向、预测变动、缺口、下周计划.
        ids = set(WEEKLY_BRIEF.criterion_ids)
        for criterion in ("price_performance_attribution", "view_changes", "debate_shifts",
                          "forecast_changes", "gaps_named", "next_week_plan"):
            with self.subTest(criterion=criterion):
                self.assertIn(criterion, ids)

    def test_it_also_carries_the_three_disciplines_every_output_is_held_to(self):
        ids = set(WEEKLY_BRIEF.criterion_ids)
        self.assertIn("number_provenance", ids)
        self.assertIn("citation_hygiene", ids)
        self.assertIn("citation_dedupe", ids)

    def test_the_template_is_the_authoritys_own_section_list(self):
        # A rubric that graded a structure the object does not have would be
        # grading the roadmap. The copy exists so a golden case needs no live
        # authority; this assertion is what stops it drifting.
        self.assertEqual(WEEKLY_BRIEF_SECTIONS, ISSUE_SECTIONS)

    def test_only_the_two_ungranted_layers_are_gated(self):
        gated = {c.criterion_id: c.capability for c in WEEKLY_BRIEF.criteria if c.capability}
        self.assertEqual(gated, {
            "price_performance_attribution": "market_price",
            "debate_shifts": "debate_map",
        })
        # Forecast reconciliation is live and the second published issue was
        # meant to carry it, so a brief that says nothing about the forecast is
        # a brief that skipped something it could have done.
        self.assertIsNone(WEEKLY_BRIEF.criterion("forecast_changes").capability)

    def test_the_grading_notes_forbid_scoring_an_ungranted_layer_zero(self):
        notes = " ".join(WEEKLY_BRIEF.grading_notes)
        self.assertIn("not_applicable_yet", notes)
        self.assertIn("永远不记 0 分", notes)
        self.assertIn("owner 搁置的是周报投递，不是周报评估", notes)

    def test_a_baseline_issue_is_not_penalised_for_having_no_delta(self):
        self.assertTrue(any("baseline" in note for note in WEEKLY_BRIEF.grading_notes))

    def test_every_capability_it_names_is_one_the_gate_can_probe(self):
        for criterion in WEEKLY_BRIEF.criteria:
            if criterion.capability:
                with self.subTest(criterion=criterion.criterion_id):
                    self.assertIn(criterion.capability, WEEKLY_BRIEF_CAPABILITY_SOURCES)


class CapabilityGateTests(unittest.TestCase):
    def _core(self, *, price: bool, debate: bool, scopes: list[str]) -> sqlite3.Connection:
        core = sqlite3.connect(":memory:")
        core.row_factory = sqlite3.Row
        core.execute("CREATE TABLE coverage_mission_pointer(mission_ref TEXT, mission_version_id TEXT)")
        core.execute("CREATE TABLE coverage_mission_versions(mission_version_id TEXT, record_json TEXT)")
        core.execute("INSERT INTO coverage_mission_pointer VALUES('m','mv')")
        core.execute("INSERT INTO coverage_mission_versions VALUES('mv',?)",
                     (json.dumps({"autonomy": {"may_write": scopes}}),))
        # Every real Core has this one; the reference check reads it, and a
        # stub without it would make this test about the wrong thing.
        core.execute("CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY)")
        if price:
            core.execute("CREATE TABLE market_price_series_versions(version_id TEXT)")
            core.execute("INSERT INTO market_price_series_versions VALUES('v')")
        if debate:
            core.execute("CREATE TABLE debate_map_versions(version_id TEXT)")
            core.execute("INSERT INTO debate_map_versions VALUES('v')")
        self.addCleanup(core.close)
        return core

    def test_without_a_core_nothing_is_assumed_to_exist(self):
        capabilities = weekly_brief_capabilities(None)
        self.assertFalse(capabilities["market_price"]["present"])
        self.assertFalse(capabilities["debate_map"]["present"])
        self.assertIn("没有 Core 连接", capabilities["market_price"]["reason"])

    def test_a_grant_with_no_records_behind_it_is_not_a_capability(self):
        core = self._core(price=False, debate=False, scopes=["market_price", "debate_map"])
        capabilities = weekly_brief_capabilities(core)
        self.assertFalse(capabilities["market_price"]["present"])
        self.assertIn("尚无记录", capabilities["market_price"]["reason"])

    def test_records_with_no_grant_behind_them_are_not_a_capability_either(self):
        core = self._core(price=True, debate=True, scopes=["claim"])
        capabilities = weekly_brief_capabilities(core)
        self.assertFalse(capabilities["market_price"]["present"])
        self.assertIn("未授予 market_price", capabilities["market_price"]["reason"])

    def test_both_halves_present_makes_the_whole_rubric_gradeable(self):
        core = self._core(price=True, debate=True, scopes=["market_price", "debate_map"])
        case = next(item for item in cases() if item["case_ref"] == "live-w36-second-issue")
        result = run_deterministic(case["artefact"], WEEKLY_BRIEF, core=core)
        gate = next(item for item in result["checks"]
                    if item["check"] == "weekly_brief_capability_gate")
        self.assertEqual(gate["status"], "pass")
        self.assertEqual(gate["count"], 0)

    def test_the_gate_withholds_rather_than_fails(self):
        # Nothing about the document failed, so the deterministic layer as a
        # whole must not be dragged down by the gate.
        case = next(item for item in cases() if item["case_ref"] == "live-w36-second-issue")
        result = run_deterministic(case["artefact"], WEEKLY_BRIEF)
        gate = next(item for item in result["checks"]
                    if item["check"] == "weekly_brief_capability_gate")
        self.assertEqual(gate["status"], "skipped")
        self.assertNotIn("weekly_brief_capability_gate", result["failed_checks"])
        for finding in gate["findings"]:
            with self.subTest(criterion=finding["criterion_id"]):
                self.assertEqual(finding["code"], "not_applicable_yet")
                self.assertTrue(finding["reason"].strip())

    def test_the_gate_is_the_only_check_that_reads_the_rubric(self):
        # It is a check about the rubric, not about the document, which is why
        # ``run_deterministic`` passes the rubric down. Everything else ignores
        # it, and this asserts the plumbing exists.
        self.assertIn("weekly_brief_capability_gate", CHECKS)
        result = CHECKS["weekly_brief_capability_gate"]({"sections": []}, {"core": None})
        self.assertEqual(result["status"], "pass")


class AdapterTests(unittest.TestCase):
    ISSUE = {
        "id": "weekly-brief-version:test", "content_hash": "hash",
        "brief_ref": "weekly-brief:test", "industry_ref": "industry:test",
    }
    BODY = (
        "# Title｜每周研究 Brief\n\n"
        "- Issue：weekly-brief-version:test (aa11bb22cc33dd44ee55ff66aa77bb88"
        "cc99dd00ee11ff22aa33bb44cc55dd66)\n\n"
        "## 本期研究变化\n\n"
        "- 新增 Claim｜company:sec-cik:0001467373｜quarterly_revenue_yoy_growth｜"
        "5.59 percent｜2026-03-01..2026-05-31｜claim-version:" + "a" * 64 + "\n\n"
        "## 来源与 authority\n\n"
        "- source:sec-edgar｜evidence=evidence-version:" + "b" * 64
        + "｜retrieved=2026-08-27T06:31:56.471063+00:00｜hash=" + "c" * 64 + "\n"
    )
    CLAIMS = [{
        "claim_version_ref": "claim-version:" + "a" * 64,
        "period": "2026-03-01..2026-05-31", "value": "5.59", "unit": "percent",
        "normalized_statement": "Accenture plc reported Revenues of USD 18718144000 for "
                                "2026-03-01..2026-05-31, up 5.59% year over year.",
    }]

    def build(self, **kwargs):
        return artefact_from_weekly_brief(
            self.ISSUE, body=self.BODY, claim_versions=self.CLAIMS, **kwargs
        )

    def test_the_preamble_is_not_a_section(self):
        art = self.build()
        self.assertEqual([s["title"] for s in art["sections"]],
                         ["本期研究变化", "来源与 authority"])

    def test_a_claim_ref_becomes_a_citation_and_leaves_a_marker_behind(self):
        art = self.build()
        first = art["sections"][0]
        self.assertEqual(first["claim_refs"], ["claim-version:" + "a" * 64])
        self.assertNotIn("a" * 64, first["body"])
        self.assertIn(REF_MARKER, first["body"])

    def test_a_company_ref_becomes_its_ticker_when_one_is_known(self):
        art = self.build(company_names={"company:sec-cik:0001467373": "ACN"})
        self.assertIn("ACN", art["sections"][0]["body"])
        self.assertNotIn("0001467373", art["sections"][0]["body"])

    def test_a_cik_is_not_left_in_the_prose_to_be_read_as_a_figure(self):
        # 0001467373 is a value token by every rule the number check has.
        art = self.build()
        result = run_deterministic(art, WEEKLY_BRIEF)
        numbers = next(item for item in result["checks"]
                       if item["check"] == "numbers_without_refs")
        self.assertEqual(numbers["status"], "pass")

    def test_a_figure_is_traceable_in_both_the_renderings_that_exist(self):
        # The filing says "up 5.59%"; the brief prints "5.59 percent". Only one
        # of those is the token in the body.
        art = self.build()
        texts = [item["text"] for item in art["sections"][0]["numbers"]]
        self.assertIn("5.59 percent", texts)
        self.assertTrue(any("up 5.59%" in text for text in texts))

    def test_hashes_and_timestamps_are_machinery_not_figures(self):
        art = self.build()
        sources = art["sections"][1]
        self.assertNotIn("471063", sources["body"])
        self.assertNotIn("c" * 64, sources["body"])

    def test_the_template_it_is_measured_against_is_the_frozen_one(self):
        self.assertEqual(tuple(self.build()["expected_sections"]), WEEKLY_BRIEF_SECTIONS)


class GoldenSetTests(unittest.TestCase):
    def test_two_of_them_are_the_live_issues_with_their_real_refs(self):
        # The live Core holds exactly two published weekly briefs, so "the
        # three most recent" is two. Both are here, both by their real version
        # id, and both were delivered to Discord.
        live = [case for case in cases() if case["case_ref"].startswith("live-")]
        self.assertEqual(len(live), 2)
        for case in live:
            with self.subTest(case=case["case_ref"]):
                self.assertTrue(case["artefact"]["ref"].startswith("weekly-brief-version:"))
                refs = {ref for section in case["artefact"]["sections"]
                        for ref in section["claim_refs"]}
                self.assertTrue(all(ref.startswith("claim-version:") for ref in refs))

    def test_both_live_issues_are_missing_the_reconciliation_section(self):
        # Neither was published with 预测对账: the section joined the template
        # after them, and both records still declare seven sections. It is the
        # one structural defect the live corpus actually has.
        for case in cases():
            if not case["case_ref"].startswith("live-"):
                continue
            with self.subTest(case=case["case_ref"]):
                result = run_deterministic(case["artefact"], WEEKLY_BRIEF)
                sections = next(item for item in result["checks"]
                                if item["check"] == "required_sections_present")
                self.assertEqual(sections["status"], "fail")
                self.assertEqual(sections["findings"],
                                 [{"code": "missing_section", "title": "预测对账"}])

    def test_the_live_issues_carry_no_unsourced_figure(self):
        # Same reading as Q1's on the Initial Screens: the number discipline is
        # a hard gate upstream, and it holds.
        for case in cases():
            if not case["case_ref"].startswith("live-"):
                continue
            with self.subTest(case=case["case_ref"]):
                checks = {item["check"]: item["status"]
                          for item in run_deterministic(case["artefact"], WEEKLY_BRIEF)["checks"]}
                self.assertEqual(checks["numbers_without_refs"], "pass")
                self.assertEqual(checks["residual_citation_artefacts"], "pass")
                self.assertEqual(checks["duplicate_parallel_citations"], "pass")

    def test_each_broken_variant_is_caught_by_the_check_it_was_built_for(self):
        built_for = {
            "broken-unsourced-figure": "numbers_without_refs",
            "broken-residual-markers": "residual_citation_artefacts",
            "broken-duplicate-parallel-citations": "duplicate_parallel_citations",
        }
        for case in cases():
            if case["case_ref"] not in built_for:
                continue
            with self.subTest(case=case["case_ref"]):
                checks = {item["check"]: item["status"]
                          for item in run_deterministic(case["artefact"], WEEKLY_BRIEF)["checks"]}
                self.assertEqual(checks[built_for[case["case_ref"]]], "fail")

    def test_a_broken_variant_breaks_only_what_it_was_built_to_break(self):
        # The point of deriving all three from one live issue: a defect that
        # bleeds into a neighbouring check is a detector problem, not a
        # document problem.
        baseline = {
            item["check"]: item["status"]
            for item in run_deterministic(
                next(c for c in cases() if c["case_ref"] == "live-w36-second-issue")["artefact"],
                WEEKLY_BRIEF,
            )["checks"]
        }
        built_for = {
            "broken-unsourced-figure": "numbers_without_refs",
            "broken-residual-markers": "residual_citation_artefacts",
            "broken-duplicate-parallel-citations": "duplicate_parallel_citations",
        }
        for case in cases():
            if case["case_ref"] not in built_for:
                continue
            actual = {item["check"]: item["status"]
                      for item in run_deterministic(case["artefact"], WEEKLY_BRIEF)["checks"]}
            for check, status in baseline.items():
                if check == built_for[case["case_ref"]]:
                    continue
                with self.subTest(case=case["case_ref"], check=check):
                    self.assertEqual(actual[check], status)

    def test_every_case_names_the_criteria_the_gate_withholds(self):
        for case in cases():
            with self.subTest(case=case["case_ref"]):
                self.assertEqual(
                    set(case["expected"]["not_applicable_yet"]),
                    {"price_performance_attribution", "debate_shifts"},
                )

    def test_the_pinned_gate_count_is_the_size_of_what_is_not_yet_gradeable(self):
        # This number should fall to zero as Wave 1A's grant and Wave 2's
        # DebateMap land, and a golden set is where that becomes visible.
        for case in cases():
            with self.subTest(case=case["case_ref"]):
                self.assertEqual(
                    case["expected"]["deterministic"]["weekly_brief_capability_gate"],
                    {"status": "skipped", "count": 2},
                )

    def test_the_rubric_short_name_matches_the_golden_directory(self):
        self.assertEqual(GOLDEN.name, "weekly-brief")
        self.assertIs(get_rubric(GOLDEN.name), WEEKLY_BRIEF)


if __name__ == "__main__":
    unittest.main()
