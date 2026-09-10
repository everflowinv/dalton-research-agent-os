"""W4: the tick that decides whether to pay for a review, or to check for free.

Chem's zero-base cron fired zero times, so the failure mode this guards is not
"the review is wrong" but "the review never happens" -- and its mirror, "the
review happens every five minutes".  Both are decisions this coordinator makes
with no Core and no model in the test: the state reader is injected, so the
tick's arithmetic is tested rather than the Ledger's.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from dalton_core.mission_zero_base_lane import (
    MissionZeroBaseLaneCoordinator,
    argv_fragment,
    build_launcher,
    may_write_review,
)
from dalton_core.lane_failure_class import Classification, CONTENT_REFUSED

from tests.zero_base_fixtures import MISSION

NOW = datetime(2026, 3, 12, 9, tzinfo=timezone.utc)
DUE = [{"company_ref": "company:ACN", "trigger": "monthly", "period_label": "2026-03",
        "review_ref": "zero-base-review:acn", "due": True, "reason": "x"}]


class FakeLauncher:
    def __init__(self, tickets=None) -> None:
        self.started: list[dict] = []
        self.tickets = tickets or {}
        self.counter = 0

    def start(self, *, mode, batch_ref, company_refs=()):
        self.counter += 1
        ticket = {"id": f"zero-base-review-run:{self.counter:024x}", "mode": mode,
                  "batch_ref": batch_ref, "company_refs": list(company_refs)}
        self.started.append(ticket)
        self.tickets.setdefault(ticket["id"], {"status": "running", **ticket})
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]


def coordinator(launcher, *, due=(), digest="digest-1", mission=MISSION):
    return MissionZeroBaseLaneCoordinator(
        launcher=launcher,
        mission=lambda: mission,
        lane_state=lambda _mission, _now: {"due": list(due), "checks_digest": digest},
        clock=lambda: NOW,
    )


class DispatchTests(unittest.TestCase):
    def test_a_due_company_launches_a_paid_review(self) -> None:
        launcher = FakeLauncher()
        result = coordinator(launcher, due=DUE).dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["mode"], "review")
        self.assertEqual(launcher.started[0]["company_refs"], ["company:ACN"])
        self.assertEqual(len(launcher.started[0]["batch_ref"]), 64)

    def test_nothing_due_but_a_moved_ledger_launches_the_free_pass(self) -> None:
        launcher = FakeLauncher()
        result = coordinator(launcher, due=(), digest="digest-1").dispatch_once()
        self.assertEqual((result["status"], result["mode"]), ("launched", "checks"))
        self.assertEqual(len(launcher.started[0]["batch_ref"]), 64)

    def test_a_settled_check_pass_makes_the_lane_idle_until_it_moves(self) -> None:
        launcher = FakeLauncher()
        lane = coordinator(launcher, due=(), digest="digest-1")
        ticket = lane.dispatch_once()
        launcher.tickets[ticket["ticket_ref"]] = {
            "status": "succeeded", "mode": "checks", "batch_ref": launcher.started[-1]["batch_ref"],
            "summary": {"review_status": "checks_only",
                        "checks": {"checked": 3, "fresh": 1, "digest": "digest-1"}},
        }
        self.assertEqual(lane.dispatch_once()["status"], "idle")
        # ...and moves again the moment a judgement's window settles.
        lane.lane_state = lambda _m, _n: {"due": [], "checks_digest": "digest-2"}
        self.assertEqual(lane.dispatch_once()["status"], "launched")

    def test_a_crashed_child_does_not_move_the_watermark(self) -> None:
        # The bug this guards: a child that wrote half the pass and then died
        # would otherwise make the lane believe the ledger was fresh, and the
        # other half would never be written.
        launcher = FakeLauncher()
        lane = coordinator(launcher, due=(), digest="digest-1")
        ticket = lane.dispatch_once()
        launcher.tickets[ticket["ticket_ref"]] = {
            "status": "failed", "mode": "checks", "batch_ref": launcher.started[-1]["batch_ref"],
            "summary": {"failure_reason": "boom",
                        "checks": {"checked": 3, "fresh": 1, "digest": "digest-1"}},
        }
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("boom", held["reason"])

    def test_a_failed_batch_is_retried_when_its_inputs_move(self) -> None:
        launcher = FakeLauncher()
        lane = coordinator(launcher, due=(), digest="digest-1")
        ticket = lane.dispatch_once()
        launcher.tickets[ticket["ticket_ref"]] = {
            "status": "failed", "mode": "checks", "batch_ref": launcher.started[-1]["batch_ref"],
            "summary": {"failure_reason": "boom"},
        }
        self.assertEqual(lane.dispatch_once()["status"], "held")
        lane.lane_state = lambda _m, _n: {"due": [], "checks_digest": "digest-2"}
        self.assertEqual(lane.dispatch_once()["status"], "launched")

    def test_a_provider_contract_change_retires_an_old_review_refusal(self) -> None:
        launcher = FakeLauncher()
        lane = coordinator(launcher, due=DUE, digest="")
        with patch("dalton_core.mission_zero_base_lane.verifier_provider_contract_fingerprint",
                   return_value="a" * 64):
            old_key = __import__("dalton_core.mission_zero_base_lane", fromlist=["review_item_key"]).review_item_key(DUE[0])
            lane.budget.record(old_key, classification=Classification(
                CONTENT_REFUSED, "verifier refused the content", "fixture"))
            self.assertEqual(lane.dispatch_once()["status"], "terminal")
        with patch("dalton_core.mission_zero_base_lane.verifier_provider_contract_fingerprint",
                   return_value="b" * 64):
            self.assertEqual(lane.dispatch_once()["status"], "launched")

    def test_dependency_park_replays_and_same_batch_gets_a_probe(self) -> None:
        with TemporaryDirectory() as directory:
            launcher = FakeLauncher()
            def build():
                return MissionZeroBaseLaneCoordinator(
                    launcher=launcher, mission=lambda: MISSION,
                    lane_state=lambda _m, _n: {
                        "due": [], "checks_digest": "digest-1"},
                    clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
                    failure_ledger_dir=Path(directory),
                )
            lane = build()
            first = lane.dispatch_once()
            launcher.tickets[first["ticket_ref"]] = {
                "status": "failed", "mode": "checks", "batch_ref": "digest-1",
                "summary": {"failure_reason": "model_unavailable"},
            }
            probe = lane.dispatch_once()
            self.assertEqual(probe["status"], "launched")
            launcher.tickets[probe["ticket_ref"]] = {
                "status": "failed", "mode": "checks", "batch_ref": "digest-1",
                "summary": {"failure_reason": "model_unavailable"},
            }
            restarted = build()
            self.assertEqual(len(restarted.budget.parked_items()), 1)
            self.assertEqual(restarted.dispatch_once()["status"], "launched")

    def test_a_running_child_keeps_the_slot(self) -> None:
        launcher = FakeLauncher()
        lane = coordinator(launcher, due=DUE)
        lane.dispatch_once()
        self.assertEqual(lane.dispatch_once()["status"], "running")
        self.assertEqual(len(launcher.started), 1)

    def test_a_mission_without_the_deliverable_grant_pays_for_nothing(self) -> None:
        launcher = FakeLauncher()
        mission = {**MISSION, "autonomy": {**MISSION["autonomy"], "may_write": ["claim"]}}
        result = coordinator(launcher, due=DUE, mission=mission).dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertEqual(launcher.started, [])

    def test_no_mission_is_unconfigured_not_a_crash(self) -> None:
        launcher = FakeLauncher()
        result = coordinator(launcher, due=DUE, mission=None).dispatch_once()
        self.assertEqual(result["status"], "unconfigured")

    def test_a_state_read_that_throws_is_this_lane_s_problem_only(self) -> None:
        launcher = FakeLauncher()
        lane = MissionZeroBaseLaneCoordinator(
            launcher=launcher, mission=lambda: MISSION,
            lane_state=lambda _m, _n: (_ for _ in ()).throw(RuntimeError("no table")),
            clock=lambda: NOW,
        )
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("no table", result["reason"])

    def test_the_grant_is_read_off_the_mission_and_nothing_else(self) -> None:
        self.assertTrue(may_write_review(MISSION))
        self.assertFalse(may_write_review(None))
        self.assertFalse(may_write_review({"autonomy": {}}))


class InstallationTests(unittest.TestCase):
    class Args:
        db = "/tmp/does-not-matter/core.sqlite"
        zero_base_review_model_config = None
        zero_base_review_verifier_model_config = None
        zero_base_review_policy = None
        scheduler = None

    def test_a_writer_with_no_model_configuration_has_no_lane(self) -> None:
        self.assertIsNone(build_launcher(self.Args()))

    def test_the_plist_fragment_is_gated_on_the_configuration(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)

            class Context:
                pass

            context = Context()
            context.state = state
            self.assertEqual(argv_fragment(context), [])
            (state / "zero-base-review-model-config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(argv_fragment(context), [])
            (state / "zero-base-review-verifier-model-config.json").write_text(
                "{}", encoding="utf-8")
            fragment = argv_fragment(context)
            self.assertEqual(fragment[0], "--zero-base-review-model-config")
            (state / "tracking-policy.json").write_text("{}", encoding="utf-8")
            self.assertIn("--zero-base-review-policy", argv_fragment(context))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
