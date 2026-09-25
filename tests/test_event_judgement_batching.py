"""2026-09-25: low-tier inputs share a call; a document found twice is read once.

Nothing is skipped and no cheaper model decides anything: every input still
reaches the judge in full.  What changes is how many calls that takes -- and
never for an input shaped like the three that have ever moved a view.
"""

from __future__ import annotations

from datetime import timedelta

import json
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.event_judgement_cli import (
    MAX_DOCUMENT_CLAIMS,
    MAX_LOW_TIER_BATCH,
    ClaimDocuments,
    incremental_group_hash,
    is_low_tier,
)
from tests.test_event_judgement import JUDGE_ROUTE, FakeModel, decision
from tests.p14a_fixtures import ACN
from tests.test_event_judgement_ledger_pool import NOW, Harness

# The three inputs that have ever produced a decision other than NO_CHANGE on
# legacy (2026-09-12, -14, -18), as the events carried them.
HISTORICAL_MOVERS = (
    ("claim:public_web:784a55262752fb344e0ab70dbc31dfac", "Q3 FY26",
     "Accenture reported new bookings down year-over-year on a local currency basis, "
     "suggesting softness in demand."),
    ("claim:public_web:febc7ff60b5f256bc63d25810d77290d", "Q3 FY26",
     "The company's new bookings declined in local currency year-over-year, with a "
     "book-to-bill ratio around one."),
    ("claim:public_web:105274408425a7bdedcdb512991fa086", "FY2025",
     "ACN discloses that volatile, negative or uncertain economic and geopolitical "
     "conditions have undermined and could again undermine client business confidence, "
     "causing clients to reduce or defer spending on new initiatives and technologies "
     "and to reduce, delay or eliminate spending under existing contracts."),
)



class LowTierBatchTests(Harness):
    def test_one_company_s_low_tier_inputs_from_one_day_share_a_slot(self):
        made = [self.document(kind, f"doc:{kind}")
                for kind in ("news", "sales_note", "expert_excerpt")]
        made.append(self.claim("claim:sales_notes:1", "The desk hears pricing is firm."))
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual({row["id"] for row in groups[0]}, {row["id"] for row in made})

    def test_a_busy_day_is_several_small_batches(self):
        for index in range(MAX_LOW_TIER_BATCH + 1):
            self.document("news", f"doc:{index}", hour=index)
        self.assertEqual(sorted(len(group) for group in self.groups()),
                         [1, MAX_LOW_TIER_BATCH])

    def test_different_days_are_different_batches(self):
        self.document("news", "doc:1", day=8)
        self.document("news", "doc:2", day=9)
        self.assertEqual(len(self.groups()), 2)

    def test_filings_transcripts_prices_and_public_web_claims_keep_their_own_call(self):
        for kind, document in (("transcript", "doc:t"), ("filing", "sec:filing:1")):
            self.document(kind, document)
        self.document("news", "doc:n")
        for ref, period, statement in HISTORICAL_MOVERS:
            self.claim(ref, statement, period=period)
        groups = self.groups()
        singles = [group for group in groups if len(group) == 1]
        self.assertEqual(len(groups), 6)
        self.assertEqual(len(singles), 6)
        for ref, _, _ in HISTORICAL_MOVERS:
            self.assertFalse(is_low_tier({"kind": "claim", "payload": {"claim_ref": ref}}))

    def test_a_batch_is_one_call_that_reads_every_input_and_records_each(self):
        made = [self.document("news", f"doc:{index}", hour=index) for index in range(3)]
        made.append(self.claim("claim:guidepoint:1", "An expert says budgets are flat."))
        summary, judge, verifier = self.judge_run()
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(len(verifier.prompts), 1)
        for row in made:
            self.assertIn(row["id"], judge.prompts[0])
            self.assertIn(row["id"], verifier.prompts[0])
        self.assertIn("Other inputs judged together with this one", judge.prompts[0])
        self.assertIn("If any single input warrants a decision other than NO_CHANGE",
                      judge.prompts[0])
        self.assertNotIn("monthly rows", judge.prompts[0])
        self.assertEqual(self.judgements.judged_count(ACN), 4)
        costs = [row["cost_micros"] for row in self.store.connection.execute(
            "SELECT cost_micros FROM event_judgements ORDER BY cost_micros")]
        self.assertEqual(costs, [0, 0, 0, 40_000])

    def test_a_batched_input_that_moves_the_view_can_be_cited(self):
        made = [self.document("news", f"doc:{index}", hour=index) for index in range(2)]
        summary, _, _ = self.judge_run(judge=FakeModel([decision(
            action="research", word="THESIS_WEAKENED", citations=[made[1]["id"]],
            research_question="Does the report hold?")], route=JUDGE_ROUTE))
        self.assertEqual(summary["decisions"], {"THESIS_WEAKENED": 1})

    def test_the_mission_lane_can_name_a_batch_to_its_child(self):
        from dalton_core.mission_event_judgement_lane import pending_event_groups

        for index in range(3):
            self.document("news", f"doc:{index}", hour=index)
        pending = pending_event_groups(self.store, self.missions, self.mission)
        self.assertEqual(len(pending), 1)
        chosen = pending[0]
        summary, _, _ = self.judge_run(company_ref=ACN, event_refs=chosen["event_refs"],
                                 event_group_hash=chosen["group_hash"], dry_run=True)
        self.assertEqual(summary["judgement_status"], "dry_run")
        self.assertEqual(summary["candidates"], 1)


class HistoricalMoverReplayTests(Harness):
    """Every input that ever moved a view still gets a full call of its own."""

    def test_each_historical_mover_is_judged_alone_beside_a_day_of_low_tier_inputs(self):
        low = [self.document("sales_note", f"sales-note:{index}", hour=index)
               for index in range(4)]
        low += [self.claim(f"claim:sales_notes:{index}", f"Desk colour {index}.", hour=index)
                for index in range(4)]
        low += [self.claim(f"claim:company_wiki:{index}", f"Wiki line {index}.", hour=index)
                for index in range(2)]
        movers = [self.claim(ref, statement, period=period, hour=12 + index)
                  for index, (ref, period, statement) in enumerate(HISTORICAL_MOVERS)]
        summary, judge, _ = self.judge_run(per_company=20, max_events=20)
        mover_prompts = [prompt for prompt in judge.prompts
                         if any(row["id"] in prompt for row in movers)]
        self.assertEqual(len(mover_prompts), len(movers))
        for prompt in mover_prompts:
            self.assertNotIn("Other inputs judged together", prompt)
            self.assertEqual(sum(row["id"] in prompt for row in movers), 1)
        self.assertEqual(len(judge.prompts), len(movers) + 2)
        self.assertEqual(self.judgements.judged_count(ACN), len(low) + len(movers))


class RepeatedDocumentTests(Harness):
    def test_a_document_found_again_after_a_no_change_is_not_paid_for_again(self):
        first = self.document("transcript", "alphaengine-doc:1", discovery="d1")
        self.judge_run()
        again = self.document("transcript", "alphaengine-doc:1", discovery="d2", day=10)
        summary, judge, _ = self.judge_run(now=NOW + timedelta(days=1))
        self.assertEqual(judge.prompts, [])
        self.assertEqual(summary["repeats"], 1)
        self.assertEqual(summary["cost_micros"], 0)
        record = self.judgements.judgement_for(again["id"])
        self.assertEqual(record["decision"], "NO_CHANGE")
        self.assertEqual(record["effect"]["kind"], "repeated_document")
        self.assertEqual(record["effect"]["primary_event_ref"], first["id"])
        self.assertEqual(record["model"]["cost_micros"], 0)
        self.assertNotIn("daily_digest", record["effect"])

    def test_a_document_once_judged_to_matter_is_judged_again_in_full(self):
        first = self.document("transcript", "alphaengine-doc:1", discovery="d1")
        self.judge_run(judge=FakeModel([decision(
            action="research", word="THESIS_WEAKENED", citations=[first["id"]],
            research_question="Does the call confirm the slowdown?")], route=JUDGE_ROUTE))
        self.assertEqual(self.judgements.judgement_for(first["id"])["decision"],
                         "THESIS_WEAKENED")
        self.document("transcript", "alphaengine-doc:1", discovery="d2", day=10)
        summary, judge, _ = self.judge_run(now=NOW + timedelta(days=1))
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(summary["repeats"], 0)

    def test_one_document_found_twice_before_judging_is_one_call(self):
        first = self.document("transcript", "alphaengine-doc:1", discovery="d1", day=8)
        second = self.document("transcript", "alphaengine-doc:1", discovery="d2")
        groups = self.groups()
        self.assertEqual([{row["id"] for row in group} for group in groups],
                         [{first["id"], second["id"]}])
        self.assertEqual(incremental_group_hash(groups[0]),
                         incremental_group_hash([second, first]))
        summary, judge, _ = self.judge_run()
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(self.judgements.judged_count(ACN), 2)



class DocumentClaimTests(Harness):
    """2026-09-25: the Claims of one document share a call; each is still read.

    The 28 risk factors extracted from one DXC 10-K page were judged one Opus
    call each (all NO_CHANGE); a Claim event carries no ``document_ref``, so
    "one document, one read" never reached Claims.  The document is found
    through the Claim's evidence, which these tests stand in for.
    """

    def setUp(self):
        super().setUp()
        self.documents: dict[str, str] = {}
        patcher = patch.object(
            ClaimDocuments, "_resolve",
            lambda _self, ref: self.documents.get(ref))
        patcher.start()
        self.addCleanup(patcher.stop)

    def doc_claim(self, ref, document, statement=None, **kwargs):
        event = self.claim(ref, statement or f"statement {ref}", **kwargs)
        self.documents[event["payload"]["claim_version_ref"]] = document
        return event

    def test_one_document_s_claims_share_a_slot_of_at_most_twelve(self):
        first = [self.doc_claim(f"claim:public_web:a{index:02d}", "public-web-document:a",
                                hour=index)
                 for index in range(MAX_DOCUMENT_CLAIMS + 2)]
        other = [self.doc_claim(f"claim:public_web:b{index}", "public-web-document:b",
                                hour=20 + index) for index in range(2)]
        groups = self.groups()
        sizes = sorted(len(group) for group in groups)
        self.assertEqual(sizes, [2, 2, MAX_DOCUMENT_CLAIMS])
        by_document = {frozenset(row["id"] for row in group) for group in groups}
        self.assertIn(frozenset(row["id"] for row in other), by_document)
        self.assertIn(frozenset(row["id"] for row in first[:MAX_DOCUMENT_CLAIMS]),
                      by_document)

    def test_public_web_and_alphaengine_are_not_low_tier(self):
        for source in ("public_web", "alphaengine"):
            self.assertFalse(is_low_tier(
                {"kind": "claim", "payload": {"claim_ref": f"claim:{source}:1"}}))
        # Without a document, two alphaengine Claims of a day keep a call each.
        self.claim("claim:alphaengine:1", "Management guided revenue down.")
        self.claim("claim:alphaengine:2", "Management raised the margin target.", hour=11)
        self.assertEqual(sorted(len(group) for group in self.groups()), [1, 1])

    def test_different_documents_are_different_slots(self):
        self.doc_claim("claim:alphaengine:1", "alphaengine-doc:1")
        self.doc_claim("claim:alphaengine:2", "alphaengine-doc:2", hour=11)
        self.doc_claim("claim:alphaengine:3", "alphaengine-doc:1", hour=12)
        self.assertEqual(sorted(len(group) for group in self.groups()), [1, 2])

    def test_a_document_batch_reads_every_claim_and_any_one_can_decide(self):
        made = [self.doc_claim(f"claim:public_web:r{index}", "public-web-document:10k",
                               hour=index) for index in range(3)]
        mover = made[2]
        summary, judge, verifier = self.judge_run(judge=FakeModel([decision(
            action="research", word="THESIS_WEAKENED", citations=[mover["id"]],
            research_question="Does the risk factor bite?")], route=JUDGE_ROUTE))
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(len(verifier.prompts), 1)
        for prompt in (judge.prompts[0], verifier.prompts[0]):
            for row in made:
                self.assertIn(row["id"], prompt)
                self.assertIn(row["payload"]["statement"], prompt)
            self.assertIn("Other Claims drawn from the same document", prompt)
            self.assertIn("ANY SINGLE Claim can change the decision by itself", prompt)
            self.assertIn("read each of them as if it had arrived alone", prompt)
        self.assertEqual(summary["decisions"], {"THESIS_WEAKENED": 1})
        self.assertEqual(self.judgements.judged_count(ACN), 3)

    def test_the_public_web_line_is_only_for_public_web_documents(self):
        for index in range(2):
            self.doc_claim(f"claim:alphaengine:{index}", "alphaengine-doc:1", hour=index)
        _, judge, _ = self.judge_run()
        self.assertIn("ANY SINGLE Claim", judge.prompts[0])
        self.assertNotIn("public web pages", judge.prompts[0])

    def test_a_claim_is_never_answered_from_its_document_s_earlier_judgement(self):
        self.doc_claim("claim:public_web:1", "public-web-document:a")
        self.judge_run()
        self.doc_claim("claim:public_web:2", "public-web-document:a", day=10)
        summary, judge, _ = self.judge_run(now=NOW + timedelta(days=1))
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(summary["repeats"], 0)

    def test_the_historical_movers_are_each_read_with_their_document(self):
        # The three decisions other than NO_CHANGE came from two ACN pages.
        pages = ("public-web-document:acn-q3", "public-web-document:acn-q3",
                 "public-web-document:acn-10k")
        movers = [self.doc_claim(ref, page, statement, period=period, hour=index)
                  for index, ((ref, period, statement), page)
                  in enumerate(zip(HISTORICAL_MOVERS, pages))]
        peers = [self.doc_claim(f"claim:public_web:peer{index}",
                                "public-web-document:acn-10k", hour=5 + index)
                 for index in range(4)]
        _, judge, _ = self.judge_run(per_company=20, max_events=20)
        self.assertEqual(len(judge.prompts), 2)
        for row in movers:
            carrying = [prompt for prompt in judge.prompts if row["id"] in prompt]
            self.assertEqual(len(carrying), 1)
            self.assertIn(row["payload"]["statement"], carrying[0])
            self.assertIn("ANY SINGLE Claim can change the decision by itself", carrying[0])
        self.assertEqual(self.judgements.judged_count(ACN), len(movers) + len(peers))

    def test_the_mission_lane_can_name_a_document_batch_to_its_child(self):
        from dalton_core.mission_event_judgement_lane import pending_event_groups

        for index in range(3):
            self.doc_claim(f"claim:public_web:{index}", "public-web-document:a", hour=index)
        pending = pending_event_groups(self.store, self.missions, self.mission)
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0]["event_refs"]), 3)
        chosen = pending[0]
        summary, _, _ = self.judge_run(company_ref=ACN, event_refs=chosen["event_refs"],
                                       event_group_hash=chosen["group_hash"], dry_run=True)
        self.assertEqual(summary["candidates"], 1)


class ClaimDocumentResolutionTests(unittest.TestCase):
    """Claim -> evidence -> correction set (directly or via its citation
    binding) -> ``document_ref``; else the evidence's source envelope."""

    def connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE evidence_versions (evidence_version_id TEXT, evidence_json TEXT);
            CREATE TABLE evidence_relations (evidence_version_id TEXT, claim_version_id TEXT);
            CREATE TABLE transcript_correction_set_versions (version_id TEXT,
                record_json TEXT);
            CREATE TABLE transcript_claim_citation_bindings (binding_id TEXT,
                correction_set_version_ref TEXT);
        """)
        sets = {"transcript-correction-set-version:1": "public-web-document:10k",
                "transcript-correction-set-version:2": "alphaengine-doc:7"}
        for version, document in sets.items():
            connection.execute("INSERT INTO transcript_correction_set_versions VALUES(?,?)",
                               (version, json.dumps({"document_ref": document})))
        connection.execute("INSERT INTO transcript_claim_citation_bindings VALUES(?,?)",
                           ("transcript-claim-citation-binding:b",
                            "transcript-correction-set-version:2"))
        evidence = {
            "cv-lineage": {"source_lineage": ["source:public-web",
                                              "transcript-correction-set-version:1"],
                           "source_envelope_ref": "source-envelope:x"},
            "cv-binding": {"source_lineage": ["source:alphaengine"],
                           "artifact_refs": [{"ref": "transcript-claim-citation-binding:b"}],
                           "source_envelope_ref": "source-envelope:y"},
            "cv-envelope": {"source_lineage": ["source:sec-edgar"],
                            "source_envelope_ref": "statement-ingest:z"},
        }
        for claim, record in evidence.items():
            connection.execute("INSERT INTO evidence_versions VALUES(?,?)",
                               ("ev-" + claim, json.dumps(record)))
            connection.execute("INSERT INTO evidence_relations VALUES(?,?)",
                               ("ev-" + claim, claim))
        return connection

    def claim(self, ref):
        return {"kind": "claim", "payload": {"claim_version_ref": ref}}

    def test_each_path_and_its_fallbacks(self):
        documents = ClaimDocuments(self.connection())
        self.assertEqual(documents(self.claim("cv-lineage")), "public-web-document:10k")
        self.assertEqual(documents(self.claim("cv-binding")), "alphaengine-doc:7")
        self.assertEqual(documents(self.claim("cv-envelope")), "statement-ingest:z")
        self.assertIsNone(documents(self.claim("cv-none")))
        self.assertIsNone(documents({"kind": "news", "payload": {"claim_version_ref": "x"}}))

    def test_a_core_without_the_tables_has_no_documents(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.assertIsNone(ClaimDocuments(connection)(self.claim("cv")))

if __name__ == "__main__":
    import unittest

    unittest.main()
