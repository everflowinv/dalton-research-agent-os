"""Q2 / C4: the weekly reflection, over a Core with two weeks in it.

Two weeks, because every metric here is a *window* metric and a fixture with
one week cannot tell "counted the right rows" from "counted all of them".  The
week under test is the closed one; the week before it is populated with rows
that must not be counted, and every assertion below is really the same
assertion twice.

The freeze gets its own class.  ``ResearchCycleReflection`` is not allowed to
write the Ledger, change policy or admit a question, and a promise in a
docstring is not a control: the tests assert it from two sides -- the module
imports no authority that could, and a connection that refuses every write
outside the reflection's own two tables survives a full run.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.research_cycle_reflection import (
    IDLE_LANE_STATUSES,
    MAX_BACKLOG_CANDIDATES,
    NARRATIVE_TITLE,
    ResearchCycleReflectionAuthority,
    ResearchCycleReflectionValidationError,
    backlog_candidates,
    build_reflection,
    closed_week,
    compute_metrics,
    idle_tick_ratio,
    inputs_hash,
    iso_week_label,
    narrative,
    policy_suggestions,
    reflection_ref_for,
    work_order_family,
)
from dalton_core.research_cycle_reflection_cli import load_tick_summaries, main
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.store import DaltonStore

from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.reflection_fixtures import (
    MISSION,
    add_backlog_event,
    add_journal_entry,
    add_mission_version,
    add_quality_score,
    add_research_plan,
    add_retirement,
    add_spend,
    add_stage_record,
    open_fixture_core,
    tick_summary,
    week_of,
)

# A Monday 00:00 UTC. The closed week is therefore the seven days before it.
MONDAY = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 7, 9, 30, tzinfo=timezone.utc)


def two_week_core() -> sqlite3.Connection:
    """Last week and the week before it, deliberately different."""

    core = open_fixture_core()
    add_mission_version(core, MISSION)

    # ---- the week under test: 2026-08-31 .. 2026-09-07 --------------------
    for index, (family, micros) in enumerate((
        ("document-extraction", 900_000), ("document-extraction", 100_000),
        ("llm-research-planner", 250_000), ("cockpit-quality", 50_000),
        ("alphaengine-doc", 700_000),
    )):
        add_spend(core, at=week_of(MONDAY, -3), amount_micros=micros,
                  work_order_ref=f"work:{family}-{'a' * 32}", suffix=f":{index}")
    # One call the provider never priced: counted, not costed.
    add_spend(core, at=week_of(MONDAY, -2), amount_micros=None,
              work_order_ref=f"work:document-extraction-{'b' * 32}")
    add_backlog_event(core, question_ref="question:new-1", state="open", at=week_of(MONDAY, -5))
    add_backlog_event(core, question_ref="question:new-2", state="open", at=week_of(MONDAY, -4))
    add_backlog_event(core, question_ref="question:old-1", state="answered", at=week_of(MONDAY, -1))
    add_retirement(core, claim_version_ref="claim-version:retired-1",
                   decision="retired", at=week_of(MONDAY, -2))
    add_retirement(core, claim_version_ref="claim-version:kept-1",
                   decision="kept", at=week_of(MONDAY, -2))
    add_research_plan(core, plan_id="plan-this-week", at=week_of(MONDAY, -6), inquiries=[
        {"company_ref": None, "question": "Whose bookings are decelerating?",
         "wants": ["filing"], "because": "growth dispersion"},
        {"company_ref": None, "question": "Does DXC's decline narrow?",
         "wants": ["filing"], "because": "one negative print"},
    ])
    add_quality_score(core, target_ref="weekly-brief-version:one",
                      rubric_ref="rubric:weekly-brief", at=week_of(MONDAY, -1),
                      judge_status="scored")
    add_quality_score(core, target_ref="weekly-brief-version:two",
                      rubric_ref="rubric:weekly-brief", at=week_of(MONDAY, -1))
    add_journal_entry(core, target_ref="weekly-brief-version:one",
                      verdict="revise", at=week_of(MONDAY, -1))

    # ---- the week before, which must not be counted anywhere --------------
    add_spend(core, at=week_of(MONDAY, -10), amount_micros=5_000_000,
              work_order_ref=f"work:document-extraction-{'c' * 32}")
    add_backlog_event(core, question_ref="question:older", state="open", at=week_of(MONDAY, -11))
    add_retirement(core, claim_version_ref="claim-version:retired-old",
                   decision="retired", at=week_of(MONDAY, -12))
    add_research_plan(core, plan_id="plan-last-week", at=week_of(MONDAY, -13), inquiries=[
        {"company_ref": None, "question": "old", "wants": [], "because": "old"},
    ])
    add_quality_score(core, target_ref="weekly-brief-version:old",
                      rubric_ref="rubric:initial-screen", at=week_of(MONDAY, -10))
    add_journal_entry(core, target_ref="weekly-brief-version:old",
                      verdict="useful", at=week_of(MONDAY, -10))

    # An entered stage nobody closed, older than both weeks.
    add_stage_record(core, company_ref="company:sec-cik:0001058290",
                     stage_ref="initial_screen", status="entered", at=week_of(MONDAY, -40))
    # And one that was entered long ago and closed inside the week under test.
    add_stage_record(core, company_ref="company:sec-cik:0001467373",
                     stage_ref="initial_screen", status="entered", at=week_of(MONDAY, -35))
    add_stage_record(core, company_ref="company:sec-cik:0001467373",
                     stage_ref="initial_screen", status="gate_passed", at=week_of(MONDAY, -2))
    return core


TICKS = [
    tick_summary(document_extraction="idle", research_plan="skipped"),
    tick_summary(document_extraction="idle", research_plan="idle"),
    tick_summary(document_extraction="launched", research_plan="idle"),
    tick_summary(document_extraction="idle", research_plan="skipped"),
]


class WeekBoundaryTests(unittest.TestCase):
    def test_the_closed_week_is_the_seven_days_before_the_last_monday(self):
        week = closed_week(NOW)
        self.assertEqual(week["iso_week"], "2026-W36")
        self.assertEqual(week["start"], "2026-08-31T00:00:00.000000+00:00")
        self.assertEqual(week["end"], "2026-09-07T00:00:00.000000+00:00")

    def test_every_day_of_a_week_reflects_on_the_same_closed_week(self):
        # The lane fires on Monday, but a writer restarted on Thursday must not
        # start reflecting on a week in progress.
        weeks = {closed_week(NOW + timedelta(days=offset))["iso_week"] for offset in range(0, 7)}
        self.assertEqual(weeks, {"2026-W36"})
        self.assertEqual(closed_week(NOW + timedelta(days=7))["iso_week"], "2026-W37")

    def test_the_boundary_is_local_midnight_not_utc_midnight(self):
        eastern = timezone(timedelta(hours=-4))
        local = datetime(2026, 9, 7, 9, 30, tzinfo=eastern)
        self.assertEqual(closed_week(local)["end"], "2026-09-07T04:00:00.000000+00:00")

    def test_the_label_is_the_iso_week_of_the_windows_start(self):
        self.assertEqual(iso_week_label(datetime(2026, 8, 31, tzinfo=timezone.utc)), "2026-W36")


class WorkOrderFamilyTests(unittest.TestCase):
    def test_a_families_digest_is_not_part_of_its_name(self):
        self.assertEqual(work_order_family("work:document-extraction-" + "a" * 32),
                         "document-extraction")
        self.assertEqual(work_order_family("work:alphaengine-doc:" + "b" * 24),
                         "alphaengine-doc")
        self.assertEqual(work_order_family("work:cockpit-quality-" + "c" * 32),
                         "cockpit-quality")

    def test_something_that_is_not_a_work_order_is_not_guessed_at(self):
        self.assertEqual(work_order_family(None), "unattributed")
        self.assertEqual(work_order_family("nonsense"), "unattributed")


class MetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = two_week_core()
        self.addCleanup(self.core.close)
        self.window = closed_week(NOW)
        self.metrics = compute_metrics(
            self.core, window=self.window, budget=MISSION["budget"],
            tick_summaries=TICKS, now=NOW,
        )

    def test_spend_is_grouped_by_pool_and_last_weeks_five_dollars_is_not_in_it(self):
        spend = self.metrics["spend"]
        self.assertTrue(spend["available"])
        self.assertEqual(spend["total_cost_usd"], 2.0)
        self.assertEqual(spend["calls"], 6)
        self.assertEqual(spend["unpriced_calls"], 1)
        pools = {item["pool"]: item for item in spend["pools"]}
        self.assertEqual(set(pools), {
            "document-extraction", "alphaengine-doc", "llm-research-planner", "cockpit-quality",
        })
        self.assertEqual(pools["document-extraction"]["cost_usd"], 1.0)
        self.assertEqual(pools["document-extraction"]["share_of_spend"], 0.5)

    def test_a_pool_names_its_lane_only_where_the_code_proves_it(self):
        pools = {item["pool"]: item for item in self.metrics["spend"]["pools"]}
        self.assertEqual(pools["document-extraction"]["lane"], "document_extraction")
        self.assertEqual(pools["llm-research-planner"]["lane"], "research_plan")
        # AlphaEngine acquisition is not built by any registered lane's own
        # code path, so it is reported by family with no lane rather than
        # attributed to a plausible one.
        self.assertIsNone(pools["alphaengine-doc"]["lane"])

    def test_the_cap_is_the_missions_daily_number_times_the_window(self):
        self.assertEqual(self.metrics["spend"]["mission_window_cap_usd"], 700.0)
        self.assertEqual(self.metrics["spend"]["share_of_mission_cap"], round(2.0 / 700.0, 6))

    def test_backlog_counts_this_weeks_movement_and_the_standing_open_set(self):
        backlog = self.metrics["backlog"]
        self.assertEqual(backlog["new_questions"], 2)
        self.assertEqual(backlog["answered"], 1)
        # question:older opened last week and never moved; question:new-1 and
        # new-2 opened this week. The answered one is no longer open.
        self.assertEqual(backlog["open_at_end"], 3)

    def test_claims_retired_this_week_excludes_last_weeks_retirement(self):
        claims = self.metrics["claims"]
        self.assertEqual(claims["retired"], 1)
        self.assertEqual(claims["kept"], 1)
        self.assertEqual(claims["retired_refs"], ["claim-version:retired-1"])

    def test_planner_inquiries_are_counted_and_the_zero_dispatch_says_why(self):
        planner = self.metrics["planner"]
        self.assertEqual(planner["plans_recorded"], 1)
        self.assertEqual(planner["inquiries_raised"], 2)
        self.assertEqual(planner["dispatched"], 0)
        self.assertEqual(planner["dispatch_ratio"], 0.0)
        self.assertIn("P14e", planner["dispatch_reason"])

    def test_a_tick_is_idle_only_when_every_lane_was(self):
        ticks = self.metrics["ticks"]
        self.assertEqual(ticks["ticks"], 4)
        self.assertEqual(ticks["idle_ticks"], 3)
        self.assertEqual(ticks["ratio"], 0.75)
        self.assertEqual(ticks["idle_by_lane"]["research_plan"], 1.0)
        self.assertEqual(ticks["idle_by_lane"]["document_extraction"], 0.75)

    def test_without_archived_summaries_the_idle_ratio_is_absent_not_zero(self):
        absent = idle_tick_ratio([])
        self.assertFalse(absent["available"])
        self.assertIn("heartbeat.json", absent["reason"])
        self.assertNotIn("ratio", absent)

    def test_an_unconfigured_lane_neither_makes_a_tick_idle_nor_busy(self):
        self.assertNotIn("unconfigured", IDLE_LANE_STATUSES)
        result = idle_tick_ratio([tick_summary(a="idle", b="unconfigured")])
        self.assertEqual(result["idle_ticks"], 0)

    def test_an_open_checkpoint_ages_from_when_it_was_entered(self):
        checkpoints = self.metrics["human_checkpoints"]
        self.assertEqual(checkpoints["open"], 1)
        self.assertEqual(checkpoints["checkpoints"][0]["company_ref"],
                         "company:sec-cik:0001058290")
        self.assertGreater(checkpoints["oldest_age_days"], 39)
        # The Accenture stage was entered 35 days ago and closed this week, so
        # it is not open and it does count as closed.
        self.assertEqual(checkpoints["closed_this_week"], 1)

    def test_quality_scores_separate_the_judged_from_the_deterministic_only(self):
        scores = self.metrics["quality_scores"]
        self.assertEqual(scores["published"], 2)
        self.assertEqual(scores["by_rubric"], {"rubric:weekly-brief": 2})
        self.assertEqual(scores["judged"], 1)
        self.assertEqual(scores["deterministic_only"], 1)

    def test_feedback_is_counted_by_word_and_from_both_places_it_is_written(self):
        journal = self.metrics["journal"]
        self.assertEqual(journal["entries"], 1)
        self.assertEqual(journal["by_word"]["revise"], 1)
        self.assertEqual(journal["by_word"]["useful"], 0)
        self.assertEqual(journal["outstanding_words"], 1)
        self.assertTrue(journal["sources"]["analyst_journal"]["available"])
        # The weekly brief keeps its own feedback table with the same five
        # words; this Core does not have one, and that is reported rather than
        # folded into the count.
        self.assertFalse(journal["sources"]["weekly_brief"]["available"])

    def test_a_missing_authority_is_an_absence_with_a_reason_not_a_zero(self):
        bare = open_fixture_core(schemas=("schema.sql",))
        self.addCleanup(bare.close)
        metrics = compute_metrics(bare, window=self.window, budget=MISSION["budget"], now=NOW)
        for name in ("spend", "backlog", "claims", "planner", "quality_scores"):
            with self.subTest(metric=name):
                self.assertFalse(metrics[name]["available"])
                self.assertTrue(metrics[name]["reason"].strip())

    def test_it_reads_a_real_store_without_falling_over(self):
        # The fixture is built from the schema files; this is the same reading
        # against a Core built the way the writer builds one. A fresh store
        # carries the Ledger and nothing else -- every other authority installs
        # its own schema when it is constructed -- so seven of the eight
        # metrics are legitimately absent here. What is under test is that
        # each of them is *shaped* like an answer and none of them raises: a
        # reflection running the day before an authority is deployed has to
        # report the gap, not crash the lane.
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        metrics = compute_metrics(
            store.connection, window=self.window, budget=MISSION["budget"], now=NOW,
        )
        self.assertEqual(len(metrics), 8)
        for name, value in metrics.items():
            with self.subTest(metric=name):
                self.assertIn("available", value)
                if not value["available"]:
                    self.assertTrue(value["reason"].strip())
        self.assertEqual(
            [name for name, value in metrics.items() if value["available"]], ["journal"],
        )
        self.assertEqual(metrics["journal"]["entries"], 0)


class NarrativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = two_week_core()
        self.addCleanup(self.core.close)
        self.window = closed_week(NOW)
        self.metrics = compute_metrics(
            self.core, window=self.window, budget=MISSION["budget"],
            tick_summaries=TICKS, now=NOW,
        )

    def test_the_section_is_the_question_the_meeting_opens_with(self):
        self.assertEqual(NARRATIVE_TITLE, "我们把时间花在哪")

    def test_the_prose_names_the_largest_pool_and_the_table_carries_the_numbers(self):
        rendered = narrative(self.metrics, self.window)
        self.assertIn("document-extraction", rendered["prose"])
        self.assertIn("2026-W36", rendered["prose"])
        self.assertEqual(rendered["table"][0]["pool"], "document-extraction")
        self.assertEqual(rendered["table"][0]["cost_usd"], 1.0)

    def test_every_sentence_is_a_rendering_of_a_number_in_the_record(self):
        rendered = narrative(self.metrics, self.window)
        self.assertIn("新登记 2 条", rendered["prose"])
        self.assertIn("被回答 1 条", rendered["prose"])
        self.assertIn("退役 1 条", rendered["prose"])

    def test_an_unavailable_metric_is_said_out_loud_rather_than_skipped(self):
        metrics = compute_metrics(
            self.core, window=self.window, budget=MISSION["budget"], now=NOW,
        )
        rendered = narrative(metrics, self.window)
        self.assertIn("闲置率不可算", rendered["prose"])


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = two_week_core()
        self.addCleanup(self.core.close)
        self.window = closed_week(NOW)
        self.metrics = compute_metrics(
            self.core, window=self.window, budget=MISSION["budget"],
            tick_summaries=TICKS, now=NOW,
        )

    def test_a_candidate_is_exactly_what_the_backlog_asks_for(self):
        for candidate in backlog_candidates(self.metrics, self.window):
            with self.subTest(candidate=candidate["question"][:30]):
                self.assertEqual(set(candidate), {"question", "because", "refs"})
                self.assertTrue(candidate["question"].strip())
                self.assertTrue(candidate["because"].strip())

    def test_undispatched_inquiries_and_a_stale_checkpoint_each_raise_one(self):
        questions = " ".join(item["question"] for item in backlog_candidates(self.metrics, self.window))
        self.assertIn("inquiry", questions)
        self.assertIn("initial_screen", questions)

    def test_a_half_idle_week_asks_whether_the_lanes_had_nothing_or_could_not_choose(self):
        questions = " ".join(item["question"] for item in backlog_candidates(self.metrics, self.window))
        self.assertIn("无事可做", questions)

    def test_the_list_is_bounded(self):
        self.assertLessEqual(len(backlog_candidates(self.metrics, self.window)),
                             MAX_BACKLOG_CANDIDATES)

    def test_policy_suggestions_are_sentences_about_policy_and_nothing_else(self):
        suggestions = policy_suggestions(self.metrics)
        self.assertTrue(suggestions)
        for item in suggestions:
            with self.subTest(suggestion=item[:30]):
                self.assertIsInstance(item, str)
        self.assertTrue(any("budget_pool" in item for item in suggestions))


class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.authority = ResearchCycleReflectionAuthority(self.store)
        self.core = two_week_core()
        self.addCleanup(self.core.close)
        self.body = build_reflection(
            self.core, mission=MISSION, now=NOW, tick_summaries=TICKS,
        )

    def test_a_reflection_reads_back_under_the_ref_its_week_gives_it(self):
        written = self.authority.record(self.body, actor_ref="automation:coverage-mission")
        self.assertEqual(written["status"], "fresh")
        self.assertEqual(written["version"], 1)
        self.assertEqual(written["reflection_ref"],
                         reflection_ref_for(MISSION["mission_ref"], "2026-W36"))
        self.assertEqual(self.authority.for_week(MISSION["mission_ref"], "2026-W36")["id"],
                         written["id"])
        self.assertEqual(self.authority.weeks(MISSION["mission_ref"]), ["2026-W36"])

    def test_the_same_week_with_the_same_inputs_is_a_duplicate(self):
        first = self.authority.record(self.body, actor_ref="automation:coverage-mission")
        again = self.authority.record(self.body, actor_ref="automation:coverage-mission")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.authority.versions(first["reflection_ref"])), 1)

    def test_a_week_whose_numbers_moved_is_a_new_version_of_the_same_week(self):
        self.authority.record(self.body, actor_ref="automation:coverage-mission")
        add_backlog_event(self.core, question_ref="question:late",
                          state="open", at=week_of(MONDAY, -1))
        moved = build_reflection(self.core, mission=MISSION, now=NOW, tick_summaries=TICKS)
        self.assertNotEqual(moved["inputs_hash"], self.body["inputs_hash"])
        second = self.authority.record(moved, actor_ref="automation:coverage-mission")
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(self.authority.versions(second["reflection_ref"])), 2)

    def test_ageing_alone_does_not_make_a_new_version(self):
        # An open checkpoint is a second older every second. If ageing were in
        # the identity, every tick would write a version and the duplicate rule
        # would never fire once.
        later = build_reflection(
            self.core, mission=MISSION, now=NOW + timedelta(hours=6), tick_summaries=TICKS,
        )
        self.assertEqual(later["inputs_hash"], self.body["inputs_hash"])
        self.assertNotEqual(
            later["metrics"]["human_checkpoints"]["oldest_age_days"],
            self.body["metrics"]["human_checkpoints"]["oldest_age_days"],
        )

    def test_an_anonymous_writer_is_refused(self):
        with self.assertRaises(ResearchCycleReflectionValidationError):
            self.authority.record(self.body, actor_ref="coverage-mission")

    def test_a_candidate_that_is_not_question_because_and_refs_is_refused(self):
        body = dict(self.body)
        body["backlog_candidates"] = [{"question": "q", "because": "b", "refs": [], "decision": "admit"}]
        with self.assertRaises(ResearchCycleReflectionValidationError):
            self.authority.record(body, actor_ref="automation:coverage-mission")

    def test_the_record_is_immutable_in_the_database(self):
        written = self.authority.record(self.body, actor_ref="automation:coverage-mission")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE research_cycle_reflection_versions SET iso_week='2026-W99' "
                "WHERE version_id=?", (written["id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM research_cycle_reflection_versions WHERE version_id=?",
                (written["id"],),
            )

    def test_an_unauthorised_insert_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO research_cycle_reflection_versions(version_id,reflection_ref,"
                "version_number,prior_version_ref,mission_ref,mission_version_ref,iso_week,"
                "window_start,window_end,inputs_hash,backlog_candidate_count,"
                "policy_suggestion_count,record_json,content_hash,actor_ref,created_at) "
                "VALUES('v','r',1,NULL,'m','mv','2026-W36','a','b','h',0,0,'{}','h','human:x','t')"
            )


class FreezeTests(unittest.TestCase):
    """The v0.4 freeze: no Ledger write, no policy change, no question admitted."""

    MODULE = Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "research_cycle_reflection.py"

    def test_the_module_imports_nothing_that_could_write_the_ledger(self):
        source = self.MODULE.read_text(encoding="utf-8")
        forbidden = (
            "claim_index", "coverage_mission import", "mission_deliverable",
            "research_question_backlog", "governance", "candidate_staging",
            "thesis", "ClaimAuthority",
        )
        imports = [
            line for line in source.splitlines()
            if line.startswith(("import ", "from ")) or line.strip().startswith(("import ", "from "))
        ]
        for line in imports:
            for name in forbidden:
                with self.subTest(line=line.strip(), forbidden=name):
                    self.assertNotIn(name, line)

    def test_the_only_writes_are_its_own_two_tables(self):
        # A spy connection: everything the run does is allowed to read, and any
        # write outside the reflection's own tables raises. It is the freeze
        # expressed as a control rather than as a promise.
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        authority = ResearchCycleReflectionAuthority(store)
        core = two_week_core()
        self.addCleanup(core.close)
        body = build_reflection(core, mission=MISSION, now=NOW, tick_summaries=TICKS)

        allowed = {"research_cycle_reflection_versions", "research_cycle_reflection_pointer"}
        seen: list[str] = []

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                table = str(arg1 or "")
                if not table.startswith("sqlite_"):
                    seen.append(table)
                    if table not in allowed:
                        return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.connection.set_authorizer(authorizer)
        self.addCleanup(store.connection.set_authorizer, None)
        written = authority.record(body, actor_ref="automation:coverage-mission")
        self.assertEqual(written["status"], "fresh")
        self.assertTrue(seen)
        self.assertEqual(set(seen) - allowed, set())

    def test_computing_a_reflection_writes_nothing_at_all(self):
        core = two_week_core()
        self.addCleanup(core.close)

        def deny_writes(action, arg1, arg2, db_name, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        core.set_authorizer(deny_writes)
        self.addCleanup(core.set_authorizer, None)
        body = build_reflection(core, mission=MISSION, now=NOW, tick_summaries=TICKS)
        self.assertEqual(body["iso_week"], "2026-W36")

    def test_the_record_says_out_loud_what_it_is_not_allowed_to_do(self):
        core = two_week_core()
        self.addCleanup(core.close)
        body = build_reflection(core, mission=MISSION, now=NOW, tick_summaries=TICKS)
        self.assertIn("不写 Ledger", body["authority_note"])
        self.assertIn("不改 policy", body["authority_note"])
        self.assertIn("不登记问题", body["authority_note"])

    def test_the_inputs_hash_covers_the_metrics_and_the_window(self):
        core = two_week_core()
        self.addCleanup(core.close)
        window = closed_week(NOW)
        metrics = compute_metrics(core, window=window, budget=MISSION["budget"],
                                  tick_summaries=TICKS, now=NOW)
        digest = inputs_hash(metrics, window)
        other = dict(window, start="2026-08-24T00:00:00.000000+00:00")
        self.assertNotEqual(inputs_hash(metrics, other), digest)


class CommandLineTests(unittest.TestCase):
    """The child the lane spawns, run for real against a state directory."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        # A real mission published through its own authority, because the CLI's
        # first act is to find one and a hand-written row is not one.
        store = DaltonStore(str(self.state / "core.sqlite"))
        try:
            fixtures = bootstrap_method_authorities(store)
            params = mission_params(fixtures)
            ref = params.pop("mission_ref")
            params["autonomy"] = {
                **params["autonomy"],
                "may_write": sorted(set(params["autonomy"]["may_write"]) | {"deliverable"}),
            }
            self.mission = CoverageMissionAuthority(store).create_mission(ref, **params)
            self.mission_ref = ref
        finally:
            store.close()

    def run_cli(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        text = buffer.getvalue().strip()
        return code, (json.loads(text) if text else {})

    def test_a_dry_run_computes_a_week_and_writes_nothing(self):
        code, result = self.run_cli(
            ["run", "--state-dir", str(self.state), "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(result["recorded"])
        self.assertEqual(result["narrative"]["title"], NARRATIVE_TITLE)
        self.assertTrue(result["inputs_hash"])

    def test_running_twice_records_once(self):
        first = self.run_cli(["run", "--state-dir", str(self.state)])[1]
        second = self.run_cli(["run", "--state-dir", str(self.state)])[1]
        self.assertEqual(first["recorded"]["status"], "fresh")
        self.assertEqual(second["recorded"]["status"], "duplicate")
        self.assertEqual(second["recorded"]["id"], first["recorded"]["id"])

    def test_the_child_leaves_the_summary_the_lane_reads_back(self):
        target = self.state / "ticket"
        self.run_cli(
            ["run", "--state-dir", str(self.state), "--summary-dir", str(target), "--quiet"]
        )
        summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["recorded"]["status"], "fresh")
        self.assertIn("policy_suggestions", summary)

    def test_show_reads_a_week_back_by_its_label(self):
        self.run_cli(["run", "--state-dir", str(self.state)])
        code, result = self.run_cli(
            ["show", "--state-dir", str(self.state), "--week", "2026-W36"]
        )
        self.assertEqual(code, 0)
        self.assertIn(closed_week(datetime.now().astimezone())["iso_week"],
                      self.run_cli(["show", "--state-dir", str(self.state)])[1]["reflection_ref"])
        self.assertIn("2026-W36", result["reflection_ref"])

    def test_archived_tick_summaries_are_read_when_a_directory_is_given(self):
        archive = self.state / "tick-summaries"
        archive.mkdir()
        for index, summary in enumerate(TICKS):
            (archive / f"{index:04d}.json").write_text(
                json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        (archive / "broken.json").write_text("not json", encoding="utf-8")
        self.assertEqual(len(load_tick_summaries(archive)), len(TICKS))
        result = self.run_cli([
            "run", "--state-dir", str(self.state), "--dry-run",
            "--tick-summary-dir", str(archive),
        ])[1]
        self.assertTrue(result["metrics"]["ticks"]["available"])
        self.assertEqual(result["metrics"]["ticks"]["ticks"], len(TICKS))

    def test_without_an_archive_the_metric_is_absent_rather_than_zero(self):
        result = self.run_cli(["run", "--state-dir", str(self.state), "--dry-run"])[1]
        self.assertFalse(result["metrics"]["ticks"]["available"])


if __name__ == "__main__":
    unittest.main()
