"""S1: run a ``host_tool`` connector as a child process and record it as one.

Three of Dalton's connector transports now have a runner. Public HTTPS has
one, loopback MCP has one, and ``host_tool`` -- the transport for everything
that is a program on this machine rather than a service on the network -- did
not. Every ``host_tool`` template in the inventory (the two human feeds, and
the shadow Xueqiu and X templates waiting behind them) therefore had a frozen
contract and no way to satisfy it: a child could read the data, but nothing
turned that read into a ``ConnectorInvocation`` and a ``SourceEnvelope``, and
without those the mission ledger has nothing to bind a discovery to.

**What this is not.** It is not a second copy of
``ConnectorTransportExecutor``. That executor is the right thing for a paid
network call: it carries a Scheduler lease, a durable journal with five crash
barriers, and orphaned-reservation recovery, because a call that may have
already charged somebody must never be silently repeated. A host tool is a
different risk shape -- a local file read, free, idempotent, with no upstream
that can be double-charged and no provider whose meter can drift from ours --
so this runner writes the same authority chain directly through
``ConnectorStore`` and does not pretend to a lease it has no use for. What it
gives up is written down here rather than discovered later: no lease
enforcement, no journal replay, no orphan recovery. A crash between the
reservation and the attempt leaves an unsettled reservation, which the
existing ``unsettled_reservations`` sweep already reports.

**The credential rule.** The child is executed with an environment this runner
builds: the interpreter's own execution variables, plus exactly the credential
*slot names* the profile declares, resolved by the caller. The child never
sees ``os.environ``. That is the whole point -- a host tool that needs a cookie
gets the one slot it declared and nothing else, and a host tool that needs
nothing (both feeds) gets nothing.

**Raw stdout is the artifact.** The child prints its closed observation wire
and nothing else; those exact bytes are hashed into the spool before they are
parsed, and only then is the parsed wire checked against the operation's
frozen output schema. A wire the contract cannot describe fails the run after
the bytes are safe, not before.

Generic on purpose: nothing in here knows what a sales note or a wiki document
is. A caller supplies an identity (the packaged template's frozen contract for
one operation), a command builder, and parameters.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .connector import source_envelope_content_hash
from .connector_inventory import load_packaged_connector_inventory
from .connector_quota_policy import (
    apply_governed_quota_to_limits,
    governed_daily_quota,
)
from .contracts import ExecutionInvocation, ExecutionKind
from .store import canonical_json, content_hash

RUNNER_RUNTIME_REF = "runtime:dalton-core-host-tool-runner:0.1"
RUNNER_ACTOR_REF = "actor:dalton-core-trusted-runner"
DEFAULT_ACTOR_REF = "automation:coverage-mission"
ACCESS_POLICY_REF = "policy:connector-access:host-tool:0.1"
RETENTION_POLICY_REF = "policy:connector-retention:host-tool:0.1"
TERMS_POLICY_REF = "policy:connector-terms:host-tool:0.1"

# One host-tool response is a bounded local read. These are ceilings, not
# expectations, and the spool refuses beyond them rather than growing.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 5_000
DEFAULT_TIMEOUT_SECONDS = 180.0

# Execution variables, not source variables. The child needs an interpreter,
# a home for its temporary files and a locale; it does not need whatever else
# happens to be exported in the writer's process. Credentials arrive only as
# the slots the profile declares.
_PASSTHROUGH_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
)


class HostToolRunError(RuntimeError):
    """The host-tool run cannot proceed, or its result cannot be recorded."""


@dataclass(frozen=True)
class HostToolReceipt:
    """What a completed host-tool run binds, for a caller that must cite it.

    The fields are refs and hashes only. A caller re-reads them through
    ``ConnectorCompletionReceiptReader`` rather than trusting this object,
    which is why the object is frozen and carries nothing a reader could not
    check for itself.
    """

    operation: str
    parameters: dict[str, Any]
    query_hash: str
    connector_profile_ref: str
    connector_profile_hash: str
    connector_invocation_ref: str
    connector_invocation_hash: str
    physical_attempt_ref: str
    raw_artifact_version_ref: str
    raw_response_hash: str
    source_envelope_ref: str
    source_envelope_hash: str
    document_refs: list[str]
    observation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "parameters": dict(self.parameters),
            "query_hash": self.query_hash,
            "connector_profile_ref": self.connector_profile_ref,
            "connector_profile_hash": self.connector_profile_hash,
            "connector_invocation_ref": self.connector_invocation_ref,
            "connector_invocation_hash": self.connector_invocation_hash,
            "physical_attempt_ref": self.physical_attempt_ref,
            "raw_artifact_version_ref": self.raw_artifact_version_ref,
            "raw_response_hash": self.raw_response_hash,
            "source_envelope_ref": self.source_envelope_ref,
            "source_envelope_hash": self.source_envelope_hash,
            "document_refs": list(self.document_refs),
            "observation": dict(self.observation),
        }


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class HostToolRunner:
    """Execute one frozen ``host_tool`` operation and leave Core able to cite it."""

    def __init__(
        self,
        *,
        store: Any,
        connectors: Any,
        observability: Any,
        spool: Any,
        template_key: str,
        identity: Mapping[str, Any],
        governance: Any,
        command: Callable[[Mapping[str, Any], Path], Sequence[str]],
        connector_slug: str | None = None,
        credential_slot_refs: Sequence[str] = (),
        credential_resolver: Callable[[str], str] | None = None,
        actor_ref: str = DEFAULT_ACTOR_REF,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_records: int = MAX_RECORDS,
    ) -> None:
        self.store = store
        self.connectors = connectors
        self.observability = observability
        self.spool = spool
        self.template_key = template_key
        self.identity = dict(identity)
        self.governance = governance
        self.command = command
        self.connector_slug = connector_slug or template_key
        self.credential_slot_refs = tuple(credential_slot_refs)
        self.credential_resolver = credential_resolver
        self.actor_ref = actor_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.max_records = int(max_records)
        self._template = load_packaged_connector_inventory()["templates"][template_key]
        self._authorities: dict[str, Any] | None = None

    # -- approval ---------------------------------------------------------

    def _require_approved(self) -> None:
        """Approval first: nothing is registered before the record is checked.

        The same three questions the child asks, asked here too, because a
        run that will be refused should not first write a profile version and
        a call spec into Core.
        """

        if not getattr(self.governance, "approved", False):
            raise HostToolRunError(
                f"{self.template_key} governance record is not approved"
            )
        if self.governance.capability_id != self.identity["capability_id"]:
            raise HostToolRunError("governance record covers a different capability")
        if self.governance.wire["expected_source_hash"] != self.identity["source_hash"]:
            raise HostToolRunError("governance source hash differs from the packaged template")
        if self.governance.wire["expected_schema_hash"] != self.identity["schema_hash"]:
            raise HostToolRunError(
                "governance schema hash differs from the packaged contract; "
                "the approval does not cover this output contract"
            )

    # -- frozen contract --------------------------------------------------

    def _contract(self) -> dict[str, Any]:
        operation = self.identity["operation"]
        for item in self._template["operations"]:
            if item["operation"] == operation:
                return item
        raise HostToolRunError(f"packaged template has no {operation} operation")

    def output_schema(self) -> dict[str, Any]:
        ref = self._contract()["output_schema_ref"]
        for document in self._template["schema_documents"]:
            if document["schema_ref"] == ref:
                return document["document"]
        raise HostToolRunError("packaged template has no output contract for this operation")

    # -- authorities ------------------------------------------------------

    def ensure_authorities(self) -> dict[str, Any]:
        """Profile, price rate and rate policy for this exact operation.

        One profile per operation rather than one per connector: the frozen
        schema hash binds one operation, and a profile listing both would let
        an approval for the index be spent on the document.
        """

        if self._authorities is not None:
            return self._authorities
        self._require_approved()
        from .alphaengine_core_search import register_chained_profile

        operation = self.identity["operation"]
        contract = self._contract()
        created_at = self.governance.effective_from
        suffix = self.identity["schema_hash"][:20]
        connector_ref = f"connector:host-tool:{self.template_key}:{operation}"
        profile_id = f"connector-profile:host-tool:{self.template_key}:{operation}:{suffix}"
        environment_hash = content_hash({
            "runtime": RUNNER_RUNTIME_REF,
            "adapter": self.identity["adapter_ref"],
            "operation": operation,
        })
        profile_wire = {
            "schema_version": "0.1",
            "id": profile_id,
            "created_at": created_at,
            "connector_ref": connector_ref,
            "version": None,
            "prior_version_ref": None,
            "capability_id": self.identity["capability_id"],
            # There is no live Catalog descriptor for a host tool yet, so the
            # descriptor identity is bound to the approval that authorises it.
            # That is weaker than a published descriptor and it is not
            # pretending otherwise: it still moves whenever the approval does.
            "descriptor_revision_ref": f"{self.identity['capability_id']}@{self.governance.id}",
            "descriptor_hash": self.governance.content_hash,
            "source_identity": dict(self.identity["source_identity"]),
            "source_hash": self.identity["source_hash"],
            "schema_hash": self.identity["schema_hash"],
            "catalog_epoch": 1,
            "adapter_ref": self.identity["adapter_ref"],
            "adapter_hash": self.identity["adapter_hash"],
            "runner_runtime_ref": RUNNER_RUNTIME_REF,
            "runner_actor_ref": RUNNER_ACTOR_REF,
            "runner_environment_hash": environment_hash,
            "allowed_operations": [operation],
            # A child process has no host to allow and no scheme to pin.
            "allowed_hosts": [],
            "auth_mode": "host_tool",
            "credential_slot_refs": list(self.credential_slot_refs),
            "input_schema_refs": {operation: contract["input_schema_ref"]},
            "input_schema_hashes": {operation: contract["input_schema_hash"]},
            "output_schema_refs": {operation: contract["output_schema_ref"]},
            "output_schema_hashes": {operation: contract["output_schema_hash"]},
            "pagination": {"mode": "none", "cursor_field": None, "max_pages": 1},
            "completeness": {operation: contract["completeness_ceiling"]},
            "max_response_bytes": self.max_response_bytes,
            "max_records": self.max_records,
            "timeout_ms": int(self.timeout_seconds * 1000),
            "access_policy_ref": ACCESS_POLICY_REF,
            "retention_policy_ref": RETENTION_POLICY_REF,
            "terms_policy_ref": TERMS_POLICY_REF,
            "network_policy": None,
        }
        profile = register_chained_profile(
            self.connectors, profile_wire, idempotency_key=f"{profile_id}:register"
        )
        rate_id = f"price-rate:host-tool:{self.template_key}:{operation}:calls:0.1"
        rate = self.connectors.register_price_rate(
            {
                "schema_version": "0.1",
                "id": rate_id,
                "created_at": created_at,
                "price_rate_ref": rate_id,
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "meter": "calls",
                "unit_quantity": 1,
                # Zero, explicitly. A local file read costs nothing, and the
                # price book requires an explicit zero rather than an absence.
                "unit_price_micros": 0,
                "rounding_mode": "ceiling",
                "currency": "USD",
                "effective_from": created_at,
                "effective_until": None,
                "source_ref": "owner:host-tool-local-read",
                "actor_ref": self.actor_ref,
            },
            idempotency_key=f"{rate_id}:register",
        )
        quota = governed_daily_quota(self.connector_slug, operation)
        # A document-unit quota counts documents, so a call reserves one of
        # them; a search-unit quota counts records, so a call reserves the
        # profile's record ceiling. Reserving the ceiling against a
        # document-unit limit spends a day's allowance on one read.
        self._quota_unit = quota["quota_unit"]
        limits = apply_governed_quota_to_limits(
            quota,
            max_response_bytes=self.max_response_bytes,
            max_records=self.max_records,
        )
        policy_id = f"policy:connector-rate:host-tool:{self.template_key}:{operation}:0.1"
        policy = self.connectors.register_rate_policy(
            {
                "schema_version": "0.1",
                "id": policy_id,
                "created_at": created_at,
                "policy_ref": policy_id,
                "quota_scope_ref": f"quota-scope:{self.template_key}:{operation}",
                "version": 1,
                "prior_version_ref": None,
                "connector_profile_ref": profile["id"],
                "window_seconds": quota["window_seconds"],
                "reset_timezone": quota["reset_timezone"],
                "max_concurrency": 1,
                "quota_currency": "USD",
                "price_rate_refs": [rate["price_rate_ref"]],
                "required_price_meters": ["calls"],
                "price_book_hash": content_hash({
                    "price_rate_refs": [rate["price_rate_ref"]],
                    "required_price_meters": ["calls"],
                }),
                "limits": limits,
                "effective_from": created_at,
                "effective_until": None,
                "actor_ref": self.actor_ref,
            },
            idempotency_key=f"{policy_id}:register",
        )
        self._authorities = {"profile": profile, "rate": rate, "policy": policy}
        return self._authorities

    # -- the child --------------------------------------------------------

    def _environment(self) -> dict[str, str]:
        """The child's whole environment, built here rather than inherited.

        Every credential the child may see is a slot the profile declared and
        the caller resolved. The child never resolves one for itself, which is
        what makes "this connector holds no credential" a checkable statement
        rather than a hope.
        """

        env = {
            name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ
        }
        env["PYTHONUNBUFFERED"] = "1"
        for slot in self.credential_slot_refs:
            if self.credential_resolver is None:
                raise HostToolRunError(
                    f"profile declares credential slot {slot} but no resolver was given"
                )
            value = self.credential_resolver(slot)
            if not isinstance(value, str) or not value:
                raise HostToolRunError(f"credential slot {slot} resolved to nothing")
            env[slot.rsplit(":", 1)[-1].upper().replace("-", "_")] = value
        return env

    def _execute(
        self, parameters: Mapping[str, Any], output_dir: Path | None
    ) -> tuple[bytes, int, str | None]:
        """Run the child and return its raw stdout, exit code and failure note.

        ``output_dir`` is where the child may leave durable files of its own --
        an acquisition manifest, say. When a caller supplies one, the run is
        the caller's run and its files outlive this call; otherwise the child
        gets a scratch directory that is deleted, because a run nobody asked
        to keep should not leave anything behind.
        """

        with tempfile.TemporaryDirectory(prefix="host-tool-") as scratch:
            target = Path(output_dir) if output_dir is not None else Path(scratch)
            argv = [str(item) for item in self.command(parameters, target)]
            if not argv:
                raise HostToolRunError("host-tool command builder produced no argv")
            try:
                completed = subprocess.run(
                    argv,
                    cwd=scratch,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._environment(),
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return b"", -1, "timeout"
            except OSError as exc:
                return b"", -1, f"OSError: {exc}"
        raw = completed.stdout or b""
        if len(raw) > self.max_response_bytes:
            return raw[: self.max_response_bytes], completed.returncode, "response too large"
        note = None
        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            note = f"exit {completed.returncode}: {detail[:300]}" if detail else \
                f"exit {completed.returncode}"
        return raw, completed.returncode, note

    # -- one run ----------------------------------------------------------

    def run(self, *, parameters: Mapping[str, Any], work_ref: str,
            output_dir: Path | None = None) -> HostToolReceipt:
        """Execute once and record the whole chain, or raise with a reason."""

        authorities = self.ensure_authorities()
        profile = authorities["profile"]
        policy = authorities["policy"]
        operation = self.identity["operation"]
        parameters = json.loads(canonical_json(dict(parameters)))
        query_hash = content_hash({"operation": operation, "parameters": parameters})
        created_at = _wire_time(self.clock())
        # One run, one invocation. Reading the same document twice is two
        # reads of a source that may have changed underneath, and collapsing
        # them onto one invocation would make the second read's bytes look
        # like they came from the first read's call.
        suffix = content_hash({
            "query_hash": query_hash, "created_at": created_at, "work_ref": work_ref,
        })[:20]

        work_hash = content_hash({"work_order_ref": work_ref, "query_hash": query_hash})
        call_id = f"connector-call:host-tool:{self.template_key}:{suffix}"
        invocation_id = f"connector-invocation:host-tool:{self.template_key}:{suffix}"
        artifact_ref = f"artifact:host-tool:{self.template_key}:{suffix}:raw"

        call = self.connectors.register_call_spec(
            {
                "schema_version": "0.1",
                "id": call_id,
                "created_at": created_at,
                "work_order_ref": work_ref,
                "work_order_hash": work_hash,
                "connector_profile_ref": profile["id"],
                "operation": operation,
                "parameters": parameters,
                "query_hash": query_hash,
            },
            idempotency_key=f"{call_id}:register",
        )
        execution = ExecutionInvocation(
            schema_version="0.1",
            id=invocation_id,
            created_at=created_at,
            kind=ExecutionKind.CONNECTOR,
            work_order_ref=work_ref,
            profile_ref=profile["id"],
            capability=self.identity["capability_id"],
            input_refs=(call["id"],),
            output_refs=(artifact_ref,),
            started_at=created_at,
            completed_at=None,
            side_effects=(),
            runtime_ref=profile["runner_runtime_ref"],
            actor_ref=profile["runner_actor_ref"],
            environment_hash=profile["runner_environment_hash"],
        )
        invocation = self.connectors.register_invocation(
            {
                "schema_version": "0.1",
                "id": invocation_id,
                "created_at": created_at,
                "work_order_ref": work_ref,
                "work_order_hash": work_hash,
                "connector_profile_ref": profile["id"],
                "connector_profile_hash": profile["content_hash"],
                "call_spec_ref": call["id"],
                "call_spec_hash": call["content_hash"],
                # No Catalog lease: this runner does not hold one, and saying
                # so with a derived ref is honest where a fabricated lease id
                # would look like an authority that was never granted.
                "capability_lease_ref": f"lease:host-tool:{self.template_key}:{suffix}",
                "capability_lease_hash": content_hash({
                    "approval": self.governance.content_hash, "query": query_hash,
                }),
                "descriptor_revision_ref": profile["descriptor_revision_ref"],
                "catalog_epoch": profile["catalog_epoch"],
                "logical_invocation_key": "connector-logical:" + content_hash({
                    "work_order_ref": work_ref,
                    "work_order_hash": work_hash,
                    "connector_profile_hash": profile["content_hash"],
                    "call_spec_hash": call["content_hash"],
                }),
            },
            execution=execution,
            idempotency_key=f"{invocation_id}:register",
        )

        reserved_records = 1 if self._quota_unit == "document" else self.max_records
        reservation = self.connectors.reserve_quota(
            invocation["id"],
            policy["id"],
            1,
            {"calls": 1, "bytes": self.max_response_bytes,
             "records": reserved_records, "cost_micros": 0},
            ttl_seconds=max(60, int(self.timeout_seconds) + 60),
            idempotency_key=f"{invocation_id}:reserve:1",
        )

        started_at = _wire_time(self.clock())
        raw, exit_code, note = self._execute(parameters, output_dir)
        completed_at = _wire_time(self.clock())
        provider_request_id = f"host-tool:{self.template_key}:{suffix}"

        # Artifact always: the bytes are hashed into the spool before they are
        # parsed, so a wire that fails the contract still leaves the read
        # recoverable.
        digest = hashlib.sha256(raw).hexdigest()
        sink = self.spool.open_sink(
            f"raw-sink:{digest}", max_response_bytes=max(1, len(raw))
        )
        sink.write(raw)
        raw_object = sink.finalize()

        outcome = "succeeded" if note is None and raw else (
            "timeout" if note == "timeout" else "failed"
        )
        attempt = self.connectors.record_physical_attempt(
            invocation["id"], reservation["id"], 1, outcome,
            started_at=started_at, completed_at=completed_at,
            provider_request_id=provider_request_id if outcome == "succeeded" else None,
            idempotency_key=f"{invocation_id}:attempt:1",
        )
        # Contract last, and metered by what the contract says came back. The
        # bytes are already in the spool, so a wire the frozen schema cannot
        # describe still settles honestly -- an indeterminate settlement, a
        # recoverable artifact -- rather than vanishing.
        observation: dict[str, Any] | None = None
        failure = note
        document_refs: list[str] = []
        if outcome == "succeeded":
            try:
                observation = json.loads(raw.decode("utf-8"))
                from .authority_resolver import _schema_matches

                _schema_matches(observation, self.output_schema(), "output")
                document_refs = list(observation["source_record_refs"])
                if len(document_refs) > self.max_records:
                    raise HostToolRunError(
                        "host-tool response exceeds the profile record ceiling"
                    )
            except Exception as exc:  # noqa: BLE001 - settle, then report
                observation, document_refs = None, []
                failure = f"{type(exc).__name__}: {exc}"

        measured = observation is not None
        records = (1 if self._quota_unit == "document" else len(document_refs)) if measured else 0
        usage = self.connectors.record_usage(
            attempt["id"],
            {"calls": 1, "bytes": len(raw), "records": records, "cost_micros": 0},
            measurement_status="final" if measured else "unavailable",
            metering_source="runner_measured",
            idempotency_key=f"{invocation_id}:usage:1",
        )
        cost = self.connectors.record_cost_for_runner(
            usage["id"],
            cost_status="actual" if measured else "estimated",
            calculation_ref=f"calculation:host-tool:{self.template_key}:{suffix}",
            actor_ref=self.actor_ref,
            idempotency_key=f"{invocation_id}:cost:1",
        )
        self.connectors.settle_quota_for_runner(
            reservation["id"],
            "consumed" if measured else "indeterminate",
            usage_entry_ref=usage["id"],
            cost_entry_ref=cost["id"],
            idempotency_key=f"{invocation_id}:settle:1",
        )
        if not measured:
            raise HostToolRunError(
                f"{self.template_key} {operation} child {outcome}: "
                f"{failure or 'no output'}"
            )

        self.observability.register_artifact_version_v2(
            artifact_ref,
            version_id=artifact_ref,
            title=f"{self.template_key} {operation} raw response",
            kind="connector_raw_response",
            media_type="application/json",
            artifact_content_hash=raw_object.content_hash,
            size_bytes=raw_object.size_bytes,
            storage_locator=raw_object.storage_locator,
            producer_execution_ref=invocation["execution_ref"],
            result_envelope_ref=f"result-envelope:host-tool:{self.template_key}:{suffix}",
            result_envelope_hash=content_hash({"invocation": invocation["id"]}),
            access_class="restricted",
            preview_status="unavailable",
            actor_ref=self.actor_ref,
            idempotency_key=f"{artifact_ref}:register",
        )

        envelope_id = f"source-envelope:host-tool:{self.template_key}:{suffix}"
        envelope = {
            "schema_version": "0.1",
            "id": envelope_id,
            "created_at": completed_at,
            "connector_invocation_ref": invocation["id"],
            "connector_profile_ref": profile["id"],
            "physical_attempt_refs": [attempt["id"]],
            "result_physical_attempt_ref": attempt["id"],
            "source": profile["source_identity"]["source_ref"],
            "operation": operation,
            "source_record_refs": document_refs,
            "published_at": None,
            "updated_at": None,
            "as_of": None,
            "retrieved_at": completed_at,
            "cursor": None,
            "provider_request_id": provider_request_id,
            "raw_artifact_version_ref": artifact_ref,
            "raw_response_hash": raw_object.content_hash,
            "source_schema_hash": profile["output_schema_hashes"][operation],
            "source_content_hash": "",
            # A local read of a bounded window either found everything in it or
            # failed; there is no page left behind to make it partial.
            "completeness": profile["completeness"][operation] if document_refs else "unknown",
            "status": "complete" if document_refs else "empty",
            "access_policy_ref": profile["access_policy_ref"],
            "retention_policy_ref": profile["retention_policy_ref"],
            "terms_policy_ref": profile["terms_policy_ref"],
            "error": None,
        }
        envelope["source_content_hash"] = source_envelope_content_hash(envelope)
        recorded = self.connectors.record_source_envelope(
            envelope, idempotency_key=f"{envelope_id}:record"
        )
        return HostToolReceipt(
            operation=operation,
            parameters=parameters,
            query_hash=query_hash,
            connector_profile_ref=profile["id"],
            connector_profile_hash=profile["content_hash"],
            connector_invocation_ref=invocation["id"],
            connector_invocation_hash=invocation["content_hash"],
            physical_attempt_ref=attempt["id"],
            raw_artifact_version_ref=artifact_ref,
            raw_response_hash=raw_object.content_hash,
            source_envelope_ref=recorded["id"],
            source_envelope_hash=recorded["content_hash"],
            document_refs=document_refs,
            observation=observation,
        )


__all__ = [
    "ACCESS_POLICY_REF",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RECORDS",
    "MAX_RESPONSE_BYTES",
    "RETENTION_POLICY_REF",
    "RUNNER_ACTOR_REF",
    "RUNNER_RUNTIME_REF",
    "TERMS_POLICY_REF",
    "HostToolReceipt",
    "HostToolRunError",
    "HostToolRunner",
]
