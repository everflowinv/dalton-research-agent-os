"""D5: polish what is going to be read, and never hide what is being decided.

``research-language-policy.json`` says ``{"required": true}`` on the live Core
and that has been read as "translate and language-check every version of every
product, including the intermediate ones and the ones about to be rejected".
Half the last forty-eight hours of work orders went on it.

The flag keeps its name and gains a scope, and the default scope is the change:
what is going to be delivered gets polished, and nothing else does.  The legacy
bare ``true`` reads as the new default rather than as the old behaviour --
nobody chose the old behaviour, and the installation that wants it says so in
one line.

And the second half, which is not about cost at all: a product sitting at a
human checkpoint is shown in its own words whether or not the polish has run.
Asking somebody to approve twelve answers they cannot see is not a decision.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_language_policy import (
    DEFAULT_SCOPE,
    POLICY_FILE_NAME,
    STAGES,
    UNPOLISHED_NOTE,
    ResearchLanguagePolicyError,
    default_policy,
    hide_body_until_reviewed,
    load_policy,
    normalise,
    review_required_for,
)


class DefaultTests(unittest.TestCase):
    def test_the_default_polishes_only_what_is_delivered(self):
        policy = default_policy()
        self.assertTrue(policy["required"])
        self.assertEqual(policy["scope"], DEFAULT_SCOPE)
        self.assertTrue(review_required_for(policy, "approved_final"))
        for stage in ("intermediate", "rejected", "pending_decision"):
            self.assertFalse(review_required_for(policy, stage))

    def test_the_legacy_bare_flag_reads_as_the_new_default(self):
        policy = normalise({"required": True})
        self.assertEqual(policy["scope"], DEFAULT_SCOPE)
        self.assertFalse(review_required_for(policy, "intermediate"))

    def test_an_installation_that_wants_everything_says_so_in_one_line(self):
        policy = normalise({"required": True, "scope": "all"})
        for stage in STAGES:
            self.assertTrue(review_required_for(policy, stage))

    def test_turning_it_off_turns_it_off_everywhere(self):
        policy = normalise({"required": False})
        self.assertEqual(policy["scope"], "off")
        for stage in STAGES:
            self.assertFalse(review_required_for(policy, stage))

    def test_a_misspelt_key_is_a_typo_rather_than_a_default(self):
        with self.assertRaises(ResearchLanguagePolicyError):
            normalise({"requred": True})
        with self.assertRaises(ResearchLanguagePolicyError):
            normalise({"scope": "sometimes"})

    def test_an_unknown_stage_is_refused_rather_than_guessed(self):
        with self.assertRaises(ResearchLanguagePolicyError):
            review_required_for(default_policy(), "whenever")


class HidingTests(unittest.TestCase):
    def test_a_body_at_a_checkpoint_is_never_hidden(self):
        for scope in ("off", "approved_final", "all"):
            policy = normalise({"required": scope != "off", "scope": scope})
            self.assertFalse(hide_body_until_reviewed(policy, "pending_decision"))

    def test_a_deliverable_still_waits_for_its_polish_when_the_policy_asks(self):
        self.assertTrue(hide_body_until_reviewed(default_policy(), "approved_final"))

    def test_the_page_has_a_sentence_for_showing_unpolished_text(self):
        self.assertIn("尚未润色", UNPOLISHED_NOTE)


class FileTests(unittest.TestCase):
    def test_an_absent_file_is_the_default(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(load_policy(Path(name)), default_policy())

    def test_a_file_is_read(self):
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / POLICY_FILE_NAME).write_text(
                json.dumps({"required": True, "scope": "all"}), encoding="utf-8")
            self.assertEqual(load_policy(Path(name))["scope"], "all")

    def test_an_unreadable_file_falls_back_rather_than_stopping_research(self):
        # The one place in this work package where fail-open is right: language
        # review is presentation, and a Core that cannot read its polish
        # setting should still publish research.
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / POLICY_FILE_NAME).write_text("{not json", encoding="utf-8")
            self.assertEqual(load_policy(Path(name)), default_policy())


class ApprovalCardTests(unittest.TestCase):
    """The page itself: the gate's twelve answers are rendered, not withheld."""

    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / "src" / "dalton_core"
                     / "cockpit_control.html").read_text(encoding="utf-8")

    def test_the_gate_card_no_longer_withholds_its_details(self):
        # The old shape moved every detail into the technical fold and printed
        # one sentence about text formatting in its place.
        self.assertNotIn('if(gatePending)', self.html)
        self.assertIn("文字表达尚未润色", self.html)

    def test_the_card_shows_the_previous_review_and_what_changed(self):
        self.assertIn("上一版审阅意见", self.html)
        self.assertIn("本版改了什么", self.html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
