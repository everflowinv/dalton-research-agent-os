"""P17d: failure classes, the parked-item ledger, and the cockpit's four panels.

The case these tests are written against is Task 62 of the predecessor project:
three attempts at AlphaEngine's desktop module page, three
``status=no_module_page``, permanently failed.  Rule 14 says every live
incident produces a test; this file is that test, plus the two properties that
make the fix honest -- a parked item is still probed, and the ledger it is
parked in cannot be edited.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.lane_failure_class import (
    CONTENT_REFUSED,
    DEPENDENCY_UNAVAILABLE,
    FAILURE_CLASSES,
    NOT_PERMITTED,
    LANE_RULES,
    PARK_PROBE_INTERVAL_SECONDS,
    RULES,
    TRANSIENT,
    LaneFailureBudget,
    classify,
    classify_settled,
    name_dependency,
)
from dalton_core.lane_failure_ledger import (
    LaneFailureLedger,
    LaneFailureLedgerError,
    default_path,
    lane_budget,
    summarise_events,
)

NOW = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
TASK_62 = "AlphaEngine Desktop status=no_module_page"


class ClassifyTests(unittest.TestCase):
    def test_governance_gates_wait_for_permission(self) -> None:
        for reason in (
            "gated:mission does not grant document_extraction writes",
            "gated:active governance policy does not list this operation",
        ):
            with self.subTest(reason=reason):
                found = classify(reason, lane="document_extraction")
                self.assertEqual(found.failure_class, NOT_PERMITTED)
                self.assertTrue(found.awaits_permission)

    def test_the_task_62_reason_is_a_dependency_not_a_bad_task(self) -> None:
        found = classify(TASK_62)
        self.assertEqual(found.failure_class, DEPENDENCY_UNAVAILABLE)
        self.assertEqual(found.dependency, "alphaengine_desktop")
        self.assertTrue(found.parks)
        self.assertFalse(found.terminal)

    def test_the_p14e_hold_reason_names_the_writer_rpc(self) -> None:
        # The exact string ``bounded_planner_driver`` emits, asserted in
        # tests/test_bounded_planner_driver.py.
        found = classify("probe_transport_unavailable:RuntimeError")
        self.assertEqual(found.failure_class, DEPENDENCY_UNAVAILABLE)
        self.assertEqual(found.dependency, "writer_rpc")

    def test_unusable_content_is_terminal(self) -> None:
        found = classify("the review is unreadable")
        self.assertEqual(found.failure_class, CONTENT_REFUSED)
        self.assertTrue(found.terminal)
        self.assertIsNone(found.dependency)

    def test_an_unknown_reason_is_transient_and_keeps_its_words(self) -> None:
        found = classify("MarketDataAdapterError: Yahoo has no such ticker")
        self.assertEqual(found.failure_class, TRANSIENT)
        self.assertEqual(found.rule, "unmapped")
        self.assertEqual(found.reason, "MarketDataAdapterError: Yahoo has no such ticker")

    def test_a_vendor_name_alone_never_makes_an_outage(self) -> None:
        """The precision this design turns on.

        "Yahoo has no such ticker" names a vendor and is not an outage.  A
        classifier that read the word "Yahoo" as a dependency failure would
        park that company against a source that is working perfectly, and it
        would never come back -- the same permanent suspension in a new suit.
        """

        for reason in (
            "MarketDataAdapterError: Yahoo has no such ticker",
            "AlphaEngine returned a document with no text",
            "GuidepointError: the query matched nothing",
        ):
            with self.subTest(reason=reason):
                self.assertNotEqual(
                    classify(reason).failure_class, DEPENDENCY_UNAVAILABLE)

    def test_an_anonymous_outage_is_named_from_the_vendor_it_mentions(self) -> None:
        found = classify("ConnectionError: sec.gov did not answer")
        self.assertEqual(found.failure_class, DEPENDENCY_UNAVAILABLE)
        self.assertEqual(found.dependency, "sec")

    def test_a_lane_that_talks_to_one_source_names_it_without_being_told(self) -> None:
        self.assertEqual(
            classify("TimeoutError: read timed out",
                     lane="mission_market_prices").dependency,
            "market_data")
        self.assertEqual(
            classify("TimeoutError: read timed out").dependency, "unknown")
        self.assertEqual(name_dependency("nothing recognisable"), "unknown")

    def test_a_quota_is_a_dependency_not_a_failure_of_the_work(self) -> None:
        for reason, dependency in (
            ("skipped:pool_exhausted", "model_budget"),
            ("quota_exhausted", "quota"),
            ("budget_refused", "model_budget"),
        ):
            with self.subTest(reason=reason):
                found = classify(reason)
                self.assertEqual(found.failure_class, DEPENDENCY_UNAVAILABLE)
                self.assertEqual(found.dependency, dependency)

    def test_reading_our_own_bookkeeping_is_not_the_source_refusing(self) -> None:
        """``unreadable_last_run`` is Guidepoint's word for *our* record.

        It sits before ``unreadable`` in the table on purpose: read the other
        way round, a lane that could not open its own cadence row would declare
        the query permanently unanswerable.
        """

        self.assertEqual(classify("unreadable_last_run").failure_class, TRANSIENT)

    def test_a_settled_ticket_with_no_reason_falls_back_to_its_status(self) -> None:
        found = classify_settled({"status": "orphaned"})
        self.assertEqual(found.reason, "last run: orphaned")
        self.assertEqual(found.failure_class, TRANSIENT)

    def test_nothing_said_at_all_is_transient(self) -> None:
        found = classify(None)
        self.assertEqual(found.failure_class, TRANSIENT)
        self.assertEqual(found.reason, "")

    def test_every_rule_declares_a_known_class_and_a_unique_id(self) -> None:
        ids = [rule.rule_id for rule in RULES]
        self.assertEqual(len(ids), len(set(ids)))
        for rule in RULES:
            with self.subTest(rule=rule.rule_id):
                self.assertIn(rule.failure_class, FAILURE_CLASSES)
                self.assertEqual(rule.pattern, rule.pattern.lower())
                if rule.failure_class != DEPENDENCY_UNAVAILABLE:
                    self.assertIsNone(rule.dependency)

    def test_a_lane_rule_wins_over_the_general_table(self) -> None:
        self.assertIn("mission_statements", LANE_RULES)
        found = classify("dispatch carries no lane ticket", lane="mission_statements")
        self.assertEqual(found.failure_class, TRANSIENT)
        self.assertTrue(found.rule.startswith("mission_statements:"))


class LaneVocabularyMigrationTests(unittest.TestCase):
    """The reason strings the existing lanes emit, and where each one lands.

    This is the migration: the lanes are not asked to say anything new, so
    "migrated" means "its vocabulary is in the table and lands where a person
    would put it".  A lane whose words are not here is listed in the report.
    """

    VOCABULARY: tuple[tuple[str, str, str], ...] = (
        # lane driver key, reason as the lane emits it, expected class
        ("mission_market_prices", "MarketDataAdapterError: Yahoo has no such ticker", TRANSIENT),
        ("mission_market_prices", "last run: orphaned", TRANSIENT),
        ("mission_market_prices", "ConnectionError: yahoo refused the connection", DEPENDENCY_UNAVAILABLE),
        ("mission_catalyst_calendar", "the vendor half failed", TRANSIENT),
        ("mission_consensus", "quota_exhausted", DEPENDENCY_UNAVAILABLE),
        ("mission_consensus", "last run: failed", TRANSIENT),
        ("mission_ownership", "HTTPError: 503", DEPENDENCY_UNAVAILABLE),
        ("mission_ownership", "source_unavailable", DEPENDENCY_UNAVAILABLE),
        ("mission_statements", "no filing found for this company", CONTENT_REFUSED),
        ("mission_statements", "returned no filing with XBRL", CONTENT_REFUSED),
        ("mission_statements", "dispatch carries no lane ticket", TRANSIENT),
        ("mission_sec_quarters", "原始件读不到或哈希不符，不据此排队", CONTENT_REFUSED),
        ("mission_crowd_sources", "openclaw is not connected in this mission", DEPENDENCY_UNAVAILABLE),
        ("document_extraction", "gated:model_reservation_overrun", DEPENDENCY_UNAVAILABLE),
        ("claim_index", "last run: model_unavailable", DEPENDENCY_UNAVAILABLE),
        ("conviction_call", "last run: refused", TRANSIENT),
        ("company_model_forecast", "refused:the driver pack disagrees", CONTENT_REFUSED),
        ("research_task", "skipped:pool_exhausted", DEPENDENCY_UNAVAILABLE),
        ("guidepoint_discovery", "unreadable_last_run", TRANSIENT),
        ("mission_source_discovery", "not_registered:ConnectorError", DEPENDENCY_UNAVAILABLE),
        ("research_task", TASK_62, DEPENDENCY_UNAVAILABLE),
    )

    def test_every_lane_reason_lands_where_a_person_would_put_it(self) -> None:
        for lane, reason, expected in self.VOCABULARY:
            with self.subTest(lane=lane, reason=reason):
                self.assertEqual(classify(reason, lane=lane).failure_class, expected)

    def test_every_dependency_verdict_names_something(self) -> None:
        for lane, reason, expected in self.VOCABULARY:
            if expected != DEPENDENCY_UNAVAILABLE:
                continue
            with self.subTest(lane=lane, reason=reason):
                self.assertTrue(classify(reason, lane=lane).dependency)

    def test_the_lanes_that_took_the_shared_budget_all_use_it(self) -> None:
        """The four counted-budget lanes, migrated off their private copies."""

        import dalton_core.mission_catalyst_lane as catalyst
        import dalton_core.mission_consensus_lane as consensus
        import dalton_core.mission_market_price_lane as prices
        import dalton_core.mission_ownership_lane as ownership

        for module in (prices, catalyst, consensus, ownership):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertIn("lane_budget", source)
                # The private pair is gone, not shadowed.
                self.assertNotIn("self._failures", source)
                self.assertNotIn("self._failure_reason", source)

    def test_each_migrated_lane_parks_under_its_registry_key(self) -> None:
        """A lane parked under a name the cockpit cannot label is invisible."""

        from dalton_core.cockpit_plane import REGISTRY_LANE_LABELS
        import dalton_core.mission_catalyst_lane as catalyst
        import dalton_core.mission_consensus_lane as consensus
        import dalton_core.mission_market_price_lane as prices
        import dalton_core.mission_ownership_lane as ownership

        for module in (prices, catalyst, consensus, ownership):
            with self.subTest(module=module.__name__):
                self.assertIn(module.DRIVER_KEY, REGISTRY_LANE_LABELS)
                self.assertEqual(module.LANE.driver_key, module.DRIVER_KEY)


class BudgetTests(unittest.TestCase):
    def test_permission_refusal_spends_no_budget_and_clears_after_success(self) -> None:
        budget = self.budget()
        decision = budget.record(
            "doc:permission",
            reason="gated:mission does not grant document_extraction writes")
        self.assertEqual(decision.action, "not_permitted")
        self.assertEqual(budget.attempts("doc:permission"), 0)
        self.assertEqual(budget.blocked("doc:permission").action, "not_permitted")
        self.assertEqual(budget.permission_items()[0]["item_key"], "doc:permission")
        budget.clear("doc:permission")
        self.assertIsNone(budget.blocked("doc:permission"))

    def setUp(self) -> None:
        self.now = NOW

    def clock(self) -> datetime:
        return self.now

    def budget(self, **kwargs) -> LaneFailureBudget:
        return LaneFailureBudget("research_task", clock=self.clock, **kwargs)

    def test_three_transient_failures_still_hold_the_item(self) -> None:
        budget = self.budget()
        for expected in ("retry", "retry", "held"):
            self.assertEqual(budget.record("acn", reason="boom").action, expected)
        blocked = budget.blocked("acn")
        self.assertEqual(blocked.action, "held")
        self.assertEqual(blocked.failures, 3)

    def test_a_dependency_outage_never_spends_the_budget(self) -> None:
        """Task 62, replayed.

        Fifty desktop-page failures and the item is still parked, not failed:
        the count it would have exhausted is not the count it is charged to.
        """

        budget = self.budget()
        for _ in range(50):
            # The real loop: ask whether the item may run, run it, park the
            # failure.  Fifty rounds, and the transient count is still zero.
            budget.blocked("task:62")
            decision = budget.record("task:62", reason=TASK_62)
            self.assertEqual(decision.action, "parked")
        self.assertEqual(budget.attempts("task:62"), 0)
        self.assertEqual(budget.blocked("task:62").action, "parked")
        self.assertEqual(budget.parked_items()[0]["dependency"], "alphaengine_desktop")

    def test_a_parked_item_is_probed_rather_than_abandoned(self) -> None:
        """A park that could not be probed is the suspension it replaced.

        The first attempt after a park is free -- P14e's "the next tick picks
        it up again" -- and after that one probe per interval.
        """

        budget = self.budget()
        budget.record("task:62", reason=TASK_62)
        self.assertIsNone(budget.blocked("task:62"), "the free probe is admitted")
        budget.record("task:62", reason=TASK_62)
        self.assertEqual(budget.blocked("task:62").action, "parked")
        self.now = NOW + timedelta(seconds=PARK_PROBE_INTERVAL_SECONDS + 1)
        self.assertIsNone(budget.blocked("task:62"), "the interval admits one more")
        self.assertEqual(budget.blocked("task:62").action, "parked")

    def test_one_probe_serves_every_item_on_the_same_dependency(self) -> None:
        budget = self.budget()
        for item in ("task:62", "task:63", "task:64"):
            budget.record(item, reason=TASK_62)
        admitted = [item for item in ("task:62", "task:63", "task:64")
                    if budget.blocked(item) is None]
        self.assertEqual(admitted, ["task:62"], "one probe, not one per item")

    def test_a_successful_run_resumes_everything_on_that_dependency(self) -> None:
        budget = self.budget()
        for item in ("task:62", "task:63"):
            budget.record(item, reason=TASK_62)
        self.assertEqual(budget.clear("task:62"), ["task:62", "task:63"])
        self.assertEqual(budget.parked_items(), [])
        self.assertIsNone(budget.blocked("task:63"))

    def test_a_probe_of_another_lane_s_dependency_leaves_this_one_alone(self) -> None:
        budget = self.budget()
        budget.record("task:62", reason=TASK_62)
        budget.record("task:70", reason="ConnectionError: sec.gov did not answer")
        self.assertEqual(budget.dependency_answered("sec"), ["task:70"])
        self.assertEqual([row["item_key"] for row in budget.parked_items()], ["task:62"])

    def test_refused_content_is_terminal_and_never_retried(self) -> None:
        budget = self.budget()
        decision = budget.record("doc:9", reason="the scan is unreadable")
        self.assertEqual(decision.action, "terminal")
        for _ in range(5):
            self.assertEqual(budget.blocked("doc:9").action, "terminal")
        # Nothing resumes it: it was a statement about bytes, not about a source.
        budget.dependency_answered("alphaengine")
        budget.clear("doc:9")
        self.assertEqual(budget.blocked("doc:9").action, "terminal")
        self.assertEqual(budget.terminal_items()[0]["item_key"], "doc:9")

    def test_terminal_supersedes_an_earlier_park_for_the_same_item(self) -> None:
        budget = self.budget()
        budget.record("doc:9", reason=TASK_62)
        budget.record("doc:9", reason="the scan is unreadable")
        self.assertEqual(budget.blocked("doc:9").action, "terminal")
        self.assertEqual(budget.parked_items(), [])

    def test_the_summary_counts_the_three_classes_apart(self) -> None:
        budget = self.budget()
        budget.record("a", reason=TASK_62)
        budget.record("b", reason="the scan is unreadable")
        for _ in range(3):
            budget.record("c", reason="boom")
        summary = budget.summary()
        self.assertEqual(
            (summary["parked"], summary["terminal"], summary["held"]), (1, 1, 1))
        self.assertEqual(summary["dependencies"], ["alphaengine_desktop"])
        self.assertEqual(summary["ledger"], "unused")

    def test_a_broken_ledger_is_reported_and_does_not_fail_the_tick(self) -> None:
        class Broken:
            def append_event(self, **kwargs):
                raise OSError("disk is gone")

            def append_dependency_ok(self, **kwargs):
                raise OSError("disk is gone")

        budget = self.budget(ledger=Broken())
        self.assertEqual(budget.record("a", reason=TASK_62).action, "parked")
        self.assertEqual(budget.ledger_status, "unrecorded:OSError")


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = NOW

    def clock(self) -> datetime:
        return self.now

    def test_the_schema_refuses_an_update_or_a_delete(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite") as ledger:
            ledger.append_event(
                lane="research_task", item_key="task:62", event="parked",
                failure_class=DEPENDENCY_UNAVAILABLE, dependency="alphaengine_desktop",
                reason=TASK_62, rule="alphaengine_no_module_page", at=NOW)
            for statement in (
                "UPDATE lane_failure_events SET reason='fine'",
                "DELETE FROM lane_failure_events",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        ledger.connection.execute(statement)

    def test_the_same_event_twice_is_one_row(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite") as ledger:
            first = ledger.append_event(
                lane="research_task", item_key="task:62", event="parked",
                failure_class=DEPENDENCY_UNAVAILABLE, dependency="alphaengine_desktop",
                reason=TASK_62, rule="r", at=NOW)
            second = ledger.append_event(
                lane="research_task", item_key="task:62", event="parked",
                failure_class=DEPENDENCY_UNAVAILABLE, dependency="alphaengine_desktop",
                reason=TASK_62, rule="r", at=NOW)
            self.assertEqual(first["status"], "recorded")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(len(ledger.events()), 1)

    def test_an_unknown_event_or_class_is_refused(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite") as ledger:
            with self.assertRaises(LaneFailureLedgerError):
                ledger.append_event(
                    lane="l", item_key="i", event="invented",
                    failure_class=TRANSIENT, reason="r", rule="r")
            with self.assertRaises(LaneFailureLedgerError):
                ledger.append_event(
                    lane="l", item_key="i", event="parked",
                    failure_class="invented", reason="r", rule="r")

    def test_a_read_only_ledger_refuses_to_write(self) -> None:
        path = self.root / "l.sqlite"
        with LaneFailureLedger(path) as ledger:
            ledger.append_event(
                lane="l", item_key="i", event="parked",
                failure_class=DEPENDENCY_UNAVAILABLE, dependency="d",
                reason="r", rule="r", at=NOW)
        with LaneFailureLedger(path, read_only=True) as ledger:
            with self.assertRaises(LaneFailureLedgerError):
                ledger.append_event(
                    lane="l", item_key="i", event="parked",
                    failure_class=TRANSIENT, reason="r", rule="r")

    def test_the_backlog_groups_by_dependency_with_first_and_last_seen(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite", clock=self.clock) as ledger:
            budget = LaneFailureBudget(
                "research_task", ledger=ledger, clock=self.clock)
            budget.record("task:62", reason=TASK_62)
            self.now = NOW + timedelta(hours=5)
            budget.record("task:62", reason=TASK_62)
            budget.record("task:63", reason=TASK_62)
            backlog = ledger.parked_by_dependency(now=self.now)
        self.assertEqual(backlog["parked_items"], 2)
        bucket = backlog["dependencies"][0]
        self.assertEqual(bucket["dependency"], "alphaengine_desktop")
        self.assertEqual(bucket["item_count"], 2)
        self.assertEqual(bucket["lanes"], ["research_task"])
        item = next(row for row in bucket["items"] if row["item_key"] == "task:62")
        self.assertEqual(item["first_seen"][:19], NOW.isoformat()[:19])
        self.assertEqual(item["last_seen"][:19], (NOW + timedelta(hours=5)).isoformat()[:19])
        self.assertEqual(item["attempts"], 2)

    def test_a_dependency_that_answered_leaves_the_backlog(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite", clock=self.clock) as ledger:
            budget = LaneFailureBudget(
                "research_task", ledger=ledger, clock=self.clock)
            budget.record("task:62", reason=TASK_62)
            budget.record("task:63", reason=TASK_62)
            self.now = NOW + timedelta(hours=1)
            budget.clear("task:62")
            backlog = ledger.parked_by_dependency(now=self.now)
        self.assertEqual(backlog["parked_items"], 0)
        self.assertEqual(backlog["dependencies"], [])

    def test_terminal_items_are_listed_apart_from_parked_ones(self) -> None:
        with LaneFailureLedger(self.root / "l.sqlite", clock=self.clock) as ledger:
            budget = LaneFailureBudget(
                "research_task", ledger=ledger, clock=self.clock)
            budget.record("task:62", reason=TASK_62)
            budget.record("doc:9", reason="the scan is unreadable")
            backlog = ledger.parked_by_dependency(now=self.now)
        self.assertEqual(backlog["parked_items"], 1)
        self.assertEqual(backlog["terminal_count"], 1)
        self.assertEqual(backlog["terminal_items"][0]["item_key"], "doc:9")

    def test_the_fold_runs_on_rows_without_a_database(self) -> None:
        rows = [
            {"lane": "l", "item_key": "i", "event": "parked", "recorded_at": "a",
             "dependency": "sec", "reason": "down", "rule": "r"},
            {"lane": "l", "item_key": "j", "event": "parked", "recorded_at": "b",
             "dependency": "sec", "reason": "down", "rule": "r"},
            {"lane": "l", "item_key": "", "event": "dependency_ok",
             "recorded_at": "c", "dependency": "sec", "reason": "back", "rule": "r"},
        ]
        self.assertEqual(summarise_events(rows)["parked_items"], 0)
        self.assertEqual(summarise_events(rows[:2])["parked_items"], 2)

    def test_a_restart_replays_the_park_instead_of_re_learning_it(self) -> None:
        """The property that makes this append-only rather than a state column.

        A writer that restarts while AlphaEngine is still down must not send
        three fresh children at a dead page before rediscovering it.
        """

        first = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        first.record("task:62", reason=TASK_62)
        self.assertIsNone(first.blocked("task:62"), "the free probe")
        first.record("task:62", reason=TASK_62)
        self.assertEqual(first.blocked("task:62").action, "parked")

        after_restart = lane_budget(
            "research_task", state_dir=self.root, clock=self.clock)
        self.assertEqual(
            [row["item_key"] for row in after_restart.parked_items()], ["task:62"])
        self.assertEqual(
            after_restart.parked_items()[0]["dependency"], "alphaengine_desktop")
        # And it probes once immediately: a restart is the most likely thing to
        # have fixed the dependency.
        self.assertIsNone(after_restart.blocked("task:62"))
        self.assertEqual(after_restart.blocked("task:62").action, "parked")

    def test_a_terminal_verdict_survives_a_restart_too(self) -> None:
        first = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        first.record("doc:9", reason="the scan is unreadable")
        again = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        self.assertEqual(again.blocked("doc:9").action, "terminal")

    def test_replaying_recovery_never_appends_another_recovery(self) -> None:
        first = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        first.record("task:62", reason=TASK_62)
        self.now = NOW + timedelta(seconds=1)
        first.clear("task:62")
        with LaneFailureLedger(default_path(self.root), clock=self.clock) as ledger:
            original = ledger.events(now=self.now)
        for restart in range(3):
            self.now += timedelta(seconds=1)
            restored = lane_budget("research_task", state_dir=self.root, clock=self.clock)
            self.assertEqual(restored.parked_items(), [])
            with LaneFailureLedger(default_path(self.root), clock=self.clock) as ledger:
                self.assertEqual(ledger.events(now=self.now), original, restart)

    def test_equal_timestamp_recovery_follows_append_order(self) -> None:
        with LaneFailureLedger(self.root / "same-clock.sqlite", clock=self.clock) as ledger:
            for index in range(8):
                budget = LaneFailureBudget(f"lane:{index}", ledger=ledger, clock=self.clock)
                budget.record("work", reason=TASK_62)
                budget.clear("work")
                rows = ledger.events(now=self.now, lane=f"lane:{index}")
                self.assertEqual([row["event"] for row in rows], ["parked", "dependency_ok"])
                restored = LaneFailureBudget(f"lane:{index}", clock=self.clock).replay(rows)
                self.assertEqual(restored.parked_items(), [])
            self.assertEqual(ledger.parked_by_dependency(now=self.now)["parked_items"], 0)

    def test_without_a_state_directory_the_budget_still_works(self) -> None:
        budget = lane_budget("research_task", clock=self.clock)
        self.assertEqual(budget.record("a", reason=TASK_62).action, "parked")
        self.assertEqual(budget.ledger_status, "unused")

    def test_obsolete_item_does_not_claim_its_shared_dependency_recovered(self) -> None:
        budget = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        budget.record("company:a|old", reason=TASK_62)
        budget.record("company:b|current", reason=TASK_62)
        self.assertTrue(budget.retire("company:a|old", reason="new company A inputs"))
        self.assertFalse(budget.retire("company:a|old"))
        restored = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        self.assertEqual([r['item_key'] for r in restored.parked_items()], ['company:b|current'])
        with LaneFailureLedger(default_path(self.root), clock=self.clock) as ledger:
            rows = ledger.events(now=self.now)
            self.assertNotIn('dependency_ok', [r['event'] for r in rows])
            self.assertEqual(ledger.parked_by_dependency(now=self.now)['parked_items'], 1)

    def test_retired_permission_and_content_stay_historical_after_restart(self) -> None:
        budget = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        budget.record('permission:old', reason='gated:mission does not grant dossier')
        budget.record('content:old', reason='the scan is unreadable')
        budget.retire('permission:old')
        budget.retire('content:old')
        restored = lane_budget("research_task", state_dir=self.root, clock=self.clock)
        self.assertEqual(restored.permission_items(), [])
        self.assertEqual(restored.terminal_items(), [])
        with LaneFailureLedger(default_path(self.root), clock=self.clock) as ledger:
            self.assertEqual(len(ledger.events(now=self.now)), 4)
            view = ledger.parked_by_dependency(now=self.now)
            self.assertEqual((view['permission_count'], view['terminal_count']), (0, 0))

    def test_the_ledger_file_lands_beside_the_scheduler(self) -> None:
        self.assertEqual(
            default_path(self.root).name, "lane-failure-ledger.sqlite")


class MigratedLaneBehaviourTests(unittest.TestCase):
    """The four counted-budget lanes, seen through their own dispatch results."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_price_lane_parks_a_company_on_a_dead_source(self) -> None:
        from tests.test_mission_market_price_lane import (
            ACN, LaneTestCase, mission,
        )

        case = LaneTestCase("__init__")
        lane = case.coordinator(params=mission(universe=[
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        # Two failures: the first parks, the second spends the free probe.
        for _ in range(2):
            result = lane.dispatch_once()
            self.assertEqual(result["status"], "launched")
            case.launcher.finish(result["ticket_ref"], status="failed", summary={
                "failure_reason": "ConnectionError: yahoo refused the connection"})
            lane._settle_open()
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        row = held["skipped"][0]
        self.assertEqual(row["reason"], "parked")
        self.assertEqual(row["failure_class"], "dependency_unavailable")
        self.assertEqual(row["dependency"], "market_data")
        # And the count it would have exhausted was never charged.
        self.assertEqual(lane.budget.attempts(ACN), 0)


class SchemaRegistrationTests(unittest.TestCase):
    """Rule 9's four places, for the one new schema this slice adds."""

    def test_bootstrap_applies_the_ledger_schema(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES

        self.assertIn(
            ("lane_failure_ledger_schema.sql", "lane-failure-ledger.sqlite"),
            SCHEMA_DATABASES)

    def test_the_rehearsal_knows_who_owns_the_schema(self) -> None:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "rehearse_deploy_for_test",
            Path(__file__).resolve().parents[1] / "scripts" / "rehearse_deploy.py")
        module = importlib.util.module_from_spec(spec)
        # Registered before execution: ``@dataclass`` resolves annotations
        # through ``sys.modules[cls.__module__]``, and a module that is not
        # there yet makes the decorator fail rather than the schema check.
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        owned = {row.schema for row in module.SIDECAR_MIGRATIONS}
        self.assertIn("lane_failure_ledger_schema.sql", owned)
        self.assertIn("lane_failure_ledger_schema.sql", module.known_schema_files())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
