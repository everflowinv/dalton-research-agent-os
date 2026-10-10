"""As-of material/source proofs and admission boundary regression."""
from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from dalton_core.material_requests import open_request, close
from dalton_core.weekly_brief import WeeklyBriefConflict
from dalton_core.weekly_brief_coordinator import _utc
from tests import test_mission_progress_weekly as fixtures


class ProgressSnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MissionProgressLocalCycleTests(
            methodName="test_normal_cycle_delivers_exact_progress_body_and_replays_once")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def request(self, subject=None, now=None):
        f = self.fixture
        return open_request(f.state, requester={"lane": "dossier", "product_ref": "dossier:synthetic"},
                            subject=subject or f.mission["universe"][0]["company_ref"],
                            need={"kind": "document", "doc_kinds": ["10-K"]},
                            now=(now or f.now - timedelta(minutes=1)).isoformat())

    def test_material_request_state_is_frozen_and_future_close_does_not_rewrite_issue(self):
        f = self.fixture
        request = self.request()
        self.request(subject="company:outside-this-mission")
        result = f.cycle()
        before = f.weekly.issue(result["issue_version_ref"])
        entries = before["snapshot"]["material_requests"]["entries"]
        self.assertEqual([request["id"]], [e["request"]["id"] for e in entries])
        self.assertEqual("open", entries[0]["status"])
        self.assertIn("10-K", before["body"])
        close(f.state, request["id"], status="found", reason="Fixture later success", now=(f.now + timedelta(days=1)).isoformat())
        self.assertEqual(before, f.weekly.issue(before["id"]))

    def test_material_request_event_byte_drift_refuses_frozen_read(self):
        f = self.fixture
        self.request()
        issue_id = f.cycle()["issue_version_ref"]
        c = sqlite3.connect(f.state / "material-requests.sqlite")
        self.addCleanup(c.close)
        c.execute("DROP TRIGGER material_request_events_no_update")
        c.execute("UPDATE material_request_events SET record_json='{} '")
        c.commit()
        with self.assertRaises(WeeklyBriefConflict):
            f.weekly.issue(issue_id)

    def test_records_use_actual_creation_time_and_cannot_have_earlier_delivery(self):
        f = self.fixture
        later = _utc(f.now + timedelta(days=2))
        with patch("dalton_core.mission_progress_weekly._now", return_value=later):
            result = f.cycle()
        issue = f.weekly.issue(result["issue_version_ref"])
        self.assertEqual(later, issue["created_at"])
        self.assertEqual(_utc(f.now), issue["period"]["end"])
        self.assertEqual(later, f.agenda.pending_outbox()[0]["payload"]["created_at"])
        with self.assertRaises(WeeklyBriefConflict):
            f.weekly.record_scheduled_delivery(
                cycle_id=result["cycle_ref"], issue_version_ref=issue["id"], issue_version_hash=issue["content_hash"],
                destination_ref=f.plan["destination_ref"], external_message_ref="file:" + issue["markdown_hash"],
                artifact_sha256=issue["markdown_hash"], delivered_at=_utc(f.now), delivery_id="delivery:early",
                actor_ref="core", idempotency_key="delivery:early")



if __name__ == "__main__":
    unittest.main()
