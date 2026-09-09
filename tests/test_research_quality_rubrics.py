"""Q1: the rubrics are contracts, so their hashes are pinned.

A rubric that can be edited without anyone noticing is not a standard, it is a
preference.  Every score record binds the rubric's content hash, so changing a
criterion's wording changes what every future score means -- and these
assertions are the place where that change has to be made deliberately.

If one of these fails and the change was intended: update the hash here, say
why in the commit, and expect the scores written under the old hash to keep
saying what they said.  They were true about the old standard.
"""

from __future__ import annotations

import unittest

from dalton_core.research_quality_rubrics import (
    ASK_ANSWER,
    COMPANY_DOSSIER,
    DOSSIER_SECTIONS,
    INITIAL_SCREEN,
    RUBRICS,
    SCALE,
    UnknownRubric,
    rubric,
    rubric_hashes,
)
from dalton_core.research_quality_score import CHECKS

PINNED = {
    "rubric:initial-screen": "334c9aee0ffebfc3816ee8f367d9304c225f1a05d29cfb3dbf405247260e2d64",
    "rubric:ask-answer": "7fe91c6057b145c7d7f23e5697a6e097c487a9905dfce0c5fa1ae4e6e9146e37",
    "rubric:company-dossier": "0b1718d165bad133ce66fc5ca2cfb5ac3d5fb5d67dc61ff30d8bc54f8519ee51",
}


class FrozenRubricTests(unittest.TestCase):
    def test_the_hashes_are_what_the_report_published(self):
        self.assertEqual(rubric_hashes(), PINNED)

    def test_the_hash_covers_the_standard_and_moves_when_it_does(self):
        before = INITIAL_SCREEN.content_hash
        body = INITIAL_SCREEN.body()
        body["criteria"][0]["anchors"]["4"] = "something else"
        from dalton_core.store import content_hash

        self.assertNotEqual(content_hash(body), before)
        # And the rubric itself did not move: body() returns a copy.
        self.assertEqual(INITIAL_SCREEN.content_hash, before)

    def test_a_rubric_can_be_looked_up_by_short_name_or_ref(self):
        self.assertIs(rubric("initial_screen"), INITIAL_SCREEN)
        self.assertIs(rubric("rubric:initial-screen"), INITIAL_SCREEN)
        self.assertIs(rubric("ask_answer"), ASK_ANSWER)
        self.assertIs(rubric("company_dossier"), COMPANY_DOSSIER)
        with self.assertRaises(UnknownRubric):
            rubric("weekly_brief")

    def test_there_is_deliberately_no_weekly_brief_rubric(self):
        # The owner deferred the weekly brief to the end of the roadmap. A
        # rubric for an artefact nobody is building would be graded by nothing.
        self.assertNotIn("rubric:weekly-brief", RUBRICS)


class CriterionShapeTests(unittest.TestCase):
    def test_every_criterion_is_anchored_at_zero_two_and_four(self):
        for item in RUBRICS.values():
            for criterion in item.criteria:
                with self.subTest(rubric=item.rubric_ref, criterion=criterion.criterion_id):
                    self.assertEqual(set(criterion.anchors), {"0", "2", "4"})
                    for level, text in criterion.anchors.items():
                        self.assertTrue(text.strip(), f"{criterion.criterion_id} anchor {level}")

    def test_the_scale_names_all_five_levels(self):
        self.assertEqual(set(SCALE), {"0", "1", "2", "3", "4"})

    def test_every_criterion_says_what_evidence_a_score_needs(self):
        for item in RUBRICS.values():
            for criterion in item.criteria:
                with self.subTest(criterion=criterion.criterion_id):
                    self.assertTrue(criterion.evidence_required.strip())
                    self.assertIn(criterion.layer, {"deterministic", "judge", "both"})

    def test_criterion_ids_are_unique_within_a_rubric(self):
        for item in RUBRICS.values():
            with self.subTest(rubric=item.rubric_ref):
                self.assertEqual(len(set(item.criterion_ids)), len(item.criterion_ids))

    def test_every_named_check_exists_in_the_scorer(self):
        # The contract between the two modules: a rubric may only name a check
        # that can actually be run, or a score would silently be missing a
        # criterion's evidence.
        for item in RUBRICS.values():
            for check in item.deterministic_checks:
                with self.subTest(rubric=item.rubric_ref, check=check):
                    self.assertIn(check, CHECKS)

    def test_a_criterion_with_checks_is_not_declared_judge_only(self):
        for item in RUBRICS.values():
            for criterion in item.criteria:
                if criterion.checks:
                    with self.subTest(criterion=criterion.criterion_id):
                        self.assertIn(criterion.layer, {"deterministic", "both"})


class RubricContentTests(unittest.TestCase):
    def test_the_initial_screen_rubric_covers_the_four_live_defects(self):
        ids = set(INITIAL_SCREEN.criterion_ids)
        self.assertIn("number_provenance", ids)
        self.assertIn("citation_hygiene", ids)
        self.assertIn("citation_dedupe", ids)
        self.assertIn("anti_thesis", ids)
        self.assertIn("gaps_honest", ids)

    def test_the_initial_screen_rubric_does_not_penalise_the_blank_valuation(self):
        # S6 is left blank on purpose: no market-data authority is admitted and
        # the Playbook says an unsourced number is worse than a gap. A judge
        # that does not know that marks the document down for obeying its own
        # contract.
        self.assertTrue(any("估值" in note for note in INITIAL_SCREEN.grading_notes))

    def test_the_ask_rubric_restates_adr_0006(self):
        ids = set(ASK_ANSWER.criterion_ids)
        self.assertIn("cites_only_shown_claims", ids)
        self.assertIn("no_invented_numbers", ids)
        self.assertIn("confidence_stated", ids)
        self.assertIn("admits_unknown", ids)
        self.assertIn("stays_a_cockpit_artifact", ids)

    def test_the_dossier_rubric_carries_the_roadmaps_stop_loss(self):
        # "dossier 版本必须绑定新证据才能发布；不满足就 duplicate"
        ids = set(COMPANY_DOSSIER.criterion_ids)
        self.assertIn("new_version_new_evidence", ids)
        self.assertIn("no_restatement_drift", ids)

    def test_the_dossier_has_ten_named_sections(self):
        self.assertEqual(len(DOSSIER_SECTIONS), 10)
        self.assertEqual(len(set(DOSSIER_SECTIONS)), 10)


if __name__ == "__main__":
    unittest.main()
