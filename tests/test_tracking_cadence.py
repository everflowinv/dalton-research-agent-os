"""P14a: tracking is resident; only the rate is a judgement, and it is versioned."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.metric_base import ACTIVE_COVERAGE_SPINE, STAGE_SPINE, metrics_for
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.tracking_cadence import (
    TrackingCadenceAuthority,
    TrackingCadenceConflict,
    TrackingCadenceValidationError,
    active_coverage_metrics,
    due_sources,
    immediate_pull_sources,
    load_policy,
    next_due,
    screen_passed_companies,
)
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, EPAM, OWNER, P14aHarness

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
POLICY = Path(__file__).resolve().parents[1] / "deploy/phase9/p14a-tracking-policy-v1.json"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY)

    def test_the_shipped_policy_names_every_source_the_owner_named(self):
        for source in ("yfinance", "sec", "alphaengine", "x-xreach", "sales-notes",
                       "guidepoint", "company-wiki", "employee-reviews",
                       "gemini-web-search", "catalyst-calendar"):
            self.assertIn(source, self.policy["cadences"])

    def test_prices_filings_and_the_catalyst_calendar_are_fixed(self):
        self.assertFalse(self.policy["cadences"]["yfinance"]["adjustable"])
        self.assertFalse(self.policy["cadences"]["sec"]["adjustable"])
        self.assertFalse(self.policy["cadences"]["catalyst-calendar"]["adjustable"])

    def test_the_owner_baselines_are_what_the_plan_says(self):
        # AlphaEngine 2/day, X 2/day, sales notes twice a day, wiki and
        # Guidepoint weekly.
        self.assertEqual(self.policy["cadences"]["alphaengine"]["interval_seconds"], 43200)
        self.assertEqual(self.policy["cadences"]["x-xreach"]["interval_seconds"], 43200)
        self.assertEqual(self.policy["cadences"]["sales-notes"]["interval_seconds"], 86400)
        self.assertEqual(self.policy["cadences"]["guidepoint"]["interval_seconds"], 604800)
        self.assertEqual(self.policy["cadences"]["company-wiki"]["interval_seconds"], 604800)

    def test_an_immediate_pull_names_only_sources_with_a_baseline(self):
        self.assertTrue(
            set(self.policy["immediate_pull"]["source_keys"]) <= set(self.policy["cadences"])
        )

    def test_the_divergence_window_and_threshold_are_in_the_policy(self):
        moves = self.policy["abnormal_move"]
        self.assertEqual(moves["window_trading_days"], 10)
        self.assertEqual(moves["divergence_vs_basket_percent"], "6.0")

    def test_a_thesis_is_read_as_long_unless_the_policy_says_otherwise(self):
        # The fund is fundamental long-biased; the assumption is named in one
        # place rather than inferred from an implied_expectation sentence.
        self.assertEqual(self.policy["thesis_stances"]["default"], "long")
        self.assertEqual(self.policy["thesis_stances"]["overrides"], {})

    def test_the_policy_is_content_hashed_so_a_version_can_cite_it(self):
        self.assertEqual(len(self.policy["content_hash"]), 64)

    def test_a_policy_with_no_cadences_is_refused(self, ):
        with self.assertRaises(TrackingCadenceValidationError):
            load_policy_from({"schema_version": "0.1", "policy_ref": "x", "cadences": [],
                              "abnormal_move": {}, "immediate_pull": {}})

    def test_an_immediate_pull_naming_an_unknown_source_is_refused(self):
        with self.assertRaises(TrackingCadenceValidationError) as caught:
            load_policy_from({
                "schema_version": "0.1", "policy_ref": "x",
                "cadences": [{"source_key": "a", "interval_seconds": 3600,
                              "adjustable": True, "because": "b"}],
                "abnormal_move": {}, "immediate_pull": {"source_keys": ["b"]},
            })
        self.assertIn("no baseline cadence", str(caught.exception))


def load_policy_from(value):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(value, handle)
        path = handle.name
    return load_policy(path)


class NextDueTests(unittest.TestCase):
    def setUp(self):
        self.cadence = {"interval_seconds": 43200, "origin": "baseline"}

    def test_a_source_never_pulled_is_due(self):
        answer = next_due(self.cadence, now=NOW, last_pull_at=None)
        self.assertTrue(answer["due"])
        self.assertEqual(answer["reason"], "never pulled")

    def test_a_source_pulled_within_the_interval_is_not_due(self):
        answer = next_due(self.cadence, now=NOW,
                          last_pull_at=(NOW - timedelta(hours=2)).isoformat())
        self.assertFalse(answer["due"])
        self.assertEqual(answer["due_at"], (NOW + timedelta(hours=10)).isoformat(timespec="seconds"))

    def test_a_source_past_its_interval_is_due(self):
        answer = next_due(self.cadence, now=NOW,
                          last_pull_at=(NOW - timedelta(hours=13)).isoformat())
        self.assertTrue(answer["due"])

    def test_an_event_pulls_a_source_forward_whatever_the_interval_says(self):
        answer = next_due(self.cadence, now=NOW,
                          last_pull_at=(NOW - timedelta(minutes=1)).isoformat(),
                          pulled_forward_by="research-event:abc")
        self.assertTrue(answer["due"])
        self.assertEqual(answer["origin"], "immediate")
        self.assertIn("research-event:abc", answer["reason"])


class ImmediatePullTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY)

    def event(self, kind="price_move", minutes_ago=30):
        return {
            "id": "research-event:move",
            "kind": kind,
            "occurred_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def test_a_fresh_price_move_pulls_the_three_news_sources_forward(self):
        pulled = immediate_pull_sources(self.policy, [self.event()], NOW)
        self.assertEqual(set(pulled), {"alphaengine", "x-xreach", "gemini-web-search"})

    def test_a_stale_move_pulls_nothing(self):
        # An event from last week does not justify a pull today, or the cadence
        # would be permanently meaningless.
        self.assertEqual(
            immediate_pull_sources(self.policy, [self.event(minutes_ago=60 * 24 * 7)], NOW), {}
        )

    def test_a_news_event_is_not_a_trigger(self):
        self.assertEqual(immediate_pull_sources(self.policy, [self.event(kind="news")], NOW), {})


class MembershipTests(P14aHarness):
    def test_a_company_with_a_passed_screen_is_tracked(self):
        self.pass_screen(ACN)
        self.assertEqual(screen_passed_companies(self.missions, self.mission), [ACN])

    def test_a_company_still_in_its_screen_is_not(self):
        self.enter_screen(CTSH)
        self.assertEqual(screen_passed_companies(self.missions, self.mission), [])

    def test_tracking_is_resident_across_later_stage_work(self):
        # The owner's instruction: once the screen passes, the company is
        # tracked daily whatever else the brain is doing -- deeper coverage of
        # it, the next company's screen, an open research task. Membership is
        # computed from the one monotone fact, so nothing can switch it off.
        self.pass_screen(ACN)
        self.missions.record_stage(
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
            company_ref=ACN, stage_ref="deep_insight_gate", status="entered",
            evidence_refs=[self.mission["id"]], rationale="deeper coverage started",
            actor_ref=AUTOMATION,
            idempotency_key=f"{self.mission['id']}:{ACN}:deep_insight_gate:entered",
        )
        self.enter_screen(EPAM)
        self.assertIn(ACN, screen_passed_companies(self.missions, self.mission))

    def test_the_order_follows_the_mission_universe(self):
        self.pass_screen(EPAM)
        self.pass_screen(ACN)
        self.assertEqual(
            screen_passed_companies(self.missions, self.mission), [ACN, EPAM]
        )

    def test_residency_survives_the_next_mission_version(self):
        # A stage record binds the version it was written under, and a new
        # version does not copy the old records forward. Reading only the
        # current version would drop all four covered companies out of
        # tracking on the day the owner published the grant that lets tracking
        # write anything at all.
        self.pass_screen(ACN)
        first_version = self.mission["id"]
        self.assertEqual(screen_passed_companies(self.missions, self.mission), [ACN])
        self.grant("consensus_estimate")
        self.assertNotEqual(self.mission["id"], first_version)
        self.assertEqual(
            [record["stage_ref"] for record
             in self.missions.stage_records(self.mission["id"])],
            [],
            "the new version carries no stage records of its own",
        )
        self.assertEqual(screen_passed_companies(self.missions, self.mission), [ACN])

    def test_no_stage_record_is_written(self):
        # Owner decision 2026-09-09: resident tracking is not the Playbook's
        # sixth research stage. Two different things, one name; this module
        # writes no stage record.
        self.pass_screen(ACN)
        screen_passed_companies(self.missions, self.mission)
        stages = {record["stage_ref"]
                  for record in self.missions.stage_records(self.mission["id"])}
        self.assertEqual(stages, {"initial_screen"})


class SpineTests(P14aHarness):
    def test_active_coverage_now_declares_figures(self):
        self.assertEqual(STAGE_SPINE["active_coverage"], ACTIVE_COVERAGE_SPINE)
        refs = {item["metric_ref"] for item in metrics_for("active_coverage", ACN)}
        self.assertEqual(refs, {"metric:tracked-events-seen",
                                "metric:tracked-events-judged",
                                "metric:tracked-events-open"})

    def test_the_figures_are_counted_off_the_two_ledgers(self):
        events = ResearchEventAuthority(self.store)
        judgements = EventJudgementAuthority(self.store)
        record_event(
            events, company_ref=ACN, kind="news",
            occurred_at="2026-09-09T00:00:00+00:00",
            source_refs=["source:alphaengine"],
            payload={"document_ref": "alphaengine-doc:1", "source_ref": "source:alphaengine",
                     "spec_ref": None, "discovery_ref": None, "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        counts = active_coverage_metrics(events, judgements, ACN)
        self.assertEqual(counts["metric:tracked-events-seen"], 1)
        self.assertEqual(counts["metric:tracked-events-judged"], 0)
        self.assertEqual(counts["metric:tracked-events-open"], 1)


class CadenceVersionTests(P14aHarness):
    def setUp(self):
        super().setUp()
        self.policy = load_policy(POLICY)
        self.cadences = TrackingCadenceAuthority(self.store)

    def propose(self, *, source="alphaengine", interval=259200, refs=("research-event:1",),
                actor=AUTOMATION, reason="driver_event"):
        return self.cadences.propose(
            company_ref=ACN, source_key=source, interval_seconds=interval,
            because="three AlphaEngine searches in a row returned nothing this "
                    "company did not already hold",
            evidence_refs=list(refs), change_reason=reason, policy=self.policy,
            mission=self.mission, actor_ref=actor,
        )

    def test_the_baseline_applies_until_a_version_exists(self):
        cadence = self.cadences.cadence(ACN, "alphaengine", policy=self.policy)
        self.assertEqual(cadence["origin"], "baseline")
        self.assertEqual(cadence["interval_seconds"], 43200)
        self.assertIsNone(cadence["version_ref"])

    def test_the_brain_can_stretch_a_thin_company_to_three_days(self):
        published = self.propose()
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["version"], 1)
        cadence = self.cadences.cadence(ACN, "alphaengine", policy=self.policy)
        self.assertEqual(cadence["interval_seconds"], 259200)
        self.assertEqual(cadence["origin"], "version")

    def test_a_version_is_never_an_edit(self):
        first = self.propose()
        second = self.propose(interval=604800, refs=("research-event:2",))
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["prior_version_ref"], first["id"])
        chain = self.cadences.versions(ACN, "alphaengine")
        self.assertEqual([row["interval_seconds"] for row in chain], [259200, 604800])

    def test_the_same_proposal_on_the_same_evidence_is_a_duplicate(self):
        self.propose()
        again = self.propose()
        self.assertEqual(again["status"], "duplicate")

    def test_a_proposal_with_no_evidence_is_a_rewrite_and_is_refused(self):
        with self.assertRaises(TrackingCadenceValidationError):
            self.propose(refs=())

    def test_a_fixed_cadence_cannot_be_changed_by_the_brain(self):
        with self.assertRaises(TrackingCadenceConflict) as caught:
            self.propose(source="yfinance", interval=86400)
        self.assertIn("fixed cadence", str(caught.exception))

    def test_a_source_with_no_baseline_has_no_cadence_to_propose(self):
        with self.assertRaises(TrackingCadenceValidationError):
            self.propose(source="bloomberg")

    def test_a_first_version_that_restates_the_baseline_is_a_duplicate(self):
        self.assertEqual(self.propose(interval=43200)["status"], "duplicate")

    def test_an_unknown_change_reason_is_refused(self):
        with self.assertRaises(TrackingCadenceValidationError):
            self.propose(reason="because_i_said_so")

    def test_a_human_may_publish_a_cadence_version(self):
        self.assertEqual(self.propose(actor=OWNER)["status"], "published")

    def test_due_sources_answers_for_every_source_at_once(self):
        answers = due_sources(
            self.cadences, company_ref=ACN, policy=self.policy, now=NOW,
            last_pulls={"alphaengine": (NOW - timedelta(hours=1)).isoformat()},
            events=[],
        )
        self.assertEqual(set(answers), set(self.policy["cadences"]))
        self.assertFalse(answers["alphaengine"]["due"])
        self.assertTrue(answers["guidepoint"]["due"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
