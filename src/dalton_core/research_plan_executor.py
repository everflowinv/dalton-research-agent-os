"""Authority-bound executor for the approved SEC public research plan tree.

This module connects the human-gated ResearchPlan task tree to the already
built real components.  The executor runs the currently admitted node of one
exact accepted + started SEC public plan, one node at a time:

- ``connector``: the plan root WorkOrder is claimed on the Scheduler and
  executed through the existing ``ConnectorTransportExecutor`` + SEC public
  adapter path.  The compiled runner request, the actual runner request in
  the Core runner journal, the v0.2 completion receipt and the transport's
  own Scheduler formal result / ResultEnvelope preserve the exact
  receipt/journal/source/artifact authority bindings the coordinator
  verifies at admission.
- ``authority_resolver``: re-reads the connector receipt/source/artifact
  authority, persists the deterministic bridge records (CompiledConnectorPlan,
  ContextPack, ClaimIndex snapshot, ResearchCheckpoint) derived only from
  immutable plan authority, and resolves through the real
  ``ConnectorAuthorityResolver``.
- ``verifier``: re-reads the typed checkpoint/plan/context/request/receipt
  records, re-resolves the exact source authority, verifies the source
  material and a plan-derived numeric filing-count spec through the real
  verifier, and completes with the closed ``ResearchPlanStageOutput`` proof.
- ``candidate_staging``: re-derives the same verified material/bundles,
  builds candidate evidence/claim and stages through the real
  ``CandidateStagingStore`` (candidate-only; no Ledger write, no auto-accept
  of human review).

Every internal step persists the Scheduler formal result + ResultEnvelope
whose ``outputs`` carry the closed, hashed ``ResearchPlanStageOutput`` the
coordinator already verifies, then calls the coordinator to admit exactly the
next node.  Admission is the only enqueue path; the Scheduler stays the only
queue authority.

Fail-closed guarantees:

- every call re-reads the exact plan version, the exact accepted approval +
  started binding (workflow version, links, root WorkOrder) and every typed
  bridge/authority record before any write; ref/hash/plan/upstream mismatch
  raises ``ResearchPlanExecutorConflict`` and writes nothing;
- every durable write (connector receipt, checkpoint, staging candidate,
  Scheduler completion, admission) is idempotent with a plan-derived key, so
  crash/replay cannot fork a second task/result/staging candidate;
- the executor never writes the formal Ledger (the ClaimIndex is a read-only
  snapshot) and never auto-accepts human review;
- unsupported states fail closed: a work order already leased by a crashed
  run returns ``pending`` until the lease expires, a retryable connector
  attempt leaves the tree pending (the coordinator observes the exact attempt
  states), and a rejected verification completes the node as ``failed`` so
  the tree blocks instead of looping.

Transport-internal crash windows (between the transport's durable journal
states) belong to the ``ConnectorTransportExecutor`` recovery protocol and
are not re-implemented here; the executor only replays seams that persist
through its own idempotent writes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .agenda import read_exact_agenda_cycle, read_exact_mandate_version
from .authority_resolver import (
    ConnectorAuthorityResolver,
    validate_authority_resolution,
)
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorNotFound, ConnectorStore
from .connector_inventory import load_packaged_connector_inventory
from .connector_runner import validate_connector_runner_request
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, ResultEnvelope
from .research_context import (
    build_claim_index,
    build_compiled_connector_plan,
    build_context_pack,
    validate_compiled_connector_plan,
    validate_compiled_connector_step,
    validate_context_pack,
    validate_runner_request_plan_binding,
)
from .research_coordinator import (
    ResearchCoordinatorStore,
    validate_connector_completion_receipt,
    validate_research_checkpoint,
)
from .research_plan import (
    PLANNER_HASH,
    PLANNER_REF,
    SEC_CAPABILITY,
    SEC_COMPANY_FACTS_OPERATION,
    SEC_OPERATION,
    SEC_RUNTIME_PROFILE_REF,
    _plan_work_orders,
    plan_start_ref_for,
    read_exact_research_plan_start,
    read_exact_research_plan_version,
)
from .research_plan_coordinator import (
    ResearchPlanCoordinator,
    _stage_output_ref,
)
from .research_verification import (
    CandidateStagingStore,
    build_authority_source_material,
    build_candidate_claim,
    build_candidate_evidence,
    validate_candidate_claim,
    validate_candidate_evidence,
    validate_numeric_verification_spec,
    validate_verification_bundle,
    verify_authority_source_material,
    verify_numeric_spec,
)
from .scheduler import Scheduler
from .store import canonical_json, content_hash

# Frozen public read-only connector policy constants for the SEC runtime
# profile.  These are the closed values the packaged SEC template's
# transport/auth boundary implies and the plan's frozen scope requires.
_NETWORK_POLICY = {
    "allowed_schemes": ["https"],
    "allow_redirects": False,
    "max_redirects": 0,
    "resolve_public_only": True,
}
_PROFILE_ACCESS_POLICY_REF = "policy:access:public"
_PROFILE_RETENTION_POLICY_REF = "policy:retention:filing"
_PROFILE_TERMS_POLICY_REF = "policy:terms:sec"
_DESCRIPTOR_POLICY_REF = "policy:capability-v1"
_RUNNER_ENVIRONMENT_ID = "runner-environment:sec-public:v1"
_RUNNER_ACTOR_REF = "runner:research-plan-executor"
_RATE_POLICY_REF = "connector-rate-policy:sec-public"
_ROUNDING = {"mode": "half_up", "digits": 0}

_STAGE_RECORD_KINDS = {
    "authority_resolver": ("authority_resolution",),
    "verifier": ("source_verification", "numeric_verification"),
    "candidate_staging": ("candidate_evidence", "candidate_claim"),
}


class ResearchPlanExecutorError(Exception):
    """Base error for the research-plan tree executor."""


class ResearchPlanExecutorConflict(ResearchPlanExecutorError):
    """Authority drift detected; the executor wrote nothing."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResearchPlanExecutorError(f"{name} must be a non-empty string")
    return value


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ResearchPlanExecutorError(f"{name} must be lowercase SHA-256")
    return value


def _wire_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ResearchPlanExecutorError("executor clock must return aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _derived_ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def _plan_steps(plan_wire: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = plan_wire["execution_scope"]["steps"]
    if not isinstance(steps, list) or len(steps) != 4:
        raise ResearchPlanExecutorConflict(
            "research plan must define the closed four-node tree"
        )
    return steps


def sec_descriptor_spec(
    template: Mapping[str, Any],
    permissions: Mapping[str, Any],
    created_at: str,
    *,
    capability_policy_ref: str = _DESCRIPTOR_POLICY_REF,
    operation_name: str = SEC_OPERATION,
) -> dict[str, Any]:
    """The closed SEC capability descriptor proposal for one template.

    The proposal is a pure function of the packaged template, the
    operator-injected permissions and the executor clock, so the operator can
    publish it once and the executor re-reads the same descriptor on replay.
    """

    identity = sec_connector_identity(template, operation_name)
    return {
        "schema_version": "0.1",
        "id": identity["capability_id"],
        "version": 1,
        "created_at": created_at,
        "kind": "connector",
        "name": "sec-public-research",
        "label": "SEC public research adapter",
        "summary": "Read an approved public SEC data endpoint",
        "aliases": ["SEC filings", "SEC company facts"],
        "tags": ["connector", "public", "SEC"],
        "intent_examples": ["list public SEC filings", "read one SEC company concept"],
        "source": {
            "type": identity["capability_id"].split(":")[1],
            "namespace": identity["capability_id"].split(":")[2],
            "source_ref": "artifact:sec-public-source",
            "source_version": "1",
        },
        "contract": {
            "mode": "typed_call",
            "input_schema_ref": identity["input_schema_ref"],
            "output_schema_ref": identity["output_schema_ref"],
            "instruction_ref": None,
            "adapter_ref": identity["adapter_ref"],
        },
        "permissions": dict(permissions),
        "eligibility": {
            "state": "ready",
            "visibility_scopes": ["research"],
            "policy_ref": capability_policy_ref,
            "valid_until": None,
        },
        "source_hash": identity["source_hash"],
        "schema_hash": identity["schema_hash"],
    }


def sec_connector_identity(
    template: Mapping[str, Any], operation_name: str = SEC_OPERATION
) -> dict[str, Any]:
    """Deterministic SEC runtime identity derived from the packaged template.

    Every value is a pure function of the frozen packaged SEC template and
    the frozen plan constants, so the operator-installed runner manifest and
    the executor cannot drift apart without breaking a hash comparison.
    """

    approved_operations = (SEC_OPERATION, SEC_COMPANY_FACTS_OPERATION)
    operations = {
        item["operation"]: item
        for item in template["operations"]
        if item["operation"] in approved_operations
    }
    operation = operations.get(operation_name)
    if operation is None:
        raise ResearchPlanExecutorConflict(
            f"packaged SEC template lacks the frozen {operation_name} operation"
        )
    if set(operations) != set(approved_operations):
        raise ResearchPlanExecutorConflict(
            "packaged SEC template lacks an approved public operation"
        )
    source_identity = dict(template["source_identity"])
    source_hash = content_hash(source_identity)
    adapter_ref = template["transport"]["target_ref"]
    schema_hash = content_hash({
        "allowed_operations": list(approved_operations),
        "input_schema_refs": {
            name: operations[name]["input_schema_ref"] for name in approved_operations
        },
        "input_schema_hashes": {
            name: operations[name]["input_schema_hash"] for name in approved_operations
        },
        "output_schema_refs": {
            name: operations[name]["output_schema_ref"] for name in approved_operations
        },
        "output_schema_hashes": {
            name: operations[name]["output_schema_hash"] for name in approved_operations
        },
    })
    return {
        "capability_id": SEC_CAPABILITY,
        "source_identity": source_identity,
        "source_hash": source_hash,
        "schema_hash": schema_hash,
        "adapter_ref": adapter_ref,
        "adapter_hash": content_hash({
            "adapter_ref": adapter_ref, "source": source_identity["source_ref"],
        }),
        "allowed_operations": list(approved_operations),
        "input_schema_refs": {
            name: operations[name]["input_schema_ref"] for name in approved_operations
        },
        "input_schema_hashes": {
            name: operations[name]["input_schema_hash"] for name in approved_operations
        },
        "output_schema_refs": {
            name: operations[name]["output_schema_ref"] for name in approved_operations
        },
        "output_schema_hashes": {
            name: operations[name]["output_schema_hash"] for name in approved_operations
        },
        "completeness": {
            name: operations[name]["completeness_ceiling"]
            for name in approved_operations
        },
        "operation": operation_name,
        "input_schema_ref": operation["input_schema_ref"],
        "input_schema_hash": operation["input_schema_hash"],
        "output_schema_ref": operation["output_schema_ref"],
        "output_schema_hash": operation["output_schema_hash"],
        "completeness_required": operation["completeness_ceiling"],
        "pagination": {
            "mode": operation["pagination"]["mode"],
            "cursor_field": operation["pagination"]["cursor_field"],
            "max_pages": operation["pagination"]["max_pages"],
        },
        "allowed_hosts": sorted(template["transport"]["allowed_hosts"]),
    }


def sec_adapter_parameters(plan_wire: Mapping[str, Any]) -> dict[str, Any]:
    """Map the frozen plan request onto the SEC adapter's closed parameter set.

    The mapping is deterministic (CIK/form/window stay byte-identical; the
    page limit is the plan's frozen budget bound), so the adapter replay in
    the authority resolver is reproducible from plan authority alone.
    """

    scope = plan_wire["execution_scope"]
    request = scope["parameters"]
    if scope["operation"] == SEC_COMPANY_FACTS_OPERATION:
        return {
            "cik": request["cik"],
            "taxonomy": request["taxonomy"],
            "concept": request["concept"],
            "unit": request["unit"],
            "form": request["form"],
            "filed_to": request["filed_to"],
        }
    if scope["operation"] != SEC_OPERATION:
        raise ResearchPlanExecutorConflict(
            "plan operation is outside the approved SEC adapter routes"
        )
    budget = plan_wire["execution_scope"]["budget"]
    limit = budget["max_pages"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ResearchPlanExecutorConflict(
            "plan budget lacks the frozen positive max_pages bound"
        )
    return {
        "issuer": request["issuer_cik"],
        "form": request["form"],
        "date_from": request["filing_date_from"],
        "date_to": request["filing_date_to"],
        "limit": limit,
    }


def stage_proof_wire(
    *,
    plan_wire: Mapping[str, Any],
    step: Mapping[str, Any],
    upstream_work_order: Mapping[str, Any],
    upstream_formal: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    created_at: str,
) -> dict[str, Any]:
    """Build the closed ResearchPlanStageOutput proof the coordinator verifies."""

    fields = {
        "schema_version", "id", "created_at", "plan_version_ref",
        "plan_version_hash", "step_ref", "step_hash", "stage", "operation",
        "output_contract_ref", "upstream_work_order_ref", "upstream_result_ref",
        "upstream_result_hash", "records", "content_hash",
    }
    body = {
        "schema_version": "0.1",
        "id": _stage_output_ref(
            plan_version_ref=plan_wire["id"],
            step_ref=step["id"],
            upstream_result_ref=upstream_formal["result_envelope_id"],
            upstream_result_hash=upstream_formal["result_envelope_hash"],
        ),
        "created_at": created_at,
        "plan_version_ref": plan_wire["id"],
        "plan_version_hash": plan_wire["content_hash"],
        "step_ref": step["id"],
        "step_hash": step["content_hash"],
        "stage": step["stage"],
        "operation": step["operation"],
        "output_contract_ref": step["output_contract_ref"],
        "upstream_work_order_ref": upstream_work_order["id"],
        "upstream_result_ref": upstream_formal["result_envelope_id"],
        "upstream_result_hash": upstream_formal["result_envelope_hash"],
        "records": [dict(item) for item in records],
    }
    body["content_hash"] = content_hash(body)
    wire = json.loads(canonical_json(body))
    if set(wire) != fields:
        raise ResearchPlanExecutorConflict(
            "research-plan stage output proof has an invalid closed shape"
        )
    return wire


def re_read_stage_records(
    upstream_formal: Mapping[str, Any], *, expected_kinds: Sequence[str]
) -> list[dict[str, Any]]:
    """Re-read the typed record refs/hashes from the upstream stage envelope."""

    try:
        envelope = upstream_formal["result_envelope"]
    except (TypeError, ValueError) as exc:
        raise ResearchPlanExecutorConflict(
            "upstream formal result embeds invalid ResultEnvelope JSON"
        ) from exc
    output = envelope.get("outputs")
    if not isinstance(output, Mapping):
        raise ResearchPlanExecutorConflict(
            "upstream stage envelope lacks a closed output proof"
        )
    records = output.get("records")
    if (
        not isinstance(records, list)
        or tuple(item.get("kind") for item in records if isinstance(item, Mapping))
        != tuple(expected_kinds)
        or len(records) != len(expected_kinds)
    ):
        raise ResearchPlanExecutorConflict(
            "upstream stage output record kinds do not match the frozen stage"
        )
    result: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping) or set(item) != {"kind", "ref", "hash"}:
            raise ResearchPlanExecutorConflict(
                "upstream stage output record is not a closed ref/hash binding"
            )
        result.append({
            "kind": _text(item["kind"], "record.kind"),
            "ref": _text(item["ref"], "record.ref"),
            "hash": _hash(item["hash"], "record.hash"),
        })
    return result


class ResearchPlanExecutor:
    """Executes the current admitted node of one exact started SEC plan.

    Construction requires the exact plan/scheduler/coordinator and the real
    connector authority components (catalog, connector store, transport
    executor, authority resolver, candidate staging store).  The clock must
    be the same frozen clock the scheduler/catalog/connectors/transport use;
    the operator-injected ``permissions`` and ``policy_resolver`` are the
    same authority the capability catalog was built with.
    """

    def __init__(
        self,
        *,
        plan: Any,
        scheduler: Scheduler,
        connector_records: ResearchCoordinatorStore,
        coordinator: ResearchPlanCoordinator,
        connectors: ConnectorStore,
        catalog: CapabilityCatalog,
        transport: ConnectorTransportExecutor,
        resolver: ConnectorAuthorityResolver,
        staging: CandidateStagingStore,
        clock: Callable[[], datetime],
        permissions: Mapping[str, Any],
        policy_resolver: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        principal_ref: str,
        runner_environment_hash: str,
        actor_ref: str = _RUNNER_ACTOR_REF,
        capability_policy_ref: str = _DESCRIPTOR_POLICY_REF,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(scheduler, Scheduler):
            raise TypeError("scheduler must be an exact Scheduler")
        if not isinstance(connector_records, ResearchCoordinatorStore):
            raise TypeError(
                "connector_records must be an exact ResearchCoordinatorStore"
            )
        if not isinstance(coordinator, ResearchPlanCoordinator):
            raise TypeError("coordinator must be an exact ResearchPlanCoordinator")
        if not isinstance(connectors, ConnectorStore):
            raise TypeError("connectors must be an exact ConnectorStore")
        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must be an exact CapabilityCatalog")
        if not isinstance(transport, ConnectorTransportExecutor):
            raise TypeError("transport must be an exact ConnectorTransportExecutor")
        if not isinstance(resolver, ConnectorAuthorityResolver):
            raise TypeError("resolver must be an exact ConnectorAuthorityResolver")
        if not isinstance(staging, CandidateStagingStore):
            raise TypeError("staging must be an exact CandidateStagingStore")
        if plan is not coordinator.plan:
            raise TypeError("executor plan must be the coordinator plan authority")
        if scheduler is not coordinator.scheduler:
            raise TypeError("executor scheduler must be the coordinator scheduler")
        if connector_records is not coordinator.connector_records:
            raise TypeError(
                "executor connector records must be the coordinator authority"
            )
        if (
            resolver.scheduler is not scheduler
            or resolver.connectors is not connectors
            or resolver.coordinator is not connector_records
        ):
            raise TypeError(
                "executor resolver must read the exact supplied authorities"
            )
        if not isinstance(permissions, Mapping) or not isinstance(
            permissions.get("side_effects"), (list, tuple)
        ):
            raise ResearchPlanExecutorConflict(
                "operator-injected permissions must declare side_effects"
            )
        if "read:public-http" not in permissions["side_effects"]:
            raise ResearchPlanExecutorConflict(
                "SEC public executor permissions must include read:public-http"
            )
        if not callable(policy_resolver):
            raise TypeError("policy_resolver must be callable")
        if scheduler.connection is not coordinator.plan.connection:
            raise TypeError(
                "executor plan/scheduler/coordinator must share one Core connection"
            )
        self.plan = coordinator.plan
        self.scheduler = scheduler
        self.connector_records = connector_records
        self.coordinator = coordinator
        self.connectors = connectors
        self.catalog = catalog
        self.transport = transport
        self.resolver = resolver
        self.staging = staging
        self.clock = clock
        self.permissions = dict(permissions)
        self.policy_resolver = policy_resolver
        self.principal_ref = _text(principal_ref, "principal_ref")
        self.actor_ref = _text(actor_ref, "actor_ref")
        self.runner_environment_hash = _hash(
            runner_environment_hash, "runner_environment_hash"
        )
        self.capability_policy_ref = _text(
            capability_policy_ref, "capability_policy_ref"
        )
        self.fault_injector = fault_injector

    # ------------------------------------------------------------------ #

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    def _admit(self, plan_wire: Mapping[str, Any], upstream_index: int) -> dict[str, Any]:
        """Admit the immediate child of an exact succeeded upstream node.

        Before admitting the resolver child of the connector node, the
        connector receipt chain is (re)built from stored authority, so a
        crash between the transport completion and the receipt store replays
        through the same idempotent store and cannot leave the coordinator
        without its receipt proof.
        """

        work_orders = self._work_orders(plan_wire)
        upstream = work_orders[upstream_index]
        if upstream_index == 0:
            steps = _plan_steps(plan_wire)
            formal = self.scheduler.formal_result(upstream["id"])
            if formal is None or formal["terminal_state"] != "succeeded":
                raise ResearchPlanExecutorConflict(
                    "connector node is not terminally succeeded before admission"
                )
            self._ensure_receipt_from_authority(
                plan_wire, steps[0], upstream, formal
            )
        admitted = self.coordinator.admit_next_work_order(
            plan_version_ref=plan_wire["id"],
            upstream_work_order_ref=upstream["id"],
        )
        if admitted["status"] == "fresh":
            return {
                "status": "admitted",
                "plan_version_ref": plan_wire["id"],
                "admitted_work_order_ref": admitted["admitted_work_order_ref"],
                "admitted_work_order_hash": admitted["admitted_work_order_hash"],
                "admitted_step_ordinal": admitted["admitted_step_ordinal"],
                "admitted_stage": admitted["admitted_stage"],
            }
        return dict(admitted)

    # ------------------------------------------------------------------ #
    # Entry point

    def run_once(self, *, plan_version_ref: str) -> dict[str, Any]:
        """Execute the current admitted node and admit exactly the next one.

        Idempotent replay: a crash between any durable write and the next
        write converges on the same rows; a crashed run that never wrote
        leaves the node claimable again after lease expiry.
        """

        plan_version_ref = _text(plan_version_ref, "plan_version_ref")
        cursor = self.plan.connection.cursor()
        plan_wire = read_exact_research_plan_version(cursor, plan_version_ref)
        # Requirement: exact accepted + started SEC public plan.  The start
        # reader re-validates the accepted approval, the WorkflowRunVersion,
        # the WorkOrderLink rows and the admitted root WorkOrder.
        read_exact_research_plan_start(
            cursor, plan_start_ref_for(plan_version_ref)
        )
        work_orders = self._work_orders(plan_wire)
        steps = _plan_steps(plan_wire)

        current_index: int | None = None
        for index, work_order in enumerate(work_orders):
            formal = self.scheduler.formal_result(work_order["id"])
            if formal is None:
                current_index = index
                break
            if formal["terminal_state"] != "succeeded":
                return {
                    "status": "blocked",
                    "plan_version_ref": plan_wire["id"],
                    "work_order_ref": work_order["id"],
                    "stage": steps[index]["stage"],
                    "reason": "node terminated unsuccessfully",
                }
        if current_index is None:
            self._audit_completed_tree(plan_wire, steps, work_orders)
            return {
                "status": "complete",
                "plan_version_ref": plan_wire["id"],
                "plan_version_hash": plan_wire["content_hash"],
            }

        current = work_orders[current_index]
        admitted = cursor.execute(
            "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
            (current["id"],),
        ).fetchone()
        if admitted is None:
            if current_index == 0:
                raise ResearchPlanExecutorConflict(
                    "started plan root WorkOrder is not admitted"
                )
            # Crash between upstream completion and admission: replay the
            # admission only; the upstream node is already terminal.
            return self._admit(plan_wire, current_index - 1)

        if current_index == 0:
            outcome = self._execute_connector(plan_wire, steps[0], current)
        else:
            outcome = self._execute_internal(
                plan_wire, steps, work_orders, current_index
            )
        if outcome["status"] != "succeeded":
            return outcome
        return self._admit(plan_wire, current_index)

    # ------------------------------------------------------------------ #
    # Connector node

    @staticmethod
    def _work_orders(plan_wire: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _plan_work_orders(plan_wire)

    def _connector_authority(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        *,
        scheduler_attempt_number: int,
    ) -> dict[str, Any]:
        """Register the deterministic SEC runtime authority records.

        All refs/hashes derive from the packaged template and immutable plan
        authority; every registration is idempotent.  The runtime profile
        keeps the plan's frozen connector profile ref so the plan scope, the
        compiled step and the runner request cannot drift apart.
        """

        template = load_packaged_connector_inventory()["templates"]["sec"]
        if (
            plan_wire["execution_scope"]["connector_profile_ref"] != template["id"]
            or plan_wire["execution_scope"]["connector_profile_hash"]
            != template["content_hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "plan connector profile drifted from the packaged SEC template"
            )
        identity = sec_connector_identity(
            template, plan_wire["execution_scope"]["operation"]
        )
        now = _wire_time(self.clock())

        # Capability descriptor: publish only when the exact capability is
        # absent; re-publishing an existing descriptor would fork the epoch.
        try:
            descriptor = self.catalog.describe(
                identity["capability_id"], visibility_scopes=["research"]
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(sec_descriptor_spec(
                template, self.permissions, now,
                capability_policy_ref=self.capability_policy_ref,
                operation_name=plan_wire["execution_scope"]["operation"],
            ))
        # Profile/pricing authority is shared across SEC plans.  Anchor those
        # immutable version-1 records to the descriptor's own timestamp and
        # global idempotency keys; otherwise a scheduler retry (or a second
        # plan) at a later clock value would conflict with the first plan's
        # already-installed profile even though no authority changed.
        authority_created_at = descriptor.created_at
        policy = self.policy_resolver({"policy_ref": self.capability_policy_ref})
        policy_hash = _hash(policy.get("content_hash"), "resolved policy content_hash")

        profile = self.connectors.register_profile({
            "schema_version": "0.1",
            "id": template["id"],
            "created_at": authority_created_at,
            "connector_ref": template["connector_ref"],
            "version": 1,
            "prior_version_ref": None,
            "capability_id": descriptor.id,
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "source_identity": identity["source_identity"],
            "source_hash": identity["source_hash"],
            "schema_hash": identity["schema_hash"],
            "catalog_epoch": descriptor.catalog_epoch,
            "adapter_ref": identity["adapter_ref"],
            "adapter_hash": identity["adapter_hash"],
            "runner_runtime_ref": SEC_RUNTIME_PROFILE_REF,
            "runner_actor_ref": self.actor_ref,
            "runner_environment_hash": self.runner_environment_hash,
            "allowed_operations": identity["allowed_operations"],
            "allowed_hosts": identity["allowed_hosts"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "input_schema_refs": identity["input_schema_refs"],
            "input_schema_hashes": identity["input_schema_hashes"],
            "output_schema_refs": identity["output_schema_refs"],
            "output_schema_hashes": identity["output_schema_hashes"],
            "pagination": identity["pagination"],
            "completeness": identity["completeness"],
            "max_response_bytes": plan_wire["execution_scope"]["budget"][
                "max_response_bytes"
            ],
            "max_records": 100,
            "timeout_ms": plan_wire["execution_scope"]["budget"]["max_seconds"] * 1000,
            "access_policy_ref": _PROFILE_ACCESS_POLICY_REF,
            "retention_policy_ref": _PROFILE_RETENTION_POLICY_REF,
            "terms_policy_ref": _PROFILE_TERMS_POLICY_REF,
            "network_policy": _NETWORK_POLICY,
        }, idempotency_key="connector-profile:sec-public:v1")

        parameters = sec_adapter_parameters(plan_wire)
        call_spec = self.connectors.register_call_spec({
            "schema_version": "0.1",
            "id": step["id"],
            "created_at": plan_wire["created_at"],
            "work_order_ref": work_order["id"],
            "work_order_hash": content_hash(work_order),
            "connector_profile_ref": profile["id"],
            "operation": identity["operation"],
            "parameters": parameters,
            "query_hash": content_hash(
                {"operation": identity["operation"], "parameters": parameters}
            ),
        }, idempotency_key=f"research-plan:{plan_wire['id']}:sec-call")

        invocation_id = _derived_ref("connector-invocation:research-plan", {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
        })
        # The transport derives the raw artifact version id from the
        # invocation + physical attempt number.  A fresh invocation always
        # starts at physical attempt 1, so the executor can precompute the
        # exact version id and bind it as the single execution output; the
        # coordinator's receipt chain and the authority resolver both require
        # the receipt artifact ref to equal the execution output ref.
        artifact_version_id = "artifact-version:" + content_hash({
            "kind": "artifact-version",
            "idempotency_key": f"runner:{invocation_id}:1:artifact",
        })
        try:
            invocation = self.connectors.get_invocation(invocation_id)
        except ConnectorNotFound:
            # One logical connector invocation spans Scheduler recovery after
            # a pre-dispatch crash.  Its capability lease therefore covers
            # the plan's whole bounded retry window, while every actual
            # RunnerRequest still binds the current Scheduler lease/attempt.
            ttl_seconds = policy.get("max_lease_seconds")
            if (
                isinstance(ttl_seconds, bool)
                or not isinstance(ttl_seconds, int)
                or ttl_seconds < 1
            ):
                raise ResearchPlanExecutorConflict(
                    "resolved capability policy lacks a positive lease bound"
                )
            lease = self.catalog.prepare(
                work_order,
                capability_id=descriptor.id,
                revision_ref=descriptor.revision_ref,
                catalog_epoch=descriptor.catalog_epoch,
                descriptor_hash=descriptor.content_hash,
                source_hash=descriptor.source_hash,
                schema_hash=descriptor.schema_hash,
                policy_ref=descriptor.eligibility.policy_ref,
                policy_hash=policy_hash,
                principal_ref=self.principal_ref,
                visibility_scopes=["research"],
                ttl_seconds=ttl_seconds,
            )
            execution = ExecutionInvocation(
                schema_version="0.1",
                id=invocation_id,
                created_at=now,
                kind=ExecutionKind.CONNECTOR,
                work_order_ref=work_order["id"],
                profile_ref=profile["id"],
                capability=profile["capability_id"],
                input_refs=(call_spec["id"],),
                output_refs=(artifact_version_id,),
                started_at=now,
                completed_at=None,
                side_effects=(),
                runtime_ref=profile["runner_runtime_ref"],
                actor_ref=profile["runner_actor_ref"],
                environment_hash=profile["runner_environment_hash"],
            )
            invocation = self.connectors.register_invocation({
                "schema_version": "0.1",
                "id": invocation_id,
                "created_at": now,
                "work_order_ref": work_order["id"],
                "work_order_hash": content_hash(work_order),
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "call_spec_ref": call_spec["id"],
                "call_spec_hash": call_spec["content_hash"],
                "capability_lease_ref": lease.id,
                "capability_lease_hash": lease.content_hash,
                "descriptor_revision_ref": descriptor.revision_ref,
                "catalog_epoch": descriptor.catalog_epoch,
                "logical_invocation_key": "connector-logical:" + content_hash({
                    "work_order_ref": work_order["id"],
                    "work_order_hash": content_hash(work_order),
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_hash": call_spec["content_hash"],
                }),
            }, execution=execution, idempotency_key=(
                f"research-plan:{plan_wire['id']}:sec-invocation"
            ))
            execution_wire: dict[str, Any] | None = execution.to_dict()
        else:
            expected = {
                "work_order_ref": work_order["id"],
                "work_order_hash": content_hash(work_order),
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "call_spec_ref": call_spec["id"],
                "call_spec_hash": call_spec["content_hash"],
                "descriptor_revision_ref": descriptor.revision_ref,
                "catalog_epoch": descriptor.catalog_epoch,
            }
            if any(invocation.get(name) != value for name, value in expected.items()):
                raise ResearchPlanExecutorConflict(
                    "stored logical connector invocation drifted from plan authority"
                )
            lease = self.catalog.get_lease(invocation["capability_lease_ref"])
            if lease.content_hash != invocation["capability_lease_hash"]:
                raise ResearchPlanExecutorConflict(
                    "stored connector invocation capability lease drifted"
                )
            execution_wire = None

        price = self.connectors.register_price_rate({
            "schema_version": "0.1",
            "id": "connector-price:sec-public:zero:v1",
            "created_at": authority_created_at,
            "price_rate_ref": "connector-price:sec-public:zero",
            "version": 1,
            "prior_version_ref": None,
            "connector_profile_ref": profile["id"],
            "meter": "calls",
            "unit_quantity": 1,
            "unit_price_micros": 0,
            "rounding_mode": "ceiling",
            "currency": "USD",
            "effective_from": authority_created_at,
            "effective_until": None,
            "source_ref": "pricing:connector-price:sec-public:zero",
            "actor_ref": self.actor_ref,
        }, idempotency_key="connector-price:sec-public:zero:v1")
        price_book = {
            "price_rate_refs": [price["id"]],
            "required_price_meters": ["calls"],
        }
        self.connectors.register_rate_policy({
            "schema_version": "0.1",
            "id": "connector-rate-policy:sec-public:v1",
            "created_at": authority_created_at,
            "policy_ref": _RATE_POLICY_REF,
            "quota_scope_ref": "connector-quota-scope:sec-public",
            "version": 1,
            "prior_version_ref": None,
            "connector_profile_ref": profile["id"],
            "window_seconds": 60,
            "reset_timezone": "UTC",
            "max_concurrency": 2,
            "quota_currency": "USD",
            "price_rate_refs": price_book["price_rate_refs"],
            "required_price_meters": price_book["required_price_meters"],
            "price_book_hash": content_hash(price_book),
            "limits": {
                "calls": 2,
                "bytes": plan_wire["execution_scope"]["budget"]["max_response_bytes"],
                "records": 100,
                "cost_micros": 1000,
            },
            "effective_from": authority_created_at,
            "effective_until": None,
            "actor_ref": self.actor_ref,
        }, idempotency_key="connector-rate-policy:sec-public:v1")

        return {
            "template": template,
            "identity": identity,
            "descriptor": descriptor,
            "lease": lease,
            "profile": profile,
            "call_spec": call_spec,
            "invocation": invocation,
            "execution": execution_wire,
            "parameters": parameters,
        }

    def _bridge_records(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the deterministic compiled-plan authority bridge records.

        The bridge records are a pure projection of immutable plan authority
        plus the registered SEC runtime authority: the CompiledConnectorPlan,
        the read-only ClaimIndex snapshot and the ContextPack.  Storing them
        idempotently means replay cannot fork a second authority.
        """

        identity = authority["identity"]
        created_at = plan_wire["created_at"]
        plan_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
        }
        compiled_step_spec = {
            "source_ref": identity["source_identity"]["source_ref"],
            "source_hash": identity["source_hash"],
            "connector_profile_ref": authority["profile"]["id"],
            "connector_profile_hash": authority["profile"]["content_hash"],
            "operation": identity["operation"],
            "parameters": authority["parameters"],
            "input_schema_ref": identity["input_schema_ref"],
            "input_schema_hash": identity["input_schema_hash"],
            "output_schema_ref": identity["output_schema_ref"],
            "output_schema_hash": identity["output_schema_hash"],
            "completeness_required": identity["completeness_required"],
            "depends_on": [],
            "fallback_step_refs": [],
            "max_attempts": step["max_attempts"],
        }
        compiled_plan = build_compiled_connector_plan(
            task_ref=work_order["id"],
            task_hash=content_hash(work_order),
            planner_ref=PLANNER_REF,
            planner_hash=PLANNER_HASH,
            routing_policy_ref="routing:research-plan:" + plan_wire["id"],
            routing_policy_hash=content_hash({
                **plan_identity,
                "routing": "sec-public-" + identity["operation"],
            }),
            step_specs=[compiled_step_spec],
            created_at=created_at,
        )
        self.connector_records.store_plan(compiled_plan)

        cursor = self.plan.connection.cursor()
        mandate = read_exact_mandate_version(
            cursor, plan_wire["agenda_binding"]["mandate_version_ref"]
        )
        claim_index = build_claim_index(
            ledger=self.plan.store, created_at=created_at
        )
        context = build_context_pack(
            [{
                "kind": "mandate",
                "ref": mandate["id"],
                "hash": mandate["content_hash"],
                "priority": 100,
                "content": mandate["objective"],
            }],
            task_ref=work_order["id"],
            task_hash=content_hash(work_order),
            compiled_plan_ref=compiled_plan["id"],
            compiled_plan_hash=compiled_plan["content_hash"],
            claim_index_ref=claim_index["id"],
            claim_index_hash=claim_index["content_hash"],
            created_at=created_at,
            max_tokens=100,
            max_bytes=2_000,
        )
        self.connector_records.store_context_pack(context)
        return {
            "compiled_plan": compiled_plan,
            "compiled_step": compiled_plan["steps"][0],
            "claim_index": claim_index,
            "context_pack": context,
        }

    def _runner_requests(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        authority: Mapping[str, Any],
        bridge: Mapping[str, Any],
        *,
        claim: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build the compiled and actual runner request wires.

        The compiled request carries the compiled-plan binding (it is what
        the checkpoint/resolver/verifier read); the actual request strips the
        compiled binding and re-binds the CallSpec hash, which is the only
        wire the ConnectorRunnerAdmissionGate accepts.
        """

        attempt_number = claim["attempt"]["attempt_number"]
        request_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
            "scheduler_attempt_number": attempt_number,
        }
        request_id = _derived_ref("connector-runner-request:research-plan", request_identity)
        compiled_step = bridge["compiled_step"]
        profile = authority["profile"]
        call_spec = authority["call_spec"]
        lease = authority["lease"]
        invocation = authority["invocation"]
        base = {
            "schema_version": "0.1",
            "id": request_id,
            "created_at": _wire_time(self.clock()),
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "execution_ref": invocation["execution_ref"],
            "execution_hash": invocation["execution_hash"],
            "work_order_ref": work_order["id"],
            "work_order_hash": content_hash(work_order),
            "scheduler_attempt_number": attempt_number,
            "scheduler_lease_revision_ref": claim["lease"]["id"],
            "scheduler_lease_hash": claim["lease"]["content_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "call_spec_ref": call_spec["id"],
            "call_spec_hash": content_hash({
                "step_ref": compiled_step["id"],
                "operation": compiled_step["operation"],
                "parameters": compiled_step["parameters"],
            }),
            "capability_lease_ref": lease.id,
            "capability_lease_hash": lease.content_hash,
            "principal_ref": self.principal_ref,
            "runner_runtime_ref": profile["runner_runtime_ref"],
            "runner_actor_ref": profile["runner_actor_ref"],
            "runner_environment_hash": profile["runner_environment_hash"],
            "compiled_connector_plan_ref": bridge["compiled_plan"]["id"],
            "compiled_connector_plan_hash": bridge["compiled_plan"]["content_hash"],
            "compiled_step_ref": compiled_step["id"],
            "compiled_step_hash": compiled_step["content_hash"],
            "idempotency_key": f"research-plan:{plan_wire['id']}:connector:{attempt_number}",
        }
        compiled = validate_connector_runner_request(
            {**base, "content_hash": content_hash(base)}
        )
        self.connector_records.store_runner_request(compiled)

        actual_base = {key: value for key, value in base.items() if key != "content_hash"}
        actual_base["id"] = _derived_ref(
            "connector-runner-request:research-plan", {
                **request_identity, "wire": "actual",
            }
        )
        actual_base["idempotency_key"] = (
            f"research-plan:{plan_wire['id']}:connector:{attempt_number}:actual"
        )
        actual_base["call_spec_hash"] = call_spec["content_hash"]
        actual_base.pop("compiled_connector_plan_ref", None)
        actual_base.pop("compiled_connector_plan_hash", None)
        actual_base.pop("compiled_step_ref", None)
        actual_base.pop("compiled_step_hash", None)
        actual = validate_connector_runner_request(
            {**actual_base, "content_hash": content_hash(actual_base)}
        )
        return {"compiled": compiled, "actual": actual}

    def _receipt_wire(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        request_identity: Mapping[str, Any],
        requests: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt_id = _derived_ref(
            "connector-completion-receipt:research-plan", request_identity
        )
        receipt = {
            "schema_version": "0.2",
            "id": receipt_id,
            "created_at": response["created_at"],
            "runner_request_ref": requests["compiled"]["id"],
            "runner_request_hash": requests["compiled"]["content_hash"],
            "actual_runner_request_ref": requests["actual"]["id"],
            "actual_runner_request_hash": requests["actual"]["content_hash"],
            "status": response["outcome"],
            "result_ref": response["result_envelope_ref"],
            "result_hash": response["result_envelope_hash"],
            "source_envelopes": [] if response["source_envelope_ref"] is None else [{
                "ref": response["source_envelope_ref"],
                "hash": response["source_envelope_hash"],
            }],
            "artifacts": [] if response["raw_artifact_version_ref"] is None else [{
                "ref": response["raw_artifact_version_ref"],
                "hash": response["raw_artifact_version_hash"],
            }],
            "next_cursor": None,
            "error_code": None,
            "retry_after_ms": None,
        }
        receipt["content_hash"] = content_hash(receipt)
        return validate_connector_completion_receipt(receipt)

    def _store_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        self._inject("before_receipt_store")
        stored = self.connector_records.store_completion_receipt(receipt)
        self._inject("after_receipt_store")
        if stored["content_hash"] != receipt["content_hash"]:
            raise ResearchPlanExecutorConflict(
                "connector completion receipt store drifted"
            )
        return stored

    def observability_artifact(self, artifact_ref: str) -> dict[str, Any]:
        row = self.plan.connection.execute(
            "SELECT v.record_json,v.content_hash FROM observability_artifact_versions_v2 v "
            "WHERE v.version_id=?",
            (artifact_ref,),
        ).fetchone()
        if row is None:
            raise ResearchPlanExecutorConflict(
                f"ArtifactVersion row is missing for {artifact_ref}"
            )
        wire = json.loads(row["record_json"])
        if wire.get("content_hash") != row["content_hash"]:
            raise ResearchPlanExecutorConflict(
                "ArtifactVersion row drifted from its stored hash"
            )
        return wire

    def _ensure_receipt_from_authority(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        formal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Rebuild and store the receipt after a crash between transport and admit.

        The receipt is fully re-derivable from stored authority: the formal
        ResultEnvelope, the source envelope row, the artifact row and the
        compiled/actual runner request rows.  The rebuilt wire is byte-
        identical to the one the happy path stores, so the idempotent store
        converges on the same row.
        """

        try:
            envelope = formal["result_envelope"]
        except (TypeError, ValueError) as exc:
            raise ResearchPlanExecutorConflict(
                "connector formal result embeds invalid ResultEnvelope JSON"
            ) from exc
        source_envelope_ref = envelope["outputs"].get("source_envelope_ref")
        if not isinstance(source_envelope_ref, str) or not source_envelope_ref:
            raise ResearchPlanExecutorConflict(
                "connector ResultEnvelope lacks its source envelope output"
            )
        source_row = self.connectors.connection.execute(
            "SELECT record_json,content_hash FROM connector_source_envelopes "
            "WHERE source_envelope_id=?",
            (source_envelope_ref,),
        ).fetchone()
        if source_row is None:
            raise ResearchPlanExecutorConflict(
                "connector ResultEnvelope source envelope row is missing"
            )
        artifact_refs = envelope.get("artifact_refs") or []
        if len(artifact_refs) != 1:
            raise ResearchPlanExecutorConflict(
                "connector ResultEnvelope lacks its exact raw artifact output"
            )
        artifact = self.observability_artifact(artifact_refs[0])
        request_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
            "scheduler_attempt_number": formal["attempt_number"],
        }
        compiled_row = self.connector_records.connection.execute(
            "SELECT request_json,content_hash FROM research_runner_requests "
            "WHERE runner_request_id=?",
            (_derived_ref("connector-runner-request:research-plan", request_identity),),
        ).fetchone()
        if compiled_row is None:
            raise ResearchPlanExecutorConflict(
                "compiled runner request row is missing for the succeeded connector"
            )
        compiled = validate_connector_runner_request(
            json.loads(compiled_row["request_json"])
        )
        if compiled["content_hash"] != compiled_row["content_hash"]:
            raise ResearchPlanExecutorConflict(
                "compiled runner request row drifted from its stored hash"
            )
        actual_id = _derived_ref("connector-runner-request:research-plan", {
            **request_identity, "wire": "actual",
        })
        actual_row = self.plan.connection.execute(
            "SELECT request_hash,request_json FROM runner_request_journal "
            "WHERE runner_request_ref=?",
            (actual_id,),
        ).fetchone()
        if actual_row is None:
            raise ResearchPlanExecutorConflict(
                "actual runner request is absent from the Core runner journal"
            )
        actual = validate_connector_runner_request(
            json.loads(actual_row["request_json"])
        )
        if actual["content_hash"] != actual_row["request_hash"]:
            raise ResearchPlanExecutorConflict(
                "actual runner request journal row drifted from its stored hash"
            )
        response = {
            "created_at": envelope["created_at"],
            "outcome": "succeeded",
            "result_envelope_ref": envelope["id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "source_envelope_ref": source_envelope_ref,
            "source_envelope_hash": source_row["content_hash"],
            "raw_artifact_version_ref": artifact_refs[0],
            "raw_artifact_version_hash": artifact["content_hash"],
        }
        return self._store_receipt(self._receipt_wire(
            plan_wire, step, request_identity,
            {"compiled": compiled, "actual": actual}, response,
        ))

    def _execute_connector(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
    ) -> dict[str, Any]:
        formal = self.scheduler.formal_result(work_order["id"])
        if formal is not None:
            if formal["terminal_state"] != "succeeded":
                return {
                    "status": "blocked",
                    "plan_version_ref": plan_wire["id"],
                    "work_order_ref": work_order["id"],
                    "stage": step["stage"],
                }
            self._ensure_receipt_from_authority(plan_wire, step, work_order, formal)
            return {"status": "succeeded", "plan_version_ref": plan_wire["id"]}

        # This thin slice can safely recover a crash that happened before the
        # connector was dispatched.  A Scheduler retry after any physical
        # connector attempt would require advancing the generic execution's
        # single artifact output binding; that protocol is not implemented
        # here, so redispatch fails closed instead of inventing a second
        # logical invocation or rebinding the existing execution.
        invocation_id = _derived_ref("connector-invocation:research-plan", {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
        })
        physical_attempt = self.connectors.connection.execute(
            "SELECT 1 FROM connector_physical_attempts "
            "WHERE connector_invocation_ref=? LIMIT 1",
            (invocation_id,),
        ).fetchone()
        if physical_attempt is not None:
            raise ResearchPlanExecutorConflict(
                "connector redispatch after a physical attempt is unsupported"
            )

        claim = self.scheduler.claim(self.actor_ref, work_order_id=work_order["id"])
        if claim is None:
            return {
                "status": "pending",
                "plan_version_ref": plan_wire["id"],
                "work_order_ref": work_order["id"],
                "reason": "not_claimable",
            }
        attempt_number = claim["attempt"]["attempt_number"]
        authority = self._connector_authority(
            plan_wire, step, work_order, scheduler_attempt_number=attempt_number
        )
        bridge = self._bridge_records(plan_wire, step, work_order, authority)
        requests = self._runner_requests(
            plan_wire, step, work_order, authority, bridge, claim=claim
        )
        self._inject("before_transport")
        response = self.transport.execute(
            requests["actual"], scheduler_lease_token=claim["lease_token"]
        )
        self._inject("after_transport")
        if response["outcome"] != "succeeded":
            return {
                "status": response["outcome"],
                "plan_version_ref": plan_wire["id"],
                "work_order_ref": work_order["id"],
                "result_envelope_ref": response["result_envelope_ref"],
                "retry_at": response["retry_at"],
            }
        request_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
            "scheduler_attempt_number": attempt_number,
        }
        receipt = self._receipt_wire(
            plan_wire, step, request_identity, requests, response
        )
        self._store_receipt(receipt)
        # The transport itself persisted the Scheduler formal result +
        # ResultEnvelope; fail closed if the exact binding is not visible.
        persisted = self.scheduler.formal_result(work_order["id"])
        if (
            persisted is None
            or persisted["terminal_state"] != "succeeded"
            or persisted["result_envelope_id"] != response["result_envelope_ref"]
            or persisted["result_envelope_hash"] != response["result_envelope_hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "connector Scheduler formal result does not bind the transport result"
            )
        return {
            "status": "succeeded",
            "plan_version_ref": plan_wire["id"],
            "source_envelope_ref": response["source_envelope_ref"],
            "receipt_ref": receipt["id"],
        }

    # ------------------------------------------------------------------ #
    # Internal nodes

    def _re_read_bridge_authority(
        self, plan_wire: Mapping[str, Any], connector_step: Mapping[str, Any],
        *, attempt_number: int,
    ) -> dict[str, Any]:
        """Re-read the typed bridge records and fail closed on any drift.

        The deterministic ids are recomputed from plan authority and compared
        against the stored rows; every row is re-validated through its typed
        validator so a tampered record cannot be mistaken for authority.
        """

        request_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": connector_step["id"],
            "scheduler_attempt_number": attempt_number,
        }
        request_id = _derived_ref(
            "connector-runner-request:research-plan", request_identity
        )
        receipt_id = _derived_ref(
            "connector-completion-receipt:research-plan", request_identity
        )
        checkpoint_id = _derived_ref("research-checkpoint:research-plan", {
            "plan_version_ref": plan_wire["id"],
            "step_ref": connector_step["id"],
            "connector_attempt_number": attempt_number,
        })

        def read_record(
            table: str, id_column: str, identifier: str, name: str,
            json_column: str,
            validator: Callable[[Mapping[str, Any]], dict[str, Any]],
        ) -> dict[str, Any]:
            found = self.connector_records.connection.execute(
                f"SELECT {json_column},content_hash FROM {table} WHERE {id_column}=?",
                (identifier,),
            ).fetchone()
            if found is None:
                raise ResearchPlanExecutorConflict(
                    f"{name} row is missing from connector authority"
                )
            try:
                raw = json.loads(found[json_column])
            except (TypeError, ValueError) as exc:
                raise ResearchPlanExecutorConflict(
                    f"{name} record_json is not valid JSON"
                ) from exc
            validated = validator(raw)
            if (
                canonical_json(validated) != found[json_column]
                or validated["content_hash"] != found["content_hash"]
            ):
                raise ResearchPlanExecutorConflict(
                    f"{name} record drifted from its stored hash"
                )
            return validated

        checkpoint = read_record(
            "research_checkpoints", "checkpoint_id", checkpoint_id,
            "ResearchCheckpoint", "checkpoint_json", validate_research_checkpoint,
        )
        compiled_plan = read_record(
            "compiled_connector_plans", "plan_id", checkpoint["compiled_plan_ref"],
            "CompiledConnectorPlan", "plan_json", validate_compiled_connector_plan,
        )
        context_pack = read_record(
            "context_packs", "context_pack_id", checkpoint["context_pack_ref"],
            "ContextPack", "context_pack_json", validate_context_pack,
        )
        compiled_step = next(
            (item for item in compiled_plan["steps"] if item["id"] == checkpoint["step_ref"]),
            None,
        )
        if compiled_step is None:
            raise ResearchPlanExecutorConflict(
                "checkpoint step is not a node of its compiled plan"
            )
        compiled_step = validate_compiled_connector_step(compiled_step)

        def validate_request(raw: Mapping[str, Any]) -> dict[str, Any]:
            return validate_runner_request_plan_binding(raw, compiled_plan, compiled_step)

        runner_request = read_record(
            "research_runner_requests", "runner_request_id",
            checkpoint["runner_request_ref"], "ConnectorRunnerRequest",
            "request_json", validate_request,
        )
        receipt = read_record(
            "research_completion_receipts", "receipt_id",
            checkpoint["completion_receipt_ref"], "ConnectorCompletionReceipt",
            "receipt_json", validate_connector_completion_receipt,
        )
        if (
            checkpoint["id"] != checkpoint_id
            or checkpoint["runner_request_ref"] != request_id
            or checkpoint["completion_receipt_ref"] != receipt_id
            or checkpoint["connector_attempt_number"] != attempt_number
            or runner_request["id"] != request_id
            or receipt["id"] != receipt_id
            or runner_request["scheduler_attempt_number"] != attempt_number
            or context_pack["task_ref"] != runner_request["work_order_ref"]
            or context_pack["task_hash"] != runner_request["work_order_hash"]
            or receipt["runner_request_ref"] != runner_request["id"]
            or receipt["runner_request_hash"] != runner_request["content_hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "bridge authority records do not bind the exact plan step"
            )
        return {
            "checkpoint": checkpoint,
            "compiled_plan": compiled_plan,
            "compiled_step": compiled_step,
            "context_pack": context_pack,
            "runner_request": runner_request,
            "receipt": receipt,
            "checkpoint_id": checkpoint_id,
            "receipt_id": receipt_id,
        }

    def _checkpoint(
        self,
        plan_wire: Mapping[str, Any],
        connector_step: Mapping[str, Any],
        bridge: Mapping[str, Any],
        requests: Mapping[str, Any],
        receipt: Mapping[str, Any],
        *,
        attempt_number: int,
    ) -> dict[str, Any]:
        checkpoint_id = _derived_ref("research-checkpoint:research-plan", {
            "plan_version_ref": plan_wire["id"],
            "step_ref": connector_step["id"],
            "connector_attempt_number": attempt_number,
        })
        checkpoint = {
            "schema_version": "0.1",
            "id": checkpoint_id,
            "created_at": receipt["created_at"],
            "run_ref": "research-plan-run:" + plan_wire["id"],
            "attempt_ref": "research-attempt:" + plan_wire["id"],
            "attempt_hash": content_hash({
                "plan_version_ref": plan_wire["id"],
                "connector_attempt_number": attempt_number,
            }),
            "sequence": 1,
            "compiled_plan_ref": bridge["compiled_plan"]["id"],
            "compiled_plan_hash": bridge["compiled_plan"]["content_hash"],
            "context_pack_ref": bridge["context_pack"]["id"],
            "context_pack_hash": bridge["context_pack"]["content_hash"],
            "step_ref": bridge["compiled_step"]["id"],
            "step_hash": bridge["compiled_step"]["content_hash"],
            "connector_attempt_number": attempt_number,
            "runner_request_ref": requests["compiled"]["id"],
            "runner_request_hash": requests["compiled"]["content_hash"],
            "completion_receipt_ref": receipt["id"],
            "completion_receipt_hash": receipt["content_hash"],
            "authority_bindings": {
                "connector_profile_ref": requests["compiled"]["connector_profile_ref"],
                "connector_profile_hash": requests["compiled"]["connector_profile_hash"],
                "capability_lease_ref": requests["compiled"]["capability_lease_ref"],
                "capability_lease_hash": requests["compiled"]["capability_lease_hash"],
                "source_ref": bridge["compiled_step"]["source_ref"],
                "source_hash": bridge["compiled_step"]["source_hash"],
            },
            "outcome": receipt["status"],
            "source_envelopes": receipt["source_envelopes"],
            "artifacts": receipt["artifacts"],
            "next_cursor": receipt["next_cursor"],
            "retry_after_ms": receipt["retry_after_ms"],
            "idempotency_key": requests["compiled"]["idempotency_key"],
            "prior_checkpoint_ref": None,
            "prior_checkpoint_hash": None,
        }
        checkpoint["content_hash"] = content_hash(checkpoint)
        wire = validate_research_checkpoint(checkpoint)
        self._inject("before_checkpoint_store")
        stored = self.connector_records.append_checkpoint(wire)
        self._inject("after_checkpoint_store")
        if stored["content_hash"] != wire["content_hash"]:
            raise ResearchPlanExecutorConflict("ResearchCheckpoint store drifted")
        return wire

    def _numeric_spec(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        material: Mapping[str, Any],
    ) -> dict[str, Any]:
        scope = plan_wire["execution_scope"]
        request = scope["parameters"]
        if scope["operation"] == SEC_COMPANY_FACTS_OPERATION:
            payload = material["normalized_payload"]
            current = payload["current"]
            prior = payload["prior"]
            period = f"{current['start']}..{current['end']}"
            growth = format(Decimal(payload["growth_percent"]), "f")
            if "." in growth:
                growth = growth.rstrip("0").rstrip(".")
            spec = {
                "schema_version": "0.1",
                "id": _derived_ref("numeric-spec:research-plan", {
                    "plan_version_ref": plan_wire["id"],
                    "step_ref": step["id"],
                }),
                "created_at": material["retrieved_at"],
                "operator": "growth_percentage",
                "inputs": [
                    {
                        "name": "current_quarter",
                        "value": current["value"],
                        "unit": "number",
                        "currency": None,
                        "scale": "one",
                        "period": period,
                        "source_material_ref": material["id"],
                        "source_material_hash": material["content_hash"],
                        "json_pointer": "/current/value",
                        "extractor": "number",
                    },
                    {
                        "name": "prior_year_quarter",
                        "value": prior["value"],
                        "unit": "number",
                        "currency": None,
                        "scale": "one",
                        "period": f"{prior['start']}..{prior['end']}",
                        "source_material_ref": material["id"],
                        "source_material_hash": material["content_hash"],
                        "json_pointer": "/prior/value",
                        "extractor": "number",
                    },
                ],
                "output_value": growth,
                "output_unit": "percent",
                "output_currency": None,
                "output_scale": "one",
                "output_period": period,
                "rounding": {"mode": "half_up", "digits": 2},
            }
            spec["content_hash"] = content_hash(spec)
            return validate_numeric_verification_spec(spec)
        if scope["operation"] != SEC_OPERATION:
            raise ResearchPlanExecutorConflict(
                "numeric verifier operation is outside the approved SEC reads"
            )
        count = len(material["source_record_refs"])
        period = f"{request['filing_date_from']}..{request['filing_date_to']}"
        spec = {
            "schema_version": "0.1",
            "id": _derived_ref("numeric-spec:research-plan", {
                "plan_version_ref": plan_wire["id"],
                "step_ref": step["id"],
            }),
            "created_at": material["retrieved_at"],
            "operator": "identity",
            "inputs": [{
                "name": "filing_count",
                "value": str(count),
                "unit": "records",
                "currency": None,
                "scale": "one",
                "period": period,
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": "/records",
                "extractor": "count",
            }],
            "output_value": str(count),
            "output_unit": "records",
            "output_currency": None,
            "output_scale": "one",
            "output_period": period,
            "rounding": _ROUNDING,
        }
        spec["content_hash"] = content_hash(spec)
        return validate_numeric_verification_spec(spec)

    def _material_and_bundles(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        bridge: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Re-resolve the exact connector authority and verify material/bundles.

        This is the deterministic recomputation shared by the verifier and
        candidate-staging nodes; the staging node additionally fails closed
        when the recomputed bundle refs/hashes differ from the upstream
        verifier stage output records.
        """

        source_envelope_ref = bridge["checkpoint"]["source_envelopes"][0]["ref"]
        resolved = self.resolver.resolve(
            source_envelope_ref, checkpoint_ref=bridge["checkpoint_id"]
        )
        summary = validate_authority_resolution(resolved.summary)
        if (
            summary["source_envelope_ref"] != source_envelope_ref
            or summary["checkpoint_ref"] != bridge["checkpoint_id"]
            or summary["checkpoint_hash"] != bridge["checkpoint"]["content_hash"]
            or summary["receipt_ref"] != bridge["receipt"]["id"]
            or summary["receipt_hash"] != bridge["receipt"]["content_hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "authority resolution does not bind the exact plan checkpoint"
            )
        material = build_authority_source_material(resolved)
        source_bundle = verify_authority_source_material(
            material,
            resolver=self.resolver,
            checkpoint=bridge["checkpoint"],
            plan=bridge["compiled_plan"],
            context_pack=bridge["context_pack"],
            step=bridge["compiled_step"],
            runner_request=bridge["runner_request"],
            receipt=bridge["receipt"],
        )
        if source_bundle["verdict"] != "pass":
            return {
                "status": "rejected",
                "material": material,
                "source_bundle": source_bundle,
                "numeric_spec": None,
                "numeric_bundle": None,
            }
        numeric_spec = self._numeric_spec(plan_wire, step, material)
        numeric_bundle = verify_numeric_spec(
            numeric_spec,
            checkpoint_ref=bridge["checkpoint"]["id"],
            checkpoint_hash=bridge["checkpoint"]["content_hash"],
            source_material=material,
            source_bundle=source_bundle,
        )
        if numeric_bundle["verdict"] != "pass":
            return {
                "status": "rejected",
                "material": material,
                "source_bundle": source_bundle,
                "numeric_spec": numeric_spec,
                "numeric_bundle": numeric_bundle,
            }
        return {
            "status": "verified",
            "authority_resolution": summary,
            "material": material,
            "source_bundle": source_bundle,
            "numeric_spec": numeric_spec,
            "numeric_bundle": numeric_bundle,
        }

    def _complete_internal(
        self,
        plan_wire: Mapping[str, Any],
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        upstream_work_order: Mapping[str, Any],
        upstream_formal: Mapping[str, Any],
        records: Sequence[Mapping[str, Any]],
        *,
        status: str = "succeeded",
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist the Scheduler formal result + ResultEnvelope + stage proof."""

        created_at = _wire_time(self.clock())
        proof = stage_proof_wire(
            plan_wire=plan_wire,
            step=step,
            upstream_work_order=upstream_work_order,
            upstream_formal=upstream_formal,
            records=records,
            created_at=created_at,
        )
        result = ResultEnvelope(
            schema_version="0.1",
            id=_derived_ref("result-envelope:research-plan", {
                "plan_version_ref": plan_wire["id"],
                "step_ref": step["id"],
            }),
            created_at=created_at,
            work_order_ref=work_order["id"],
            invocation_ref=_derived_ref("execution:research-plan", {
                "plan_version_ref": plan_wire["id"],
                "step_ref": step["id"],
            }),
            status=status,
            outputs=proof,
            actual_side_effects=(),
            usage_refs=(),
            artifact_refs=(),
            error=error,
            metadata={},
        )
        wire = result.to_dict()
        result_hash = content_hash(wire)
        claim = self.scheduler.claim(self.actor_ref, work_order_id=work_order["id"])
        if claim is None:
            existing = self.scheduler.formal_result(work_order["id"])
            if existing is not None:
                return {"status": "already_completed", "formal": existing}
            return {
                "status": "pending",
                "plan_version_ref": plan_wire["id"],
                "work_order_ref": work_order["id"],
                "reason": "not_claimable",
            }
        attempt_number = claim["attempt"]["attempt_number"]
        self._inject("before_internal_complete")
        completed = self.scheduler.complete(
            work_order["id"],
            attempt_number,
            self.actor_ref,
            claim["lease_token"],
            wire,
            idempotency_key=f"research-plan:{plan_wire['id']}:{step['ordinal']}:complete",
            result_envelope_hash=result_hash,
        )
        self._inject("after_internal_complete")
        if completed["status"] != "fresh":
            raise ResearchPlanExecutorConflict(
                "internal step Scheduler completion did not converge"
            )
        return {
            "status": "succeeded",
            "plan_version_ref": plan_wire["id"],
            "result_envelope_ref": wire["id"],
            "result_envelope_hash": result_hash,
        }

    def _execute_internal(
        self,
        plan_wire: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        work_orders: Sequence[Mapping[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        step = steps[index]
        work_order = work_orders[index]
        upstream = work_orders[index - 1]
        upstream_formal = self.scheduler.formal_result(upstream["id"])
        if upstream_formal is None or upstream_formal["terminal_state"] != "succeeded":
            raise ResearchPlanExecutorConflict(
                "internal step upstream node is not terminally succeeded"
            )
        if index == 1:
            return self._execute_resolver(
                plan_wire, steps, work_orders, index, step, work_order,
                upstream, upstream_formal,
            )
        if index == 2:
            return self._execute_verifier(
                plan_wire, steps, work_orders, index, step, work_order,
                upstream, upstream_formal,
            )
        if index == 3:
            return self._execute_staging(
                plan_wire, steps, work_orders, index, step, work_order,
                upstream, upstream_formal,
            )
        raise ResearchPlanExecutorConflict(
            "research plan tree contains an unsupported node ordinal"
        )

    def _connector_attempt(self, plan_wire: Mapping[str, Any]) -> int:
        work_orders = self._work_orders(plan_wire)
        formal = self.scheduler.formal_result(work_orders[0]["id"])
        if formal is None or formal["terminal_state"] != "succeeded":
            raise ResearchPlanExecutorConflict(
                "connector node is not terminally succeeded"
            )
        return int(formal["attempt_number"])

    def _audit_completed_tree(
        self,
        plan_wire: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        work_orders: Sequence[Mapping[str, Any]],
    ) -> None:
        """Re-read every typed authority before reporting a completed tree.

        A terminal Scheduler row is not by itself proof that the typed rows
        behind its stage-output refs still exist and remain canonical.  This
        audit is read-only: the coordinator re-verifies the final admission
        chain, the resolver re-reads connector/checkpoint authority, and the
        candidate store rows are validated against deterministic
        recomputation.  Any post-completion tamper therefore fails closed
        instead of being hidden by an early ``complete`` return.
        """

        outcome = self.coordinator.admit_next_work_order(
            plan_version_ref=plan_wire["id"],
            upstream_work_order_ref=work_orders[-1]["id"],
        )
        if outcome.get("status") != "complete":
            raise ResearchPlanExecutorConflict(
                "completed plan tree did not re-verify as coordinator-complete"
            )

        connector_formal = self.scheduler.formal_result(work_orders[0]["id"])
        if connector_formal is None or connector_formal["terminal_state"] != "succeeded":
            raise ResearchPlanExecutorConflict(
                "completed plan lost its connector formal result"
            )
        bridge = self._re_read_bridge_authority(
            plan_wire, steps[0], attempt_number=int(connector_formal["attempt_number"])
        )
        recomputed = self._material_and_bundles(plan_wire, steps[2], bridge)
        if recomputed["status"] != "verified":
            raise ResearchPlanExecutorConflict(
                "completed plan no longer re-verifies its source/numeric authority"
            )

        resolver_formal = self.scheduler.formal_result(work_orders[1]["id"])
        verifier_formal = self.scheduler.formal_result(work_orders[2]["id"])
        staging_formal = self.scheduler.formal_result(work_orders[3]["id"])
        if any(item is None for item in (
            resolver_formal, verifier_formal, staging_formal
        )):
            raise ResearchPlanExecutorConflict(
                "completed plan is missing an internal formal result"
            )

        resolver_records = re_read_stage_records(
            resolver_formal,
            expected_kinds=_STAGE_RECORD_KINDS["authority_resolver"],
        )
        if resolver_records[0] != {
            "kind": "authority_resolution",
            "ref": recomputed["authority_resolution"]["id"],
            "hash": recomputed["authority_resolution"]["content_hash"],
        }:
            raise ResearchPlanExecutorConflict(
                "completed resolver proof drifted from typed authority"
            )

        verifier_records = re_read_stage_records(
            verifier_formal,
            expected_kinds=_STAGE_RECORD_KINDS["verifier"],
        )
        expected_verifications = {
            "source_verification": recomputed["source_bundle"],
            "numeric_verification": recomputed["numeric_bundle"],
        }
        for record in verifier_records:
            expected = expected_verifications[record["kind"]]
            if (record["ref"], record["hash"]) != (
                expected["id"], expected["content_hash"]
            ):
                raise ResearchPlanExecutorConflict(
                    "completed verifier proof drifted from typed authority"
                )
            row = self.staging.connection.execute(
                "SELECT record_json,content_hash FROM candidate_verifications "
                "WHERE verification_id=?",
                (record["ref"],),
            ).fetchone()
            if row is None:
                raise ResearchPlanExecutorConflict(
                    "completed plan is missing a staged verification record"
                )
            verified = validate_verification_bundle(json.loads(row["record_json"]))
            if (
                canonical_json(verified) != row["record_json"]
                or verified["content_hash"] != row["content_hash"]
                or canonical_json(verified) != canonical_json(expected)
            ):
                raise ResearchPlanExecutorConflict(
                    "staged verification record drifted from deterministic authority"
                )

        staging_records = re_read_stage_records(
            staging_formal,
            expected_kinds=_STAGE_RECORD_KINDS["candidate_staging"],
        )
        typed_candidates = {
            "candidate_evidence": (
                "candidate_evidence_versions", "version_id",
                validate_candidate_evidence,
            ),
            "candidate_claim": (
                "candidate_claim_versions", "version_id",
                validate_candidate_claim,
            ),
        }
        for record in staging_records:
            table, id_column, validator = typed_candidates[record["kind"]]
            row = self.staging.connection.execute(
                f"SELECT record_json,content_hash FROM {table} WHERE {id_column}=?",
                (record["ref"],),
            ).fetchone()
            if row is None:
                raise ResearchPlanExecutorConflict(
                    "completed plan is missing a staged candidate record"
                )
            candidate = validator(json.loads(row["record_json"]))
            if (
                canonical_json(candidate) != row["record_json"]
                or candidate["content_hash"] != row["content_hash"]
                or candidate["content_hash"] != record["hash"]
            ):
                raise ResearchPlanExecutorConflict(
                    "staged candidate record drifted from its stage proof"
                )

    def _execute_resolver(
        self,
        plan_wire: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        work_orders: Sequence[Mapping[str, Any]],
        index: int,
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        upstream: Mapping[str, Any],
        upstream_formal: Mapping[str, Any],
    ) -> dict[str, Any]:
        connector_step = steps[0]
        try:
            envelope = upstream_formal["result_envelope"]
        except (TypeError, ValueError) as exc:
            raise ResearchPlanExecutorConflict(
                "connector formal result embeds invalid ResultEnvelope JSON"
            ) from exc
        source_envelope_ref = envelope["outputs"].get("source_envelope_ref")
        if not isinstance(source_envelope_ref, str) or not source_envelope_ref:
            raise ResearchPlanExecutorConflict(
                "connector ResultEnvelope lacks its source envelope output"
            )
        attempt_number = upstream_formal["attempt_number"]
        request_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": connector_step["id"],
            "scheduler_attempt_number": attempt_number,
        }
        receipt_row = self.connector_records.connection.execute(
            "SELECT receipt_json,content_hash FROM research_completion_receipts "
            "WHERE receipt_id=?",
            (_derived_ref("connector-completion-receipt:research-plan", request_identity),),
        ).fetchone()
        if receipt_row is None:
            raise ResearchPlanExecutorConflict(
                "connector completion receipt is missing for the resolver node"
            )
        receipt = validate_connector_completion_receipt(
            json.loads(receipt_row["receipt_json"])
        )
        if (
            receipt["content_hash"] != receipt_row["content_hash"]
            or receipt["result_ref"] != envelope["id"]
            or receipt["result_hash"] != upstream_formal["result_envelope_hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "connector completion receipt does not bind the formal result"
            )
        compiled_row = self.connector_records.connection.execute(
            "SELECT request_json,content_hash FROM research_runner_requests "
            "WHERE runner_request_id=?",
            (_derived_ref("connector-runner-request:research-plan", request_identity),),
        ).fetchone()
        if compiled_row is None:
            raise ResearchPlanExecutorConflict(
                "compiled runner request is missing for the resolver node"
            )
        compiled = validate_connector_runner_request(
            json.loads(compiled_row["request_json"])
        )
        if (
            compiled["content_hash"] != compiled_row["content_hash"]
            or compiled["work_order_ref"] != upstream["id"]
        ):
            raise ResearchPlanExecutorConflict(
                "compiled runner request drifted from the connector node"
            )
        authority = self._connector_authority(
            plan_wire, connector_step, upstream,
            scheduler_attempt_number=attempt_number,
        )
        bridge = self._bridge_records(plan_wire, connector_step, upstream, authority)
        checkpoint = self._checkpoint(
            plan_wire, connector_step, bridge,
            {"compiled": compiled, "actual": None}, receipt,
            attempt_number=attempt_number,
        )
        resolved = self.resolver.resolve(
            source_envelope_ref, checkpoint_ref=checkpoint["id"]
        )
        summary = validate_authority_resolution(resolved.summary)
        if (
            summary["source_envelope_ref"] != source_envelope_ref
            or summary["receipt_ref"] != receipt["id"]
            or summary["receipt_hash"] != receipt["content_hash"]
            or summary["checkpoint_ref"] != checkpoint["id"]
        ):
            raise ResearchPlanExecutorConflict(
                "authority resolution does not bind the exact connector receipt"
            )
        records = [{
            "kind": "authority_resolution",
            "ref": summary["id"],
            "hash": summary["content_hash"],
        }]
        return self._complete_internal(
            plan_wire, step, work_order, upstream, upstream_formal, records
        )

    def _execute_verifier(
        self,
        plan_wire: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        work_orders: Sequence[Mapping[str, Any]],
        index: int,
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        upstream: Mapping[str, Any],
        upstream_formal: Mapping[str, Any],
    ) -> dict[str, Any]:
        records = re_read_stage_records(
            upstream_formal, expected_kinds=_STAGE_RECORD_KINDS["authority_resolver"]
        )
        if len(records) != 1 or records[0]["kind"] != "authority_resolution":
            raise ResearchPlanExecutorConflict(
                "verifier upstream lacks the exact authority resolution record"
            )
        connector_step = steps[0]
        connector_attempt = self._connector_attempt(plan_wire)
        bridge = self._re_read_bridge_authority(
            plan_wire, connector_step, attempt_number=connector_attempt
        )
        recomputed = self._material_and_bundles(plan_wire, step, bridge)
        if recomputed["status"] == "rejected":
            return self._complete_internal(
                plan_wire, step, work_order, upstream, upstream_formal, [],
                status="failed",
                error={
                    "code": "verification_rejected",
                    "message": "source or numeric verification did not pass",
                },
            )
        if (
            recomputed["authority_resolution"]["id"] != records[0]["ref"]
            or recomputed["authority_resolution"]["content_hash"]
            != records[0]["hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "verifier upstream authority resolution record drifted"
            )
        return self._complete_internal(
            plan_wire, step, work_order, upstream, upstream_formal,
            [
                {"kind": "source_verification",
                 "ref": recomputed["source_bundle"]["id"],
                 "hash": recomputed["source_bundle"]["content_hash"]},
                {"kind": "numeric_verification",
                 "ref": recomputed["numeric_bundle"]["id"],
                 "hash": recomputed["numeric_bundle"]["content_hash"]},
            ],
        )

    def _execute_staging(
        self,
        plan_wire: Mapping[str, Any],
        steps: Sequence[Mapping[str, Any]],
        work_orders: Sequence[Mapping[str, Any]],
        index: int,
        step: Mapping[str, Any],
        work_order: Mapping[str, Any],
        upstream: Mapping[str, Any],
        upstream_formal: Mapping[str, Any],
    ) -> dict[str, Any]:
        records = re_read_stage_records(
            upstream_formal, expected_kinds=_STAGE_RECORD_KINDS["verifier"]
        )
        if len(records) != 2 or {item["kind"] for item in records} != {
            "source_verification", "numeric_verification",
        }:
            raise ResearchPlanExecutorConflict(
                "staging upstream lacks the exact verification records"
            )
        connector_step = steps[0]
        connector_attempt = self._connector_attempt(plan_wire)
        bridge = self._re_read_bridge_authority(
            plan_wire, connector_step, attempt_number=connector_attempt
        )
        # The numeric verification authority belongs to the verifier node,
        # not to candidate staging.  Recompute the exact verifier spec so its
        # deterministic refs/hashes can be compared with the upstream proof.
        recomputed = self._material_and_bundles(plan_wire, steps[2], bridge)
        if recomputed["status"] == "rejected":
            raise ResearchPlanExecutorConflict(
                "candidate staging cannot accept an unverified upstream"
            )
        by_kind = {item["kind"]: item for item in records}
        if (
            recomputed["source_bundle"]["id"] != by_kind["source_verification"]["ref"]
            or recomputed["source_bundle"]["content_hash"]
            != by_kind["source_verification"]["hash"]
            or recomputed["numeric_bundle"]["id"]
            != by_kind["numeric_verification"]["ref"]
            or recomputed["numeric_bundle"]["content_hash"]
            != by_kind["numeric_verification"]["hash"]
        ):
            raise ResearchPlanExecutorConflict(
                "staging upstream verification records drifted"
            )
        cursor = self.plan.connection.cursor()
        cycle = read_exact_agenda_cycle(
            cursor, plan_wire["agenda_binding"]["cycle_ref"]
        )
        subject_ref = cycle["company_ref"]
        request = plan_wire["execution_scope"]["parameters"]
        operation_name = plan_wire["execution_scope"]["operation"]
        if operation_name == SEC_COMPANY_FACTS_OPERATION:
            payload = recomputed["material"]["normalized_payload"]
            period = f"{payload['current']['start']}..{payload['current']['end']}"
            aspect = "quarterly_revenue_yoy_growth"
            basis = "official-filing-xbrl"
            direction = (
                "up" if not recomputed["numeric_spec"]["output_value"].startswith("-")
                else "down"
            )
            statement = (
                f"{payload['entity_name']} reported {payload['label']} of "
                f"{payload['unit']} {payload['current']['value']} for {period}, "
                f"{direction} {recomputed['numeric_spec']['output_value'].lstrip('-')}% "
                f"year over year from {payload['unit']} {payload['prior']['value']} "
                "in the comparable quarter."
            )
        elif operation_name == SEC_OPERATION:
            period = f"{request['filing_date_from']}..{request['filing_date_to']}"
            count = len(recomputed["material"]["source_record_refs"])
            aspect = "filing_count"
            basis = "official-filing"
            statement = (
                f"The SEC public {request['form']} filing list for CIK "
                f"{request['issuer_cik']} in window {period} contains {count} filings."
            )
        else:
            raise ResearchPlanExecutorConflict(
                "candidate operation is outside the approved SEC reads"
            )
        candidate_identity = {
            "plan_version_ref": plan_wire["id"],
            "step_ref": step["id"],
            "subject_ref": subject_ref,
            "aspect": aspect,
        }
        evidence = build_candidate_evidence(
            recomputed["material"],
            recomputed["source_bundle"],
            candidate_evidence_ref=_derived_ref(
                "candidate-evidence:research-plan", candidate_identity
            ),
            actor_ref=self.actor_ref,
            created_at=recomputed["material"]["retrieved_at"],
            verification_mode="connector_authority",
        )
        claim = build_candidate_claim(
            evidence,
            recomputed["source_bundle"],
            recomputed["numeric_spec"],
            recomputed["numeric_bundle"],
            candidate_claim_ref=_derived_ref(
                "candidate-claim:research-plan", candidate_identity
            ),
            subject_ref=subject_ref,
            metric_or_aspect=aspect,
            basis=basis,
            normalized_statement=statement,
            actor_ref=self.actor_ref,
            created_at=recomputed["material"]["retrieved_at"],
        )
        evidence = validate_candidate_evidence(evidence)
        claim = validate_candidate_claim(claim)
        staged = self.staging.stage(
            checkpoint=bridge["checkpoint"],
            plan=bridge["compiled_plan"],
            context_pack=bridge["context_pack"],
            step=bridge["compiled_step"],
            runner_request=bridge["runner_request"],
            receipt=bridge["receipt"],
            material=recomputed["material"],
            numeric_spec=recomputed["numeric_spec"],
            source_verification=recomputed["source_bundle"],
            numeric_verification=recomputed["numeric_bundle"],
            evidence=evidence,
            claim=claim,
            idempotency_key=f"research-plan:{plan_wire['id']}:{step['ordinal']}:stage",
            verification_mode="connector_authority",
            authority_resolver=self.resolver,
        )
        self._inject("after_staging_commit")
        records = [
            {"kind": "candidate_evidence", "ref": evidence["id"],
             "hash": evidence["content_hash"]},
            {"kind": "candidate_claim", "ref": claim["id"],
             "hash": claim["content_hash"]},
        ]
        return self._complete_internal(
            plan_wire, step, work_order, upstream, upstream_formal, records
        )


__all__ = [
    "ResearchPlanExecutor",
    "ResearchPlanExecutorConflict",
    "ResearchPlanExecutorError",
    "re_read_stage_records",
    "sec_adapter_parameters",
    "sec_connector_identity",
    "sec_descriptor_spec",
    "stage_proof_wire",
]
