"""P14a: the judgement lane on a tick, and the child that spends the money."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.event_judgement import EventJudgementAuthority, pool_state
from dalton_core.event_judgement_cli import (
    MAX_COST_USD,
    config_fingerprint,
    group_evidence_events,
    missing_write_scopes,
    run_judgement,
    same_routing_policy,
    unjudged_event_groups,
    unjudged_events,
)
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.event_judgement_launcher import EventJudgementLauncher
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
from dalton_core.research_event import PAYLOAD_FIELDS, ResearchEventAuthority, record_event
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

    def start(self, *, batch_ref, company_ref=None, event_ref=None, group_key=None):
        if not self.configured:
            raise LaneChildRejected("needs a judge and a verifier configuration")
        ticket = {"id": f"event-judgement-run:{len(self.started):024d}",
                  "batch_ref": batch_ref, "company_ref": company_ref,
                  "event_ref": event_ref, "group_key": group_key,
                  "status": "running"}
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

    def test_launcher_binds_the_selected_company_and_group_event(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            judge, verifier = state / "judge.json", state / "verifier.json"
            judge.write_text("{}")
            verifier.write_text("{}")
            launcher = EventJudgementLauncher(
                state_dir=state, judge_model_config=judge,
                verifier_model_config=verifier,
            )
            self.addCleanup(launcher.close)
            command = launcher._command(
                ticket_dir=state, company_ref=ACN, event_ref="research-event:chosen")
            self.assertEqual(command[command.index("--company-ref") + 1], ACN)
            self.assertEqual(command[command.index("--event-ref") + 1],
                             "research-event:chosen")

    def test_launcher_adopts_an_exact_finished_ticket_after_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            judge, verifier = state / "judge.json", state / "verifier.json"
            judge.write_text("{}")
            verifier.write_text("{}")
            first_launcher = EventJudgementLauncher(
                state_dir=state, judge_model_config=judge,
                verifier_model_config=verifier,
            )
            first = first_launcher.start(
                batch_ref="group|configuration:x", company_ref=ACN,
                event_ref="research-event:chosen", group_key="group")
            first_launcher.wait(timeout=30)
            settled = first_launcher.status(first["id"])
            first_launcher.close()
            log = Path(settled["command"][settled["command"].index("--summary-dir") + 1]) / "run.log"
            before = log.read_bytes()

            restarted = EventJudgementLauncher(
                state_dir=state, judge_model_config=judge,
                verifier_model_config=verifier,
            )
            self.addCleanup(restarted.close)
            adopted = restarted.start(
                batch_ref="group|configuration:x", company_ref=ACN,
                event_ref="research-event:chosen", group_key="group")
            self.assertEqual(adopted["id"], first["id"])
            self.assertNotEqual(adopted["status"], "running")
            self.assertEqual(log.read_bytes(), before)


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
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
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

    def test_a_refused_newest_group_does_not_starve_the_next_company(self):
        candidates = [
            {"company_ref": ACN, "event_ref": "event:new", "group_key": "group:a"},
            {"company_ref": CTSH, "event_ref": "event:older", "group_key": "group:b"},
        ]
        coordinator = MissionEventJudgementLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission,
            pending=lambda mission: candidates,
        )
        first = coordinator.dispatch_once()
        self.assertEqual(first["company_ref"], ACN)
        self.launcher.settle(first["ticket_ref"], {
            "judgement_status": "refused", "judged": 0, "refused": 1,
            "failure_reason": "the verifier rejected the event content",
        })
        second = coordinator.dispatch_once()
        self.assertEqual(second["status"], "launched")
        self.assertEqual(second["company_ref"], CTSH)
        self.assertIn("group:a", second["held"])

    def test_content_hold_survives_restart_and_prevents_a_paid_loop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            candidate = [{"company_ref": ACN, "event_ref": "event:1",
                          "group_key": "group:durable"}]
            first = MissionEventJudgementLaneCoordinator(
                launcher=self.launcher, mission=lambda: self.mission,
                pending=lambda mission: candidate, failure_ledger_dir=name,
            )
            launched = first.dispatch_once()
            self.launcher.settle(launched["ticket_ref"], {
                "judgement_status": "refused", "judged": 0, "refused": 1,
                "failure_reason": "the verifier rejected the event content",
            })
            self.assertEqual(first.dispatch_once()["status"], "held")
            restarted = MissionEventJudgementLaneCoordinator(
                launcher=self.launcher, mission=lambda: self.mission,
                pending=lambda mission: candidate, failure_ledger_dir=name,
            )
            self.assertEqual(restarted.dispatch_once()["status"], "held")
            self.assertEqual(len(self.launcher.started), 1)

    def test_busy_outcome_gets_only_the_bounded_transient_retries(self):
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            candidate = [{"company_ref": ACN, "event_ref": "event:busy",
                          "group_key": "group:busy"}]
            coordinator = MissionEventJudgementLaneCoordinator(
                launcher=self.launcher, mission=lambda: self.mission,
                pending=lambda mission: candidate, failure_ledger_dir=name,
            )
            for _ in range(3):
                launched = coordinator.dispatch_once()
                self.assertEqual(launched["status"], "launched")
                self.launcher.settle(launched["ticket_ref"], {
                    "judgement_status": "busy", "judged": 0, "refused": 0,
                    "failure_reason": "child_slot_busy",
                })
            self.assertEqual(coordinator.dispatch_once()["status"], "held")
            self.assertEqual(len(self.launcher.started), 3)


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

    def test_events_outside_the_current_mission_are_not_selected(self):
        outside = self.event(company=CTSH, document="alphaengine-doc:outside",
                             occurred="2026-09-09T12:00:00+00:00")
        inside = self.event(document="alphaengine-doc:inside",
                            occurred="2026-09-09T11:00:00+00:00")
        mission = {**self.mission, "universe": [{"company_ref": ACN}]}
        self.assertNotEqual(outside["id"], inside["id"])
        self.assertEqual(newest_unjudged(self.store, mission), inside["id"])

    def test_an_event_from_another_mission_is_not_selected(self):
        other_params = dict(self.params)
        other_params["idempotency_key"] = "mission-other"
        other_params["version_id"] = "coverage-mission-version:other:1"
        other_params["autonomy"] = {
            **other_params["autonomy"],
            "may_write": list(dict.fromkeys(
                [*other_params["autonomy"]["may_write"], "market_event"])),
        }
        other = self.missions.create_mission(
            "coverage-mission:other", **other_params)
        foreign = record_event(
            self.events, company_ref=ACN, kind="news",
            occurred_at="2026-09-09T12:00:00+00:00",
            source_refs=["source:alphaengine", "alphaengine-doc:foreign"],
            payload={"document_ref": "alphaengine-doc:foreign",
                     "source_ref": "source:alphaengine", "spec_ref": "sell-side-reports",
                     "discovery_ref": "d", "title": None, "host": None},
            mission=other, actor_ref=AUTOMATION,
        )
        inside = self.event(document="alphaengine-doc:inside",
                            occurred="2026-09-09T11:00:00+00:00")
        self.assertNotEqual(foreign["id"], inside["id"])
        self.assertEqual(newest_unjudged(self.store, self.mission), inside["id"])

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

    def test_child_selection_reads_only_the_group_named_by_the_ticket(self):
        self.pass_screen(ACN)
        first = self.event(document="alphaengine-doc:first",
                           occurred="2026-09-09T10:00:00+00:00")
        self.event(document="alphaengine-doc:second",
                   occurred="2026-09-09T11:00:00+00:00")
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "target-summary",
            company_ref=ACN, event_ref=first["id"], dry_run=True, now=NOW,
        )
        self.assertEqual(summary["judgement_status"], "dry_run")
        self.assertEqual(summary["candidates"], 1)

    def test_monthly_buyback_rows_share_one_slot_without_displacing_form_4(self):
        from tests.test_buyback_disclosure import buyback_event, table_payload
        from tests.test_insider_context import event as insider_event, insider_payload

        for month in ("March", "April", "May"):
            candidate = buyback_event(table_payload(period_label=month))
            record_event(
                self.events, company_ref=ACN, kind=candidate["kind"],
                occurred_at=candidate["occurred_at"],
                source_refs=candidate["source_refs"], payload=candidate["payload"],
                mission=self.mission, actor_ref=AUTOMATION,
            )
        candidate = insider_event(insider_payload())
        record_event(
            self.events, company_ref=ACN, kind=candidate["kind"],
            occurred_at=candidate["occurred_at"],
            source_refs=candidate["source_refs"], payload=candidate["payload"],
            mission=self.mission, actor_ref=AUTOMATION,
        )
        groups = unjudged_event_groups(
            self.events, self.judgements, company_ref=ACN, limit=2
        )
        self.assertEqual([len(group) for group in groups], [3, 1])
        self.assertEqual(groups[1][0]["kind"], "insider_transaction")


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

    def hk_buyback(self, day: str, week: str, *, company_ref=ACN):
        payload = {field: None for field in PAYLOAD_FIELDS["buyback_disclosure"]}
        payload.update({
            "disclosure_kind": "next_day_return", "accession": day,
            "filing_date": day, "period_start": day, "period_end": day,
            "period_label": day, "shares_purchased": "230000",
            "average_price_paid": "600.00", "total_paid": "138000000",
            "currency": "HKD", "market": "HK", "source_ref": "source:hkex",
            "excerpt": f"{day} 230000 shares", "excerpt_hash": day.replace("-", "") * 4,
            "invocation_ref": f"connector-invocation:hkex:{day}",
            "artifact_hash": (day.replace("-", "") * 7)[:64],
            "event_key": f"hk:{company_ref}:{day}",
            "cumulative_shares": "44382700", "cumulative_basis": "since_mandate",
            "cluster_key": f"{week}:{day}",
        })
        return record_event(
            self.events, company_ref=company_ref, kind="buyback_disclosure",
            occurred_at=f"{day}T00:00:00+08:00",
            source_refs=[payload["invocation_ref"]], payload=payload,
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def run_child(self, *, judge_replies=None, verifier_replies=None, **kwargs):
        judge_model = kwargs.pop(
            "judge_model", FakeModel(judge_replies or [decision()], route=JUDGE_ROUTE))
        verifier_model = kwargs.pop(
            "verifier_model", FakeModel(verifier_replies or [PASS], route=VERIFIER_ROUTE))
        return run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=judge_model, verifier_model=verifier_model,
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

    def test_one_buyback_table_and_one_form_4_use_two_calls_and_process_four_rows(self):
        from tests.test_buyback_disclosure import buyback_event, table_payload
        from tests.test_insider_context import event as insider_event, insider_payload

        for month in ("March", "April", "May"):
            candidate = buyback_event(table_payload(period_label=month))
            record_event(
                self.events, company_ref=ACN, kind=candidate["kind"],
                occurred_at=candidate["occurred_at"],
                source_refs=candidate["source_refs"], payload=candidate["payload"],
                mission=self.mission, actor_ref=AUTOMATION,
            )
        candidate = insider_event(insider_payload())
        record_event(
            self.events, company_ref=ACN, kind=candidate["kind"],
            occurred_at=candidate["occurred_at"],
            source_refs=candidate["source_refs"], payload=candidate["payload"],
            mission=self.mission, actor_ref=AUTOMATION,
        )
        judge_model = FakeModel([decision(), decision()], route=JUDGE_ROUTE)
        verifier_model = FakeModel([PASS, PASS], route=VERIFIER_ROUTE)
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW, per_company=2,
            judge_model=judge_model,
            verifier_model=verifier_model,
            family_resolver=resolver(),
        )
        self.assertEqual(summary["judged"], 2)
        self.assertEqual(self.judgements.judged_count(ACN), 4)
        self.assertEqual(len(judge_model.prompts), 2)
        self.assertIn("Other monthly rows in this filing", judge_model.prompts[0])
        self.assertIn("Other monthly rows in this filing", verifier_model.prompts[0])
        ledger_cost = self.store.connection.execute(
            "SELECT SUM(cost_micros) AS cost FROM event_judgements"
        ).fetchone()["cost"]
        self.assertEqual(ledger_cost, 80_000)
        alias_costs = self.store.connection.execute(
            "SELECT cost_micros FROM event_judgements "
            "WHERE json_extract(record_json,'$.effect.kind')='grouped_judgement'"
        ).fetchall()
        self.assertEqual([row["cost_micros"] for row in alias_costs], [0, 0])
        self.assertEqual(
            unjudged_events(self.events, self.judgements, company_ref=ACN, limit=5), []
        )

    def test_hk_week_waits_until_closed_then_late_input_rejudges_full_week(self):
        # NOW is Wednesday in W37. Daily W37 rows remain raw and unjudged.
        self.hk_buyback("2026-09-08", "2026-W37")
        self.hk_buyback("2026-09-09", "2026-W37")
        waiting_judge = FakeModel([decision()], route=JUDGE_ROUTE)
        waiting = self.run_child(judge_model=waiting_judge,
                                 verifier_replies=[PASS])
        self.assertEqual(waiting["judgement_status"], "nothing_unjudged")
        self.assertEqual(waiting_judge.prompts, [])

        first = self.hk_buyback("2026-09-01", "2026-W36")
        second = self.hk_buyback("2026-09-02", "2026-W36")
        judge_model = FakeModel([decision()], route=JUDGE_ROUTE)
        verifier_model = FakeModel([PASS], route=VERIFIER_ROUTE)
        summary = self.run_child(judge_model=judge_model,
                                 verifier_model=verifier_model)
        self.assertEqual(summary["judged"], 1)
        self.assertIn(first["id"], judge_model.prompts[0])
        self.assertIn(second["id"], judge_model.prompts[0])
        self.assertIn("HK daily rows in this closed ISO week", judge_model.prompts[0])
        self.assertIn(second["id"], verifier_model.prompts[0])
        first_work = self.store.connection.execute(
            "SELECT json_extract(record_json,'$.model.work_order_ref') AS ref "
            "FROM event_judgements WHERE event_ref=?", (first["id"],),
        ).fetchone()["ref"]

        late = self.hk_buyback("2026-09-03", "2026-W36")
        late_judge = FakeModel([decision()], route=JUDGE_ROUTE)
        late_verifier = FakeModel([PASS], route=VERIFIER_ROUTE)
        again = self.run_child(judge_model=late_judge,
                               verifier_model=late_verifier)
        self.assertEqual(again["judged"], 1)
        for event in (first, second, late):
            self.assertIn(event["id"], late_judge.prompts[0])
            self.assertIn(event["id"], late_verifier.prompts[0])
        late_work = self.store.connection.execute(
            "SELECT json_extract(record_json,'$.model.work_order_ref') AS ref "
            "FROM event_judgements WHERE event_ref=?", (late["id"],),
        ).fetchone()["ref"]
        self.assertNotEqual(first_work, late_work)
        self.assertEqual(len(self.events.events(company_ref=ACN, limit=20)), 5)
        rows = self.store.connection.execute(
            "SELECT cost_micros, json_extract(record_json,'$.effect.kind') AS kind "
            "FROM event_judgements WHERE event_ref IN (?,?,?)",
            (first["id"], second["id"], late["id"]),
        ).fetchall()
        self.assertEqual(sum(row["cost_micros"] for row in rows), 80_000)
        self.assertEqual([row["cost_micros"] for row in rows
                          if row["kind"] == "grouped_judgement"], [0])

    def test_hk_week_groups_are_isolated_by_company_and_week(self):
        a_w35 = self.hk_buyback("2026-08-26", "2026-W35")
        a_w36 = self.hk_buyback("2026-09-01", "2026-W36")
        b_w36 = self.hk_buyback("2026-09-02", "2026-W36", company_ref=CTSH)
        acn = unjudged_event_groups(
            self.events, self.judgements, company_ref=ACN, limit=3, now=NOW)
        ctsh = unjudged_event_groups(
            self.events, self.judgements, company_ref=CTSH, limit=3, now=NOW)
        self.assertEqual([[row["id"] for row in group] for group in acn],
                         [[a_w35["id"]], [a_w36["id"]]])
        self.assertEqual([[row["id"] for row in group] for group in ctsh],
                         [[b_w36["id"]]])
        self.assertEqual(
            [row["company_ref"] for row in group_evidence_events(self.events, ctsh[0])],
            [CTSH],
        )

    def test_a_verifier_rejection_leaves_the_event_unjudged_and_the_reason_visible(self):
        self.event()
        summary = self.run_child(verifier_replies=[REJECT])
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("all attempted event judgements", summary["failure_reason"])
        self.assertEqual(summary["effects"][0]["findings"][0]["code"],
                         "decision_not_supported_by_the_event")
        self.assertEqual(self.judgements.judged_count(ACN), 0)

    def test_a_refused_output_is_not_recorded_as_a_decision(self):
        self.event()
        summary = self.run_child(judge_replies=["not json at all"])
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("did not return an object", summary["effects"][0]["reason"])

    def test_mixed_success_and_refusal_remains_a_partial_success(self):
        self.event(document="alphaengine-doc:1")
        self.event(document="alphaengine-doc:2")
        summary = self.run_child(
            judge_replies=["not json", decision()], verifier_replies=[PASS, PASS])
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["judgement_status"], "partial")
        self.assertEqual((summary["judged"], summary["refused"]), (1, 1))
        self.assertEqual(summary["formal_authority_writes"], 1)

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


class SameFamilyGateTests(P14aHarness):
    """B3: a misconfigured verifier must not poison every event it touches."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.pass_screen(ACN)

    def config(self, name, routing_policy, slot="slot:one"):
        path = self.state_dir / name
        path.write_text(json.dumps({
            "routing_policy_ref": routing_policy,
            "credential_slot_refs": [slot],
            "model_router_db": "/tmp/router.sqlite",
            "broker_socket": "/tmp/broker.sock",
            "broker_auth_key": "/tmp/key", "broker_client_id": "dalton",
            "expected_agent_id": "agent", "budget_db": "/tmp/budget.sqlite",
            "budget_policy_ref": "policy:day",
        }), encoding="utf-8")
        return path

    def event(self, document="alphaengine-doc:1"):
        return record_event(
            self.events, company_ref=ACN, kind="news",
            occurred_at="2026-09-09T10:00:00+00:00",
            source_refs=["source:alphaengine", document],
            payload={"document_ref": document, "source_ref": "source:alphaengine",
                     "spec_ref": None, "discovery_ref": None, "title": None,
                     "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_two_configurations_naming_one_routing_policy_are_caught_for_nothing(self):
        judge_config = self.config("judge.json", "routing-policy:extraction:1")
        verifier_config = self.config(
            "verifier.json", "routing-policy:extraction:1", slot="slot:two")
        self.assertIn("same routing policy",
                      same_routing_policy(judge_config, verifier_config))
        self.event()
        judge_model = FakeModel([decision()], route=JUDGE_ROUTE)
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model_config=judge_config, verifier_model_config=verifier_config,
            judge_model=judge_model,
            verifier_model=FakeModel([PASS], route=VERIFIER_ROUTE),
            family_resolver=resolver(),
        )
        self.assertEqual(summary["judgement_status"], "gated:same_family")
        self.assertEqual(judge_model.prompts, [], "nothing was paid for")

    def test_two_different_policies_pass_the_cheap_check(self):
        self.assertIsNone(same_routing_policy(
            self.config("judge.json", "routing-policy:a:1"),
            self.config("verifier.json", "routing-policy:b:1"),
        ))

    def test_the_run_stops_at_the_first_pair_when_the_route_families_match(self):
        for index in range(4):
            self.event(document=f"alphaengine-doc:{index}")
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel([decision()] * 4, route=JUDGE_ROUTE),
            verifier_model=FakeModel([PASS] * 4, route=VERIFIER_ROUTE),
            family_resolver=resolver({JUDGE_ROUTE: "anthropic",
                                      VERIFIER_ROUTE: "anthropic"}),
        )
        self.assertEqual(summary["judgement_status"], "gated:same_family")
        self.assertEqual(summary["refused"], 1, "one pair, not four")
        self.assertEqual(summary["judged"], 0)

    def test_a_fixed_configuration_makes_the_event_retryable(self):
        # The WorkOrder is content addressed on (purpose, request_id, mission,
        # prompt) and not on the configuration, so without a fingerprint the
        # scheduler would replay the poisoned result for ever.
        broken = config_fingerprint(self.config("judge.json", "routing-policy:a:1"),
                                    self.config("verifier.json", "routing-policy:a:1"))
        fixed = config_fingerprint(self.config("judge.json", "routing-policy:a:1"),
                                   self.config("verifier.json", "routing-policy:b:1"))
        self.assertNotEqual(broken, fixed)
        self.assertEqual(len(broken), 8)

    def test_the_fingerprint_is_stable_for_an_unchanged_configuration(self):
        judge_config = self.config("judge.json", "routing-policy:a:1")
        verifier_config = self.config("verifier.json", "routing-policy:b:1")
        self.assertEqual(config_fingerprint(judge_config, verifier_config),
                         config_fingerprint(judge_config, verifier_config))

    def test_refused_pairs_are_charged_to_the_day(self):
        self.event()
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, now=NOW,
            judge_model=FakeModel(["not json"], route=JUDGE_ROUTE),
            verifier_model=FakeModel([PASS], route=VERIFIER_ROUTE),
            family_resolver=resolver(),
        )
        self.assertEqual(summary["judged"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["cost_micros"], 20_000)
        self.assertEqual(self.judgements.day_cost_micros("2026-09-09"), 20_000)


class UnjudgedSelectionTests(P14aHarness):
    """S3: an anti-join, oldest first, not a window that can be entirely judged."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)

    def event(self, index, day=9):
        return record_event(
            self.events, company_ref=ACN, kind="news",
            occurred_at=f"2026-09-{day:02d}T{index % 24:02d}:00:00+00:00",
            source_refs=["source:alphaengine", f"alphaengine-doc:{index}"],
            payload={"document_ref": f"alphaengine-doc:{index}",
                     "source_ref": "source:alphaengine", "spec_ref": None,
                     "discovery_ref": None, "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def judge_it(self, event):
        self.judgements.record(
            event=event,
            judgement={"decision": "NO_CHANGE", "action": "no_change",
                       "driver_refs": [], "thesis_refs": [], "because": "b",
                       "citations": [], "model": {"cost_micros": 0}},
            verification={"status": "verified", "verdict": "pass", "findings": []},
            effect={"kind": "no_change", "status": "recorded"},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_the_oldest_unjudged_events_come_first(self):
        made = [self.event(index) for index in range(1, 6)]
        batch = unjudged_events(self.events, self.judgements, company_ref=ACN, limit=2)
        self.assertEqual([row["id"] for row in batch], [made[0]["id"], made[1]["id"]])

    def test_an_old_event_is_reached_after_the_newest_window_is_judged(self):
        old = self.event(1, day=1)
        for index in range(2, 30):
            self.judge_it(self.event(index))
        batch = unjudged_events(self.events, self.judgements, company_ref=ACN, limit=3)
        self.assertEqual([row["id"] for row in batch], [old["id"]])

    def test_a_judged_event_is_never_selected_again(self):
        made = self.event(1)
        self.judge_it(made)
        self.assertEqual(
            unjudged_events(self.events, self.judgements, company_ref=ACN, limit=5), []
        )
