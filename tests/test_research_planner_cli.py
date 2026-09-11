"""P13n: the child that decides, and what it does when it cannot."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.company_dossier_launcher import run_digest
from dalton_core.research_planner_cli import MAX_COST_USD, build_state, run_planner
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params


class PlannerChildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.summary_dir = self.root / "out"
        self.store = DaltonStore(str(self.state / "core.sqlite"))
        self.missions = CoverageMissionAuthority(self.store)

    def close(self):
        self.store.close()

    def publish_mission(self):
        fixtures = bootstrap_method_authorities(self.store)
        params = mission_params(fixtures)
        ref = params.pop("mission_ref")
        mission = self.missions.create_mission(ref, **params)
        self.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer").fetchone()
        return mission

    def plan(self, **overrides):
        kwargs = {
            "state_dir": self.state, "model_config_path": None,
            "summary_dir": self.summary_dir, "scheduler_db": None,
            "plans_dir": self.state / "discovery-plans", "dry_run": True,
        }
        kwargs.update(overrides)
        self.close()
        try:
            return run_planner(**kwargs)
        finally:
            self.store = DaltonStore(str(self.state / "core.sqlite"))
            self.missions = CoverageMissionAuthority(self.store)

    def test_a_core_with_no_mission_is_idle_not_an_error(self):
        summary = self.plan()
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["plan_status"], "no_mission")

    def test_a_dry_run_assembles_the_state_and_spends_nothing(self):
        self.publish_mission()
        summary = self.plan()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["plan_status"], "gated")
        self.assertIsNotNone(summary["state_hash"])
        self.assertEqual(summary["cost_micros"], 0)
        self.assertGreater(summary["prompt_bytes"], 0)

    def test_state_includes_exact_completed_dossier_feedback(self):
        mission = self.publish_mission()
        company_ref = mission["universe"][0]["company_ref"]
        signature = "ledger:exact"
        suffix = run_digest(company_ref, signature)
        directory = self.state / "company-dossier-runs" / suffix
        directory.mkdir(mode=0o700, parents=True)
        directory.parent.chmod(0o700)
        for name, value in {
            "ticket.json": {
                "id": f"company-dossier-run:{suffix}",
                "company_ref": company_ref,
                "signature": signature,
                "run_digest": suffix,
                "status": "succeeded",
                "exit_code": 0,
                "completed_at": "2026-09-11T12:00:00+00:00",
            },
            "summary.json": {
                "status": "succeeded", "company_ref": company_ref,
                "dossier_status": "insufficient_evidence",
                "repair_targets": [{
                    "unit": "kpi_dictionary", "code": "missing_evidence",
                    "detail": "missing exact numerator and denominator",
                }],
            },
        }.items():
            path = directory / name
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
        state = build_state(
            self.store, self.missions, mission,
            plans_dir=self.state / "discovery-plans",
            as_of="2026-09-11T12:01:00+00:00",
        )
        company = next(item for item in state["companies"]
                       if item["company_ref"] == company_ref)
        feedback = company["dossier_feedback"]
        self.assertEqual(feedback["source_ticket_ref"],
                         f"company-dossier-run:{suffix}")
        self.assertEqual(feedback["repair_targets"][0]["unit"],
                         "kpi_dictionary")
        self.assertEqual(len(feedback["feedback_hash"]), 64)

    def test_no_model_configured_is_gated_and_says_so(self):
        self.publish_mission()
        summary = self.plan(dry_run=False, model_config_path=None)
        self.assertEqual(summary["plan_status"], "gated")
        self.assertIn("no planner model", summary["failure_reason"])

    def test_an_unchanged_state_replays_the_plan_instead_of_paying(self):
        # The whole reason the state hashes: an expensive model on a
        # five-minute tick is only affordable if an unchanged world is free.
        mission = self.publish_mission()
        first = self.plan()
        from dalton_core.store import content_hash
        plan = {"mission_version_ref": mission["id"], "state_hash": first["state_hash"],
                "assessment": "steady", "directives": [], "inquiries": []}
        self.missions.record_research_plan(
            {**plan, "content_hash": content_hash(plan)},
            decided_by=mission["autonomy"]["automation_principal"])
        # The model config is never read: an unchanged state short-circuits
        # before anything is opened, which is the property under test.
        config = self.root / "never-read.json"
        config.write_text("{}", encoding="utf-8")
        again = self.plan(dry_run=False, model_config_path=config)
        self.assertEqual(again["plan_status"], "unchanged")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["cost_micros"], 0)

    def test_the_summary_is_written_owner_only_even_on_a_dry_run(self):
        self.publish_mission()
        self.plan()
        path = self.summary_dir / "summary.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["formal_authority_writes"], 0)

    def test_it_never_claims_authority_writes(self):
        self.publish_mission()
        self.assertEqual(self.plan()["formal_authority_writes"], 0)

    def test_the_reservation_covers_the_call_the_work_order_permits(self):
        # Two ways to get this wrong, and this lane has hit both. Too low and
        # the router refuses before the call is made: it estimates at the
        # *permitted* output, which is $0.33 on this model, so anything under
        # that buys nothing but refusals. Too high and the reservation is money
        # the rest of the day cannot spend -- P13n set it to $0.40 because
        # $1.50 pushed the day past the mission's $5 cap.
        #
        # P13am: that cap is $100 now, so the ceiling here is about staying a
        # small fraction of a day rather than about fitting inside one.
        self.assertGreater(MAX_COST_USD, 0.33)
        self.assertLess(MAX_COST_USD, 5.0)

    def test_a_lease_another_attempt_holds_is_busy_not_a_crash(self):
        # The work order is keyed by the state hash, so a hand-run beside the
        # tick's child shares an id. Crashing loses the summary the parent
        # reads, which is how the failure first appeared: "unexpected
        # LeaseRejected" with no plan_status at all.
        from unittest.mock import patch

        from dalton_core.scheduler import LeaseRejected

        self.publish_mission()
        config = self.root / "model.json"
        config.write_text("{}", encoding="utf-8")
        with patch("dalton_core.research_planner_cli.CockpitModel") as model:
            model.return_value.call.side_effect = LeaseRejected(
                "attempt is not the current leased attempt")
            summary = self.plan(dry_run=False, model_config_path=config)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["plan_status"], "busy")
        self.assertIn("LeaseRejected", summary["failure_reason"])

    def test_the_lease_outlasts_the_call_it_covers(self):
        # The scheduler's default lease is 30s and its ceiling 60; a planner
        # call is allowed 300. When the lease lapsed mid-call the completion
        # was refused as "attempt is not the current leased attempt" -- the
        # work was done and paid for, and the answer thrown away.
        from dalton_core.cockpit_model import _LEASE_GRACE_SECONDS
        from dalton_core.research_planner_cli import TIMEOUT_SECONDS

        self.assertGreater(_LEASE_GRACE_SECONDS, 0)
        self.assertGreater(TIMEOUT_SECONDS + _LEASE_GRACE_SECONDS, TIMEOUT_SECONDS)



if __name__ == "__main__":
    unittest.main()
