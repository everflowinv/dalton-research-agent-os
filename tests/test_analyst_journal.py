"""Q1: PM feedback, kept where the next draft can read it."""

from __future__ import annotations

import unittest

from dalton_core.analyst_journal import (
    AnalystJournalAuthority,
    AnalystJournalValidationError,
    VERDICTS,
    journal_context,
    render_journal_for_prompt,
    validate_score_override,
)
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"
OWNER = "human:lumos"
SCREEN = "mission-deliverable-version:06e126b45f078b98e1a92d241c0f244f"
SCREEN_HASH = "ab05c760e6afc8324ab0b06a0df054637d808f257fa4bf9c1eee4f0eb49b2c31"


class JournalHarness(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.journal = AnalystJournalAuthority(self.store)

    def add(self, **overrides):
        params = {"target_ref": SCREEN, "target_hash": SCREEN_HASH,
                  "target_kind": "initial_screen", "verdict": "read", "actor_ref": OWNER,
                  "company_ref": ACN}
        params.update(overrides)
        return self.journal.add(**params)


class VocabularyTests(JournalHarness):
    def test_the_five_words_are_the_whole_vocabulary(self):
        self.assertEqual(VERDICTS,
                         ("read", "useful", "needs_more_evidence", "disagree", "revise"))
        for verdict in VERDICTS:
            with self.subTest(verdict=verdict):
                entry = self.add(verdict=verdict, target_ref=f"{SCREEN}:{verdict}")
                self.assertEqual(entry["verdict"], verdict)

    def test_a_word_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(AnalystJournalValidationError):
            self.add(verdict="excellent")

    def test_a_target_kind_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(AnalystJournalValidationError):
            self.add(target_kind="spreadsheet")

    def test_only_a_person_may_write_in_the_journal(self):
        # Automation grading itself is the quality score and has its own record.
        with self.assertRaises(AnalystJournalValidationError) as caught:
            self.add(actor_ref="automation:coverage-mission")
        self.assertIn("human:", str(caught.exception))


class EntryTests(JournalHarness):
    def test_an_entry_binds_what_was_read_by_hash(self):
        entry = self.add(verdict="useful", note="S4 的反向观点写得好")
        self.assertEqual(entry["target_hash"], SCREEN_HASH)
        self.assertEqual(entry["entry_number"], 1)
        self.assertEqual(entry["status"], "fresh")
        self.assertEqual(self.journal.entry(entry["id"])["note"], "S4 的反向观点写得好")

    def test_entries_are_numbered_per_target_and_never_edited(self):
        first = self.add(verdict="needs_more_evidence", note="缺 bookings")
        second = self.add(verdict="revise", note="补上 bookings 后重写 S7")
        self.assertEqual([first["entry_number"], second["entry_number"]], [1, 2])
        self.assertEqual([entry["verdict"] for entry in self.journal.for_target(SCREEN)],
                         ["needs_more_evidence", "revise"])

    def test_a_repeat_click_is_a_duplicate_rather_than_a_second_entry(self):
        first = self.add(verdict="useful", idempotency_key="cockpit:button:1")
        again = self.add(verdict="useful", idempotency_key="cockpit:button:1")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.journal.for_target(SCREEN)), 1)

    def test_the_same_verdict_about_a_rewritten_document_is_a_new_entry(self):
        self.add(verdict="useful")
        rewritten = self.add(verdict="useful", target_hash="f" * 64)
        self.assertEqual(rewritten["entry_number"], 2)
        self.assertNotEqual(rewritten["target_hash"], SCREEN_HASH)

    def test_entries_are_append_only_at_the_database(self):
        self.add()
        with self.assertRaises(Exception):
            self.store.connection.execute("UPDATE analyst_journal_entries SET verdict='useful'")
        with self.assertRaises(Exception):
            self.store.connection.execute("DELETE FROM analyst_journal_entries")

    def test_a_write_outside_the_authority_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self.store.connection.execute(
                "INSERT INTO analyst_journal_entries(entry_id,entry_number,target_ref,target_hash,"
                "target_kind,verdict,record_json,content_hash,actor_ref,created_at) "
                "VALUES('e',1,'t','h','initial_screen','read','{}','c','human:x','2026-09-09')")
        self.assertIn("AnalystJournalAuthority", str(caught.exception))


class ScoreOverrideTests(JournalHarness):
    def test_an_override_names_the_rubric_it_is_speaking_about(self):
        override = validate_score_override(
            {"rubric_ref": "rubric:initial-screen", "scores": {"gaps_honest": 4}})
        self.assertEqual(override["scores"], {"gaps_honest": 4})
        with self.assertRaises(AnalystJournalValidationError):
            validate_score_override({"scores": {"gaps_honest": 4}})

    def test_a_score_outside_the_scale_is_refused(self):
        for value in (5, -1, 2.5, "3", True):
            with self.subTest(value=value), self.assertRaises(AnalystJournalValidationError):
                validate_score_override({"rubric_ref": "rubric:initial-screen",
                                         "scores": {"gaps_honest": value}})

    def test_an_override_with_nothing_in_it_is_refused(self):
        with self.assertRaises(AnalystJournalValidationError):
            validate_score_override({"rubric_ref": "rubric:initial-screen"})

    def test_an_override_is_recorded_beside_the_judge_not_instead_of_it(self):
        entry = self.add(verdict="disagree",
                         score_override={"rubric_ref": "rubric:initial-screen",
                                         "scores": {"key_driver_thesis": 1}, "overall": 2})
        self.assertEqual(entry["score_override"]["overall"], 2)
        self.assertEqual(entry["score_override"]["scores"], {"key_driver_thesis": 1})


class ReaderTests(JournalHarness):
    def setUp(self):
        super().setUp()
        self.add(verdict="read")
        self.add(verdict="needs_more_evidence", note="缺 bookings，无法判断订单动能")
        self.add(target_ref="mission-deliverable-version:epam", target_hash="e" * 64,
                 company_ref=EPAM, verdict="useful", note="EPAM 的口径核对写得清楚")

    def test_a_reader_for_one_target(self):
        self.assertEqual(len(self.journal.for_target(SCREEN)), 2)

    def test_a_reader_for_one_company(self):
        self.assertEqual([entry["verdict"] for entry in self.journal.for_company(ACN)],
                         ["read", "needs_more_evidence"])
        self.assertEqual(len(self.journal.for_company(EPAM)), 1)

    def test_the_entries_that_ask_for_something_can_be_read_on_their_own(self):
        outstanding = self.journal.outstanding(ACN)
        self.assertEqual([entry["verdict"] for entry in outstanding], ["needs_more_evidence"])
        self.assertEqual(len(self.journal.outstanding()), 1)

    def test_the_context_is_shaped_for_a_prompt_with_the_newest_last(self):
        context = journal_context(self.journal.for_company(ACN))
        self.assertEqual(context["entries"], 2)
        self.assertIn("缺 bookings", context["lines"][-1])
        self.assertIn("证据不够", context["lines"][-1])

    def test_the_prompt_block_says_what_to_do_with_the_feedback(self):
        block = render_journal_for_prompt(self.journal.for_company(ACN))
        self.assertIn("analyst journal", block)
        self.assertIn("缺 bookings", block)
        self.assertIn("不要再用同样的材料交一遍", block)

    def test_an_empty_journal_contributes_nothing_to_a_prompt(self):
        self.assertEqual(render_journal_for_prompt([]), "")

    def test_the_context_is_bounded(self):
        for index in range(40):
            self.add(target_ref=f"{SCREEN}:{index}", verdict="read")
        context = journal_context(self.journal.for_company(ACN))
        self.assertEqual(context["entries"], 20)
        self.assertEqual(context["of"], 42)


if __name__ == "__main__":
    unittest.main()
