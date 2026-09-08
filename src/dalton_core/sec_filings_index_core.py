"""P10p: run one governed SEC filings index into Core connector authority.

The Initial Screen wants the latest annual report, and the only thing that
names where a 10-K lives is the SEC submissions index.  Reading that index is
a *discovery*: it yields documents to go and fetch, not claims.  Discovery
sources in this system do not run through a ResearchPlan -- the plan path
exists to turn one connector call into a verified Claim, and its closed
four-node tree has no shape for "here are three documents".  So this follows
the web search and web fetch lanes instead: build the governed call, persist
the invocation, envelope and raw bytes, and hand the caller a receipt whose
document refs the acquisition lane already knows how to fetch.

What the receipt carries that a normal envelope does not is the filing URLs.
The frozen ``list_filings`` output schema deliberately does not include the
primary document path, so the URLs are rebuilt from the exact raw artifact by
``build_filing_url_authorities`` -- the same way the search lane rebuilds its
URLs from the bytes it ranked.  They are minted as ``public-web-url`` refs, so
a 10-K joins the existing fetch and extraction pipeline rather than needing
one of its own.

The capability is the narrow one the owner signed in P10e: this lane cannot
read company facts, and the descriptor it publishes is the ordinary SEC
descriptor asked for the ``list_filings`` operation (P10n), not a second one
built by hand.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .alphaengine_core_acquisition import (
    RUNNER_ACTOR_REF,
    RUNNER_RUNTIME_REF,
    VISIBILITY_SCOPES,
)
from .alphaengine_core_search import register_chained_profile
from .capability_catalog import CapabilityCatalog, CapabilityNotFound
from .connector import ConnectorStore
from .connector_authority_port import (
    ConnectorAuthorityPort,
    ConnectorCompletionReceiptReader,
)
from .connector_governance import ConnectorGovernance
from .connector_quota_policy import apply_governed_quota_to_limits, governed_daily_quota
from .connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from .connector_transport_executor import ConnectorTransportExecutor
from .contracts import ExecutionInvocation, ExecutionKind, WorkOrder
from .observability import ObservabilityStore
from .public_http_transport import PublicHttpTransport
from .raw_spool import RawSpool
from .research_context import build_compiled_connector_plan
from .research_plan_executor import sec_descriptor_spec
from .runner_journal import RunnerJournal, RunnerJournalNotFound
from .scheduler import Scheduler
from .sec_filings_index import (
    CAPABILITY_ID,
    OPERATION,
    SIDE_EFFECT,
    build_filing_url_authorities,
    filings_index_adapter_hash,
    filings_index_contract,
    filings_index_permissions,
)
from .sec_public_adapter import SecPublicHttpAdapter
from .store import DaltonStore, canonical_json, content_hash

TEMPLATE_KEY = "sec"
LANE_SLUG = "sec-filings-index"
PROFILE_PREFIX = f"connector-profile:{LANE_SLUG}"
PRICE_RATE_PREFIX = f"price-rate:{LANE_SLUG}"
RATE_POLICY_PREFIX = f"rate-policy:{LANE_SLUG}"
QUOTA_SCOPE_REF = f"connector-quota-scope:{LANE_SLUG}:{OPERATION}"
PLANNER_REF = f"planner:{LANE_SLUG}:v1"
ROUTING_POLICY_REF = f"routing-policy:{LANE_SLUG}:v1"
INDEX_HOST = "data.sec.gov"
MAX_RESPONSE_BYTES = 8_388_608
MAX_RECORDS = 100
TIMEOUT_MS = 60_000
DEFAULT_USER_AGENT = "Dalton Research Agent OS SEC filings-index lane (owner: lumos)"


TRAILING_WINDOW_HOURS = 24


def count_recent_index_calls(connection: Any, *, as_of: datetime | None = None) -> int:
    """Trailing-24h ``list_filings`` invocations against the Core profile."""

    from datetime import timedelta

    now = as_of or datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=TRAILING_WINDOW_HOURS)).isoformat(
        timespec="microseconds"
    )
    row = connection.execute(
        "SELECT COUNT(*) FROM connector_invocations "
        "WHERE connector_profile_ref=? AND created_at >= ?",
        (f"{PROFILE_PREFIX}:v1", window_start),
    ).fetchone()
    return int(row[0])


class SecFilingsIndexCoreError(RuntimeError):
    """The governed filings-index call could not be run or bound."""


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SecFilingsIndexCoreError(f"{name} is not an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise SecFilingsIndexCoreError(f"{name} must carry a timezone")
    return parsed


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def validate_filings_index_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The closed parameter set the frozen ``list_filings`` contract accepts."""

    if not isinstance(spec, Mapping) or set(spec) != {
        "issuer", "form", "date_from", "date_to", "limit",
    }:
        raise SecFilingsIndexCoreError(
            "filings index spec must be exactly issuer/form/date_from/date_to/limit"
        )
    issuer = spec["issuer"]
    if not isinstance(issuer, str) or not issuer.isdigit() or len(issuer) != 10:
        raise SecFilingsIndexCoreError("issuer must be a zero-padded 10-digit CIK")
    form = spec["form"]
    if not isinstance(form, str) or not form.strip():
        raise SecFilingsIndexCoreError("form must be a non-empty string")
    for name in ("date_from", "date_to"):
        value = spec[name]
        if not isinstance(value, str):
            raise SecFilingsIndexCoreError(f"{name} must be a date string")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise SecFilingsIndexCoreError(f"{name} must be YYYY-MM-DD") from exc
    if spec["date_from"] > spec["date_to"]:
        raise SecFilingsIndexCoreError("date_from must not be after date_to")
    limit = spec["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECORDS:
        raise SecFilingsIndexCoreError(f"limit must be an integer 1..{MAX_RECORDS}")
    return {
        "issuer": issuer, "form": form.strip(),
        "date_from": spec["date_from"], "date_to": spec["date_to"], "limit": limit,
    }


class SecFilingsIndexCore:
    """Run one governed ``list_filings`` into Core authority and name its URLs."""

    def __init__(
        self,
        *,
        store: DaltonStore,
        connectors: ConnectorStore,
        observability: ObservabilityStore,
        journal: RunnerJournal,
        scheduler: Scheduler,
        catalog: CapabilityCatalog,
        spool: RawSpool,
        governance: ConnectorGovernance,
        transport: Any | None = None,
        adapter: Any | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if type(store) is not DaltonStore:
            raise TypeError("filings index requires an exact DaltonStore")
        if type(connectors) is not ConnectorStore or connectors.connection is not store.connection:
            raise TypeError("filings index requires the Core ConnectorStore")
        if (
            type(observability) is not ObservabilityStore
            or observability.connection is not store.connection
        ):
            raise TypeError("filings index requires the Core ObservabilityStore")
        if type(journal) is not RunnerJournal:
            raise TypeError("filings index requires the Core RunnerJournal")
        if type(spool) is not RawSpool:
            raise TypeError("filings index requires an exact RawSpool")
        if not isinstance(governance, ConnectorGovernance):
            raise TypeError("filings index requires ConnectorGovernance")
        if governance.capability_id != CAPABILITY_ID:
            raise SecFilingsIndexCoreError(
                "governance record does not authorize the filings-index capability"
            )
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise SecFilingsIndexCoreError("lease_seconds must be a positive integer")
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.journal = journal
        self.scheduler = scheduler
        self.catalog = catalog
        self.spool = spool
        self.governance = governance
        self.user_agent = user_agent
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self.template, self.contract = filings_index_contract()
        self.permissions = filings_index_permissions()
        self.adapter = adapter if adapter is not None else SecPublicHttpAdapter(
            transport=transport if transport is not None else PublicHttpTransport(),
            user_agent=user_agent,
            clock=self.clock,
        )
        self.receipts = ConnectorCompletionReceiptReader(
            connectors=connectors, observability=observability
        )
        self.authority_port = ConnectorAuthorityPort(
            connectors=connectors, observability=observability, scheduler=scheduler,
            receipt_reader=self.receipts,
        )
        self._descriptor: Any | None = None
        self._authorities: dict[str, Any] | None = None

    # -- governed authorities ------------------------------------------------
    def _descriptor_spec(self) -> dict[str, Any]:
        # P10n: the ordinary SEC descriptor builder, asked for this operation,
        # already produces the identity the signed record binds.
        return sec_descriptor_spec(
            self.template,
            self.permissions,
            self.governance.effective_from,
            capability_policy_ref=self.governance.policy_ref,
            operation_name=OPERATION,
        )

    def ensure_descriptor(self) -> Any:
        if self._descriptor is not None:
            return self._descriptor
        spec = self._descriptor_spec()
        try:
            descriptor = self.catalog.describe(
                CAPABILITY_ID, visibility_scopes=list(VISIBILITY_SCOPES)
            )
        except CapabilityNotFound:
            descriptor = self.catalog.publish(spec)
        if (
            descriptor.source_hash != spec["source_hash"]
            or descriptor.schema_hash != spec["schema_hash"]
            or descriptor.eligibility.policy_ref != self.governance.policy_ref
            or descriptor.permissions.to_dict() != spec["permissions"]
        ):
            raise SecFilingsIndexCoreError(
                "published filings-index capability differs from the governed spec"
            )
        self._descriptor = descriptor
        return descriptor

    def ensure_authorities(self) -> dict[str, Any]:
        """Profile / price / rate policy / runner manifest for the index host."""

        if self._authorities is not None:
            return self._authorities
        descriptor = self.ensure_descriptor()
        rate_policy_ref = f"{RATE_POLICY_PREFIX}:{OPERATION}"
        binding = {
            "binding_ref": f"runner-binding:{LANE_SLUG}:0.1",
            "descriptor_revision_ref": descriptor.revision_ref,
            "descriptor_hash": descriptor.content_hash,
            "adapter_ref": self.template["transport"]["target_ref"],
            "adapter_hash": filings_index_adapter_hash(),
            "source_ref": self.template["source_identity"]["source_ref"],
            "source_hash": descriptor.source_hash,
            "operation": OPERATION,
            "input_schema_ref": self.contract["input_schema_ref"],
            "input_schema_hash": self.contract["input_schema_hash"],
            "output_schema_ref": self.contract["output_schema_ref"],
            "output_schema_hash": self.contract["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": filings_index_permissions(),
            "side_effects": [SIDE_EFFECT],
            "rate_policy_ref": rate_policy_ref,
        }
        manifest = validate_runner_environment_manifest(
            _with_hash(
                {
                    "schema_version": "0.1",
                    "id": f"runner-environment:dalton-core-{LANE_SLUG}:0.1",
                    "created_at": self.governance.effective_from,
                    "runner_runtime_ref": RUNNER_RUNTIME_REF,
                    "runner_actor_ref": RUNNER_ACTOR_REF,
                    "resolver_ref": "resolver:dalton-core-static:0.1",
                    "resolver_version": "0.1",
                    "package_manifest_ref": f"artifact:runner-packages:{LANE_SLUG}:0.1",
                    "package_manifest_hash": content_hash(
                        {"adapter_hash": filings_index_adapter_hash(), "host": INDEX_HOST}
                    ),
                    "bindings": [binding],
                }
            )
        )
        profile = register_chained_profile(
            self.connectors,
            {
                "schema_version": "0.1",
                "id": f"{PROFILE_PREFIX}:v1",
                "created_at": self.governance.effective_from,
                "connector_ref": self.template["connector_ref"],
                "version": None,
                "prior_version_ref": None,
                "capability_id": CAPABILITY_ID,
                "descriptor_revision_ref": descriptor.revision_ref,
                "descriptor_hash": descriptor.content_hash,
                "source_identity": dict(self.template["source_identity"]),
                "source_hash": descriptor.source_hash,
                "schema_hash": descriptor.schema_hash,
                "catalog_epoch": descriptor.catalog_epoch,
                "adapter_ref": self.template["transport"]["target_ref"],
                "adapter_hash": filings_index_adapter_hash(),
                "runner_runtime_ref": RUNNER_RUNTIME_REF,
                "runner_actor_ref": RUNNER_ACTOR_REF,
                "runner_environment_hash": manifest["content_hash"],
                "allowed_operations": [OPERATION],
                "allowed_hosts": [INDEX_HOST],
                "auth_mode": "none",
                "credential_slot_refs": [],
                "input_schema_refs": {OPERATION: self.contract["input_schema_ref"]},
                "input_schema_hashes": {OPERATION: self.contract["input_schema_hash"]},
                "output_schema_refs": {OPERATION: self.contract["output_schema_ref"]},
                "output_schema_hashes": {OPERATION: self.contract["output_schema_hash"]},
                "pagination": {
                    "mode": self.contract["pagination"]["mode"],
                    "cursor_field": self.contract["pagination"]["cursor_field"],
                    "max_pages": self.contract["pagination"]["max_pages"],
                },
                "completeness": {OPERATION: self.contract["completeness_ceiling"]},
                "max_response_bytes": MAX_RESPONSE_BYTES,
                "max_records": MAX_RECORDS,
                "timeout_ms": TIMEOUT_MS,
                "access_policy_ref": "policy:access:public-sec",
                "retention_policy_ref": "policy:retention:public-sec-filing-index",
                "terms_policy_ref": "policy:terms:sec-fair-access",
                "network_policy": {
                    "allowed_schemes": ["https"],
                    "allow_redirects": False,
                    "max_redirects": 0,
                    "resolve_public_only": True,
                },
            },
            idempotency_key=f"{LANE_SLUG}:profile:v1",
        )
        price_ref = f"{PRICE_RATE_PREFIX}:calls"
        price = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": f"{price_ref}:v1",
                "created_at": self.governance.effective_from,
                "price_rate_ref": price_ref,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "meter": "calls",
                "unit_quantity": 1,
                "unit_price_micros": 0,
                "rounding_mode": "ceiling",
                "currency": "USD",
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "source_ref": "pricing:public-sec:free",
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key=f"{LANE_SLUG}:price:v1",
        )
        quota = governed_daily_quota(TEMPLATE_KEY, OPERATION)
        price_book = {"price_rate_refs": [price["id"]], "required_price_meters": ["calls"]}
        rate_policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": f"{rate_policy_ref}:v1",
                "created_at": self.governance.effective_from,
                "policy_ref": rate_policy_ref,
                "quota_scope_ref": QUOTA_SCOPE_REF,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "window_seconds": quota["window_seconds"],
                "reset_timezone": quota["reset_timezone"],
                "max_concurrency": 1,
                "quota_currency": "USD",
                **price_book,
                "price_book_hash": content_hash(price_book),
                "limits": apply_governed_quota_to_limits(
                    quota,
                    max_response_bytes=profile["max_response_bytes"],
                    max_records=profile["max_records"],
                ),
                "effective_from": self.governance.effective_from,
                "effective_until": None,
                "actor_ref": self.governance.approved_by,
            },
            idempotency_key=f"{LANE_SLUG}:rate-policy:v1",
        )
        self._authorities = {
            "descriptor": descriptor, "binding": binding, "manifest": manifest,
            "profile": profile, "price": price, "rate_policy": rate_policy,
        }
        return self._authorities

    # -- one index read ------------------------------------------------------
    def build_request(
        self, spec: Mapping[str, Any], *, created_at: str | None = None
    ) -> dict[str, Any]:
        """Bind one validated spec to a creation time; the pair is the identity."""

        parameters = validate_filings_index_spec(spec)
        created = created_at or _wire_time(self.clock())
        _parse_time(created, "created_at")
        identity = {
            "operation": OPERATION, "parameters": parameters, "created_at": created,
        }
        return {
            "operation": OPERATION,
            "parameters": parameters,
            "created_at": created,
            "query_hash": content_hash({"operation": OPERATION, "parameters": parameters}),
            "request_hash": content_hash(identity),
        }

    def list_filings(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Execute (or durably replay) one index read; return the bound receipt."""

        if not isinstance(request, Mapping) or set(request) != {
            "operation", "parameters", "created_at", "query_hash", "request_hash",
        }:
            raise SecFilingsIndexCoreError("index request has an invalid closed shape")
        rebuilt = self.build_request(request["parameters"], created_at=request["created_at"])
        if rebuilt != dict(request):
            raise SecFilingsIndexCoreError("index request identity drifted")
        parameters = rebuilt["parameters"]
        created_at = rebuilt["created_at"]
        suffix = rebuilt["request_hash"][:20]
        authorities = self.ensure_authorities()
        profile = authorities["profile"]
        descriptor = authorities["descriptor"]
        binding = authorities["binding"]
        manifest = authorities["manifest"]

        work_id = f"work:{LANE_SLUG}:{suffix}"
        call_id = f"connector-call:{LANE_SLUG}:{suffix}"
        invocation_id = f"connector-invocation:{LANE_SLUG}:{suffix}"
        artifact_ref = f"artifact:{LANE_SLUG}:{suffix}:raw"
        request_id = f"connector-runner-request:{LANE_SLUG}:{suffix}"

        resolver = StaticAdapterResolver(
            manifest,
            {binding["binding_ref"]: self.adapter},
            {binding["binding_ref"]: lambda value, expected=parameters: value == expected},
        )
        gate = ConnectorRunnerAdmissionGate(
            scheduler=self.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=resolver,
            visibility_scopes=list(VISIBILITY_SCOPES),
            clock=self.clock,
        )
        executor = ConnectorTransportExecutor(
            gate=gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority_port,
            connector_reader=self.connectors,
            clock=self.clock,
        )
        work = WorkOrder(
            schema_version="0.1",
            id=work_id,
            created_at=created_at,
            updated_at=created_at,
            question=(
                "List one issuer's public SEC filings of one form into Core "
                "connector authority (discovery only)"
            ),
            requested_capabilities=(CAPABILITY_ID,),
            runtime_profile_ref=RUNNER_RUNTIME_REF,
            budget={"max_seconds": self.lease_seconds},
            idempotency_key=work_id,
            declared_side_effects=(SIDE_EFFECT,),
            status="ready",
            input_refs=(call_id,),
        )
        work_hash = content_hash(work.to_dict())

        try:
            stored = self.journal.request(request_id)
        except RunnerJournalNotFound:
            stored = None
        replayed = False
        if stored is not None:
            latest = self.journal.latest(request_id)
            if latest["state"] != "responded":
                raise SecFilingsIndexCoreError(
                    f"index runner request {request_id} is incomplete at durable "
                    f"state {latest['state']}; run transport recovery first"
                )
            replayed = True
            response = executor.execute(stored, scheduler_lease_token="replay")
        else:
            call = self.connectors.register_call_spec(
                {
                    "schema_version": "0.1",
                    "id": call_id,
                    "created_at": created_at,
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "connector_profile_ref": profile["id"],
                    "operation": OPERATION,
                    "parameters": parameters,
                    "query_hash": rebuilt["query_hash"],
                },
                idempotency_key=f"{call_id}:register",
            )
            execution = ExecutionInvocation(
                schema_version="0.1",
                id=invocation_id,
                created_at=created_at,
                kind=ExecutionKind.CONNECTOR,
                work_order_ref=work.id,
                profile_ref=profile["id"],
                capability=CAPABILITY_ID,
                input_refs=(call["id"],),
                output_refs=(artifact_ref,),
                started_at=created_at,
                completed_at=None,
                side_effects=(),
                runtime_ref=profile["runner_runtime_ref"],
                actor_ref=profile["runner_actor_ref"],
                environment_hash=profile["runner_environment_hash"],
            )
            lease = self.catalog.prepare(
                work,
                capability_id=CAPABILITY_ID,
                revision_ref=descriptor.revision_ref,
                catalog_epoch=descriptor.catalog_epoch,
                descriptor_hash=descriptor.content_hash,
                source_hash=descriptor.source_hash,
                schema_hash=descriptor.schema_hash,
                policy_ref=self.governance.policy_ref,
                policy_hash=self.governance.policy_hash(),
                principal_ref=self.governance.principal_ref,
                visibility_scopes=list(VISIBILITY_SCOPES),
                ttl_seconds=self.lease_seconds,
            )
            invocation = self.connectors.register_invocation(
                {
                    "schema_version": "0.1",
                    "id": invocation_id,
                    "created_at": created_at,
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "capability_lease_ref": lease.id,
                    "capability_lease_hash": lease.content_hash,
                    "descriptor_revision_ref": descriptor.revision_ref,
                    "catalog_epoch": descriptor.catalog_epoch,
                    "logical_invocation_key": "connector-logical:" + content_hash(
                        {
                            "work_order_ref": work.id,
                            "work_order_hash": work_hash,
                            "connector_profile_hash": profile["content_hash"],
                            "call_spec_hash": call["content_hash"],
                        }
                    ),
                },
                execution=execution,
                idempotency_key=f"{invocation_id}:register",
            )
            enqueued = self.scheduler.enqueue(work)
            if enqueued.get("status") == "conflict":
                raise SecFilingsIndexCoreError(
                    f"scheduler already holds another WorkOrder for {work.id}"
                )
            claim = self.scheduler.claim(
                RUNNER_ACTOR_REF, work_order_id=work.id, lease_seconds=self.lease_seconds
            )
            if claim is None:
                raise SecFilingsIndexCoreError(
                    f"WorkOrder {work.id} is not claimable; it may already be complete "
                    "without a durable runner response"
                )
            runner_request = _with_hash(
                {
                    "schema_version": "0.1",
                    "id": request_id,
                    "created_at": created_at,
                    "connector_invocation_ref": invocation["id"],
                    "connector_invocation_hash": invocation["content_hash"],
                    "execution_ref": invocation["execution_ref"],
                    "execution_hash": invocation["execution_hash"],
                    "work_order_ref": work.id,
                    "work_order_hash": work_hash,
                    "scheduler_attempt_number": int(
                        claim["lease"].get("attempt_number") or claim["attempt"]["attempt_number"]
                    ),
                    "scheduler_lease_revision_ref": claim["lease"]["id"],
                    "scheduler_lease_hash": claim["lease"]["content_hash"],
                    "connector_profile_ref": profile["id"],
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_ref": call["id"],
                    "call_spec_hash": call["content_hash"],
                    "capability_lease_ref": lease.id,
                    "capability_lease_hash": lease.content_hash,
                    "principal_ref": self.governance.principal_ref,
                    "runner_runtime_ref": profile["runner_runtime_ref"],
                    "runner_actor_ref": profile["runner_actor_ref"],
                    "runner_environment_hash": profile["runner_environment_hash"],
                    "idempotency_key": request_id,
                }
            )
            response = executor.execute(runner_request, scheduler_lease_token=claim["lease_token"])
        return self._receipt(rebuilt, response, profile, replayed=replayed)

    def _receipt(
        self,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        profile: Mapping[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        invocation = self.receipts.get_invocation(response["connector_invocation_ref"])
        if invocation["content_hash"] != response["connector_invocation_hash"]:
            raise SecFilingsIndexCoreError("index invocation authority drifted")
        base: dict[str, Any] = {
            "operation": OPERATION,
            "parameters": dict(request["parameters"]),
            "created_at": request["created_at"],
            "query_hash": request["query_hash"],
            "request_hash": request["request_hash"],
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "connector_invocation_ref": invocation["id"],
            "connector_invocation_hash": invocation["content_hash"],
            "runner_response_ref": response["id"],
            "outcome": response["outcome"],
            "replayed": replayed,
            "provider_calls": 0 if replayed else 1,
            "source_envelope_ref": None,
            "source_envelope_hash": None,
            "raw_artifact_version_ref": None,
            "raw_response_hash": None,
            "source_record_refs": [],
            "filings": [],
            "document_refs": [],
        }
        if response["outcome"] != "succeeded" or response["source_envelope_ref"] is None:
            return base
        source = self.receipts.get_source_envelope(response["source_envelope_ref"])
        if source is None or source["content_hash"] != response["source_envelope_hash"]:
            raise SecFilingsIndexCoreError("index source envelope authority drifted")
        if (
            source["connector_invocation_ref"] != invocation["id"]
            or source["operation"] != OPERATION
            or source["source"] != profile["source_identity"]["source_ref"]
        ):
            raise SecFilingsIndexCoreError("index source envelope does not bind the call")
        filings = self.filing_authorities(source["id"], request["parameters"])
        base.update({
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "raw_artifact_version_ref": source.get("raw_artifact_version_ref"),
            "raw_response_hash": source.get("raw_response_hash"),
            "source_record_refs": list(source["source_record_refs"]),
            "filings": filings,
            "document_refs": [item["url_ref"] for item in filings],
        })
        return base

    def filing_authorities(
        self, source_envelope_ref: str, parameters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Rebuild the fetchable filing URLs of one completed index read.

        Nothing is stored: the URLs are derived from the exact raw artifact,
        because the frozen output schema names the filings but not where they
        live.  The parameters are passed in rather than dug back out of the
        envelope because they are what decides *which* filings the URLs are
        for, and the caller is holding the authoritative copy -- the same one
        the envelope's call spec was registered from.
        """

        source = self.receipts.get_source_envelope(source_envelope_ref)
        if source is None:
            raise SecFilingsIndexCoreError("index source envelope was not found")
        raw = self.spool.read_object(source["raw_response_hash"])
        return build_filing_url_authorities(raw, validate_filings_index_spec(parameters))


__all__ = [
    "CAPABILITY_ID",
    "DEFAULT_USER_AGENT",
    "INDEX_HOST",
    "LANE_SLUG",
    "OPERATION",
    "SecFilingsIndexCore",
    "SecFilingsIndexCoreError",
    "validate_filings_index_spec",
]
