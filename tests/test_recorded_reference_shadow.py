from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_connector_adapter_request,
    validate_runner_environment_manifest,
)
from dalton_core.contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.recorded_reference_shadow import (
    RecordedReferenceShadowCoordinator,
    RecordedReferenceShadowError,
    validate_recorded_reference_shadow_plan,
)
from dalton_core.recorded_source_adapter import (
    RecordedSourceFixtureAdapter,
    build_recorded_source_fixtures,
    load_recorded_source_fixture,
    validate_recorded_source_fixture,
)
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.test_capability_catalog import FakeAuthorities, descriptor_spec
from tests.test_connector import (
    MutableClock,
    price_rate_spec,
    rate_policy_spec,
    validate_json_schema,
)
from tests.test_connector_runner import permissions


ROOT = Path(__file__).resolve().parents[1]


def with_hash(wire: dict) -> dict:
    result = copy.deepcopy(wire)
    result["content_hash"] = content_hash(result)
    return result


def _input_validator(source: str):
    def validate(parameters: dict) -> bool:
        if source == "cninfo":
            return (
                set(parameters)
                == {"stock_code", "date_from", "date_to", "page", "page_size"}
                and all(
                    isinstance(parameters[name], str) and parameters[name]
                    for name in ("stock_code", "date_from", "date_to")
                )
                and all(
                    isinstance(parameters[name], int)
                    and not isinstance(parameters[name], bool)
                    and parameters[name] >= 1
                    for name in ("page", "page_size")
                )
            )
        return (
            set(parameters) in (
                {"issuer", "form", "date_from", "date_to", "limit"},
                {"issuer", "form", "date_from", "date_to", "limit", "cursor"},
            )
            and all(
                isinstance(parameters[name], str) and parameters[name]
                for name in ("issuer", "form", "date_from", "date_to")
            )
            and isinstance(parameters["limit"], int)
            and not isinstance(parameters["limit"], bool)
            and parameters["limit"] >= 1
            and (
                "cursor" not in parameters
                or parameters["cursor"] is None
                or isinstance(parameters["cursor"], str)
            )
        )

    return validate


class CapturingRecordedSourceFixtureAdapter(RecordedSourceFixtureAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []

    def __call__(self, request, raw_sink):
        self.requests.append(copy.deepcopy(dict(request)))
        return super().__call__(request, raw_sink)


class ReferenceShadowHarness:
    def __init__(
        self,
        source: str,
        scenario: str,
        *,
        max_pages: int = 2,
        fault_at: str | None = None,
    ):
        self.source = source
        self.scenario = scenario
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, default_lease_seconds=30,
            max_lease_seconds=60,
        )
        self.authorities = FakeAuthorities()
        self.authorities.policy_permissions = permissions()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.temp = tempfile.TemporaryDirectory()
        inventory = load_packaged_connector_inventory()
        self.template = inventory["templates"][source]
        self.operation = "list_announcements" if source == "cninfo" else "list_filings"
        operation = next(
            item for item in self.template["operations"]
            if item["operation"] == self.operation
        )
        self.fixture = load_recorded_source_fixture(source)
        self.adapter = CapturingRecordedSourceFixtureAdapter(
            self.fixture, scenario=scenario, advance_clock=self.clock.advance
        )
        capability_id = f"capability:dalton:connector:{source}-recorded"
        adapter_ref = f"adapter:recorded-reference-shadow:{source}:0.1"
        adapter_hash = content_hash(
            {"adapter_ref": adapter_ref, "fixture_hash": self.fixture["content_hash"]}
        )
        schema_hash = content_hash(
            {
                "allowed_operations": [self.operation],
                "input_schema_refs": {self.operation: operation["input_schema_ref"]},
                "input_schema_hashes": {self.operation: operation["input_schema_hash"]},
                "output_schema_refs": {self.operation: operation["output_schema_ref"]},
                "output_schema_hashes": {self.operation: operation["output_schema_hash"]},
            }
        )
        source_hash = content_hash(self.template["source_identity"])
        descriptor_wire = descriptor_spec(
            capability_id,
            name=f"recorded-{source}-reference-shadow",
            side_effects=["read:recorded-fixture"],
        )
        descriptor_wire.update(
            {
                "kind": "connector",
                "label": f"Recorded {source} reference shadow",
                "summary": "Replay a bounded official-source fixture without network",
                "contract": {
                    "mode": "typed_call",
                    "input_schema_ref": operation["input_schema_ref"],
                    "output_schema_ref": operation["output_schema_ref"],
                    "instruction_ref": None,
                    "adapter_ref": adapter_ref,
                },
                "permissions": permissions(),
                "source_hash": source_hash,
                "schema_hash": schema_hash,
            }
        )
        self.descriptor = self.catalog.publish(descriptor_wire)
        binding = {
            "binding_ref": f"runner-binding:{source}-recorded:v1",
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": adapter_ref,
            "adapter_hash": adapter_hash,
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": source_hash,
            "operation": self.operation,
            "input_schema_ref": operation["input_schema_ref"],
            "input_schema_hash": operation["input_schema_hash"],
            "output_schema_ref": operation["output_schema_ref"],
            "output_schema_hash": operation["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": permissions(),
            "side_effects": ["read:recorded-fixture"],
            "rate_policy_ref": "connector-rate-policy:a-share",
        }
        manifest = {
            "schema_version": "0.1",
            "id": f"runner-environment:{source}-recorded:v1",
            "created_at": self.clock().isoformat(timespec="microseconds"),
            "runner_runtime_ref": "runtime:connector-runner:0.1",
            "runner_actor_ref": "runner:connector",
            "resolver_ref": "resolver:connector-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": f"artifact:runner-packages:{source}-recorded:v1",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
        }
        self.manifest = validate_runner_environment_manifest(with_hash(manifest))
        resolver = StaticAdapterResolver(
            self.manifest,
            {binding["binding_ref"]: self.adapter},
            {binding["binding_ref"]: _input_validator(source)},
        )
        profile = {
            "schema_version": "0.1",
            "id": f"connector-profile:{source}-recorded:v1",
            "created_at": self.clock().isoformat(),
            "connector_ref": self.template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": capability_id,
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "source_identity": self.template["source_identity"],
            "source_hash": source_hash,
            "schema_hash": schema_hash,
            "catalog_epoch": self.descriptor.catalog_epoch,
            "adapter_ref": adapter_ref,
            "adapter_hash": adapter_hash,
            "runner_runtime_ref": "runtime:connector-runner:0.1",
            "runner_actor_ref": "runner:connector",
            "runner_environment_hash": self.manifest["content_hash"],
            "allowed_operations": [self.operation],
            "allowed_hosts": self.template["transport"]["allowed_hosts"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "input_schema_refs": {self.operation: operation["input_schema_ref"]},
            "input_schema_hashes": {self.operation: operation["input_schema_hash"]},
            "output_schema_refs": {self.operation: operation["output_schema_ref"]},
            "output_schema_hashes": {self.operation: operation["output_schema_hash"]},
            "pagination": {
                key: operation["pagination"][key]
                for key in ("mode", "cursor_field", "max_pages")
            },
            "completeness": {self.operation: "enumerated"},
            "max_response_bytes": 1_000_000,
            "max_records": 1000,
            "timeout_ms": 5_000,
            "access_policy_ref": "policy:access:public",
            "retention_policy_ref": "policy:retention:filing",
            "terms_policy_ref": f"policy:terms:{source}",
            "network_policy": {
                "allowed_schemes": ["https"],
                "allow_redirects": False,
                "max_redirects": 0,
                "resolve_public_only": True,
            },
        }
        self.profile = self.connectors.register_profile(
            profile, idempotency_key=f"shadow:{source}:profile"
        )
        self.call_id = f"connector-call:{source}:reference-shadow"
        self.work = WorkOrder(
            schema_version="0.1",
            id=f"work:{source}:reference-shadow:1",
            created_at=self.clock().isoformat(),
            updated_at=self.clock().isoformat(),
            question=f"Replay bounded {source} reference fixture",
            requested_capabilities=(capability_id,),
            runtime_profile_ref=self.profile["runner_runtime_ref"],
            budget={"max_seconds": 25},
            idempotency_key=f"work:{source}:reference-shadow:1",
            declared_side_effects=("read:recorded-fixture",),
            status="ready",
            input_refs=(self.call_id,),
        )
        self.work_hash = content_hash(self.work.to_dict())
        policy = self.authorities.policy({"policy_ref": "policy:capability-v1"})
        self.capability_lease = self.catalog.prepare(
            self.work,
            capability_id=self.descriptor.id,
            revision_ref=self.descriptor.revision_ref,
            catalog_epoch=self.descriptor.catalog_epoch,
            descriptor_hash=self.descriptor.content_hash,
            source_hash=self.descriptor.source_hash,
            schema_hash=self.descriptor.schema_hash,
            policy_ref="policy:capability-v1",
            policy_hash=policy["content_hash"],
            principal_ref="principal:worker-1",
            visibility_scopes=["research"],
            ttl_seconds=60,
        )
        parameters = (
            {
                "stock_code": "600309", "date_from": "2026-01-01",
                "date_to": "2026-06-30", "page": 1, "page_size": 50,
            }
            if source == "cninfo"
            else {
                "issuer": "0000000001", "form": "10-Q",
                "date_from": "2026-01-01", "date_to": "2026-06-30",
                "limit": 50,
            }
        )
        self.call = self.connectors.register_call_spec(
            {
                "schema_version": "0.1", "id": self.call_id,
                "created_at": self.clock().isoformat(),
                "work_order_ref": self.work.id, "work_order_hash": self.work_hash,
                "connector_profile_ref": self.profile["id"],
                "operation": self.operation, "parameters": parameters,
                "query_hash": content_hash(
                    {"operation": self.operation, "parameters": parameters}
                ),
            },
            idempotency_key=f"shadow:{source}:call",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id=f"connector-invocation:{source}:reference-shadow:1",
            created_at=self.clock().isoformat(),
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=self.work.id,
            profile_ref=self.profile["id"],
            capability=capability_id,
            input_refs=(self.call["id"],),
            output_refs=(f"artifact:{source}:reference-shadow:raw",),
            started_at=self.clock().isoformat(),
            completed_at=None,
            side_effects=(),
            runtime_ref=self.profile["runner_runtime_ref"],
            actor_ref=self.profile["runner_actor_ref"],
            environment_hash=self.profile["runner_environment_hash"],
        )
        self.execution = execution
        self.invocation = self.connectors.register_invocation(
            {
                "schema_version": "0.1", "id": execution.id,
                "created_at": self.clock().isoformat(),
                "work_order_ref": self.work.id, "work_order_hash": self.work_hash,
                "connector_profile_ref": self.profile["id"],
                "connector_profile_hash": self.profile["content_hash"],
                "call_spec_ref": self.call["id"],
                "call_spec_hash": self.call["content_hash"],
                "capability_lease_ref": self.capability_lease.id,
                "capability_lease_hash": self.capability_lease.content_hash,
                "descriptor_revision_ref": self.descriptor.revision_ref,
                "catalog_epoch": self.descriptor.catalog_epoch,
                "logical_invocation_key": "connector-logical:" + content_hash(
                    {
                        "work_order_ref": self.work.id,
                        "work_order_hash": self.work_hash,
                        "connector_profile_hash": self.profile["content_hash"],
                        "call_spec_hash": self.call["content_hash"],
                    }
                ),
            },
            execution=execution,
            idempotency_key=f"shadow:{source}:invocation",
        )
        zero_rate = self.connectors.register_price_rate(
            price_rate_spec(
                self.profile["id"],
                identifier=f"connector-price:{source}:zero:v1",
                rate_ref=f"connector-price:{source}:zero",
                meter="calls", unit_quantity=1, unit_price_micros=0,
            ),
            idempotency_key=f"shadow:{source}:price",
        )
        self.connectors.register_rate_policy(
            rate_policy_spec(
                self.profile["id"],
                identifier=f"connector-rate-policy:{source}:v1",
                price_rate_refs=(zero_rate["id"],),
                required_price_meters=("calls",),
                limits={
                    "calls": 20, "bytes": 2_000_000,
                    "records": 20_000, "cost_micros": 1000,
                },
                max_concurrency=1,
            ),
            idempotency_key=f"shadow:{source}:rate-policy",
        )
        self.scheduler.enqueue(self.work)
        self.claim = self.scheduler.claim(
            self.profile["runner_actor_ref"], work_order_id=self.work.id
        )
        assert self.claim is not None
        self.gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler, catalog=self.catalog,
            connectors=self.connectors, resolver=resolver,
            visibility_scopes=["research"], clock=self.clock,
        )
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        authority = ConnectorAuthorityPort(
            connectors=self.connectors,
            observability=ObservabilityStore(self.core),
            scheduler=self.scheduler,
        )
        plan = {
            "schema_version": "0.1",
            "id": f"recorded-reference-shadow-plan:{source}:0.1",
            "created_at": self.clock().isoformat(timespec="microseconds"),
            "inventory_template_ref": self.template["id"],
            "inventory_template_hash": self.template["content_hash"],
            "recorded_fixture_ref": self.fixture["id"],
            "recorded_fixture_hash": self.fixture["content_hash"],
            "connector_profile_ref": self.profile["id"],
            "connector_profile_hash": self.profile["content_hash"],
            "source_ref": self.template["source_identity"]["source_ref"],
            "operation": self.operation,
            "pagination_mode": operation["pagination"]["mode"],
            "cursor_field": operation["pagination"]["cursor_field"],
            "max_pages": max_pages,
            "bounded_window_fields": ["date_from", "date_to"],
            "completeness_target": "enumerated",
        }
        self.plan = with_hash(plan)
        self.coordinator = RecordedReferenceShadowCoordinator(
            plan=self.plan, gate=self.gate, journal=self.journal,
            spool=self.spool, authority=authority,
            connector_reader=self.connectors, clock=self.clock,
            fault_hook=(
                None
                if fault_at is None
                else lambda barrier: (
                    (_ for _ in ()).throw(RuntimeError(barrier))
                    if barrier == fault_at
                    else None
                )
            ),
        )

    def request(self) -> dict:
        return with_hash(
            {
                "schema_version": "0.1",
                "id": f"connector-runner-request:{self.source}:shadow:1",
                "created_at": self.clock().isoformat(timespec="microseconds"),
                "connector_invocation_ref": self.invocation["id"],
                "connector_invocation_hash": self.invocation["content_hash"],
                "execution_ref": self.invocation["execution_ref"],
                "execution_hash": self.invocation["execution_hash"],
                "work_order_ref": self.work.id, "work_order_hash": self.work_hash,
                "scheduler_attempt_number": 1,
                "scheduler_lease_revision_ref": self.claim["lease"]["id"],
                "scheduler_lease_hash": self.claim["lease"]["content_hash"],
                "connector_profile_ref": self.profile["id"],
                "connector_profile_hash": self.profile["content_hash"],
                "call_spec_ref": self.call["id"],
                "call_spec_hash": self.call["content_hash"],
                "capability_lease_ref": self.capability_lease.id,
                "capability_lease_hash": self.capability_lease.content_hash,
                "principal_ref": "principal:worker-1",
                "runner_runtime_ref": self.profile["runner_runtime_ref"],
                "runner_actor_ref": self.profile["runner_actor_ref"],
                "runner_environment_hash": self.profile["runner_environment_hash"],
                "idempotency_key": f"runner-request:{self.source}:shadow:1",
            }
        )

    def execute(self) -> dict:
        return self.coordinator.execute(
            self.request(), scheduler_lease_token=self.claim["lease_token"]
        )

    def count(self, table: str, *, scheduler: bool = False) -> int:
        connection = self.scheduler.connection if scheduler else self.core.connection
        return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def close(self) -> None:
        self.temp.cleanup()
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class RecordedReferenceShadowTests(unittest.TestCase):
    def harness(
        self,
        source: str,
        scenario: str,
        *,
        max_pages: int = 2,
        fault_at: str | None = None,
    ) -> ReferenceShadowHarness:
        harness = ReferenceShadowHarness(
            source, scenario, max_pages=max_pages, fault_at=fault_at
        )
        self.addCleanup(harness.close)
        return harness

    def test_fixture_build_is_deterministic_and_packaged_exactly(self) -> None:
        built = build_recorded_source_fixtures()
        schema = json.loads(
            (ROOT / "contracts" / "recorded-source-fixture.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for source in ("cninfo", "sec"):
            validate_json_schema(built[source], schema, schema)
            self.assertEqual(load_recorded_source_fixture(source), built[source])
            self.assertEqual(
                json.loads(
                    (
                        ROOT / "src" / "dalton_core" / "reference_shadow_fixtures"
                        / f"{source}.json"
                    ).read_text(encoding="utf-8")
                ),
                built[source],
            )

    def test_cninfo_and_sec_pagination_record_every_page_and_replay_zero_rows(self) -> None:
        for source in ("cninfo", "sec"):
            with self.subTest(source=source):
                harness = self.harness(source, "pagination")
                response = harness.execute()
                self.assertEqual(response["outcome"], "succeeded")
                self.assertEqual(harness.count("connector_physical_attempts"), 2)
                self.assertEqual(harness.count("connector_usage_entries"), 2)
                self.assertEqual(harness.count("connector_cost_entries"), 2)
                self.assertEqual(harness.count("connector_quota_settlements"), 2)
                self.assertEqual(harness.count("observability_artifact_versions_v2"), 2)
                self.assertEqual(harness.count("connector_source_envelopes"), 1)
                source_wire = json.loads(
                    harness.core.connection.execute(
                        "SELECT record_json FROM connector_source_envelopes"
                    ).fetchone()["record_json"]
                )
                self.assertEqual(source_wire["completeness"], "enumerated")
                self.assertEqual(source_wire["status"], "complete")
                self.assertEqual(len(source_wire["physical_attempt_refs"]), 2)
                self.assertEqual(len(source_wire["source_record_refs"]), 2)
                artifacts = [
                    json.loads(row["record_json"])
                    for row in harness.core.connection.execute(
                        "SELECT record_json FROM observability_artifact_versions_v2 "
                        "ORDER BY version_number"
                    ).fetchall()
                ]
                self.assertIsNone(artifacts[0]["prior_version_ref"])
                self.assertEqual(artifacts[1]["prior_version_ref"], artifacts[0]["id"])
                attempts = [
                    json.loads(row["record_json"])
                    for row in harness.core.connection.execute(
                        "SELECT record_json FROM connector_physical_attempts "
                        "ORDER BY physical_attempt_number"
                    ).fetchall()
                ]
                self.assertEqual(
                    [item["physical_attempt_number"] for item in attempts], [1, 2]
                )
                self.assertEqual(len(harness.adapter.requests), 2)
                first_request, second_request = harness.adapter.requests
                self.assertEqual(
                    first_request["pagination_authority"]["page_ordinal"], 1
                )
                self.assertEqual(
                    second_request["pagination_authority"]["page_ordinal"], 2
                )
                self.assertEqual(
                    second_request["pagination_authority"]["prior_adapter_request_hash"],
                    first_request["content_hash"],
                )
                if source == "cninfo":
                    self.assertEqual(
                        [item["parameters"]["page"] for item in harness.adapter.requests],
                        [1, 2],
                    )
                else:
                    self.assertNotIn("cursor", first_request["parameters"])
                    self.assertEqual(second_request["parameters"]["cursor"], "cursor-2")
                counts = {
                    table: harness.count(table)
                    for table in (
                        "connector_physical_attempts", "connector_usage_entries",
                        "connector_cost_entries", "connector_quota_settlements",
                        "observability_artifact_versions_v2",
                        "connector_source_envelopes", "runner_attempt_journal_events",
                    )
                }
                duplicate = harness.execute()
                self.assertEqual(duplicate["idempotency_status"], "duplicate")
                self.assertEqual(
                    counts,
                    {table: harness.count(table) for table in counts},
                )

    def test_empty_and_bounded_partial_are_explicit(self) -> None:
        empty = self.harness("cninfo", "empty")
        self.assertEqual(empty.execute()["outcome"], "succeeded")
        source = json.loads(
            empty.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes"
            ).fetchone()["record_json"]
        )
        self.assertEqual((source["status"], source["source_record_refs"]), ("empty", []))

        partial = self.harness("sec", "partial", max_pages=1)
        self.assertEqual(partial.execute()["outcome"], "succeeded")
        source = json.loads(
            partial.core.connection.execute(
                "SELECT record_json FROM connector_source_envelopes"
            ).fetchone()["record_json"]
        )
        self.assertEqual((source["status"], source["completeness"]), ("partial", "partial"))

    def test_failure_matrix_never_fabricates_source_or_artifact(self) -> None:
        for scenario in ("schema_drift", "rate_limited", "timeout", "malformed"):
            with self.subTest(scenario=scenario):
                harness = self.harness("cninfo", scenario)
                response = harness.execute()
                self.assertEqual(response["outcome"], "retryable")
                self.assertIsNone(response["source_envelope_ref"])
                self.assertIsNone(response["raw_artifact_version_ref"])
                self.assertEqual(harness.count("connector_source_envelopes"), 0)
                self.assertEqual(harness.count("observability_artifact_versions_v2"), 0)
                self.assertEqual(harness.spool.total_bytes(), 0)

    def test_plan_fixture_and_window_drift_fail_closed_before_transport(self) -> None:
        harness = self.harness("sec", "success")
        for mutation in ("template_hash", "fixture_hash", "reversed_window"):
            with self.subTest(mutation=mutation):
                plan = copy.deepcopy(harness.plan)
                if mutation == "template_hash":
                    plan["inventory_template_hash"] = "0" * 64
                elif mutation == "fixture_hash":
                    plan["recorded_fixture_hash"] = "0" * 64
                else:
                    changed_call = copy.deepcopy(harness.call)
                    changed_call["parameters"]["date_from"] = "2027-01-01"
                    changed_call["parameters"]["date_to"] = "2026-01-01"
                    # The immutable CallSpec cannot be replaced; this branch proves the
                    # plan validator itself does not accept a reversed field declaration.
                    plan["bounded_window_fields"] = ["date_to", "date_from"]
                plan = with_hash({key: value for key, value in plan.items() if key != "content_hash"})
                with self.assertRaises(RecordedReferenceShadowError):
                    coordinator = RecordedReferenceShadowCoordinator(
                        plan=plan, gate=harness.gate, journal=harness.journal,
                        spool=harness.spool, authority=harness.coordinator.authority,
                        connector_reader=harness.connectors, clock=harness.clock,
                    )
                    coordinator.execute(
                        harness.request(),
                        scheduler_lease_token=harness.claim["lease_token"],
                    )
                self.assertEqual(harness.count("connector_physical_attempts"), 0)

    def test_fixture_validator_rejects_scenario_semantic_forks(self) -> None:
        fixture = build_recorded_source_fixtures()["cninfo"]
        for mutation in ("behavior", "error", "cursor", "identity"):
            changed = copy.deepcopy(fixture)
            if mutation == "behavior":
                changed["scenarios"][0]["behavior"] = "normalize_error"
            elif mutation == "error":
                changed["scenarios"][-1]["error_code"] = "schema_drift"
            elif mutation == "cursor":
                changed["scenarios"][2]["pages"][1]["request_cursor"] = "forged"
            else:
                changed["created_at"] = "/etc/passwd"
            changed = with_hash(
                {key: value for key, value in changed.items() if key != "content_hash"}
            )
            with self.subTest(mutation=mutation), self.assertRaises(Exception):
                validate_recorded_source_fixture(changed)

    def test_paginated_adapter_request_02_schema_and_hostile_mutations(self) -> None:
        harness = self.harness("cninfo", "success")
        admission = harness.gate.validate(
            harness.request(), scheduler_lease_token=harness.claim["lease_token"]
        )
        harness.gate.reserve_for_admission(
            admission,
            scheduler_lease_token=harness.claim["lease_token"],
            idempotency_key="shadow:adapter-request-v02:reserve",
        )
        base = harness.gate.build_adapter_request(
            admission, scheduler_lease_token=harness.claim["lease_token"]
        )
        request = harness.coordinator._paginated_adapter_request(  # noqa: SLF001
            base, admission, ordinal=1, prior_receipt=None
        )
        schema = json.loads(
            (ROOT / "contracts" / "connector-adapter-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validate_json_schema(request, schema, schema)
        self.assertEqual(request["protocol_version"], "0.2")
        for mutation in ("query_hash", "prior_hash", "ordinal"):
            changed = copy.deepcopy(request)
            if mutation == "query_hash":
                changed["query_hash"] = "0" * 64
            elif mutation == "prior_hash":
                changed["pagination_authority"]["prior_observation_hash"] = "1" * 64
            else:
                changed["pagination_authority"]["page_ordinal"] = 2
            changed = with_hash(
                {key: value for key, value in changed.items() if key != "content_hash"}
            )
            with self.subTest(mutation=mutation), self.assertRaises(Exception):
                validate_connector_adapter_request(changed)

    def test_shadow_does_not_write_research_beliefs(self) -> None:
        harness = self.harness("sec", "success")
        harness.execute()
        for table in ("evidence_versions", "claim_versions", "thesis_versions"):
            self.assertEqual(harness.count(table), 0)

    def test_response_journal_and_scheduler_completion_crash_windows_converge(self) -> None:
        for barrier in ("after_response_journaled", "after_scheduler_completed"):
            with self.subTest(barrier=barrier):
                harness = self.harness("cninfo", "success", fault_at=barrier)
                with self.assertRaisesRegex(RuntimeError, barrier):
                    harness.execute()
                before = {
                    table: harness.count(table)
                    for table in (
                        "connector_physical_attempts", "connector_usage_entries",
                        "connector_cost_entries", "connector_quota_settlements",
                        "observability_artifact_versions_v2",
                        "connector_source_envelopes", "runner_attempt_journal_events",
                    )
                }
                harness.coordinator.fault_hook = None
                response = harness.execute()
                self.assertEqual(response["idempotency_status"], "duplicate")
                self.assertEqual(
                    harness.count("scheduler_result_envelopes", scheduler=True), 1
                )
                self.assertEqual(
                    before,
                    {table: harness.count(table) for table in before},
                )


if __name__ == "__main__":
    unittest.main()
