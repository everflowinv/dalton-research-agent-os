"""P15d: the child checks the grant before it spends, and the lane stays quiet.

The invariant worth asserting is not "the queue drained" but "a company we
agree with the market about is never drafted", which is the owner's rule and
the only thing that keeps this lane from producing a weekly newsletter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.conviction_call import (
    CHECKPOINT_KIND,
    WRITE_SCOPE,
    ConvictionCallAuthority,
    week_key,
)
from dalton_core.conviction_call_cli import (
    build_parser,
    checkpointed,
    consensus_gap,
    fingerprint_of,
    gate_inputs,
    granted,
    open_debates,
    run_conviction_call,
)
from dalton_core.conviction_call_launcher import ConvictionCallLauncher, run_digest
from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.lane_registry import (
    lane_for_operation, lane_init_kwargs, registered_lanes, tick_lanes,
)
from dalton_core.mission_conviction_lane import (
    LAUNCHER_KWARG, MissionConvictionLaneCoordinator, argv_fragment, build_launcher,
)
from tests.p14a_fixtures import ACN, CTSH, P14aHarness

DRIVER = "driver:d"
DEBATE = "debate:bookings"
CATALYST = "catalyst-entry:acn:earnings"


def draft_reply(**overrides) -> str:
    reply = {
        "direction": "long", "decision": "THESIS_STRENGTHENED", "confidence": "medium",
        "time_horizon": "6_12_months",
        "our_view": {"statement": "the lag is two to four quarters", "refs": ["T1"]},
        "market_view": {"available": True, "reason": None,
                        "statement": "the street models the two as concurrent",
                        "refs": ["D1"], "sources": ["debate_market_position"]},
        "where_market_is_wrong": {"statement": "the lag, not the direction",
                                  "refs": ["D1"]},
        "convergence_pathway": {"statement": "the print settles it", "refs": ["D1"]},
        # No catalyst rows on this Core (C1's calendar is empty in the
        # fixture), so the pathway is undated -- which the mechanical rubric
        # refuses, so the test that wants a published call supplies a dated
        # one through ``dated_pathway``.
        "event_pathway": [{"signal": "book-to-bill above 1.1 on the call",
                           "catalyst_row_id": None, "refs": ["D1"]}],
        "upside": {"statement": "re-rating to the median", "percent": "55",
                   "refs": ["D1"]},
        "downside": {"statement": "the lag is wrong", "percent": "20", "refs": ["D1"]},
        "falsifiers": [{"statement": "book-to-bill under 1.0 twice",
                        "thesis_row_id": "T1", "falsifier_ref": "falsifier:x"}],
    }
    reply.update(overrides)
    return json.dumps(reply)


PASS = json.dumps({"verdict": "pass", "findings": []})


class FakeModel:
    """Two canned replies, and a record of what was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    def call(self, *, purpose, request_id, prompt, mission):
        index = len(self.prompts)
        self.prompts.append(prompt)
        return {"text": self.replies[index], "replayed": False, "cost_micros": 500,
                "work_order_ref": f"work:cockpit-conviction_call-{index}",
                "invocation_ref": f"inv-{index}", "route_decision_ref": f"route-{index}"}


def fake_family(_config, route_decision_ref):
    return {"route-0": "family-a", "route-1": "family-b"}.get(route_decision_ref)


class ConvictionHarness(P14aHarness):
    """A Core with a passed screen, an admitted thesis and a debate we differ on."""

    grants = ("observation", "stage_record")

    def setUp(self):
        super().setUp()
        self.pass_screen(ACN)

    def admit_thesis(self, company_ref="industry:us-it-services",
                     thesis_ref="thesis:us-it-services:lag"):
        """The long way round: through the authority that admits theses.

        Industry-level, like the live one and like P14a's fixture: a thesis
        with no company subject covers every name in the industry, which is how
        a company with no thesis of its own still holds a view.

        A row inserted by hand would not carry the admission decision the
        table's own CHECK requires, and a fixture that disables a constraint
        stops testing the thing.
        """

        admission = self.state["admission"]
        candidate = admission.propose_thesis_admission(
            candidate_id=f"thesis-admission-candidate:{thesis_ref}",
            thesis_ref=thesis_ref, company_ref=company_ref,
            industry_ref="industry:us-it-services", template_ref="template:x",
            driver_refs=[DRIVER],
            mandate_version_ref=self.state["mandate"]["id"],
            mandate_version_hash=self.state["mandate"]["content_hash"],
            driver_pack_version_ref=self.state["pack"]["id"],
            driver_pack_version_hash=self.state["pack"]["content_hash"],
            content={
                "statement": "Bookings lead revenue by two to four quarters.",
                "mechanism": "The lag is the whole of the disagreement.",
                "confidence": "medium",
                "implied_expectation": "FY27 growth is high single digit.",
                "claim_refs": [], "catalyst_refs": ["catalyst:quarterly-results"],
                "falsifier_refs": ["falsifier:x"], "change_reason": "fixture",
            },
            actor_ref="human:coverage-owner",
            idempotency_key=f"thesis-admission-candidate:{thesis_ref}",
        )
        return admission.decide_thesis_admission(
            candidate_id=candidate["id"], candidate_hash=candidate["content_hash"],
            verdict="admit", rationale="It names its drivers and its falsifiers.",
            decision_id=f"thesis-admission-decision:{thesis_ref}",
            actor_ref="human:portfolio-manager",
            idempotency_key=f"thesis-admission-decision:{thesis_ref}",
        )

    def publish_debate(self, *, lean="bear", side="bull", available=True,
                       subject=ACN):
        from dalton_core.debate_map import DebateMapAuthority

        thesis_refs = [row["ref"] for row
                       in __import__("dalton_core.event_judgement", fromlist=["x"])
                       .company_theses(self.store.connection, subject)]
        claim = self.claim(subject=subject, statement="需求在改善。")
        return DebateMapAuthority(self.store).publish_map(
            subject_ref=subject, subject_kind="company",
            change_reason="evidence_thicker", change_evidence_refs=[claim],
            constitution_ref=self.state["constitution"]["id"],
            constitution_hash=self.state["constitution"]["content_hash"],
            evidence_fingerprint="fingerprint",
            debates=[{
                "debate_ref": DEBATE, "question": "Will bookings hold above ten?",
                "driver_refs": [DRIVER], "admission_index": 0, "causal_link_index": 0,
                "bull_position": {"statement": "bookings accelerating",
                                  "claim_refs": [claim]},
                "bear_position": {"statement": "demand decelerating",
                                  "claim_refs": [claim]},
                "market_position": ({"available": True, "lean": lean,
                                     "statement": "the street models deceleration",
                                     "refs": [claim]} if available else
                                    {"available": False, "lean": None,
                                     "statement": None, "refs": []}),
                "our_position": {"state": "held", "side": side,
                                 "statement": "we model acceleration",
                                 "refs": thesis_refs or [claim]},
                "status": "open", "last_shift_reason": None,
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "source_independence": {"bull_sources": 2, "bear_sources": 2},
            }],
            actor_ref="automation:dalton", created_at="2026-09-09T00:00:00+00:00",
        )

    def eligible_company(self):
        self.admit_thesis()
        self.publish_debate()

    def run_child(self, **overrides):
        params = {"state_dir": self.state_dir, "company_ref": ACN,
                  "summary_dir": self.state_dir, "model_config_path": None,
                  "scheduler_db": None, "dry_run": True}
        params.update(overrides)
        return run_conviction_call(**params)

    def with_model(self):
        config = self.state_dir / "model.json"
        config.write_text(json.dumps({"model_router_db": str(self.state_dir / "r.db")}),
                          encoding="utf-8")
        return config


class WriteScopeTests(ConvictionHarness):
    def test_the_scope_and_the_checkpoint_are_the_words_wave_zero_added(self):
        self.assertEqual(WRITE_SCOPE, "conviction_call")
        self.assertEqual(CHECKPOINT_KIND, "conviction_call")
        self.assertTrue(granted({"autonomy": {"may_write": ["conviction_call"]}}))
        self.assertFalse(granted({"autonomy": {"may_write": ["claim"]}}))
        self.assertTrue(checkpointed(
            {"autonomy": {"human_checkpoints": ["conviction_call"]}}))
        self.assertFalse(checkpointed({"autonomy": {"human_checkpoints": []}}))

    def test_a_mission_without_the_scope_holds_before_spending(self):
        summary = self.run_child()
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["call_status"], "not_authorized")
        self.assertIn("conviction_call", summary["failure_reason"])

    def test_a_mission_nobody_agreed_to_decide_on_also_holds(self):
        # Automation may only propose. A proposal against a mission with no
        # conviction_call checkpoint is a proposal with no decider.
        self.grant(WRITE_SCOPE)
        summary = self.run_child()
        self.assertEqual(summary["call_status"], "no_checkpoint")


class GateRunTests(ConvictionHarness):
    def setUp(self):
        super().setUp()
        self.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))

    def test_a_company_with_no_thesis_is_refused_for_free(self):
        summary = self.run_child()
        self.assertEqual((summary["status"], summary["call_status"]),
                         ("succeeded", "not_eligible"))
        self.assertFalse(summary["eligible"])
        self.assertIn("no_active_thesis", summary["gate_reasons"])

    def test_a_thesis_with_no_market_position_is_still_refused(self):
        self.admit_thesis()
        self.publish_debate(available=False)
        summary = self.run_child()
        self.assertEqual(summary["call_status"], "not_eligible")
        self.assertIn("no_variant_material", summary["gate_reasons"])

    def test_agreeing_with_the_market_is_refused_and_named(self):
        self.admit_thesis()
        self.publish_debate(lean="bull", side="bull")
        summary = self.run_child()
        self.assertEqual(summary["call_status"], "not_eligible")
        self.assertIn("we_agree_with_the_market", summary["gate_reasons"])

    def test_a_real_disagreement_reaches_the_table(self):
        self.eligible_company()
        summary = self.run_child()
        self.assertEqual((summary["status"], summary["call_status"]),
                         ("succeeded", "dry_run"))
        self.assertTrue(summary["eligible"])
        self.assertEqual(summary["divergent_debates"], 1)
        self.assertGreater(summary["prompt_bytes"], 0)
        self.assertTrue(summary["evidence_fingerprint"])

    def test_without_a_model_nothing_is_drafted_because_nothing_can_be(self):
        self.eligible_company()
        summary = self.run_child(dry_run=False)
        self.assertEqual(summary["call_status"], "gated")
        self.assertEqual(summary["failure_reason"], "no model configured")

    def test_the_summary_is_written_beside_the_run(self):
        self.eligible_company()
        self.run_child()
        summary = json.loads(
            (self.state_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["call_status"], "dry_run")
        self.assertEqual(summary["policy_ref"], "conviction-policy:p15d:v1")
        self.assertEqual(summary["rubric_ref"], "rubric:conviction-call")

    def test_there_is_no_consensus_on_a_core_that_has_no_consensus_authority(self):
        # The honest answer, and it must never look like agreement.
        found = consensus_gap(self.store, ACN)
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("P11b", found["reason"])

    def test_reading_a_core_with_no_debate_map_leaves_no_debate_map_behind(self):
        self.assertEqual(open_debates(self.store, ACN), [])
        row = self.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debate_map_versions'"
        ).fetchone()
        self.assertIsNone(row)


class ProposalRunTests(ConvictionHarness):
    def setUp(self):
        super().setUp()
        self.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))
        self.eligible_company()

    def draft(self, replies=(None,), **overrides):
        config = self.with_model()
        model = FakeModel(replies)
        with patch("dalton_core.conviction_call_cli.CockpitModel", model), \
                patch("dalton_core.conviction_call_cli.route_family", fake_family):
            summary = self.run_child(dry_run=False, model_config_path=config, **overrides)
        return summary, model

    def test_a_verified_draft_becomes_a_proposal_and_opens_the_checkpoint(self):
        summary, model = self.draft([draft_reply(), PASS])
        self.assertEqual(summary["call_status"], "fresh")
        self.assertEqual(summary["direction"], "long")
        self.assertEqual(summary["cost_micros"], 1_000)
        authority = ConvictionCallAuthority(self.store)
        open_calls = authority.open_calls()
        self.assertEqual(len(open_calls), 1)
        call = open_calls[0]
        self.assertEqual(call["checkpoint_kind"], "conviction_call")
        self.assertEqual(call["actor_ref"],
                         self.mission["autonomy"]["automation_principal"])
        self.assertEqual(call["drafted_by"]["model_family"], "family-a")
        self.assertEqual(call["verified_by"]["model_family"], "family-b")
        self.assertEqual(call["risk_reward"]["standard"]["status"], "met")
        self.assertEqual(call["consensus_gap"]["status"], "unavailable")
        self.assertEqual(call["debate_refs"], [DEBATE])
        # The drafting prompt showed the disagreement and the standards.
        self.assertIn("DIVERGENT", model.prompts[0])
        self.assertIn("risk-reward:value:6-12m", model.prompts[0])

    def test_the_same_evidence_twice_proposes_once(self):
        self.draft([draft_reply(), PASS])
        summary, _ = self.draft([draft_reply(), PASS])
        self.assertEqual(summary["call_status"], "duplicate")
        self.assertEqual(ConvictionCallAuthority(self.store).counts()["proposals"], 1)

    def test_a_short_with_the_wrong_horizon_is_refused_before_verifying(self):
        summary, model = self.draft([draft_reply(direction="short",
                                                 time_horizon="6_12_months")])
        self.assertEqual(summary["call_status"], "rubric_failed")
        self.assertEqual(summary["rubric_findings"],
                         ["horizon_matches_the_direction"])
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(ConvictionCallAuthority(self.store).counts()["proposals"], 0)

    def test_a_draft_that_leaves_the_table_publishes_nothing(self):
        summary, _ = self.draft([draft_reply(
            our_view={"statement": "x", "refs": ["T9"]})])
        self.assertEqual(summary["call_status"], "refused")
        self.assertEqual(ConvictionCallAuthority(self.store).counts()["proposals"], 0)

    def test_a_verifier_on_the_producers_family_publishes_nothing(self):
        config = self.with_model()
        model = FakeModel([draft_reply(), PASS])
        with patch("dalton_core.conviction_call_cli.CockpitModel", model), \
                patch("dalton_core.conviction_call_cli.route_family",
                      lambda _c, _r: "family-a"):
            summary = self.run_child(dry_run=False, model_config_path=config)
        self.assertEqual(summary["call_status"], "not_independent")
        self.assertEqual(ConvictionCallAuthority(self.store).counts()["proposals"], 0)

    def test_an_unsourced_market_view_is_no_call_rather_than_a_weak_one(self):
        summary, model = self.draft([draft_reply(market_view={
            "available": False, "reason": "no broker note names a target",
            "statement": None, "refs": [], "sources": []})])
        self.assertEqual(summary["call_status"], "no_variant_view")
        self.assertEqual(len(model.prompts), 1)


class FakeLauncher:
    def __init__(self, *, conflict=False, reject=False):
        self.conflict = conflict
        self.reject = reject
        self.started: list[tuple[str, str]] = []
        self.tickets: dict[str, dict] = {}

    def start(self, *, company_ref, fingerprint):
        if self.conflict:
            raise LaneChildConflict("busy")
        if self.reject:
            raise LaneChildRejected("no")
        self.started.append((company_ref, fingerprint))
        ticket_ref = f"conviction-call-run:{len(self.started):024d}"
        self.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "running", "company_ref": company_ref,
            "evidence_fingerprint": fingerprint, "summary": None,
        }
        return self.tickets[ticket_ref]

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, summary, status="succeeded"):
        self.tickets[ticket_ref] = {
            **self.tickets[ticket_ref], "status": status, "summary": summary}


class LaneCoordinatorTests(ConvictionHarness):
    def setUp(self):
        super().setUp()
        self.grant(WRITE_SCOPE, checkpoints=(CHECKPOINT_KIND,))
        self.launcher = FakeLauncher()
        self.coordinator = MissionConvictionLaneCoordinator(
            store=self.store, launcher=self.launcher, mission=lambda: self.mission)

    def test_a_company_that_agrees_with_the_market_is_never_chosen(self):
        self.admit_thesis()
        self.publish_debate(lean="bull", side="bull")
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_the_company_we_differ_from_the_market_on_is_launched(self):
        self.eligible_company()
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        inputs = gate_inputs(self.store, ACN)
        from dalton_core.conviction_call import precheck

        gate = precheck(company_ref=ACN, theses=inputs["theses"],
                        open_debates=inputs["open_debates"],
                        consensus_gap=inputs["consensus_gap"],
                        dossier_variant_view=inputs["dossier_variant_view"])
        self.assertEqual(result["evidence_fingerprint"], fingerprint_of(inputs, gate))

    def test_a_company_that_is_not_covered_yet_is_not_chosen(self):
        # The industry thesis covers CTSH and it has a debate we differ on, but
        # it has not passed its Initial Screen, so it is not in residency.
        self.admit_thesis()
        self.publish_debate(subject=CTSH)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")

    def test_a_call_already_made_on_this_evidence_is_not_made_again(self):
        from tests.test_conviction_call import proposal_kwargs

        self.eligible_company()
        inputs = gate_inputs(self.store, ACN)
        from dalton_core.conviction_call import precheck

        gate = precheck(company_ref=ACN, theses=inputs["theses"],
                        open_debates=inputs["open_debates"],
                        consensus_gap=inputs["consensus_gap"],
                        dossier_variant_view=inputs["dossier_variant_view"])
        ConvictionCallAuthority(self.store).propose(**proposal_kwargs(
            company_ref=ACN, mission=self.mission,
            evidence_fingerprint=fingerprint_of(inputs, gate)))
        # Idempotent per (company, evidence fingerprint): the same material
        # produced a call once, so it does not produce a second opinion.
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_a_refused_run_holds_that_evidence_back_but_not_forever(self):
        self.eligible_company()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"],
                             {"call_status": "refused", "failure_reason": "bad draft"})
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "bad draft")
        # New material changes the fingerprint, so the hold does not survive it.
        self.admit_thesis(thesis_ref="thesis:us-it-services:second")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_transient_failure_is_not_held_against_the_evidence(self):
        self.eligible_company()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], {"call_status": "model_unavailable"})
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_weeks_call_stops_the_next_one_even_across_a_restart(self):
        from tests.test_conviction_call import proposal_kwargs

        self.eligible_company()
        authority = ConvictionCallAuthority(self.store)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        authority.propose(**proposal_kwargs(
            company_ref=ACN, created_at=now, mission=self.mission,
            evidence_fingerprint="an-older-fingerprint"))
        # A brand new coordinator: nothing in memory, and the cap still holds.
        fresh = MissionConvictionLaneCoordinator(
            store=self.store, launcher=FakeLauncher(), mission=lambda: self.mission)
        result = fresh.dispatch_once()
        self.assertEqual(result["status"], "held")
        self.assertIn(week_key(now), result["reason"])

    def test_a_busy_launcher_is_reported_not_raised(self):
        self.eligible_company()
        self.coordinator.launcher = FakeLauncher(conflict=True)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.coordinator.launcher = FakeLauncher(reject=True)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "rejected")

    def test_no_mission_is_unconfigured(self):
        coordinator = MissionConvictionLaneCoordinator(
            store=self.store, launcher=self.launcher, mission=lambda: None)
        self.assertEqual(coordinator.dispatch_once()["status"], "unconfigured")


class LaneRegistrationTests(unittest.TestCase):
    def test_the_lane_registers_itself_once_with_its_own_order(self):
        spec = lane_for_operation("dispatch_conviction_call")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.order, 141)
        self.assertEqual(spec.driver_key, "conviction_call")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertIn(spec, tick_lanes())
        self.assertIn(LAUNCHER_KWARG, lane_init_kwargs())
        orders = [item.order for item in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_lane_and_its_model_call_drink_from_the_same_pool(self):
        from dalton_core.budget_pools import pool_for_operation, pool_for_purpose

        self.assertEqual(pool_for_operation("dispatch_conviction_call"), "coverage")
        self.assertEqual(pool_for_purpose("conviction_call"), "coverage")

    def test_the_lane_is_absent_when_no_model_configuration_is_installed(self):
        class Args:
            conviction_call_model_config = None
        self.assertIsNone(build_launcher(Args()))

        class Context:
            state = Path("/nonexistent")
        self.assertEqual(argv_fragment(Context()), [])

    def test_the_launcher_names_a_run_by_its_company_and_its_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = ConvictionCallLauncher(state_dir=directory)
            self.addCleanup(launcher.close)
            self.assertFalse(launcher.configured)
            with self.assertRaises(LaneChildRejected):
                launcher.start(company_ref="", fingerprint="f")
            with self.assertRaises(LaneChildRejected):
                launcher.start(company_ref=ACN, fingerprint="")
        self.assertEqual(run_digest(ACN, "f"), run_digest(ACN, "f"))
        self.assertNotEqual(run_digest(ACN, "f"), run_digest(ACN, "g"))

    def test_the_child_takes_the_arguments_the_launcher_passes(self):
        parser = build_parser()
        args = parser.parse_args(["--state-dir", "/s", "--company-ref", ACN,
                                  "--summary-dir", "/s", "--quiet"])
        self.assertEqual(args.company_ref, ACN)
        self.assertTrue(args.quiet)


class WriterOperationTests(unittest.TestCase):
    def test_the_decision_is_human_governance_only(self):
        from dalton_core import writer_server

        self.assertIn("decide_conviction_call",
                      writer_server.HUMAN_GOVERNANCE_OPERATIONS)
        # Automation principals reach the mission operations and the lane
        # ticks; the decision is in neither set.
        self.assertNotIn("decide_conviction_call",
                         writer_server.MISSION_AUTOMATION_OPERATIONS)
        from dalton_core.lane_registry import lane_operations

        self.assertNotIn("decide_conviction_call", lane_operations())
        self.assertEqual(
            writer_server.OPERATION_FIELDS["decide_conviction_call"],
            frozenset({"proposal_ref", "proposal_hash", "decision", "reason",
                       "actor_ref"}))

    def test_the_actor_is_bound_by_the_server_not_supplied_by_the_caller(self):
        from dalton_core import writer_server

        self.assertEqual(
            writer_server.OPERATION_ACTOR_FIELDS["decide_conviction_call"],
            "actor_ref")


if __name__ == "__main__":
    unittest.main()
