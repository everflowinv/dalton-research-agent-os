from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.agenda_coordinator import (
    AgendaCoordinator,
    AgendaCoordinatorConfig,
    CoordinatorError,
)
from dalton_core.contracts import InvocationGranularity, ModelInvocation, ResultEnvelope, WorkOrder
from dalton_core.governance_cli import ephemeral_call
from dalton_core.model_deployment import install_openclaw_catalog
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from dalton_core.writer_server import CORE_OPERATIONS, Principal, write_token_config


class FakeAdapter:
    calls = 0
    last_question = None

    def execute(self, work, route, profile):
        type(self).calls += 1
        type(self).last_question = work.question
        completed = "2026-08-14T10:00:01+00:00"
        invocation = ModelInvocation(
            schema_version="0.1", id="invocation:agenda-test", created_at=completed,
            work_order_ref=work.id, profile_ref=profile["profile_version_ref"],
            granularity=InvocationGranularity.TASK, capability="extract",
            provider=profile["provider"], model=profile["model"],
            model_family=profile["family"], input_refs=work.input_refs, output_refs=(),
            started_at="2026-08-14T10:00:00+00:00", completed_at=completed,
            usage={"input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
                   "cache_read_tokens": 0, "cache_write_tokens": 0,
                   "metering_source": "provider_reported", "measurement_status": "partial",
                   "authority_status": "uncommitted", "raw_provider_telemetry": {}},
            side_effects=(), runtime_ref=profile["adapter_ref"], actor_ref="broker:test",
            parent_ref=route["id"], environment_hash="env:test",
        )
        text = '{"candidates":[' \
            '{"question":"What changed in pricing?","answer_criteria":"Quantify price and volume","features":{"mandate_relevance":3,"catalyst_urgency":3,"evidence_staleness":2,"decision_impact":3},"rationale":"Affects earnings","source_refs":["event:event-1"]},' \
            '{"question":"Is evidence stale?","answer_criteria":"Find a newer primary source","features":{"mandate_relevance":2,"catalyst_urgency":1,"evidence_staleness":3,"decision_impact":2},"rationale":"Refresh needed","source_refs":["evidence:evidence-1"]},' \
            '{"question":"Does the filing change the thesis?","answer_criteria":"Map filing facts to thesis drivers","features":{"mandate_relevance":3,"catalyst_urgency":2,"evidence_staleness":1,"decision_impact":3},"rationale":"Decision relevant","source_refs":["filing:0001"]}' \
            ']}'
        result = ResultEnvelope(
            schema_version="0.1", id="result:agenda-test", created_at=completed,
            work_order_ref=work.id, invocation_ref=invocation.id, status="succeeded",
            outputs={"text": text, "content_hash": "a" * 64}, actual_side_effects=(),
            usage_refs=(f"usage:{invocation.id}",), artifact_refs=(),
            metadata={"route_decision_ref": route["id"]},
        )
        return invocation, result


class AgendaCoordinatorTests(unittest.TestCase):
    def test_missing_provider_tokens_use_authority_bound_route_estimate(self):
        class RecordingClient:
            def __init__(self):
                self.usage = None
                self.rates = []
                self.cost = None

            def record_usage(self, invocation_ref, **params):
                self.usage = {"invocation_ref": invocation_ref, **params}
                return {"id": params["entry_id"]}

            def create_price_rate_version(self, price_rate_ref, **params):
                self.rates.append({"price_rate_ref": price_rate_ref, **params})
                return {"id": params["version_id"]}

            def record_cost(self, usage_entry_ref, **params):
                self.cost = {"usage_entry_ref": usage_entry_ref, **params}
                return {"id": params["cost_entry_id"]}

        completed = "2026-08-16T02:49:06.118094+00:00"
        invocation = ModelInvocation(
            schema_version="0.1",
            id="invocation:missing-provider-usage",
            created_at=completed,
            work_order_ref="work:agenda-missing-provider-usage",
            profile_ref="model-profile-version:broker-deepseek-v4-flash:2",
            granularity=InvocationGranularity.TASK,
            capability="extract",
            provider="deepseek",
            model="deepseek-v4-flash",
            model_family="deepseek-v4",
            input_refs=("perception:test", "mandate:test"),
            output_refs=(),
            started_at="2026-08-16T02:48:59.465533+00:00",
            completed_at=completed,
            usage={
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "metering_source": "provider_reported",
                "measurement_status": "unavailable",
                "authority_status": "uncommitted",
                "raw_provider_telemetry": {"cost": {"available": False}},
            },
            side_effects=(),
            runtime_ref="adapter:openclaw-model-broker:0.1",
            actor_ref="broker:test",
            parent_ref="route-decision:test",
            environment_hash="env:test",
        )
        profile = {
            "profile_version_ref": invocation.profile_ref,
            "provider": invocation.provider,
            "model": invocation.model,
            "created_at": "2026-08-14T10:34:12.184403+00:00",
            "cost": {
                "input_per_million_usd": 0.22,
                "output_per_million_usd": 0.66,
            },
        }
        route = {
            "id": invocation.parent_ref,
            "selected_profile_version_ref": invocation.profile_ref,
            "candidate_snapshot": [
                {
                    "profile_version_ref": invocation.profile_ref,
                    "eligible": True,
                    "estimated_cost_usd": "0.001759",
                }
            ],
        }
        client = RecordingClient()

        AgendaCoordinator._record_usage_and_cost(
            client, invocation, profile, "agenda-cycle:test", route
        )

        self.assertEqual(client.usage["metering_source"], "launcher_measured")
        self.assertEqual(client.usage["measurement_status"], "partial")
        self.assertEqual(client.usage["requests"], 1)
        self.assertEqual(
            [rate["charge_type"] for rate in client.rates],
            ["input_tokens", "output_tokens", "request"],
        )
        request_rate = client.rates[-1]
        self.assertEqual(request_rate["unit_quantity"], 1)
        self.assertEqual(request_rate["unit_price_micros"], 1759)
        self.assertEqual(request_rate["source_ref"], route["id"])
        self.assertEqual(client.cost["amount_micros"], 1759)
        self.assertEqual(client.cost["cost_status"], "estimated")
        self.assertEqual(
            client.cost["calculation_ref"],
            "calculator:agenda-route-estimate:0.1",
        )
        self.assertEqual(client.cost["price_rate_refs"], [request_rate["version_id"]])

    def test_refreshed_profile_chains_price_rate_versions(self):
        class RecordingClient:
            def __init__(self):
                self.rates = []

            def record_usage(self, _invocation_ref, **params):
                return {"id": params["entry_id"]}

            def create_price_rate_version(self, price_rate_ref, **params):
                self.rates.append({"price_rate_ref": price_rate_ref, **params})
                return {"id": params["version_id"]}

            def record_cost(self, _usage_entry_ref, **params):
                return {"id": params["cost_entry_id"]}

        invocation = ModelInvocation(
            schema_version="0.1",
            id="invocation:refreshed-profile",
            created_at="2026-08-20T08:00:00+00:00",
            work_order_ref="work:agenda-refreshed-profile",
            profile_ref="model-profile-version:broker-deepseek-v4-flash:2",
            granularity=InvocationGranularity.TASK,
            capability="extract",
            provider="deepseek",
            model="deepseek-v4-flash",
            model_family="deepseek-v4",
            input_refs=(),
            output_refs=(),
            started_at="2026-08-20T07:59:59+00:00",
            completed_at="2026-08-20T08:00:00+00:00",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "metering_source": "provider_reported",
                "measurement_status": "partial",
                "authority_status": "uncommitted",
                "raw_provider_telemetry": {},
            },
            side_effects=(),
            runtime_ref="adapter:openclaw-model-broker:0.1",
            actor_ref="broker:test",
            parent_ref="route-decision:refreshed-profile",
            environment_hash="env:test",
        )
        profile = {
            "profile_version_ref": invocation.profile_ref,
            "prior_version_ref": "model-profile-version:broker-deepseek-v4-flash:1",
            "provider": invocation.provider,
            "model": invocation.model,
            "created_at": "2026-08-14T10:34:12.184403+00:00",
            "cost": {
                "input_per_million_usd": 0.22,
                "output_per_million_usd": 0.66,
            },
        }
        client = RecordingClient()

        AgendaCoordinator._record_usage_and_cost(
            client,
            invocation,
            profile,
            "agenda-cycle:refreshed-profile",
            {"id": invocation.parent_ref},
        )

        self.assertEqual(len(client.rates), 2)
        for rate in client.rates:
            expected = "price-rate-version:" + content_hash(
                {
                    "profile": profile["prior_version_ref"],
                    "charge": rate["charge_type"],
                }
            )[:32]
            self.assertEqual(rate["prior_version_ref"], expected)

    def test_cost_rejects_route_rebinding(self):
        invocation = type(
            "Invocation",
            (),
            {
                "id": "invocation:route-rebinding",
                "parent_ref": "route-decision:expected",
                "work_order_ref": "work:route-rebinding",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )()
        with self.assertRaisesRegex(
            CoordinatorError, "route decision does not match model invocation"
        ):
            AgendaCoordinator._record_usage_and_cost(
                object(),
                invocation,
                {},
                "agenda-cycle:route-rebinding",
                {"id": "route-decision:other"},
            )

    def legacy(self, path: Path):
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE companies(slug TEXT PRIMARY KEY,name TEXT,ticker TEXT,market TEXT,coverage_tier TEXT,coverage_status TEXT,archetype TEXT,investment_view TEXT,updated_at TEXT);
        CREATE TABLE events(id INTEGER,event_key TEXT,company_slug TEXT,event_type TEXT,occurred_at TEXT,title TEXT,summary TEXT,materiality TEXT,status TEXT,source_url TEXT,updated_at TEXT);
        CREATE TABLE evidence(id INTEGER,evidence_key TEXT,company_slug TEXT,claim TEXT,stance TEXT,source TEXT,source_url TEXT,as_of TEXT,confidence TEXT,valid_until TEXT,created_at TEXT);
        CREATE TABLE filings(id INTEGER,company_slug TEXT,form TEXT,filing_date TEXT,report_date TEXT,accession_no TEXT,created_at TEXT);
        INSERT INTO companies VALUES('wanhua','万华化学','600309.SS','CN','A','active','chemical','under review','2026-08-14T00:00:00+00:00');
        INSERT INTO events VALUES(1,'event-1','wanhua','filing','2026-08-14T00:00:00+00:00','New filing','summary','high','new','https://example.com','2026-08-14T00:00:00+00:00');
        INSERT INTO evidence VALUES(1,'evidence-1','wanhua','claim','supports','filing','https://example.com','2026-08-14','high','2026-09-14','2026-08-14T00:00:00+00:00');
        INSERT INTO filings VALUES(1,'wanhua','10-Q','2026-08-14','2026-06-30','0001','2026-08-14T00:00:00+00:00');
        """)
        conn.commit(); conn.close()

    def govern(self, tokens, socket, *, max_input_tokens=8000):
        base = {"token_config": tokens, "socket_path": socket, "actor_ref": "human:owner"}
        policy = {
            "schema_version": "0.1", "enabled": True, "selected_count": 2,
            "max_model_calls_per_cycle": 1, "max_daily_cycles": 1,
            "max_daily_cost_usd": 0.5, "max_monthly_cost_usd": 10.0,
            "max_input_tokens": max_input_tokens, "max_output_tokens": 1000,
            "feature_weights": {"mandate_relevance": 4, "catalyst_urgency": 3, "evidence_staleness": 2, "decision_impact": 4},
            "trial_company_refs": ["wanhua"], "cutover_enabled": False,
            "cutover_acceptance_threshold": None,
        }
        ephemeral_call(**base, operation="create_agenda_policy", params={
            "policy": policy, "effective_from": "2026-08-14T00:00:00+00:00",
            "effective_until": "2026-08-15T00:00:00+00:00", "activate": True,
            "version_id": "agenda-policy-version:phase1", "idempotency_key": "policy:phase1",
        })
        ephemeral_call(**base, operation="create_mandate", params={
            "mandate_ref": "mandate:phase1", "objective": "Find decision-useful unknowns",
            "scope_refs": ["wanhua"], "constraints": {"mode": "shadow"},
            "success_criteria": {"human_feedback_required": True},
            "effective_from": "2026-08-14T00:00:00+00:00",
            "effective_until": "2026-08-15T00:00:00+00:00", "activate": True,
            "version_id": "mandate-version:phase1", "idempotency_key": "mandate:phase1",
        })
        ephemeral_call(**base, operation="set_agenda_pause", params={
            "paused": False, "reason": "phase1 test", "version_id": "agenda-control-version:phase1",
            "idempotency_key": "resume:phase1",
        })

    def test_real_authority_chain_is_idempotent_and_pause_blocks_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"; scheduler = root / "scheduler.sqlite"; router = root / "router.sqlite"
            socket = root / "run" / "writer.sock"; tokens = root / "tokens.json"
            legacy = root / "legacy.sqlite"; self.legacy(legacy)
            write_token_config(tokens, [Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True)])
            # Keep the fixture independent of the wall-clock date on which
            # the suite is executed. The coordinator still passes its own
            # frozen cycle time below.
            install_openclaw_catalog(
                router,
                checked_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
                availability_ttl=timedelta(days=365),
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            process = subprocess.Popen(
                [sys.executable, "-m", "dalton_core.writer_server", "--db", str(core), "--socket", str(socket), "--token-config", str(tokens)],
                cwd=str(Path(__file__).parents[1]), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not socket.exists():
                    time.sleep(0.02)
                config = AgendaCoordinatorConfig(
                    scheduler_db=scheduler, model_router_db=router, writer_socket=socket,
                    core_token_config=tokens, broker_socket=root / "broker.sock",
                    broker_auth_key=root / "broker.key", perception_source_db=legacy,
                    perception_snapshot_path=root / "perception.json", company_ref="wanhua",
                    routing_policy_ref="model-routing-policy-version:dalton-openclaw:1",
                    credential_slot_refs=("credential-slot:openclaw:deepseek", "credential-slot:openclaw:openai", "credential-slot:openclaw:claude-cli"),
                    broker_client_id="client:dalton-core", expected_agent_id="chem",
                    timeout_seconds=30,
                )
                coordinator = AgendaCoordinator(config)
                FakeAdapter.calls = 0
                with patch.object(AgendaCoordinator, "_adapter", return_value=FakeAdapter()):
                    self.assertEqual(coordinator.run_once(now=datetime(2026, 8, 14, 10, tzinfo=timezone.utc))["status"], "paused")
                    self.govern(tokens, socket)
                    first = coordinator.run_once(now=datetime(2026, 8, 14, 10, tzinfo=timezone.utc))
                    second = coordinator.run_once(now=datetime(2026, 8, 14, 11, tzinfo=timezone.utc))
                self.assertEqual(first["status"], "decided")
                self.assertEqual(second["status"], "decided")
                self.assertEqual(FakeAdapter.calls, 1)
                conn = sqlite3.connect(core)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM observability_usage_entries").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM observability_cost_entries").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agenda_decisions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agenda_outbox_messages").fetchone()[0], 1)
                # The perception snapshot the cycle bound to is Core-resident.
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM perception_snapshot_versions").fetchone()[0], 1
                )
                cycle_snapshot = conn.execute(
                    "SELECT perception_snapshot_ref,perception_snapshot_hash FROM agenda_cycles"
                ).fetchone()
                registered = conn.execute(
                    "SELECT snapshot_id,content_hash FROM perception_snapshot_versions"
                ).fetchone()
                self.assertEqual(tuple(cycle_snapshot), tuple(registered))
                conn.close()
                # The prompt is the fixed wrapper plus the materializer render.
                # The retired manual splice must not be back.
                question = FakeAdapter.last_question
                self.assertIsNotNone(question)
                self.assertNotIn("MANDATE=", question)
                self.assertNotIn("PERCEPTION=", question)
                self.assertIn('"_dalton_context":"materialization"', question)
                self.assertIn('"kind":"mandate"', question)
                self.assertIn('"kind":"perception"', question)
                self.assertIn("OUTPUT_CONTRACT=", question)
            finally:
                process.terminate(); process.wait(timeout=3)

    def test_input_token_budget_fails_closed_without_truncating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"; scheduler = root / "scheduler.sqlite"; router = root / "router.sqlite"
            socket = root / "run" / "writer.sock"; tokens = root / "tokens.json"
            legacy = root / "legacy.sqlite"; self.legacy(legacy)
            write_token_config(tokens, [Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True)])
            install_openclaw_catalog(
                router,
                checked_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
                availability_ttl=timedelta(days=365),
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            process = subprocess.Popen(
                [sys.executable, "-m", "dalton_core.writer_server", "--db", str(core), "--socket", str(socket), "--token-config", str(tokens)],
                cwd=str(Path(__file__).parents[1]), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not socket.exists():
                    time.sleep(0.02)
                config = AgendaCoordinatorConfig(
                    scheduler_db=scheduler, model_router_db=router, writer_socket=socket,
                    core_token_config=tokens, broker_socket=root / "broker.sock",
                    broker_auth_key=root / "broker.key", perception_source_db=legacy,
                    perception_snapshot_path=root / "perception.json", company_ref="wanhua",
                    routing_policy_ref="model-routing-policy-version:dalton-openclaw:1",
                    credential_slot_refs=("credential-slot:openclaw:deepseek",),
                    broker_client_id="client:dalton-core", expected_agent_id="chem",
                    timeout_seconds=30,
                )
                # 40 tokens cannot hold the mandate and the perception snapshot.
                # The cycle must fail rather than drop one or truncate either.
                self.govern(tokens, socket, max_input_tokens=40)
                coordinator = AgendaCoordinator(config)
                FakeAdapter.calls = 0
                with patch.object(AgendaCoordinator, "_adapter", return_value=FakeAdapter()):
                    with self.assertRaises(Exception):
                        coordinator.run_once(now=datetime(2026, 8, 14, 10, tzinfo=timezone.utc))
                self.assertEqual(FakeAdapter.calls, 0)
                conn = sqlite3.connect(core)
                state, reason = conn.execute(
                    "SELECT state,reason FROM agenda_cycle_events ORDER BY event_seq DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(state, "failed")
                self.assertIn(
                    reason,
                    {"agenda_context_materialization_failed", "prompt_input_budget_exceeded"},
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agenda_candidates").fetchone()[0], 0)
                conn.close()
            finally:
                process.terminate(); process.wait(timeout=3)

    def test_control_plane_failure_closes_lease_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            created = "2026-08-14T10:00:00+00:00"
            work = WorkOrder(
                schema_version="0.1", id="work:agenda-control-failure",
                created_at=created, updated_at=created, question="test",
                requested_capabilities=("extract",), runtime_profile_ref="policy:test",
                budget={"max_input_tokens": 10, "max_output_tokens": 10,
                        "max_total_tokens": 20, "max_cost_usd": 1, "max_seconds": 30},
                idempotency_key="work:agenda-control-failure",
                declared_side_effects=(), status="ready", input_refs=(), metadata={},
            )
            with Scheduler(path) as scheduler:
                scheduler.enqueue(work)
                lease = scheduler.claim("worker:agenda-model", work_order_id=work.id)
                self.assertIsNotNone(lease)
                AgendaCoordinator._terminal_control_failure(
                    scheduler, work, lease, cycle_id="agenda-cycle:test",
                    code="model_adapter_rejected_or_failed",
                )
                self.assertEqual(scheduler.status(work.id)["state"], "failed")
                formal = scheduler.formal_result(work.id)
                self.assertEqual(formal["result_envelope"]["error"]["code"], "model_adapter_rejected_or_failed")

    def test_cjk_heavy_snapshot_is_bounded_to_the_provider_token_budget(self):
        """Replay of the 2026-08-25/26 live failure.

        The Dalton tokenizer counted those prompts at ~2.7k tokens against an
        8,000 policy budget, DeepSeek counted 8.5k-9.3k, and the paid
        completion was discarded as PROVIDER_BUDGET_EXCEEDED.  The coordinator
        must now bound the perception snapshot up front and never hand the
        adapter a prompt whose provider estimate exceeds the policy budget.
        """
        from tests.test_provider_token_estimate import seed_legacy
        from dalton_core.provider_token_estimate import estimate_provider_input_tokens
        from dalton_core.research_context import count_dalton_search_tokens

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"; scheduler = root / "scheduler.sqlite"; router = root / "router.sqlite"
            socket = root / "run" / "writer.sock"; tokens = root / "tokens.json"
            legacy = root / "legacy.sqlite"
            seed_legacy(legacy, evidence_rows=40, event_rows=10)
            write_token_config(tokens, [Principal("core", "core-token", CORE_OPERATIONS, unrestricted=True)])
            install_openclaw_catalog(
                router,
                checked_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
                availability_ttl=timedelta(days=365),
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            process = subprocess.Popen(
                [sys.executable, "-m", "dalton_core.writer_server", "--db", str(core), "--socket", str(socket), "--token-config", str(tokens)],
                cwd=str(Path(__file__).parents[1]), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not socket.exists():
                    time.sleep(0.02)
                config = AgendaCoordinatorConfig(
                    scheduler_db=scheduler, model_router_db=router, writer_socket=socket,
                    core_token_config=tokens, broker_socket=root / "broker.sock",
                    broker_auth_key=root / "broker.key", perception_source_db=legacy,
                    perception_snapshot_path=root / "perception.json", company_ref="wanhua",
                    routing_policy_ref="model-routing-policy-version:dalton-openclaw:1",
                    credential_slot_refs=("credential-slot:openclaw:deepseek",),
                    broker_client_id="client:dalton-core", expected_agent_id="chem",
                    timeout_seconds=30,
                )
                # The unbounded snapshot is far over 8,000 provider tokens but
                # under 8,000 Dalton tokens: exactly the live shape.
                from dalton_core.perception import LegacyCoveragePerceptionAdapter
                from dalton_core.store import canonical_json
                unbounded = LegacyCoveragePerceptionAdapter(legacy).build("wanhua")
                self.assertGreater(estimate_provider_input_tokens(canonical_json(unbounded)), 8000)
                self.assertLess(count_dalton_search_tokens(canonical_json(unbounded)), 8000)
                self.govern(tokens, socket, max_input_tokens=8000)
                coordinator = AgendaCoordinator(config)
                FakeAdapter.calls = 0
                with patch.object(AgendaCoordinator, "_adapter", return_value=FakeAdapter()):
                    first = coordinator.run_once(now=datetime(2026, 8, 14, 10, tzinfo=timezone.utc))
                self.assertEqual(first["status"], "decided")
                self.assertEqual(FakeAdapter.calls, 1)
                question = FakeAdapter.last_question
                self.assertLessEqual(estimate_provider_input_tokens(question), 8000)
                conn = sqlite3.connect(core)
                conn.row_factory = sqlite3.Row
                registered = json.loads(conn.execute(
                    "SELECT record_json FROM perception_snapshot_versions"
                ).fetchone()["record_json"])
                bounding = registered["bounding"]
                self.assertEqual(bounding["fetched"]["evidence"], 40)
                self.assertGreater(bounding["dropped"]["evidence"], 0)
                self.assertEqual(bounding["dropped"]["catalysts"], 0)
                self.assertEqual(len(registered["evidence"]), 40 - bounding["dropped"]["evidence"])
                # The registered snapshot is the one the prompt quotes.
                self.assertIn(registered["evidence"][0]["evidence_key"], question)
                dropped_key = unbounded["evidence"][-1]["evidence_key"]
                self.assertNotIn(dropped_key, question)
                conn.close()
                with Scheduler(scheduler) as sched:
                    work = json.loads(sched.connection.execute(
                        "SELECT work_order_json FROM scheduler_work_orders"
                    ).fetchone()[0])
                self.assertEqual(
                    work["metadata"]["provider_input_estimator_ref"],
                    "estimator:provider-input-chars-per-token:2.2",
                )
                self.assertLessEqual(work["metadata"]["estimated_provider_input_tokens"], 8000)
                self.assertGreater(
                    work["metadata"]["estimated_provider_input_tokens"],
                    work["metadata"]["prompt_tokens"],
                )
            finally:
                process.terminate(); process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
