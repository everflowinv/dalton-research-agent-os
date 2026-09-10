"""P14b: the human end of a ThesisRevisionCandidate (ADR-0007)."""

from __future__ import annotations

import json
import sqlite3
import unittest

from dalton_core.agenda import AgendaStore
from dalton_core.contracts import ThesisVersion
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.thesis_revision import (
    ADMISSION_ONLY_DECISION,
    TERMINAL_VERDICTS,
    VERDICTS,
    ThesisRevisionAuthority,
    ThesisRevisionConflict,
    ThesisRevisionNotFound,
    ThesisRevisionValidationError,
    proposed_content,
    revision_change_reason,
)
from tests.p14a_fixtures import ACN, AUTOMATION, OWNER, P14aHarness

INDUSTRY = "industry:us-it-services"
THESIS_REF = "thesis:acn:ai-reinvention-growth"


def thesis_content(**overrides):
    content = {
        "statement": "AI and reinvention demand can sustain Accenture growth.",
        "mechanism": "Bookings convert into revenue while productivity protects margin.",
        "confidence": "medium",
        "implied_expectation": "Bookings growth supports subsequent revenue growth.",
        "claim_refs": [],
        "catalyst_refs": ["catalyst:quarterly-results"],
        "falsifier_refs": ["falsifier:x"],
        "change_reason": "Initial human-reviewed ACN coverage admission.",
    }
    content.update(overrides)
    return content


class RevisionHarness(P14aHarness):
    """A Core with an admitted thesis and one candidate against it."""

    grants = ("market_event", "thesis_revision_candidate", "observation",
              "stage_record", "deliverable")

    def setUp(self):
        super().setUp()
        self.grant("thesis_revision_candidate",
                   checkpoints=("thesis_revision_candidate",))
        self.admission = CoverageAdmissionAuthority(self.store)
        agenda = AgendaStore(self.store)
        # The bootstrap mandate is industry-only; an admission binds the
        # company too, so this one names both.
        self.mandate = agenda.create_mandate(
            "mandate:acn-coverage",
            objective="Establish initial ACN coverage.",
            scope_refs=[ACN, INDUSTRY],
            constraints={}, success_criteria={},
            effective_from="2020-01-01T00:00:00+00:00", effective_until=None,
            actor_ref=OWNER, activate=True,
            version_id="mandate-version:acn-coverage:1",
            idempotency_key="mandate:acn-coverage:1",
        )
        self.pack = self.state["pack"]
        candidate = self.admission.propose_thesis_admission(
            candidate_id="thesis-admission-candidate:acn:1",
            thesis_ref=THESIS_REF, company_ref=ACN, industry_ref=INDUSTRY,
            template_ref="template:x", driver_refs=["driver:d"],
            mandate_version_ref=self.mandate["id"],
            mandate_version_hash=self.mandate["content_hash"],
            driver_pack_version_ref=self.pack["id"],
            driver_pack_version_hash=self.pack["content_hash"],
            content=thesis_content(), actor_ref=OWNER,
            idempotency_key="thesis-admission-candidate:acn:1",
        )
        admitted = self.admission.decide_thesis_admission(
            candidate_id=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="admit", rationale="Drivers and falsifiers are explicit.",
            decision_id="thesis-admission-decision:acn:1", actor_ref=OWNER,
            idempotency_key="thesis-admission-decision:acn:1",
        )
        self.version_one = admitted["thesis_version"]
        self.judgements = EventJudgementAuthority(self.store)
        self.events = ResearchEventAuthority(self.store)
        self.revisions = ThesisRevisionAuthority(self.store)
        self.claim_ref = self.claim(statement="Bookings fell 6% year on year.")

    def thesis_wire(self):
        return {
            "id": self.version_one["id"],
            "content_hash": self.version_one["content_hash"],
            "thesis_ref": THESIS_REF,
        }

    def judgement(self, suffix="1"):
        event = record_event(
            self.events, company_ref=ACN, kind="claim",
            occurred_at=f"2026-09-0{suffix}T00:00:00+00:00",
            source_refs=[self.claim_ref],
            payload={"claim_version_ref": self.claim_ref, "claim_ref": f"claim:test:{suffix}",
                     "metric_ref": "aspect:test", "period": "2026Q2",
                     "statement": "Bookings fell.", "source_ref": "source:sec-edgar"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        return self.judgements.record(
            event=event,
            judgement={"decision": "THESIS_WEAKENED", "action": "revise_thesis",
                       "driver_refs": [], "thesis_refs": [THESIS_REF],
                       "because": "Bookings fell against a thesis that needs them to convert.",
                       "citations": [self.claim_ref], "note": None,
                       "research_question": None, "forecast_change": None,
                       "model": {"work_order_ref": f"work:{suffix}", "cost_micros": 10}},
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "revise_thesis", "status": "candidate"},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def candidate(self, *, decision="THESIS_WEAKENED", confidence="low",
                  statement=None, suffix="1", reflection_ref=None):
        judged = self.judgement(suffix)
        return self.judgements.record_thesis_candidate(
            judgement_ref=judged["id"], thesis=self.thesis_wire(), company_ref=ACN,
            decision=decision,
            because="Bookings fell six percent against a thesis that needs conversion.",
            evidence_refs=[self.claim_ref], falsifier_ref="falsifier:x",
            proposed_statement=statement, proposed_confidence=confidence,
            mission=self.mission, actor_ref=AUTOMATION, reflection_ref=reflection_ref,
        )


class ChangeReasonTests(unittest.TestCase):
    def test_the_reason_names_the_decision_word_the_candidate_and_the_refs(self):
        reason = revision_change_reason({
            "id": "thesis-revision-candidate:abc", "decision": "THESIS_WEAKENED",
            "evidence_refs": ["claim-version:1", "claim-version:2", "claim-version:1"],
        })
        self.assertIn("THESIS_WEAKENED", reason)
        self.assertIn("thesis-revision-candidate:abc", reason)
        self.assertIn("claim-version:1", reason)
        self.assertIn("claim-version:2", reason)

    def test_a_candidate_with_no_evidence_cannot_occasion_a_version(self):
        with self.assertRaisesRegex(ThesisRevisionValidationError, "ADR-0008"):
            revision_change_reason({"id": "c", "decision": "THESIS_WEAKENED",
                                    "evidence_refs": []})

    def test_only_what_was_proposed_moves(self):
        current = thesis_content()
        content = proposed_content(current_content=current, candidate={
            "id": "c", "decision": "THESIS_WEAKENED", "evidence_refs": ["r"],
            "proposed_statement": None, "proposed_confidence": "low",
        })
        self.assertEqual(content["statement"], current["statement"])
        self.assertEqual(content["confidence"], "low")
        self.assertEqual(content["catalyst_refs"], current["catalyst_refs"])
        self.assertNotEqual(content["change_reason"], current["change_reason"])

    def test_a_float_confidence_is_refused(self):
        with self.assertRaisesRegex(ThesisRevisionValidationError, "ordinal"):
            proposed_content(current_content=thesis_content(), candidate={
                "id": "c", "decision": "THESIS_WEAKENED", "evidence_refs": ["r"],
                "proposed_statement": None, "proposed_confidence": 0.4,
            })


class DecisionTests(RevisionHarness):
    def test_accept_appends_a_version_and_leaves_the_old_one_exactly_as_it_was(self):
        candidate = self.candidate(statement="Reinvention demand is weaker than we said.")
        before = self.revisions.version_chain(THESIS_REF)
        self.assertEqual(len(before), 1)

        decided = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="accept", reason="Bookings are the falsifier and it fired.",
            actor_ref=OWNER,
        )
        self.assertEqual(decided["status"], "fresh")
        self.assertTrue(decided["terminal"])

        chain = self.revisions.version_chain(THESIS_REF)
        self.assertEqual([item["version_number"] for item in chain], [1, 2])
        self.assertEqual(chain[0]["content"], before[0]["content"])
        self.assertEqual(chain[0]["content_hash"], before[0]["content_hash"])
        self.assertEqual(chain[1]["prior_version_ref"], chain[0]["version_id"])
        self.assertEqual(chain[1]["authority_kind"], "human_admission")
        self.assertEqual(chain[1]["content"]["statement"],
                         "Reinvention demand is weaker than we said.")
        self.assertEqual(chain[1]["content"]["confidence"], "low")
        self.assertIn("THESIS_WEAKENED", chain[1]["content"]["change_reason"])
        self.assertIn(self.claim_ref, chain[1]["content"]["change_reason"])
        # The pointer moved to the new version and nothing else did.
        current = self.revisions.current_version(THESIS_REF)
        self.assertEqual(current["version_id"], chain[1]["version_id"])
        ThesisVersion.from_dict(decided["thesis_version"])
        # And the decision names how to get from the version back to the person.
        self.assertEqual(decided["resulting_thesis_version_ref"], chain[1]["version_id"])
        self.assertEqual(decided["reviewer_ref"], OWNER)

    def test_reject_records_the_reason_and_writes_no_version(self):
        candidate = self.candidate()
        decided = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="reject", reason="One quarter of bookings is noise, not a break.",
            actor_ref=OWNER,
        )
        self.assertEqual(decided["verdict"], "reject")
        self.assertTrue(decided["terminal"])
        self.assertIsNone(decided["resulting_thesis_version_ref"])
        self.assertEqual(len(self.revisions.version_chain(THESIS_REF)), 1)
        self.assertEqual(self.revisions.undecided(ACN), [])

    def test_defer_is_not_terminal_and_the_candidate_comes_back(self):
        candidate = self.candidate()
        deferred = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="defer", reason="Wait for next quarter's bookings.", actor_ref=OWNER,
        )
        self.assertFalse(deferred["terminal"])
        still_open = self.revisions.undecided(ACN)
        self.assertEqual([item["id"] for item in still_open], [candidate["id"]])
        self.assertTrue(still_open[0]["deferred"])
        self.assertEqual(still_open[0]["last_verdict"], "defer")

        accepted = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="accept", reason="The next quarter did the same thing.",
            actor_ref=OWNER,
        )
        self.assertEqual(accepted["sequence"], 1)
        self.assertEqual(len(self.revisions.version_chain(THESIS_REF)), 2)
        self.assertEqual(self.revisions.undecided(ACN), [])
        self.assertEqual([d["verdict"] for d in self.revisions.decisions(candidate["id"])],
                         ["defer", "accept"])

    def test_automation_can_never_decide(self):
        candidate = self.candidate()
        for actor in (AUTOMATION, "system:event-judgement", "coverage-mission"):
            with self.assertRaisesRegex(ThesisRevisionValidationError, "only a person"):
                self.revisions.decide(
                    candidate_ref=candidate["id"],
                    candidate_hash=candidate["content_hash"],
                    verdict="accept", reason="r", actor_ref=actor,
                )
        self.assertEqual(len(self.revisions.version_chain(THESIS_REF)), 1)

    def test_the_decision_is_bound_to_the_candidate_that_was_read(self):
        candidate = self.candidate()
        with self.assertRaisesRegex(ThesisRevisionConflict, "hash binding"):
            self.revisions.decide(
                candidate_ref=candidate["id"], candidate_hash="0" * 64,
                verdict="accept", reason="r", actor_ref=OWNER,
            )

    def test_a_second_terminal_decision_is_refused_and_a_repeat_is_a_duplicate(self):
        candidate = self.candidate()
        args = dict(candidate_ref=candidate["id"],
                    candidate_hash=candidate["content_hash"],
                    verdict="reject", reason="Noise.", actor_ref=OWNER)
        first = self.revisions.decide(**args)
        again = self.revisions.decide(**args)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        with self.assertRaisesRegex(ThesisRevisionConflict, "already reject"):
            self.revisions.decide(**{**args, "verdict": "accept", "reason": "Changed my mind."})

    def test_a_new_thesis_candidate_is_sent_to_the_admission_path(self):
        candidate = self.candidate(decision=ADMISSION_ONLY_DECISION, confidence=None)
        with self.assertRaisesRegex(ThesisRevisionConflict, "coverage admission"):
            self.revisions.decide(
                candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
                verdict="accept", reason="It is a different company story.",
                actor_ref=OWNER,
            )
        # But it can still be rejected or deferred, which is how it leaves the queue.
        rejected = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="reject", reason="Not a separate thesis.", actor_ref=OWNER,
        )
        self.assertEqual(rejected["verdict"], "reject")

    def test_a_revision_that_says_nothing_new_is_refused(self):
        candidate = self.candidate(confidence="medium", statement=None)
        with self.assertRaisesRegex(ThesisRevisionConflict, "ADR-0008"):
            self.revisions.decide(
                candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
                verdict="accept", reason="Looks right.", actor_ref=OWNER,
            )

    def test_an_unknown_candidate_and_an_unknown_verdict_are_both_refused(self):
        with self.assertRaises(ThesisRevisionNotFound):
            self.revisions.decide(
                candidate_ref="thesis-revision-candidate:nope", candidate_hash="a" * 64,
                verdict="accept", reason="r", actor_ref=OWNER,
            )
        candidate = self.candidate()
        with self.assertRaisesRegex(ThesisRevisionValidationError, "verdict"):
            self.revisions.decide(
                candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
                verdict="maybe", reason="r", actor_ref=OWNER,
            )
        self.assertEqual(set(VERDICTS), {"accept", "reject", "defer"})
        self.assertEqual(TERMINAL_VERDICTS, {"accept", "reject"})

    def test_a_person_may_revise_further_than_the_candidate_proposed(self):
        candidate = self.candidate()
        decided = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="accept", reason="Weaker, and for a different reason.",
            actor_ref=OWNER,
            content=thesis_content(
                statement="Reinvention demand is cyclical, not structural.",
                confidence="low", change_reason="ignored; the authority computes it",
            ),
        )
        version = decided["thesis_version"]
        self.assertEqual(version["statement"],
                         "Reinvention demand is cyclical, not structural.")
        # The person may rewrite the thesis; they may not rewrite why it changed.
        self.assertIn("thesis-revision-candidate:", version["change_reason"])
        self.assertIn("THESIS_WEAKENED", version["change_reason"])

    def test_a_claim_ref_that_is_not_in_the_ledger_is_refused(self):
        candidate = self.candidate()
        with self.assertRaises(ThesisRevisionNotFound):
            self.revisions.decide(
                candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
                verdict="accept", reason="r", actor_ref=OWNER,
                content=thesis_content(statement="Weaker.",
                                       claim_refs=["claim-version:invented"]),
            )


class ReadingTests(RevisionHarness):
    def test_the_reflection_travels_with_the_candidate(self):
        judged = self.judgement("2")
        reflection = self.judgements.record_reflection(
            judgement=judged,
            event=self.events.events(company_ref=ACN, kind="claim")[0],
            reflection={
                "what_we_expected": "Bookings convert within two quarters.",
                "what_happened": "They fell.",
                "why": "We read a pipeline comment as a booking.",
                "thesis_refs": [THESIS_REF],
                "citations": [self.claim_ref],
                "missed_debates": [{"question": "Is federal exposure the swing factor?",
                                    "because": "It moved when nothing else did."}],
                "followup_tracking": [], "followup_research": [],
                "market_view_vs_ours": {"available": False},
                "convergence_pathway": None,
                "model": {"work_order_ref": "work:reflect", "cost_micros": 5},
            },
            verification={"status": "verified", "verdict": "pass", "findings": []},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        candidate = self.judgements.record_thesis_candidate(
            judgement_ref=judged["id"], thesis=self.thesis_wire(), company_ref=ACN,
            decision="THESIS_WEAKENED", because="Bookings fell.",
            evidence_refs=[self.claim_ref], falsifier_ref=None,
            proposed_statement=None, proposed_confidence="low",
            mission=self.mission, actor_ref=AUTOMATION,
            reflection_ref=reflection["id"],
        )
        open_now = self.revisions.undecided(ACN)
        self.assertEqual(len(open_now), 1)
        self.assertEqual(open_now[0]["id"], candidate["id"])
        self.assertEqual(open_now[0]["reflection"]["what_happened"], "They fell.")
        self.assertEqual(
            self.revisions.candidate(candidate["id"])["reflection"]["id"],
            reflection["id"],
        )

    def test_a_decision_cannot_be_edited_or_deleted(self):
        candidate = self.candidate()
        decided = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="reject", reason="Noise.", actor_ref=OWNER,
        )
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store._transaction() as cur:
                cur.execute("UPDATE thesis_revision_decisions SET verdict='accept' "
                            "WHERE decision_id=?", (decided["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store._transaction() as cur:
                cur.execute("DELETE FROM thesis_revision_decisions WHERE decision_id=?",
                            (decided["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute(
                "INSERT INTO thesis_revision_decisions(decision_id,candidate_ref,"
                "candidate_hash,thesis_ref,thesis_version_ref,company_ref,"
                "candidate_decision,verdict,reason,terminal,record_json,content_hash,"
                "actor_ref,created_at) VALUES('d','c','h','t','v','co','NO_CHANGE',"
                "'accept','r',1,'{}','h','human:x','2026-09-09T00:00:00+00:00')"
            )

    def test_the_stored_decision_reads_back_as_written(self):
        candidate = self.candidate()
        decided = self.revisions.decide(
            candidate_ref=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="accept", reason="Bookings fired the falsifier.", actor_ref=OWNER,
        )
        row = self.store.connection.execute(
            "SELECT record_json FROM thesis_revision_decisions WHERE decision_id=?",
            (decided["id"],),
        ).fetchone()
        stored = json.loads(row["record_json"])
        self.assertEqual(stored["evidence_refs"], [self.claim_ref])
        self.assertEqual(stored["candidate_decision"], "THESIS_WEAKENED")


if __name__ == "__main__":
    unittest.main()
