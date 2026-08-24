#!/usr/bin/env python3
"""Run one isolated live AlphaEngine -> Terra TranscriptPolish canary.

The source and model outputs remain in an owner-only canary directory.  The
summary contains hashes and authority references only.  The run never writes
the live Evidence, Claim, Thesis, or production-policy stores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
    validate_alphaengine_document_page_request,
)
from dalton_core.contracts import WorkOrder
from dalton_core.model_deployment import (
    TRANSCRIPT_POLISH_DEVELOPMENT_POLICY_REF,
    TRANSCRIPT_POLISH_DEVELOPMENT_PROFILE_ID,
    install_openclaw_catalog,
    upgrade_openclaw_broker_catalog,
)
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_connector_bridge import (
    LoopbackStreamableHttpMcpHandle,
    OpenClawToolHandle,
)
from dalton_core.openclaw_model_adapter import (
    OpenClawModelAdapter,
    owner_only_secret_file_provider,
)
from dalton_core.live_mcp_connector import (
    alphaengine_document_page_from_raw_response,
)
from dalton_core.raw_spool import RawSpool
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.transcript_correction import TranscriptCorrectionAuthority
from dalton_core.transcript_polish import (
    TRANSCRIPT_POLISH_CAPABILITY,
    TRANSCRIPT_POLISH_OPERATION,
    TRANSCRIPT_POLISH_PERMISSION,
    TRANSCRIPT_POLISH_RUNTIME,
    TranscriptPolishAuthority,
    TranscriptPolishWorker,
)
from dalton_core.transcript_polish_model_worker import (
    RoutedTranscriptPolishModelWorker,
)
from dalton_core.transcript_polish_routed import (
    RoutedTranscriptPolishCoordinator,
)


SCHEMA_VERSION = "0.1"
DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8950/mcp"
DEFAULT_BROKER_SOCKET = Path(
    "/Users/everflow/.openclaw/dalton-model-broker.sock"
)
DEFAULT_BROKER_AUTH_KEY = Path(
    "/Users/everflow/.openclaw/dalton-model-broker.sock.key"
)


class AlphaEngineTranscriptCanaryError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wire_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise AlphaEngineTranscriptCanaryError(
            "canary timestamp must include timezone"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    wire["content_hash"] = content_hash(wire)
    return wire


def _secure_write(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _single_span(text: str, term: str, name: str) -> tuple[int, int]:
    if not isinstance(term, str) or not term:
        raise AlphaEngineTranscriptCanaryError(f"{name} must be non-empty")
    start = text.find(term)
    if start < 0:
        raise AlphaEngineTranscriptCanaryError(f"{name} was not found")
    if text.find(term, start + 1) >= 0:
        raise AlphaEngineTranscriptCanaryError(f"{name} is not unique")
    return start, start + len(term)


class _CanaryAuthorityReader:
    """Detached receipt reader used only by the isolated acquisition canary."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def put(self, value: Mapping[str, Any]) -> dict[str, Any]:
        wire = deepcopy(dict(value))
        self.records[wire["id"]] = wire
        return deepcopy(wire)

    def _get(self, ref: str) -> dict[str, Any] | None:
        value = self.records.get(ref)
        return None if value is None else deepcopy(value)

    get_invocation = _get
    get_profile = _get
    get_call_spec = _get
    get_reservation = _get
    get_physical_attempt = _get
    get_usage_entry = _get
    get_cost_entry = _get
    get_quota_settlement = _get
    get_source_envelope = _get
    get_artifact_version = _get


class _LiveAlphaEnginePagePort:
    """Read exact live pages and retain the minimum immutable canary receipts."""

    def __init__(
        self,
        *,
        plan: Mapping[str, Any],
        authority: _CanaryAuthorityReader,
        spool: RawSpool,
        credential_handle: OpenClawToolHandle,
        page_max_chars: int,
        call_timeout_seconds: int,
        created_at: str,
    ) -> None:
        if page_max_chars < 1 or call_timeout_seconds < 1:
            raise ValueError("live page bounds must be positive")
        self.plan = dict(plan)
        self.authority = authority
        self.spool = spool
        self.handle = credential_handle
        self.page_max_chars = page_max_chars
        self.call_timeout_seconds = call_timeout_seconds
        self.created_at = created_at
        self.cache: dict[str, dict[str, Any]] = {}
        self.provider_calls = 0

    def execute_page(self, request: Mapping[str, Any]) -> dict[str, Any]:
        page_request = validate_alphaengine_document_page_request(request)
        cached = self.cache.get(page_request["id"])
        if cached is not None:
            duplicate = deepcopy(cached)
            duplicate["idempotency_status"] = "duplicate"
            duplicate["content_hash"] = content_hash({
                key: value for key, value in duplicate.items()
                if key != "content_hash"
            })
            return duplicate
        response = self._execute_fresh(page_request)
        self.cache[page_request["id"]] = deepcopy(response)
        return response

    def _execute_fresh(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.provider_calls += 1
        offset = int(request["expected_offset"])
        deadline = _wire_time(
            _now() + timedelta(seconds=self.call_timeout_seconds)
        )
        suffix = content_hash({
            "page_request_hash": request["content_hash"],
            "document_ref": self.plan["document_ref"],
        })[:20]
        invocation = self.handle.invoke(
            "get_document",
            {
                "doc_id": self.plan["document_id"],
                "offset": offset,
                "max_chars": self.page_max_chars,
                "mode": "auto",
            },
            call_ref=f"credential-use:alphaengine-transcript-canary:{suffix}",
            deadline_at=deadline,
            max_response_bytes=int(request["max_response_bytes"]),
        )
        provider_request_id, page = alphaengine_document_page_from_raw_response(
            invocation.raw_response,
            expected_doc_id=self.plan["document_id"],
            expected_offset=offset,
            max_chars=self.page_max_chars,
        )
        raw_hash = hashlib.sha256(invocation.raw_response).hexdigest()
        sink = self.spool.open_sink(
            "raw-sink:" + content_hash({
                "page_request_hash": request["content_hash"],
            }),
            max_response_bytes=int(request["max_response_bytes"]),
        )
        try:
            sink.write(invocation.raw_response)
            raw_object = sink.finalize()
        except Exception:
            sink.abort()
            raise

        profile = self.authority.put(_with_hash({
            "id": f"connector-profile:alphaengine-page:{suffix}",
            "source_identity": {
                "source_ref": "source:alphaengine",
                "source_type": "licensed_research_library",
                "source_version": "live-mcp",
            },
            "source_hash": self.plan["source_hash"],
            "pagination": {
                "mode": "cursor", "cursor_field": "cursor", "max_pages": 20,
            },
            "max_response_bytes": request["max_response_bytes"],
        }))
        parameters = {"document_ref": self.plan["document_ref"]}
        if request["request_cursor"] is not None:
            parameters["cursor"] = request["request_cursor"]
        call = self.authority.put(_with_hash({
            "id": f"connector-call:alphaengine-page:{suffix}",
            "connector_profile_ref": profile["id"],
            "operation": "get_document",
            "parameters": parameters,
            "query_hash": content_hash({
                "operation": "get_document", "parameters": parameters,
            }),
        }))
        connector_invocation = self.authority.put(_with_hash({
            "id": f"connector-invocation:alphaengine-page:{suffix}",
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "call_spec_ref": call["id"],
            "call_spec_hash": call["content_hash"],
            "execution_ref": f"execution:alphaengine-page:{suffix}",
        }))
        units = 1 if int(request["page_ordinal"]) == 1 else 0
        reservation = self.authority.put(_with_hash({
            "id": f"connector-reservation:alphaengine-page:{suffix}",
            "reserved": {
                "calls": 1,
                "bytes": request["max_response_bytes"],
                "records": units,
                "cost_micros": 0,
            },
        }))
        attempt = self.authority.put(_with_hash({
            "id": f"connector-attempt:alphaengine-page:{suffix}",
            "connector_invocation_ref": connector_invocation["id"],
            "outcome": "succeeded",
        }))
        metrics = {
            "calls": 1,
            "bytes": len(invocation.raw_response),
            "records": units,
            "cost_micros": 0,
        }
        usage = self.authority.put(_with_hash({
            "id": f"connector-usage:alphaengine-page:{suffix}",
            "physical_attempt_ref": attempt["id"],
            "metrics": metrics,
        }))
        cost = self.authority.put(_with_hash({
            "id": f"connector-cost:alphaengine-page:{suffix}",
            "usage_entry_ref": usage["id"],
        }))
        settlement = self.authority.put(_with_hash({
            "id": f"connector-settlement:alphaengine-page:{suffix}",
            "reservation_ref": reservation["id"],
            "state": "consumed",
            "usage_entry_ref": usage["id"],
            "cost_entry_ref": cost["id"],
            "actual": metrics,
        }))
        artifact = self.authority.put(_with_hash({
            "id": f"artifact-version:alphaengine-page:{suffix}",
            "artifact_content_hash": raw_object.content_hash,
            "size_bytes": raw_object.size_bytes,
            "producer_execution_ref": connector_invocation["execution_ref"],
        }))
        terminal = bool(page["complete"])
        source = self.authority.put(_with_hash({
            "id": f"source-envelope:alphaengine-page:{suffix}",
            "connector_invocation_ref": connector_invocation["id"],
            "connector_profile_ref": profile["id"],
            "physical_attempt_refs": [attempt["id"]],
            "result_physical_attempt_ref": attempt["id"],
            "source": "source:alphaengine",
            "operation": "get_document",
            "source_record_refs": [page["source_record_ref"]],
            "cursor": page["cursor"],
            "provider_request_id": provider_request_id,
            "raw_artifact_version_ref": artifact["id"],
            "raw_response_hash": raw_hash,
            "status": "complete" if terminal else "partial",
            "completeness": page["completeness"],
        }))
        base = {
            "schema_version": "0.2",
            "id": f"connector-runner-response:alphaengine-page:{suffix}",
            "created_at": self.created_at,
            "runner_request_ref": f"connector-runner-request:{suffix}",
            "runner_request_hash": content_hash({"runner": suffix}),
            "idempotency_status": "fresh",
            "connector_invocation_ref": connector_invocation["id"],
            "connector_invocation_hash": connector_invocation["content_hash"],
            "physical_attempt_ref": attempt["id"],
            "physical_attempt_hash": attempt["content_hash"],
            "usage_entry_ref": usage["id"],
            "usage_entry_hash": usage["content_hash"],
            "cost_entry_ref": cost["id"],
            "cost_entry_hash": cost["content_hash"],
            "quota_settlement_ref": settlement["id"],
            "quota_settlement_hash": settlement["content_hash"],
            "raw_artifact_version_ref": artifact["id"],
            "raw_artifact_version_hash": artifact["content_hash"],
            "source_envelope_ref": source["id"],
            "source_envelope_hash": source["content_hash"],
            "result_envelope_ref": f"result-envelope:{suffix}",
            "result_envelope_hash": content_hash({"result": suffix}),
            "outcome": "succeeded",
            "retry_at": None,
        }
        return _with_hash(base)


def _speaker_terms(text: str) -> list[str]:
    terms: list[str] = []
    for line in text.splitlines():
        if not line.startswith("发言人") or "：" not in line:
            continue
        label = line.split("：", 1)[0]
        if label not in terms:
            terms.append(label)
    return terms


def _probe_work(
    *,
    manifest: Mapping[str, Any],
    correction_set: Mapping[str, Any],
    protected_terms: Sequence[str],
    created_at: str,
) -> WorkOrder:
    identity = content_hash({
        "manifest_hash": manifest["content_hash"],
        "correction_set_hash": correction_set["content_hash"],
        "protected_terms": list(protected_terms),
    })
    parameters = {
        "source_ref": "source:alphaengine",
        "locator": manifest["document_ref"],
        "query_terms": ["transcript-polish"],
        "source_manifest_ref": manifest["id"],
        "source_manifest_hash": manifest["content_hash"],
        "source_content_hash": manifest["assembled_object"]["content_hash"],
        "additional_protected_terms": list(protected_terms),
        "correction_set_version_ref": correction_set["id"],
        "correction_set_version_hash": correction_set["content_hash"],
        "prior_polished_artifact_version_ref": None,
    }
    return WorkOrder(
        schema_version="0.1",
        id=f"work:transcript-polish-live-canary-{identity[:24]}",
        created_at=created_at,
        updated_at=created_at,
        question="Materialize one isolated live transcript polish canary.",
        requested_capabilities=(TRANSCRIPT_POLISH_CAPABILITY,),
        runtime_profile_ref=TRANSCRIPT_POLISH_RUNTIME,
        budget={"cost_units": 1, "max_attempts": 2, "max_seconds": 900},
        idempotency_key=f"transcript-polish-live-canary:{identity}",
        declared_side_effects=(),
        status="ready",
        input_refs=(manifest["id"], correction_set["id"]),
        metadata={
            "operation": TRANSCRIPT_POLISH_OPERATION,
            "permission_scope": TRANSCRIPT_POLISH_PERMISSION,
            "parameters": parameters,
        },
    )


def _cost_usd(runs: Sequence[Mapping[str, Any]]) -> str:
    total = Decimal("0")
    seen_cost_entries: set[str] = set()
    for run in runs:
        cost = run.get("accounting", {}).get("cost", {})
        if cost.get("cost_status") != "actual":
            raise AlphaEngineTranscriptCanaryError(
                "model cost authority is not actual"
            )
        cost_ref = cost.get("id")
        if not isinstance(cost_ref, str) or not cost_ref:
            raise AlphaEngineTranscriptCanaryError(
                "model cost authority is missing its immutable ref"
            )
        if cost_ref in seen_cost_entries:
            continue
        seen_cost_entries.add(cost_ref)
        total += Decimal(str(cost["amount_micros"])) / Decimal("1000000")
    return format(total, "f")


def run_canary(
    *,
    output_dir: Path,
    document_id: str,
    unresolved_term: str,
    claim_quote: str,
    actor_ref: str,
    protected_terms: Sequence[str],
    mcp_endpoint: str,
    broker_socket: Path,
    broker_auth_key: Path,
    expected_agent_id: str,
    page_max_chars: int,
    max_input_tokens: int,
    max_output_tokens: int,
    max_cost_usd: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise AlphaEngineTranscriptCanaryError(
            "output directory already exists"
        )
    output_dir.mkdir(parents=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    created = _now()
    created_at = _wire_time(created)
    store = DaltonStore(output_dir / "canary.sqlite")
    router: ModelRouter | None = None
    try:
        spool = RawSpool(output_dir, max_total_bytes=100_000_000)
        plan = build_alphaengine_document_acquisition_plan(
            document_ref=f"alphaengine-doc:{document_id}",
            created_at=created_at,
            max_pages=20,
            page_max_response_bytes=1_000_000,
            max_total_response_bytes=20_000_000,
            max_document_chars=200_000,
        )
        authority_reader = _CanaryAuthorityReader()
        handle = LoopbackStreamableHttpMcpHandle(
            mcp_endpoint,
            allowed_tools={"get_document": "get_document"},
            timeout_seconds=float(timeout_seconds),
        )
        page_port = _LiveAlphaEnginePagePort(
            plan=plan,
            authority=authority_reader,
            spool=spool,
            credential_handle=handle,
            page_max_chars=page_max_chars,
            call_timeout_seconds=timeout_seconds,
            created_at=created_at,
        )
        manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=page_port,
            authority_reader=authority_reader,
            spool=spool,
        ).execute()
        if manifest["status"] != "complete":
            raise AlphaEngineTranscriptCanaryError(
                f"document acquisition stopped at {manifest['termination_reason']}"
            )
        source_hash = manifest["assembled_object"]["content_hash"]
        source_text = spool.read_object(source_hash).decode("utf-8")
        unresolved_start, unresolved_end = _single_span(
            source_text, unresolved_term, "unresolved_term"
        )
        claim_start, claim_end = _single_span(
            source_text, claim_quote, "claim_quote"
        )
        correction_authority = TranscriptCorrectionAuthority(
            store,
            spool=spool,
            manifest_resolver=lambda ref: manifest,
            evidence_resolver=lambda ref: {},
        )
        correction_set = correction_authority.publish(
            f"transcript-correction-set:live-canary-{document_id}",
            source_manifest_ref=manifest["id"],
            source_manifest_hash=manifest["content_hash"],
            source_content_hash=source_hash,
            review_scope="targeted_flags",
            corrections=[{
                "source_start": unresolved_start,
                "source_end": unresolved_end,
                "source_sha256": hashlib.sha256(
                    unresolved_term.encode("utf-8")
                ).hexdigest(),
                "correction_kind": "terminology",
                "disposition": "unresolved",
                "replacement_text": None,
                "rationale": (
                    "Live canary review flagged a suspected ASR term; no exact "
                    "audio or official transcript evidence was admitted."
                ),
                "evidence_bindings": [],
            }],
            actor_ref=actor_ref,
        )
        all_protected_terms = list(dict.fromkeys([
            *_speaker_terms(source_text), *protected_terms,
        ]))
        polish_authority = TranscriptPolishAuthority(
            store,
            spool=spool,
            manifest_resolver=lambda ref: manifest,
            correction_authority=correction_authority,
        )
        scheduler = Scheduler(connection=store.connection, clock=_now)
        probe = _probe_work(
            manifest=manifest,
            correction_set=correction_set,
            protected_terms=all_protected_terms,
            created_at=created_at,
        )
        enqueued = scheduler.enqueue(probe)
        if enqueued["status"] != "fresh":
            raise AlphaEngineTranscriptCanaryError(
                "probe WorkOrder did not enter isolated Scheduler"
            )
        coordinator = RoutedTranscriptPolishCoordinator(
            authority=polish_authority,
            scheduler=scheduler,
        )
        prepared = coordinator.prepare(
            probe,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_usd=max_cost_usd,
            max_seconds=timeout_seconds,
        )
        if prepared["status"] != "model_work_ready":
            raise AlphaEngineTranscriptCanaryError(
                "model WorkOrder was not prepared"
            )
        model_work = WorkOrder.from_dict(prepared["work_order"])

        router_path = output_dir / "router.sqlite"
        install_openclaw_catalog(
            router_path,
            checked_at=created,
            availability_ttl=timedelta(days=1),
        )
        upgrade_openclaw_broker_catalog(
            router_path,
            checked_at=created,
            availability_ttl=timedelta(days=1),
        )
        router = ModelRouter(router_path)
        policy = router.get_policy(TRANSCRIPT_POLISH_DEVELOPMENT_POLICY_REF)
        if policy["filters"]["allowed_profile_ids"] != [
            TRANSCRIPT_POLISH_DEVELOPMENT_PROFILE_ID
        ]:
            raise AlphaEngineTranscriptCanaryError(
                "development policy no longer pins Terra"
            )
        profile_row = router.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions "
            "WHERE profile_id=? ORDER BY version DESC LIMIT 1",
            (TRANSCRIPT_POLISH_DEVELOPMENT_PROFILE_ID,),
        ).fetchone()
        if profile_row is None:
            raise AlphaEngineTranscriptCanaryError(
                "Terra development profile is missing"
            )
        profile = json.loads(profile_row["profile_json"])
        adapter = OpenClawModelAdapter(
            broker_socket,
            route_resolver=router.get_decision,
            auth_client_id="client:dalton-core",
            auth_key_provider=owner_only_secret_file_provider(
                broker_auth_key
            ),
            timeout_seconds=float(timeout_seconds),
            max_frame_bytes=4_000_000,
            expected_agent_id=expected_agent_id,
            clock=_now,
        )
        model_worker = RoutedTranscriptPolishModelWorker(
            scheduler=scheduler,
            router=router,
            adapter=adapter,
            store=store,
            observability=ObservabilityStore(store),
            polish_worker=TranscriptPolishWorker(polish_authority),
            routing_policy_ref=TRANSCRIPT_POLISH_DEVELOPMENT_POLICY_REF,
            credential_slot_refs=(profile["credential_slot_ref"],),
            clock=_now,
        )
        model_runs: list[dict[str, Any]] = []
        for _ in range(2):
            run = model_worker.run_once(model_work)
            model_runs.append(run)
            if run["status"] == "succeeded":
                break
            if run["status"] != "retryable":
                break
        if model_runs[-1]["status"] != "succeeded":
            error = model_runs[-1].get("result", {}).get("error")
            raise AlphaEngineTranscriptCanaryError(
                f"Terra candidate failed: {error}"
            )
        advanced = coordinator.advance(probe, model_work)
        if advanced["status"] != "succeeded":
            raise AlphaEngineTranscriptCanaryError(
                "verified candidate did not close the isolated probe"
            )
        artifact_ref = advanced["result"]["artifact_refs"][0]
        artifact = polish_authority.artifact(artifact_ref)
        polished_text = polish_authority.polished_text(artifact_ref)
        if unresolved_term not in polished_text:
            raise AlphaEngineTranscriptCanaryError(
                "unresolved term did not survive the verified artifact"
            )
        citation = correction_authority.bind_claim_citation(
            correction_set["id"],
            correction_set["content_hash"],
            source_start=claim_start,
            source_end=claim_end,
        )
        if not citation["claim_eligible"]:
            raise AlphaEngineTranscriptCanaryError(
                "clean canary quote was not claim eligible"
            )
        counts = {
            table: int(store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0])
            for table in ("evidence_versions", "claim_versions", "thesis_versions")
        }
        if any(counts.values()):
            raise AlphaEngineTranscriptCanaryError(
                "isolated canary mutated formal research authority"
            )
        successful_run = model_runs[-1]
        invocation = successful_run["invocation"]
        summary = {
            "schema_version": SCHEMA_VERSION,
            "id": "alphaengine-transcript-polish-canary:" + content_hash({
                "manifest_hash": manifest["content_hash"],
                "artifact_hash": artifact["content_hash"],
                "citation_hash": citation["content_hash"],
            })[:32],
            "created_at": created_at,
            "source": {
                "document_ref": manifest["document_ref"],
                "manifest_ref": manifest["id"],
                "manifest_hash": manifest["content_hash"],
                "content_chars": manifest["content_chars"],
                "content_sha256": source_hash,
                "physical_calls": manifest["physical_calls"],
                "document_quota_units": manifest["document_quota_units"],
            },
            "correction_review": {
                "actor_ref": actor_ref,
                "review_scope": correction_set["review_scope"],
                "correction_set_ref": correction_set["id"],
                "correction_set_hash": correction_set["content_hash"],
                "accepted_count": correction_set["accepted_count"],
                "unresolved_count": correction_set["unresolved_count"],
                "unresolved_term_sha256": hashlib.sha256(
                    unresolved_term.encode("utf-8")
                ).hexdigest(),
            },
            "model": {
                "policy_ref": TRANSCRIPT_POLISH_DEVELOPMENT_POLICY_REF,
                "profile_id": profile["id"],
                "profile_version_ref": profile["profile_version_ref"],
                "provider": invocation["provider"],
                "model": invocation["model"],
                "attempts": len(model_runs),
                "input_tokens": invocation["usage"]["input_tokens"],
                "output_tokens": invocation["usage"]["output_tokens"],
                "total_tokens": invocation["usage"]["total_tokens"],
                "accounted_cost_usd": _cost_usd(model_runs),
            },
            "polished_artifact": {
                "version_ref": artifact["id"],
                "version_hash": artifact["content_hash"],
                "polished_chars": len(polished_text),
                "polished_sha256": artifact["polished_content_hash"],
                "source_to_polished_ratio": round(
                    len(polished_text) / len(source_text), 6
                ),
                "verification_status": artifact["verification_status"],
                "citation_authority": artifact["citation_authority"],
                "unresolved_term_preserved": True,
            },
            "claim_binding_dry_run": {
                "binding_ref": citation["id"],
                "binding_hash": citation["content_hash"],
                "claim_eligible": citation["claim_eligible"],
                "citation_mode": citation["citation_mode"],
                "source_span_sha256": citation["source_sha256"],
            },
            "formal_authority_counts": counts,
            "production_activated": False,
            "hard_gate_pass": True,
        }
        _secure_write(output_dir / "summary.json", summary)
        return summary
    except Exception:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at,
            "hard_gate_pass": False,
            "production_activated": False,
        }
        _secure_write(output_dir / "failed.json", failure)
        raise
    finally:
        if router is not None:
            router.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one isolated live AlphaEngine TranscriptPolish canary."
        )
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--unresolved-term", required=True)
    parser.add_argument("--claim-quote", required=True)
    parser.add_argument("--actor-ref", required=True)
    parser.add_argument("--protected-term", action="append", default=[])
    parser.add_argument("--mcp-endpoint", default=DEFAULT_MCP_ENDPOINT)
    parser.add_argument(
        "--broker-socket", type=Path, default=DEFAULT_BROKER_SOCKET
    )
    parser.add_argument(
        "--broker-auth-key", type=Path, default=DEFAULT_BROKER_AUTH_KEY
    )
    parser.add_argument("--expected-agent-id", default="chem")
    parser.add_argument("--page-max-chars", type=int, default=30_000)
    parser.add_argument("--max-input-tokens", type=int, default=50_000)
    parser.add_argument("--max-output-tokens", type=int, default=16_000)
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        summary = run_canary(
            output_dir=args.output_dir,
            document_id=args.document_id,
            unresolved_term=args.unresolved_term,
            claim_quote=args.claim_quote,
            actor_ref=args.actor_ref,
            protected_terms=args.protected_term,
            mcp_endpoint=args.mcp_endpoint,
            broker_socket=args.broker_socket,
            broker_auth_key=args.broker_auth_key,
            expected_agent_id=args.expected_agent_id,
            page_max_chars=args.page_max_chars,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_cost_usd=args.max_cost_usd,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(f"AlphaEngine transcript canary failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AlphaEngineTranscriptCanaryError",
    "run_canary",
]
