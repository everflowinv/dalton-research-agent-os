"""2026-09-27b: maintenance runs when the extraction queue is empty, P13i included.

The P13i subject re-check (``_reevaluate_unattributed``) ran only inside the
drafting run's mission loop.  93ccbcb8 gave the empty queue a support-only
child for the claim-support recheck and backfill, but the re-check was not in
it: a workspace whose queue drained -- every open review dismissed as "never
names this company" under an older name table is exactly such a queue -- never
re-asked those dismissals again.  It now runs in the support-only child with
the same per-run allowances, and a run that checked something is paced like a
busy queue (a tick later).
"""

from __future__ import annotations

from unittest.mock import patch

from dalton_core import document_extraction_cli
from dalton_core.document_extraction_cli import run_extraction
from dalton_core.document_extraction_launcher import support_progress
from dalton_core.store import content_hash
from tests import test_document_extraction_automation as _automation


def _own_tests_only(cls):
    for name in dir(_automation.AutomationDraftingTests):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@_own_tests_only
class SupportOnlyReevaluationTests(_automation.AutomationDraftingTests):
    def setUp(self) -> None:
        super().setUp()
        self._grant_automation()
        mission = self.h.missions.active_mission("coverage-mission:us-it-services")
        actor = mission["autonomy"]["automation_principal"]
        review = next(r for r in self.h.missions.document_reviews(mission["id"])
                      if r["state"] == "awaiting_human_extraction")
        # Dismissed under the deployed (untagged) rule: the queue is now empty.
        self.h.missions.resolve_document_review(
            review["review_id"], resolution="dismissed", actor_ref=actor,
            rationale="P13i: document never names this company; it is not about it",
            expected_review_hash=content_hash(review))
        self.review_id = review["review_id"]

    def _support_only_run(self):
        fixture = self.root / "fixture.json"
        fixture.write_text('{"schema_version":"0.1","suggestions":[]}', encoding="utf-8")
        base = document_extraction_cli.ExtractionHost

        class Host(base):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                inner._claim_support_verifier = object()

        with patch.object(document_extraction_cli, "ExtractionHost", Host), \
                patch.object(document_extraction_cli, "_run_claim_support_recheck",
                             lambda host, db: {"status": "idle"}), \
                patch.object(document_extraction_cli, "_secondary_sweep",
                             side_effect=AssertionError("no secondary pass")):
            return run_extraction(
                state_dir=self.root, model_config_path=self._model_config(),
                summary_dir=self.root / "support-only", spool_dir=self.root / "spool",
                scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
                max_windows=1, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite",
                support_only=True)

    def test_an_empty_queue_still_re_asks_old_dismissals(self) -> None:
        awaiting = self.h.h.core.connection.execute(
            "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'").fetchone()[0]
        self.assertEqual(awaiting, 0)
        summary = self._support_only_run()
        self.assertEqual((summary["stop_reason"], summary["reviews_scanned"], summary["drafted"]),
                         ("support_only", 0, []))
        [reevaluation] = summary["subject_reevaluation"]
        self.assertEqual([item["review_id"] for item in reevaluation["reopened"]],
                         [self.review_id], reevaluation)
        self.assertEqual(self.h.missions.document_review(self.review_id)["state"],
                         "awaiting_human_extraction")
        # Paced like a busy queue: the next support-only run is a tick away.
        self.assertTrue(support_progress(summary))

    def test_a_run_with_nothing_to_re_ask_rests(self) -> None:
        first = self._support_only_run()
        self.assertTrue(first["subject_reevaluation"][0]["reopened"])
        # The reopened review is decided again under the current rule.
        mission = self.h.missions.active_mission("coverage-mission:us-it-services")
        review = self.h.missions.document_review(self.review_id)
        from dalton_core.document_extraction import UNATTRIBUTED_REASON
        self.h.missions.resolve_document_review(
            self.review_id, resolution="dismissed",
            actor_ref=mission["autonomy"]["automation_principal"],
            rationale=f"P13i: {UNATTRIBUTED_REASON}", expected_review_hash=content_hash(review))
        second = self._support_only_run()
        self.assertEqual((second["subject_reevaluation"][0]["checked"],
                          second["subject_reevaluation"][0]["reopened"]), (0, []))
        self.assertFalse(support_progress(second))


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
