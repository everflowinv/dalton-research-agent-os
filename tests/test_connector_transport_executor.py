from __future__ import annotations

import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_runner import ConnectorRunnerAdmissionGate, StaticAdapterResolver
from dalton_core.connector_transport_executor import (
    ConnectorTransportExecutor,
    SimulatedRunnerCrash,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.recorded_connector_adapter import RecordedFixtureAdapter
from dalton_core.runner_journal import RunnerJournal
from tests import test_connector_runner as runner_helpers


FIXTURES = Path(__file__).with_name("fixtures") / "connector"


class TransportHarness:
    def __init__(
        self,
        fixture: str,
        *,
        fault_at: str | None = None,
        spool_max_total_bytes: int = 2_000_000,
    ):
        self.base = runner_helpers.ConnectorRunnerControlPlaneTests(
            "test_closed_contracts_and_static_resolver"
        )
        self.base.setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.observability = ObservabilityStore(self.base.core)
        self.journal = RunnerJournal(self.base.core, clock=self.base.clock)
        self.spool = RawSpool(
            self.temp.name, max_total_bytes=spool_max_total_bytes
        )
        adapter = RecordedFixtureAdapter(
            FIXTURES / f"{fixture}.json",
            advance_clock=self.base.clock.advance,
        )
        binding_ref = self.base.manifest["bindings"][0]["binding_ref"]
        resolver = StaticAdapterResolver(
            self.base.manifest,
            {binding_ref: adapter},
            {binding_ref: runner_helpers.validate_recorded_input},
        )
        self.gate = ConnectorRunnerAdmissionGate(
            scheduler=self.base.scheduler,
            catalog=self.base.catalog,
            connectors=self.base.connectors,
            resolver=resolver,
            visibility_scopes=["research"],
            clock=self.base.clock,
        )
        self.authority = ConnectorAuthorityPort(
            connectors=self.base.connectors,
            observability=self.observability,
            scheduler=self.base.scheduler,
        )

        def fault_hook(barrier: str) -> None:
            if barrier == fault_at:
                raise SimulatedRunnerCrash(barrier)

        self.executor = ConnectorTransportExecutor(
            gate=self.gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority,
            connector_reader=self.base.connectors,
            clock=self.base.clock,
            fault_hook=fault_hook if fault_at is not None else None,
        )

    def extend_scheduler_past_profile_timeout(self) -> None:
        renewed = self.base.scheduler.renew(
            self.base.work.id,
            1,
            self.base.profile["runner_actor_ref"],
            self.base.claim["lease_token"],
            extend_seconds=5,
        )
        self.base.claim["lease"] = renewed

    def request(self) -> dict:
        return self.base.runner_request()

    def execute(self) -> dict:
        return self.executor.execute(
            self.request(), scheduler_lease_token=self.base.claim["lease_token"]
        )

    def recovery_executor(self) -> ConnectorTransportExecutor:
        return ConnectorTransportExecutor(
            gate=self.gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority,
            connector_reader=self.base.connectors,
            clock=self.base.clock,
        )

    def counts(self) -> dict[str, int]:
        core_tables = (
            "connector_quota_reservations",
            "connector_physical_attempts",
            "connector_usage_entries",
            "connector_cost_entries",
            "connector_quota_settlements",
            "observability_artifact_versions_v2",
            "connector_source_envelopes",
            "runner_attempt_journal_events",
        )
        result = {
            table: int(
                self.base.core.connection.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
            )
            for table in core_tables
        }
        for table in ("scheduler_result_envelopes", "scheduler_formal_results"):
            result[table] = int(
                self.base.scheduler.connection.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
            )
        return result

    def close(self) -> None:
        self.temp.cleanup()
        self.base.doCleanups()


class ConnectorTransportExecutorTests(unittest.TestCase):
    def harness(
        self,
        fixture: str,
        *,
        fault_at: str | None = None,
        spool_max_total_bytes: int = 2_000_000,
    ) -> TransportHarness:
        harness = TransportHarness(
            fixture,
            fault_at=fault_at,
            spool_max_total_bytes=spool_max_total_bytes,
        )
        self.addCleanup(harness.close)
        return harness

    def test_recorded_success_runs_full_authority_chain_and_replays_zero_rows(self) -> None:
        harness = self.harness("success")
        response = harness.execute()
        self.assertEqual(response["outcome"], "succeeded")
        self.assertIsNotNone(response["raw_artifact_version_ref"])
        self.assertIsNotNone(response["source_envelope_ref"])
        runner_helpers.assert_wire_schema(
            self, "connector-runner-response.schema.json", response
        )
        expected = {
            "connector_quota_reservations": 1,
            "connector_physical_attempts": 1,
            "connector_usage_entries": 1,
            "connector_cost_entries": 1,
            "connector_quota_settlements": 1,
            "observability_artifact_versions_v2": 1,
            "connector_source_envelopes": 1,
            "runner_attempt_journal_events": 5,
            "scheduler_result_envelopes": 1,
            "scheduler_formal_results": 1,
        }
        self.assertEqual(harness.counts(), expected)
        duplicate = harness.execute()
        self.assertEqual(duplicate["idempotency_status"], "duplicate")
        self.assertEqual(harness.counts(), expected)
        raw_hash = harness.base.core.connection.execute(
            "SELECT artifact_content_hash FROM observability_artifact_versions_v2"
        ).fetchone()["artifact_content_hash"]
        self.assertTrue(harness.spool.object_exists(raw_hash))

    def test_recorded_empty_is_a_successful_empty_source_envelope(self) -> None:
        harness = self.harness("empty")
        response = harness.execute()
        self.assertEqual(response["outcome"], "succeeded")
        envelope = harness.base.core.connection.execute(
            "SELECT record_json FROM connector_source_envelopes"
        ).fetchone()["record_json"]
        self.assertIn('"status":"empty"', envelope)
        self.assertIn('"source_record_refs":[]', envelope)

    def test_429_is_retryable_without_fabricated_artifact_or_envelope(self) -> None:
        harness = self.harness("rate-limited")
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertIsNotNone(response["retry_at"])
        self.assertIsNone(response["raw_artifact_version_ref"])
        self.assertIsNone(response["source_envelope_ref"])
        attempt = harness.base.core.connection.execute(
            "SELECT outcome,retry_at FROM connector_physical_attempts"
        ).fetchone()
        self.assertEqual(attempt["outcome"], "rate_limited")
        self.assertEqual(attempt["retry_at"], response["retry_at"])
        status = harness.base.scheduler.status(harness.base.work.id)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["attempt_number"], 2)
        self.assertEqual(
            harness.counts()["observability_artifact_versions_v2"], 0
        )
        self.assertEqual(harness.counts()["connector_source_envelopes"], 0)

    def test_timeout_is_runner_determined_and_conservatively_settled(self) -> None:
        harness = self.harness("timeout")
        harness.extend_scheduler_past_profile_timeout()
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertIsNone(response["raw_artifact_version_ref"])
        attempt = harness.base.core.connection.execute(
            "SELECT outcome FROM connector_physical_attempts"
        ).fetchone()
        settlement = harness.base.core.connection.execute(
            "SELECT state FROM connector_quota_settlements"
        ).fetchone()
        self.assertEqual(attempt["outcome"], "timeout")
        self.assertEqual(settlement["state"], "indeterminate")
        self.assertEqual(harness.spool.total_bytes(), 0)

    def test_runner_watchdog_interrupts_a_hanging_recorded_adapter(self) -> None:
        harness = self.harness("success")
        executor = harness.recovery_executor()
        deadline = (
            harness.base.clock.value + timedelta(milliseconds=50)
        ).isoformat(timespec="microseconds")
        started = time.monotonic()
        with self.assertRaises(Exception) as raised:
            executor._invoke_adapter(  # noqa: SLF001 - explicit watchdog probe
                lambda request, sink: time.sleep(1),
                {"deadline_at": deadline},
                object(),
            )
        self.assertEqual(type(raised.exception).__name__, "_AdapterDeadlineExceeded")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_adapter_crash_is_failed_attempt_not_a_fake_source(self) -> None:
        harness = self.harness("crash")
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertIsNone(response["source_envelope_ref"])
        attempt = harness.base.core.connection.execute(
            "SELECT outcome FROM connector_physical_attempts"
        ).fetchone()
        settlement = harness.base.core.connection.execute(
            "SELECT state FROM connector_quota_settlements"
        ).fetchone()
        self.assertEqual(attempt["outcome"], "failed")
        self.assertEqual(settlement["state"], "indeterminate")
        self.assertEqual(harness.spool.gc_orphans(), 0)

    def test_w0_through_w4_crash_windows_recover_idempotently(self) -> None:
        cases = (
            ("after_admitted", "admitted"),
            ("after_reserved", "released_recovered"),
            ("after_sink_opened", "released_recovered"),
            ("after_transport_started", "indeterminate_recovered"),
            ("after_observed", "responded"),
            ("after_responded", "responded"),
        )
        for barrier, expected_state in cases:
            with self.subTest(barrier=barrier):
                harness = TransportHarness("success", fault_at=barrier)
                try:
                    with self.assertRaises(SimulatedRunnerCrash):
                        harness.execute()
                    kwargs = {}
                    if barrier == "after_observed":
                        kwargs["scheduler_lease_token"] = harness.base.claim[
                            "lease_token"
                        ]
                    first = harness.executor.recover(
                        harness.request()["id"], **kwargs
                    )
                    self.assertEqual(first["state"], expected_state)
                    counts = harness.counts()
                    second = harness.executor.recover(harness.request()["id"])
                    self.assertEqual(second["status"], "no-op")
                    self.assertEqual(harness.counts(), counts)
                    if barrier == "after_transport_started":
                        settlement = harness.base.core.connection.execute(
                            "SELECT state FROM connector_quota_settlements"
                        ).fetchone()
                        self.assertEqual(settlement["state"], "indeterminate")
                    if barrier == "after_reserved":
                        settlement = harness.base.core.connection.execute(
                            "SELECT state FROM connector_quota_settlements"
                        ).fetchone()
                        self.assertEqual(settlement["state"], "released")
                finally:
                    harness.close()

    def test_reservation_before_journal_coverage_recovers_conservatively(self) -> None:
        harness = self.harness("success", fault_at="after_quota_reserved")
        with self.assertRaises(SimulatedRunnerCrash):
            harness.execute()
        request_ref = harness.request()["id"]
        self.assertEqual(harness.journal.latest(request_ref)["state"], "admitted")
        self.assertEqual(harness.executor.recover(request_ref)["state"], "admitted")
        first = harness.executor.recover_orphaned_reservations()
        self.assertEqual(len(first), 1)
        settlement = harness.base.core.connection.execute(
            "SELECT state FROM connector_quota_settlements"
        ).fetchone()
        self.assertEqual(settlement["state"], "indeterminate")
        counts = harness.counts()
        self.assertEqual(harness.executor.recover_orphaned_reservations(), [])
        self.assertEqual(harness.counts(), counts)

    def test_released_settlement_crash_replays_the_same_idempotency_key(self) -> None:
        harness = self.harness(
            "success",
            fault_at="after_released_settlement",
            spool_max_total_bytes=1,
        )
        with self.assertRaises(SimulatedRunnerCrash):
            harness.execute()
        self.assertEqual(
            harness.base.core.connection.execute(
                "SELECT state FROM connector_quota_settlements"
            ).fetchone()["state"],
            "released",
        )
        recovered = harness.recovery_executor().recover(harness.request()["id"])
        self.assertEqual(recovered["state"], "released_recovered")
        counts = harness.counts()
        self.assertEqual(
            harness.recovery_executor().recover(harness.request()["id"])["status"],
            "no-op",
        )
        self.assertEqual(harness.counts(), counts)

    def test_orphaned_reservation_without_journal_is_never_released(self) -> None:
        harness = self.harness("success")
        admission = harness.gate.validate(
            harness.request(),
            scheduler_lease_token=harness.base.claim["lease_token"],
        )
        harness.gate.reserve_for_admission(
            admission,
            scheduler_lease_token=harness.base.claim["lease_token"],
            idempotency_key="runner:orphan:reserve",
        )
        first = harness.executor.recover_orphaned_reservations()
        self.assertEqual(len(first), 1)
        settlement = harness.base.core.connection.execute(
            "SELECT state FROM connector_quota_settlements"
        ).fetchone()
        self.assertEqual(settlement["state"], "indeterminate")
        counts = harness.counts()
        self.assertEqual(harness.executor.recover_orphaned_reservations(), [])
        self.assertEqual(harness.counts(), counts)

    def test_crashes_between_every_authority_write_replay_to_one_fact_chain(self) -> None:
        barriers = (
            "after_attempt_recorded",
            "after_usage_recorded",
            "after_cost_recorded",
            "after_settlement_recorded",
            "after_artifact_recorded",
            "after_source_recorded",
            "after_scheduler_completed",
        )
        for barrier in barriers:
            with self.subTest(barrier=barrier):
                harness = TransportHarness("success", fault_at=barrier)
                try:
                    with self.assertRaises(SimulatedRunnerCrash):
                        harness.execute()
                    recovery = harness.recovery_executor()
                    recovered = recovery.recover(
                        harness.request()["id"],
                        scheduler_lease_token=harness.base.claim["lease_token"],
                    )
                    self.assertEqual(recovered["state"], "responded")
                    counts = harness.counts()
                    self.assertEqual(
                        counts,
                        {
                            "connector_quota_reservations": 1,
                            "connector_physical_attempts": 1,
                            "connector_usage_entries": 1,
                            "connector_cost_entries": 1,
                            "connector_quota_settlements": 1,
                            "observability_artifact_versions_v2": 1,
                            "connector_source_envelopes": 1,
                            "runner_attempt_journal_events": 5,
                            "scheduler_result_envelopes": 1,
                            "scheduler_formal_results": 1,
                        },
                    )
                    self.assertEqual(
                        recovery.recover(harness.request()["id"])["status"],
                        "no-op",
                    )
                    self.assertEqual(harness.counts(), counts)
                finally:
                    harness.close()

    def test_authority_port_does_not_expose_a_connection(self) -> None:
        harness = self.harness("success")
        self.assertFalse(hasattr(harness.authority, "connection"))
        public = {
            name
            for name in dir(harness.authority)
            if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "complete", "record_cost", "record_physical_attempt",
                "record_source_envelope", "record_usage",
                "register_artifact_version_v2", "settle_quota",
            },
        )


if __name__ == "__main__":
    unittest.main()
