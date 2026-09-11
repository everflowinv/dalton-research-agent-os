"""Production subprocess coverage for admitted mission annual research."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.store import canonical_json, content_hash
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.mission_annual_research_launcher import (
    MissionAnnualResearchLauncher,
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
            replay_ticket = launcher.start(
                admission_ref=admission["id"],
                admission_hash=admission["content_hash"],
            )
            self.assertEqual(launcher.wait(timeout=60), 0)
            replay_finished = launcher.status(replay_ticket["id"])
        self.assertEqual(len(broker.requests), 2)
        replay_summary = replay_finished["summary"]
        self.assertEqual(replay_summary["status"], "complete")
        self.assertEqual(len(replay_summary["outcomes"]), 1)

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


if __name__ == "__main__":
    unittest.main()
