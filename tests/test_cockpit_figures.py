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
        "subject_as_named": "Accenture",
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

    def lanes(self, budget, discovery=None):
        from dalton_core.cockpit_plane import CockpitPlane

        return {l["key"]: l
                for l in CockpitPlane._base_lane_states({}, {}, discovery or {}, budget)}

    def test_the_alphaengine_note_reads_the_live_budget(self):
        # It was the literal "每 24 小时最多 30 次", so the page said 30 for days
        # after the owner raised the cap to 130.
        self.assertIn("130", self.lanes({"max_alphaengine_calls_24h": 130})["alphaengine"]["note"])
        self.assertIn("50", self.lanes({"max_alphaengine_calls_24h": 50})["alphaengine"]["note"])

    def test_the_effective_cap_wins_over_the_requested_one(self):
        # The owner raised the mission budget to 130 and the effective cap
        # stayed 30, because a constant in this codebase is tighter. Showing
        # 130 would be the old bug in the other direction.
        measured = {"discovery": {"budget": {"cap": 30, "mission_cap": 130,
                                             "owner_cap": 30, "bound_by": "owner"}}}
        note = self.lanes({"max_alphaengine_calls_24h": 130}, measured)["alphaengine"]["note"]
        self.assertIn("30", note)
        self.assertIn("130", note)
        self.assertIn("owner", note)

    def test_a_cap_the_mission_itself_sets_is_not_explained_away(self):
        measured = {"discovery": {"budget": {"cap": 130, "mission_cap": 130,
                                             "owner_cap": 500, "bound_by": "mission"}}}
        note = self.lanes({"max_alphaengine_calls_24h": 130}, measured)["alphaengine"]["note"]
        self.assertEqual(note, "每 24 小时最多 130 次")

    def test_the_acquisition_budget_is_used_when_discovery_has_none(self):
        measured = {"acquisition": {"budget": {"cap": 30, "mission_cap": 130,
                                               "owner_cap": 30, "bound_by": "owner"}}}
        self.assertIn("30", self.lanes({}, measured)["alphaengine"]["note"])

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


class BudgetBindingTests(unittest.TestCase):
    """P12d: a cap that overrides the owner's budget has to say it did."""

    def remaining(self, *, mission_cap, owner_cap):
        from unittest.mock import patch

        from dalton_core import mission_source_discovery as m

        # The trailing-window count is not what is under test; which cap binds
        # and whether it says so is.
        with patch.object(m, "count_recent_alphaengine_calls", return_value=0):
            return m.alphaengine_calls_remaining(
                None, mission_cap=mission_cap, owner_cap=owner_cap)

    def test_the_tighter_cap_wins_and_names_itself(self):
        tight = self.remaining(mission_cap=130, owner_cap=30)
        self.assertEqual((tight["cap"], tight["bound_by"]), (30, "owner"))
        self.assertEqual((tight["mission_cap"], tight["owner_cap"]), (130, 30))

    def test_a_mission_that_asks_for_less_binds_itself(self):
        loose = self.remaining(mission_cap=10, owner_cap=30)
        self.assertEqual((loose["cap"], loose["bound_by"]), (10, "mission"))

    def test_equal_caps_are_attributed_to_the_mission(self):
        same = self.remaining(mission_cap=30, owner_cap=30)
        self.assertEqual((same["cap"], same["bound_by"]), (30, "mission"))


if __name__ == "__main__":
    unittest.main()
