"""C2-5: the retirement patrol advances instead of re-reading its first batch.

The live shape (2026-09-16): ``claim_retirement_challenges`` and
``claim_retirement_decisions`` both hold 72 rows, every one written on
2026-09-07, while ``claim_versions`` grew from about a thousand to 6,390 in the
same window.  The lane was never disabled -- it is registered in
``writer_lanes`` and called on every controller tick by
``bounded_planner_driver`` -- and the mission does grant ``claim_challenge``.

It stopped because a Claim that *passed* the detectors left no trace, so the
"unchallenged Claims, oldest first" query returned the same set on every tick
and the forty-document I/O bound was always spent on the same oldest forty
originals.  5,400 Claims committed after 2026-09-07 were never looked at once.
"""

from __future__ import annotations

import sqlite3
import unittest

from dalton_core.claim_retirement import subject_needles
from dalton_core.claim_review import DETECTOR_SET_REF, ClaimReviewDriver

from tests.test_claim_retirement import (
    ACN,
    EPAM,
    OFF_TOPIC,
    ON_TOPIC,
    ClaimRetirementHarness,
)


class PatrolAdvanceTests(ClaimRetirementHarness):
    def test_a_cleared_claim_is_not_examined_a_second_time(self) -> None:
        self.claim(statement="Management sees stronger demand.", source=ON_TOPIC)
        driver = self.driver()
        first = driver.run_once()
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["examined"], 1)
        second = driver.run_once()
        # This is the whole fix: the second pass finds nothing left to do.
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["already_examined"], 1)
        self.assertEqual(second["documents_read"], 0)

    def test_the_marker_records_what_it_was_examined_against(self) -> None:
        held = self.claim(statement="Management sees stronger demand.", source=ON_TOPIC)
        self.driver().run_once()
        row = self.store.connection.execute(
            "SELECT * FROM claim_review_examinations WHERE claim_version_ref=?",
            (held["ref"],),
        ).fetchone()
        self.assertEqual(row["outcome"], "clear")
        self.assertEqual(row["detector_ref"], DETECTOR_SET_REF)
        self.assertEqual(row["source_content_hash"], held["source_sha256"])
        self.assertEqual(row["claim_version_hash"], held["hash"])

    def test_a_newly_committed_claim_is_examined_on_the_next_pass(self) -> None:
        self.claim(statement="Management sees stronger demand.", source=ON_TOPIC)
        driver = self.driver()
        driver.run_once()
        self.claim(statement="Management sees weaker demand.", source=ON_TOPIC)
        again = driver.run_once()
        self.assertEqual(again["scanned"], 1)
        self.assertEqual(again["already_examined"], 1)

    def test_a_misattributed_claim_is_still_challenged_and_retired(self) -> None:
        self.grant_claim_challenge()
        self.claim(statement="EPAM guided revenue lower.", source=OFF_TOPIC)
        summary = self.driver().run_once()
        self.assertEqual(len(summary["detected"]), 1)
        self.assertEqual(summary["detected"][0]["reason_code"], "subject_absent_from_source")
        self.assertEqual(summary["status"], "acted")
        self.assertEqual(len(summary["retired"]), 1)

    def test_an_unreadable_original_is_retried_slowly_rather_than_every_tick(self) -> None:
        # No citation chain at all: the detectors can say nothing about it.
        self.claim(statement="Management sees stronger demand.", source=None)
        driver = self.driver()
        first = driver.run_once()
        self.assertEqual(first["unreadable"], 1)
        second = driver.run_once(unreadable_retries=1)
        self.assertEqual(second["reexamined_unreadable"], 1)
        third = driver.run_once(unreadable_retries=0)
        # With no retry allowance it is deferred rather than re-read.
        self.assertEqual(third["scanned"], 0)
        self.assertEqual(third["deferred"], 1)

    def test_the_bound_is_a_rate_and_the_backlog_drains_across_passes(self) -> None:
        for index in range(5):
            self.claim(statement=f"Management sees demand {index}.", source=ON_TOPIC + str(index))
        driver = self.driver()
        examined = 0
        passes = 0
        for _ in range(6):
            summary = driver.run_once(max_documents=2)
            examined += summary["examined"]
            passes += 1
            if summary["examined"] == 0:
                break
        # Two originals a pass, five Claims: three working passes and a fourth
        # that finds nothing left.  Before the marker existed this loop would
        # have examined the same two for ever.
        self.assertEqual(examined, 5)
        self.assertLessEqual(passes, 4)

    def test_max_claims_is_accepted_as_the_name_the_lane_registry_advertises(self) -> None:
        # ``writer_server`` passes ``max_claims``; the signature only had
        # ``max_documents``, so any parameterised dispatch raised TypeError.
        self.claim(statement="Management sees stronger demand.", source=ON_TOPIC)
        summary = self.driver().run_once(max_claims=1)
        self.assertEqual(summary["scanned"], 1)


class NeedleFallbackTests(ClaimRetirementHarness):
    def test_the_mission_roster_supplies_needles_the_plan_does_not(self) -> None:
        # A subject the discovery plan does not key gets an empty needle list,
        # and an empty list makes ``subject_absent_from_source`` return False
        # by design -- so the misattribution detector could never fire for it.
        driver = ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=self.spool, needles={},
        )
        roster = driver._roster_needles()
        self.assertTrue(roster, "the mission universe should supply names")
        for company_ref, values in roster.items():
            self.assertTrue(values, company_ref)

    def test_a_planned_company_keeps_the_plan_names(self) -> None:
        driver = self.driver()
        roster = driver._roster_needles()
        self.assertEqual(driver._needles_for(EPAM, roster), ["epam"])
        self.assertEqual(driver._needles_for(ACN, roster), ["accenture", "acn"])

    def test_subject_needles_still_refuse_a_one_letter_name(self) -> None:
        self.assertEqual(subject_needles({"ticker": "X", "name": "Ab"}), ["ab"])


class ExaminationLedgerTests(ClaimRetirementHarness):
    def test_an_examination_can_never_be_deleted(self) -> None:
        self.claim(statement="Management sees stronger demand.", source=ON_TOPIC)
        self.driver().run_once()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM claim_review_examinations")

    def test_the_table_refuses_an_unauthorised_writer(self) -> None:
        from dalton_core.store import authorization_flag

        self.driver()  # installs the schema
        flag = authorization_flag(self.store.connection, "dalton_claim_review_authorized")
        flag.authorized = False
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO claim_review_examinations("
                "claim_version_ref,claim_version_hash,detector_ref,outcome,examined_at) "
                "VALUES('c','h','d','clear','t')")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
