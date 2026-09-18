"""WP-C2 on the Deep Insight Gate lane: a content refusal holds the company.

Same defect as P12a's, in the same shape.  A ``content_refused`` verdict is
recorded against the run's **signature**, and this lane's signature digests the
company's dossier head, its debate map, the numbers and the valuation rows --
all of which move while an owner is thinking, and all of which are moved every
few minutes by the lanes upstream.  So the hold binds for a handful of ticks
and then a new key admits the same four paid calls again.

Live tick ledger (``tick-ledger.sqlite``, 2,147 ``dispatch_deep_insight_gate``
ticks between 2026-09-10 and 09-18):

* Accenture ``company:sec-cik:0001467373`` -- **103 launches under 49 distinct
  signatures**;
* DXC ``company:sec-cik:001688568`` -- **51 launches under 21 signatures**, the
  return path holding on ``constitution_refused`` 23 times;
* Cognizant ``0001058290`` -- 51 launches under 26 signatures.

Under a six-hour company-keyed cooldown those would have been 15, 15 and 11.
"""

from __future__ import annotations

import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from dalton_core import mission_deep_insight_lane as lane_module
from tests import test_deep_insight_gate_lane as _gate

ACN = _gate.ACN


class Launcher:
    """One ticket at a time, answering with whatever the test refused with."""

    state_dir = None

    def __init__(self, gate_status, *, ticket_status="succeeded"):
        self.gate_status = gate_status
        self.ticket_status = ticket_status
        self.started: list[tuple[str, str | None]] = []

    def start(self, *, signature, company_ref=None, source_fingerprint=None):
        self.started.append((signature, company_ref))
        return {"id": f"ticket-{len(self.started)}", "signature": signature,
                "company_ref": company_ref}

    def status(self, ticket_ref):
        signature, company_ref = self.started[-1]
        return {
            "id": ticket_ref, "status": self.ticket_status,
            "signature": signature, "company_ref": company_ref,
            "summary": {"company_ref": company_ref,
                        "gate_status": self.gate_status,
                        "failure_reason": "the twelve answers were refused"},
        }


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class GateCooldownTests(unittest.TestCase):
    def setUp(self):
        self.harness = _gate.Harness()
        self.addCleanup(self.harness.close)
        self.connection = self.harness.store.connection
        self.clock = Clock()
        self.moves = 0

    def coordinator(self, launcher):
        return lane_module.MissionDeepInsightLaneCoordinator(
            connection=self.connection, launcher=launcher,
            companies=lambda: [ACN], failure_clock=self.clock)

    def new_evidence(self):
        """What the lanes upstream do: the company's file moves again."""

        self.moves += 1
        fresh = self.harness.fixture.add_claim(
            f"cooldown-evidence-{self.moves}", kind="qualitative", value=None,
            unit=None, statement=f"又一条新材料 {self.moves}。",
        )["claim_version_id"]
        self.harness.publish_dossier(extra={"guidance_style": fresh})

    def refuse_once(self, launcher):
        coordinator = self.coordinator(launcher)
        launched = coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        settled = coordinator.dispatch_once()["settled"]
        return coordinator, settled

    def test_a_content_refusal_holds_the_company_for_the_cooldown(self):
        coordinator, settled = self.refuse_once(Launcher("verification_failed"))
        self.assertEqual(settled["gate_status"], "verification_failed")
        held = coordinator.company_cooldown(ACN)
        self.assertIsNotNone(held)
        self.assertIn("verification_failed", held["reason"])
        self.assertEqual(held["seconds"],
                         lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS)
        self.assertEqual(settled["cooldown"]["until"], held["until"])

    def test_the_constitution_refusal_the_return_path_held_on_starts_it(self):
        # DXC's 23 holds.  ``constitution_refused`` is not in this lane's
        # terminal list -- it takes the quiet path -- so before this it was the
        # signature alone that kept the lane silent, and the signature moves.
        self.assertIn("constitution_refused", lane_module.COOLDOWN_STATUSES)
        self.assertNotIn("constitution_refused",
                         lane_module.CONTENT_TERMINAL_STATUSES)
        coordinator, _ = self.refuse_once(Launcher("constitution_refused"))
        self.assertIsNotNone(coordinator.company_cooldown(ACN))

    def test_new_evidence_during_the_cooldown_does_not_restart_or_lift_it(self):
        launcher = Launcher("verification_failed")
        coordinator, _ = self.refuse_once(launcher)
        until = coordinator.company_cooldown(ACN)["until"]
        launched = len(launcher.started)
        for _ in range(3):
            self.clock.now += timedelta(minutes=5)
            self.new_evidence()
            result = coordinator.dispatch_once()
            self.assertEqual(result["status"], "held")
            self.assertIn(ACN, result["cooling_down"])
            self.assertIn(ACN, result["held"])
            self.assertEqual(coordinator.company_cooldown(ACN)["until"], until)
        # Three ticks, three different evidence signatures, no child.
        self.assertEqual(len(launcher.started), launched)

    def test_the_cooldown_expires_on_its_own_and_the_lane_tries_again(self):
        launcher = Launcher("auto_returned")
        coordinator, _ = self.refuse_once(launcher)
        launched = len(launcher.started)
        self.clock.now += timedelta(
            seconds=lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS + 1)
        self.new_evidence()
        self.assertIsNone(coordinator.company_cooldown(ACN))
        self.assertEqual(coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(launcher.started), launched + 1)

    def test_a_reviewed_contract_change_lifts_it_at_once(self):
        # A cooldown is not a suspension: the refusal was a statement made
        # under the drafting contract of the day, and a deploy that moves it
        # makes the statement void.
        coordinator, _ = self.refuse_once(Launcher("rubric_refused"))
        self.assertIsNotNone(coordinator.company_cooldown(ACN))
        with unittest.mock.patch.object(
                lane_module.MissionDeepInsightLaneCoordinator,
                "control_fingerprint", lambda self: "moved"):
            self.assertIsNone(coordinator.company_cooldown(ACN))

    def test_the_drafting_contract_is_part_of_the_control_fingerprint(self):
        coordinator = self.coordinator(Launcher("rubric_refused"))
        before = coordinator.control_fingerprint()
        with unittest.mock.patch(
                "dalton_core.deep_insight_gate_cli.GATE_DRAFTING_CONTRACT",
                "deep-insight-gate-drafting:moved"):
            self.assertNotEqual(coordinator.control_fingerprint(), before)

    def test_a_submitted_run_ends_it(self):
        launcher = Launcher("verification_failed")
        coordinator, _ = self.refuse_once(launcher)
        self.assertIsNotNone(coordinator.company_cooldown(ACN))
        launcher.gate_status = "submitted"
        coordinator._open = "ticket-1"
        coordinator._settle_open()
        self.assertIsNone(coordinator.company_cooldown(ACN))

    def test_a_second_refusal_inside_a_cooldown_does_not_extend_it(self):
        launcher = Launcher("verification_failed")
        coordinator, _ = self.refuse_once(launcher)
        until = coordinator.company_cooldown(ACN)["until"]
        self.clock.now += timedelta(hours=1)
        coordinator._open = "ticket-1"
        coordinator._settle_open()
        self.assertEqual(coordinator.company_cooldown(ACN)["until"], until)

    def test_the_two_lanes_agree_on_the_number(self):
        from dalton_core.mission_dossier_lane import (
            CONTENT_REFUSAL_COOLDOWN_SECONDS as dossier_seconds,
        )

        self.assertEqual(lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS,
                         dossier_seconds)
        self.assertEqual(lane_module.CONTENT_REFUSAL_COOLDOWN_SECONDS, 6 * 60 * 60)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
