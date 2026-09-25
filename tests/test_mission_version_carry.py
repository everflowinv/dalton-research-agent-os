"""2026-09-25b: a mission version bump must not change what a document costs.

P1 (signing a rule in) and P2 (switching the weekly brief) publish a new
version of the mission over the old one -- ws-7d went from v3 to v4.  Three
things only looked at the version in force and broke on the bump:

* a search under v4 re-registered documents v3 had already read and decided,
  and each was read (and paid for) again;
* the owner's supplemental-read reopen refused any review not under v4;
* the figures / metric-discovery passes read closed documents out of v4 alone,
  so everything v3 had closed fell out of both.

Each test here also pins the invariant: the same actions give the same
outcome with or without a version bump in between.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionConflict
from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.document_extraction_cli import _secondary_sweep
from dalton_core.document_read_completion import review_wire
from dalton_core.document_review_reopen import FailedDocumentWindowReader, review_reopen_candidate
from dalton_core.store import content_hash
from tests.p9a_fixtures import mission_params
from tests.test_mission_source_discovery import (
    ACN, NEW_DOC, build_discovery_parameters, plan_for_tests, search_spec_hash,
)
# Through the module, so this file does not collect the harness's tests again.
from tests import test_document_extraction_automation as _automation
from tests import test_s1_human_feeds as _feeds

REF = "coverage-mission:us-it-services"
SPEC = "earnings-call-transcripts"
FAILED = {"work_order_ref": "work:old", "work_order_hash": "8" * 64,
          "result_envelope_ref": "result:old", "result_envelope_hash": "9" * 64,
          "error_code": "MODEL_CHAIN_EXHAUSTED"}


class _FormalReader:
    def read_failed_window(self, **_kwargs):
        return dict(FAILED)


def _own_tests_only(cls):
    """Run only this class's tests, not the drafting harness's it inherits."""

    for name in dir(_automation.AutomationDraftingTests):
        if name.startswith("test") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@_own_tests_only
class _VersionHarness(_automation.AutomationDraftingTests):
    """v2 holds NEW_DOC with an open review; helpers decide it and bump."""

    def setUp(self) -> None:
        super().setUp()
        self.v2 = self._grant_automation()
        self.m = self.h.missions
        self.actor = self.v2["autonomy"]["automation_principal"]
        self.review_id = next(r["review_id"] for r in self.m.document_reviews(self.v2["id"])
                              if r["state"] == "awaiting_human_extraction")
        self.service = DocumentExtractionService(self.h.writer)

    def _review(self, review_id=None):
        return self.m.document_review(review_id or self.review_id)

    def _decide(self, resolution="dismissed", rationale="ADR-0005: no admissible statement"):
        review = self._review()
        kwargs = ({"rationale": rationale} if resolution == "dismissed" else
                  {"candidate_claim_version_ref": "candidate-claim-version:fixture:1"})
        self.m.resolve_document_review(self.review_id, resolution=resolution, actor_ref=self.actor,
                                       expected_review_hash=content_hash(review), **kwargs)
        return self._review()

    def _publish(self, number: int, *, carry: bool = True) -> dict:
        """What P1/P2 do: a new version over the one in force."""

        current = self.m.active_mission(REF)
        params = mission_params(self.h.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({"version_id": f"coverage-mission-version:us-it-services:{number}",
                       "prior_version_ref": current["id"],
                       "idempotency_key": f"coverage-mission:us-it-services:{number}"})
        params.pop("mission_ref")
        version = self.m.create_mission(REF, **params)
        if carry:
            self.m.carry_forward_superseded_documents(REF)
            self.m.backfill_document_reviews(REF)
        return version

    def _rediscover(self) -> dict:
        """The search under the version in force finds NEW_DOC again (bytes held)."""

        version = self.m.active_mission(REF)
        plan = plan_for_tests()
        query = build_discovery_parameters(plan, spec_ref=SPEC, company_ref=ACN,
                                           as_of=self.h.h.clock().date())
        receipt = self.h.h.search.search(self.h.h.search.build_request(query))
        grant = self.m.authorize_source_discovery(
            company_ref=ACN, source_ref="source:alphaengine", requested_by=self.actor,
            mission_version_ref=version["id"])
        return self.m.record_source_discovery(
            authorization=grant, discovery_plan_ref=plan["id"],
            discovery_plan_hash=plan["content_hash"], spec_ref=SPEC,
            query_hash=search_spec_hash(query), parameters=query,
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
            document_refs=[NEW_DOC], in_authority_document_refs=[NEW_DOC])

    def _settle_like_a_tick(self) -> None:
        """What the discovery tick does next: settle held rows, open reviews."""

        for row in self.m.already_held_documents():
            self.m.settle_document_already_held(row["record_id"])
            self.m.register_document_review(row["record_id"], requested_by=self.actor)
        self.m.backfill_document_reviews(REF)

    def _owed_reads(self) -> list[tuple[str, str]]:
        """(version, document) for every open review the queue would read."""

        return sorted(
            (row["mission_version_ref"], row["document_ref"])
            for row in self.h.h.core.connection.execute(
                "SELECT r.mission_version_ref, r.document_ref FROM coverage_mission_document_reviews r "
                "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
                "WHERE r.state='awaiting_human_extraction'").fetchall())

    def _reopen(self, review_id=None):
        review = self._review(review_id)
        return self.m.reopen_document_review(
            review["review_id"], expected_review_hash=content_hash(review), actor_ref="human:owner",
            decision_ref="owner-decision:supplemental-read:1", failed_windows=[dict(FAILED)],
            formal_reader=_FormalReader())


@_own_tests_only
class RediscoveryTests(_VersionHarness):
    """Item 1: a re-discovered, already decided document is carried, not re-read."""

    def test_a_decision_made_under_the_old_version_is_carried_not_reread(self) -> None:
        old = self._decide()
        v3 = self._publish(3)
        # The ordinary carry-forward leaves a finished document behind.
        self.assertEqual(self.m.discovered_documents(v3["id"]), [])
        result = self._rediscover()
        [carried] = result["carried_decided"]
        self.assertEqual((carried["document_ref"], carried["from_version_ref"], carried["from_review_id"],
                          carried["state"], carried["mission_version_ref"]),
                         (NEW_DOC, self.v2["id"], self.review_id, "dismissed", v3["id"]))
        new = self._review(carried["review_id"])
        # The same decision, held by v3: nothing to read.
        for key in ("state", "rationale", "candidate_claim_version_ref", "created_at", "updated_at",
                    "company_ref", "source_ref", "document_ref"):
            self.assertEqual(new[key], old[key], key)
        [row] = self.m.discovered_documents(v3["id"])
        old_row = self.m.discovered_documents(self.v2["id"])[0]
        self.assertEqual((row["status"], row["ticket_ref"], row["discovery_ref"]),
                         ("acquired", old_row["ticket_ref"], old_row["discovery_ref"]))
        self._settle_like_a_tick()
        self.assertEqual(self._owed_reads(), [])
        # Idempotent: the document is v3's now; carry-forward has nothing owed.
        self.assertEqual(self.m.carry_forward_superseded_documents(REF), [])
        self.assertNotIn("carried_decided", self._rediscover())
        self.assertEqual(self._owed_reads(), [])

    def test_the_same_search_costs_the_same_with_or_without_a_version_bump(self) -> None:
        self._decide()
        # Same version: the second search finds a document the version holds.
        self._rediscover()
        self._settle_like_a_tick()
        before = self._owed_reads()
        # Bumped: the same search must still owe nothing.
        self._publish(3)
        self._rediscover()
        self._settle_like_a_tick()
        self.assertEqual((before, self._owed_reads()), ([], []))

    def test_a_staged_decision_keeps_its_candidate(self) -> None:
        self._decide("extraction_staged")
        self._publish(3)
        [carried] = self._rediscover()["carried_decided"]
        new = self._review(carried["review_id"])
        self.assertEqual((new["state"], new["candidate_claim_version_ref"]),
                         ("extraction_staged", "candidate-claim-version:fixture:1"))

    def test_only_the_newest_row_of_a_document_speaks_for_it(self) -> None:
        # v2 dismissed it; v3 re-opened it (the P13i re-evaluation carried it)
        # and has not finished it; v4 is published before carry-forward runs.
        from dalton_core.document_extraction import SUBJECT_RULE_REF

        old = self._decide(rationale="P13i: document never names this company; it is not about it")
        self._publish(3)
        reopened = self.m.reopen_unattributed_document_review(
            self.review_id, expected_review_hash=content_hash(old), actor_ref=self.actor,
            rule_ref=SUBJECT_RULE_REF, matched=["Accenture"])
        v4 = self._publish(4, carry=False)
        result = self._rediscover()
        # v2's dismissal is not the newest word on the document: no carry of
        # it, the document registers for v4 like any unfinished one.
        self.assertNotIn("carried_decided", result)
        [row] = self.m.discovered_documents(v4["id"])
        self.assertEqual(row["status"], "already_in_authority")
        self.assertEqual(self._review(reopened["carried_to"]["review_id"])["state"],
                         "awaiting_human_extraction")

    def test_an_open_review_is_not_a_decision(self) -> None:
        v3 = self._publish(3)
        # Carry-forward already moved the unfinished document; nothing to carry.
        self.assertNotIn("carried_decided", self._rediscover())
        self.assertEqual([r["state"] for r in self.m.document_reviews(v3["id"])],
                         ["awaiting_human_extraction"])


@_own_tests_only
class HumanReopenTests(_VersionHarness):
    """Item 2: the owner's supplemental reopen works on any version of the mission."""

    def test_same_version_reopen_is_unchanged(self) -> None:
        self._decide()
        record = self._reopen()
        self.assertNotIn("carried_to", record)
        self.assertEqual(self._owed_reads(), [(self.v2["id"], NEW_DOC)])

    def test_a_review_on_a_superseded_version_is_carried_then_reopened(self) -> None:
        old = self._decide()
        v3 = self._publish(3)
        record = self._reopen()
        carried = record["carried_to"]
        self.assertEqual(carried["mission_version_ref"], v3["id"])
        # The old decision stands where it was made; the append-only record
        # names the review that was reopened in its place.
        self.assertEqual(self._review(), old)
        self.assertEqual(record["prior_review"], old)
        new = self._review(carried["review_id"])
        self.assertEqual((new["state"], new["document_ref"], new["created_at"]),
                         ("awaiting_human_extraction", NEW_DOC, old["created_at"]))
        # Invariant: one open review of the document under the version in
        # force -- what the same reopen gives without the bump.
        self.assertEqual(self._owed_reads(), [(v3["id"], NEW_DOC)])
        [saved] = self.h.h.core.connection.execute(
            "SELECT record_json FROM coverage_mission_document_review_reopens").fetchall()
        self.assertEqual(json.loads(saved["record_json"])["carried_to"], carried)
        # Replay is a duplicate while the carried review is still open.
        self.assertEqual(self._reopen()["status"], "duplicate")
        self.assertEqual(self._owed_reads(), [(v3["id"], NEW_DOC)])

    def test_a_carried_decision_is_the_one_reopened(self) -> None:
        self._decide()
        v3 = self._publish(3)
        [carried] = self._rediscover()["carried_decided"]
        record = self._reopen()
        self.assertEqual(record["carried_to"]["review_id"], carried["review_id"])
        self.assertEqual(self._review(carried["review_id"])["state"], "awaiting_human_extraction")
        self.assertEqual(self._review()["state"], "dismissed")
        self.assertEqual(self._owed_reads(), [(v3["id"], NEW_DOC)])

    def test_the_newest_holder_of_the_document_is_the_one_carried(self) -> None:
        # v2 dismissed it; a search under v3 carried the dismissal; v4 is in
        # force and does not hold it.  The v3 copy is what moves to v4.
        self._decide()
        self._publish(3)
        [copy] = self._rediscover()["carried_decided"]
        v4 = self._publish(4)
        record = self._reopen()
        self.assertEqual(record["carried_to"]["mission_version_ref"], v4["id"])
        self.assertEqual(self._review()["state"], "dismissed")
        self.assertEqual(self._review(copy["review_id"])["state"], "dismissed")
        self.assertEqual(self._owed_reads(), [(v4["id"], NEW_DOC)])

    def test_a_document_decided_differently_under_the_current_version_is_refused(self) -> None:
        self._decide()
        self._publish(3)
        [carried] = self._rediscover()["carried_decided"]
        self._reopen(carried["review_id"])  # reopened where it is held now
        with self.assertRaisesRegex(CoverageMissionConflict, "decided there"):
            self._reopen()

    def test_the_read_only_candidate_review_accepts_a_superseded_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, scheduler = Path(tmp, "core.sqlite"), Path(tmp, "scheduler.sqlite")
            sqlite3.connect(scheduler).close()
            db = sqlite3.connect(core)
            db.executescript(
                "CREATE TABLE coverage_mission_versions(mission_version_id TEXT, mission_ref TEXT, version_number INT);"
                "CREATE TABLE coverage_mission_pointer(mission_ref TEXT, mission_version_id TEXT);"
                "CREATE TABLE coverage_mission_discovered_documents(mission_version_ref TEXT, document_ref TEXT);"
                "CREATE TABLE coverage_mission_statement_filings(company_ref TEXT, accession TEXT, form TEXT);"
                "CREATE TABLE coverage_mission_document_reviews(review_id TEXT, mission_version_ref TEXT,"
                " company_ref TEXT, source_ref TEXT, document_ref TEXT, discovered_document_ref TEXT, state TEXT,"
                " candidate_claim_version_ref TEXT, rationale TEXT, registered_by TEXT, created_at TEXT,"
                " updated_at TEXT);"
                "INSERT INTO coverage_mission_versions VALUES('m:3','m',3),('m:4','m',4);"
                "INSERT INTO coverage_mission_pointer VALUES('m','m:4');"
                "INSERT INTO coverage_mission_statement_filings VALUES('c','0001','10-K');"
                "INSERT INTO coverage_mission_document_reviews VALUES('r:3','m:3','c','source:sec-edgar',"
                " 'sec:filing:0001','d:3','dismissed',NULL,'why','a','t0','t1');")
            db.commit()
            db.row_factory = sqlite3.Row
            prior = review_wire(db.execute("SELECT * FROM coverage_mission_document_reviews").fetchone())
            candidate = {"schema_version": "0.1", "action": "authorize_supplemental_document_reads",
                         "signed_decision": None, "human_signature_required": True,
                         "items": [{"review_id": "r:3", "prior_review_hash": content_hash(prior),
                                    "failed_windows": [dict(FAILED)]}]}
            candidate["candidate_hash"] = content_hash(candidate)

            def check():
                with patch.object(FailedDocumentWindowReader, "read_failed_window",
                                  return_value=dict(FAILED)):
                    return review_reopen_candidate(core_db=core, scheduler_db=scheduler,
                                                   candidate=candidate)

            # v3 review, v4 in force, v4 does not hold the filing: ready.
            self.assertEqual(check()["ready_review_ids"], ["r:3"])
            # v4 holds it under the same dismissal (carried): still ready.
            db.execute("INSERT INTO coverage_mission_discovered_documents VALUES('m:4','sec:filing:0001')")
            db.execute("INSERT INTO coverage_mission_document_reviews VALUES('r:4','m:4','c','source:sec-edgar',"
                       "'sec:filing:0001','d:4','dismissed',NULL,'why','a','t0','t1')")
            db.commit()
            self.assertEqual(check()["ready_review_ids"], ["r:3"])
            # v4 decided it differently: refused, as the writer would.
            db.execute("UPDATE coverage_mission_document_reviews SET state='awaiting_human_extraction',"
                       "rationale=NULL WHERE review_id='r:4'")
            db.commit()
            with self.assertRaisesRegex(ValueError, "decided there"):
                check()
            # A mission no longer pointed at is still refused.
            db.execute("DELETE FROM coverage_mission_pointer")
            db.commit()
            with self.assertRaisesRegex(ValueError, "not current"):
                check()
            db.close()


class FeedLaneBumpTests(_feeds.FeedEndToEndHarness):
    """Item 1 through the lane that did it live: ws-7d's 32 re-registrations
    were all sales notes and company-wiki documents."""

    def _owed(self):
        return sorted(row["document_ref"] for row in self.core.connection.execute(
            "SELECT r.document_ref FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'").fetchall())

    def _decide_all(self):
        actor = self.mission["autonomy"]["automation_principal"]
        for review in self.missions.document_reviews(self.mission["id"], limit=100):
            self.missions.resolve_document_review(
                review["review_id"], resolution="dismissed", actor_ref=actor,
                rationale="ADR-0005: no admissible statement",
                expected_review_hash=content_hash(review))

    def test_decided_notes_found_again_after_a_bump_are_not_reread(self) -> None:
        coordinator = self.coordinator()
        coordinator.dispatch_once(universe=_feeds.UNIVERSE, since="2026-08-01")
        self._decide_all()
        # Same version: the next tick holds all four and owes nothing.
        again = coordinator.dispatch_once(universe=_feeds.UNIVERSE, since="2026-08-01")
        self.assertEqual(again["read"]["already_held"], 4)
        self.assertEqual(self._owed(), [])
        # P1/P2: a new version over the one that decided them.
        params = _feeds.mission_params(self.bootstrap)
        params["source_plan"] = list(params["source_plan"]) + [{
            "source_ref": _feeds.SALES_NOTES, "role": "named sell-side notes on this machine",
            "status": "connected"}]
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        params.update({"version_id": self.mission["id"].rsplit(":", 1)[0] + ":2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:feed-bump:2"})
        ref = params.pop("mission_ref")
        v2 = self.missions.create_mission(ref, **params)
        self.assertEqual(self.missions.carry_forward_superseded_documents(ref), [])
        bumped = self.coordinator().dispatch_once(universe=_feeds.UNIVERSE, since="2026-08-01")
        self.assertEqual(bumped["status"], "dispatched")
        carried = [item for outcome in bumped["read"]["outcomes"]
                   for item in outcome.get("carried_decided", ())]
        self.assertEqual(len(carried), 4, bumped["read"])
        # The invariant: exactly what the same tick owed without the bump.
        self.assertEqual(self._owed(), [])
        self.assertEqual({r["state"] for r in self.missions.document_reviews(v2["id"], limit=100)},
                         {"dismissed"})


if __name__ == "__main__":
    unittest.main()
