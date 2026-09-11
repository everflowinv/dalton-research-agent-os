from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.host_recovery import host_recovery_states, validate_recovery_policy
from dalton_core.mission_source_discovery import (
    DiscoveryPlanError, MissionSourceDiscoveryCoordinator, build_discovery_plan,
    validate_discovery_plan,
)
from dalton_core.public_web_core_search import FakeWebSearchHandle
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities
from tests.test_mission_source_discovery import ACN, CTSH, Clock, FakeAcquisitionLauncher
from tests.test_mission_web_search_discovery import (
    FakeWebSearchLauncher, mission_with_web_status, web_plan_for_tests,
)
from tests.test_public_web_core_search import WebSearchHarness


RECOVERY = {"schema_version": "0.1", "backoff_multiplier": 2,
            "max_cooldown_seconds": 86400}
POLICY = {"minimum_distinct_urls": 3, "window_seconds": 86400,
          "cooldown_seconds": 21600, "recovery": RECOVERY}


class HostRecoveryFoldTests(unittest.TestCase):
    def test_window_expiry_does_not_release_quarantine_and_failures_back_off(self):
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        events = [{"host": "blocked.example", "created_at": (now + timedelta(seconds=i)).isoformat(),
                   "document_ref": f"url:{i}", "outcome": "transport_terminal"} for i in range(3)]

        def state(at):
            return host_recovery_states(events, as_of=at, **POLICY)

        self.assertEqual(state(now + timedelta(hours=1))[0]["state"], "quarantined")
        later = now + timedelta(days=2)
        self.assertEqual(state(later)[0]["state"], "probe_due")
        for index, outcome in enumerate(("unknown_failure", "transport_retryable", "transport_terminal")):
            events.append({"host": "blocked.example", "created_at": later.isoformat(),
                           "document_ref": "url:0", "outcome": outcome})
            held = state(later)[0]
            self.assertEqual(held["state"], "quarantined")
            self.assertEqual(held["cooldown_seconds"], min(21600 * 2 ** (index + 1), 86400))
            later += timedelta(days=2)
        events.append({"host": "blocked.example", "created_at": later.isoformat(),
                       "document_ref": "url:0", "outcome": "acquired"})
        self.assertEqual(state(later), [])

    def test_retryable_failures_alone_never_quarantine_a_host(self):
        now = datetime.now(timezone.utc)
        events = [{"host": "temporary.example", "created_at": now.isoformat(),
                   "document_ref": f"url:{i}", "outcome": "transport_retryable"} for i in range(5)]
        self.assertEqual(host_recovery_states(events, as_of=now, **POLICY), [])

    def test_multiplier_one_retains_the_configured_interval_after_a_failed_probe(self):
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        events = [{"host": "fixed.example", "created_at": now.isoformat(),
                   "document_ref": f"url:{i}", "outcome": "transport_terminal"}
                  for i in range(3)]
        later = now + timedelta(days=2)
        events.append({"host": "fixed.example", "created_at": later.isoformat(),
                       "document_ref": "url:0", "outcome": "transport_terminal"})
        fixed_policy = {**POLICY, "recovery": {**RECOVERY, "backoff_multiplier": 1}}
        held = host_recovery_states(events, as_of=later, **fixed_policy)[0]
        self.assertEqual(held["state"], "quarantined")
        self.assertEqual(held["cooldown_seconds"], POLICY["cooldown_seconds"])
        self.assertEqual(
            held["next_probe"],
            (later + timedelta(seconds=POLICY["cooldown_seconds"])).isoformat(
                timespec="microseconds"))

    def test_recovery_configuration_is_closed_and_preserves_legacy_plan(self):
        old = web_plan_for_tests(acquisition={"preferred_hosts": [], "skip_hosts": []})
        baseline = json.dumps(old, sort_keys=True)
        validate_discovery_plan(old)
        self.assertEqual(json.dumps(old, sort_keys=True), baseline)
        new = dict(old, schema_version="0.5", acquisition={**old["acquisition"], "failure_cooldown": POLICY})
        new["content_hash"] = content_hash({k: v for k, v in new.items() if k != "content_hash"})
        self.assertEqual(validate_discovery_plan(new)["acquisition"]["failure_cooldown"], POLICY)
        with self.assertRaises(ValueError):
            validate_recovery_policy({**RECOVERY, "max_cooldown_seconds": 5}, initial_seconds=21600)
        with self.assertRaises(ValueError):
            validate_recovery_policy({**RECOVERY, "backoff_multiplier": True}, initial_seconds=21600)
        fixed = validate_recovery_policy(
            {**RECOVERY, "backoff_multiplier": 1}, initial_seconds=21600)
        self.assertEqual(fixed["backoff_multiplier"], 1)
        new["acquisition"]["failure_cooldown"] = {**POLICY, "recovery": {**RECOVERY, "extra": 1}}
        new["content_hash"] = content_hash({k: v for k, v in new.items() if k != "content_hash"})
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(new)


class HostRecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.clock = Clock()
        citations = [{"url": f"https://blocked.example/doc/{i}", "title": f"Blocked {i}"} for i in range(5)]
        citations.append({"url": "https://available.example/company", "title": "Available IR"})
        self.h = WebSearchHarness(Path(temp.name), FakeWebSearchHandle(citations), clock=self.clock)
        self.addCleanup(self.h.close)
        state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        params = mission_with_web_status(state, status="connected", grant=True, version=1, prior=None)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        base = web_plan_for_tests()
        self.plan = build_discovery_plan(
            plan_id="discovery-plan:host-recovery:test", created_at=base["created_at"],
            mission_ref=base["mission_ref"], companies={ACN: "Accenture", CTSH: "Cognizant"},
            source_ref="source:web-search", max_calls_24h=100, specs=base["specs"],
            acquisition={"preferred_hosts": ["blocked.example"], "skip_hosts": []},
            failure_cooldown=POLICY)
        search = FakeWebSearchLauncher(self.h, self.missions, self.plan)
        self.fetch = FakeAcquisitionLauncher(self.h)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=search, acquisition_launcher=self.fetch, clock=self.clock)
        self.assertEqual(self.coordinator.launch_discovery()["status"], "launched")
        self.coordinator.settle_dispatches()
        self.discovery = self.missions.source_discoveries(self.mission["id"])[0]
        invocation = self.h.core.connection.execute(
            "SELECT content_hash FROM connector_invocations WHERE connector_invocation_id=?",
            (self.discovery["connector_invocation_ref"],)).fetchone()
        self.evidence_hash = invocation["content_hash"]
        for i in range(3):
            doc = self.missions.next_discovered_document(source_ref="source:web-search", included_hosts=["blocked.example"])
            self.missions.mark_discovered_document_launched(doc["record_id"], f"fetch:seed:{i}")
            self.fail(doc["record_id"])

    def fail(self, ref):
        with patch("dalton_core.coverage_mission._now", return_value=self.clock().isoformat(timespec="microseconds")):
            self.missions.settle_discovered_document(
                ref, status="acquisition_failed", reason="forbidden", failure_retryable=False,
                transport_code="HTTP_403", transport_evidence_ref=self.discovery["connector_invocation_ref"],
                transport_evidence_hash=self.evidence_hash)

    def test_other_host_runs_first_and_only_one_probe_returns_after_window_expiry(self):
        self.clock.advance(days=2)  # The initial rolling window has expired.
        live = self.coordinator.launch_acquisition()
        self.assertEqual(live["status"], "launched")
        row = self.h.core.connection.execute(
            "SELECT host FROM coverage_mission_discovered_documents WHERE record_id=?",
            (live["record_id"],)).fetchone()
        self.assertEqual(row["host"], "available.example")
        self.assertNotIn("host_recovery_probe", live)
        self.missions.settle_discovered_document(live["record_id"], status="acquired")
        probe = self.coordinator.launch_acquisition()
        self.assertTrue(probe["host_recovery_probe"])
        calls = len(self.fetch.calls)
        self.assertEqual(self.coordinator.launch_acquisition()["status"], "busy")
        self.assertEqual(len(self.fetch.calls), calls)
        self.fail(probe["record_id"])
        held = self.coordinator.launch_acquisition()
        self.assertEqual(held["status"], "idle")
        self.assertEqual(len(self.fetch.calls), calls)
        self.assertEqual(held["cooldown_hosts"][0]["cooldown_seconds"], 43200)
        self.assertEqual(held["held_by_host_quarantine"], 1)
        self.clock.advance(hours=13)
        recovered = self.coordinator.launch_acquisition()
        self.assertTrue(recovered["host_recovery_probe"])
        with patch("dalton_core.coverage_mission._now", return_value=self.clock().isoformat(timespec="microseconds")):
            self.missions.settle_discovered_document(recovered["record_id"], status="acquired")
        self.assertEqual(self.missions.host_failure_cooldowns(
            source_ref="source:web-search", as_of=self.clock(), **POLICY), [])

    def test_static_skip_also_excludes_due_probes(self):
        self.clock.advance(days=2)
        self.coordinator.skip_hosts = ("blocked.example", "available.example")
        self.assertEqual(self.coordinator.launch_acquisition()["status"], "idle")
        self.assertEqual(self.fetch.calls, [])

    def test_recreated_coordinator_recovers_quarantine_from_persisted_attempts(self):
        self.clock.advance(days=2)
        database = self.h.core.path
        self.h.core.close()
        self.h.core = DaltonStore(database)
        restarted_missions = CoverageMissionAuthority(self.h.core)
        restarted_fetch = FakeAcquisitionLauncher(self.h)
        restarted = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=restarted_missions, plan=self.plan,
            search_launcher=None, acquisition_launcher=restarted_fetch, clock=self.clock)
        live = restarted.launch_acquisition()
        self.assertEqual(live["status"], "launched")
        row = self.h.core.connection.execute(
            "SELECT host FROM coverage_mission_discovered_documents WHERE record_id=?",
            (live["record_id"],)).fetchone()
        self.assertEqual(row["host"], "available.example")
        self.assertNotIn("host_recovery_probe", live)
        restarted_missions.settle_discovered_document(live["record_id"], status="acquired")
        probe = restarted.launch_acquisition()
        self.assertTrue(probe["host_recovery_probe"])
        self.assertEqual(len(restarted_fetch.calls), 2)
        self.assertEqual(restarted.launch_acquisition()["status"], "busy")
        self.assertEqual(len(restarted_fetch.calls), 2)
        with patch("dalton_core.coverage_mission._now",
                   return_value=self.clock().isoformat(timespec="microseconds")):
            restarted_missions.settle_discovered_document(
                probe["record_id"], status="acquisition_failed", reason="forbidden",
                failure_retryable=False, transport_code="HTTP_403",
                transport_evidence_ref=self.discovery["connector_invocation_ref"],
                transport_evidence_hash=self.evidence_hash)
        held = restarted.launch_acquisition()
        self.assertEqual(held["status"], "idle")
        self.assertEqual(held["cooldown_hosts"][0]["cooldown_seconds"], 43200)
        self.assertEqual(len(restarted_fetch.calls), 2)

    def test_terminal_rows_reenter_only_as_an_explicit_due_single_probe(self):
        # Exhaust the two remaining new URLs on the same unreachable host.
        for i in range(2):
            doc = self.missions.next_discovered_document(
                source_ref="source:web-search", included_hosts=["blocked.example"])
            self.missions.mark_discovered_document_launched(doc["record_id"], f"fetch:extra:{i}")
            self.fail(doc["record_id"])
        self.coordinator.skip_hosts = ("available.example",)
        self.clock.advance(days=2)
        self.assertIsNone(self.missions.retryable_failed_document(
            source_ref="source:web-search", older_than=timedelta(seconds=1), as_of=self.clock()))
        probe = self.coordinator.launch_acquisition()
        self.assertEqual(probe["status"], "launched")
        self.assertTrue(probe["retry"])
        self.assertTrue(probe["host_recovery_probe"])
        self.assertEqual(self.coordinator.launch_acquisition()["status"], "busy")
        self.assertEqual(len(self.fetch.calls), 1)
