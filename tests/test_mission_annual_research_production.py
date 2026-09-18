"""Production subprocess coverage for admitted mission annual research."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.store import canonical_json, content_hash
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.mission_annual_research_launcher import (
    MissionAnnualResearchLauncher,
)
from dalton_core.mission_annual_research_lane import (
    MissionAnnualResearchCoordinator,
)
from dalton_core.lane_child_launcher import LaneChildRejected
from tests.test_mission_annual_research import MissionAnnualFixture
from tests.test_openclaw_model_adapter import FakeBroker, seal, success_response


class MissionAnnualResearchProductionTests(unittest.TestCase):
    def _install_manifest_pointer(self, fixture: MissionAnnualFixture) -> Path:
        ticket_dir = fixture.state / "fetches" / fixture.source.ticket_ref.split(":", 1)[1]
        ticket_dir.mkdir(parents=True)
        document_ref = f"sec:filing:{fixture.source.accession}"
        for name, value in (
            ("ticket.json", {
                "id": fixture.source.ticket_ref,
                "status": "succeeded",
                "document_ref": document_ref,
            }),
            ("summary.json", {
                "url_ref": document_ref,
                "canonical_url": fixture.source.url,
                "manifest_ref": fixture.source.manifest["id"],
                "manifest_hash": fixture.source.manifest["content_hash"],
                "status": "succeeded",
            }),
            ("manifest.json", fixture.source.manifest),
        ):
            path = ticket_dir / name
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        governance = fixture.state / "web-fetch-governance.json"
        governance.write_text("{}\n", encoding="utf-8")
        os.chmod(governance, 0o600)
        return governance

    def _production_configs(self, fixture: MissionAnnualFixture, broker: FakeBroker) -> None:
        key = fixture.state / "broker.key"
        key.write_text("a" * 64, encoding="utf-8")
        os.chmod(key, 0o600)
        for path in (
            fixture.state / "registered-annual-report-draft-model-config.json",
            fixture.state / "registered-annual-report-verifier-model-config.json",
        ):
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update({
                "broker_socket": str(broker.path.resolve()),
                "broker_auth_key": str(key.resolve()),
                "broker_client_id": "client:dalton-core",
                "expected_agent_id": "dalton-model-broker",
                "call_budget": {
                    "max_input_tokens": 32_000,
                    "max_output_tokens": 4_000,
                    "max_cost_usd": 1.0,
                    "timeout_seconds": 30,
                },
                "run_budget": {"max_units": 1, "max_seconds": 300},
                "transport_retry": {
                    "max_definitely_not_sent_retries": 0,
                    "queue_wait_seconds": 0,
                    "retry_backoff_seconds": 0,
                },
            })
            path.write_text(canonical_json(value) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)

    def test_real_unix_broker_budget_and_replay_are_closed_by_admission(self) -> None:
        fixture = MissionAnnualFixture(self)
        fixture.harness.clock.value = datetime.now(timezone.utc)
        governance = self._install_manifest_pointer(fixture)
        statement = (
            "The company serves varied customers and depends on outsourcing partners."
        )
        replies = iter((
            (fixture.draft_profile, canonical_json({
                "schema_version": "0.1",
                "answer": statement,
                "candidate": {
                    "normalized_statement": statement,
                    "metric_or_aspect": "customer and operating dependencies",
                    "period": "FY2025 annual report",
                    "basis": "reported",
                    "cited_match_indexes": [0],
                },
            })),
            (fixture.verifier_profile, canonical_json({
                "schema_version": "0.1",
                "verdict": "pass",
                "verified_statement": statement,
                "findings": [],
            })),
        ))

        def respond(request):
            semantic = dict(request)
            semantic.pop("queueWaitMs", None)
            profile, text = next(replies)
            response = success_response(semantic, text=text)
            response.pop("contentHash")
            response["provider"] = profile["provider"]
            response["model"] = profile["model"]
            response["canonicalModel"] = f"{profile['provider']}/{profile['model']}"
            return seal(response)

        broker = FakeBroker(fixture.state, respond, connections=2)
        self.addCleanup(broker.close)
        self._production_configs(fixture, broker)
        admission = fixture.authority.admit(**fixture.args())
        staging_path = fixture.state / "mission-annual-staging.sqlite"
        CandidateStagingStore(staging_path).close()
        launcher = MissionAnnualResearchLauncher(
            state_dir=fixture.state,
            staging_path=staging_path,
            web_fetch_governance_path=governance,
            spool_dir=fixture.source.root / "spool",
            python_executable=sys.executable,
        )
        self.addCleanup(launcher.close)
        python_path = os.pathsep.join((
            str(Path(__file__).parents[1] / "src"),
            str(Path(__file__).parents[1]),
        ))
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            ticket = launcher.start(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
            )
            self.assertEqual(launcher.wait(timeout=60), 0)
            finished = launcher.status(ticket["id"])
        summary = dict(finished["summary"])
        asserted = summary.pop("content_hash")
        self.assertEqual(asserted, content_hash(summary))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["admission_ref"], admission["id"])
        self.assertEqual(len(broker.requests), 2)

        with sqlite3.connect(fixture.state / "budget.sqlite") as budget:
            rows = budget.execute(
                "SELECT a.work_order_ref,a.attempt_number,s.actual_micros "
                "FROM thesis_impact_day_admissions a JOIN "
                "thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
                "WHERE a.work_order_ref LIKE 'work:mission-annual-research-%' "
                "ORDER BY a.created_at"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[2] for row in rows], [10_000, 10_000])
        staged = CandidateStagingStore(
            fixture.state / "mission-annual-staging.sqlite"
        )
        try:
            self.assertEqual(staged.counts()["candidate_stage_requests"], 1)
        finally:
            staged.close()

        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            with self.assertRaisesRegex(
                LaneChildRejected, "requires controlled re-entry"
            ):
                launcher.start(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                )
            replay_ticket = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=ticket["id"],
                authorization="test:exact-scheduler-replay",
            )
            self.assertEqual(launcher.wait(timeout=60), 0)
            replay_finished = launcher.status(replay_ticket["id"])
        self.assertEqual(len(broker.requests), 2)
        replay_summary = replay_finished["summary"]
        self.assertEqual(replay_summary["status"], "complete")
        self.assertEqual(len(replay_summary["outcomes"]), 1)
        marker = launcher._ticket_path(ticket["id"]).with_name(
            "controlled-reentry-"
            + hashlib.sha256(b"test:exact-scheduler-replay").hexdigest()[:24]
            + ".json"
        )
        self.assertTrue(marker.is_file())

        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            wrong = launcher.start(
                admission_ref=admission["id"],
                admission_hash="0" * 64,
            )
            self.assertEqual(launcher.wait(timeout=60), 1)
            refused = launcher.status(wrong["id"])
        self.assertEqual(refused["summary"]["status"], "failed")
        self.assertIn("hash drifted", refused["summary"]["error"])
        self.assertEqual(len(broker.requests), 2)

        summary_path = launcher._ticket_path(wrong["id"]).with_name("summary.json")
        tampered = dict(refused["summary"])
        tampered["status"] = "complete"
        summary_path.write_text(canonical_json(tampered) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LaneChildRejected, "summary authority drifted"):
            launcher.status(wrong["id"])

    def test_changed_ticket_identity_rebinds_when_the_admission_did_not_change(self):
        """A release renamed the ticket; the admission is byte-identical."""

        from dalton_core.lane_child_launcher import write_owner_only

        fixture = MissionAnnualFixture(self)
        fixture.harness.clock.value = datetime.now(timezone.utc)
        governance = self._install_manifest_pointer(fixture)
        admission = fixture.authority.admit(**fixture.args())
        staging_path = fixture.state / "mission-annual-rebind-staging.sqlite"
        CandidateStagingStore(staging_path).close()
        launcher = MissionAnnualResearchLauncher(
            state_dir=fixture.state,
            staging_path=staging_path,
            web_fetch_governance_path=governance,
            spool_dir=fixture.source.root / "spool",
            python_executable=sys.executable,
        )
        self.addCleanup(launcher.close)
        configuration = launcher.configuration()
        signature = {
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "configuration": configuration,
        }
        prior_ref = "mission-annual-research:" + content_hash(signature)[:24]
        ticket_path = launcher._ticket_path(prior_ref)
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        now = fixture.harness.clock.value.isoformat()
        write_owner_only(ticket_path, {
            "schema_version": "0.1", "id": prior_ref, **signature,
            "configuration_hash": content_hash(configuration),
            "started_at": now, "pid": 1, "command": ["true"],
            "status": "failed", "exit_code": 1, "completed_at": now,
        })
        summary = {
            "schema_version": "0.1", "created_at": now,
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "status": "incomplete", "outcomes": [], "error": None,
        }
        write_owner_only(ticket_path.with_name("summary.json"),
                         {**summary, "content_hash": content_hash(summary)})
        # A release rewrites a runtime config file.  Nothing about the
        # admission moved, but the ticket identity is a digest of both.
        governance.write_text('{"release": "two"}\n', encoding="utf-8")
        spawned = []

        def fake_spawn(*, digest, record, _controlled_reentry=None, **kwargs):
            spawned.append({"digest": digest, "record": dict(record),
                            "reentry": _controlled_reentry})
            return {"id": f"mission-annual-research:{digest}",
                    "status": "running", **dict(record)}

        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            rebound = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(rebound["rebound_from_ticket_ref"], prior_ref)
        self.assertNotEqual(rebound["id"], prior_ref)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0]["record"]["rebound_from_ticket_ref"], prior_ref)
        # The one-shot re-entry claim is archived against the prior run.
        markers = sorted(ticket_path.parent.glob("controlled-reentry-*.json"))
        self.assertEqual(len(markers), 1)
        # ``spawn`` was stubbed, so nothing ran under that claim.  A claim that
        # bought nothing is not an attempt: the next tick completes it rather
        # than telling a person the lane already tried.
        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            again = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(again["rebound_from_ticket_ref"], prior_ref)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(
            len(sorted(ticket_path.parent.glob("controlled-reentry-*.json"))), 1)
        # A child really runs under the claim: from here the attempt happened.
        moved = launcher.configuration()
        rebound_path = launcher._ticket_path(rebound["id"])
        rebound_path.parent.mkdir(parents=True, exist_ok=True)
        write_owner_only(rebound_path, {
            "schema_version": "0.1", "id": rebound["id"],
            "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
            "configuration": moved, "configuration_hash": content_hash(moved),
            "rebound_from_ticket_ref": prior_ref,
            "started_at": (datetime.now(timezone.utc)
                           + timedelta(minutes=5)).isoformat(),
            "pid": 1, "command": ["true"], "status": "failed",
            "exit_code": 1, "completed_at": None,
        })
        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(LaneChildRejected, "already attempted"):
                launcher.resume(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                    prior_ticket_ref=prior_ref,
                    authorization="test:exact-scheduler-replay")
        self.assertEqual(len(spawned), 2)
        # The owner door: one more re-entry, once, recorded with who said so.
        granted = launcher.authorize_controlled_reentry(
            admission["id"], actor_ref="human:lumos", granted_at=now)
        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            allowed = launcher.resume(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
                prior_ticket_ref=prior_ref,
                authorization="test:exact-scheduler-replay")
        self.assertEqual(allowed["rebound_from_ticket_ref"], prior_ref)
        self.assertEqual(len(spawned), 3)
        self.assertFalse(Path(granted["grant_path"]).exists())
        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(LaneChildRejected, "already attempted"):
                launcher.resume(
                    admission_ref=admission["id"],
                    admission_hash=admission["content_hash"],
                    prior_ticket_ref=prior_ref,
                    authorization="test:exact-scheduler-replay")
        self.assertEqual(len(spawned), 3)
        # A moved admission is a different thing entirely and is still refused.
        with mock.patch.object(launcher, "spawn", side_effect=fake_spawn):
            with self.assertRaisesRegex(
                LaneChildRejected, "changed ticket identity",
            ):
                launcher.resume(
                    admission_ref=admission["id"], admission_hash="0" * 64,
                    prior_ticket_ref=prior_ref,
                    authorization="test:other-authorization")
        self.assertEqual(len(spawned), 3)

    def test_production_budget_refusal_is_visible_to_writer_without_broker_io(self):
        fixture = MissionAnnualFixture(self)
        fixture.harness.clock.value = datetime.now(timezone.utc)
        governance = self._install_manifest_pointer(fixture)
        consumed = fixture.budget.admit(
            policy_version_id="budget-policy:mission-annual:1",
            day=fixture.harness.clock.value.date().isoformat(),
            work_order_ref="work:other-production-budget-consumer",
            attempt_number=1, phase="assessment",
            route_decision_ref="route:other-production-budget-consumer",
            reserved_micros=9_500_000,
        )
        self.assertEqual(consumed["status"], "fresh")

        def unexpected(_request):
            self.fail("budget refusal must happen before broker I/O")

        broker = FakeBroker(fixture.state, unexpected, connections=1)
        self.addCleanup(broker.close)
        self._production_configs(fixture, broker)
        admission = fixture.authority.admit(**fixture.args())
        staging_path = fixture.state / "mission-annual-budget-staging.sqlite"
        CandidateStagingStore(staging_path).close()
        launcher = MissionAnnualResearchLauncher(
            state_dir=fixture.state, staging_path=staging_path,
            web_fetch_governance_path=governance,
            spool_dir=fixture.source.root / "spool",
            python_executable=sys.executable,
        )
        self.addCleanup(launcher.close)
        python_path = os.pathsep.join((
            str(Path(__file__).parents[1] / "src"),
            str(Path(__file__).parents[1]),
        ))
        with mock.patch.dict(os.environ, {"PYTHONPATH": python_path}):
            ticket = launcher.start(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
            )
            self.assertEqual(launcher.wait(timeout=60), 1)
            failed = launcher.status(ticket["id"])
        self.assertEqual(failed["summary"]["status"], "failed")
        self.assertEqual(broker.requests, [])

        coordinator = MissionAnnualResearchCoordinator(
            store=fixture.store, launcher=launcher,
        )
        pointer = {
            "ticket_ref": ticket["id"], "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"],
        }
        coordinator.latest_path.write_text(
            canonical_json({**pointer, "content_hash": content_hash(pointer)}) + "\n",
            encoding="utf-8",
        )
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "recovery_required")
        hold = json.loads(coordinator.holds_path.read_text(encoding="utf-8"))[
            "holds"
        ][admission["id"]]
        self.assertEqual(hold["disposition"], "recovery_required")
        self.assertTrue(
            hold["reason"].startswith("budget_or_pool_refused:"), hold
        )
        self.assertEqual(broker.requests, [])


if __name__ == "__main__":
    unittest.main()
