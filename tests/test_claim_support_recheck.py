"""2026-09-26b: the recheck of outage-held candidates moves through its queue and commits once.

Live, the recheck ordered the held candidates by creation and asked the first
24 each run; the verifier had already answered those "not supported", so the
same 24 were read from the verdict cache every run -- legacy asked 24 and
admitted 0 twice, with 163 left and 139 never asked.  And its match on the
statement text also found candidates staged earlier with the same text, so the
same statement could be committed twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dalton_core.claim_support_recheck as recheck_module
import dalton_core.claim_support_verification as verification_module
from dalton_core.claim_support_recheck import ClaimSupportRecheck
from dalton_core.claim_support_verification import PURPOSE, ClaimSupportVerifier
from tests.test_claim_retirement import AUTOMATION, EPAM, ClaimRetirementHarness
from tests.test_claim_support_backfill import StatementModel

MISSION = {"id": "coverage-mission-version:fixture:1"}
ROUTE_FAILURE = "CockpitModelRouteUnavailable: no model route is available right now"
V1 = "claim-support-verification:v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Reviewer:
    """The candidate staging reads the recheck makes, over an in-memory staging db."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "CREATE TABLE candidate_claim_versions(version_id TEXT PRIMARY KEY, created_at TEXT, record_json TEXT);"
            "CREATE TABLE human_review_decisions(candidate_claim_version_ref TEXT);")
        self.bundles: dict[str, dict] = {}

    def add(self, ref: str, created_at: str, claim: dict, evidence: dict) -> None:
        self.connection.execute("INSERT INTO candidate_claim_versions VALUES(?,?,?)",
                                (ref, created_at, json.dumps(claim)))
        self.bundles[ref] = {"claim": claim, "evidence": evidence}

    def candidate_bundle(self, ref: str) -> dict:
        return self.bundles[ref]

    def candidate_authority_bundle(self, ref: str) -> dict:
        return {"candidate_ref": ref}


class _Store:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.commits: list[str] = []

    def commit_policy_candidate(self, *, candidate_ref: str, idempotency_key: str) -> dict:
        self.commits.append(candidate_ref)
        return {"status": "committed", "claim_version_ref": "claim-version:" + candidate_ref[-8:]}


class RecheckHarness(ClaimRetirementHarness):
    TEXT = ("发言人Operator： Next question please.\n"
            "发言人Ravi Kumar： We delivered another strong quarter of large deal bookings, signing seven "
            "deals. {n} Annual contract value decreased modestly.\n")

    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.scheduler_db = Path(temp.name) / "scheduler.sqlite"
        with sqlite3.connect(self.scheduler_db) as db:
            db.execute("CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY, work_order_json TEXT)")
        self.reviewer = _Reviewer()
        self.recheck_store = _Store(self.store.connection)
        self.verifier = ClaimSupportVerifier(
            store=self.store, model_call=StatementModel({}), purpose=PURPOSE,
            daily_cap_micros=10_000_000, producer_family=lambda ref: "deepseek-v4")
        self._n = 0
        self._outage: list[dict] = []

    # -- fixtures -----------------------------------------------------------

    def _binding(self, n: int, text: str, start: int, end: int) -> str:
        digest = _sha(text)
        self.objects[digest] = text.encode("utf-8")
        binding = f"transcript-claim-citation-binding:held{n:027d}"
        correction = f"transcript-correction-set-version:held{n:027d}"
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO transcript_correction_set_versions(version_id,correction_set_ref,version_number,"
                "source_manifest_ref,source_manifest_hash,source_content_hash,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (correction, f"transcript-correction-set:held:{n}", 1, f"manifest:held:{n}", "0" * 64,
                 digest, json.dumps({"id": correction, "source_content_hash": digest,
                                     "document_ref": "alphaengine-doc:ctsh-q2",
                                     "raw_review": {"rationale": "model draft x via route-decision:drafter"}}),
                 "0" * 64, AUTOMATION, "2026-09-25T00:00:00+00:00"))
            cur.execute(
                "INSERT INTO transcript_claim_citation_bindings(binding_id,correction_set_version_ref,"
                "source_manifest_ref,source_manifest_hash,source_content_hash,source_start,source_end,"
                "claim_eligible,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (binding, correction, f"manifest:held:{n}", "0" * 64, digest, start, end, 1,
                 json.dumps({"id": binding, "correction_set_version_ref": correction,
                             "source_sha256": _sha(text[start:end])}),
                 "0" * 64, "2026-09-25T00:00:00+00:00"))
        return binding

    def held(self, statement: str, *, text: str | None = None, span: tuple[int, int] | None = None,
             created_at: str | None = None) -> str:
        """One candidate an outage batch held, and the batch item it was asked as."""

        self._n += 1
        n = self._n
        text = text or self.TEXT.format(n=f"Deal {n} closed.")
        if span is None:
            start = text.index("We delivered")
            span = (start, text.index(" Annual"))
        binding = self._binding(n, text, *span)
        ref = f"candidate-claim-version:{n:08d}"
        claim = {"claim_kind": "qualitative", "normalized_statement": statement,
                 "subject_ref": EPAM, "period": "Q2 FY2026"}
        evidence = {"source_ref": "source:alphaengine", "artifact_refs": [{"ref": binding}]}
        self.reviewer.add(ref, created_at or f"2026-09-25T12:{n:02d}:00+00:00", claim, evidence)
        self._outage.append({"item_id": f"i{len(self._outage) + 1}", "subject": "EPAM",
                             "statement": statement, "cited_text": text[span[0]:span[1]]})
        return ref

    def outage(self) -> None:
        """The exhausted batch: its WorkOrder prompt and its systemic last reason."""

        request_key = "a" * 32
        with sqlite3.connect(self.scheduler_db) as db:
            db.execute("INSERT OR REPLACE INTO scheduler_work_orders VALUES(?,?)", (
                f"work:cockpit-{PURPOSE}-1",
                json.dumps({"question": "... UNTRUSTED_ITEMS=" + json.dumps(self._outage),
                            "metadata": {"request_id": f"{request_key}:2026-09-26T00"}})))
        self.verifier.records.write(
            "INSERT OR REPLACE INTO claim_support_attempts(request_key,purpose,attempts,last_bucket,"
            "last_reason,updated_at) VALUES(?,?,?,?,?,?)",
            (request_key, PURPOSE, 3, "2026-09-26T00", ROUTE_FAILURE, "2026-09-26T01:00:00+00:00"))

    def recheck(self, answers: dict, *, max_items: int = 24) -> tuple[ClaimSupportRecheck, StatementModel]:
        model = StatementModel(answers)
        self.verifier.model_call = model
        return ClaimSupportRecheck(store=self.recheck_store, reviewer=self.reviewer, verifier=self.verifier,
                                   scheduler_db=self.scheduler_db, max_items=max_items,
                                   spool=self.spool), model


def _asked(model: StatementModel) -> list[str]:
    asked = []
    for call in model.calls:
        prompt = call["prompt"]
        asked += [item["statement"] for item in
                  json.loads(prompt[prompt.rindex("UNTRUSTED_ITEMS=") + len("UNTRUSTED_ITEMS="):])]
    return asked


class QueueTests(RecheckHarness):
    NO = ("not_supported", "about_subject", None)
    YES = ("supported", "about_subject", None)

    def test_the_head_of_the_queue_moves_on_and_every_candidate_is_asked_once(self) -> None:
        statements = [f"Cognizant signed deal {n}." for n in range(1, 6)]
        for statement in statements:
            self.held(statement)
        self.outage()
        answers = {statement: self.NO for statement in statements}
        seen: list[str] = []
        for _ in range(3):
            recheck, model = self.recheck(answers, max_items=2)
            summary = recheck.run_once(mission=MISSION)
            seen += _asked(model)
        self.assertEqual(seen, statements)  # oldest first, each once, never the same head twice
        recheck, model = self.recheck(answers, max_items=2)
        final = recheck.run_once(mission=MISSION)
        self.assertEqual((final["remaining_before"], final["asked"], model.calls), (0, 0, []))
        self.assertEqual(summary["still_held"], 1)
        marks = self.store.connection.execute(
            "SELECT rule_ref, outcome, COUNT(*) FROM claim_support_recheck_marks GROUP BY 1, 2").fetchall()
        self.assertEqual([tuple(row) for row in marks], [(verification_module.CONTRACT_REF, "verdict", 5)])

    def test_a_rejection_under_the_old_contract_is_asked_once_under_the_new_one(self) -> None:
        statement = "In Q2 FY2026, Cognizant's Ravi Kumar said it signed seven large deals."
        ref = self.held(statement)
        self.outage()
        with patch.object(verification_module, "CONTRACT_REF", V1), \
                patch.object(recheck_module, "CONTRACT_REF", V1):
            recheck, _model = self.recheck({statement: self.NO})
            old = recheck.run_once(mission=MISSION)
            recheck, model = self.recheck({statement: self.NO})
            cached = recheck.run_once(mission=MISSION)
        self.assertEqual((old["still_held"], cached["asked"], model.calls), (1, 0, []))
        recheck, model = self.recheck({statement: self.YES})
        new = recheck.run_once(mission=MISSION)
        self.assertEqual([item["candidate_claim_ref"] for item in new["admitted"]], [ref])
        # Whole sentences, the speaker and the period went with it.
        prompt = model.calls[0]["prompt"]
        item = json.loads(prompt[prompt.rindex("UNTRUSTED_ITEMS=") + len("UNTRUSTED_ITEMS="):])[0]
        self.assertEqual(item["document"], {"period": "Q2 FY2026", "speaker": "Ravi Kumar"})
        self.assertTrue(item["cited_text"].startswith("发言人Ravi Kumar： We delivered"))
        recheck, model = self.recheck({statement: self.YES})
        self.assertEqual((recheck.run_once(mission=MISSION)["asked"], model.calls), (0, []))
        self.assertEqual(self.recheck_store.commits, [ref])

    def test_a_held_candidate_that_repeats_a_ledger_claim_is_never_committed(self) -> None:
        statement = "Cognizant signed seven large deals in the quarter."
        text = self.TEXT.format(n="Earlier note.")
        start, end = text.index("We delivered"), text.index(" Annual")
        # Already in the Ledger: same statement, subject, document and span.
        ledger = self.claim(subject=EPAM, statement=statement, source=text, span=(start, end))
        held = self.held(statement, text=text, span=(start, end))
        self.outage()
        recheck, model = self.recheck({statement: self.YES})
        summary = recheck.run_once(mission=MISSION)
        self.assertEqual(summary["duplicates"], [{"candidate_claim_ref": held,
                                                  "duplicate_of": ledger["ref"]}])
        self.assertEqual((summary["asked"], model.calls, self.recheck_store.commits), (0, [], []))

    def test_two_held_candidates_with_the_same_text_and_source_commit_once(self) -> None:
        statement = "Cognizant signed seven large deals in the quarter."
        text = self.TEXT.format(n="Same note.")
        span = (text.index("We delivered"), text.index(" Annual"))
        first = self.held(statement, text=text, span=span, created_at="2026-09-03T00:00:00+00:00")
        second = self.held(statement, text=text, span=span, created_at="2026-09-25T00:00:00+00:00")
        self.outage()
        recheck, model = self.recheck({statement: self.YES})
        summary = recheck.run_once(mission=MISSION)
        self.assertEqual(self.recheck_store.commits, [first])
        self.assertEqual(summary["duplicates"], [{"candidate_claim_ref": second, "duplicate_of": first}])
        self.assertEqual(len(_asked(model)), 1)
        # A different span of the same document is a different Claim.
        self.assertNotEqual(recheck_module._identity(EPAM, statement, {"document_sha256": "d", "start": 1,
                                                                       "end": 5}),
                            recheck_module._identity(EPAM, statement, {"document_sha256": "d", "start": 1,
                                                                       "end": 6}))


class NearDuplicateTests(RecheckHarness):
    """2026-09-27: a restatement of a live Claim of the same span is not committed again."""

    YES = ("supported", "about_subject", None)
    LIVE = "Cognizant signed seven large deals in the quarter, a strong bookings quarter."
    SAME = "Cognizant signed seven large deals during the quarter, a strong bookings quarter."

    def _document(self) -> tuple[str, tuple[int, int]]:
        text = self.TEXT.format(n="Earlier note.")
        return text, (text.index("We delivered"), text.index(" Annual"))

    def test_a_restatement_of_a_live_claim_of_the_same_span_is_a_duplicate(self) -> None:
        text, span = self._document()
        ledger = self.claim(subject=EPAM, statement=self.LIVE, source=text, span=span)
        # Overlapping, not identical: the candidate cites a sentence inside the Claim's span.
        held = self.held(self.SAME, text=text, span=(span[0], span[1] - 5))
        self.outage()
        recheck, model = self.recheck({self.SAME: self.YES})
        summary = recheck.run_once(mission=MISSION)
        [duplicate] = summary["duplicates"]
        self.assertEqual((duplicate["candidate_claim_ref"], duplicate["duplicate_of"]),
                         (held, ledger["ref"]))
        self.assertGreaterEqual(duplicate["similarity"], recheck_module.NEAR_DUPLICATE_RATIO)
        self.assertEqual((summary["asked"], model.calls, self.recheck_store.commits), (0, [], []))
        [(outcome, detail)] = self.store.connection.execute(
            "SELECT outcome, detail FROM claim_support_recheck_marks").fetchall()
        self.assertEqual(outcome, "duplicate")
        self.assertIn("same document, overlapping cited span", detail)

    def test_a_retired_claim_does_not_stand_in_for_the_candidate(self) -> None:
        text, span = self._document()
        ledger = self.claim(subject=EPAM, statement=self.LIVE, source=text, span=span)
        held = self.held(self.SAME, text=text, span=span)
        self.outage()
        with patch("dalton_core.claim_retirement.retired_claim_version_refs",
                   return_value={ledger["ref"]}):
            recheck, _model = self.recheck({self.SAME: self.YES})
            summary = recheck.run_once(mission=MISSION)
        self.assertEqual((summary["duplicates"], self.recheck_store.commits), ([], [held]))

    def test_a_different_span_different_numbers_or_a_negation_is_a_different_claim(self) -> None:
        text, span = self._document()
        self.claim(subject=EPAM, statement=self.LIVE, source=text,
                   span=(text.index("Annual contract"), len(text) - 1))
        other_span = self.held(self.SAME, text=text, span=span)
        text2, span2 = self._document()
        text2 = text2.replace("Earlier note.", "Second note.")
        self.claim(subject=EPAM, statement="Cognizant signed 7 large deals in the quarter.",
                   source=text2, span=span2)
        numbers = self.held("Cognizant signed 8 large deals in the quarter.", text=text2, span=span2)
        negated = self.held("Cognizant did not sign seven large deals in the quarter, a strong bookings quarter.",
                            text=text, span=span)
        self.outage()
        recheck, _model = self.recheck({s: self.YES for s in (
            self.SAME, "Cognizant signed 8 large deals in the quarter.",
            "Cognizant did not sign seven large deals in the quarter, a strong bookings quarter.")})
        summary = recheck.run_once(mission=MISSION)
        self.assertEqual(summary["duplicates"], [])
        self.assertEqual(sorted(self.recheck_store.commits), sorted([other_span, numbers, negated]))

    def test_two_held_restatements_of_one_span_commit_once(self) -> None:
        text, span = self._document()
        first = self.held(self.LIVE, text=text, span=span, created_at="2026-09-03T00:00:00+00:00")
        second = self.held(self.SAME, text=text, span=span, created_at="2026-09-25T00:00:00+00:00")
        self.outage()
        recheck, model = self.recheck({self.LIVE: self.YES, self.SAME: self.YES})
        summary = recheck.run_once(mission=MISSION)
        self.assertEqual(self.recheck_store.commits, [first])
        self.assertEqual([(d["candidate_claim_ref"], d["duplicate_of"]) for d in summary["duplicates"]],
                         [(second, first)])
        self.assertEqual(_asked(model), [self.LIVE])


class NearDuplicateRuleTests(unittest.TestCase):
    """The rule on the pairs it was measured on (legacy, 2026-09-27)."""

    def test_the_restatements_legacy_committed_are_caught(self) -> None:
        pairs = [
            ("RBC names IBM's key competitors as traditional hyperscalers Google, Microsoft and Amazon, "
             "along with consulting firms Deloitte and Accenture.",
             "IBM's key competitors are named as hyperscalers Google, Microsoft and Amazon, along with "
             "consulting firms Deloitte and Accenture."),
            ("RBC names IBM's key competitors as traditional hyperscalers Google, Microsoft and Amazon, "
             "along with consulting firms Deloitte and Accenture.",
             "IBM's key competitors include hyperscalers Google, Microsoft and Amazon, along with "
             "consulting firms Deloitte and Accenture."),
        ]
        for first, second in pairs:
            with self.subTest(first=first[:30]):
                self.assertIsNotNone(recheck_module.near_duplicate(first, second))

    def test_close_but_different_statements_are_not(self) -> None:
        near = recheck_module.near_duplicate
        self.assertIsNone(near(
            "DXC reported Q4 FY2026 total revenue declined year-to-year, below its guidance range.",
            "DXC attributed its Q4 revenue miss to weakening discretionary spending on short-term services."))
        self.assertIsNone(near("EPAM grew revenue 5% in Q2 2026.", "EPAM grew revenue 7% in Q2 2026."))
        self.assertIsNone(near("EPAM expects demand to improve in 2H.",
                               "EPAM doesn't expect demand to improve in 2H."))
        self.assertTrue(recheck_module.spans_overlap(
            {"document_sha256": "d", "start": 0, "end": 10}, {"document_sha256": "d", "start": 9, "end": 20}))
        self.assertFalse(recheck_module.spans_overlap(
            {"document_sha256": "d", "start": 0, "end": 10}, {"document_sha256": "d", "start": 10, "end": 20}))
        self.assertFalse(recheck_module.spans_overlap(
            {"document_sha256": "d", "start": 0, "end": 10}, {"document_sha256": "e", "start": 0, "end": 10}))


if __name__ == "__main__":
    unittest.main()
