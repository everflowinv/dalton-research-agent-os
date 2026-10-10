"""The directed-document reply contract, against the replies ws-7d actually paid for.

2026-09-25..28, ws-7d894366: 15 document-research admissions ended in
``recovery_required``.  Of the 56 paid draft/verifier replies the lane threw
away, 25 were schema-valid JSON wrapped in one ```json fence (glm-5.3 and
twice claude-opus-5) and 22 were claude-opus-5 answers that honestly listed a
remaining gap in ``missing`` (the filing gives remaining performance
obligations company-wide, not for Google Cloud alone).  The bounded automatic
retry bought the same reply again each time, and so would an owner's
re-authorization.
"""

from __future__ import annotations

import json
import unittest

from dalton_core.annual_report_qualitative import (
    AnnualReportQualitativeError, parse_verifier_text, unfence_json_text,
)
from dalton_core.document_research_qualitative import parse_draft_text

CANDIDATE = {
    "normalized_statement": "Alphabet disclosed remaining performance obligations "
                            "at FY2025 year end, primarily related to Google Cloud.",
    "metric_or_aspect": "remaining performance obligations",
    "period": "FY2025",
    "basis": "10-K revenue note: $242.8 billion, just over half within 24 months.",
    "cited_match_indexes": [1],
}


def _draft(**overrides):
    return {"schema_version": "0.1", "status": "answered",
            "answer": "Alphabet reports its backlog company-wide.",
            "candidate": dict(CANDIDATE), "missing": [], **overrides}


class DraftContractTests(unittest.TestCase):
    def test_one_enclosing_json_fence_is_not_a_contract_failure(self) -> None:
        body = json.dumps({"schema_version": "0.1", "status": "insufficient_evidence",
                           "answer": "The excerpts do not disclose it.",
                           "candidate": None, "missing": ["the revenue note"]},
                          ensure_ascii=False, indent=2)
        for text in (f"```json\n{body}\n```", f"```\n{body}\n```\n",
                     f"  ```JSON\r\n{body}\r\n```  "):
            with self.subTest(text=text[:12]):
                parsed = parse_draft_text(text, match_count=3)
                self.assertEqual(parsed["status"], "insufficient_evidence")

    def test_anything_but_exactly_one_outer_fence_is_still_refused(self) -> None:
        body = json.dumps(_draft())
        for text in (f"Here is the JSON:\n```json\n{body}\n```",
                     f"```json\n{body}\n```\nHope this helps.",
                     f"```json\n{body}",                      # truncated reply
                     f"```json\n{body}\n```\n```json\n{body}\n```",
                     f"```json\n{body[:-40]}\n```"):
            with self.subTest(text=text[:20]), self.assertRaisesRegex(
                    AnnualReportQualitativeError, "not strict JSON"):
                parse_draft_text(text, match_count=3)
        self.assertEqual(unfence_json_text("{}"), "{}")

    def test_an_answered_draft_may_name_what_the_excerpts_leave_open(self) -> None:
        parsed = parse_draft_text(json.dumps(_draft(missing=[
            "a Google Cloud-only figure (the 10-K gives the company-wide total)",
        ])), match_count=3)
        self.assertEqual(parsed["status"], "answered")
        self.assertEqual(len(parsed["missing"]), 1)

    def test_the_rest_of_the_draft_contract_is_unchanged(self) -> None:
        cases = (
            (_draft(missing=[""]), "missing evidence is invalid"),
            (_draft(status="insufficient_evidence"), "null candidate"),
            (_draft(status="insufficient_evidence", candidate=None), "concrete missing"),
            (_draft(candidate={**CANDIDATE, "cited_match_indexes": [9]}), "citations"),
            (_draft(extra=True), "closed shape"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                    AnnualReportQualitativeError, message):
                parse_draft_text(json.dumps(value), match_count=3)


class VerifierContractTests(unittest.TestCase):
    def test_a_fenced_verifier_reply_parses(self) -> None:
        reply = {"schema_version": "0.1", "verdict": "pass",
                 "verified_statement": CANDIDATE["normalized_statement"],
                 "findings": []}
        parsed = parse_verifier_text(f"```json\n{json.dumps(reply)}\n```",
                                     draft={"candidate": CANDIDATE})
        self.assertEqual(parsed["verdict"], "pass")

    def test_a_pass_that_restates_the_statement_is_still_refused(self) -> None:
        # e71b2f03: gemini-3-8-flash passed the draft but rewrote the statement
        # with figures the draft had deliberately kept out.  That is not a
        # verdict on *this* statement, so it stays a contract failure.
        reply = {"schema_version": "0.1", "verdict": "pass",
                 "verified_statement": "Alphabet had $242.8 billion of RPO.",
                 "findings": []}
        with self.assertRaisesRegex(AnnualReportQualitativeError, "exact candidate"):
            parse_verifier_text(json.dumps(reply), draft={"candidate": CANDIDATE})


if __name__ == "__main__":
    unittest.main()
