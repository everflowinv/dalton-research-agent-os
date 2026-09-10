"""P14a: the event ledger -- typed payloads, one event per fact, no repairs."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from dalton_core.research_event import (
    EVENT_KINDS,
    EVIDENCE_TIERS,
    PAYLOAD_FIELDS,
    ResearchEventAuthority,
    ResearchEventConflict,
    ResearchEventValidationError,
    claim_event_candidates,
    classify_document,
    document_event_candidates,
    day_start,
    event_ref_for,
    payload_hash,
    record_event,
    rfc3339,
    validate_payload,
    worst_tier,
)
from dalton_core.store import canonical_json
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, OWNER, P14aHarness

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def news_payload(document="alphaengine-doc:1"):
    return {
        "document_ref": document, "source_ref": "source:alphaengine",
        "spec_ref": "sell-side-reports", "discovery_ref": "discovery:1",
        "title": None, "host": None,
    }


class VocabularyTests(unittest.TestCase):
    def test_every_kind_declares_a_payload_shape(self):
        self.assertEqual(set(PAYLOAD_FIELDS), set(EVENT_KINDS))

    def test_the_owner_second_instruction_kinds_are_present(self):
        # A sales note, a crowd post and an expert excerpt say different things
        # about the market's view than a sell-side report does; collapsing them
        # into news would throw away what they are read for.
        for kind in ("sales_note", "crowd_post", "expert_excerpt"):
            self.assertIn(kind, EVENT_KINDS)

    def test_evidence_tiers_are_ordered_best_first(self):
        self.assertEqual(EVIDENCE_TIERS[0], "primary_filing")
        self.assertEqual(EVIDENCE_TIERS[-1], "crowd")

    def test_the_weakest_tier_in_a_batch_is_how_it_should_be_believed(self):
        self.assertEqual(worst_tier(["primary_filing", "crowd"]), "crowd")
        self.assertEqual(worst_tier([]), "news_media")


class PayloadTests(unittest.TestCase):
    def test_an_undeclared_field_is_refused_not_dropped(self):
        with self.assertRaises(ResearchEventValidationError) as caught:
            validate_payload("news", {**news_payload(), "sentiment": "bullish"})
        self.assertIn("sentiment", str(caught.exception))

    def test_absent_fields_become_null_so_the_hash_is_stable(self):
        one = validate_payload("news", news_payload())
        two = validate_payload("news", {k: v for k, v in news_payload().items() if v is not None})
        self.assertEqual(payload_hash(one), payload_hash(two))

    def test_a_payload_that_says_nothing_is_not_an_event(self):
        with self.assertRaises(ResearchEventValidationError):
            validate_payload("news", {})

    def test_nested_values_are_refused(self):
        with self.assertRaises(ResearchEventValidationError):
            validate_payload("news", {**news_payload(), "title": {"text": "x"}})

    def test_a_naive_timestamp_is_refused_rather_than_assumed_utc(self):
        with self.assertRaises(ResearchEventValidationError):
            rfc3339("2026-09-09T12:00:00")
        self.assertTrue(rfc3339("2026-09-09T12:00:00Z").endswith("+00:00"))

    def test_a_date_becomes_a_moment(self):
        self.assertEqual(day_start("2026-09-09"), "2026-09-09T00:00:00+00:00")


class ClassificationTests(unittest.TestCase):
    def test_the_spec_decides_before_the_source(self):
        # The same AlphaEngine library holds sell-side reports and call minutes.
        self.assertEqual(
            classify_document("earnings-call-transcripts", "source:alphaengine"),
            ("transcript", "management_direct"),
        )
        self.assertEqual(
            classify_document("sell-side-reports", "source:alphaengine"),
            ("news", "sell_side"),
        )

    def test_the_owner_named_sources_classify_to_their_own_kinds(self):
        self.assertEqual(classify_document(None, "source:sales-notes")[0], "sales_note")
        self.assertEqual(classify_document(None, "source:x")[0], "crowd_post")
        self.assertEqual(classify_document(None, "source:guidepoint")[0], "expert_excerpt")

    def test_an_unrecognised_source_is_not_promoted_by_being_unrecognised(self):
        self.assertEqual(classify_document("who-knows", "source:mystery"),
                         ("news", "news_media"))


class LedgerTests(P14aHarness):
    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)

    def record(self, *, company=ACN, kind="news", payload=None, occurred="2026-09-09T10:00:00+00:00",
               actor=AUTOMATION, tier=None, mission=None):
        return record_event(
            self.events, company_ref=company, kind=kind, occurred_at=occurred,
            source_refs=["source:alphaengine", "alphaengine-doc:1"],
            payload=payload or news_payload(), evidence_tier=tier,
            mission=mission or self.mission, actor_ref=actor,
        )

    def test_a_recorded_event_reads_back_and_is_hash_bound(self):
        written = self.record()
        self.assertEqual(written["status"], "fresh")
        read = self.events.event(written["id"])
        self.assertEqual(read["content_hash"], written["content_hash"])
        self.assertEqual(read["kind"], "news")
        self.assertEqual(read["evidence_tier"], "news_media")

    def test_the_same_fact_twice_is_one_event(self):
        first = self.record()
        second = self.record(occurred="2026-09-10T10:00:00+00:00")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.events.events(company_ref=ACN)), 1)

    def test_a_different_payload_is_a_different_event(self):
        self.record()
        second = self.record(payload=news_payload("alphaengine-doc:2"))
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(len(self.events.events(company_ref=ACN)), 2)

    def test_the_id_is_a_function_of_what_the_event_says(self):
        written = self.record()
        self.assertEqual(
            written["id"],
            event_ref_for(ACN, "news", payload_hash(validate_payload("news", news_payload()))),
        )

    def test_an_event_with_no_source_ref_is_refused(self):
        with self.assertRaises(ResearchEventValidationError):
            self.events.record(
                company_ref=ACN, kind="news", occurred_at="2026-09-09T10:00:00+00:00",
                source_refs=[], payload=news_payload(), evidence_tier=None,
                mission=self.mission, actor_ref=AUTOMATION,
            )

    def test_a_mission_without_the_word_cannot_write_events(self):
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = [w for w in autonomy["may_write"] if w != "market_event"]
        params["autonomy"] = autonomy
        params.update({"version_id": "coverage-mission-version:us-it-services:9",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:9"})
        ungranted = self.missions.create_mission(self.mission_ref, **params)
        with self.assertRaises(ResearchEventConflict) as caught:
            self.record(mission=ungranted)
        self.assertIn("market_event", str(caught.exception))

    def test_a_company_outside_the_universe_is_refused(self):
        with self.assertRaises(ResearchEventConflict):
            self.record(company="company:sec-cik:0000000001")

    def test_a_human_may_record_an_event_without_the_mission_grant(self):
        written = self.record(actor=OWNER)
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(written["actor_ref"], OWNER)

    def test_events_are_immutable_in_the_store(self):
        written = self.record()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction() as cur:
                cur.execute("UPDATE research_events SET kind='filing' WHERE event_id=?",
                            (written["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction() as cur:
                cur.execute("DELETE FROM research_events WHERE event_id=?", (written["id"],))

    def test_a_tampered_record_is_a_conflict_not_a_read(self):
        written = self.record()
        forged = dict(written)
        forged["kind"] = "filing"
        with self.missions._transaction() as cur:
            cur.execute("PRAGMA writable_schema=OFF")
        # Rewriting the row is impossible through the triggers, so the drift is
        # simulated by inserting a second row whose json disagrees with its
        # columns; the reader must refuse it.
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO research_events(event_id,company_ref,kind,occurred_at,"
                "evidence_tier,payload_hash,mission_version_ref,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("research-event:forged", ACN, "filing", written["occurred_at"],
                 written["evidence_tier"], "0" * 64, self.mission["id"],
                 canonical_json(written), written["content_hash"], AUTOMATION,
                 written["created_at"]),
            )
        with self.assertRaises(ResearchEventConflict):
            self.events.event("research-event:forged")

    def test_counts_are_per_kind_and_per_company(self):
        self.record()
        self.record(kind="filing", payload={
            "document_ref": "sec:filing:1", "source_ref": "source:sec-edgar",
            "spec_ref": "annual-report-10k", "discovery_ref": "d", "title": None, "host": None,
        })
        self.record(company=CTSH)
        self.assertEqual(self.events.counts(ACN), {"news": 1, "filing": 1})
        self.assertEqual(self.events.counts(CTSH), {"news": 1})

    def test_latest_occurred_at_bounds_an_emitter_scan(self):
        self.assertIsNone(self.events.latest_occurred_at(ACN, "news"))
        self.record()
        self.assertEqual(
            self.events.latest_occurred_at(ACN, "news")[:10], "2026-09-09"
        )


class ClaimEmitterTests(P14aHarness):
    def test_a_new_claim_becomes_an_event_and_a_retired_one_does_not(self):
        live = self.claim(statement="ACN 说需求在改善。")
        dead = self.claim(statement="这条已经被退役。")
        with self.store._transaction() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS claim_retirement_decisions ("
                "decision_id TEXT PRIMARY KEY, claim_version_ref TEXT, decision TEXT)"
            )
            cur.execute(
                "INSERT INTO claim_retirement_decisions VALUES(?,?,?)",
                ("d1", dead, "retired"),
            )
        candidates = claim_event_candidates(self.store.connection, company_ref=ACN, now=NOW)
        refs = {row["payload"]["claim_version_ref"] for row in candidates}
        self.assertIn(live, refs)
        self.assertNotIn(dead, refs)

    def test_a_claim_older_than_the_window_is_not_rediscovered_every_day(self):
        self.claim(created_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(
            claim_event_candidates(self.store.connection, company_ref=ACN, now=NOW), []
        )


class DocumentEmitterAttributionTests(P14aHarness):
    def _document(self, record_id, discovery_id, spec_ref, *, review=None):
        with self.missions._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries("
                "record_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,connector_invocation_ref,"
                "source_envelope_ref,source_envelope_hash,actor_ref,requested_by,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (discovery_id, self.mission["id"], self.mission["content_hash"], ACN,
                 "source:alphaengine", "plan", "0" * 64, spec_ref, "1" * 64,
                 f"invocation:{discovery_id}", f"envelope:{discovery_id}", "2" * 64,
                 AUTOMATION, AUTOMATION, "{}", "3" * 64,
                 "2026-09-09T10:00:00+00:00"),
            )
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents("
                "record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, self.mission["id"], ACN, "source:alphaengine", "doc:shared",
                 discovery_id, "acquired", "2026-09-09T10:00:00+00:00", "2026-09-09T10:00:00+00:00"),
            )
            if review:
                cur.execute(
                    "INSERT INTO coverage_mission_document_reviews("
                    "review_id,mission_version_ref,company_ref,source_ref,document_ref,"
                    "discovered_document_ref,state,registered_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"review:{record_id}", self.mission["id"], ACN, "source:alphaengine",
                     "doc:shared", record_id, review, AUTOMATION,
                     "2026-09-09T11:00:00+00:00", "2026-09-09T11:00:00+00:00"),
                )

    def test_same_document_uses_strongest_spec_independent_of_row_order(self):
        self._document("doc-row:1", "discovery:1", "sell-side-reports")
        self.mission = self.grant(*self.grants)
        self._document("doc-row:2", "discovery:2", "earnings-call-transcripts")
        rows = document_event_candidates(
            self.store.connection, company_ref=ACN,
            mission_ref=self.mission_ref, now=NOW,
        )
        self.assertEqual([(row["kind"], row["payload"]["spec_ref"]) for row in rows],
                         [("transcript", "earnings-call-transcripts")])

    def test_dismissed_document_is_not_emitted_through_an_alias_spec(self):
        self._document("doc-row:1", "discovery:1", "earnings-call-transcripts")
        self.mission = self.grant(*self.grants)
        self._document("doc-row:2", "discovery:2", "sell-side-reports", review="dismissed")
        self.assertEqual(document_event_candidates(
            self.store.connection, company_ref=ACN,
            mission_ref=self.mission_ref, now=NOW,
        ), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
