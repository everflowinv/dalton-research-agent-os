from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dalton_core.contracts import WorkOrder
from dalton_core.model_router import ModelRouter, canonical_hash as dalton_hash
from dalton_core.openclaw_model_adapter import (
    BrokerFrameTooLarge,
    BrokerProtocolError,
    BrokerTimeout,
    ModelAdmissionError,
    OpenClawModelAdapter,
    PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
    RouteAuthorityError,
    canonical_hash,
    canonical_json,
    owner_only_secret_file_provider,
)
from dalton_core.scheduler import Scheduler


FIXED_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
AUTH_CLIENT_ID = "client:dalton-runtime"
AUTH_SECRET = b"a" * 64


def endpoint_profile() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "profile_version_ref": "model-profile-version:research:1",
        "id": "profile:research",
        "version": 1,
        "created_at": "2026-08-14T11:00:00+00:00",
        "prior_version_ref": None,
        "provider": "openai",
        "model": "gpt-5.6",
        "family": "gpt-5",
        "adapter_ref": "adapter:openclaw-model-broker:0.1",
        "credential_slot_ref": "credential-slot:openai:dalton",
        "capabilities": ["research", "verify"],
        "modalities": ["text"],
        "context": {
            "max_context_tokens": 100_000,
            "max_output_tokens": 2_000,
        },
        "availability": {
            "state": "available",
            "checked_at": "2026-08-14T11:00:00+00:00",
            "valid_until": "2026-08-15T12:00:00+00:00",
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": 1.0,
            "output_per_million_usd": 2.0,
        },
        "limits": {
            "max_input_tokens": 10_000,
            "max_output_tokens": 1_500,
            "max_total_tokens": 11_500,
            "max_cost_usd": 2.0,
        },
    }


def routing_policy() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "policy_version_ref": "model-routing-policy-version:default:1",
        "id": "model-routing-policy:default",
        "version": 1,
        "created_at": "2026-08-14T11:00:00+00:00",
        "prior_version_ref": None,
        "filters": {
            "allowed_profile_ids": ["profile:research"],
            "allowed_providers": ["openai"],
            "allowed_families": [],
            "allowed_adapter_refs": ["adapter:openclaw-model-broker:0.1"],
            "required_modalities": ["text"],
            "family_independence_capabilities": [],
        },
        "ordered_preferences": [
            {"field": "profile_version_ref", "direction": "asc"}
        ],
    }


def work_order(
    *,
    question: str = "Explain the research evidence",
    budget: dict[str, Any] | None = None,
) -> WorkOrder:
    return WorkOrder.from_dict(
        {
            "schema_version": "0.1",
            "id": "work:model-completion-1",
            "created_at": "2026-08-14T11:30:00+00:00",
            "updated_at": "2026-08-14T11:30:00+00:00",
            "question": question,
            "requested_capabilities": ["research"],
            "runtime_profile_ref": "runtime-profile:dalton:0.1",
            "budget": budget
            or {
                "max_input_tokens": 1_000,
                "max_output_tokens": 500,
                "max_total_tokens": 1_500,
                "max_cost_usd": 0.5,
            },
            "idempotency_key": "work-key:model-completion-1",
            "declared_side_effects": [],
            "status": "ready",
            "input_refs": ["claim:one"],
            "metadata": {},
        }
    )


def seal(response: dict[str, Any]) -> dict[str, Any]:
    response = dict(response)
    response["contentHash"] = canonical_hash(response)
    return response


def core_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request.items()
        if key not in {"auth", "replayOnly"}
    }


def success_response(
    request: dict[str, Any],
    *,
    text: str = "Bound answer",
    usage: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    core = core_request(request)
    return seal(
        {
            "schemaVersion": "0.1",
            "brokerVersion": "0.1.0-spike.1",
            "runtimeVersion": "2026.8.13",
            "invocationId": core["invocationId"],
            "workOrderId": core["workOrderId"],
            "profileId": core["profileId"],
            "requestHash": canonical_hash(core),
            "idempotencyStatus": "fresh",
            "ok": True,
            "provider": "openai",
            "model": "gpt-5.6",
            "canonicalModel": "openai/gpt-5.6",
            "agentId": "dalton-model-broker",
            "text": text,
            "usage": usage
            or {
                "inputTokens": 100,
                "outputTokens": 50,
                "cacheReadTokens": 0,
                "cacheWriteTokens": None,
                "totalTokens": 150,
            },
            "cost": cost or {"available": True, "usd": 0.01},
            "error": None,
        }
    )


def failure_response(
    request: dict[str, Any],
    *,
    code: str = "BUSY",
    message: str = "broker concurrency limit reached",
) -> dict[str, Any]:
    core = core_request(request)
    return seal(
        {
            "schemaVersion": "0.1",
            "brokerVersion": "0.1.0-spike.1",
            "runtimeVersion": "2026.8.13",
            "invocationId": core["invocationId"],
            "workOrderId": core["workOrderId"],
            "profileId": core["profileId"],
            "requestHash": canonical_hash(core),
            "idempotencyStatus": "fresh",
            "ok": False,
            "provider": None,
            "model": None,
            "canonicalModel": None,
            "agentId": None,
            "text": None,
            "usage": {
                "inputTokens": None,
                "outputTokens": None,
                "cacheReadTokens": None,
                "cacheWriteTokens": None,
                "totalTokens": None,
            },
            "cost": {"available": False, "usd": None},
            "error": {"code": code, "message": message},
        }
    )


class FakeBroker:
    def __init__(
        self,
        directory: Path,
        responder: Callable[[dict[str, Any]], dict[str, Any] | bytes | None],
        *,
        connections: int = 1,
    ) -> None:
        self.path = directory / f"broker-{time.time_ns()}.sock"
        self.requests: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []
        self._responder = responder
        self._connections = connections
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(os.fspath(self.path))
        os.chmod(self.path, 0o600)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            for _ in range(self._connections):
                client, _ = self._server.accept()
                with client:
                    request_bytes = bytearray()
                    while b"\n" not in request_bytes:
                        chunk = client.recv(16_384)
                        if not chunk:
                            return
                        request_bytes.extend(chunk)
                    request = json.loads(bytes(request_bytes[:-1]).decode("utf-8"))
                    self.requests.append(request)
                    response = self._responder(request)
                    if response is None:
                        return
                    if isinstance(response, bytes):
                        payload = response
                    else:
                        payload = canonical_json(response).encode("utf-8") + b"\n"
                    try:
                        client.sendall(payload)
                    except BrokenPipeError:
                        pass
        except OSError:
            # Admission may fail before the adapter connects; closing the
            # fixture then aborts accept on macOS.
            pass
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self._server.close()

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if self.errors:
            raise self.errors[0]


class OpenClawModelAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.router = ModelRouter(self.directory / "router.sqlite", clock=lambda: FIXED_NOW)
        self.router.register_profile(endpoint_profile())
        self.router.register_policy(routing_policy())
        self.work = work_order()
        routed = self.router.route(
            self.work,
            attempt_number=1,
            capability="research",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1_000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            idempotency_key="route-key:model-completion-1",
        )
        self.route = routed["decision"]
        self.profile = self.router.get_profile("model-profile-version:research:1")

    def tearDown(self) -> None:
        self.router.close()
        self.temp.cleanup()

    def run_with(
        self,
        responder: Callable[[dict[str, Any]], dict[str, Any] | bytes | None],
        *,
        work: WorkOrder | None = None,
        route: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
        timeout: float = 1.0,
        frame_limit: int = 262_144,
        expected_agent_id: str = "dalton-model-broker",
        route_resolver: Callable[[str], dict[str, Any] | None] | None = None,
        auth_key_provider: Callable[[], bytes] | None = None,
        provider_control_mode: str = "provider-controlled-v1",
    ):
        broker = FakeBroker(self.directory, responder)
        try:
            adapter = OpenClawModelAdapter(
                broker.path,
                route_resolver=(
                    self.router.get_decision
                    if route_resolver is None
                    else route_resolver
                ),
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=(
                    (lambda: AUTH_SECRET)
                    if auth_key_provider is None
                    else auth_key_provider
                ),
                timeout_seconds=timeout,
                max_frame_bytes=frame_limit,
                expected_agent_id=expected_agent_id,
                provider_control_mode=provider_control_mode,
                clock=lambda: FIXED_NOW,
            )
            result = adapter.execute(
                work or self.work,
                route or self.route,
                profile or self.profile,
            )
            return result, broker
        except Exception:
            broker.close()
            raise

    def test_hashing_matches_node_broker_canonical_vectors(self) -> None:
        # Generated once with the broker's canonicalJson/contentHash functions;
        # these constants prevent a Python-only fake from masking number-format
        # differences such as Node's 1.0 -> 1 and 1e-6 -> 0.000001.
        self.assertEqual(
            canonical_hash(
                {
                    "cost": {"available": True, "usd": 1.0},
                    "usage": {"inputTokens": 1, "totalTokens": 1},
                }
            ),
            "cf62732aa84cc27964a4a1bcdfa3d0b799c2b12441176d87073a401a002734d7",
        )
        self.assertEqual(
            canonical_hash({"x": 1e-6, "y": 1e21, "z": -0.0}),
            "122b164adb03e65ea39e9068a98e35f94486b0b9fe444ece0f5b034678e7253c",
        )
        for invalid in ({"value": "\ud800"}, {"\udfff": "value"}):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(BrokerProtocolError):
                    canonical_json(invalid)
                with self.assertRaises(BrokerProtocolError):
                    canonical_hash(invalid)

    def test_success_uses_only_admitted_authority_and_returns_uncommitted_contracts(self) -> None:
        (invocation, result), broker = self.run_with(success_response)
        broker.close()
        self.assertEqual(len(broker.requests), 1)
        request = broker.requests[0]
        self.assertEqual(
            set(request),
            {
                "schemaVersion", "invocationId", "workOrderId", "profileId",
                "model", "prompt", "maxTokens", "timeoutMs", "auth",
            },
        )
        self.assertEqual(request["prompt"], self.work.question)
        self.assertEqual(request["model"], "openai/gpt-5.6")
        self.assertEqual(request["profileId"], "profile:research")
        self.assertEqual(request["maxTokens"], 500)
        self.assertEqual(
            set(request["auth"]),
            {"scheme", "clientId", "timestampMs", "nonce", "mac"},
        )
        self.assertEqual(request["auth"]["scheme"], "hmac-sha256-v1")
        self.assertEqual(request["auth"]["clientId"], AUTH_CLIENT_ID)
        self.assertRegex(request["auth"]["nonce"], r"^[0-9a-f]{32}$")
        unsigned = dict(request)
        unsigned["auth"] = {
            key: value for key, value in request["auth"].items() if key != "mac"
        }
        self.assertEqual(
            request["auth"]["mac"],
            hmac.new(
                AUTH_SECRET,
                canonical_json(unsigned).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )
        self.assertNotIn(AUTH_SECRET.decode(), canonical_json(request))
        for forbidden in ("agent", "baseUrl", "headers", "key", "authProfile"):
            self.assertNotIn(forbidden, request)
        self.assertEqual(invocation.profile_ref, self.profile["profile_version_ref"])
        self.assertEqual(invocation.model_family, "gpt-5")
        self.assertEqual(invocation.parent_ref, self.route["id"])
        self.assertEqual(invocation.output_refs, result.artifact_refs)
        self.assertEqual(invocation.usage["authority_status"], "uncommitted")
        self.assertEqual(invocation.usage["raw_provider_telemetry"]["cost"]["usd"], 0.01)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.usage_refs, (f"usage:{invocation.id}",))
        self.assertIs(result.metadata["required_provider_controls"], False)
        self.assertIsNone(result.metadata["provider_control_schema_hash"])
        serialized = canonical_json({"invocation": invocation.to_dict(), "result": result.to_dict()})
        self.assertNotIn(self.work.question, serialized)
        self.assertNotIn(AUTH_SECRET.decode(), serialized)
        scheduler = Scheduler(
            self.directory / "scheduler.sqlite",
            clock=lambda: FIXED_NOW,
            max_attempts=1,
        )
        try:
            scheduler.enqueue(self.work)
            lease = scheduler.claim("worker:openclaw", work_order_id=self.work.id)
            accepted = scheduler.complete(
                self.work.id,
                lease["attempt"]["attempt_number"],
                "worker:openclaw",
                lease["lease_token"],
                result,
                idempotency_key="completion:model-completion-1",
            )
            self.assertEqual(accepted["work_state"], "succeeded")
        finally:
            scheduler.close()

    def test_independent_verifier_binds_required_provider_controls(self) -> None:
        verifier = WorkOrder.from_dict({
            **self.work.to_dict(),
            "id": "work:model-verification-1",
            "question": "Verify the exact thesis impact assessment",
            "requested_capabilities": ["verify"],
            "idempotency_key": "work-key:model-verification-1",
            "metadata": {"verifier_output_schema_version": "0.2"},
        })
        routed = self.router.route(
            verifier,
            attempt_number=1,
            capability="verify",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1_000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            producer_family="anthropic-claude",
            idempotency_key="route-key:model-verification-1",
        )["decision"]
        (invocation, result), broker = self.run_with(
            success_response,
            work=verifier,
            route=routed,
        )
        broker.close()
        controls = broker.requests[0]["requiredControls"]
        self.assertEqual(
            set(controls),
            {
                "maxInputTokens", "maxOutputTokens", "maxTotalTokens",
                "maxCostUsd", "structuredOutput",
            },
        )
        self.assertEqual(controls["maxInputTokens"], 1_000)
        self.assertEqual(controls["maxOutputTokens"], 500)
        self.assertEqual(controls["maxTotalTokens"], 1_500)
        self.assertEqual(controls["maxCostUsd"], 0.5)
        structured = controls["structuredOutput"]
        self.assertEqual(structured["schemaName"], "thesis_impact_verifier_output_v0_2")
        self.assertEqual(structured["schemaHash"], canonical_hash(structured["jsonSchema"]))
        self.assertTrue(result.metadata["required_provider_controls"])
        self.assertEqual(
            result.metadata["provider_control_schema_hash"],
            structured["schemaHash"],
        )
        self.assertEqual(invocation.granularity.value, "verification")

    def test_posthoc_mode_is_explicit_and_calibration_only(self) -> None:
        verifier = WorkOrder.from_dict({
            **self.work.to_dict(),
            "id": "work:model-calibration-posthoc",
            "question": "Verify one frozen calibration case",
            "requested_capabilities": ["verify"],
            "idempotency_key": "work-key:model-calibration-posthoc",
            "metadata": {
                "phase": "verification-calibration",
                "execution_tier": PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
                "verifier_output_schema_version": "0.2",
            },
        })
        routed = self.router.route(
            verifier,
            attempt_number=1,
            capability="verify",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1_000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            producer_family="anthropic-claude",
            idempotency_key="route-key:model-calibration-posthoc",
        )["decision"]
        (invocation, result), broker = self.run_with(
            success_response,
            work=verifier,
            route=routed,
            provider_control_mode=PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        )
        broker.close()
        self.assertNotIn("requiredControls", broker.requests[0])
        self.assertEqual(invocation.granularity.value, "verification")
        self.assertFalse(result.metadata["required_provider_controls"])
        self.assertEqual(
            result.metadata["provider_control_mode"],
            PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
        )

        production_shaped = WorkOrder.from_dict({
            **verifier.to_dict(),
            "id": "work:model-production-posthoc-forbidden",
            "idempotency_key": "work-key:model-production-posthoc-forbidden",
            "metadata": {"verifier_output_schema_version": "0.2"},
        })
        production_route = self.router.route(
            production_shaped,
            attempt_number=1,
            capability="verify",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1_000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            producer_family="anthropic-claude",
            idempotency_key="route-key:model-production-posthoc-forbidden",
        )["decision"]
        with self.assertRaisesRegex(ModelAdmissionError, "restricted to exact calibration"):
            self.run_with(
                success_response,
                work=production_shaped,
                route=production_route,
                provider_control_mode=PROVIDER_CONTROL_MODE_CALIBRATION_POSTHOC,
            )

    def test_request_and_response_hashes_and_route_hash_are_enforced(self) -> None:
        def bad_request_hash(request: dict[str, Any]) -> dict[str, Any]:
            response = success_response(request)
            response["requestHash"] = "0" * 64
            return seal({key: value for key, value in response.items() if key != "contentHash"})

        with self.assertRaisesRegex(BrokerProtocolError, "requestHash"):
            self.run_with(bad_request_hash)

        def bad_content_hash(request: dict[str, Any]) -> dict[str, Any]:
            response = success_response(request)
            response["contentHash"] = "0" * 64
            return response

        with self.assertRaisesRegex(BrokerProtocolError, "contentHash"):
            self.run_with(bad_content_hash)

        tampered = dict(self.route)
        tampered["selected_profile_hash"] = "0" * 64
        with self.assertRaisesRegex(RouteAuthorityError, "Router authority"):
            self.run_with(success_response, route=tampered)

    def test_route_must_exactly_match_read_only_router_authority(self) -> None:
        forged = dict(self.route)
        forged["policy_hash"] = "0" * 64
        forged["content_hash"] = dalton_hash(
            {key: value for key, value in forged.items() if key != "content_hash"}
        )
        with self.assertRaisesRegex(RouteAuthorityError, "differs"):
            self.run_with(success_response, route=forged)

        missing = dict(self.route)
        missing["id"] = "route-decision:missing"
        missing["content_hash"] = dalton_hash(
            {key: value for key, value in missing.items() if key != "content_hash"}
        )
        with self.assertRaisesRegex(RouteAuthorityError, "missing"):
            self.run_with(success_response, route=missing)

        mismatched_authority = dict(self.route)
        mismatched_authority["request_hash"] = "f" * 64
        mismatched_authority["content_hash"] = dalton_hash(
            {
                key: value
                for key, value in mismatched_authority.items()
                if key != "content_hash"
            }
        )
        with self.assertRaisesRegex(RouteAuthorityError, "differs"):
            self.run_with(
                success_response,
                route_resolver=lambda _decision_id: mismatched_authority,
            )

    def test_unknown_usage_and_cost_remain_null_uncommitted_telemetry(self) -> None:
        unknown = {
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadTokens": None,
            "cacheWriteTokens": None,
            "totalTokens": None,
        }
        (invocation, _), broker = self.run_with(
            lambda request: success_response(
                request,
                usage=unknown,
                cost={"available": False, "usd": None},
            )
        )
        broker.close()
        self.assertIsNone(invocation.usage["input_tokens"])
        self.assertIsNone(invocation.usage["raw_provider_telemetry"]["cost"]["usd"])
        self.assertEqual(invocation.usage["measurement_status"], "unavailable")

    def test_provider_usage_and_cost_over_budget_fail_closed(self) -> None:
        excessive_usage = {
            "inputTokens": 1_001,
            "outputTokens": 1,
            "cacheReadTokens": None,
            "cacheWriteTokens": None,
            "totalTokens": 1_002,
        }
        for response in (
            lambda request: success_response(request, usage=excessive_usage),
            lambda request: success_response(
                request, cost={"available": True, "usd": 0.51}
            ),
        ):
            with self.subTest(response=response):
                (invocation, result), broker = self.run_with(response)
                broker.close()
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["code"], "PROVIDER_BUDGET_EXCEEDED")
                self.assertEqual(result.outputs, {})
                self.assertEqual(
                    invocation.usage["metering_source"], "provider_reported"
                )

    def test_actual_provider_model_and_agent_are_exact(self) -> None:
        for field, value in (
            ("provider", "other"),
            ("canonicalModel", "openai/other"),
            ("agentId", "general-agent"),
        ):
            with self.subTest(field=field):
                def responder(request: dict[str, Any], field=field, value=value):
                    response = success_response(request)
                    response[field] = value
                    return seal(
                        {key: item for key, item in response.items() if key != "contentHash"}
                    )

                with self.assertRaises(BrokerProtocolError):
                    self.run_with(responder)

        def configured_agent(request: dict[str, Any]) -> dict[str, Any]:
            response = success_response(request)
            response["agentId"] = "dalton-research-agent"
            return seal(
                {key: item for key, item in response.items() if key != "contentHash"}
            )

        (_, result), broker = self.run_with(
            configured_agent, expected_agent_id="dalton-research-agent"
        )
        broker.close()
        self.assertEqual(result.status, "succeeded")

    def test_request_and_response_frame_limits(self) -> None:
        large_work = work_order(question="q" * 2_000)
        # Obtain a second immutable authority decision for the larger prompt;
        # the adapter may not accept a caller-rehashed clone of the first one.
        changed_route = self.router.route(
            large_work,
            attempt_number=2,
            decision_kind="retry",
            previous_decision_ref=self.route["id"],
            capability="research",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1_000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            idempotency_key="route-key:model-completion-large",
        )["decision"]
        with self.assertRaises(BrokerFrameTooLarge):
            self.run_with(success_response, work=large_work, route=changed_route, frame_limit=1024)
        with self.assertRaises(BrokerFrameTooLarge):
            self.run_with(
                lambda request: success_response(request, text="x" * 5_000),
                frame_limit=1024,
            )

    def test_timeout_is_hard_wall_clock_boundary(self) -> None:
        def slow(_request: dict[str, Any]):
            time.sleep(0.2)
            return None

        with self.assertRaises(BrokerTimeout):
            self.run_with(slow, timeout=0.05)

    def test_repeated_invocation_rechecks_authority_and_accepts_broker_duplicate(self) -> None:
        resolve_calls: list[str] = []
        response_count = 0

        def resolver(decision_id: str) -> dict[str, Any]:
            resolve_calls.append(decision_id)
            return self.router.get_decision(decision_id)

        def responder(request: dict[str, Any]) -> dict[str, Any]:
            nonlocal response_count
            response_count += 1
            response = success_response(request)
            if response_count == 2:
                response["idempotencyStatus"] = "duplicate"
                response = seal(
                    {
                        key: value
                        for key, value in response.items()
                        if key != "contentHash"
                    }
                )
            return response

        broker = FakeBroker(self.directory, responder, connections=2)
        try:
            adapter = OpenClawModelAdapter(
                broker.path,
                route_resolver=resolver,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=lambda: AUTH_SECRET,
                clock=lambda: FIXED_NOW,
            )
            first_invocation, first_result = adapter.execute(
                self.work, self.route, self.profile
            )
            second_invocation, second_result = adapter.execute(
                self.work, self.route, self.profile
            )
        finally:
            broker.close()
        self.assertEqual(first_invocation.id, second_invocation.id)
        self.assertEqual(first_result.id, second_result.id)
        self.assertEqual(
            second_result.metadata["broker_idempotency_status"], "duplicate"
        )
        self.assertEqual(resolve_calls, [self.route["id"], self.route["id"]])
        self.assertEqual(core_request(broker.requests[0]), core_request(broker.requests[1]))
        self.assertNotEqual(
            broker.requests[0]["auth"]["nonce"], broker.requests[1]["auth"]["nonce"]
        )
        self.assertNotEqual(
            broker.requests[0]["auth"]["mac"], broker.requests[1]["auth"]["mac"]
        )

    def test_replay_only_is_authenticated_and_returns_only_a_duplicate(self) -> None:
        def responder(request: dict[str, Any]) -> dict[str, Any]:
            self.assertIs(request["replayOnly"], True)
            unsigned = dict(request)
            unsigned["auth"] = {
                key: value for key, value in request["auth"].items() if key != "mac"
            }
            expected = hmac.new(
                AUTH_SECRET,
                canonical_json(unsigned).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            self.assertEqual(request["auth"]["mac"], expected)
            response = success_response(request)
            response["idempotencyStatus"] = "duplicate"
            return seal(
                {key: value for key, value in response.items() if key != "contentHash"}
            )

        broker = FakeBroker(self.directory, responder)
        try:
            adapter = OpenClawModelAdapter(
                broker.path,
                route_resolver=self.router.get_decision,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=lambda: AUTH_SECRET,
                clock=lambda: FIXED_NOW,
            )
            invocation, result = adapter.replay(
                self.work, self.route, self.profile
            )
        finally:
            broker.close()

        self.assertEqual(invocation.parent_ref, self.route["id"])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.metadata["broker_request_mode"], "replay_only")
        self.assertEqual(result.metadata["broker_idempotency_status"], "duplicate")
        self.assertEqual(
            core_request(broker.requests[0]),
            {
                key: value
                for key, value in broker.requests[0].items()
                if key not in {"auth", "replayOnly"}
            },
        )

        broker = FakeBroker(self.directory, success_response)
        try:
            adapter = OpenClawModelAdapter(
                broker.path,
                route_resolver=self.router.get_decision,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=lambda: AUTH_SECRET,
                clock=lambda: FIXED_NOW,
            )
            with self.assertRaisesRegex(
                BrokerProtocolError, "durable duplicate"
            ):
                adapter.replay(self.work, self.route, self.profile)
        finally:
            broker.close()

    def test_auth_is_mandatory_wrong_key_fails_closed_and_secret_never_escapes(self) -> None:
        with self.assertRaises(TypeError):
            OpenClawModelAdapter(  # type: ignore[call-arg]
                self.directory / "missing.sock",
                route_resolver=self.router.get_decision,
            )

        def verifying_responder(request: dict[str, Any]) -> dict[str, Any]:
            unsigned = dict(request)
            unsigned["auth"] = {
                key: value for key, value in request["auth"].items() if key != "mac"
            }
            expected = hmac.new(
                AUTH_SECRET,
                canonical_json(unsigned).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, request["auth"]["mac"]):
                return seal(
                    {
                        "schemaVersion": "0.1",
                        "brokerVersion": "0.1.0-spike.1",
                        "runtimeVersion": "2026.8.13",
                        "ok": False,
                        "error": {
                            "code": "AUTH_INVALID",
                            "message": "authentication envelope is invalid",
                        },
                    }
                )
            return success_response(request)

        wrong_secret = b"b" * 64
        with self.assertRaises(BrokerProtocolError) as caught:
            self.run_with(
                verifying_responder,
                auth_key_provider=lambda: wrong_secret,
            )
        self.assertNotIn(wrong_secret.decode(), str(caught.exception))
        secret_error = "a" * 64

        def leaking_provider() -> bytes:
            raise RuntimeError(secret_error)

        with self.assertRaises(ModelAdmissionError) as provider_error:
            self.run_with(success_response, auth_key_provider=leaking_provider)
        self.assertNotIn(secret_error, str(provider_error.exception))
        self.assertIsNone(provider_error.exception.__context__)

    def test_indeterminate_duplicate_is_failed_without_automatic_replay(self) -> None:
        def indeterminate(request: dict[str, Any]) -> dict[str, Any]:
            response = failure_response(
                request,
                code="IDEMPOTENCY_INDETERMINATE",
                message="prior host completion may have run; automatic replay is blocked",
            )
            response["idempotencyStatus"] = "duplicate"
            return seal(
                {key: value for key, value in response.items() if key != "contentHash"}
            )

        (_, result), broker = self.run_with(indeterminate)
        broker.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "IDEMPOTENCY_INDETERMINATE")
        self.assertEqual(result.metadata["broker_idempotency_status"], "duplicate")

    @unittest.skipUnless(shutil.which("node"), "Node is required for broker conformance")
    def test_real_node_broker_hmac_uds_conformance(self) -> None:
        broker_root = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "openclaw-model-broker"
        )
        if not (broker_root / "src" / "auth.mjs").exists():
            self.skipTest("Dalton OpenClaw model broker source is unavailable")
        state_dir = self.directory / "node-broker-state"
        script = r'''
import { ModelBroker } from "./src/broker.mjs";
import { BrokerServer } from "./src/server.mjs";
const runtime={version:"node-hmac-conformance",llm:{complete:async (x)=>({text:"real-node-bound",provider:"openai",model:"gpt-5.6",agentId:x.agentId,usage:{inputTokens:10,outputTokens:2,totalTokens:12,costUsd:0.1}})}};
const config={dedicatedAgentId:"dalton-model-broker",clientId:"client:dalton-runtime",socketName:"broker.sock",profiles:[{id:"profile:research",model:"openai/gpt-5.6",maxTokens:2000,timeoutMs:5000}]};
const server=new BrokerServer(new ModelBroker(runtime,config)); const path=await server.start(process.env.DALTON_TEST_STATE_DIR); console.log(path);
process.on("SIGTERM",async()=>{await server.stop();process.exit(0)});
'''
        process = subprocess.Popen(
            ["node", "--input-type=module", "-e", script],
            cwd=broker_root,
            env={**os.environ, "DALTON_TEST_STATE_DIR": os.fspath(state_dir)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        node_router = None
        try:
            socket_path = process.stdout.readline().strip()
            if not socket_path:
                self.fail("real Node broker did not publish its UDS endpoint")
            current = datetime.now(timezone.utc)
            current_profile = endpoint_profile()
            current_profile.update(
                {
                    "created_at": (current - timedelta(hours=2)).isoformat(),
                    "availability": {
                        "state": "available",
                        "checked_at": (current - timedelta(hours=1)).isoformat(),
                        "valid_until": (current + timedelta(hours=1)).isoformat(),
                    },
                }
            )
            node_router = ModelRouter(
                self.directory / "node-router.sqlite",
                clock=lambda: datetime.now(timezone.utc),
            )
            node_router.register_profile(current_profile)
            node_router.register_policy(routing_policy())
            current_route = node_router.route(
                self.work,
                attempt_number=1,
                capability="research",
                policy_version_ref="model-routing-policy-version:default:1",
                credential_slot_refs=["credential-slot:openai:dalton"],
                required_modalities=["text"],
                required_context_tokens=1_000,
                estimated_input_tokens=500,
                estimated_output_tokens=250,
                idempotency_key="route-key:node-hmac-conformance",
            )["decision"]
            stored_profile = node_router.get_profile(
                "model-profile-version:research:1"
            )
            adapter = OpenClawModelAdapter(
                socket_path,
                route_resolver=node_router.get_decision,
                auth_client_id=AUTH_CLIENT_ID,
                auth_key_provider=owner_only_secret_file_provider(
                    state_dir / "broker.sock.key"
                ),
                clock=lambda: datetime.now(timezone.utc),
            )
            invocation, result = adapter.execute(
                self.work, current_route, stored_profile
            )
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.outputs["text"], "real-node-bound")
            self.assertEqual(invocation.usage["input_tokens"], 10)
            duplicate_invocation, duplicate_result = adapter.execute(
                self.work, current_route, stored_profile
            )
            self.assertEqual(duplicate_invocation.id, invocation.id)
            self.assertEqual(
                duplicate_result.metadata["broker_idempotency_status"], "duplicate"
            )
            verifier = WorkOrder.from_dict({
                **self.work.to_dict(),
                "id": "work:node-controlled-verifier",
                "question": "Verify one exact structured assessment",
                "requested_capabilities": ["verify"],
                "idempotency_key": "work-key:node-controlled-verifier",
                "metadata": {"verifier_output_schema_version": "0.2"},
            })
            verifier_route = node_router.route(
                verifier,
                attempt_number=1,
                capability="verify",
                policy_version_ref="model-routing-policy-version:default:1",
                credential_slot_refs=["credential-slot:openai:dalton"],
                required_modalities=["text"],
                required_context_tokens=1_000,
                estimated_input_tokens=500,
                estimated_output_tokens=250,
                producer_family="anthropic-claude",
                idempotency_key="route-key:node-controlled-verifier",
            )["decision"]
            _, controlled_result = adapter.execute(
                verifier, verifier_route, stored_profile
            )
            self.assertEqual(controlled_result.status, "failed")
            self.assertEqual(
                controlled_result.error["code"],
                "REQUIRED_CONTROLS_UNAVAILABLE",
            )
            self.assertTrue(
                controlled_result.metadata["required_provider_controls"]
            )
        finally:
            if node_router is not None:
                node_router.close()
            process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_credential_shaped_response_fields_are_rejected(self) -> None:
        def top_level_secret(request: dict[str, Any]) -> dict[str, Any]:
            response = success_response(request)
            response["apiKey"] = "should-never-cross"
            return seal({key: value for key, value in response.items() if key != "contentHash"})

        with self.assertRaisesRegex(BrokerProtocolError, "unexpected shape"):
            self.run_with(top_level_secret)

        def nested_secret(request: dict[str, Any]) -> dict[str, Any]:
            response = success_response(request)
            response["usage"]["accessToken"] = "should-never-cross"
            return seal({key: value for key, value in response.items() if key != "contentHash"})

        with self.assertRaisesRegex(BrokerProtocolError, "usage"):
            self.run_with(nested_secret)

    def test_closed_broker_error_becomes_failed_contract_and_prompt_echo_is_rejected(self) -> None:
        (invocation, result), broker = self.run_with(failure_response)
        broker.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["source"], "openclaw-model-broker")
        self.assertEqual(invocation.usage["measurement_status"], "unavailable")
        self.assertNotIn(self.work.question, canonical_json(result.to_dict()))
        with self.assertRaisesRegex(BrokerProtocolError, "unsafe"):
            self.run_with(
                lambda request: failure_response(
                    request, message=f"host failed for {request['prompt']}"
                )
            )


if __name__ == "__main__":
    unittest.main()
