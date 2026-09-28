"""2026-09-28: one document, one number, one Claim -- under the filer's label.

ws-7d's META 2025 10-K entered the Ledger three times as revenue ("2025",
"full year 2025", "Year Ended December 31, 2025") and twice as net income, one
of them labelled "net income adjusted for certain non-cash items"; AMZN's
revenue twice, once labelled "Consolidated".  All from one connector
invocation, because a figure was keyed on its free-text period and its own row.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from decimal import Decimal

from dalton_core.document_figure_identity import (
    amount,
    duplicate_groups,
    figure_identity,
    normalize_period,
    row_label,
    same_amount,
)
from dalton_core.quantitative_claim_promotion import (
    document_figure_proposal,
    promotion_id_for,
)
from dalton_core.store import content_hash

META = "company:ticker:meta"
AMZN = "company:ticker:amzn"
META_10K = "0001628280-26-003942"
AMZN_10K = "0001018724-26-000004"
DEC = (12, 31)


class PeriodTests(unittest.TestCase):
    def test_the_spellings_of_one_fiscal_year_are_one_period(self) -> None:
        year = ("2025-01-01", "2025-12-31")
        for text in ("Year Ended December 31, 2025", "year ended December 31, 2025",
                     "Twelve months ended December 31, 2025", "fiscal year ended Dec. 31, 2025",
                     "Year Ended December 31, 2025 vs Year Ended December 31, 2024"):
            self.assertEqual(normalize_period(text), year, text)
        for text in ("2025", "full year 2025", "Full-year 2025", "FY2025", "FY25",
                     "fiscal 2025", "2025 compared to 2024"):
            self.assertEqual(normalize_period(text, fiscal_year_end=DEC), year, text)

    def test_a_fiscal_year_ends_where_the_filer_says(self) -> None:
        # ACN's year ends 31 August, DXC's 31 March, MSFT's 30 June.
        self.assertEqual(normalize_period("fiscal 2025", fiscal_year_end=(8, 31)),
                         ("2024-09-01", "2025-08-31"))
        self.assertEqual(normalize_period("Fiscal Year Ended March 31, 2026"),
                         ("2025-04-01", "2026-03-31"))
        self.assertEqual(normalize_period("FY2026", fiscal_year_end=(6, 30)),
                         ("2025-07-01", "2026-06-30"))
        self.assertEqual(normalize_period("Three months ended June 30, 2025"),
                         ("2025-04-01", "2025-06-30"))

    def test_what_cannot_be_dated_is_left_as_written(self) -> None:
        # No fiscal year end to anchor a bare year; a quarter with no year; a
        # retailer's January year end; a 13-week quarter ending on a Saturday.
        self.assertIsNone(normalize_period("2025"))
        for text in ("Q2", "first half", "current", "Q2 FY2027", ""):
            self.assertIsNone(normalize_period(text, fiscal_year_end=DEC), text)
        self.assertIsNone(normalize_period("fiscal 2025", fiscal_year_end=(1, 31)))
        self.assertIsNone(normalize_period("three months ended June 28, 2025"))


class AmountTests(unittest.TestCase):
    def test_one_amount_stated_to_two_precisions_is_one_amount(self) -> None:
        self.assertTrue(same_amount(amount("200.97", "billion"), amount("200966", "million")))
        self.assertTrue(same_amount(amount("60.46", "billion"), amount("60458000000", "one")))
        self.assertTrue(same_amount(amount("716924", "million"), amount("716,924", "million")))
        self.assertFalse(same_amount(amount("201.5", "billion"), amount("200966", "million")))
        self.assertFalse(same_amount(amount("5.28", None), amount("5.29", None)))
        self.assertIsNone(amount("12", "gazillion"))
        self.assertEqual(amount("200.97", "billion"), (Decimal("200970000000"), 7))


AMZN_TABLE = ("North America\n\nNet sales\n\n$\n\n352,828\n\n$\n\n387,497\n\n$\n\n426,305\n\n"
              "Operating expenses\n\n337,951\n\n362,530\n\n396,686")
META_PROSE = ("Cash provided by operating activities during 2025 mostly consisted of $60.46 "
              "billion net income adjusted for certain non-cash items, such as $20.43 billion")
ACN_TOTAL = "Asia Pacific (3)\n\n1,810\n\n63\n\nTotal\n\n$\n\n10,226\n\n$\n\n615\n\n15.6\n\n%"


class RowLabelTests(unittest.TestCase):
    def test_the_label_of_the_row_the_number_is_printed_on(self) -> None:
        self.assertEqual(row_label(AMZN_TABLE, "426305"), "Net sales")
        self.assertEqual(row_label(AMZN_TABLE, "352828"), "Net sales")
        self.assertEqual(row_label(AMZN_TABLE, "362530"), "Operating expenses")

    def test_a_sentence_and_a_row_that_names_nothing_give_no_label(self) -> None:
        self.assertIsNone(row_label(META_PROSE, "60.46"))
        self.assertIsNone(row_label(ACN_TOTAL, "15.6"))
        self.assertIsNone(row_label(None, "1"))


def core() -> sqlite3.Connection:
    """The two statement tables the identity reads, holding META's and AMZN's 10-Ks."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT, company_ref TEXT, "
        "accession TEXT, form TEXT, report_date TEXT);"
        "CREATE TABLE coverage_mission_statement_lines(line_id TEXT, ingest_id TEXT, "
        "statement TEXT, ordinal INTEGER, concept TEXT, label TEXT, dimension_axis TEXT, "
        "dimension_member TEXT, period_start TEXT, period_end TEXT, value TEXT, unit TEXT);")
    for ingest, company, accession in (("i-meta", META, META_10K), ("i-amzn", AMZN, AMZN_10K)):
        connection.execute("INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?)",
                           (ingest, company, accession, "10-K", "2025-12-31"))
    for line in (
        ("l1", "i-meta", "income", 261, "us-gaap:NetIncomeLoss", "Net income", "60458000000"),
        ("l2", "i-meta", "income", 262,
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue", "200966000000"),
        ("l3", "i-amzn", "income", 308,
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "Total net sales",
         "716924000000"),
    ):
        connection.execute(
            "INSERT INTO coverage_mission_statement_lines VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,?)",
            (*line[:6], "2025-01-01", "2025-12-31", line[6], "usd"))
    return connection


def figure(figure_id, company, accession, metric, label, period, value, scale, created,
           citation="") -> dict:
    return {"figure_id": figure_id, "company_ref": company, "document_ref": f"sec:filing:{accession}",
            "metric_ref": metric, "as_reported_label": label, "period": period, "value": value,
            "unit": "currency", "currency": "USD", "scale": scale,
            "source_grade": "company-filed-document", "quote_id": "quote:0:1:ab",
            "citation_text": citation, "content_hash": "h-" + figure_id, "created_at": created}


# ws-7d, 2026-09-28: the figures behind the duplicate Claims, as Core holds them.
WS7D = [
    figure("f-meta-ni-1", META, META_10K, "metric:net-income", "Net income",
           "year ended December 31, 2025", "60.46", "billion", "2026-09-17T16:21"),
    figure("f-meta-rev-1", META, META_10K, "metric:revenue", "Total revenue for 2025", "2025",
           "200.97", "billion", "2026-09-18T05:58"),
    figure("f-meta-rev-2", META, META_10K, "metric:revenue", "Total revenue for 2025",
           "full year 2025", "200.97", "billion", "2026-09-18T09:10"),
    figure("f-amzn-rev-1", AMZN, AMZN_10K, "metric:revenue", "Net sales",
           "Year Ended December 31, 2025", "716924", "million", "2026-09-18T10:23", AMZN_TABLE),
    figure("f-meta-rev-3", META, META_10K, "metric:revenue", "Revenue",
           "Year Ended December 31, 2025", "200966", "million", "2026-09-18T10:38"),
    figure("f-amzn-rev-2", AMZN, AMZN_10K, "metric:revenue", "Consolidated", "2025",
           "716924", "million", "2026-09-18T11:36"),
    figure("f-meta-ni-2", META, META_10K, "metric:net-income",
           "net income adjusted for certain non-cash items", "2025", "60.46", "billion",
           "2026-09-25T07:56", META_PROSE),
    # A different number for the same period is not a duplicate.
    figure("f-amzn-ni", AMZN, AMZN_10K, "metric:net-income", "Net income",
           "Year Ended December 31, 2025", "77670", "million", "2026-09-23T14:26"),
]


class DuplicateTests(unittest.TestCase):
    def test_one_documents_spellings_of_one_number_collapse_to_one(self) -> None:
        duplicates, identities = duplicate_groups(core(), WS7D)
        self.assertEqual(duplicates, {
            # META revenue: the one the filed line confirms at its precision.
            "f-meta-rev-1": "f-meta-rev-3", "f-meta-rev-2": "f-meta-rev-3",
            # META net income: both confirmed; the earlier is kept.
            "f-meta-ni-2": "f-meta-ni-1",
            "f-amzn-rev-2": "f-amzn-rev-1",
        })
        self.assertEqual(identities["f-meta-rev-1"]["period"], "2025-01-01..2025-12-31")

    def test_the_label_is_the_filers_not_the_models(self) -> None:
        connection = core()
        labels = {item["figure_id"]: figure_identity(connection, item) for item in WS7D}
        self.assertEqual((labels["f-meta-ni-2"]["label"], labels["f-meta-ni-2"]["label_source"]),
                         ("Net income", "filed_concept"))
        self.assertEqual(labels["f-amzn-rev-2"]["label"], "Total net sales")
        self.assertEqual(labels["f-meta-rev-1"]["label"], "Revenue")
        # No filed line for it: the document's own row label, then the model's words.
        orphan = dict(WS7D[3], document_ref="alphaengine-doc:1", figure_id="x")
        self.assertEqual(figure_identity(connection, orphan)["label"], "Net sales")
        prose = dict(WS7D[6], document_ref="alphaengine-doc:1", figure_id="y")
        self.assertEqual(figure_identity(connection, prose)["label_source"], "as_reported")

    def test_one_promotion_id_per_number_and_statement_lines_keep_theirs(self) -> None:
        connection = core()
        ids = {item["figure_id"]: promotion_id_for(
            document_figure_proposal(item, figure_identity(connection, item))) for item in WS7D}
        # Same amount, same precision: one key whatever the period's spelling.
        self.assertEqual(ids["f-meta-rev-1"], ids["f-meta-rev-2"])
        self.assertEqual(ids["f-meta-ni-1"], ids["f-meta-ni-2"])
        self.assertEqual(ids["f-amzn-rev-1"], ids["f-amzn-rev-2"])
        # 200.97 billion and 200966 million are one number at two precisions;
        # a key cannot round, so the sweep folds them first (duplicate_groups)
        # and only the kept figure is ever written down.
        self.assertNotEqual(ids["f-meta-rev-1"], ids["f-meta-rev-3"])
        self.assertNotEqual(ids["f-amzn-rev-1"], ids["f-amzn-ni"])
        proposal = document_figure_proposal(WS7D[6], figure_identity(connection, WS7D[6]))
        self.assertEqual(proposal["period"], "2025-01-01..2025-12-31")
        self.assertIn("「Net income」", proposal["normalized_statement"])
        # Every other kind keeps the key the 2,176 admitted rows were written under.
        line = {"origin_kind": "statement_line", "origin_ref": "line:1", "company_ref": META,
                "metric_or_aspect": "revenue", "period": "2025-01-01..2025-12-31"}
        self.assertEqual(promotion_id_for(line), "quantitative-claim-promotion:" + content_hash({
            "company_ref": META, "metric_or_aspect": "revenue",
            "period": "2025-01-01..2025-12-31", "origin_ref": "line:1"})[:32])
        # And so does a figure whose period did not normalise.
        loose = document_figure_proposal(dict(WS7D[1], document_ref="alphaengine-doc:1",
                                              period="Q2"), None)
        self.assertNotIn("same_document_key", loose)
        self.assertEqual(json.loads(json.dumps(loose))["period"], "Q2")


class FigureStagingTests(unittest.TestCase):
    """Through the real promoter, staging gate and auto-commit replay."""

    CITATION = ("Net revenues were $17.7 billion ($17,700 million) for the year ended "
                "December 31, 2025, and margin was 15.6 percent.")

    def setUp(self) -> None:
        from tests.test_figure_admission import FigureHarness

        self.harness = FigureHarness()
        self.addCleanup(self.harness.close)
        self.staging = self.harness.staging()
        self.addCleanup(self.staging.close)

    def test_three_spellings_stage_once_and_commit_with_dates(self) -> None:
        from dalton_core.claim_index_figures import promote_verified_figures
        from dalton_core.research_auto_commit import MISSION_VERIFIED_FIGURE_RULE_REF
        from tests.test_quantitative_claim_admission import VerifiedFigureAdmissionTests

        record = self.harness.record_figure
        record(period="Year Ended December 31, 2025", citation_text=self.CITATION)
        # (The write path already folds a case-only difference; these are not.)
        record(period="fiscal year ended December 31, 2025", citation_text=self.CITATION)
        record(period="twelve months ended December 31, 2025", value="17700",
               scale="million", citation_text=self.CITATION)
        # No filed line confirms any of them: the most precisely stated is kept.
        from dalton_core.claim_index_figures import DocumentFigureResolver

        [first] = [item for item in DocumentFigureResolver(
            self.harness.core.connection).figures() if item["scale"] == "million"]
        result = promote_verified_figures(
            self.harness.core, self.staging, actor_ref="automation:coverage-mission")
        self.assertEqual([item["figure_id"] for item in result["promoted"]],
                         [first["figure_id"]])
        self.assertEqual(sorted(item.get("duplicate_of") for item in result["skipped"]),
                         [first["figure_id"]] * 2)
        [staged] = result["results"]
        claim = staged["claim"]
        self.assertEqual(claim["period"], "2025-01-01..2025-12-31")
        self.assertIn("(reported as: twelve months ended December 31, 2025)",
                      claim["normalized_statement"])
        # The signed rule replays the candidate from Core and admits it as is.
        signer = VerifiedFigureAdmissionTests()
        signer.core = self.harness.core
        signer.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        committed = self.harness.core.commit_policy_candidate(
            evidence=staged["evidence"], claim=claim, material=staged["material"],
            source_verification=staged["source_verification"],
            numeric_verification=staged["numeric_verification"],
            idempotency_key="policy-ledger:" + claim["id"])
        self.assertEqual(committed["status"], "fresh")
        # Once in the Ledger, none of the three is staged again.
        again = promote_verified_figures(
            self.harness.core, self.staging, actor_ref="automation:coverage-mission",
            promoted={first["figure_id"]})
        self.assertEqual(again["promoted"], [])

    def test_the_promotion_lane_admits_one_and_never_restages_it(self) -> None:
        from pathlib import Path

        from dalton_core.quantitative_claim_promotion_cli import run_promotion
        from dalton_core.research_auto_commit import MISSION_VERIFIED_FIGURE_RULE_REF
        from tests.test_quantitative_claim_admission import VerifiedFigureAdmissionTests

        record = self.harness.record_figure
        record(period="Year Ended December 31, 2025", citation_text=self.CITATION)
        record(period="fiscal year ended December 31, 2025", citation_text=self.CITATION)
        signer = VerifiedFigureAdmissionTests()
        signer.core = self.harness.core
        signer.sign(MISSION_VERIFIED_FIGURE_RULE_REF)
        root = Path(self.harness._dir.name)

        def run(name):
            return run_promotion(state_dir=root, summary_dir=root / name,
                                 staging_db=self.harness.staging_path)

        def ledger():
            return self.harness.core.connection.execute(
                "SELECT disposition, COUNT(*) FROM quantitative_claim_promotions "
                "WHERE origin_kind='document_figure' GROUP BY disposition").fetchall()

        claims = self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        first = run("run-1")
        self.assertEqual((first["promoted"]["document_figure"], first["duplicate_figures"],
                          first["formal_authority_writes"]), (1, 1, 1), first)
        second = run("run-2")
        self.assertEqual((second["promoted"]["document_figure"], second["duplicate_figures"],
                          second["formal_authority_writes"], second["blocked"]),
                         (0, 1, 0, []), second)
        self.assertEqual([tuple(row) for row in ledger()], [("admitted", 1)])
        self.assertEqual(self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM claim_versions").fetchone()[0], claims + 1)


if __name__ == "__main__":
    unittest.main()
