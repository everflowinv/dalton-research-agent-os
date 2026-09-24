"""2026-09-24b: P13i dismissals made under an older subject rule are re-asked.

A P13i dismissal ("document never names this company") is a fact about the
document's bytes *and* the name table it was checked against.  ws-7d holds
~240 of them decided against a table with no row for the company; they are
re-checked once per rule, a few per tick, and reopened when the same bytes now
name the company.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from dalton_core.document_extraction import (
    SUBJECT_RULE_REF,
    UNATTRIBUTED_REASON,
    DocumentExtractionService,
)
from dalton_core.document_extraction_cli import _reevaluate_unattributed
from dalton_core.document_extraction_windows import ExtractionWindowLedger
from dalton_core.store import content_hash
# Through the module, so this file does not collect the harness's tests again.
from tests import test_document_extraction_automation as _automation


def _own_tests_only(cls):
    """Run only this class's tests, not the drafting harness's it inherits."""

    for name in dir(_automation.AutomationDraftingTests):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@_own_tests_only
class ReevaluationTests(_automation.AutomationDraftingTests):
    """Old P13i dismissals are re-asked once per rule, paced, and reopened."""

    def setUp(self) -> None:
        super().setUp()
        self._grant_automation()
        self.mission = self.h.missions.active_mission("coverage-mission:us-it-services")
        self.actor = self.mission["autonomy"]["automation_principal"]
        review = next(r for r in self.h.missions.document_reviews(self.mission["id"])
                      if r["state"] == "awaiting_human_extraction")
        # The deployed rule's dismissal: no rule tag in the rationale.
        self.h.missions.resolve_document_review(
            review["review_id"], resolution="dismissed", actor_ref=self.actor,
            rationale="P13i: document never names this company; it is not about it",
            expected_review_hash=content_hash(review))
        self.review_id = review["review_id"]
        self.service = DocumentExtractionService(self.h.writer)
        self.windows = ExtractionWindowLedger(self.h.h.core.connection)

    def _host(self):
        host = type("H", (), {})()
        host.store = self.h.h.core
        host.coverage_mission = self.h.missions
        return host

    def _reevaluate(self, **caps):
        return _reevaluate_unattributed(self._host(), self.service, self.windows,
                                        self.mission, self.actor, **caps)

    def test_an_old_dismissal_that_now_names_the_company_is_reopened_once(self) -> None:
        result = self._reevaluate()
        self.assertEqual([item["review_id"] for item in result["reopened"]], [self.review_id], result)
        review = self.h.missions.document_review(self.review_id)
        self.assertEqual(review["state"], "awaiting_human_extraction")
        [row] = self.h.h.core.connection.execute(
            "SELECT record_json FROM coverage_mission_document_review_reopens").fetchall()
        record = json.loads(row["record_json"])
        self.assertEqual((record["basis"], record["decision_ref"], record["failed_windows"]),
                         ("subject_rule_reevaluation", SUBJECT_RULE_REF, []))
        self.assertTrue(record["prior_review"]["rationale"].startswith("P13i:"))
        self.assertEqual(self._reevaluate()["reopened"], [])

    def test_a_dismissal_decided_under_the_current_rule_is_final(self) -> None:
        from dalton_core.coverage_mission import CoverageMissionConflict

        self._reevaluate()
        reopened = self.h.missions.document_review(self.review_id)
        self.h.missions.resolve_document_review(
            self.review_id, resolution="dismissed", actor_ref=self.actor,
            rationale=f"P13i: {UNATTRIBUTED_REASON}", expected_review_hash=content_hash(reopened))
        again = self._reevaluate()
        self.assertEqual((again["checked"], again["reopened"]), (0, []), again)
        review = self.h.missions.document_review(self.review_id)
        with self.assertRaises(CoverageMissionConflict):
            self.h.missions.reopen_unattributed_document_review(
                self.review_id, expected_review_hash=content_hash(review), actor_ref=self.actor,
                rule_ref=SUBJECT_RULE_REF, matched=["Accenture"])
        self.assertEqual(self.h.missions.document_review(self.review_id)["state"], "dismissed")

    def test_only_the_missions_principal_or_a_person_may_reopen(self) -> None:
        from dalton_core.coverage_mission import CoverageMissionValidationError

        review = self.h.missions.document_review(self.review_id)
        with self.assertRaises(CoverageMissionValidationError):
            self.h.missions.reopen_unattributed_document_review(
                self.review_id, expected_review_hash=content_hash(review),
                actor_ref="automation:someone-else", rule_ref=SUBJECT_RULE_REF,
                matched=["Accenture"])
        with self.assertRaises(CoverageMissionValidationError):
            self.h.missions.reopen_unattributed_document_review(
                self.review_id, expected_review_hash=content_hash(review),
                actor_ref=self.actor, rule_ref=SUBJECT_RULE_REF, matched=[])

    def test_the_daily_allowance_is_respected(self) -> None:
        result = self._reevaluate(per_day=0)
        self.assertEqual(result["reopened"], [])
        self.assertEqual(result["stop_reason"], "daily reopen allowance spent")
        self.assertEqual(result["remaining"], 1)

    def test_a_document_still_unnamed_is_written_down_and_not_reread(self) -> None:
        with patch.object(DocumentExtractionService, "document_names_subject",
                          return_value={"checked": True, "names_subject": False, "matched": []}):
            first = self._reevaluate()
            second = self._reevaluate()
        self.assertEqual((first["checked"], first["still_unattributed"]), (1, 1))
        self.assertEqual((second["checked"], second["remaining"]), (0, 0))
        self.assertEqual(self.h.missions.document_review(self.review_id)["state"], "dismissed")


if __name__ == "__main__":
    unittest.main()
