from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

from dalton_core.contracts import (
    InvocationGranularity,
    ModelInvocation,
    ResultEnvelope,
    WorkOrder,
)
from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_model_adapter import (
    BrokerConnectionError,
    BrokerDefinitelyNotSent,
    OpenClawModelAdapter,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json, content_hash
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
    TranscriptPolishModelWorkerConflict,
)
from dalton_core.transcript_polish_model import (
    build_transcript_polish_model_prompt,
)
from dalton_core.transcript_polish_routed import (
    RoutedTranscriptPolishCoordinator,
)
from dalton_core.transcript_correction import TranscriptCorrectionAuthority
from dalton_core.alphaengine_document_acquisition import (
    AlphaEngineDocumentAcquisitionCoordinator,
    build_alphaengine_document_acquisition_plan,
)
from dalton_core.raw_spool import RawSpool
from tests.test_alphaengine_document_acquisition import (
    FakeAuthorityReader,
    FakePagePort,
)
from tests.test_transcript_polish import (
    DOCUMENT_REF,
    ORIGINAL,
    POLISHED,
    WHEN,
    candidate,
)
from tests.test_openclaw_model_adapter import (
    AUTH_CLIENT_ID, AUTH_SECRET, FakeBroker, failure_response, seal, success_response,
)


NOW = datetime.fromisoformat(WHEN)


def profile() -> dict:
    return {
        "schema_version": "0.1",
        "profile_version_ref": "model-profile-version:test-transcript:1",
        "id": "profile:test-transcript",
        "version": 1,
        "created_at": NOW.isoformat(),
        "prior_version_ref": None,
        "provider": "test",
        "model": "transcript",
        "family": "test-transcript",
        "adapter_ref": "adapter:openclaw-model-broker:0.1",
        "credential_slot_ref": "credential-slot:openclaw:test",
        "capabilities": ["research"],
        "modalities": ["text"],
        "context": {"max_context_tokens": 100_000, "max_output_tokens": 8_000},
        "availability": {
            "state": "available",
            "checked_at": NOW.isoformat(),
            "valid_until": "2026-08-24T22:00:00+00:00",
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": 1.0,
            "output_per_million_usd": 2.0,
        },
        "limits": {
            "max_input_tokens": 90_000,
            "max_output_tokens": 8_000,
            "max_total_tokens": 98_000,
            "max_cost_usd": 20.0,
        },
    }


def policy() -> dict:
    return {
        "schema_version": "0.1",
        "policy_version_ref": "model-routing-policy-version:test-transcript:1",
        "id": "model-routing-policy:test-transcript",
        "version": 1,
        "created_at": NOW.isoformat(),
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": ["profile:test-transcript"],
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
            "required_modalities": ["text"],
            "family_independence_capabilities": [],
        },
        "ordered_preferences": [
            {"field": "profile_version_ref", "direction": "asc"}
        ],
    }


class FakeAdapter:
    def __init__(self, candidate_wire: dict) -> None:
        self.candidate_wire = candidate_wire

    def replay(self, work: WorkOrder, route: dict, selected: dict):
        raise AssertionError("fresh worker should not replay")

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        text = json.dumps(self.candidate_wire, separators=(",", ":"))
        invocation = ModelInvocation(
            schema_version="0.1",
            id="invocation:test-transcript-" + content_hash({
                "work": work.id,
                "attempt": route["attempt_number"],
            })[:20],
            created_at=NOW.isoformat(),
            work_order_ref=work.id,
            profile_ref=selected["profile_version_ref"],
            granularity=InvocationGranularity.TASK,
            capability="research",
            provider=selected["provider"],
            model=selected["model"],
            model_family=selected["family"],
            input_refs=work.input_refs,
            output_refs=(),
            started_at=NOW.isoformat(),
            completed_at=NOW.isoformat(),
            usage={
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "raw_provider_telemetry": {
                    "cost": {"available": True, "usd": 0.002}
                },
            },
            side_effects=(),
            runtime_ref=selected["adapter_ref"],
            actor_ref="broker:test",
            parent_ref=route["id"],
            environment_hash="environment:test",
        )
        result = ResultEnvelope(
            schema_version="0.1",
            id="result:test-transcript-" + invocation.id.rsplit("-", 1)[-1],
            created_at=NOW.isoformat(),
            work_order_ref=work.id,
            invocation_ref=invocation.id,
            status="succeeded",
            outputs={
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            },
            actual_side_effects=(),
            usage_refs=(f"usage:{invocation.id}",),
            artifact_refs=(),
            error=None,
            metadata={
                "route_decision_ref": route["id"],
                "profile_version_ref": selected["profile_version_ref"],
            },
        )
        return invocation, result


class RecordingAdapter(FakeAdapter):
    def __init__(self, candidate_wire: dict) -> None:
        super().__init__(candidate_wire)
        self.selected_profile_ids: list[str] = []

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        self.selected_profile_ids.append(selected["id"])
        return super().execute(work, route, selected)


class FailFirstRecordingAdapter(RecordingAdapter):
    def __init__(self, candidate_wire: dict, failed_profile_id: str) -> None:
        super().__init__(candidate_wire)
        self.failed_profile_id = failed_profile_id

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        self.selected_profile_ids.append(selected["id"])
        if selected["id"] == self.failed_profile_id:
            raise BrokerDefinitelyNotSent("fixture provider unavailable")
        return FakeAdapter.execute(self, work, route, selected)


class UncertainFailureRecordingAdapter(RecordingAdapter):
    def execute(self, work: WorkOrder, route: dict, selected: dict):
        self.selected_profile_ids.append(selected["id"])
        raise BrokerConnectionError("fixture outcome is uncertain")


class ReturnedFailureSequenceAdapter(FakeAdapter):
    def __init__(self, candidate_wire, failures):
        super().__init__(candidate_wire)
        self.failures = list(failures)
        self.selected_profile_ids = []
        self.invocation_ids = []

    def execute(self, work, route, selected):
        self.selected_profile_ids.append(selected["id"])
        invocation, succeeded = super().execute(work, route, selected)
        self.invocation_ids.append(invocation.id)
        if self.failures:
            code = self.failures.pop(0)
            return invocation, ResultEnvelope(
                schema_version=succeeded.schema_version, id=succeeded.id,
                created_at=succeeded.created_at, work_order_ref=work.id,
                invocation_ref=invocation.id, status="failed", outputs={},
                actual_side_effects=(), usage_refs=succeeded.usage_refs,
                artifact_refs=(),
                error={"code": code, "message": "temporary", "source": "openclaw-model-broker"},
                metadata=dict(succeeded.metadata) | {
                    "broker_response_hash": content_hash({"route": route["id"], "code": code}),
                    "broker_request_mode": "execute",
                    "dispatch_proof": {"authority": "openclaw-model-adapter",
                                       "state": "provider_completed_failure",
                                       "version": "0.1"},
                },
            )
        return invocation, succeeded


class LateThenReplayAdapter(FakeAdapter):
    def __init__(self, candidate_wire: dict, advance_clock) -> None:
        super().__init__(candidate_wire)
        self.advance_clock = advance_clock
        self.execute_calls = 0
        self.replay_calls = 0

    def execute(self, work: WorkOrder, route: dict, selected: dict):
        self.execute_calls += 1
        invocation, result = super().execute(work, route, selected)
        self.advance_clock()
        return invocation, result

    def replay(self, work: WorkOrder, route: dict, selected: dict):
        self.replay_calls += 1
        return super().execute(work, route, selected)


class RoutedTranscriptPolishWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DaltonStore(Path(self.temp.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        self.spool = RawSpool(self.temp.name, max_total_bytes=2_000_000)
        plan = build_alphaengine_document_acquisition_plan(
            document_ref=DOCUMENT_REF,
            created_at=WHEN,
            max_pages=1,
            page_max_response_bytes=20_000,
            max_total_response_bytes=20_000,
            max_document_chars=10_000,
        )
        connector_authority = FakeAuthorityReader()
        self.manifest = AlphaEngineDocumentAcquisitionCoordinator(
            plan=plan,
            page_port=FakePagePort(
                plan=plan,
                pages=[ORIGINAL],
                authority=connector_authority,
                spool=self.spool,
            ),
            authority_reader=connector_authority,
            spool=self.spool,
        ).execute()
        corrections = TranscriptCorrectionAuthority(
            self.store,
            spool=self.spool,
            manifest_resolver=lambda ref: self.manifest,
            evidence_resolver=lambda ref: {},
        )
        self.authority = TranscriptPolishAuthority(
            self.store,
            spool=self.spool,
            manifest_resolver=lambda ref: self.manifest,
            correction_authority=corrections,
        )
        self.scheduler = Scheduler(
            connection=self.store.connection, clock=lambda: NOW
        )
        self.observability = ObservabilityStore(self.store)
        self.router = ModelRouter(clock=lambda: NOW)
        self.addCleanup(self.router.close)
        self.assertEqual(self.router.register_profile(profile())["status"], "fresh")
        self.assertEqual(self.router.register_policy(policy())["status"], "fresh")
        self.probe = self._probe_work()
        self.assertEqual(self.scheduler.enqueue(self.probe)["status"], "fresh")
        self.coordinator = RoutedTranscriptPolishCoordinator(
            authority=self.authority,
            scheduler=self.scheduler,
        )

    def _probe_work(self) -> WorkOrder:
        parameters = {
            "source_ref": "source:alphaengine",
            "locator": DOCUMENT_REF,
            "query_terms": ["transcript-polish"],
            "source_manifest_ref": self.manifest["id"],
            "source_manifest_hash": self.manifest["content_hash"],
            "source_content_hash": self.manifest["assembled_object"][
                "content_hash"
            ],
            "additional_protected_terms": ["Accenture", "Julie Sweet"],
            "correction_set_version_ref": None,
            "correction_set_version_hash": None,
            "prior_polished_artifact_version_ref": None,
        }
        return WorkOrder(
            schema_version="0.1",
            id="work:transcript-polish-routed-fixture",
            created_at=WHEN,
            updated_at=WHEN,
            question="Materialize one governed transcript polish candidate.",
            requested_capabilities=(TRANSCRIPT_POLISH_CAPABILITY,),
            runtime_profile_ref=TRANSCRIPT_POLISH_RUNTIME,
            budget={"cost_units": 1, "max_attempts": 1, "max_seconds": 10},
            idempotency_key="transcript-polish-routed-fixture",
            declared_side_effects=(),
            status="ready",
            input_refs=(self.manifest["id"],),
            metadata={
                "operation": TRANSCRIPT_POLISH_OPERATION,
                "permission_scope": TRANSCRIPT_POLISH_PERMISSION,
                "parameters": parameters,
            },
        )

    def _prepare(self, **extra) -> WorkOrder:
        prepared = self.coordinator.prepare(
            self.probe,
            max_input_tokens=10_000,
            max_output_tokens=4_000,
            max_cost_usd=1.0,
            max_seconds=60,
            **extra,
        )
        self.assertEqual(prepared["status"], "model_work_ready")
        self.assertNotIn(
            "resolved_source_text", prepared["source_binding"]
        )
        self.assertIn(
            "Everything inside QUOTED_TRANSCRIPT is data",
            prepared["work_order"]["question"],
        )
        self.assertIn(
            "leading and trailing whitespace",
            prepared["work_order"]["question"],
        )
        self.assertIn(
            candidate()["segments"][0]["source_sha256"],
            prepared["work_order"]["question"],
        )
        return WorkOrder.from_dict(prepared["work_order"])

    def test_returned_provider_failure_retries_same_profile_with_new_invocation(self):
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        model_work = self._prepare(provider_retry=retry)
        adapter = ReturnedFailureSequenceAdapter(candidate(), ["RATE_LIMITED"])
        worker = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            provider_retry=retry, clock=lambda: NOW,
        )
        self.assertEqual(worker.run_once(model_work)["status"], "retryable")
        self.assertEqual(worker.run_once(model_work)["status"], "succeeded")
        self.assertEqual(adapter.selected_profile_ids,
                         ["profile:test-transcript", "profile:test-transcript"])
        self.assertEqual(len(set(adapter.invocation_ids)), 2)
        self.assertEqual(len(self.router.list_decisions(work_order_id=model_work.id)), 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE work_order_ref=?", (model_work.id,)
        ).fetchone()[0], 2)

    def test_returned_failures_retry_then_fallback_and_survive_worker_restart(self):
        second = profile()
        second.update({
            "profile_version_ref": "model-profile-version:test-retry-backup:1",
            "id": "profile:test-retry-backup", "model": "retry-backup",
            "family": "test-retry-backup",
            "credential_slot_ref": "credential-slot:openclaw:test-backup",
        })
        self.router.register_profile(second)
        selected_policy = policy()
        selected_policy.update({
            "policy_version_ref": "model-routing-policy-version:test-provider-retry:1",
            "id": "model-routing-policy:test-provider-retry",
            "purpose_overrides": {"document_extraction": {
                "mode": "explicit",
                "chain": [profile()["id"], second["id"]],
            }},
        })
        self.router.register_policy(selected_policy)
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        model_work = self._prepare(provider_retry=retry)
        adapter = ReturnedFailureSequenceAdapter(
            candidate(), ["RATE_LIMITED", "PROVIDER_INTERNAL_ERROR"])

        class PurposeWorker(RoutedTranscriptPolishModelWorker):
            purpose = "document_extraction"

        def make_worker():
            return PurposeWorker(
                scheduler=self.scheduler, router=self.router, adapter=adapter,
                store=self.store, observability=self.observability,
                polish_worker=TranscriptPolishWorker(self.authority),
                routing_policy_ref=selected_policy["policy_version_ref"],
                credential_slot_refs=(profile()["credential_slot_ref"],
                                      second["credential_slot_ref"]),
                provider_retry=retry, clock=lambda: NOW,
            )

        self.assertEqual(make_worker().run_once(model_work)["status"], "retryable")
        self.assertEqual(make_worker().run_once(model_work)["status"], "retryable")
        final = make_worker().run_once(model_work)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(adapter.selected_profile_ids, [
            "profile:test-transcript", "profile:test-transcript",
            "profile:test-retry-backup",
        ])
        self.assertEqual(len(set(adapter.invocation_ids)), 3)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM observability_cost_entries"
        ).fetchone()[0], 3)
        parents = {
            json.loads(row[0])["parent_ref"] for row in self.store.connection.execute(
                "SELECT invocation_json FROM model_invocations WHERE work_order_ref=?",
                (model_work.id,),
            ).fetchall()
        }
        self.assertEqual(len(parents), 3)
        replay = make_worker().run_once(model_work)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(adapter.invocation_ids), 3)

    def test_provider_retry_preserves_declared_tier_order_without_purpose_override(self):
        preferred = profile()
        preferred.update({
            "profile_version_ref": "model-profile-version:zz-preferred:1",
            "id": "profile:zz-preferred", "model": "zz-preferred",
            "family": "zz-preferred",
            "credential_slot_ref": "credential-slot:openclaw:zz-preferred",
        })
        self.router.register_profile(preferred)
        selected_policy = policy()
        selected_policy.update({
            "policy_version_ref": "model-routing-policy-version:tier-provider-retry:1",
            "id": "model-routing-policy:tier-provider-retry",
            "fallback_chains": {
                "tiers": {"cheap": [preferred["id"], profile()["id"]]}
            },
        })
        # A tier orders candidates already authorized by the policy filters.
        # Only an explicit purpose override may escape a legacy profile pin.
        selected_policy["filters"]["allowed_profile_ids"] = [
            preferred["id"], profile()["id"],
        ]
        self.router.register_policy(selected_policy)
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        work = self._prepare(provider_retry=retry)
        adapter = ReturnedFailureSequenceAdapter(
            candidate(), ["RATE_LIMITED", "PROVIDER_INTERNAL_ERROR"]
        )

        class PurposeWorker(RoutedTranscriptPolishModelWorker):
            purpose = "document_extraction"

        def run():
            return PurposeWorker(
                scheduler=self.scheduler, router=self.router, adapter=adapter,
                store=self.store, observability=self.observability,
                polish_worker=TranscriptPolishWorker(self.authority),
                routing_policy_ref=selected_policy["policy_version_ref"],
                credential_slot_refs=(
                    profile()["credential_slot_ref"],
                    preferred["credential_slot_ref"],
                ),
                provider_retry=retry, clock=lambda: NOW,
            ).run_once(work)
        self.assertEqual(run()["status"], "retryable")
        self.assertEqual(adapter.selected_profile_ids, [preferred["id"]])
        self.assertEqual(run()["status"], "retryable")
        self.assertEqual(run()["status"], "succeeded")
        self.assertEqual(adapter.selected_profile_ids, [
            preferred["id"], preferred["id"], profile()["id"],
        ])
        self.assertEqual(len(set(adapter.invocation_ids)), 3)

    def test_unknown_returned_failure_is_terminal(self):
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        model_work = self._prepare(provider_retry=retry)
        adapter = ReturnedFailureSequenceAdapter(candidate(), ["NOT_RATE_LIMITED"])
        worker = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            provider_retry=retry, clock=lambda: NOW,
        )
        self.assertEqual(worker.run_once(model_work)["status"], "failed")
        self.assertEqual(len(adapter.invocation_ids), 1)

    def test_provider_retry_history_tamper_fails_closed_and_nonproof_does_not_reset(self):
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        model_work = self._prepare(provider_retry=retry)
        adapter = ReturnedFailureSequenceAdapter(candidate(), ["RATE_LIMITED"])
        worker = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            provider_retry=retry, clock=lambda: NOW,
        )
        self.assertEqual(worker.run_once(model_work)["status"], "retryable")
        original = dict(self.scheduler.connection.execute(
            "SELECT result_envelope_json,result_envelope_hash,attempt_number "
            "FROM scheduler_result_envelopes WHERE work_order_id=?",
            (model_work.id,),
        ).fetchone())

        class Rows:
            def __init__(self, rows): self.rows = rows
            def fetchall(self): return self.rows
        class Connection:
            def __init__(self, rows): self.rows = rows
            def execute(self, *_): return Rows(self.rows)

        nonproof = ResultEnvelope.from_dict(json.loads(
            original["result_envelope_json"])).to_dict()
        nonproof["id"] = "result:unrelated-capacity-retry"
        nonproof["metadata"] = {"route_decision_ref": "route:unrelated"}
        nonproof_row = {"result_envelope_json": canonical_json(nonproof),
                        "result_envelope_hash": content_hash(nonproof),
                        "attempt_number": 2}
        worker.scheduler = SimpleNamespace(connection=Connection([nonproof_row, original]))
        state = worker._provider_retry_state(model_work)
        self.assertEqual(state["same_profile_retries"], 1)

        missing = json.loads(original["result_envelope_json"])
        del missing["metadata"]["provider_retry_state"]
        missing_row = {"result_envelope_json": canonical_json(missing),
                       "result_envelope_hash": content_hash(missing),
                       "attempt_number": 1}
        worker.scheduler = SimpleNamespace(connection=Connection([missing_row]))
        with self.assertRaises(TranscriptPolishModelWorkerConflict):
            worker._provider_retry_state(model_work)
        malformed_state = json.loads(original["result_envelope_json"])
        malformed_state["metadata"]["provider_retry_state"] = None
        malformed_state_row = {
            "result_envelope_json": canonical_json(malformed_state),
            "result_envelope_hash": content_hash(malformed_state),
            "attempt_number": 1,
        }
        worker.scheduler = SimpleNamespace(connection=Connection([malformed_state_row]))
        with self.assertRaises(TranscriptPolishModelWorkerConflict):
            worker._provider_retry_state(model_work)
        worker.scheduler = SimpleNamespace(connection=Connection([{
            "result_envelope_json": "{", "result_envelope_hash": "0" * 64,
            "attempt_number": 1,
        }]))
        with self.assertRaises(TranscriptPolishModelWorkerConflict):
            worker._provider_retry_state(model_work)
        self.assertEqual(len(adapter.invocation_ids), 1)

    def test_real_unix_adapter_capacity_proof_returns_scheduler_pending(self):
        model_work = self._prepare()
        broker = FakeBroker(
            Path(self.temp.name),
            lambda request: failure_response(
                request,
                dispatch_proof={"authority": "openclaw-model-broker",
                                "state": "definitely_not_sent", "version": "0.1"},
            ),
        )
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(
            broker.path, route_resolver=self.router.get_decision,
            auth_client_id=AUTH_CLIENT_ID,
            auth_key_provider=lambda: AUTH_SECRET,
            expected_agent_id="dalton-model-broker", clock=lambda: NOW,
        )
        result = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            clock=lambda: NOW,
        ).run_once(model_work)
        self.assertEqual(result["status"], "retryable")
        self.assertEqual(self.scheduler.status(model_work.id)["state"], "ready")
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE work_order_ref=?",
            (model_work.id,),
        ).fetchone()[0], 0)

    def test_real_unix_capacity_code_without_proof_is_paid_terminal(self):
        model_work = self._prepare()
        broker = FakeBroker(Path(self.temp.name), failure_response)
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(
            broker.path, route_resolver=self.router.get_decision,
            auth_client_id=AUTH_CLIENT_ID, auth_key_provider=lambda: AUTH_SECRET,
            expected_agent_id="dalton-model-broker", clock=lambda: NOW,
        )
        result = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",), clock=lambda: NOW,
        ).run_once(model_work)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.scheduler.status(model_work.id)["state"], "failed")
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE work_order_ref=?", (model_work.id,)
        ).fetchone()[0], 1)

    def test_real_unix_adapter_returned_failure_retries_as_new_paid_attempt(self):
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        model_work = self._prepare(provider_retry=retry)
        def responder(request):
            if len(broker.requests) == 1:
                return failure_response(
                    request, code="RATE_LIMITED",
                    dispatch_proof={"authority": "openclaw-model-broker",
                                    "state": "provider_completed_failure", "version": "0.1"})
            response = success_response(request, text=canonical_json(candidate()))
            response.update({"provider": "test", "model": "transcript",
                             "canonicalModel": "test/transcript"})
            return seal({key: value for key, value in response.items()
                         if key != "contentHash"})
        broker = FakeBroker(Path(self.temp.name), responder, connections=2)
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(
            broker.path, route_resolver=self.router.get_decision,
            auth_client_id=AUTH_CLIENT_ID, auth_key_provider=lambda: AUTH_SECRET,
            expected_agent_id="dalton-model-broker", clock=lambda: NOW,
        )
        worker = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref="model-routing-policy-version:test-transcript:1",
            credential_slot_refs=("credential-slot:openclaw:test",),
            provider_retry=retry, clock=lambda: NOW,
        )
        self.assertEqual(worker.run_once(model_work)["status"], "retryable")
        self.assertEqual(worker.run_once(model_work)["status"], "succeeded")
        self.assertEqual(len(broker.requests), 2)
        self.assertNotEqual(broker.requests[0]["invocationId"], broker.requests[1]["invocationId"])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE work_order_ref=?",
            (model_work.id,),
        ).fetchone()[0], 2)

    def _worker(self, candidate_wire: dict) -> RoutedTranscriptPolishModelWorker:
        return RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=FakeAdapter(candidate_wire),
            store=self.store,
            observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref=(
                "model-routing-policy-version:test-transcript:1"
            ),
            credential_slot_refs=("credential-slot:openclaw:test",),
            clock=lambda: NOW,
        )

    def test_routed_candidate_is_accounted_verified_and_closes_probe(self) -> None:
        model_work = self._prepare()
        routed = self._worker(candidate()).run_once(model_work)
        self.assertEqual(routed["status"], "succeeded")
        self.assertEqual(routed["accounting"]["cost"]["cost_status"], "actual")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM transcript_polish_artifact_versions"
            ).fetchone()[0],
            1,
        )
        advanced = self.coordinator.advance(self.probe, model_work)
        self.assertEqual(advanced["status"], "succeeded")
        self.assertEqual(
            self.scheduler.formal_result(self.probe.id)["terminal_state"],
            "succeeded",
        )
        artifact_ref = advanced["result"]["artifact_refs"][0]
        self.assertEqual(self.authority.polished_text(artifact_ref), POLISHED)
        replay = self.coordinator.advance(self.probe, model_work)
        self.assertTrue(replay["replayed"])

    def test_purpose_chain_selects_approved_profile_outside_legacy_pin(self) -> None:
        first = profile()
        first.update({
            "profile_version_ref": "model-profile-version:test-transcript-busy:1",
            "id": "profile:test-transcript-busy",
            "model": "transcript-busy",
            "family": "test-transcript-busy",
            "credential_slot_ref": "credential-slot:openclaw:test-busy",
        })
        second = profile()
        second.update({
            "profile_version_ref": "model-profile-version:test-transcript-backup:1",
            "id": "profile:test-transcript-backup",
            "model": "transcript-backup",
            "family": "test-transcript-backup",
            "credential_slot_ref": "credential-slot:openclaw:test-backup",
        })
        self.assertEqual(
            self.router.register_profile(first)["status"], "fresh"
        )
        self.assertEqual(self.router.register_profile(second)["status"], "fresh")
        selected_policy = policy()
        selected_policy.update({
            "policy_version_ref": "model-routing-policy-version:test-transcript-chain:1",
            "id": "model-routing-policy:test-transcript-chain",
            "purpose_overrides": {
                "document_extraction": {
                    "mode": "explicit",
                    "chain": [first["id"], second["id"]],
                }
            },
        })
        self.assertEqual(
            self.router.register_policy(selected_policy)["status"], "fresh"
        )
        adapter = FailFirstRecordingAdapter(candidate(), first["id"])

        class PurposeWorker(RoutedTranscriptPolishModelWorker):
            purpose = "document_extraction"

        model_work = self._prepare()
        worker = PurposeWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=adapter,
            store=self.store,
            observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref=selected_policy["policy_version_ref"],
            credential_slot_refs=(
                "credential-slot:openclaw:test",
                "credential-slot:openclaw:test-busy",
                "credential-slot:openclaw:test-backup",
            ),
            clock=lambda: NOW,
        )
        routed = worker.run_once(model_work)
        self.assertEqual(routed["status"], "succeeded")
        self.assertEqual(
            adapter.selected_profile_ids,
            ["profile:test-transcript-busy", "profile:test-transcript-backup"],
        )
        self.assertEqual(
            routed["route"]["selected_profile_version_ref"],
            second["profile_version_ref"],
        )
        self.assertEqual(len(self.router.chain_links(work_order_id=model_work.id)), 2)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM model_invocations"
            ).fetchone()[0],
            1,
        )

    def test_uncertain_connection_failure_does_not_walk_to_second_profile(self):
        second = profile()
        second.update({
            "profile_version_ref": "model-profile-version:test-uncertain-backup:1",
            "id": "profile:test-uncertain-backup",
            "model": "uncertain-backup",
            "family": "test-uncertain-backup",
            "credential_slot_ref": "credential-slot:openclaw:uncertain-backup",
        })
        self.router.register_profile(second)
        selected_policy = policy()
        selected_policy.update({
            "policy_version_ref": "model-routing-policy-version:test-uncertain:1",
            "id": "model-routing-policy:test-uncertain",
            "purpose_overrides": {
                "document_extraction": {
                    "mode": "explicit",
                    "chain": [profile()["id"], second["id"]],
                }
            },
        })
        self.router.register_policy(selected_policy)
        adapter = UncertainFailureRecordingAdapter(candidate())

        class PurposeWorker(RoutedTranscriptPolishModelWorker):
            purpose = "document_extraction"

        model_work = self._prepare()
        result = PurposeWorker(
            scheduler=self.scheduler, router=self.router, adapter=adapter,
            store=self.store, observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref=selected_policy["policy_version_ref"],
            credential_slot_refs=(profile()["credential_slot_ref"],
                                  second["credential_slot_ref"]),
            clock=lambda: NOW,
        ).run_once(model_work)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(adapter.selected_profile_ids, [profile()["id"]])
        self.assertEqual(len(self.router.chain_links(work_order_id=model_work.id)), 1)

    def test_late_model_result_replays_without_second_execution(self) -> None:
        model_work = self._prepare()
        clock = {"now": NOW}
        self.scheduler.clock = lambda: clock["now"]
        adapter = LateThenReplayAdapter(
            candidate(),
            lambda: clock.__setitem__(
                "now", clock["now"] + timedelta(seconds=31)
            ),
        )
        worker = RoutedTranscriptPolishModelWorker(
            scheduler=self.scheduler,
            router=self.router,
            adapter=adapter,
            store=self.store,
            observability=self.observability,
            polish_worker=TranscriptPolishWorker(self.authority),
            routing_policy_ref=(
                "model-routing-policy-version:test-transcript:1"
            ),
            credential_slot_refs=("credential-slot:openclaw:test",),
            clock=lambda: clock["now"],
        )

        late = worker.run_once(model_work)
        self.assertEqual(late["status"], "retryable")
        self.assertTrue(late["late_completion_rejected"])
        self.assertEqual(adapter.execute_calls, 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM transcript_polish_artifact_versions"
            ).fetchone()[0],
            0,
        )

        recovered = worker.run_once(model_work)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertTrue(recovered["route_replayed"])
        self.assertEqual(adapter.execute_calls, 1)
        self.assertEqual(adapter.replay_calls, 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM transcript_polish_artifact_versions"
            ).fetchone()[0],
            1,
        )

    def test_numeric_drift_is_bounded_retry_and_never_closes_probe(self) -> None:
        model_work = self._prepare()
        drifted = candidate(POLISHED.replace("1.2 billion", "1.3 billion"))
        routed = self._worker(drifted).run_once(model_work)
        self.assertEqual(routed["status"], "retryable")
        self.assertEqual(
            routed["result"]["error"]["code"],
            "MODEL_CANDIDATE_CONSERVATION_REJECTED",
        )
        self.assertIsNone(self.scheduler.formal_result(self.probe.id))
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM transcript_polish_artifact_versions"
            ).fetchone()[0],
            0,
        )

    def test_invalid_candidate_contract_is_bounded_retry(self) -> None:
        model_work = self._prepare()
        routed = self._worker({
            "schema_version": "0.1",
            "segments": [],
        }).run_once(model_work)
        self.assertEqual(routed["status"], "retryable")
        self.assertEqual(
            routed["result"]["error"]["code"],
            "MODEL_OUTPUT_CONTRACT_REJECTED",
        )
        self.assertIsNone(self.scheduler.formal_result(self.probe.id))

    def test_prompt_precomputes_contiguous_bounded_span_hashes(self) -> None:
        parameters = self.probe.metadata["parameters"]
        source = self.authority.model_source_context(
            source_manifest_ref=parameters["source_manifest_ref"],
            source_manifest_hash=parameters["source_manifest_hash"],
            source_content_hash=parameters["source_content_hash"],
        )
        long_text = "word " * 900
        source["resolved_source_text"] = long_text
        source["resolved_source_hash"] = hashlib.sha256(
            long_text.encode("utf-8")
        ).hexdigest()
        prompt = build_transcript_polish_model_prompt(
            source,
            additional_protected_terms=[],
        )
        quoted = json.loads(prompt.split("QUOTED_TRANSCRIPT=", 1)[1])
        spans = quoted["source_segments"]
        self.assertGreater(len(spans), 1)
        self.assertEqual(spans[0]["source_start"], 0)
        self.assertEqual(spans[-1]["source_end"], len(long_text))
        for index, span in enumerate(spans):
            if index:
                self.assertEqual(
                    spans[index - 1]["source_end"], span["source_start"]
                )
            self.assertLessEqual(
                span["source_end"] - span["source_start"], 2_000
            )
            self.assertEqual(
                span["source_sha256"],
                hashlib.sha256(span["source_text"].encode("utf-8")).hexdigest(),
            )

    def test_prompt_exposes_exact_core_auto_protected_terms(self) -> None:
        parameters = self.probe.metadata["parameters"]
        source = self.authority.model_source_context(
            source_manifest_ref=parameters["source_manifest_ref"],
            source_manifest_hash=parameters["source_manifest_hash"],
            source_content_hash=parameters["source_content_hash"],
        )
        source["resolved_source_text"] = "OpenAI spoke."
        source["resolved_source_hash"] = hashlib.sha256(
            source["resolved_source_text"].encode("utf-8")
        ).hexdigest()
        prompt = build_transcript_polish_model_prompt(
            source,
            additional_protected_terms=[],
        )
        quoted = json.loads(prompt.split("QUOTED_TRANSCRIPT=", 1)[1])
        self.assertIn("OpenAI", quoted["core_protected_terms"])


if __name__ == "__main__":
    unittest.main()
