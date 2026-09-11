import unittest

from dalton_core.document_read_completion import (
    DocumentReadCompletionAuthority, DocumentReadCompletionError,
)
from dalton_core.store import content_hash
from tests.test_mission_stage import ACN, AUTOMATION, REPORTS, StageHarness


class DocumentReadCompletionTests(StageHarness):
    def setUp(self):
        super().setUp()
        self.document_ref = self.document(ACN, REPORTS, "acquired")
        self.missions.backfill_document_reviews(self.mission_ref)
        self.review = self.missions.document_reviews(self.mission["id"], company_ref=ACN)[0]
        self.authority = DocumentReadCompletionAuthority(self.store.connection)
        self.source_hash = content_hash(self.review)

    def window(self, offset=0, next_offset=None):
        return {"offset": offset, "context_ref": f"context:{offset}",
                "context_hash": "5" * 64, "next_offset": next_offset,
                "source_content_hash": "6" * 64,
                "source_review_hash": self.source_hash,
                "work_order_ref": f"work:{offset}",
                "result_envelope_ref": f"result:{offset}",
                "result_envelope_hash": "7" * 64, "status": "succeeded"}

    def record(self, windows, validator=lambda value: dict(value)):
        return self.authority.record(
            review_id=self.review["review_id"], source_review_hash=self.source_hash,
            actor_ref=AUTOMATION,
            windows=windows, receipt_reader=type("Reader", (), {"read_completion_receipt": staticmethod(lambda **kw: validator(next(w for w in windows if w["offset"] == kw["offset"])))})(),
            created_at="2026-09-11T00:00:00+00:00")

    def test_exact_verified_chain_is_immutable_and_counts_as_read(self):
        result = self.record([self.window(0, 1), self.window(1, None)])
        self.assertEqual(result["status"], "fresh")
        # A crash here leaves an open, retryable review and must not claim read.
        self.assertEqual(self.item(self.evaluate(), ACN, "broker_research")["read"], 0)
        self.missions.resolve_document_review(
            self.review["review_id"], resolution="dismissed", actor_ref=AUTOMATION,
            rationale="ADR-0005: no new qualitative claim", expected_review_hash=self.source_hash)
        self.assertEqual(self.record([self.window(0, 1), self.window(1, None)])["status"], "duplicate")
        item = self.item(self.evaluate(), ACN, "broker_research")
        self.assertEqual((item["have"], item["read"]), (1, 1))

    def test_boolean_or_drifted_validator_response_is_not_proof(self):
        with self.assertRaises((DocumentReadCompletionError, TypeError)):
            self.record([self.window()], validator=lambda _value: True)
        with self.assertRaisesRegex(DocumentReadCompletionError, "authority binding drifted"):
            self.record([self.window()], validator=lambda value: {**value, "work_order_ref": "work:other"})
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM document_read_completion_proofs").fetchone()[0], 0)

    def test_failed_or_incomplete_window_chain_is_rejected(self):
        with self.assertRaisesRegex(DocumentReadCompletionError, "did not succeed"):
            self.record([{**self.window(), "status": "failed"}])
        with self.assertRaisesRegex(DocumentReadCompletionError, "incomplete"):
            self.record([self.window(0, 1)])

    def test_source_review_hash_and_automation_actor_are_required(self):
        with self.assertRaisesRegex(DocumentReadCompletionError, "source review hash drifted"):
            self.authority.record(
                review_id=self.review["review_id"], source_review_hash="0" * 64, actor_ref=AUTOMATION,
                windows=[self.window()], receipt_reader=type("Reader", (), {"read_completion_receipt": staticmethod(lambda **kw: self.window())})())
        with self.assertRaisesRegex(DocumentReadCompletionError, "automation principal"):
            self.authority.record(
                review_id=self.review["review_id"], source_review_hash=self.source_hash,
                actor_ref="human:owner",
                windows=[self.window()], receipt_reader=type("Reader", (), {"read_completion_receipt": staticmethod(lambda **kw: self.window())})())

    def test_raw_insert_and_proof_after_resolution_are_rejected(self):
        with self.assertRaisesRegex(Exception, "requires authority"):
            self.store.connection.execute(
                "INSERT INTO document_read_completion_proofs VALUES(?,?,?,?,?,?,?,?)",
                ("proof:x", self.review["review_id"], self.source_hash, self.document_ref,
                 ACN, "{}", "0" * 64, "2026-09-11T00:00:00+00:00"))
        self.missions.resolve_document_review(
            self.review["review_id"], resolution="dismissed", actor_ref=AUTOMATION,
            rationale="fixture", expected_review_hash=self.source_hash)
        with self.assertRaisesRegex(DocumentReadCompletionError, "must precede"):
            self.record([self.window()])


if __name__ == "__main__":
    unittest.main()
