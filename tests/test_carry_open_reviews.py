"""2026-09-27: a mission upgrade must not strand an open review.

ws-7d's policy-5 cascade published mission v5 over v4.  Extraction counts only
reviews under the pointed version, and the only automatic carry ran from the
AlphaEngine / web-search / SEC discovery coordinators, so the 47 reviews the
feed lanes and Guidepoint had opened stayed on v2/v4 with nothing open under
v5: the extraction child never started, and the claim-support recheck inside
it stopped with it.  ``carry_open_reviews_forward`` moves them for every
source, and the writer's extraction tick calls it before counting.
"""

from __future__ import annotations

from types import SimpleNamespace

from dalton_core.store import content_hash
from tests import test_mission_version_carry as _carry
from tests.test_mission_source_discovery import NEW_DOC

REF = _carry.REF


def _open_under_pointer(connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
        "WHERE r.state='awaiting_human_extraction'").fetchone()[0]


@_carry._own_tests_only
class CarryOpenReviewsTests(_carry._VersionHarness):

    def test_an_open_review_left_on_a_superseded_version_is_carried(self) -> None:
        old = self._review()
        # The cascade the signing scripts run: no discovery coordinator runs
        # afterwards for this source, which is the feed/Guidepoint case.
        v3 = self._publish(3, carry=False)
        connection = self.h.h.core.connection
        self.assertEqual(_open_under_pointer(connection), 0)
        [item] = self.m.carry_open_reviews_forward(REF)
        self.assertEqual(item["status"], "carried")
        self.assertEqual(item["mission_version_ref"], v3["id"])
        new = self._review(item["review_id"])
        # Reopened where it was in the queue, not at the end, and nothing read.
        self.assertEqual((new["state"], new["document_ref"], new["created_at"]),
                         ("awaiting_human_extraction", NEW_DOC, old["created_at"]))
        self.assertEqual(_open_under_pointer(connection), 1)
        # The old row is left as it was (append-only history) ...
        self.assertEqual(self._review(), old)
        # ... and a second pass has nothing to do.
        self.assertEqual(self.m.carry_open_reviews_forward(REF), [])

    def test_a_decided_review_is_not_reopened(self) -> None:
        self._decide()
        self._publish(3, carry=False)
        self.assertEqual(self.m.carry_open_reviews_forward(REF), [])
        self.assertEqual(_open_under_pointer(self.h.h.core.connection), 0)

    def test_a_document_the_new_version_already_holds_is_left_there(self) -> None:
        v3 = self._publish(3, carry=True)
        # Discovery's own carry already moved it; nothing is doubled.
        self.assertEqual(self.m.carry_open_reviews_forward(REF), [])
        self.assertEqual(len([r for r in self.m.document_reviews(v3["id"])
                              if r["document_ref"] == NEW_DOC]), 1)

    def test_a_source_the_new_version_does_not_connect_is_reported_not_moved(self) -> None:
        current = self.m.active_mission(REF)
        from tests.p9a_fixtures import mission_params

        params = mission_params(self.h.state)
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "not_connected"
        params.update({"version_id": "coverage-mission-version:us-it-services:3",
                       "prior_version_ref": current["id"],
                       "idempotency_key": "coverage-mission:us-it-services:3"})
        params.pop("mission_ref")
        self.m.create_mission(REF, **params)
        [item] = self.m.carry_open_reviews_forward(REF)
        self.assertEqual(item["status"], "skipped")
        self.assertIn("source", item["reason"])
        self.assertEqual(_open_under_pointer(self.h.h.core.connection), 0)

    def test_the_extraction_tick_carries_before_it_counts(self) -> None:
        self._publish(3, carry=False)
        seen = {}
        writer = self.h.writer

        def dispatch_once():
            seen["awaiting"] = _open_under_pointer(self.h.h.core.connection)
            return {"status": "launched", "awaiting": seen["awaiting"]}

        writer._document_extraction_coordinator = SimpleNamespace(dispatch_once=dispatch_once)
        try:
            result = writer._op_dispatch_document_extraction({})
        finally:
            writer._document_extraction_coordinator = None
        self.assertEqual(seen["awaiting"], 1)
        self.assertEqual(result["carried_open_reviews"], {"carried": 1})
        # Idle ticks say nothing extra.
        writer._document_extraction_coordinator = SimpleNamespace(
            dispatch_once=lambda: {"status": "idle", "awaiting": 1})
        try:
            self.assertNotIn("carried_open_reviews",
                             writer._op_dispatch_document_extraction({}))
        finally:
            writer._document_extraction_coordinator = None


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
