from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Callable, Mapping
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
from dalton_core.contracts import ExecutionInvocation, ExecutionKind, ResultEnvelope, WorkOrder
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
from dalton_core.scheduler import (
    LeaseExpired,
    Scheduler,
    SchedulerConflict,
    SchedulerValidationError,
)
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


class HostileOutputAdapter(CapturingRecordedSourceFixtureAdapter):
    output_mutation: str

    def __call__(self, request, raw_sink):
        result = super().__call__(request, raw_sink)
        structured = result["structured_output"]
        if self.output_mutation == "extra":
            structured["unexpected"] = True
        elif self.output_mutation == "missing":
            structured.pop("provider_status")
        elif self.output_mutation == "type":
            structured["page_ordinal"] = True
        else:  # pragma: no cover - test helper guard
            raise AssertionError(self.output_mutation)
        return with_hash(
            {key: value for key, value in result.items() if key != "content_hash"}
        )


class MutatingResponseJournal:
    def __init__(self, journal: RunnerJournal):
        self._journal = journal

    def __getattr__(self, name):
        return getattr(self._journal, name)

    def append(self, runner_request_ref, state, payload, **kwargs):
        changed = copy.deepcopy(payload)
        if state == "responded" and "response" in changed:
            response = changed["response"]
            response["outcome"] = "failed"
            changed["response"] = with_hash(
                {key: value for key, value in response.items() if key != "content_hash"}
            )
        return self._journal.append(runner_request_ref, state, changed, **kwargs)


class ReferenceShadowHarness:
    def __init__(
        self,
        source: str,
        scenario: str,
        *,
        max_pages: int = 2,
        fault_at: str | None = None,
        parameter_overrides: Mapping[str, object] | None = None,
        profile_mutator: Callable[[dict], None] | None = None,
        spool_max_total_bytes: int = 2_000_000,
    ):
        self.source = source
        self.scenario = scenario
        self.clock = MutableClock()
        self.core = DaltonStore(":memory:")
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            ":memory:", clock=self.clock, default_lease_seconds=30,
            max_lease_seconds=60, trusted_journal_reader=self.journal,
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
        package_manifest_ref = (
            f"artifact:runner-package:recorded-reference-shadow:{source}:0.1"
        )
        package_manifest_hash = content_hash(
            {
                "schema_version": "0.1", "adapter_ref": adapter_ref,
                "adapter_hash": adapter_hash,
                "recorded_fixture_ref": self.fixture["id"],
                "recorded_fixture_hash": self.fixture["content_hash"],
            }
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
            "package_manifest_ref": package_manifest_ref,
            "package_manifest_hash": package_manifest_hash,
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
        if profile_mutator is not None:
            profile_mutator(profile)
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
        if parameter_overrides is not None:
            parameters.update(parameter_overrides)
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
        self.spool = RawSpool(
            self.temp.name, max_total_bytes=spool_max_total_bytes
        )
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
            "inventory_fixture_manifest_ref": self.template["fixture_manifest_ref"],
            "inventory_fixture_manifest_hash": self.template["fixture_manifest_hash"],
            "recorded_fixture_ref": self.fixture["id"],
            "recorded_fixture_hash": self.fixture["content_hash"],
            "transport_target_ref": self.template["transport"]["target_ref"],
            "transport_target_hash": self.template["transport"]["target_hash"],
            "transport_host_policy": self.template["transport"]["host_policy"],
            "adapter_ref": adapter_ref, "adapter_hash": adapter_hash,
            "package_manifest_ref": package_manifest_ref,
            "package_manifest_hash": package_manifest_hash,
            "recorded_scenario": scenario,
            "recorded_scenario_ref": self.adapter.scenario_ref,
            "recorded_scenario_hash": self.adapter.scenario_hash,
            "recorded_behavior": self.adapter.behavior,
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
            clock=self.clock,
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
        if hasattr(self, "_request_wire"):
            return copy.deepcopy(self._request_wire)
        self._request_wire = with_hash(
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
        return copy.deepcopy(self._request_wire)

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
        parameter_overrides: Mapping[str, object] | None = None,
        profile_mutator: Callable[[dict], None] | None = None,
        spool_max_total_bytes: int = 2_000_000,
    ) -> ReferenceShadowHarness:
        harness = ReferenceShadowHarness(
            source, scenario, max_pages=max_pages, fault_at=fault_at,
            parameter_overrides=parameter_overrides,
            profile_mutator=profile_mutator,
            spool_max_total_bytes=spool_max_total_bytes,
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
        cross_source = copy.deepcopy(built["cninfo"])
        cross_source["parent_parameters"] = built["sec"]["parent_parameters"]
        with self.assertRaises(AssertionError):
            validate_json_schema(cross_source, schema, schema)

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
                if scenario in {"schema_drift", "malformed"}:
                    result = harness.journal.latest(harness.request()["id"])[
                        "payload"
                    ]["result_envelope"]
                    self.assertEqual(result["error"]["code"], scenario)

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
                        clock=harness.clock,
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

    def test_runtime_fixture_must_equal_packaged_deterministic_fixture(self) -> None:
        harness = self.harness("cninfo", "pagination")
        mutated = copy.deepcopy(harness.fixture)
        pagination = next(
            item for item in mutated["scenarios"]
            if item["scenario"] == "pagination"
        )
        pagination["pages"][0]["raw_payload"]["announcements"][0]["title"] = (
            "Hostile but self-consistent replacement title"
        )
        mutated = with_hash(
            {key: value for key, value in mutated.items() if key != "content_hash"}
        )
        mutated = validate_recorded_source_fixture(mutated)
        harness.adapter.fixture = mutated
        harness.adapter._case = next(
            item for item in mutated["scenarios"]
            if item["scenario"] == "pagination"
        )
        plan = copy.deepcopy(harness.plan)
        plan["recorded_fixture_hash"] = mutated["content_hash"]
        plan = with_hash(
            {key: value for key, value in plan.items() if key != "content_hash"}
        )
        coordinator = RecordedReferenceShadowCoordinator(
            plan=plan, gate=harness.gate, journal=harness.journal,
            spool=harness.spool, authority=harness.coordinator.authority,
            clock=harness.clock,
        )
        with self.assertRaises(RecordedReferenceShadowError):
            coordinator.execute(
                harness.request(),
                scheduler_lease_token=harness.claim["lease_token"],
            )
        self.assertEqual(harness.count("connector_physical_attempts"), 0)

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
        for mutation in ("query_hash", "prior_hash", "ordinal", "scenario"):
            changed = copy.deepcopy(request)
            if mutation == "query_hash":
                changed["query_hash"] = "0" * 64
            elif mutation == "prior_hash":
                changed["pagination_authority"]["prior_observation_hash"] = "1" * 64
            elif mutation == "scenario":
                changed["pagination_authority"]["recorded_scenario_ref"] = ""
            else:
                changed["pagination_authority"]["page_ordinal"] = 2
            changed = with_hash(
                {key: value for key, value in changed.items() if key != "content_hash"}
            )
            with self.subTest(mutation=mutation), self.assertRaises(Exception):
                validate_connector_adapter_request(changed)

    def test_fixture_query_and_selected_scenario_are_explicit_authority(self) -> None:
        mismatched = self.harness(
            "cninfo", "success",
            parameter_overrides={
                "stock_code": "000001", "date_from": "2020-01-01",
                "date_to": "2020-01-31",
            },
        )
        with self.assertRaises(RecordedReferenceShadowError):
            mismatched.execute()
        self.assertEqual(mismatched.count("connector_physical_attempts"), 0)

        hidden = self.harness("cninfo", "success")
        hidden.adapter._case = next(
            item for item in hidden.fixture["scenarios"]
            if item["scenario"] == "rate_limited"
        )
        with self.assertRaises(RecordedReferenceShadowError):
            hidden.execute()
        self.assertEqual(hidden.count("connector_physical_attempts"), 0)

    def test_normalized_output_is_validated_against_frozen_closed_schema(self) -> None:
        for mutation in ("extra", "missing", "type"):
            with self.subTest(mutation=mutation):
                harness = self.harness("sec", "success")
                harness.adapter.__class__ = HostileOutputAdapter
                harness.adapter.output_mutation = mutation
                response = harness.execute()
                self.assertEqual(response["outcome"], "retryable")
                self.assertIsNone(response["raw_artifact_version_ref"])
                self.assertIsNone(response["source_envelope_ref"])
                self.assertEqual(harness.count("observability_artifact_versions_v2"), 0)
                self.assertEqual(harness.count("connector_source_envelopes"), 0)

    def test_runtime_profile_and_inventory_graph_drift_fail_before_transport(self) -> None:
        def hostile_hosts(profile: dict) -> None:
            profile["allowed_hosts"] = ["evil.example"]

        def hostile_network(profile: dict) -> None:
            profile["network_policy"]["allow_redirects"] = True
            profile["network_policy"]["max_redirects"] = 1

        for mutation in (hostile_hosts, hostile_network):
            with self.subTest(mutation=mutation.__name__):
                harness = self.harness(
                    "cninfo", "success", profile_mutator=mutation
                )
                with self.assertRaises(RecordedReferenceShadowError):
                    harness.execute()
                self.assertEqual(harness.count("connector_physical_attempts"), 0)

        harness = self.harness("sec", "success")
        plan = copy.deepcopy(harness.plan)
        plan["inventory_fixture_manifest_hash"] = "1" * 64
        plan = with_hash(
            {key: value for key, value in plan.items() if key != "content_hash"}
        )
        coordinator = RecordedReferenceShadowCoordinator(
            plan=plan, gate=harness.gate, journal=harness.journal,
            spool=harness.spool, authority=harness.coordinator.authority,
            clock=harness.clock,
        )
        with self.assertRaises(RecordedReferenceShadowError):
            coordinator.execute(
                harness.request(),
                scheduler_lease_token=harness.claim["lease_token"],
            )
        self.assertEqual(harness.count("connector_physical_attempts"), 0)

    def test_page_crash_barriers_recover_or_close_without_orphan_raw(self) -> None:
        expectations = {
            "after_page_reserved": "succeeded",
            "after_page_transport_started": "retryable",
            "after_page_observed": "succeeded",
            "after_page_artifact_recorded": "succeeded",
        }
        for barrier, outcome in expectations.items():
            with self.subTest(barrier=barrier):
                harness = self.harness("cninfo", "success", fault_at=barrier)
                with self.assertRaisesRegex(RuntimeError, barrier):
                    harness.execute()
                harness.coordinator.fault_hook = None
                response = harness.execute()
                self.assertEqual(response["outcome"], outcome)
                self.assertEqual(
                    harness.count("scheduler_result_envelopes", scheduler=True), 1
                )
                if outcome == "succeeded":
                    self.assertEqual(
                        harness.count("observability_artifact_versions_v2"), 1
                    )
                else:
                    self.assertEqual(
                        harness.count("observability_artifact_versions_v2"), 0
                    )
                    settlement = harness.core.connection.execute(
                        "SELECT state FROM connector_quota_settlements"
                    ).fetchone()
                    self.assertEqual(settlement["state"], "indeterminate")

        child = self.harness(
            "cninfo", "pagination", fault_at="after_page_responded"
        )
        with self.assertRaisesRegex(RuntimeError, "after_page_responded"):
            child.execute()
        child.coordinator.fault_hook = None
        self.assertEqual(child.execute()["outcome"], "succeeded")
        self.assertEqual(child.count("connector_physical_attempts"), 2)
        self.assertEqual(child.count("observability_artifact_versions_v2"), 2)

        expired = self.harness(
            "sec", "success", fault_at="after_page_reserved"
        )
        with self.assertRaisesRegex(RuntimeError, "after_page_reserved"):
            expired.execute()
        expired.coordinator.fault_hook = None
        expired.clock.advance(31)
        with self.assertRaises(LeaseExpired):
            expired.execute()
        settlement = expired.core.connection.execute(
            "SELECT state FROM connector_quota_settlements"
        ).fetchone()
        self.assertEqual(settlement["state"], "released")
        self.assertEqual(
            expired.journal.latest(expired.request()["id"])["state"],
            "released_recovered",
        )

        for barrier, expected in (
            ("after_page_transport_started", "retryable"),
            ("after_page_observed", "succeeded"),
        ):
            with self.subTest(expired_recovery=barrier):
                durable = self.harness("sec", "success", fault_at=barrier)
                with self.assertRaisesRegex(RuntimeError, barrier):
                    durable.execute()
                durable.coordinator.fault_hook = None
                durable.clock.advance(31)
                response = durable.execute()
                self.assertEqual(response["outcome"], expected)
                self.assertEqual(
                    durable.count("scheduler_result_envelopes", scheduler=True), 1
                )

    def test_second_page_capacity_failure_keeps_first_page_registered_and_converges(self) -> None:
        harness = self.harness(
            "cninfo", "pagination", spool_max_total_bytes=1_000_100
        )
        response = harness.execute()
        self.assertEqual(response["outcome"], "retryable")
        self.assertIsNone(response["raw_artifact_version_ref"])
        self.assertIsNone(response["source_envelope_ref"])
        self.assertEqual(harness.count("connector_physical_attempts"), 2)
        self.assertEqual(harness.count("observability_artifact_versions_v2"), 1)
        self.assertEqual(harness.count("connector_source_envelopes"), 0)
        before = {
            table: harness.count(table)
            for table in (
                "connector_physical_attempts", "connector_usage_entries",
                "connector_cost_entries", "connector_quota_settlements",
                "observability_artifact_versions_v2", "runner_attempt_journal_events",
            )
        }
        duplicate = harness.execute()
        self.assertEqual(duplicate["idempotency_status"], "duplicate")
        self.assertEqual(
            before, {table: harness.count(table) for table in before}
        )

    def test_journaled_completion_reconciles_after_expiry_and_reclaim_race_fails_closed(self) -> None:
        for swept in (False, True):
            with self.subTest(swept=swept):
                harness = self.harness(
                    "sec", "success", fault_at="after_response_journaled"
                )
                with self.assertRaisesRegex(RuntimeError, "after_response_journaled"):
                    harness.execute()
                harness.coordinator.fault_hook = None
                harness.clock.advance(31)
                if swept:
                    harness.scheduler.sweep_expired()
                response = harness.execute()
                self.assertEqual(response["outcome"], "succeeded")
                self.assertEqual(
                    harness.count("scheduler_result_envelopes", scheduler=True), 1
                )

        raced = self.harness(
            "sec", "success", fault_at="after_response_journaled"
        )
        with self.assertRaisesRegex(RuntimeError, "after_response_journaled"):
            raced.execute()
        raced.coordinator.fault_hook = None
        raced.clock.advance(31)
        raced.scheduler.sweep_expired()
        self.assertIsNotNone(
            raced.scheduler.claim("runner:connector", work_order_id=raced.work.id)
        )
        with self.assertRaises(SchedulerConflict):
            raced.execute()
        self.assertEqual(raced.count("scheduler_result_envelopes", scheduler=True), 0)

    def test_scheduler_reconciliation_rejects_caller_claim_without_journal(self) -> None:
        with self.assertRaises(SchedulerValidationError):
            Scheduler(trusted_journal_reader=object())
        harness = self.harness("cninfo", "success")
        request = harness.request()
        result = ResultEnvelope(
            schema_version="0.1",
            id="result-envelope:forged-without-journal",
            created_at=harness.clock().isoformat(timespec="microseconds"),
            work_order_ref=harness.work.id,
            invocation_ref=harness.execution.id,
            status="succeeded",
            outputs={},
            actual_side_effects=(),
            usage_refs=(),
            artifact_refs=(),
            error=None,
            metadata={"runner_request_ref": request["id"]},
        )
        result_hash = content_hash(result.to_dict())
        with self.assertRaises(SchedulerConflict):
            harness.scheduler.reconcile_journaled_completion(
                harness.work.id,
                1,
                request["runner_actor_ref"],
                result,
                lease_revision_ref=request["scheduler_lease_revision_ref"],
                lease_hash=request["scheduler_lease_hash"],
                work_order_hash=request["work_order_hash"],
                idempotency_key="forged-journal-reconciliation",
                result_envelope_hash=result_hash,
            )
        self.assertEqual(harness.count("runner_attempt_journal_events"), 0)
        self.assertEqual(harness.count("scheduler_result_envelopes", scheduler=True), 0)
        harness.journal.begin_request(request)
        harness.journal.append(
            request["id"], "reserved", {"reservation_ref": "reservation:synthetic"}
        )
        harness.journal.append(
            request["id"], "observed", {"reservation_ref": "reservation:synthetic"}
        )
        harness.journal.append(
            request["id"],
            "responded",
            {
                "reservation_ref": "reservation:synthetic",
                "result_envelope": result.to_dict(),
                "result_envelope_hash": result_hash,
            },
        )
        with self.assertRaises(SchedulerConflict):
            harness.scheduler.reconcile_journaled_completion(
                harness.work.id,
                1,
                request["runner_actor_ref"],
                result,
                lease_revision_ref=request["scheduler_lease_revision_ref"],
                lease_hash=request["scheduler_lease_hash"],
                work_order_hash=request["work_order_hash"],
                idempotency_key="malformed-journal-reconciliation",
                result_envelope_hash=result_hash,
            )
        self.assertEqual(harness.count("scheduler_result_envelopes", scheduler=True), 0)

    def test_hostile_parent_response_is_rejected_before_scheduler_reconciliation(self) -> None:
        harness = self.harness(
            "cninfo", "success", fault_at="after_response_journaled"
        )
        hostile_journal = MutatingResponseJournal(harness.journal)
        harness.journal = hostile_journal
        harness.coordinator.journal = hostile_journal
        with self.assertRaises(Exception):
            harness.execute()
        self.assertEqual(harness.count("scheduler_result_envelopes", scheduler=True), 0)

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
