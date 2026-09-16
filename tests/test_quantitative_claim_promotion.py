"""C2-4: filed numbers become quantitative Claims, anchored to the filed row.

The live shape (2026-09-16): 6,390 Claim versions, 22 of them quantitative and
all 22 the same measure; 16,903 filed statement lines and 8 verified document
figures that had never become a Claim at all.  The Dossier's
``numbers_without_refs`` failure and the conviction call's
``market_view_not_supported_by_cited_rows`` failure are both that gap.

The fixture rows below are copied from the live IBM 10-Q ingest
(``0000051143-26-000078``, period 2025-01-01..2025-06-30) so the normalisation
is tested against numbers that were actually filed.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dalton_core.claim_index_tagging import quantitative_aspect
from dalton_core.quantitative_claim_promotion import (
    CONCEPT_METRICS,
    FILED_BASIS,
    QuantitativeClaimPromotionError,
    QuantitativeClaimPromotionLedger,
    canonical_decimal,
    derived_ratio_proposals,
    document_figure_proposal,
    metric_for_line,
    period_wire,
    promotion_id_for,
    statement_line_proposal,
    statement_line_proposals,
    unit_wire,
)
from dalton_core.store import authorization_flag, content_hash

FILING = {
    "ingest_id": "mission-statement-ingest:ibm-2026q2",
    "company_ref": "company:sec-cik:0000051143",
    "cik": "0000051143",
    "entity_name": "International Business Machines Corporation",
    "accession": "0000051143-26-000078",
    "form": "10-Q",
    "filed": "2026-07-23",
    "report_date": "2026-06-30",
    "line_count": 483,
    "source_record_refs_json": '["connector-source-record:sec-xbrl-ibm-2026q2"]',
    "content_hash": "c" * 64,
    "recorded_at": "2026-07-24T00:00:00+00:00",
}


def line(**overrides):
    base = {
        "line_id": "mission-statement-line:ibm-1",
        "ingest_id": FILING["ingest_id"],
        "statement": "income",
        "ordinal": 1,
        "concept": "us-gaap:Revenues",
        "label": "Total revenue",
        "level": 0,
        "parent_concept": None,
        "is_breakdown": 0,
        "dimension_axis": None,
        "dimension_member": None,
        "dimension_count": None,
        "period_start": "2025-01-01",
        "period_end": "2025-06-30",
        "value": "32500000000",
        "unit": "usd",
        "balance": None,
    }
    base.update(overrides)
    return base


class NormalisationTests(unittest.TestCase):
    def test_a_filed_integer_keeps_every_digit(self) -> None:
        self.assertEqual(canonical_decimal("32500000000"), "32500000000")

    def test_a_filed_negative_stays_negative(self) -> None:
        self.assertEqual(canonical_decimal("-204000000"), "-204000000")

    def test_an_exponent_spelling_is_the_same_number(self) -> None:
        self.assertEqual(canonical_decimal("1.8008E+10"), "18008000000")

    def test_a_row_with_no_number_is_refused_rather_than_guessed(self) -> None:
        for value in (None, "", "  ", "n/a", True):
            self.assertIsNone(canonical_decimal(value), value)

    def test_a_flow_names_its_window_and_a_balance_names_its_date(self) -> None:
        self.assertEqual(period_wire("2025-01-01", "2025-06-30"), "2025-01-01..2025-06-30")
        self.assertEqual(period_wire(None, "2025-06-30"), "as of 2025-06-30")
        self.assertIsNone(period_wire("2025-01-01", None))

    def test_a_unit_nobody_wrote_down_is_refused_rather_than_invented(self) -> None:
        self.assertEqual(unit_wire("usd"), ("USD", "USD"))
        self.assertEqual(unit_wire("usdPerShare"), ("USD per share", "USD"))
        self.assertEqual(unit_wire("shares"), ("shares", None))
        self.assertIsNone(unit_wire("bushels"))

    def test_every_promoted_name_is_recognised_by_the_index_rule_tagger(self) -> None:
        # If a name falls through to ``other`` the index cannot place the
        # Claim, and the whole path quietly needs a model again.
        for metric, _label in CONCEPT_METRICS.values():
            self.assertNotEqual(quantitative_aspect(metric), "other", metric)
        for metric in ("segment revenue: Software", "gross margin", "operating margin"):
            self.assertNotEqual(quantitative_aspect(metric), "other", metric)


class StatementLineProposalTests(unittest.TestCase):
    def test_a_filed_revenue_line_becomes_an_anchored_quantitative_claim(self) -> None:
        proposal = statement_line_proposal(line(), FILING)
        self.assertEqual(proposal["metric_or_aspect"], "revenue")
        self.assertEqual(proposal["claim_kind"], "quantitative")
        self.assertEqual(proposal["value"], "32500000000")
        self.assertEqual((proposal["unit"], proposal["currency"], proposal["scale"]),
                         ("USD", "USD", "one"))
        self.assertEqual(proposal["period"], "2025-01-01..2025-06-30")
        self.assertEqual(proposal["basis"], FILED_BASIS)
        self.assertEqual(proposal["source_document_ref"],
                         "sec:filing:0000051143-26-000078")
        # Every byte a reader needs to walk it back to the SEC.
        anchor = proposal["anchor"]
        self.assertEqual(anchor["line_id"], "mission-statement-line:ibm-1")
        self.assertEqual(anchor["accession"], "0000051143-26-000078")
        self.assertEqual(anchor["concept"], "us-gaap:Revenues")
        self.assertEqual(anchor["ordinal"], 1)
        self.assertEqual(anchor["filing_content_hash"], "c" * 64)
        self.assertEqual(anchor["source_record_refs"],
                         ["connector-source-record:sec-xbrl-ibm-2026q2"])
        self.assertEqual(anchor["value_as_filed"], "32500000000")

    def test_the_statement_a_person_reads_names_the_filing_and_the_row(self) -> None:
        statement = statement_line_proposal(line(), FILING)["normalized_statement"]
        self.assertIn("International Business Machines Corporation", statement)
        self.assertIn("10-Q", statement)
        self.assertIn("0000051143-26-000078", statement)
        self.assertIn("us-gaap:Revenues", statement)
        self.assertIn("营业收入", statement)
        self.assertIn("未经任何换算", statement)

    def test_a_dimensioned_line_is_a_segment_number_not_the_group_number(self) -> None:
        proposal = statement_line_proposal(line(
            line_id="mission-statement-line:ibm-2", ordinal=2,
            concept="us-gaap:Revenues", label="Software",
            dimension_axis="srt:ProductOrServiceAxis",
            dimension_member="ibm:SoftwareMember", is_breakdown=1,
            value="7500000000",
        ), FILING)
        self.assertEqual(proposal["metric_or_aspect"], "segment revenue: Software")
        self.assertIn("分部", proposal["normalized_statement"])
        self.assertEqual(quantitative_aspect(proposal["metric_or_aspect"]),
                         "segments_and_mix")

    def test_an_unmapped_concept_is_silence_rather_than_a_guess(self) -> None:
        self.assertIsNone(metric_for_line(line(concept="ibm:ExpenseAndIncomeOther")))
        self.assertIsNone(statement_line_proposal(
            line(concept="ibm:ExpenseAndIncomeOther"), FILING))

    def test_diluted_eps_keeps_its_per_share_unit(self) -> None:
        proposal = statement_line_proposal(line(
            line_id="mission-statement-line:ibm-eps", ordinal=9,
            concept="us-gaap:EarningsPerShareDiluted", label="Diluted",
            value="3.47", unit="usdPerShare",
        ), FILING)
        self.assertEqual(proposal["metric_or_aspect"], "diluted eps")
        self.assertEqual((proposal["value"], proposal["unit"], proposal["currency"]),
                         ("3.47", "USD per share", "USD"))


class DerivedRatioTests(unittest.TestCase):
    def _consolidated(self):
        rows = [
            line(line_id="l:rev", ordinal=1, concept="us-gaap:Revenues",
                 label="Total revenue", value="32500000000"),
            line(line_id="l:gp", ordinal=2, concept="us-gaap:GrossProfit",
                 label="Gross profit", value="18008000000"),
            line(line_id="l:oi", ordinal=3, concept="us-gaap:OperatingIncomeLoss",
                 label="Operating income", value="3900000000"),
        ]
        return [statement_line_proposal(row, FILING) for row in rows]

    def test_both_margins_are_computed_from_two_lines_of_one_filing(self) -> None:
        # WP-F: a margin enters the Ledger as a *ratio*, not as a percentage.
        # ``verify_numeric_spec`` fixes the ``ratio`` operator's output at
        # unit "ratio", and a percentage would be a number no verifier can
        # recompute. The sentence a person reads still says 55.41%.
        derived = derived_ratio_proposals(self._consolidated(), FILING)
        by_metric = {item["metric_or_aspect"]: item for item in derived}
        self.assertEqual(set(by_metric), {"gross margin", "operating margin"})
        self.assertEqual(by_metric["gross margin"]["value"], "0.554092")
        self.assertEqual(by_metric["gross margin"]["unit"], "ratio")
        self.assertIsNone(by_metric["gross margin"]["currency"])
        self.assertIn("55.41%", by_metric["gross margin"]["normalized_statement"])
        self.assertEqual(
            Decimal(by_metric["operating margin"]["value"]),
            (Decimal("3900000000") / Decimal("32500000000")).quantize(
                Decimal("0.000001")))

    def test_the_derivation_names_both_rows_it_divided(self) -> None:
        derived = derived_ratio_proposals(self._consolidated(), FILING)
        anchor = derived[0]["anchor"]
        self.assertEqual(anchor["operation"], "numerator / denominator")
        self.assertEqual(anchor["rounding"], {"mode": "half_up", "digits": 6})
        self.assertEqual(anchor["numerator"]["line_id"], "l:gp")
        self.assertEqual(anchor["denominator"]["line_id"], "l:rev")
        self.assertEqual(derived[0]["numerator_ref"], "l:gp")
        self.assertEqual(derived[0]["denominator_ref"], "l:rev")

    def test_a_segment_profit_is_never_divided_by_group_revenue(self) -> None:
        segment = statement_line_proposal(line(
            line_id="l:seg-gp", ordinal=4, concept="us-gaap:GrossProfit",
            label="Software", dimension_axis="srt:ProductOrServiceAxis",
            dimension_member="ibm:SoftwareMember", value="5000000000",
        ), FILING)
        revenue = statement_line_proposal(line(line_id="l:rev", value="32500000000"), FILING)
        self.assertEqual(derived_ratio_proposals([segment, revenue], FILING), [])

    def test_a_missing_denominator_produces_nothing_rather_than_zero(self) -> None:
        only_profit = [statement_line_proposal(line(
            line_id="l:gp", concept="us-gaap:GrossProfit", label="Gross profit",
            value="18008000000"), FILING)]
        self.assertEqual(derived_ratio_proposals(only_profit, FILING), [])


class DocumentFigureProposalTests(unittest.TestCase):
    FIGURE = {
        "figure_id": "mission-document-figure:bookings-1",
        "company_ref": "company:sec-cik:0001467373",
        "review_ref": "mission-document-review:abc",
        "document_ref": "alphaengine:doc:123",
        "source_manifest_hash": "d" * 64,
        "quote_id": "quote:40800:42000:233a4436b95b89d5",
        "citation_text": "New bookings of $21.1 billion for the quarter",
        "metric_ref": "bookings",
        "as_reported_label": "New bookings",
        "period": "2026Q3",
        "value": "21100000000",
        "unit": "USD",
        "currency": "USD",
        "scale": "one",
        "basis": "company-filed-document",
        "source_grade": "company-filed-document",
        "verified_by": "verifier:document-figure:0.1",
        "observed_by": "automation:coverage-mission",
        "created_at": "2026-09-10T00:00:00+00:00",
        "content_hash": "e" * 64,
    }

    def test_a_company_published_figure_carries_its_quoted_bytes(self) -> None:
        proposal = document_figure_proposal(self.FIGURE)
        self.assertEqual(proposal["metric_or_aspect"], "bookings")
        self.assertEqual(proposal["value"], "21100000000")
        self.assertEqual(proposal["anchor"]["quote_id"],
                         "quote:40800:42000:233a4436b95b89d5")
        self.assertEqual(
            proposal["anchor"]["citation_hash"],
            content_hash({"quote_id": self.FIGURE["quote_id"],
                          "raw_text": self.FIGURE["citation_text"]}),
        )
        self.assertIn("逐位校验", proposal["normalized_statement"])

    def test_a_number_someone_said_on_a_call_is_never_promoted(self) -> None:
        spoken = {**self.FIGURE, "source_grade": "earnings-call-transcript"}
        self.assertIsNone(document_figure_proposal(spoken))


class PromotionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(str(Path(self.temp.name) / "core.sqlite"))
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.ledger = QuantitativeClaimPromotionLedger(self.connection)
        self.proposal = statement_line_proposal(line(), FILING)

    def test_one_number_is_promoted_once_however_often_it_is_swept(self) -> None:
        first = self.ledger.record(self.proposal, disposition="staged")
        self.assertEqual(first["status"], "fresh")
        second = self.ledger.record(self.proposal, disposition="staged")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.ledger.counts(), {"staged": 1})

    def test_the_identity_is_company_measure_period_and_the_row(self) -> None:
        other_period = statement_line_proposal(
            line(line_id="l:q3", period_start="2025-07-01", period_end="2025-09-30"),
            FILING)
        self.assertNotEqual(promotion_id_for(self.proposal),
                            promotion_id_for(other_period))
        self.ledger.record(self.proposal, disposition="staged")
        self.assertEqual(self.ledger.record(other_period, disposition="staged")["status"],
                         "fresh")

    def test_a_blocked_number_records_the_door_it_is_waiting_at(self) -> None:
        record = self.ledger.record(
            self.proposal, disposition="blocked", reason="staging chain is not built")
        self.assertEqual(record["disposition"], "blocked")
        self.assertEqual(record["reason"], "staging chain is not built")

    def test_an_invented_disposition_is_refused(self) -> None:
        with self.assertRaises(QuantitativeClaimPromotionError):
            self.ledger.record(self.proposal, disposition="probably_fine")

    def test_the_table_refuses_an_unauthorised_writer(self) -> None:
        flag = authorization_flag(
            self.connection, "dalton_quantitative_claim_promotion_authorized")
        flag.authorized = False
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO quantitative_claim_promotions("
                "promotion_id,company_ref,metric_or_aspect,period,origin_kind,origin_ref,"
                "origin_hash,source_document_ref,value,unit,scale,normalized_statement,"
                "disposition,content_hash,created_at) "
                "VALUES('p','c','revenue','2025','statement_line','l','h','d','1','USD',"
                "'one','s','staged','h','t')")

    def test_a_promotion_can_never_be_deleted(self) -> None:
        self.ledger.record(self.proposal, disposition="staged")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM quantitative_claim_promotions")


class StatementSweepTests(unittest.TestCase):
    """The sweep over a Core that holds real filed rows."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(str(Path(self.temp.name) / "core.sqlite"))
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE coverage_mission_statement_filings ("
            "ingest_id TEXT PRIMARY KEY, company_ref TEXT, cik TEXT, entity_name TEXT,"
            "accession TEXT, form TEXT, filed TEXT, report_date TEXT, line_count INTEGER,"
            "source_record_refs_json TEXT, content_hash TEXT, recorded_at TEXT);"
            "CREATE TABLE coverage_mission_statement_lines ("
            "line_id TEXT PRIMARY KEY, ingest_id TEXT, statement TEXT, ordinal INTEGER,"
            "concept TEXT, label TEXT, level INTEGER, parent_concept TEXT,"
            "is_breakdown INTEGER, dimension_axis TEXT, dimension_member TEXT,"
            "dimension_count INTEGER, period_start TEXT, period_end TEXT, value TEXT,"
            "unit TEXT, balance TEXT);"
        )
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(FILING[key] for key in (
                "ingest_id", "company_ref", "cik", "entity_name", "accession", "form",
                "filed", "report_date", "line_count", "source_record_refs_json",
                "content_hash", "recorded_at")),
        )
        for row in (
            line(line_id="l:rev", ordinal=1, value="32500000000"),
            line(line_id="l:gp", ordinal=2, concept="us-gaap:GrossProfit",
                 label="Gross profit", value="18008000000"),
            line(line_id="l:other", ordinal=3, concept="ibm:ExpenseAndIncomeOther",
                 label="Other", value="-204000000"),
            line(line_id="l:novalue", ordinal=4, concept="us-gaap:NetIncomeLoss",
                 label="Net income", value=None),
        ):
            self.connection.execute(
                "INSERT INTO coverage_mission_statement_lines VALUES("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in (
                    "line_id", "ingest_id", "statement", "ordinal", "concept", "label",
                    "level", "parent_concept", "is_breakdown", "dimension_axis",
                    "dimension_member", "dimension_count", "period_start", "period_end",
                    "value", "unit", "balance")),
            )
        self.connection.commit()

    def test_the_sweep_promotes_the_mapped_lines_and_the_margin(self) -> None:
        proposals = statement_line_proposals(self.connection)
        names = sorted(item["metric_or_aspect"] for item in proposals)
        self.assertEqual(names, ["gross margin", "gross profit", "revenue"])
        self.assertTrue(all(item["claim_kind"] == "quantitative" for item in proposals))

    def test_a_second_sweep_is_the_same_set_and_the_ledger_absorbs_it(self) -> None:
        ledger = QuantitativeClaimPromotionLedger(self.connection)
        first = [ledger.record(item, disposition="blocked", reason="waiting")
                 for item in statement_line_proposals(self.connection)]
        self.assertEqual({item["status"] for item in first}, {"fresh"})
        second = [ledger.record(item, disposition="blocked", reason="waiting")
                  for item in statement_line_proposals(self.connection)]
        self.assertEqual({item["status"] for item in second}, {"duplicate"})

    def test_a_new_filing_adds_only_its_own_numbers(self) -> None:
        ledger = QuantitativeClaimPromotionLedger(self.connection)
        for item in statement_line_proposals(self.connection):
            ledger.record(item, disposition="blocked", reason="waiting")
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mission-statement-ingest:ibm-2026q3", FILING["company_ref"], FILING["cik"],
             FILING["entity_name"], "0000051143-26-000099", "10-Q", "2026-10-22",
             "2026-09-30", 400, FILING["source_record_refs_json"], "f" * 64,
             "2026-10-23T00:00:00+00:00"),
        )
        row = line(line_id="l:rev-q3", ingest_id="mission-statement-ingest:ibm-2026q3",
                   period_start="2025-07-01", period_end="2025-09-30", value="17000000000")
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row[key] for key in (
                "line_id", "ingest_id", "statement", "ordinal", "concept", "label",
                "level", "parent_concept", "is_breakdown", "dimension_axis",
                "dimension_member", "dimension_count", "period_start", "period_end",
                "value", "unit", "balance")),
        )
        self.connection.commit()
        fresh = [ledger.record(item, disposition="blocked", reason="waiting")
                 for item in statement_line_proposals(self.connection)]
        self.assertEqual(sum(1 for item in fresh if item["status"] == "fresh"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
