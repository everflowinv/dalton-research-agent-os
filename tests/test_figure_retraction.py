"""P12i: a figure that should never have been recorded is withdrawn, with a reason.

Two of the first figures this system stored were "EPAM revenue = 14.3 billion
RMB" (a Haier European-business call) and "revenue of AUD 169 million" (an EOS
call). The digits were verified against the bytes they cited; the bytes were
about another company. A third was ACN's fiscal-2025 revenue recorded twice
from two windows of one 10-K, kept apart only by "Fiscal 2025" against
"fiscal 2025".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionNotFound,
    _period_key,
)
from dalton_core.document_figure_grade import FILED
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
CITATION = "Revenues of $69.7 billion for fiscal 2025, and 270 clients."


def figure(**overrides):
    base = {
        "quote_id": "quote:0:1200:aaaaaaaaaaaaaaaa",
        "metric_ref": "metric:revenue", "subject_as_named": "Accenture",
        "as_reported_label": "Revenues",
        "value": "69.7", "unit": "currency", "currency": "USD",
        "period": "fiscal 2025", "basis": "gaap-reported", "scale": "billion",
        "citation_text": CITATION,
    }
    base.update(overrides)
    return base


class PeriodKeyTests(unittest.TestCase):
    def test_the_same_period_spelled_two_ways_is_one_period(self):
        self.assertEqual(_period_key("Fiscal 2025"), _period_key("fiscal 2025"))
        self.assertEqual(_period_key(" FY2026 Q3 "), _period_key("fy2026-q3"))

    def test_different_periods_stay_different(self):
        self.assertNotEqual(_period_key("fiscal 2025"), _period_key("fiscal 2024"))


class RetractionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, *figures, document_ref="sec:filing:1"):
        return self.authority.record_document_figures(
            company_ref=ACN, review_ref="mission-document-review:1",
            document_ref=document_ref, source_manifest_hash="0" * 64,
            source_grade=FILED, figures=list(figures), observed_by="automation:x",
        )

    def figure_id(self):
        return self.authority.document_figures(ACN)[0]["figure_id"]

    def test_one_fact_found_in_two_windows_is_recorded_once(self):
        # The ACN duplicate: same document, same fact, two windows, and the
        # period spelled differently in each.
        self.record(figure(period="Fiscal 2025", quote_id="quote:40800:42000:a"))
        again = self.record(figure(period="fiscal 2025", quote_id="quote:186000:187200:b"))
        self.assertEqual(again["recorded"], [])
        self.assertEqual(len(self.authority.document_figures(ACN)), 1)

    def test_a_different_period_in_the_same_document_is_its_own_figure(self):
        self.record(figure(period="fiscal 2025"))
        other = self.record(figure(period="fiscal 2024", value="64.1",
                                   citation_text="Revenues of $64.1 billion in fiscal 2024."))
        self.assertEqual(other["recorded"], ["metric:revenue"])
        self.assertEqual(len(self.authority.document_figures(ACN)), 2)

    def test_a_retracted_figure_is_returned_by_no_read(self):
        self.record(figure())
        self.authority.retract_document_figure(
            self.figure_id(), reason="document is about another company",
            retracted_by="automation:x")
        self.assertEqual(self.authority.document_figures(ACN), [])
        self.assertEqual(self.authority.document_figures(ACN, source_grade=FILED), [])
        self.assertEqual(
            self.authority.document_figures(ACN, metric_ref="metric:revenue"), [])

    def test_the_reason_survives_so_the_mistake_stays_legible(self):
        self.record(figure())
        figure_id = self.figure_id()
        self.authority.retract_document_figure(
            figure_id, reason="a Haier call, not EPAM", retracted_by="automation:x")
        [withdrawn] = self.authority.retracted_document_figures()
        self.assertEqual(withdrawn["figure_id"], figure_id)
        self.assertIn("Haier", withdrawn["reason"])
        self.assertEqual(withdrawn["value"], "69.7")

    def test_retracting_twice_records_once(self):
        self.record(figure())
        figure_id = self.figure_id()
        first = self.authority.retract_document_figure(
            figure_id, reason="wrong company", retracted_by="automation:x")
        again = self.authority.retract_document_figure(
            figure_id, reason="wrong company", retracted_by="automation:x")
        self.assertEqual((first["status_marker"], again["status_marker"]),
                         ("fresh", "duplicate"))

    def test_a_figure_that_does_not_exist_cannot_be_retracted(self):
        with self.assertRaises(CoverageMissionNotFound):
            self.authority.retract_document_figure(
                "mission-document-figure:nope", reason="x", retracted_by="automation:x")

    def test_retractions_are_append_only_and_authority_only(self):
        self.record(figure())
        self.authority.retract_document_figure(
            self.figure_id(), reason="wrong company", retracted_by="automation:x")
        for statement in (
            "UPDATE coverage_mission_document_figure_retractions SET reason='x'",
            "DELETE FROM coverage_mission_document_figure_retractions",
            "INSERT INTO coverage_mission_document_figure_retractions("
            "figure_id,reason,retracted_by,retracted_at) VALUES('x','y','z','t')",
        ):
            with self.assertRaises(Exception):
                self.store.connection.execute(statement)


class ReRecordTests(RetractionTests):
    def test_a_withdrawn_figure_is_not_quietly_reinstated(self):
        self.record(figure())
        self.authority.retract_document_figure(
            self.authority.connection.execute(
                "SELECT figure_id FROM coverage_mission_document_figures").fetchone()[0],
            reason="document is about another company", retracted_by="automation:x")
        again = self.record(figure())
        self.assertEqual(again["recorded"], [])
        self.assertEqual(again["retracted"], ["metric:revenue"])
        self.assertEqual(again["duplicates"], [])
        self.assertEqual(self.authority.document_figures(ACN), [])

    def test_an_ordinary_duplicate_is_still_reported_as_one(self):
        self.record(figure())
        again = self.record(figure())
        self.assertEqual(again["duplicates"], ["metric:revenue"])
        self.assertEqual(again["retracted"], [])


if __name__ == "__main__":
    unittest.main()
