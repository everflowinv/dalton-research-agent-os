"""P14a: the judgement lane on a tick, and the child that spends the money."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.event_judgement import EventJudgementAuthority, pool_state
from dalton_core.event_judgement_cli import (
    MAX_COST_USD,
    missing_write_scopes,
    run_judgement,
    unjudged_events,
)
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.lane_registry import LaunchAgentContext, lane_for_operation
from dalton_core.mission_event_judgement_lane import (
    JUDGE_MODEL_CONFIG,
    LANE,
    VERIFIER_MODEL_CONFIG,
    MissionEventJudgementLaneCoordinator,
    argv_fragment,
    build_launcher,
    newest_unjudged,
)
from dalton_core.model_configurations import model_config_names
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.tracking_cadence import POLICY_PATH
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, P14aHarness
from tests.test_event_judgement import (
    JUDGE_ROUTE,
    PASS,
    REFLECTION,
    REJECT,
    VERIFIER_ROUTE,
    FakeModel,
    decision,
    resolver,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class FakeLauncher:
    def __init__(self, *, configured=True):
        self.configured = configured
        self.started: list[str] = []
        self.tickets: dict[str, dict] = {}

    def start(self, *, batch_ref):
        if not self.configured:
            raise LaneChildRejected("needs a judge and a verifier configuration")
        ticket = {"id": f"event-judgement-run:{len(self.started):024d}",
                  "batch_ref": batch_ref, "status": "running"}
        self.started.append(batch_ref)
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, summary):
        self.tickets[ticket_ref] = {**self.tickets[ticket_ref],
                                    "status": "succeeded", "summary": summary}


class RegistrationTests(unittest.TestCase):
    def test_the_lane_runs_last_and_after_the_tracking_lane(self):
        self.assertEqual(LANE.operation, "dispatch_event_judgement")
        self.assertEqual(LANE.driver_key, "event_judgement")
        self.assertIs(lane_for_operation("dispatch_event_judgement"), LANE)
        self.assertGreater(LANE.order,
                           lane_for_operation("dispatch_mission_tracking").order)
        self.assertGreater(LANE.order, lane_for_operation("dispatch_initial_screen").order)

    def test_both_configurations_or_neither(self):
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            (state / JUDGE_MODEL_CONFIG).write_text("{}", encoding="utf-8")
            # A judge with no verifier would produce decisions whose
            # verification field exists and means nothing.
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            (state / VERIFIER_MODEL_CONFIG).write_text("{}", encoding="utf-8")
            argv = argv_fragment(LaunchAgentContext(state=state))
            self.assertIn("--event-judgement-model-config", argv)
            self.assertIn("--event-verifier-model-config", argv)

    def test_the_policy_is_passed_through_when_it_is_installed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            for config in (JUDGE_MODEL_CONFIG, VERIFIER_MODEL_CONFIG,
                           "tracking-policy.json"):
                (state / config).write_text("{}", encoding="utf-8")
            self.assertIn("--event-judgement-policy",
                          argv_fragment(LaunchAgentContext(state=state)))

    def test_one_configuration_means_no_launcher(self):
        class Args:
            event_judgement_model_config = "/tmp/judge.json"
            event_verifier_model_config = None
            event_judgement_policy = None
            scheduler = None
            db = "/tmp/core.sqlite"

        self.assertIsNone(build_launcher(Args()))

    def test_both_configurations_are_registered_for_a_cap_raise(self):
        # A configuration left out of a day-cap raise keeps naming a superseded
        # policy version and its lane keeps being refused.
        names = model_config_names()
        self.assertIn("event-judgement-model-config.json", names)
        self.assertIn("event-verifier-model-config.json", names)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.launcher = FakeLauncher()
        self.mission = {"id": "coverage-mission-version:us-it-services:1"}
        self.newest = "research-event:abc"
        self.coordinator = MissionEventJudgementLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission,
            pending=lambda mission: self.newest,
        )

    def test_nothing_unjudged_is_idle_and_costs_nothing(self):
        self.newest = None
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_a_batch_is_dispatched_once(self):
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_new_event_is_a_new_batch(self):
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], {"judged": 1})
        self.newest = "research-event:def"
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["settled"]["judged"], 1)

    def test_a_failing_selector_does_not_take_the_tick_down(self):
        def boom(mission):
            raise RuntimeError("no such table")

        coordinator = MissionEventJudgementLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission, pending=boom,
        )
        self.assertEqual(coordinator.dispatch_once()["status"], "unavailable")

    def test_an_uninstalled_lane_is_rejected(self):
        self.launcher.configured = False
        self.assertEqual(self.coordinator.dispatch_once()["status"], "rejected")


class SelectionTests(P14aHarness):
    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)

    def event(self, *, company=ACN, document="alphaengine-doc:1",
              occurred="2026-09-09T10:00:00+00:00"):
        return record_event(
            self.events, company_ref=company, kind="news", occurred_at=occurred,
            source_refs=["source:alphaengine", document],
            payload={"document_ref": document, "source_ref": "source:alphaengine",
                     "spec_ref": "sell-side-reports", "discovery_ref": "d",
                     "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_an_empty_ledger_has_nothing_to_judge(self):
        self.assertIsNone(newest_unjudged(self.store, self.mission))

    def test_the_newest_unjudged_event_names_the_batch(self):
        first = self.event()
        second = self.event(document="alphaengine-doc:2",
                            occurred="2026-09-09T11:00:00+00:00")
        self.assertEqual(newest_unjudged(self.store, self.mission), second["id"])

    def test_a_judged_event_is_never_selected_again(self):
        first = self.event()
        self.judgements.record(
            event=first,
            judgement={"decision": "NO_CHANGE", "action": "no_change",
                       "driver_refs": [], "thesis_refs": [], "because": "b",
                       "citations": [], "model": {"cost_micros": 0}},
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertIsNone(newest_unjudged(self.store, self.mission))
        self.assertEqual(
            unjudged_events(self.events, self.judgements, company_ref=ACN, limit=3), []
        )

    def test_the_batch_is_bounded_per_company(self):
        for index in range(5):
            self.event(document=f"alphaengine-doc:{index}")
        batch = unjudged_events(self.events, self.judgements, company_ref=ACN, limit=2)
        self.assertEqual(len(batch), 2)


class GrantTests(P14aHarness):
    grants = ()

    def test_the_words_the_mission_has_not_granted_are_reported(self):
        # The fixture mission already grants forecast_line, so what is missing
        # is the note and the ADR-0007 proposal.
        self.assertEqual(
            missing_write_scopes(self.mission),
            ["deliverable", "thesis_revision_candidate"],
        )

    def test_the_deliverable_word_is_the_one_that_gates_the_lane(self):
        self.grant("deliverable")
        self.assertNotIn("deliverable", missing_write_scopes(self.mission))


class ChildTests(P14aHarness):
    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.pass_screen(ACN)

    def event(self, document="alphaengine-doc:1"):
        return record_event(
            self.events, company_ref=ACN, kind="news",
            occurred_at="2026-09-09T10:00:00+00:00",
            source_refs=["source:alphaengine", document],
            payload={"document_ref": document, "source_ref": "source:alphaengine",
                     "spec_ref": "sell-side-reports", "discovery_ref": "d",
                     "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def run_child(self, *, judge_replies=None, verifier_replies=None, **kwargs):
        return run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel(judge_replies or [decision()], route=JUDGE_ROUTE),
            verifier_model=FakeModel(verifier_replies or [PASS], route=VERIFIER_ROUTE),
            family_resolver=resolver(), **kwargs,
        )

    def test_nothing_unjudged_is_idle(self):
        summary = self.run_child()
        self.assertEqual(summary["judgement_status"], "nothing_unjudged")

    def test_one_event_is_judged_recorded_and_never_judged_twice(self):
        self.event()
        summary = self.run_child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["decisions"], {"NO_CHANGE": 1})
        self.assertEqual(summary["actions"], {"no_change": 1})
        again = self.run_child()
        self.assertEqual(again["judgement_status"], "nothing_unjudged")
        self.assertEqual(self.judgements.judged_count(ACN), 1)

    def test_a_verifier_rejection_leaves_the_event_unjudged_and_the_reason_visible(self):
        self.event()
        summary = self.run_child(verifier_replies=[REJECT])
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["effects"][0]["findings"][0]["code"],
                         "decision_not_supported_by_the_event")
        self.assertEqual(self.judgements.judged_count(ACN), 0)

    def test_a_refused_output_is_not_recorded_as_a_decision(self):
        self.event()
        summary = self.run_child(judge_replies=["not json at all"])
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertIn("did not return an object", summary["effects"][0]["reason"])

    def test_the_note_path_publishes_a_deliverable(self):
        self.event()
        summary = self.run_child(judge_replies=[decision(
            action="note", word="THESIS_WEAKENED",
            citations=["alphaengine-doc:1"],
            note="一份卖方报告重申了买入评级，没有改变我们对 driver 的看法。",
        )])
        self.assertEqual(summary["actions"], {"note": 1})
        self.assertEqual(summary["effects"][0]["kind"], "note")
        self.assertEqual(summary["effects"][0]["status"], "fresh")

    def test_the_pool_is_accounted_and_reported(self):
        self.event()
        summary = self.run_child()
        self.assertEqual(summary["pool"]["pool"], "event_response")
        self.assertEqual(summary["cost_micros"], 40_000)
        after = pool_state(self.judgements, self.mission, day="2026-09-09")
        self.assertEqual(after["spent_micros"], 40_000)

    def test_an_exhausted_pool_stops_the_batch_with_the_c2_word(self):
        for index in range(3):
            self.event(document=f"alphaengine-doc:{index}")
        # Spend the whole pool first.
        expensive = FakeModel([decision()] * 3, route=JUDGE_ROUTE,
                              cost_micros=int(MAX_COST_USD * 1_000_000) * 200)
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW, judge_model=expensive,
            verifier_model=FakeModel([PASS] * 3, route=VERIFIER_ROUTE,
                                     cost_micros=0),
            family_resolver=resolver(),
        )
        self.assertEqual(summary["judgement_status"], "skipped:pool_exhausted")
        self.assertLess(summary["judged"], 3)

    def test_a_run_without_both_models_is_gated_rather_than_half_verified(self):
        self.event()
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel([decision()]), verifier_model=None,
            family_resolver=resolver(),
        )
        self.assertEqual(summary["judgement_status"], "gated")
        self.assertIn("independent verifier", summary["failure_reason"])

    def test_a_dry_run_makes_no_call(self):
        self.event()
        summary = self.run_child(dry_run=True)
        self.assertEqual(summary["judgement_status"], "dry_run")
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(self.judgements.judged_count(ACN), 0)

    def test_the_summary_is_always_written(self):
        self.run_child()
        summary = json.loads((self.state_dir / "judge" / "summary.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], "0.1")

    def test_an_untracked_company_s_events_are_not_judged(self):
        record_event(
            self.events, company_ref=CTSH, kind="news",
            occurred_at="2026-09-09T10:00:00+00:00",
            source_refs=["source:alphaengine", "alphaengine-doc:9"],
            payload={"document_ref": "alphaengine-doc:9",
                     "source_ref": "source:alphaengine", "spec_ref": None,
                     "discovery_ref": None, "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        summary = self.run_child()
        self.assertEqual(summary["judgement_status"], "nothing_unjudged")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ReflectionRunTests(P14aHarness):
    """The owner's third instruction, end to end through the child."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.pass_screen(ACN)

    def divergence(self):
        return record_event(
            self.events, company_ref=ACN, kind="price_divergence",
            occurred_at="2026-09-09T00:00:00+00:00",
            source_refs=["market-price-series-version:1", "thesis-version:acn"],
            payload={"window_days": 10, "from_date": "2026-08-26",
                     "as_of": "2026-09-09", "cumulative_return_percent": "-8.0000",
                     "basket_return_percent": "0.0000",
                     "excess_vs_basket_percent": "-8.0000", "basket_members": 4,
                     "thesis_ref": "thesis-version:acn", "thesis_stance": "long",
                     "divergence_percent": "8.0000", "threshold_percent": "6.0",
                     "price_version_ref": "market-price-series-version:1"},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def run_child(self, *, judge_replies, verifier_replies):
        return run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel(judge_replies, route=JUDGE_ROUTE),
            verifier_model=FakeModel(verifier_replies, route=VERIFIER_ROUTE),
            family_resolver=resolver(),
        )

    def reflection_for(self, event):
        return {**REFLECTION, "citations": [event["id"]]}

    def test_a_no_change_on_a_divergence_still_writes_down_why_we_hold(self):
        event = self.divergence()
        summary = self.run_child(
            judge_replies=[decision(), self.reflection_for(event)],
            verifier_replies=[PASS, PASS],
        )
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["decisions"], {"NO_CHANGE": 1})
        self.assertEqual(summary["reflections"], 1)
        written = self.judgements.reflections(ACN)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["trigger_kind"], "price_divergence")
        self.assertFalse(written[0]["market_view_vs_ours"]["available"])
        self.assertTrue(written[0]["convergence_pathway"])

    def test_the_follow_ups_are_reported_as_candidates_not_written(self):
        from dalton_core.tracking_cadence import TrackingCadenceAuthority

        event = self.divergence()
        summary = self.run_child(
            judge_replies=[decision(), self.reflection_for(event)],
            verifier_replies=[PASS, PASS],
        )
        followups = summary["followups"][0]
        self.assertEqual(followups["tracking"][0]["source_key"], "alphaengine")
        self.assertEqual(followups["research"][0]["question"],
                         "Did a federal contract stop?")
        self.assertIsNone(TrackingCadenceAuthority(self.store).latest(ACN, "alphaengine"))

    def test_a_refused_reflection_leaves_the_judgement_and_says_so(self):
        event = self.divergence()
        summary = self.run_child(
            judge_replies=[decision(), "not a reflection"],
            verifier_replies=[PASS, PASS],
        )
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["reflections"], 0)
        self.assertEqual(summary["reflections_refused"], 1)
        self.assertEqual(self.judgements.reflections(ACN), [])

    def test_an_ordinary_event_owes_no_reflection_and_pays_for_none(self):
        record_event(
            self.events, company_ref=ACN, kind="news",
            occurred_at="2026-09-09T10:00:00+00:00",
            source_refs=["source:alphaengine", "alphaengine-doc:1"],
            payload={"document_ref": "alphaengine-doc:1",
                     "source_ref": "source:alphaengine", "spec_ref": None,
                     "discovery_ref": None, "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )
        summary = self.run_child(judge_replies=[decision()], verifier_replies=[PASS])
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["reflections"], 0)
        self.assertEqual(summary["cost_micros"], 40_000)
