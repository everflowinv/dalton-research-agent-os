"""P11w: a stored figure has passed its own citation check, and says its grade."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionValidationError,
)
from dalton_core.document_figure_grade import FILED, SPOKEN
from dalton_core.store import DaltonStore

COMPANY = "company:sec-cik:0001467373"
ACTOR = "automation:dalton-mission"
QUOTE = "quote:0:200:aaaaaaaaaaaaaaaa"
CITATION = "Net revenues were $17.7 billion and new bookings were $21.3 billion."


def figure(**overrides):
    base = {
        "quote_id": QUOTE,
        "metric_ref": "metric:revenue",
        "as_reported_label": "Net revenues",
        "value": "17.7",
        "unit": "currency",
        "currency": "USD",
        "period": "FY2026Q3",
        "basis": "gaap-reported",
        "scale": "billion",
        "citation_text": CITATION,
    }
    base.update(overrides)
    return base


class DocumentFigureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, *figures, grade=FILED, document_ref="sec:filing:0001467373-25-000217"):
        return self.authority.record_document_figures(
            company_ref=COMPANY, review_ref="mission-document-review:1",
            document_ref=document_ref, source_manifest_hash="0" * 64,
            source_grade=grade, figures=list(figures), observed_by=ACTOR,
        )

    def test_a_verified_figure_is_stored_with_what_it_cited(self):
        self.assertEqual(self.record(figure())["recorded"], ["metric:revenue"])
        [held] = self.authority.document_figures(COMPANY)
        self.assertEqual((held["value"], held["unit"], held["scale"]),
                         ("17.7", "currency", "billion"))
        self.assertEqual(held["as_reported_label"], "Net revenues")
        # The quote and the manifest hash are stored so the check that let this
        # figure in can be run again by someone who does not trust it.
        self.assertEqual(held["citation_text"], CITATION)
        self.assertEqual(held["source_manifest_hash"], "0" * 64)
        self.assertTrue(held["verified_by"].startswith("verifier:document-figure"))

    def test_a_number_its_citation_does_not_contain_never_reaches_the_table(self):
        # The check is re-run here rather than trusted from the caller, so a
        # caller that forgot to verify cannot write an unverified row.
        with self.assertRaises(CoverageMissionValidationError) as caught:
            self.record(figure(value="18.2"))
        self.assertIn("not supported by the quote", str(caught.exception))
        self.assertEqual(self.authority.document_figures(COMPANY), [])

    def test_a_label_its_citation_does_not_contain_is_refused_too(self):
        with self.assertRaises(CoverageMissionValidationError):
            self.record(figure(as_reported_label="Total revenue"))
        self.assertEqual(self.authority.document_figures(COMPANY), [])

    def test_a_figure_without_its_quote_is_refused(self):
        bare = figure()
        bare.pop("citation_text")
        with self.assertRaises(CoverageMissionValidationError):
            self.record(bare)

    def test_the_same_figure_filed_and_spoken_is_two_rows(self):
        # This is the whole point: both are kept, and they are told apart.
        self.record(figure(), grade=FILED)
        self.record(figure(), grade=SPOKEN, document_ref="alphaengine-doc:call-1")
        grades = sorted(item["source_grade"] for item in self.authority.document_figures(COMPANY))
        self.assertEqual(grades, sorted([FILED, SPOKEN]))

    def test_a_model_that_should_only_stand_on_filed_figures_can_ask_for_those(self):
        self.record(figure(), grade=FILED)
        self.record(figure(), grade=SPOKEN, document_ref="alphaengine-doc:call-1")
        filed = self.authority.document_figures(COMPANY, source_grade=FILED)
        self.assertEqual([item["source_grade"] for item in filed], [FILED])
        self.assertEqual(filed[0]["document_ref"], "sec:filing:0001467373-25-000217")
        # And the spoken one was not discarded to achieve that.
        self.assertEqual(len(self.authority.document_figures(COMPANY, source_grade=SPOKEN)), 1)

    def test_the_same_document_read_twice_stores_one_figure(self):
        self.record(figure())
        again = self.record(figure())
        self.assertEqual((again["recorded"], again["duplicates"]), ([], ["metric:revenue"]))
        self.assertEqual(len(self.authority.document_figures(COMPANY)), 1)

    def test_a_grade_nobody_defined_is_refused(self):
        for grade in (None, "", "probably-fine", "sell-side-reports"):
            with self.assertRaises(CoverageMissionValidationError):
                self.record(figure(), grade=grade)
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.document_figures(COMPANY, source_grade="probably-fine")

    def test_a_second_period_of_the_same_metric_is_its_own_figure(self):
        self.record(figure())
        other = self.record(figure(
            value="21.3", as_reported_label="new bookings",
            metric_ref="metric:new-bookings", period="FY2026Q3",
        ))
        self.assertEqual(other["recorded"], ["metric:new-bookings"])
        self.assertEqual(len(self.authority.document_figures(COMPANY)), 2)
        self.assertEqual(
            len(self.authority.document_figures(COMPANY, metric_ref="metric:revenue")), 1)

    def test_figures_cannot_be_edited_deleted_or_written_from_outside(self):
        self.record(figure())
        for statement in (
            "UPDATE coverage_mission_document_figures SET value='99'",
            "DELETE FROM coverage_mission_document_figures",
        ):
            with self.assertRaises(Exception):
                self.store.connection.execute(statement)
        with self.assertRaises(Exception):
            self.store.connection.execute(
                "INSERT INTO coverage_mission_document_figures("
                "figure_id,company_ref,review_ref,document_ref,source_manifest_hash,"
                "quote_id,citation_text,metric_ref,as_reported_label,period,value,unit,"
                "currency,scale,basis,source_grade,verified_by,observed_by,created_at,"
                "content_hash) VALUES('x',?,'r','d','h','q','c','metric:x','x','p','1',"
                "'currency',NULL,NULL,'b',?,'v','a','t','h')",
                (COMPANY, FILED),
            )

    def test_one_bad_figure_is_refused_before_a_good_one_beside_it_is_written(self):
        with self.assertRaises(CoverageMissionValidationError):
            self.record(figure(), figure(metric_ref="metric:new-bookings",
                                         as_reported_label="new bookings", value="99.9"))
        self.assertEqual(self.authority.document_figures(COMPANY), [])


if __name__ == "__main__":
    unittest.main()
