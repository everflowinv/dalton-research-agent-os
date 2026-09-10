"""W4: the derived ledger that says whether "no change" was right.

Chem's 89-of-92 ``NO_CHANGE`` is the failure this exists to make visible, so
the tests are mostly about the cases where a naive implementation would
flatter the lane: a window that has not finished yet must not read as "held",
a basket of one must not read as a comparison, and a decision word with no
direction must not be graded at all.
"""

from __future__ import annotations

import sqlite3
import unittest

from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.judgement_outcome import (
    FORMULA_REF,
    JudgementOutcomeAuthority,
    JudgementOutcomeValidationError,
    build_outcome_checks,
    check_kind_for,
    check_ref_for,
    checks_digest,
    count_outcomes,
    no_change_check,
    revise_check,
    settled_dates,
    window_for,
)
from dalton_core.store import DaltonStore

from tests.zero_base_fixtures import (
    MISSION,
    STANCES,
    THRESHOLDS,
    add_judgement,
    add_reconciliation,
    at,
    flat_universe,
    series,
    trading_days,
)

DAYS = trading_days("2026-03-02", 12)


def judgement(**overrides):
    body = {
        "id": "event-judgement:one", "event_ref": "research-event:one",
        "company_ref": "company:ACN", "decision": "NO_CHANGE", "action": "no_change",
        "created_at": at(DAYS[0]),
    }
    body.update(overrides)
    return body


class WindowTests(unittest.TestCase):
    def test_the_window_needs_n_settled_sessions_after_the_decision(self) -> None:
        # Four sessions after the anchor, five asked for: there is no answer
        # yet, and "no answer yet" is not "no divergence".
        self.assertIsNone(window_for(DAYS[:5], anchor=DAYS[0], sessions=5))
        self.assertEqual(
            window_for(DAYS[:6], anchor=DAYS[0], sessions=5), (DAYS[0], DAYS[5])
        )

    def test_the_anchor_falls_back_to_the_last_close_before_it(self) -> None:
        # The decision was taken on a day the market did not trade; the window
        # opens at the last close that exists, not at nothing.
        dates = [DAYS[0], DAYS[3], DAYS[4], DAYS[5]]
        self.assertEqual(window_for(dates, anchor=DAYS[1], sessions=2), (DAYS[0], DAYS[4]))

    def test_a_decision_before_the_series_starts_has_no_window(self) -> None:
        self.assertIsNone(window_for(DAYS[3:], anchor=DAYS[0], sessions=1))

    def test_settled_dates_are_the_intersection_not_the_union(self) -> None:
        universe = {
            "company:ACN": series("company:ACN", DAYS[:6], [100.0] * 6),
            "company:CTSH": series("company:CTSH", DAYS[:4], [100.0] * 4),
        }
        self.assertEqual(settled_dates(universe), DAYS[:4])

    def test_a_provisional_bar_is_not_a_settled_day(self) -> None:
        one = series("company:ACN", DAYS[:3], [100.0] * 3)
        one["bars"][-1]["captured_at"] = DAYS[2] + "T13:00:00+00:00"
        self.assertEqual(settled_dates({"company:ACN": one}), DAYS[:2])


class NoChangeCheckTests(unittest.TestCase):
    def check(self, closes, **kwargs):
        universe = flat_universe(DAYS[:6], subject="company:ACN", subject_closes=closes)
        return no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS,
            stance=STANCES["company:ACN"], **kwargs,
        )

    def test_a_price_that_ran_against_a_long_thesis_is_a_candidate(self) -> None:
        # Down 10% while the basket did nothing: the market spent five sessions
        # arguing with a decision to do nothing, which is the case the owner
        # wants surfaced.
        found = self.check([100.0, 99.0, 97.0, 95.0, 92.0, 90.0])
        self.assertEqual(found["outcome"], "should_have_moved")
        self.assertEqual(found["basket_members"], 3)
        self.assertEqual(found["threshold_percent"], "6.0")
        self.assertEqual((found["from_date"], found["as_of"]), (DAYS[0], DAYS[5]))

    def test_a_price_inside_the_threshold_is_held_not_a_candidate(self) -> None:
        found = self.check([100.0, 100.0, 99.0, 98.0, 97.0, 98.0])
        self.assertEqual(found["outcome"], "held")

    def test_running_the_thesis_s_own_way_is_never_a_candidate(self) -> None:
        # Up 20% against a flat basket. A long thesis that was right is not
        # "should have moved"; a check that fired on the absolute gap would
        # call this one a miss.
        found = self.check([100.0, 105.0, 110.0, 115.0, 118.0, 120.0])
        self.assertEqual(found["outcome"], "held")

    def test_a_short_thesis_reads_the_same_move_the_other_way(self) -> None:
        universe = flat_universe(
            DAYS[:6], subject="company:ACN",
            subject_closes=[100.0, 105.0, 110.0, 115.0, 118.0, 120.0],
        )
        found = no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS,
            stance={"thesis_ref": "thesis:ACN", "stance": "short"},
        )
        self.assertEqual(found["outcome"], "should_have_moved")

    def test_an_unfinished_window_is_pending_and_says_how_far_it_got(self) -> None:
        universe = flat_universe(
            DAYS[:3], subject="company:ACN", subject_closes=[100.0, 90.0, 80.0],
        )
        found = no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS,
            stance=STANCES["company:ACN"],
        )
        self.assertEqual(found["outcome"], "pending")
        self.assertEqual(found["settled_after_anchor"], 2)

    def test_a_basket_too_thin_to_be_a_basket_is_unavailable(self) -> None:
        universe = {
            "company:ACN": series("company:ACN", DAYS[:6], [100, 99, 97, 95, 92, 90]),
            "company:CTSH": series("company:CTSH", DAYS[:6], [100.0] * 6),
        }
        found = no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS,
            stance=STANCES["company:ACN"],
        )
        self.assertEqual(found["outcome"], "unavailable")
        self.assertIn("篮子", found["reason"])

    def test_no_thesis_stance_is_unavailable_rather_than_defaulted(self) -> None:
        universe = flat_universe(
            DAYS[:6], subject="company:ACN",
            subject_closes=[100.0, 99.0, 97.0, 95.0, 92.0, 90.0],
        )
        found = no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS, stance=None,
        )
        self.assertEqual(found["outcome"], "unavailable")

    def test_a_company_with_no_series_is_unavailable(self) -> None:
        universe = flat_universe(DAYS[:6], subject="company:ACN", subject_closes=[100.0] * 6)
        universe["company:ACN"] = None
        found = no_change_check(
            judgement=judgement(), series_by_company=universe,
            dates=settled_dates(universe), thresholds=THRESHOLDS,
            stance=STANCES["company:ACN"],
        )
        self.assertEqual(found["outcome"], "unavailable")


class ReviseCheckTests(unittest.TestCase):
    def rows(self, forecast, actual, created_at=None):
        return [{
            "id": "forecast-reconciliation:one", "metric_ref": "metric:revenue",
            "period_end": "2026-03-31", "band": "notable",
            "created_at": created_at or at(DAYS[4]),
            "forecast_value": forecast, "actual_value": actual,
        }]

    def test_an_actual_above_the_forecast_confirms_a_strengthened_thesis(self) -> None:
        found = revise_check(
            judgement=judgement(decision="THESIS_STRENGTHENED", action="revise_forecast"),
            reconciliations=self.rows("100", "110"),
        )
        self.assertEqual(found["outcome"], "moved_right")
        self.assertEqual((found["implied_direction"], found["actual_direction"]),
                         ("up", "up"))

    def test_an_actual_below_the_forecast_confirms_a_weakened_thesis(self) -> None:
        found = revise_check(
            judgement=judgement(decision="THESIS_WEAKENED", action="revise_thesis"),
            reconciliations=self.rows("100", "90"),
        )
        self.assertEqual(found["outcome"], "moved_right")

    def test_the_opposite_direction_is_recorded_and_not_hidden(self) -> None:
        found = revise_check(
            judgement=judgement(decision="THESIS_BROKEN", action="revise_thesis"),
            reconciliations=self.rows("100", "130"),
        )
        self.assertEqual(found["outcome"], "moved_wrong")

    def test_an_actual_equal_to_the_forecast_confirms_nothing(self) -> None:
        found = revise_check(
            judgement=judgement(decision="THESIS_WEAKENED", action="revise_thesis"),
            reconciliations=self.rows("100", "100"),
        )
        self.assertEqual(found["outcome"], "not_confirmed")

    def test_only_a_reconciliation_after_the_decision_settles_it(self) -> None:
        found = revise_check(
            judgement=judgement(decision="THESIS_WEAKENED", action="revise_thesis",
                                created_at=at(DAYS[8])),
            reconciliations=self.rows("100", "90", created_at=at(DAYS[4])),
        )
        self.assertEqual(found["outcome"], "pending")

    def test_the_first_reconciliation_settles_it_not_the_flattering_one(self) -> None:
        rows = [
            {"id": "forecast-reconciliation:first", "created_at": at(DAYS[2]),
             "forecast_value": "100", "actual_value": "130", "band": "notable",
             "metric_ref": "metric:revenue", "period_end": "2026-03-31"},
            {"id": "forecast-reconciliation:second", "created_at": at(DAYS[6]),
             "forecast_value": "100", "actual_value": "80", "band": "notable",
             "metric_ref": "metric:revenue", "period_end": "2026-06-30"},
        ]
        found = revise_check(
            judgement=judgement(decision="THESIS_WEAKENED", action="revise_thesis"),
            reconciliations=rows,
        )
        self.assertEqual(found["outcome"], "moved_wrong")
        self.assertEqual(found["reconciliation_ref"], "forecast-reconciliation:first")

    def test_a_decision_word_with_no_direction_is_never_graded(self) -> None:
        found = revise_check(
            judgement=judgement(decision="NEW_THESIS", action="revise_thesis"),
            reconciliations=self.rows("100", "110"),
        )
        self.assertEqual(found["outcome"], "unavailable")

    def test_notes_and_research_earn_no_check_at_all(self) -> None:
        self.assertIsNone(check_kind_for("note"))
        self.assertIsNone(check_kind_for("research"))
        self.assertEqual(check_kind_for("no_change"), "no_change")
        self.assertEqual(check_kind_for("revise_dossier"), "revise")


class OutcomeAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        EventJudgementAuthority(self.store)
        self.outcomes = JudgementOutcomeAuthority(self.store)
        self.universe = flat_universe(
            DAYS[:6], subject="company:ACN",
            subject_closes=[100.0, 99.0, 97.0, 95.0, 92.0, 90.0],
        )
        add_judgement(
            self.store, judgement_id="event-judgement:one", company_ref="company:ACN",
            decision="NO_CHANGE", action="no_change", created_at=at(DAYS[0]),
        )

    def build(self, universe=None):
        return build_outcome_checks(
            self.store.connection, company_refs=["company:ACN"],
            series_by_company=universe or self.universe,
            thresholds=THRESHOLDS, stances=STANCES,
        )

    def test_the_pass_is_replayable_and_the_second_write_is_a_duplicate(self) -> None:
        checks = self.build()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["outcome"], "should_have_moved")
        first = self.outcomes.record_all(checks, actor_ref="automation:coverage-mission")
        self.assertEqual((first["fresh"], first["duplicate"]), (1, 0))
        again = self.build()
        self.assertEqual(checks_digest(checks), checks_digest(again))
        second = self.outcomes.record_all(again, actor_ref="automation:coverage-mission")
        self.assertEqual((second["fresh"], second["duplicate"]), (0, 1))
        self.assertEqual(len(self.outcomes.versions(checks[0]["check_ref"])), 1)

    def test_a_pending_check_that_settles_becomes_a_second_version(self) -> None:
        short = flat_universe(
            DAYS[:3], subject="company:ACN", subject_closes=[100.0, 98.0, 96.0],
        )
        pending = self.build(short)
        self.assertEqual(pending[0]["outcome"], "pending")
        self.outcomes.record_all(pending, actor_ref="automation:coverage-mission")
        settled = self.build()
        self.outcomes.record_all(settled, actor_ref="automation:coverage-mission")
        chain = self.outcomes.versions(pending[0]["check_ref"])
        self.assertEqual([row["version"] for row in chain], [1, 2])
        self.assertEqual([row["outcome"] for row in chain],
                         ["pending", "should_have_moved"])
        self.assertEqual(chain[1]["prior_version_ref"], chain[0]["id"])
        # The pointer follows, so the counts a reader sees are the newest word.
        self.assertEqual(self.outcomes.counts()["should_have_moved"], 1)
        self.assertEqual(self.outcomes.counts()["pending"], 0)

    def test_the_ledger_reads_back_by_content_hash(self) -> None:
        checks = self.build()
        written = self.outcomes.record(checks[0], actor_ref="automation:coverage-mission")
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(
            self.outcomes.for_judgement("event-judgement:one")["id"], written["id"]
        )

    def test_a_person_does_not_write_a_derived_row(self) -> None:
        checks = self.build()
        with self.assertRaises(JudgementOutcomeValidationError):
            self.outcomes.record(checks[0], actor_ref="human:owner")

    def test_a_row_claiming_another_formula_is_refused(self) -> None:
        checks = self.build()
        with self.assertRaises(JudgementOutcomeValidationError):
            self.outcomes.record(
                {**checks[0], "formula_ref": "judgement-outcome-formula:made-up"},
                actor_ref="automation:coverage-mission",
            )

    def test_an_outcome_from_the_other_check_s_vocabulary_is_refused(self) -> None:
        checks = self.build()
        with self.assertRaises(JudgementOutcomeValidationError):
            self.outcomes.record({**checks[0], "outcome": "moved_right"},
                                 actor_ref="automation:coverage-mission")

    def test_a_check_ref_that_does_not_derive_from_its_judgement_is_refused(self) -> None:
        checks = self.build()
        with self.assertRaises(JudgementOutcomeValidationError):
            self.outcomes.record({**checks[0], "check_ref": check_ref_for("something-else")},
                                 actor_ref="automation:coverage-mission")

    def test_the_table_refuses_a_write_that_did_not_come_through_the_store(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO judgement_outcome_check_versions(version_id,check_ref,"
                "version_number,prior_version_id,judgement_ref,company_ref,check_kind,"
                "outcome,formula_ref,inputs_hash,record_json,content_hash,actor_ref,"
                "created_at) VALUES('v','c',1,NULL,'j','company:ACN','no_change','held',"
                "?,'h','{}','x','automation:x','2026-01-01')", (FORMULA_REF,),
            )

    def test_a_written_check_can_never_be_updated_or_deleted(self) -> None:
        checks = self.build()
        self.outcomes.record(checks[0], actor_ref="automation:coverage-mission")
        for statement in (
            "UPDATE judgement_outcome_check_versions SET outcome='held'",
            "DELETE FROM judgement_outcome_check_versions",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store._transaction() as cur:
                    cur.execute(statement)

    def test_a_revise_judgement_is_graded_off_the_reconciliation_ledger(self) -> None:
        from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority

        ForecastReconciliationAuthority(self.store)
        add_judgement(
            self.store, judgement_id="event-judgement:two", company_ref="company:CTSH",
            decision="THESIS_WEAKENED", action="revise_forecast", created_at=at(DAYS[0]),
        )
        add_reconciliation(
            self.store, reconciliation_id="forecast-reconciliation:one",
            company_ref="company:CTSH", forecast="100", actual="88",
            created_at=at(DAYS[4]),
        )
        checks = build_outcome_checks(
            self.store.connection, company_refs=["company:ACN", "company:CTSH"],
            series_by_company=self.universe, thresholds=THRESHOLDS, stances=STANCES,
        )
        counts = count_outcomes(checks)
        self.assertEqual(counts["moved_right"], 1)
        self.assertEqual(counts["should_have_moved"], 1)

    def test_a_core_with_no_judgement_table_yields_no_checks(self) -> None:
        bare = DaltonStore(":memory:")
        self.addCleanup(bare.close)
        self.assertEqual(
            build_outcome_checks(
                bare.connection, company_refs=["company:ACN"],
                series_by_company=self.universe, thresholds=THRESHOLDS, stances=STANCES,
            ),
            [],
        )


class RegistrationTests(unittest.TestCase):
    def test_the_schema_is_registered_where_a_new_schema_must_be(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES
        from scripts.rehearse_deploy import CORE_MIGRATIONS

        self.assertIn("judgement_outcome_schema.sql", dict(SCHEMA_DATABASES))
        self.assertIn(
            "judgement_outcome_schema.sql",
            {spec.schema for spec in CORE_MIGRATIONS},
        )

    def test_the_mission_universe_fixture_is_what_the_basket_is_made_of(self) -> None:
        # Guards the fixture itself: a three-name basket is what makes the
        # min_basket_members test mean anything.
        self.assertEqual(len(MISSION["universe"]), 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
