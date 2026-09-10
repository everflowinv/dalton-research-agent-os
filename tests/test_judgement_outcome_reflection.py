"""W4: the outcome ledger seen from the weekly reflection and from the cockpit.

Chem §3.3 asked for "不动也要能被评价" and the reflection is where the owner
would look for it, so the two readings that matter are tested here: the counts
when the ledger exists, and the honest absence when it does not.  A page of
zeroes would say every decision we ever made was right, which is exactly the
claim Chem could not support and we are not going to make either.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.judgement_outcome import FORMULA_REF
from dalton_core.research_cycle_reflection import (
    REFLECTION_VERSION,
    closed_week,
    compute_metrics,
    inputs_hash,
    judgement_outcomes,
    narrative,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash

from tests.reflection_fixtures import SCHEMAS, open_fixture_core

NOW = datetime(2026, 3, 16, 9, tzinfo=timezone.utc)
WINDOW = closed_week(NOW)


def add_check(
    core, *, check_ref, judgement_ref, company_ref, kind, outcome, at,
    formula_ref=FORMULA_REF, version=1,
):
    record = {
        "id": f"judgement-outcome-version:{check_ref}:{version}",
        "check_ref": check_ref, "judgement_ref": judgement_ref,
        "company_ref": company_ref, "check_kind": kind, "outcome": outcome,
        "formula_ref": formula_ref, "version": version, "created_at": at,
    }
    record["content_hash"] = content_hash(record)
    core.execute(
        "INSERT INTO judgement_outcome_check_versions(version_id,check_ref,"
        "version_number,prior_version_id,judgement_ref,company_ref,check_kind,outcome,"
        "formula_ref,inputs_hash,record_json,content_hash,actor_ref,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record["id"], check_ref, version, None, judgement_ref, company_ref, kind,
         outcome, formula_ref, f"hash-{check_ref}-{version}", canonical_json(record),
         record["content_hash"], "automation:coverage-mission", at),
    )
    core.execute(
        "INSERT OR REPLACE INTO judgement_outcome_check_pointer(check_ref,version_id,"
        "version_number,judgement_ref,company_ref,check_kind,outcome,content_hash,"
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (check_ref, record["id"], version, judgement_ref, company_ref, kind, outcome,
         record["content_hash"], at),
    )
    return record


class MetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = open_fixture_core(SCHEMAS + ("judgement_outcome_schema.sql",))
        self.addCleanup(self.core.close)

    def test_a_core_without_the_table_says_so_instead_of_reporting_zero(self) -> None:
        bare = open_fixture_core()
        self.addCleanup(bare.close)
        found = judgement_outcomes(bare, WINDOW)
        self.assertFalse(found["available"])
        self.assertIn("judgement_outcome_check_pointer", found["reason"])

    def test_an_empty_ledger_is_an_absence_not_a_clean_record(self) -> None:
        found = judgement_outcomes(self.core, WINDOW)
        self.assertFalse(found["available"])
        self.assertEqual(found["checked"], 0)

    def test_the_counts_are_the_newest_word_on_every_judgement(self) -> None:
        inside = WINDOW["start"]
        outside = "2026-01-05T00:00:00.000000+00:00"
        add_check(self.core, check_ref="c1", judgement_ref="j1",
                  company_ref="company:ACN", kind="no_change",
                  outcome="should_have_moved", at=inside)
        add_check(self.core, check_ref="c2", judgement_ref="j2",
                  company_ref="company:ACN", kind="no_change", outcome="held",
                  at=outside)
        add_check(self.core, check_ref="c3", judgement_ref="j3",
                  company_ref="company:CTSH", kind="revise", outcome="moved_right",
                  at=outside)
        add_check(self.core, check_ref="c4", judgement_ref="j4",
                  company_ref="company:CTSH", kind="no_change", outcome="pending",
                  at=inside)
        found = judgement_outcomes(self.core, WINDOW)
        self.assertTrue(found["available"])
        self.assertEqual(found["checked"], 4)
        self.assertEqual(found["companies"], 2)
        self.assertEqual(found["by_kind"], {"no_change": 3, "revise": 1})
        self.assertEqual(found["to_date"]["should_have_moved"], 1)
        self.assertEqual(found["to_date"]["moved_right"], 1)
        self.assertEqual(found["to_date"]["held"], 1)
        # The weekly slice is the news; the ledger is cumulative.
        self.assertEqual(found["this_week"]["should_have_moved"], 1)
        self.assertEqual(found["this_week"]["held"], 0)
        self.assertEqual(found["should_have_moved"], 1)

    def test_a_row_under_an_older_formula_is_visible_as_such(self) -> None:
        add_check(self.core, check_ref="c1", judgement_ref="j1",
                  company_ref="company:ACN", kind="no_change", outcome="held",
                  at=WINDOW["start"], formula_ref="judgement-outcome-formula:w4:v0")
        found = judgement_outcomes(self.core, WINDOW)
        self.assertEqual(found["stale_formula"], ["judgement-outcome-formula:w4:v0"])
        self.assertEqual(found["current_formula_ref"], FORMULA_REF)

    def test_the_metric_joins_the_other_eight(self) -> None:
        add_check(self.core, check_ref="c1", judgement_ref="j1",
                  company_ref="company:ACN", kind="no_change",
                  outcome="should_have_moved", at=WINDOW["start"])
        metrics = compute_metrics(self.core, window=WINDOW, now=NOW)
        self.assertIn("judgement_outcomes", metrics)
        self.assertTrue(metrics["judgement_outcomes"]["available"])
        # A new metric changes what a week's reading is, so the counter version
        # moved with it: a week reflected on under 0.1 gets one more reading.
        self.assertEqual(REFLECTION_VERSION, "0.2")
        digest = inputs_hash(metrics, WINDOW)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, inputs_hash(metrics, WINDOW))

    def test_the_counts_change_the_inputs_hash(self) -> None:
        before = inputs_hash(compute_metrics(self.core, window=WINDOW, now=NOW), WINDOW)
        add_check(self.core, check_ref="c1", judgement_ref="j1",
                  company_ref="company:ACN", kind="no_change",
                  outcome="should_have_moved", at=WINDOW["start"])
        after = inputs_hash(compute_metrics(self.core, window=WINDOW, now=NOW), WINDOW)
        self.assertNotEqual(before, after)

    def test_the_prose_says_it_is_a_candidate_and_not_a_score(self) -> None:
        add_check(self.core, check_ref="c1", judgement_ref="j1",
                  company_ref="company:ACN", kind="no_change",
                  outcome="should_have_moved", at=WINDOW["start"])
        metrics = compute_metrics(self.core, window=WINDOW, now=NOW)
        prose = narrative(metrics, WINDOW)["prose"]
        self.assertIn("当时该动没动", prose)
        self.assertIn("不是绩效考核", prose)

    def test_the_prose_reports_the_absence_rather_than_staying_silent(self) -> None:
        metrics = compute_metrics(self.core, window=WINDOW, now=NOW)
        self.assertIn("判断结果台账读不出来", narrative(metrics, WINDOW)["prose"])


class CockpitPanelTests(unittest.TestCase):
    def panel(self, metric):
        from dalton_core.cockpit_plane import _judgement_outcome_panel

        return _judgement_outcome_panel(metric)

    def test_a_missing_metric_is_an_honest_absence(self) -> None:
        self.assertFalse(self.panel(None)["available"])
        self.assertFalse(
            self.panel({"available": False, "reason": "还没跑过"})["available"]
        )

    def test_the_panel_is_the_owner_s_words_and_only_the_rows_that_exist(self) -> None:
        found = self.panel({
            "available": True, "checked": 5, "companies": 2,
            "to_date": {"should_have_moved": 1, "held": 3, "moved_right": 1,
                        "pending": 0, "moved_wrong": 0, "not_confirmed": 0,
                        "unavailable": 0},
            "this_week": {"should_have_moved": 1},
            "should_have_moved": 1, "moved_right": 1,
        })
        self.assertTrue(found["available"])
        labels = {row["outcome"]: row["label"] for row in found["rows"]}
        self.assertEqual(labels["should_have_moved"], "当时该动没动（候选）")
        self.assertEqual(set(labels), {"should_have_moved", "held", "moved_right"})
        self.assertEqual(found["rows"][0]["this_week"], 1)
        self.assertIn("不是绩效考核", found["note"])


class ChildTests(unittest.TestCase):
    """The CLI's own wiring, on a Core with nothing in it.

    A full review needs a published mission, a stage ladder and a model; those
    paths are covered by the unit tests of the pieces.  What is worth an
    end-to-end test here is that the child never crashes a tick: it writes a
    summary and reports why it did nothing.
    """

    def test_a_core_with_no_mission_is_idle_and_still_writes_its_summary(self) -> None:
        from dalton_core.zero_base_review_cli import run_zero_base

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            DaltonStore(str(state / "core.sqlite")).close()
            summary_dir = state / "ticket"
            summary = run_zero_base(
                state_dir=state, summary_dir=summary_dir, mode="checks", now=NOW,
            )
            self.assertEqual((summary["status"], summary["review_status"]),
                             ("idle", "no_mission"))
            written = json.loads((summary_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(written["mode"], "checks")

    def test_the_check_pass_runs_against_a_real_store(self) -> None:
        from dalton_core.event_judgement import EventJudgementAuthority
        from dalton_core.tracking_cadence import load_policy
        from dalton_core.zero_base_review_cli import run_checks

        from tests.zero_base_fixtures import MISSION, add_judgement, at

        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        EventJudgementAuthority(store)
        add_judgement(store, judgement_id="event-judgement:one",
                      company_ref="company:ACN", decision="NO_CHANGE",
                      action="no_change", created_at=at("2026-03-02"))
        found = run_checks(
            store, mission=MISSION, tracked=["company:ACN"], policy=load_policy(),
            actor_ref="automation:coverage-mission",
        )
        # No price series in this Core, so the one gradable judgement is
        # `unavailable` with a reason -- which is the point: it is on the books
        # as unscorable rather than absent from them.
        self.assertEqual(found["checked"], 1)
        self.assertEqual(found["counts"]["unavailable"], 1)
        self.assertTrue(found["digest"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
