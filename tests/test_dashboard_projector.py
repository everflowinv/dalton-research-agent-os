from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.dashboard import DashboardQueryService
from dalton_core.agenda import AgendaStore
from dalton_core.dashboard_projector import (
    DashboardProjector,
    DashboardProjectorError,
    ProjectionSourceError,
)
from dalton_core.capability_catalog import (
    CapabilityCatalog,
    ExternalSnapshotRejected,
)
from dalton_core.connector import ConnectorStore
from dalton_core.contracts import ExecutionInvocation, ExecutionKind
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_metadata import OpenClawMetadataImporter
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_model_router import profile
from tests.test_connector import (
    MutableClock as ConnectorClock,
    WHEN as CONNECTOR_WHEN,
    price_rate_spec as connector_price_rate_spec,
    profile_spec as connector_profile_spec,
    rate_policy_spec as connector_rate_policy_spec,
)
from tests.test_openclaw_metadata import (
    FakeAuthorities as MetadataAuthorities,
    NOW as METADATA_NOW,
    make_snapshot as make_metadata_snapshot,
)


START = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
SECRET_PROMPT = "SECRET-PROMPT-DO-NOT-PROJECT"
SECRET_USAGE = "SECRET-USAGE-CREDENTIAL"
SECRET_LOCATOR = "SECRET-ARTIFACT-LOCATOR"
SECRET_OUTPUT = "SECRET-FULL-OUTPUT"
SECRET_CONNECTOR = "SECRET-CONNECTOR-AUTHORITY-BODY"


class MutableClock:
    def __init__(self, value: datetime = START):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def work_order(identifier: str, capability: str) -> dict:
    now = START.isoformat()
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": now,
        "updated_at": now,
        "question": f"{SECRET_PROMPT}:{identifier}",
        "requested_capabilities": [capability],
        "runtime_profile_ref": "runtime:projector-test",
        "budget": {"max_seconds": 30},
        "idempotency_key": f"enqueue:{identifier}",
        "declared_side_effects": [],
        "status": "ready",
        "input_refs": [],
        "metadata": {"prompt": SECRET_PROMPT},
    }


def invocation(identifier: str, work_ref: str, *, capability: str, model: str) -> dict:
    now = START.isoformat()
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": now,
        "work_order_ref": work_ref,
        "profile_ref": f"profile:{model}",
        "granularity": "task",
        "capability": capability,
        "provider": "provider-a",
        "model": model,
        "model_family": "family-a",
        "input_refs": [],
        "output_refs": ["artifact:report"] if identifier == "inv-1" else [],
        "started_at": now,
        "completed_at": (START + timedelta(seconds=1)).isoformat(),
        "usage": {"prompt": SECRET_PROMPT},
        "side_effects": [],
        "runtime_ref": "runtime:projector-test",
        "actor_ref": "agent:test",
        "parent_ref": None,
        "environment_hash": "e" * 64,
    }


def result_envelope() -> dict:
    return {
        "schema_version": "0.1",
        "id": "result-1",
        "created_at": (START + timedelta(seconds=2)).isoformat(),
        "work_order_ref": "work-1",
        "invocation_ref": "inv-1",
        "status": "succeeded",
        "outputs": {"full_output": SECRET_OUTPUT},
        "artifact_refs": ["artifact:report"],
        "actual_side_effects": [],
        "usage_refs": ["usage-1"],
        "error": None,
        "metadata": {"prompt": SECRET_PROMPT},
    }


class DashboardProjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.core_path = root / "core.sqlite"
        self.scheduler_path = root / "scheduler.sqlite"
        self.projection_path = root / "dashboard.sqlite"
        self.clock = MutableClock()
        self._seed_scheduler()
        self._seed_core()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_scheduler(self) -> None:
        scheduler = Scheduler(
            self.scheduler_path,
            clock=self.clock,
            default_lease_seconds=5,
            max_lease_seconds=5,
            max_renew_seconds=2,
            max_total_lease_seconds=10,
        )
        try:
            scheduler.enqueue(work_order("work-1", "research"))
            scheduler.enqueue(work_order("work-2", "verify"))
            first = scheduler.claim("worker:one", work_order_id="work-1")
            scheduler.complete(
                "work-1",
                1,
                "worker:one",
                first["lease_token"],
                result_envelope(),
                idempotency_key="complete:work-1",
            )
            scheduler.claim("worker:two", work_order_id="work-2")
            # Deliberately leave the latest authority state as leased.  The
            # projector clock advances beyond expiry without invoking sweep.
            self.clock.advance(10)
        finally:
            scheduler.close()

    def _seed_core(self) -> None:
        store = DaltonStore(self.core_path)
        obs = ObservabilityStore(store)
        try:
            store.register_invocation(
                invocation("inv-1", "work-1", capability="research", model="model-a")
            )
            store.register_invocation(
                invocation("inv-2", "work-2", capability="verify", model="model-b")
            )
            store.register_invocation(
                invocation("inv-3", "work-2", capability="summarize", model="model-b")
            )
            obs.create_workflow_version(
                "workflow:research",
                version_id="workflow-version:1",
                title="更新研究判断",
                objective="核对证据并更新结论",
                root_work_order_refs=["work-1"],
                scope_refs=["entity:wanhua"],
                governance_policy_ref="policy-1",
                actor_ref="human:lumos",
            )
            obs.link_work_order(
                "workflow:research",
                "work-1",
                "work-2",
                link_id="work-link:1",
                sequence=1,
                actor_ref="agent:planner",
            )
            obs.record_usage(
                "inv-1",
                entry_id="usage-1",
                occurred_at=START.isoformat(),
                workflow_ref="workflow:research",
                metering_source="provider_reported",
                measurement_status="final",
                raw_usage={"credential": SECRET_USAGE},
                actor_ref="system:meter",
                input_tokens=2,
                output_tokens=1,
                total_tokens=3,
                requests=1,
            )
            obs.record_usage(
                "inv-2",
                entry_id="usage-2-partial",
                occurred_at=START.isoformat(),
                workflow_ref="workflow:research",
                metering_source="worker_reported",
                measurement_status="partial",
                raw_usage={"credential": SECRET_USAGE, "total_tokens": 9},
                actor_ref="system:meter",
                input_tokens=5,
                output_tokens=4,
                total_tokens=9,
            )
            obs.record_usage(
                "inv-2",
                entry_id="usage-2-final",
                occurred_at=START.isoformat(),
                workflow_ref="workflow:research",
                metering_source="provider_reported",
                measurement_status="final",
                raw_usage={"credential": SECRET_USAGE, "total_tokens": 10},
                actor_ref="system:meter",
                input_tokens=6,
                output_tokens=4,
                total_tokens=10,
                correction_of_ref="usage-2-partial",
            )
            obs.record_usage(
                "inv-3",
                entry_id="usage-3",
                occurred_at=START.isoformat(),
                workflow_ref="workflow:research",
                metering_source="launcher_measured",
                measurement_status="unavailable",
                raw_usage={"credential": SECRET_USAGE},
                actor_ref="system:meter",
            )
            usd = obs.create_price_rate_version(
                "price:usd",
                version_id="price:usd:v1",
                provider="provider-a",
                model="model-a",
                charge_type="input_tokens",
                unit_quantity=1,
                unit_price_micros=2,
                currency="USD",
                effective_from="2026-01-01T00:00:00+00:00",
                effective_until=None,
                source_ref="pricing:official",
                actor_ref="human:governance",
            )
            eur = obs.create_price_rate_version(
                "price:eur",
                version_id="price:eur:v1",
                provider="provider-a",
                model="model-b",
                charge_type="input_tokens",
                unit_quantity=1,
                unit_price_micros=1,
                currency="EUR",
                effective_from="2026-01-01T00:00:00+00:00",
                effective_until=None,
                source_ref="pricing:official",
                actor_ref="human:governance",
            )
            obs.record_cost(
                "usage-1",
                cost_entry_id="cost-usd",
                price_rate_refs=[usd["id"]],
                amount_micros=4,
                currency="USD",
                cost_status="actual",
                calculation_ref="calculator:0.1",
                actor_ref="system:cost",
            )
            obs.record_cost(
                "usage-2-final",
                cost_entry_id="cost-eur",
                price_rate_refs=[eur["id"]],
                amount_micros=6,
                currency="EUR",
                cost_status="estimated",
                calculation_ref="calculator:0.1",
                actor_ref="system:cost",
            )
            obs.record_cost(
                "usage-3",
                cost_entry_id="cost-unpriced",
                price_rate_refs=[],
                amount_micros=None,
                currency="USD",
                cost_status="unpriced",
                calculation_ref="pricing:missing",
                actor_ref="system:cost",
            )
            envelope_hash = content_hash(result_envelope())
            obs.register_artifact_version(
                "artifact:report",
                version_id="artifact:report:v1",
                title="研究报告草稿",
                kind="deliverable",
                media_type="application/pdf",
                artifact_content_hash="a" * 64,
                size_bytes=100,
                storage_locator=f"artifact-store:{SECRET_LOCATOR}:v1",
                producer_invocation_ref="inv-1",
                result_envelope_ref="result-1",
                result_envelope_hash=envelope_hash,
                access_class="restricted",
                preview_status="redacted",
                actor_ref="system:artifact",
            )
            obs.register_artifact_version(
                "artifact:report",
                version_id="artifact:report:v2",
                title="研究报告终稿",
                kind="deliverable",
                media_type="application/pdf",
                artifact_content_hash="b" * 64,
                size_bytes=120,
                storage_locator=f"artifact-store:{SECRET_LOCATOR}:v2",
                producer_invocation_ref="inv-1",
                result_envelope_ref="result-1",
                result_envelope_hash=envelope_hash,
                access_class="restricted",
                preview_status="redacted",
                actor_ref="system:artifact",
                prior_version_ref="artifact:report:v1",
            )
        finally:
            store.close()

    def _seed_connectors(self) -> dict[str, str]:
        clock = ConnectorClock()
        store = DaltonStore(self.core_path)
        connectors = ConnectorStore(store, clock=clock)
        try:
            profile_wire = connectors.register_profile(
                connector_profile_spec(), idempotency_key="dashboard:profile"
            )
            parameters = {"stock_code": "600309", "page": 1}
            call = connectors.register_call_spec(
                {
                    "schema_version": "0.1",
                    "id": "connector-call:dashboard",
                    "created_at": CONNECTOR_WHEN.isoformat(),
                    "work_order_ref": "work:connector:dashboard",
                    "work_order_hash": "e" * 64,
                    "connector_profile_ref": profile_wire["id"],
                    "operation": "list_announcements",
                    "parameters": parameters,
                    "query_hash": content_hash(
                        {"operation": "list_announcements", "parameters": parameters}
                    ),
                },
                idempotency_key="dashboard:call",
            )
            execution = ExecutionInvocation(
                schema_version="0.1",
                id="connector-invocation:dashboard",
                created_at=CONNECTOR_WHEN.isoformat(),
                kind=ExecutionKind.CONNECTOR,
                work_order_ref="work:connector:dashboard",
                profile_ref=profile_wire["id"],
                capability=profile_wire["capability_id"],
                input_refs=(call["id"],),
                output_refs=("artifact:connector:dashboard:raw",),
                started_at=CONNECTOR_WHEN.isoformat(),
                completed_at=None,
                side_effects=(),
                runtime_ref="runtime:connector-runner:0.1",
                actor_ref="runner:connector",
                environment_hash=profile_wire["runner_environment_hash"],
            )
            invocation_wire = connectors.register_invocation(
                {
                    "schema_version": "0.1",
                    "id": execution.id,
                    "created_at": CONNECTOR_WHEN.isoformat(),
                    "work_order_ref": execution.work_order_ref,
                    "work_order_hash": "e" * 64,
                    "connector_profile_ref": profile_wire["id"],
                    "connector_profile_hash": profile_wire["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "capability_lease_ref": "lease:connector:dashboard",
                    "capability_lease_hash": "f" * 64,
                    "descriptor_revision_ref": profile_wire["descriptor_revision_ref"],
                    "catalog_epoch": 1,
                    "logical_invocation_key": "connector-logical:" + content_hash(
                        {
                            "work_order_ref": execution.work_order_ref,
                            "work_order_hash": "e" * 64,
                            "connector_profile_hash": profile_wire["content_hash"],
                            "call_spec_hash": call["content_hash"],
                        }
                    ),
                },
                execution=execution,
                idempotency_key="dashboard:invocation",
            )
            zero_rate = connectors.register_price_rate(
                connector_price_rate_spec(
                    profile_wire["id"],
                    identifier="connector-price:dashboard:calls:v1",
                    rate_ref="connector-price:dashboard:calls",
                    meter="calls",
                    unit_quantity=1,
                    unit_price_micros=0,
                ),
                idempotency_key="dashboard:price",
            )
            policy = connectors.register_rate_policy(
                connector_rate_policy_spec(
                    profile_wire["id"],
                    price_rate_refs=(zero_rate["id"],),
                    required_price_meters=("calls",),
                ),
                idempotency_key="dashboard:policy",
            )

            reservation_one = connectors.reserve_quota(
                invocation_wire["id"],
                policy["id"],
                1,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 0},
                ttl_seconds=30,
                idempotency_key="dashboard:reserve:1",
            )
            first_started = clock.value.isoformat()
            clock.advance(1)
            attempt_one = connectors.record_physical_attempt(
                invocation_wire["id"],
                reservation_one["id"],
                1,
                "succeeded",
                started_at=first_started,
                completed_at=clock.value.isoformat(),
                provider_request_id=SECRET_CONNECTOR,
                idempotency_key="dashboard:attempt:1",
            )
            usage_one = connectors.record_usage(
                attempt_one["id"],
                {"calls": 1, "bytes": 80, "records": 1, "cost_micros": 0},
                measurement_status="partial",
                metering_source="runner_measured",
                provider_usage_ref=SECRET_CONNECTOR,
                idempotency_key="dashboard:usage:1:v1",
            )
            usage_one_latest = connectors.record_usage(
                attempt_one["id"],
                {"calls": 1, "bytes": 100, "records": 2, "cost_micros": 0},
                measurement_status="final",
                metering_source="provider_reported",
                correction_of_ref=usage_one["id"],
                idempotency_key="dashboard:usage:1:v2",
            )
            cost_one = connectors.record_cost(
                usage_one_latest["id"],
                price_rate_refs=[zero_rate["id"]],
                amount_micros=0,
                currency="USD",
                cost_status="actual",
                calculation_ref="calculator:dashboard",
                actor_ref="system:cost",
                idempotency_key="dashboard:cost:1",
            )
            settlement_one = connectors.settle_quota(
                reservation_one["id"],
                "consumed",
                {"calls": 1, "bytes": 100, "records": 2, "cost_micros": 0},
                usage_entry_ref=usage_one_latest["id"],
                cost_entry_ref=cost_one["id"],
                idempotency_key="dashboard:settlement:1",
            )

            reservation_two = connectors.reserve_quota(
                invocation_wire["id"],
                policy["id"],
                2,
                {"calls": 1, "bytes": 1000, "records": 10, "cost_micros": 0},
                ttl_seconds=30,
                idempotency_key="dashboard:reserve:2",
            )
            second_started = clock.value.isoformat()
            clock.advance(1)
            retry_at = (clock.value + timedelta(seconds=30)).isoformat()
            attempt_two = connectors.record_physical_attempt(
                invocation_wire["id"],
                reservation_two["id"],
                2,
                "rate_limited",
                started_at=second_started,
                completed_at=clock.value.isoformat(),
                retry_at=retry_at,
                provider_request_id=SECRET_CONNECTOR,
                idempotency_key="dashboard:attempt:2",
            )
            usage_two = connectors.record_usage(
                attempt_two["id"],
                {"calls": 1, "bytes": 0, "records": 0, "cost_micros": 0},
                measurement_status="estimated",
                metering_source="estimated",
                provider_usage_ref=SECRET_CONNECTOR,
                idempotency_key="dashboard:usage:2",
            )
            cost_two = connectors.record_cost(
                usage_two["id"],
                price_rate_refs=[zero_rate["id"]],
                amount_micros=0,
                currency="USD",
                cost_status="estimated",
                calculation_ref="calculator:dashboard",
                actor_ref="system:cost",
                idempotency_key="dashboard:cost:2",
            )
            settlement_two = connectors.settle_quota(
                reservation_two["id"],
                "indeterminate",
                {"calls": 1, "bytes": 0, "records": 0, "cost_micros": 0},
                usage_entry_ref=usage_two["id"],
                cost_entry_ref=cost_two["id"],
                idempotency_key="dashboard:settlement:2",
            )
            connectors.record_source_health(
                profile_wire["id"],
                "open_circuit",
                actor_ref="system:health",
                connector_invocation_ref=invocation_wire["id"],
                event_id="connector-health-event:z-open",
                idempotency_key="dashboard:health",
            )
            connectors.record_source_health(
                profile_wire["id"],
                "recovered",
                actor_ref="system:health",
                connector_invocation_ref=invocation_wire["id"],
                event_id="connector-health-event:a-recovered",
                idempotency_key="dashboard:health:recovered",
            )
            with patch("dalton_core.connector.uuid.uuid4") as uuid4:
                uuid4.side_effect = [
                    SimpleNamespace(hex="f" * 32),
                    SimpleNamespace(hex="0" * 32),
                ]
                resolved_incident = connectors.open_incident(
                    profile_wire["id"],
                    "schema_drift",
                    "warning",
                    {"private_detail": SECRET_CONNECTOR},
                    actor_ref="system:health",
                    connector_invocation_ref=invocation_wire["id"],
                    incident_id="connector-incident:dashboard:resolved",
                    idempotency_key="dashboard:incident:resolved:open",
                )
                connectors.resolve_incident(
                    resolved_incident["id"],
                    actor_ref="system:health",
                    idempotency_key="dashboard:incident:resolved:close",
                )
            incident = connectors.open_incident(
                profile_wire["id"],
                "source_outage",
                "blocking",
                {"private_detail": SECRET_CONNECTOR},
                actor_ref="system:health",
                connector_invocation_ref=invocation_wire["id"],
                reservation_ref=reservation_two["id"],
                idempotency_key="dashboard:incident",
            )
            return {
                "profile": profile_wire["id"],
                "attempt_one": attempt_one["id"],
                "attempt_two": attempt_two["id"],
                "usage_one_latest": usage_one_latest["id"],
                "settlement_one": settlement_one["id"],
                "settlement_two": settlement_two["id"],
                "retry_at": attempt_two["retry_at"],
                "incident": incident["id"],
                "resolved_incident": resolved_incident["id"],
            }
        finally:
            store.close()

    def projector(self) -> DashboardProjector:
        return DashboardProjector(
            self.core_path,
            self.scheduler_path,
            clock=self.clock,
        )

    def test_end_to_end_latest_chains_tree_and_expired_lease(self) -> None:
        snapshot = self.projector().project(self.projection_path)
        self.assertTrue(snapshot["metadata"]["partial_data"])
        self.assertRegex(snapshot["metadata"]["source_watermark"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(any("尚未 sweep" in item for item in snapshot["metadata"]["warnings"]))
        self.assertTrue(any("尚未定价" in item for item in snapshot["metadata"]["warnings"]))

        work = {row["work_order_ref"]: row for row in snapshot["work_items"]}
        self.assertEqual(work["work-1"]["display_status"], "已完成")
        self.assertEqual(work["work-2"]["display_status"], "等待调度回收")
        self.assertEqual(work["work-2"]["source_state"], "leased")
        self.assertEqual(work["work-2"]["parent_work_order_ref"], "work-1")
        self.assertEqual(work["work-2"]["sequence"], 1)
        self.assertEqual(work["work-2"]["latest_result_ref"], None)
        self.assertEqual(snapshot["workflow_summaries"][0]["running_tasks"], 0)
        self.assertEqual(snapshot["workflow_summaries"][0]["display_status"], "等待调度回收")

        invocations = {row["invocation_ref"]: row for row in snapshot["invocation_slices"]}
        self.assertEqual(invocations["inv-2"]["total_tokens"], 10)
        self.assertEqual(invocations["inv-2"]["metering_source"], "provider_reported")
        self.assertNotEqual(invocations["inv-2"]["total_tokens"], 9)
        artifacts = snapshot["artifact_index"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["title"], "研究报告终稿")
        self.assertEqual(artifacts[0]["content_hash"], "b" * 64)

    def test_latest_artifact_projection_reads_v2_generation(self) -> None:
        store = DaltonStore(self.core_path)
        obs = ObservabilityStore(store)
        try:
            envelope_hash = content_hash(result_envelope())
            obs.register_artifact_version_v2(
                "artifact:report",
                version_id="artifact:report:v3",
                title="研究报告 v0.2",
                kind="deliverable",
                media_type="application/pdf",
                artifact_content_hash="c" * 64,
                size_bytes=130,
                storage_locator=f"artifact-store:{SECRET_LOCATOR}:v3",
                producer_execution_ref="inv-1",
                result_envelope_ref="result-1",
                result_envelope_hash=envelope_hash,
                access_class="restricted",
                preview_status="redacted",
                actor_ref="system:artifact",
                prior_version_ref="artifact:report:v2",
            )
        finally:
            store.close()

        snapshot = self.projector().build_snapshot()
        self.assertEqual(len(snapshot["artifact_index"]), 1)
        self.assertEqual(snapshot["artifact_index"][0]["title"], "研究报告 v0.2")
        self.assertEqual(snapshot["artifact_index"][0]["content_hash"], "c" * 64)

    def test_currency_unpriced_and_sensitive_fields_do_not_leak(self) -> None:
        snapshot = self.projector().project(self.projection_path)
        costs = {row["cost_entry_ref"]: row for row in snapshot["cost_slices"]}
        self.assertEqual(costs["cost-usd"]["currency"], "USD")
        self.assertEqual(costs["cost-usd"]["amount_micros"], 4)
        self.assertEqual(costs["cost-eur"]["currency"], "EUR")
        self.assertEqual(costs["cost-eur"]["amount_micros"], 6)
        self.assertIsNone(costs["cost-unpriced"]["amount_micros"])

        service = DashboardQueryService(self.projection_path)
        try:
            summary = service.summary()["data"]
            grouped = {row["currency"]: row for row in summary["costs"]}
            self.assertEqual(grouped["EUR"]["amount_micros"], 6)
            self.assertEqual(grouped["USD"]["amount_micros"], 4)
            self.assertEqual(grouped["USD"]["unpriced_entries"], 1)
        finally:
            service.close()

        serialized = json.dumps(snapshot, ensure_ascii=False)
        projected_bytes = self.projection_path.read_bytes()
        for secret in (SECRET_PROMPT, SECRET_USAGE, SECRET_LOCATOR, SECRET_OUTPUT):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret.encode(), projected_bytes)
        self.assertNotIn("raw_usage", serialized)
        self.assertNotIn("storage_locator", serialized)
        self.assertNotIn("outputs", serialized)
        self.assertNotIn("credential", serialized.lower())

    def test_agenda_supervision_projects_delivery_and_feedback_without_subject_ids(self) -> None:
        store = DaltonStore(self.core_path)
        agenda = AgendaStore(store)
        try:
            policy = agenda.create_policy(
                {
                    "schema_version": "0.1", "enabled": True, "selected_count": 1,
                    "max_model_calls_per_cycle": 1, "max_daily_cycles": 1,
                    "max_daily_cost_usd": 0.5, "max_monthly_cost_usd": 10.0,
                    "max_input_tokens": 8000, "max_output_tokens": 2000,
                    "feature_weights": {"mandate_relevance": 4, "catalyst_urgency": 3, "evidence_staleness": 2, "decision_impact": 4},
                    "trial_company_refs": ["wanhua"], "cutover_enabled": False,
                    "cutover_acceptance_threshold": None,
                },
                effective_from=START.isoformat(), effective_until=None,
                actor_ref="human:owner", version_id="agenda-policy:test",
                idempotency_key="agenda-policy:test",
            )
            mandate = agenda.create_mandate(
                "mandate:test", objective="Find the best question", scope_refs=["wanhua"],
                constraints={"mode": "shadow"}, success_criteria={"feedback": True},
                effective_from=START.isoformat(), effective_until=None,
                actor_ref="human:owner", version_id="mandate:test:v1",
                idempotency_key="mandate:test",
            )
            cycle = agenda.start_cycle(
                "agenda:test:wanhua", perception_snapshot_ref="perception:test",
                perception_snapshot_hash="a" * 64, mandate_version_ref=mandate["id"],
                policy_version_ref=policy["id"], company_ref="wanhua", actor_ref="core",
                cycle_id="agenda-cycle:test", idempotency_key="agenda-cycle:test",
            )
            agenda.add_candidates(
                cycle["cycle_id"], actor_ref="core", idempotency_key="agenda-candidate:test",
                candidates=[{
                    "candidate_id": "agenda-candidate:test", "company_ref": "wanhua",
                    "question": "价格变化是否影响盈利？", "answer_criteria": "核对价格和成本",
                    "features": {"mandate_relevance": 3, "catalyst_urgency": 2, "evidence_staleness": 1, "decision_impact": 3},
                    "rationale": "重要", "source_refs": ["evidence:test"],
                }],
            )
            decision = agenda.decide_cycle(
                cycle["cycle_id"], actor_ref="core", decision_id="agenda-decision:test",
                idempotency_key="agenda-decision:test",
            )
            claim = agenda.claim_outbox(
                endpoint_ref="openclaw:discord:test", actor_ref="core",
                idempotency_key="agenda-claim:test", now=START.isoformat(),
            )["claims"][0]
            agenda.record_delivery(
                claim["message_id"], state="delivered",
                delivery_attempt_id=claim["delivery_attempt_id"],
                delivery_receipt_id="discord:123", actor_ref="core",
                idempotency_key="agenda-delivery:test",
            )
            agenda.record_feedback(
                decision["id"], verdict="agree", notes="Discord reaction ✅",
                subject_ref="human:discord-932169512197955636",
                source="openclaw_discord_reaction",
                source_event_ref="discord-reaction:test", actor_ref="bridge:openclaw-discord",
                feedback_id="agenda-feedback:test", idempotency_key="agenda-feedback:test",
            )
            agenda.record_feedback(
                decision["id"], verdict="agree", notes="timeout",
                subject_ref="automation:timeout", source="auto_accept_timeout",
                source_event_ref="agenda-timeout:test",
                actor_ref="automation:agenda-timeout",
                feedback_id="agenda-feedback:auto", idempotency_key="agenda-feedback:auto",
            )
        finally:
            store.close()
        snapshot = self.projector().project(self.projection_path)
        self.assertEqual(snapshot["agenda_supervision"][0]["delivered_cards"], 1)
        self.assertEqual(snapshot["agenda_supervision"][0]["agreement_rate"], 1.0)
        self.assertEqual(snapshot["agenda_supervision"][0]["labeled_decisions"], 1)
        self.assertEqual(snapshot["agenda_supervision"][0]["auto_accepted_decisions"], 1)
        self.assertEqual(snapshot["agenda_cycle_summaries"][0]["feedback_state"], "agree")
        self.assertEqual(snapshot["agenda_cycle_summaries"][0]["auto_accept_count"], 1)
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("932169512197955636", serialized)

    def test_projector_opens_sources_read_only_and_watermark_changes_with_authority(self) -> None:
        before_core = hashlib.sha256(self.core_path.read_bytes()).hexdigest()
        before_scheduler = hashlib.sha256(self.scheduler_path.read_bytes()).hexdigest()
        first = self.projector().build_snapshot()
        self.assertEqual(before_core, hashlib.sha256(self.core_path.read_bytes()).hexdigest())
        self.assertEqual(
            before_scheduler, hashlib.sha256(self.scheduler_path.read_bytes()).hexdigest()
        )
        second = self.projector().build_snapshot()
        self.assertEqual(
            first["metadata"]["source_watermark"],
            second["metadata"]["source_watermark"],
        )
        self.assertTrue(any("Capability Catalog" in item for item in first["metadata"]["warnings"]))
        self.assertTrue(any("Model Router" in item for item in first["metadata"]["warnings"]))
        self.assertTrue(first["capability_status"])
        self.assertTrue(first["model_status"])
        self.assertEqual(first["model_status"][0]["auth_state"], "unknown")

    def test_optional_catalog_and_router_project_only_safe_current_fields(self) -> None:
        catalog_path = Path(self.temp.name) / "catalog.sqlite"
        router_path = Path(self.temp.name) / "router.sqlite"
        authorities = FakeAuthorities()
        with CapabilityCatalog(
            catalog_path,
            clock=self.clock,
            approval_resolver=authorities.approval,
            policy_resolver=authorities.policy,
        ) as catalog:
            descriptor = catalog.publish(descriptor_spec())
        with ModelRouter(router_path, clock=self.clock) as router:
            router.register_profile(profile("dashboard", model="model-a"))
        snapshot = DashboardProjector(
            self.core_path,
            self.scheduler_path,
            capability_catalog_db=catalog_path,
            model_router_db=router_path,
            clock=self.clock,
        ).build_snapshot()
        capability = snapshot["capability_status"][0]
        self.assertEqual(capability["active_revision_ref"], descriptor.revision_ref)
        self.assertEqual(capability["eligibility_state"], "ready")
        model = snapshot["model_status"][0]
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model"], "model-a")
        self.assertEqual(model["auth_state"], "unknown")
        serialized = json.dumps(snapshot, ensure_ascii=False).lower()
        self.assertNotIn("credential_slot", serialized)
        self.assertNotIn("adapter_ref", serialized)

    def test_connector_and_metadata_source_projection_is_latest_safe_and_read_only(self) -> None:
        connector_refs = self._seed_connectors()
        catalog_path = Path(self.temp.name) / "metadata-catalog.sqlite"
        authorities = MetadataAuthorities()
        first_snapshot = make_metadata_snapshot()
        with CapabilityCatalog(
            catalog_path,
            clock=lambda: METADATA_NOW,
            source_registration_resolver=authorities.source_registration,
        ) as catalog:
            catalog.register_external_source("openclaw-source:main")
            importer = OpenClawMetadataImporter(catalog, clock=lambda: METADATA_NOW)
            importer.import_snapshot(first_snapshot)
            gap = make_metadata_snapshot(
                snapshot_id="openclaw-snapshot:dashboard-gap",
                catalog_generation=3,
                prior_snapshot=first_snapshot,
            )
            with self.assertRaises(ExternalSnapshotRejected):
                importer.import_snapshot(gap)

        core_before = hashlib.sha256(self.core_path.read_bytes()).hexdigest()
        catalog_before = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        snapshot = DashboardProjector(
            self.core_path,
            self.scheduler_path,
            capability_catalog_db=catalog_path,
            clock=lambda: METADATA_NOW,
        ).project(self.projection_path)
        repeat_path = Path(self.temp.name) / "dashboard-repeat.sqlite"
        repeat_snapshot = DashboardProjector(
            self.core_path,
            self.scheduler_path,
            capability_catalog_db=catalog_path,
            clock=lambda: METADATA_NOW,
        ).project(repeat_path)
        self.assertEqual(snapshot, repeat_snapshot)
        self.assertEqual(
            hashlib.sha256(self.projection_path.read_bytes()).hexdigest(),
            hashlib.sha256(repeat_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(core_before, hashlib.sha256(self.core_path.read_bytes()).hexdigest())
        self.assertEqual(
            catalog_before, hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        )

        metadata = snapshot["metadata_source_status"]
        self.assertEqual(1, len(metadata))
        self.assertTrue(metadata[0]["active"])
        self.assertEqual(1, metadata[0]["catalog_generation"])
        self.assertEqual("gap", metadata[0]["latest_ingest_outcome"])
        self.assertEqual("gap", metadata[0]["latest_reject_outcome"])

        operation = snapshot["connector_operation_status"][0]
        self.assertEqual("list_announcements", operation["operation"])
        self.assertEqual("cninfo", operation["source_ref"])
        self.assertEqual("recovered", operation["health_state"])
        self.assertEqual("recovering", operation["circuit_state"])
        self.assertEqual(1, operation["open_blocking_incidents"])

        attempts = {
            row["physical_attempt_ref"]: row
            for row in snapshot["connector_attempt_slices"]
        }
        first = attempts[connector_refs["attempt_one"]]
        self.assertEqual(2, first["usage_revision"])
        self.assertEqual(connector_refs["usage_one_latest"], first["usage_entry_ref"])
        self.assertEqual(100, first["usage_bytes"])
        self.assertEqual(2, first["usage_records"])
        self.assertEqual("actual", first["cost_status"])
        self.assertEqual("consumed", first["settlement_state"])
        second = attempts[connector_refs["attempt_two"]]
        self.assertEqual("rate_limited", second["outcome"])
        self.assertEqual(connector_refs["retry_at"], second["retry_at"])
        self.assertEqual("estimated", second["measurement_status"])
        self.assertEqual("indeterminate", second["settlement_state"])

        quota = snapshot["connector_quota_windows"][0]
        self.assertEqual(2, quota["reservations"])
        self.assertEqual(2, quota["reserved_calls"])
        self.assertEqual(1, quota["consumed_reservations"])
        self.assertEqual(1, quota["indeterminate_reservations"])
        self.assertEqual(1, quota["consumed_calls"])
        self.assertEqual(1, quota["indeterminate_calls"])
        incidents = {
            row["incident_ref"]: row
            for row in snapshot["connector_incident_status"]
        }
        incident = incidents[connector_refs["incident"]]
        self.assertEqual(connector_refs["incident"], incident["incident_ref"])
        self.assertEqual("blocking", incident["severity"])
        self.assertEqual("opened", incident["state"])
        self.assertEqual(
            "resolved", incidents[connector_refs["resolved_incident"]]["state"]
        )

        service = DashboardQueryService(self.projection_path)
        try:
            connector_api = service.connectors()["data"]
            source_api = service.metadata_sources()["data"]
            self.assertEqual(1, len(connector_api["blocking_incidents"]))
            self.assertEqual("gap", source_api[0]["latest_reject_outcome"])
        finally:
            service.close()

        serialized = json.dumps(snapshot, ensure_ascii=False).lower()
        projected_bytes = self.projection_path.read_bytes().lower()
        self.assertNotIn(SECRET_CONNECTOR.lower(), serialized)
        self.assertNotIn(SECRET_CONNECTOR.lower().encode(), projected_bytes)
        for forbidden in (
            "record_json", "reserved_json", "actual_json", "provider_request_id",
            "provider_usage_ref", "private_detail", "credential_slot_refs",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_missing_required_authority_tables_fail_closed(self) -> None:
        empty = Path(self.temp.name) / "empty.sqlite"

        sqlite3.connect(empty).close()
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(empty, self.scheduler_path, clock=self.clock).build_snapshot()
        with self.assertRaises(DashboardProjectorError):
            self.projector().project(self.core_path)
        hardlink = Path(self.temp.name) / "core-hardlink.sqlite"
        os.link(self.core_path, hardlink)
        with self.assertRaises(DashboardProjectorError):
            self.projector().project(hardlink)

    def test_connector_authority_absence_is_compatible_but_partial_schema_fails_closed(
        self,
    ) -> None:
        connector_tables = (
            "connector_profile_versions",
            "connector_call_specs",
            "connector_invocations",
            "connector_rate_policy_versions",
            "connector_rate_policy_activation_events",
            "connector_quota_reservations",
            "connector_physical_attempts",
            "connector_usage_entries",
            "connector_price_rate_versions",
            "connector_cost_entries",
            "connector_quota_settlements",
            "connector_source_envelopes",
            "connector_incidents",
            "connector_incident_events",
            "connector_source_health_events",
            "connector_idempotency_keys",
        )

        def copy_core(name: str) -> Path:
            target = Path(self.temp.name) / name
            source = sqlite3.connect(self.core_path)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            return target

        def drop_tables(path: Path, tables: tuple[str, ...]) -> None:
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                for table in tables:
                    connection.execute(f"DROP TABLE {table}")
                connection.commit()
            finally:
                connection.close()

        def add_connector_schema(path: Path) -> None:
            store = DaltonStore(path)
            try:
                ConnectorStore(store)
            finally:
                store.close()

        old_core = copy_core("core-before-connectors.sqlite")
        old_snapshot = DashboardProjector(
            old_core, self.scheduler_path, clock=self.clock
        ).build_snapshot()
        self.assertEqual([], old_snapshot["connector_operation_status"])
        self.assertTrue(
            any("Connector authority" in item for item in old_snapshot["metadata"]["warnings"])
        )

        only_unprojected = copy_core("core-only-source-envelope.sqlite")
        add_connector_schema(only_unprojected)
        drop_tables(
            only_unprojected,
            tuple(table for table in connector_tables if table != "connector_source_envelopes"),
        )
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(
                only_unprojected, self.scheduler_path, clock=self.clock
            ).build_snapshot()

        missing_unprojected = copy_core("core-missing-source-envelope.sqlite")
        add_connector_schema(missing_unprojected)
        drop_tables(missing_unprojected, ("connector_source_envelopes",))
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(
                missing_unprojected, self.scheduler_path, clock=self.clock
            ).build_snapshot()

    def test_partial_metadata_source_schema_cannot_hide_behind_missing_catalog_anchor(
        self,
    ) -> None:
        partial_catalog = Path(self.temp.name) / "partial-catalog.sqlite"
        connection = sqlite3.connect(partial_catalog)
        try:
            connection.execute(
                "CREATE TABLE external_capability_source_heads "
                "(source_instance_ref TEXT PRIMARY KEY)"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(
                self.core_path,
                self.scheduler_path,
                capability_catalog_db=partial_catalog,
                clock=self.clock,
            ).build_snapshot()

    def test_metadata_source_authority_absence_is_legacy_but_chain_partial_fails_closed(
        self,
    ) -> None:
        authority_tables = (
            "external_capability_source_registrations",
            "external_capability_active_source",
            "external_capability_snapshot_chains",
            "external_capability_source_heads",
            "external_capability_snapshot_ingest_events",
        )
        complete = Path(self.temp.name) / "complete-metadata-catalog.sqlite"
        with CapabilityCatalog(complete, clock=self.clock):
            pass

        def copy_catalog(name: str) -> Path:
            target = Path(self.temp.name) / name
            source = sqlite3.connect(complete)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            return target

        def drop_tables(path: Path, tables: tuple[str, ...]) -> None:
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                for table in tables:
                    connection.execute(f"DROP TABLE {table}")
                connection.commit()
            finally:
                connection.close()

        legacy = copy_catalog("legacy-p03-catalog.sqlite")
        drop_tables(legacy, authority_tables)
        legacy_snapshot = DashboardProjector(
            self.core_path,
            self.scheduler_path,
            capability_catalog_db=legacy,
            clock=self.clock,
        ).build_snapshot()
        self.assertEqual([], legacy_snapshot["metadata_source_status"])
        self.assertTrue(
            any("wire 0.2" in item for item in legacy_snapshot["metadata"]["warnings"])
        )

        only_chain = copy_catalog("catalog-only-p04-chain.sqlite")
        drop_tables(
            only_chain,
            tuple(
                table
                for table in authority_tables
                if table != "external_capability_snapshot_chains"
            ),
        )
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(
                self.core_path,
                self.scheduler_path,
                capability_catalog_db=only_chain,
                clock=self.clock,
            ).build_snapshot()

        missing_chain = copy_catalog("catalog-missing-p04-chain.sqlite")
        drop_tables(missing_chain, ("external_capability_snapshot_chains",))
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(
                self.core_path,
                self.scheduler_path,
                capability_catalog_db=missing_chain,
                clock=self.clock,
            ).build_snapshot()

    def test_projection_replaces_hardlink_swapped_after_alias_check(self) -> None:
        projector = self.projector()
        original_build = projector.build_snapshot
        core_before = hashlib.sha256(self.core_path.read_bytes()).hexdigest()

        def build_and_swap_destination() -> dict:
            snapshot = original_build()
            os.link(self.core_path, self.projection_path)
            return snapshot

        projector.build_snapshot = build_and_swap_destination  # type: ignore[method-assign]
        projector.project(self.projection_path)

        self.assertEqual(
            hashlib.sha256(self.core_path.read_bytes()).hexdigest(), core_before
        )
        self.assertFalse(os.path.samefile(self.core_path, self.projection_path))
        dashboard = DashboardQueryService(self.projection_path)
        try:
            self.assertEqual(dashboard.status()["data"]["build_state"], "ready")
        finally:
            dashboard.close()


if __name__ == "__main__":
    unittest.main()
