"""P11x: figures reach the owner's page without waiting for Ledger admission.

They are held, citable and graded now.  Showing them only once the numeric
admission path exists would hide work already done, and the owner asked for
these numbers to be kept and available for reference rather than reviewed.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.cockpit_plane import FIGURE_GRADE_LABELS
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_figure_grade import FILED, SPOKEN
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"
CITATION = "Net revenues were $17.7 billion and new bookings were $21.3 billion."


def figure(**overrides):
    base = {
        "quote_id": "quote:0:200:aaaaaaaaaaaaaaaa",
        "metric_ref": "metric:revenue",
        "as_reported_label": "Net revenues",
        "value": "17.7", "unit": "currency", "currency": "USD",
        "period": "FY2026Q3", "basis": "gaap-reported", "scale": "billion",
        "citation_text": CITATION,
    }
    base.update(overrides)
    return base


class FigureProjectionTests(unittest.TestCase):
    """The projection itself, over a real figure journal."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "core.sqlite"
        self.store = DaltonStore(str(self.path))
        self.addCleanup(self.store.close)
        self.authority = CoverageMissionAuthority(self.store)

    def record(self, company, *figures, grade=FILED, document_ref="sec:filing:1"):
        self.authority.record_document_figures(
            company_ref=company, review_ref="mission-document-review:1",
            document_ref=document_ref, source_manifest_hash="0" * 64,
            source_grade=grade, figures=list(figures), observed_by="automation:x",
        )

    def project(self):
        from dalton_core.cockpit_plane import CockpitPlane

        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return CockpitPlane._figures(None, connection)
        finally:
            connection.close()

    def test_a_company_with_no_figures_is_absent_rather_than_wrong(self):
        self.assertEqual(self.project(), {})

    def test_figures_are_counted_by_grade_and_kept_apart(self):
        self.record(ACN, figure(), grade=FILED)
        self.record(ACN, figure(), grade=SPOKEN, document_ref="alphaengine-doc:call-1")
        entry = self.project()[ACN]
        self.assertEqual(entry["total"], 2)
        self.assertEqual(entry["by_grade"], {FILED: 1, SPOKEN: 1})

    def test_each_figure_carries_the_label_its_grade_means(self):
        self.record(ACN, figure(), grade=SPOKEN, document_ref="alphaengine-doc:call-1")
        [shown] = self.project()[ACN]["latest"]
        self.assertEqual(shown["grade"], SPOKEN)
        self.assertEqual(shown["grade_label"], FIGURE_GRADE_LABELS[SPOKEN])
        # The owner reads this label, so it has to say what the caveat is.
        self.assertIn("口述", shown["grade_label"])
        self.assertEqual((shown["value"], shown["currency"], shown["scale"]),
                         ("17.7", "USD", "billion"))

    def test_one_company_does_not_show_another_company_numbers(self):
        self.record(ACN, figure())
        self.record(IBM, figure(), document_ref="sec:filing:2")
        projected = self.project()
        self.assertEqual(projected[ACN]["total"], 1)
        self.assertEqual(projected[IBM]["total"], 1)

    def test_only_the_most_recent_few_are_shown_newest_first(self):
        for index in range(9):
            self.record(ACN, figure(metric_ref=f"metric:m-{index}",
                                    as_reported_label="new bookings", value="21.3"),
                        document_ref=f"sec:filing:{index}")
        entry = self.project()[ACN]
        self.assertEqual(entry["total"], 9)
        self.assertEqual(len(entry["latest"]), 6)
        self.assertEqual(entry["latest"][0]["metric_ref"], "metric:m-8")

    def test_a_core_without_the_table_yet_projects_nothing(self):
        # A deploy that has not run the migration must not break the page.
        bare = Path(self._dir.name) / "bare.sqlite"
        connection = sqlite3.connect(bare)
        connection.row_factory = sqlite3.Row
        try:
            from dalton_core.cockpit_plane import CockpitPlane

            self.assertEqual(CockpitPlane._figures(None, connection), {})
        finally:
            connection.close()


class PageTests(unittest.TestCase):
    def test_the_page_renders_figures_and_marks_the_spoken_ones(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "dalton_core"
                / "cockpit_control.html").read_text(encoding="utf-8")
        self.assertIn("figures(c.figures)", html)
        self.assertIn("GRADE_LABEL", html)
        # A spoken figure must be visually distinguishable from a filed one, or
        # the grade is stored and never seen.
        self.assertIn("tag.spoken", html)
        self.assertIn("tag.filed", html)
        for grade in FIGURE_GRADE_LABELS:
            self.assertIn(grade, html)


class SourceCapTests(unittest.TestCase):
    """P12c: the cap on the owner's page is the cap the system is running."""

    def lanes(self, budget):
        from dalton_core.cockpit_plane import CockpitPlane

        return {l["key"]: l for l in CockpitPlane._lane_states({}, {}, {}, budget)}

    def test_the_alphaengine_note_reads_the_live_budget(self):
        # It was the literal "每 24 小时最多 30 次", so the page said 30 for days
        # after the owner raised the cap to 130.
        self.assertIn("130", self.lanes({"max_alphaengine_calls_24h": 130})["alphaengine"]["note"])
        self.assertIn("50", self.lanes({"max_alphaengine_calls_24h": 50})["alphaengine"]["note"])

    def test_a_missing_cap_says_so_rather_than_inventing_one(self):
        self.assertEqual(self.lanes({})["alphaengine"]["note"], "上限未设置")
        self.assertEqual(self.lanes(None)["alphaengine"]["note"], "上限未设置")

    def test_the_note_is_not_a_constant(self):
        # The bug was a literal, so what matters is that the note moves with
        # the budget rather than that any particular wording is absent.
        notes = {self.lanes({"max_alphaengine_calls_24h": n})["alphaengine"]["note"]
                 for n in (30, 50, 130)}
        self.assertEqual(len(notes), 3)

    def test_a_source_with_no_cap_of_its_own_reports_none(self):
        from dalton_core.cockpit_plane import _source_daily_cap

        budget = {"max_alphaengine_calls_24h": 130}
        self.assertEqual(_source_daily_cap("source:alphaengine", budget), 130)
        self.assertIsNone(_source_daily_cap("source:web-search", budget))
        self.assertIsNone(_source_daily_cap("source:alphaengine", {"max_alphaengine_calls_24h": True}))


if __name__ == "__main__":
    unittest.main()
