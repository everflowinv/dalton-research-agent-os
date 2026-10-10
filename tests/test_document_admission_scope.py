"""2026-09-29: what one document may put into the Ledger (``document_admission_scope``).

EPAM's 10-K (fetched from sec.gov) went in as 79 qualitative Claims -- risk
factors and accounting policy, seven near-duplicate pairs, 21 spellings of
the period, one of them "FY2026 filing (as of 2026-09-08)" -- and IBM's as 22.
"""

from __future__ import annotations

import unittest

from dalton_core.document_admission_scope import (
    MAX_CLAIMS_PER_DOCUMENT,
    information_score,
    plan_document_admissions,
    same_finding,
    sec_filing_period,
)
from dalton_core.document_extraction import build_prompt, filing_dating

#: Pairs from EPAM's 10-K as admitted on legacy (2026-09-29), each the same
#: finding read out of two sections of the filing.
EPAM_RESTATEMENTS = (
    ("EPAM Systems describes itself as a leading global provider of digital engineering, cloud and "
     "AI-enabled transformation services, and a leading business and experience consulting partner.",
     "EPAM describes itself as a global provider of digital engineering, cloud and AI-enabled "
     "transformation services and a leading business and experience consulting partner."),
    ("EPAM Systems states that it derives revenues from a variety of service arrangements that have "
     "been evolving to provide more customized and integrated solutions to clients.",
     "EPAM Systems says it derives revenues from a variety of service arrangements that have been "
     "evolving to provide more customized and integrated solutions."),
    # Legacy claim-version ...9519aba8 / ...0e2639dc: "regardless of whether" and
    # "whether or not" say the same thing.
    ("EPAM Systems states that salaries and other compensation expenses of its delivery professionals "
     "are reported as cost of revenues regardless of whether those employees are actually performing "
     "services for clients during a given period.",
     "EPAM Systems reports salaries and other compensation expenses of its delivery professionals as "
     "cost of revenues whether or not the employees are actually performing services for clients "
     "during a given period, and reports government incentives and assistance related to those "
     "services in cost of revenues."),
    # ...cdcc0958 / ...5d2dfbac: one names the year the restrictions began, one does not.
    ("Belarus restrictions in place from April 2024 until the end of 2026 may prevent EPAM from "
     "distributing excess funds, if any, out of Belarus, though EPAM does not expect these "
     "restrictions to materially impact its ability to meet worldwide cash obligations during that "
     "period.",
     "EPAM states that Belarus restrictions on distributing dividends are scheduled to remain in place "
     "until the end of 2026 and may prevent EPAM from distributing excess funds out of Belarus, but "
     "that it does not expect these restrictions to have a material impact on its ability to meet its "
     "worldwide cash obligations during this period."),
)
#: DXC's Guidepoint excerpt d50efc...: three different things the respondent said.
DXC_DIFFERENT = (
    "The respondent said the hardest customers to replace are legacy ones where DXC delivery holds "
    "intimate working knowledge of the customer.",
    "The respondent said servers, storage, networks and support can be considered easy to replace, "
    "but replacement becomes much more problematic in the application space.",
    "The respondent said that anything still managed in a traditional DXC owned data center rather "
    "than the cloud could also be considerably challenged to move.",
)


class SameFindingTests(unittest.TestCase):
    def test_a_filing_saying_one_thing_in_two_sections_is_one_finding(self) -> None:
        for first, second in EPAM_RESTATEMENTS:
            with self.subTest(first=first[:40]):
                self.assertIsNotNone(same_finding(first, second))

    def test_different_findings_numbers_and_negations_are_kept_apart(self) -> None:
        for index, first in enumerate(DXC_DIFFERENT):
            for second in DXC_DIFFERENT[index + 1:]:
                self.assertIsNone(same_finding(first, second), (first, second))
        self.assertIsNone(same_finding("Revenue grew 5% in 2025.", "Revenue grew 7% in 2025."))
        self.assertIsNone(same_finding("EPAM expects demand to recover in 2026.",
                                       "EPAM does not expect demand to recover in 2026."))


class InformationScoreTests(unittest.TestCase):
    def test_what_changed_ranks_above_risk_factors_and_accounting_policy(self) -> None:
        change = ("In 2025, EPAM Systems' Americas segment operating profit and its margin decreased "
                  "compared to 2024, primarily due to compensation increases.")
        risk = ("EPAM Systems states that its effective tax rate could be materially adversely affected "
                "by factors including changes in the amount of income taxed.")
        policy = ("EPAM states that for stock-based awards with performance conditions, the compensation "
                  "cost it recognizes is based on the probable outcome.")
        self.assertGreater(information_score(change), information_score(risk))
        self.assertGreater(information_score(change), information_score(policy))
        self.assertLess(information_score(risk), 0)


def _candidate(key, statement, start, end, subject="company:epam"):
    return {"key": key, "subject_ref": subject, "statement": statement, "start": start, "end": end}


class PlanTests(unittest.TestCase):
    def test_a_span_the_ledger_already_cites_is_that_claim(self) -> None:
        # DXC d50efc: the 12:31 re-read cited 198-517 again, in other words.
        live = [{"ref": "claim-version:first", "subject_ref": "company:dxc", "start": 198, "end": 517,
                 "statement": "As of the interview, DXC was described as a managed services company.",
                 "candidate_ref": "candidate-claim-version:first"}]
        plan = plan_document_admissions([
            _candidate("again", "A guidepoint interviewee described DXC Technology as a managed "
                                "services company focused on running IT.", 198, 517, "company:dxc"),
            _candidate("other-subject", "An interviewee described the market.", 198, 517, "industry:it"),
        ], live)
        self.assertEqual(plan["again"], {"decision": "duplicate", "duplicate_of": "claim-version:first",
                                         "candidate_ref": "candidate-claim-version:first",
                                         "basis": "same_span", "similarity": None})
        self.assertEqual(plan["other-subject"]["decision"], "admit")

    def test_a_restatement_anywhere_in_the_document_is_a_duplicate(self) -> None:
        first, second = EPAM_RESTATEMENTS[0]
        live = [{"ref": "claim-version:item1", "subject_ref": "company:epam", "start": 190054,
                 "end": 190318, "statement": first, "candidate_ref": None}]
        plan = plan_document_admissions([_candidate("note1", second, 292340, 292586)], live)
        self.assertEqual((plan["note1"]["decision"], plan["note1"]["basis"]), ("duplicate", "same_finding"))
        # Within one read: the more informative one stays.
        plan = plan_document_admissions([_candidate("a", first, 1, 2), _candidate("b", second, 3, 4)], [])
        self.assertEqual(sorted(value["decision"] for value in plan.values()), ["admit", "duplicate"])

    def test_the_cap_counts_the_ledger_and_keeps_the_most_informative(self) -> None:
        change = "EPAM's Americas revenue grew in 2025 compared with 2024, driven by {}."
        risk = "EPAM states that {} could adversely affect its results if it occurs."
        candidates = [_candidate(f"risk{n}", risk.format(f"risk factor number {n} of kind {n * 7}"), n * 10,
                                 n * 10 + 5) for n in range(8)]
        candidates += [_candidate(f"change{n}", change.format(f"client group {n} in vertical {n * 3}"),
                                  1000 + n * 10, 1000 + n * 10 + 5) for n in range(8)]
        plan = plan_document_admissions(candidates, [])
        admitted = {key for key, value in plan.items() if value["decision"] == "admit"}
        self.assertEqual(len(admitted), MAX_CLAIMS_PER_DOCUMENT)
        self.assertTrue({f"change{n}" for n in range(8)} <= admitted)
        self.assertEqual(sum(1 for v in plan.values() if v["decision"] == "truncated"), 4)
        # Ten already in the Ledger leave room for two.
        live = [{"ref": f"claim-version:{n}", "subject_ref": "company:epam", "start": 5000 + n,
                 "end": 5001 + n, "statement": f"unrelated finding {n} about subject {n * 13}"}
                for n in range(10)]
        plan = plan_document_admissions(candidates, live)
        self.assertEqual(sum(1 for v in plan.values() if v["decision"] == "admit"), 2)


EPAM_COVER = ("epam-20251231\n\n0001352010 2025 FY FALSE ...\n\nUNITED STATES SECURITIES AND EXCHANGE "
              "COMMISSION\n\nWashington, D.C. 20549\n\nFORM 10-K\n\n☒\n\nANNUAL REPORT PURSUANT TO SECTION "
              "13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934\n\nFor the fiscal year ended December 31 "
              ", 2025\n\nOR\n\n☐\n\nTRANSITION REPORT")
EPAM_URL = "https://www.sec.gov/Archives/edgar/data/1352010/000135201026000015/epam-20251231.htm"


class FilingPeriodTests(unittest.TestCase):
    def test_the_cover_page_is_the_period_not_the_fetch_day(self) -> None:
        # Fetched 2026-09-08; admitted as "FY2026 filing (as of 2026-09-08)".
        filing = sec_filing_period(EPAM_COVER, EPAM_URL)
        self.assertEqual((filing["form"], filing["period_end"], filing["label"], filing["basis"]),
                         ("10-K", "2025-12-31", "FY2025 (fiscal year ended 2025-12-31)", "cover_page"))
        self.assertEqual(sec_filing_period("", EPAM_URL)["basis"], "edgar_document_name")
        quarter = sec_filing_period("FORM 10-Q\nFor the quarterly period ended September 30, 2025\n")
        self.assertEqual(quarter["label"], "quarter ended 2025-09-30")
        self.assertIsNone(sec_filing_period("a press release", "https://example.com/x.htm"))

    def test_a_filing_is_dated_by_its_period_and_the_prompt_says_so(self) -> None:
        dating = filing_dating("source:sec-edgar", EPAM_URL, EPAM_COVER)
        self.assertEqual((dating["document_date"], dating["document_date_basis"], dating["document_stale"]),
                         ("2025-12-31", "period_of_report", False))
        self.assertEqual(filing_dating("source:web-search", "https://example.com/a", EPAM_COVER), {})
        # A 10-K fetched from sec.gov through the web lane is a filing too.
        self.assertTrue(filing_dating("source:web-search", EPAM_URL, EPAM_COVER))
        prompt = build_prompt({"company_ref": "company:epam", "company_label": "EPAM", "document_ref": "d",
                               "offset": 0, "end": 1, "quotes": [{"quote_id": "q", "raw_text": "t"}],
                               "document_date": "2025-12-31", **dating})
        self.assertIn("Form 10-K for the fiscal year ended 2025-12-31 (its period of report)", prompt)
        self.assertIn("never against the day the filing was fetched", prompt)
        self.assertNotIn("retrieved on", prompt)

    def test_every_statement_of_a_filing_is_filed_under_its_period(self) -> None:
        from types import SimpleNamespace

        from dalton_core.document_extraction import DocumentExtractionService

        service = DocumentExtractionService(SimpleNamespace())
        context = {"source_ref": "source:sec-edgar", **filing_dating("source:sec-edgar", EPAM_URL, EPAM_COVER)}
        check = service.admission_quality_check(context)
        for period in ("FY2026 filing (as of 2026-09-08)", "as of 2026-09-08", "2025", "through 2049"):
            suggestion, hold = check({"normalized_statement": "EPAM's Americas revenue grew in 2025.",
                                      "period": period, "citation": {"raw_text": "Americas revenue grew."}})
            self.assertEqual(suggestion["period"], "FY2025 (fiscal year ended 2025-12-31)")
            self.assertIsNone(hold)


class EarlierConclusionTests(unittest.TestCase):
    """The reading queue's side of the DXC re-read: an open review whose document was read."""

    def test_an_open_review_is_closed_on_the_earlier_conclusion_without_a_read(self) -> None:
        import sqlite3
        from types import SimpleNamespace

        from dalton_core.document_admission_scope import DocumentAdmissionOutcomes
        from dalton_core.document_extraction_cli import _carry_earlier_conclusion

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        store = SimpleNamespace(connection=connection)
        first = {"review_id": "mission-document-review:v26", "document_ref": "guidepoint-excerpt:sha256:d50efc",
                 "company_ref": "company:dxc", "source_ref": "source:guidepoint",
                 "mission_version_ref": "coverage-mission-version:us-it-services:26"}
        again = {**first, "review_id": "mission-document-review:v28",
                 "mission_version_ref": "coverage-mission-version:us-it-services:28"}
        resolved = []

        def resolve(review_id, **kwargs):
            resolved.append((review_id, kwargs))
            return {"state": kwargs["resolution"]}

        host = SimpleNamespace(store=store, coverage_mission=SimpleNamespace(resolve_document_review=resolve))
        self.assertIsNone(_carry_earlier_conclusion(host, again, "hash", "automation:coverage-mission"))
        DocumentAdmissionOutcomes(store).record(
            review=first, candidate_claim_ref="candidate-claim-version:first",
            claim_version_refs=["claim-version:a", "claim-version:b"], admitted=4, held=0,
            actor_ref="automation:coverage-mission")
        # A review never carries its own conclusion.
        self.assertIsNone(_carry_earlier_conclusion(host, first, "hash", "automation:coverage-mission"))
        entry = _carry_earlier_conclusion(host, again, "hash", "automation:coverage-mission")
        self.assertEqual((entry["status"], entry["from_review_id"]),
                         ("extraction_staged", "mission-document-review:v26"))
        [(review_id, kwargs)] = resolved
        self.assertEqual((review_id, kwargs["candidate_claim_version_ref"], kwargs["expected_review_hash"]),
                         ("mission-document-review:v28", "candidate-claim-version:first", "hash"))
        self.assertIn("4 Claim(s) admitted", kwargs["rationale"])
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM document_admission_outcomes")


if __name__ == "__main__":
    unittest.main()
