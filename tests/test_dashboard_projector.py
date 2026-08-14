from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.dashboard import DashboardQueryService
from dalton_core.agenda import AgendaStore
from dalton_core.dashboard_projector import (
    DashboardProjector,
    DashboardProjectorError,
    ProjectionSourceError,
)
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_model_router import profile


START = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
SECRET_PROMPT = "SECRET-PROMPT-DO-NOT-PROJECT"
SECRET_USAGE = "SECRET-USAGE-CREDENTIAL"
SECRET_LOCATOR = "SECRET-ARTIFACT-LOCATOR"
SECRET_OUTPUT = "SECRET-FULL-OUTPUT"


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

    def test_missing_required_authority_tables_fail_closed(self) -> None:
        empty = Path(self.temp.name) / "empty.sqlite"
        import sqlite3

        sqlite3.connect(empty).close()
        with self.assertRaises(ProjectionSourceError):
            DashboardProjector(empty, self.scheduler_path, clock=self.clock).build_snapshot()
        with self.assertRaises(DashboardProjectorError):
            self.projector().project(self.core_path)
        hardlink = Path(self.temp.name) / "core-hardlink.sqlite"
        os.link(self.core_path, hardlink)
        with self.assertRaises(DashboardProjectorError):
            self.projector().project(hardlink)

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
