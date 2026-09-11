"""Annual broker retries and leases retain the configured wall-clock bounds."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.annual_report_qualitative import RegisteredAnnualReportDraftWorker
from dalton_core.annual_report_runtime import (
    AnnualReportRuntimeError,
    annual_attempt_lease_seconds,
    scheduler_policy,
)
from dalton_core.model_router import ModelRouter
from dalton_core.observability import ObservabilityStore
from dalton_core.openclaw_model_adapter import (
    BrokerDefinitelyNotSent,
    OpenClawModelAdapter,
    OpenClawModelAdapterError,
)
from dalton_core.store import canonical_json
from dalton_core.store import DaltonStore, content_hash
from dalton_core.scheduler import Scheduler
from dalton_core.sec_authority_harness import MutableClock
from tests.test_openclaw_model_adapter import (
    AUTH_CLIENT_ID,
    AUTH_SECRET,
    FIXED_NOW,
    endpoint_profile,
    routing_policy,
    success_response,
    work_order,
)
from tests.test_transcript_polish_model_worker import FakeAdapter


class _RouteAuthority:
    def __init__(self, chain):
        self.chain = list(chain)

    def get_policy(self, _ref):
        return {"fallback_chains": {"tiers": {"brain": list(self.chain)}}}

    def latest_profiles(self):
        return [{"id": item, "status": "active"} for item in self.chain]


def _execution(*, provider_retry, retries=1, elapsed=7200):
    return {
        "routing_policy_ref": "routing-policy:annual:test",
        "max_seconds": 600,
        "max_attempts": 6,
        "max_elapsed_seconds": elapsed,
        "provider_retry": provider_retry,
        "transport_retry": {
            "max_definitely_not_sent_retries": retries,
            "queue_wait_seconds": 600,
            "retry_backoff_seconds": 2,
        },
    }


class AnnualReportLeasePolicyTests(unittest.TestCase):
    def test_provider_retry_attempt_has_one_route_and_real_queue_bound(self):
        execution = _execution(provider_retry={
            "max_same_profile_retries": 1, "retry_backoff_seconds": 2,
        })
        self.assertEqual(
            annual_attempt_lease_seconds(
                execution, router=_RouteAuthority(("a", "b", "c")),
                purpose="registered_annual_report_draft",
            ),
            2432,
        )
        policy = scheduler_policy((execution, execution))
        self.assertEqual(policy["max_attempts"], 6)
        self.assertEqual(policy["max_lease_seconds"], 7200)
        self.assertEqual(policy["max_total_lease_seconds"], 7200)

    def test_legacy_chain_multiplies_each_route_and_refuses_short_work(self):
        execution = _execution(provider_retry=None, elapsed=8000)
        authority = _RouteAuthority(("a", "b", "c"))
        self.assertEqual(
            annual_attempt_lease_seconds(
                execution, router=authority,
                purpose="registered_annual_report_draft",
            ),
            7236,
        )
        execution["max_elapsed_seconds"] = 7200
        with self.assertRaisesRegex(AnnualReportRuntimeError, "7236s.*7200s"):
            annual_attempt_lease_seconds(
                execution, router=authority,
                purpose="registered_annual_report_draft",
            )


class AnnualReportDefinitelyNotSentRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.router = ModelRouter(
            self.root / "router.sqlite", clock=lambda: FIXED_NOW
        )
        self.addCleanup(self.router.close)
        self.router.register_profile(endpoint_profile())
        self.router.register_policy(routing_policy())
        self.work = work_order()
        self.route = self.router.route(
            self.work,
            attempt_number=1,
            capability="research",
            policy_version_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            required_modalities=["text"],
            required_context_tokens=1000,
            estimated_input_tokens=500,
            estimated_output_tokens=250,
            idempotency_key="annual-transport-route",
        )["decision"]
        self.profile = self.router.get_profile(
            "model-profile-version:research:1"
        )

    def _run(self, maximum):
        path = self.root / f"retry-{maximum}.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(os.fspath(path))
        os.chmod(path, 0o600)
        thread = None

        def serve():
            client, _ = server.accept()
            with client:
                raw = bytearray()
                while b"\n" not in raw:
                    raw.extend(client.recv(16_384))
                request = json.loads(bytes(raw[:-1]).decode("utf-8"))
                # queueWaitMs is broker admission policy rather than provider
                # semantic identity and the adapter deliberately excludes it
                # from the provider response requestHash.
                request.pop("queueWaitMs", None)
                client.sendall(
                    canonical_json(success_response(request)).encode("utf-8")
                    + b"\n"
                )

        sleeps = []

        def retry_sleep(_seconds):
            nonlocal thread
            sleeps.append(1)
            if len(sleeps) == maximum:
                server.listen(1)
                thread = threading.Thread(target=serve, daemon=True)
                thread.start()

        adapter = OpenClawModelAdapter(
            path,
            route_resolver=self.router.get_decision,
            auth_client_id=AUTH_CLIENT_ID,
            auth_key_provider=lambda: AUTH_SECRET,
            expected_agent_id="dalton-model-broker",
            timeout_seconds=600,
            queue_wait_seconds=600,
            clock=lambda: FIXED_NOW,
        )
        worker = object.__new__(RegisteredAnnualReportDraftWorker)
        worker._production_adapter = True
        worker.transport_retry = {
            "max_definitely_not_sent_retries": maximum,
            "queue_wait_seconds": 600,
            "retry_backoff_seconds": 1,
        }
        worker.adapter = adapter
        worker._before_transport_send = lambda *_args: None
        attempts = []
        original_assert = adapter._assert_safe_socket

        def counted_assert():
            attempts.append(1)
            return original_assert()

        try:
            with patch.object(adapter, "_assert_safe_socket", side_effect=counted_assert), \
                    patch("time.sleep", side_effect=retry_sleep):
                if maximum == 0:
                    with self.assertRaises(BrokerDefinitelyNotSent):
                        worker._execute_model(self.work, self.route, self.profile)
                else:
                    _invocation, result = worker._execute_model(
                        self.work, self.route, self.profile
                    )
                    self.assertEqual(result.status, "succeeded")
        finally:
            server.close()
            if thread is not None:
                thread.join(timeout=1)
            path.unlink(missing_ok=True)
        self.assertEqual(len(attempts), maximum + 1)
        self.assertEqual(len(sleeps), maximum)

    def test_configured_retry_bound_zero_one_and_five(self):
        for maximum in (0, 1, 5):
            with self.subTest(maximum=maximum):
                self._run(maximum)

    def test_send_is_refused_when_one_real_queue_call_no_longer_fits(self):
        worker = object.__new__(RegisteredAnnualReportDraftWorker)
        now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
        worker.clock = lambda: now
        worker.transport_retry = {
            "max_definitely_not_sent_retries": 1,
            "queue_wait_seconds": 600,
            "retry_backoff_seconds": 2,
        }
        worker._work_deadline = lambda _work: now + timedelta(seconds=1229)
        admitted = []
        worker._before_model_call = lambda *_args: admitted.append(True)
        work = SimpleNamespace(budget={"max_seconds": 600})
        with self.assertRaisesRegex(
            OpenClawModelAdapterError, "insufficient elapsed budget"
        ):
            worker._before_transport_send(work, {}, {})
        self.assertEqual(admitted, [])

        worker._work_deadline = lambda _work: now + timedelta(seconds=1230)
        worker._before_transport_send(work, {}, {})
        self.assertEqual(admitted, [True])

    def test_real_unix_response_after_old_lease_boundary_is_accepted(self):
        clock = MutableClock(FIXED_NOW)
        store = DaltonStore(self.root / "core.sqlite")
        self.addCleanup(store.close)
        observability = ObservabilityStore(store)
        execution = _execution(provider_retry={
            "max_same_profile_retries": 1, "retry_backoff_seconds": 0,
        }, retries=0, elapsed=9000)
        execution["transport_retry"] = {
            "max_definitely_not_sent_retries": 0,
            "queue_wait_seconds": 7200,
            "retry_backoff_seconds": 2,
        }
        self.assertEqual(
            annual_attempt_lease_seconds(
                execution, router=_RouteAuthority(("unused",)),
                purpose="registered_annual_report_draft",
            ),
            7830,
        )
        scheduler = Scheduler(
            connection=store.connection, clock=clock,
            **scheduler_policy((execution, execution)),
        )
        router = ModelRouter(self.root / "delayed-router.sqlite", clock=clock)
        self.addCleanup(router.close)
        router.register_profile(endpoint_profile())
        router.register_policy(routing_policy())

        answer = canonical_json({
            "schema_version": "0.1",
            "answer": "The filing supports the answer.",
            "candidate": {
                "normalized_statement": "The filing supports the answer.",
                "metric_or_aspect": "business model",
                "period": "FY2025",
                "basis": "reported",
                "cited_match_indexes": [0],
            },
        })

        def respond(request):
            # The real broker response arrives at the full configured
            # queue+call boundary. The completion grace remains available.
            clock.advance(7800)
            semantic = dict(request)
            semantic.pop("queueWaitMs", None)
            return success_response(semantic, text=answer)

        from tests.test_openclaw_model_adapter import FakeBroker

        broker = FakeBroker(self.root, respond)
        self.addCleanup(broker.close)
        adapter = OpenClawModelAdapter(
            broker.path,
            route_resolver=router.get_decision,
            auth_client_id=AUTH_CLIENT_ID,
            auth_key_provider=lambda: AUTH_SECRET,
            expected_agent_id="dalton-model-broker",
            timeout_seconds=600,
            queue_wait_seconds=7200,
            clock=clock,
        )
        wire = work_order().to_dict()
        wire["requested_capabilities"] = [
            "capability:dalton:model:qualitative-research", "research",
        ]
        wire["question"] = "Answer from one registered annual report excerpt."
        wire["budget"].update({
            "max_seconds": 600,
            "max_elapsed_seconds": 9000,
            "max_attempts": 6,
        })
        transport = {
            "max_definitely_not_sent_retries": 0,
            "queue_wait_seconds": 7200,
            "retry_backoff_seconds": 2,
        }
        wire["metadata"] = {
            "stage": "qualitative_model_draft",
            "routing_policy_ref": "model-routing-policy-version:default:1",
            "credential_slot_refs": ["credential-slot:openai:dalton"],
            "provider_retry": execution["provider_retry"],
            "transport_retry": transport,
            "prompt_hash": content_hash(wire["question"]),
            "model_request_binding_hash": "a" * 64,
            "retrieval_match_count": 1,
        }
        from dalton_core.contracts import WorkOrder

        work = WorkOrder.from_dict(wire)
        scheduler.enqueue(work)
        worker = RegisteredAnnualReportDraftWorker(
            scheduler=scheduler, router=router,
            # Construct with a fixture to keep this narrowly about transport;
            # then install the real UDS adapter used by the call below.
            adapter=FakeAdapter(json.loads(answer)),
            store=store, observability=observability, polish_worker=None,
            routing_policy_ref="model-routing-policy-version:default:1",
            credential_slot_refs=["credential-slot:openai:dalton"],
            provider_retry=execution["provider_retry"],
            transport_retry=transport,
            lease_seconds=7830,
            clock=clock,
        )
        worker.adapter = adapter
        worker._production_adapter = True
        outcome = worker.run_once(work)
        self.assertEqual(outcome["status"], "succeeded", outcome)
        self.assertEqual(len(broker.requests), 1)
        self.assertEqual(broker.requests[0]["queueWaitMs"], 7_200_000)
        lease = store.connection.execute(
            "SELECT issued_at,expires_at FROM scheduler_leases "
            "WHERE work_order_id=?", (work.id,),
        ).fetchone()
        self.assertEqual(
            (
                datetime.fromisoformat(lease["expires_at"])
                - datetime.fromisoformat(lease["issued_at"])
            ).total_seconds(),
            7830,
        )


if __name__ == "__main__":
    unittest.main()
