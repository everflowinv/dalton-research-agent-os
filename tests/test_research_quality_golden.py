"""Q1: the golden sets, run for real.

Twenty cases: the five published live Initial Screens exactly as the Ledger
holds them, three deliberately broken variants of the Accenture one, seven
question/answer/claims triples built from real ACN and EPAM Claims, and five
hand-written company-dossier examples.

The deterministic layer runs for real against every one of them, and its
answers are pinned.  A check that changes its mind about a document nobody
edited is exactly what a golden set exists to notice -- and four of the five
live screens are gate-passed, which is terminal, so those documents will never
change again.

The judge layer runs against a fake model.  What is being tested there is that
a rubric's criteria and a judged reply line up, and that an artefact this large
still fits the prompt's bounds; the model's opinion is not under test and
paying for it here would make the suite cost money.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.research_quality_rubrics import RUBRIC_ALIASES, rubric as get_rubric
from dalton_core.research_quality_score import (
    MAX_ARTEFACT_CHARS,
    MAX_INPUT_TOKENS,
    build_judge_prompt,
    judge,
    run_deterministic,
)

GOLDEN = Path(__file__).resolve().parent / "golden"


def cases(rubric_name: str | None = None) -> list[dict]:
    found = []
    for path in sorted(GOLDEN.glob("*/*.json")):
        if rubric_name is not None and path.parent.name != rubric_name:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["path"] = path
        found.append(payload)
    return found


class GoldenSetShapeTests(unittest.TestCase):
    def test_there_is_a_directory_per_rubric_and_no_others(self):
        directories = {path.name for path in GOLDEN.iterdir() if path.is_dir()}
        self.assertEqual(directories, set(RUBRIC_ALIASES))

    def test_each_rubric_has_between_five_and_ten_cases(self):
        for name in RUBRIC_ALIASES:
            with self.subTest(rubric=name):
                self.assertGreaterEqual(len(cases(name)), 5)
                self.assertLessEqual(len(cases(name)), 10)

    def test_every_case_says_where_it_came_from_and_why_it_is_here(self):
        for case in cases():
            with self.subTest(case=case["case_ref"]):
                self.assertTrue(case["source"].strip())
                self.assertTrue(case["note"].strip())
                self.assertTrue(case["expected"]["rationale"].strip())

    def test_every_case_says_its_ranges_are_calibration_rather_than_measurement(self):
        # No real judge call has been made against any of these. The ranges are
        # the author's reading of what a good grader would say; the flag is
        # there so nobody later mistakes them for observed results.
        for case in cases():
            with self.subTest(case=case["case_ref"]):
                self.assertIs(case["expected"]["score_ranges_are_calibration_only"], True)

    def test_every_case_carries_a_range_for_every_criterion(self):
        for case in cases():
            rubric = get_rubric(case["rubric"])
            ranges = case["expected"]["score_ranges"]
            with self.subTest(case=case["case_ref"]):
                self.assertEqual(set(ranges), set(rubric.criterion_ids))
                for criterion_id, (low, high) in ranges.items():
                    self.assertLessEqual(low, high, criterion_id)
                    self.assertGreaterEqual(low, 0)
                    self.assertLessEqual(high, 4)

    def test_the_live_screens_carry_their_real_refs(self):
        live = [case for case in cases("initial_screen") if case["case_ref"].startswith("live-")]
        self.assertEqual(len(live), 5)
        for case in live:
            with self.subTest(case=case["case_ref"]):
                self.assertTrue(case["artefact"]["ref"].startswith("mission-deliverable-version:"))
                refs = {
                    item["claim_version_ref"]
                    for section in case["artefact"]["sections"] for item in section["numbers"]
                }
                self.assertTrue(all(ref.startswith("claim-version:") for ref in refs))


class DeterministicGoldenTests(unittest.TestCase):
    """The layer that needs no model, run for real over every case."""

    def test_every_case_matches_its_pinned_check_results(self):
        for case in cases():
            rubric = get_rubric(case["rubric"])
            with self.subTest(case=case["case_ref"]):
                result = run_deterministic(case["artefact"], rubric)
                actual = {item["check"]: {"status": item["status"], "count": item["count"]}
                          for item in result["checks"]}
                self.assertEqual(actual, case["expected"]["deterministic"])

    def test_the_published_accenture_screen_still_carries_all_three_defects(self):
        # The three the roadmap named in Appendix A. gate_passed is terminal,
        # so this document can never be redrafted and these never go away.
        case = next(item for item in cases("initial_screen") if item["case_ref"] == "live-acn-v2")
        result = run_deterministic(case["artefact"], get_rubric("initial_screen"))
        checks = {item["check"]: item for item in result["checks"]}
        self.assertEqual(checks["residual_citation_artefacts"]["status"], "fail")
        self.assertEqual(checks["duplicate_parallel_citations"]["status"], "fail")
        duplicate = checks["duplicate_parallel_citations"]["findings"][0]
        self.assertEqual(duplicate["period"], "2025-09-01..2025-11-30")
        self.assertEqual(len(duplicate["refs"]), 3)
        # And the one numeric series: every cited figure is quarterly revenue.
        periods = {item["period"] for section in case["artefact"]["sections"]
                   for item in section["numbers"]}
        self.assertTrue(all("Revenues" in item["text"]
                            for section in case["artefact"]["sections"]
                            for item in section["numbers"]))
        self.assertGreaterEqual(len(periods), 3)

    def test_one_live_screen_is_clean_which_is_why_it_is_the_baseline(self):
        case = next(item for item in cases("initial_screen") if item["case_ref"] == "live-dxc-v1")
        self.assertTrue(run_deterministic(case["artefact"], get_rubric("initial_screen"))["passed"])

    def test_each_broken_variant_is_caught_by_the_check_it_was_built_for(self):
        built_for = {
            "broken-numbers-without-refs": "numbers_without_refs",
            "broken-residual-markers": "residual_citation_artefacts",
            "broken-missing-anti-thesis": "required_sections_present",
        }
        for case in cases("initial_screen"):
            if case["case_ref"] not in built_for:
                continue
            with self.subTest(case=case["case_ref"]):
                result = run_deterministic(case["artefact"], get_rubric("initial_screen"))
                checks = {item["check"]: item["status"] for item in result["checks"]}
                self.assertEqual(checks[built_for[case["case_ref"]]], "fail")

    def test_the_honest_unknown_answer_passes_everything(self):
        case = next(item for item in cases("ask_answer") if item["case_ref"] == "honest-unknown")
        self.assertTrue(run_deterministic(case["artefact"], get_rubric("ask_answer"))["passed"])


class ReplayingJudge:
    """A fake model that answers each golden case with its own expected scores.

    Not a stand-in for a real judge -- it cannot be wrong -- but it exercises
    exactly what the judge layer is responsible for: building a prompt from a
    real artefact and verifying a reply against the rubric it was given.
    """

    def __init__(self, scores):
        self.scores = scores
        self.prompts = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        return {"text": json.dumps({"scores": self.scores}), "replayed": False,
                "cost_micros": 0, "work_order_ref": "work:fake", "invocation_ref": "invocation:fake",
                "result_envelope_ref": "result:fake", "route_decision_ref": "route:fake"}


class JudgedGoldenTests(unittest.TestCase):
    def test_every_case_can_be_judged_and_the_reply_verifies(self):
        for case in cases():
            rubric = get_rubric(case["rubric"])
            ranges = case["expected"]["score_ranges"]
            scores = [
                {"criterion_id": criterion_id, "score": ranges[criterion_id][0],
                 "evidence": f"以 {case['case_ref']} 的这一部分为依据"}
                for criterion_id in rubric.criterion_ids
            ]
            with self.subTest(case=case["case_ref"]):
                model = ReplayingJudge(scores)
                result = judge(case["artefact"], rubric,
                               run_deterministic(case["artefact"], rubric), model=model,
                               mission={"id": "coverage-mission-version:test", "content_hash": "h"},
                               request_id=case["case_ref"])
                self.assertEqual(result["status"], "scored", result.get("reason"))
                self.assertEqual(len(result["scores"]), len(rubric.criterion_ids))

    def test_every_prompt_fits_the_byte_bound_the_call_is_admitted_under(self):
        # MAX_INPUT_TOKENS is measured in bytes, not tokens: build_work compares
        # len(prompt.encode("utf-8")) against it, and the router estimates on
        # prompt bytes too. The name of the constant is the router's; this
        # asserts what is actually applied. The biggest live screen is ~13k
        # characters of body plus its Claims; if a real document did not fit,
        # the judge would be refused before the call rather than here.
        for case in cases():
            rubric = get_rubric(case["rubric"])
            prompt = build_judge_prompt(case["artefact"], rubric,
                                        run_deterministic(case["artefact"], rubric))
            with self.subTest(case=case["case_ref"]):
                self.assertLess(len(prompt.encode("utf-8")), MAX_INPUT_TOKENS)
                self.assertLess(len(prompt), MAX_ARTEFACT_CHARS * 2)

    def test_a_reply_from_another_rubric_is_refused_on_a_real_document(self):
        case = next(item for item in cases("initial_screen") if item["case_ref"] == "live-acn-v2")
        screen = get_rubric("initial_screen")
        ask = get_rubric("ask_answer")
        model = ReplayingJudge([
            {"criterion_id": criterion_id, "score": 3, "evidence": "x"}
            for criterion_id in ask.criterion_ids
        ])
        result = judge(case["artefact"], screen, run_deterministic(case["artefact"], screen),
                       model=model, mission={"id": "m", "content_hash": "h"}, request_id="r")
        self.assertEqual(result["status"], "refused")


if __name__ == "__main__":
    unittest.main()
