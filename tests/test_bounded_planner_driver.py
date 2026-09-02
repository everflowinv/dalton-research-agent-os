"""Tier 1 probe executor and bounded planner driver tests."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.bounded_planner_driver import (
    BoundedPlannerDriver,
    BoundedPlannerDriverConfig,
    BoundedPlannerDriverError,
)
from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.bounded_probe_executor import (
    BoundedProbeExecutionError,
    execute_probe_work_order,
)
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.store import DaltonStore, content_hash
from dalton_core.writer_client import WriterClient
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)
from dalton_core.sec_lane_launcher import LaneLaunchConflict


OWNER = "human:coverage-owner"
INDUSTRY = "industry:us-it-services"
ACN = "company:sec-cik:0001467373"
NOW = datetime(2026, 8, 27, 16, 0, 0, tzinfo=timezone.utc)
FIXED_NOW = "2026-08-27T16:00:00+00:00"
CORE_TOKEN = "core-driver-test"
GOVERNANCE_TOKEN = "governance-driver-test"


def company_facts_body(accession: str = "0001467373-26-000031") -> bytes:
    payload = {
        "cik": "1467373",
        "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"form": "10-Q", "filed": "2026-06-20", "accn": accession},
            {"form": "10-K", "filed": "2025-10-30", "accn": "0001467373-25-000010"},
        ]}}}},
    }
    return json.dumps(payload).encode("utf-8")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.bytes_written = len(body)
        self.headers: list[tuple[str, str]] = []


class FakeTransport:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[dict] = []

    def request(self, request, sink, **kwargs):
        self.requests.append({"url": request.url, "kwargs": kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        sink.write(self.response.body)
        return self.response


def probe_work_order(parameters: dict | None = None) -> dict:
    return {
        "schema_version": "0.1",
        "id": "work:bounded-planner:probe:test",
        "metadata": {
            "permission_scope": "public_sec_read",
            "operation": "get_company_facts",
            "parameters": parameters or {
                "source_ref": "source:sec-edgar",
                "locator": "company-facts/CIK0001467373",
                "query_terms": ["Revenues", "10-Q", "ACN"],
            },
        },
    }


class BoundedProbeExecutorTests(unittest.TestCase):
    def test_successful_fetch_selects_latest_accession(self) -> None:
        transport = FakeTransport(FakeResponse(200, company_facts_body()))
        envelope = execute_probe_work_order(
            probe_work_order(), transport=transport, user_agent="Dalton Test",
            max_response_bytes=1_000_000, timeout_seconds=10.0, clock=lambda: NOW,
        )
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(
            [{"source_location": "sec:accession:000146737326000031"}],
            envelope["outputs"]["matches"],
        )
        self.assertEqual(["read:public-http"], envelope["actual_side_effects"])
        self.assertIn("data.sec.gov", transport.requests[0]["url"])
        self.assertEqual(envelope["metadata"]["form"], "10-Q")

    def test_annual_probe_selects_10_k_and_records_exact_form(self) -> None:
        transport = FakeTransport(FakeResponse(200, company_facts_body()))
        work = probe_work_order({
            "source_ref": "source:sec-edgar",
            "locator": "company-facts/CIK0001467373",
            "query_terms": ["Revenues", "10-K", "ACN"],
        })
        envelope = execute_probe_work_order(
            work, transport=transport, user_agent="Dalton Test",
            max_response_bytes=1_000_000, timeout_seconds=10.0, clock=lambda: NOW,
        )
        self.assertEqual(envelope["status"], "succeeded")
        self.assertEqual(envelope["metadata"]["form"], "10-K")
        self.assertEqual(
            envelope["outputs"]["matches"],
            [{"source_location": "sec:accession:000146737325000010"}],
        )

    def test_no_recent_ten_q_is_not_found_in_scope(self) -> None:
        stale = json.dumps({
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
                {"form": "10-Q", "filed": "2020-06-20", "accn": "0001467373-20-000001"},
            ]}}}},
        }).encode()
        envelope = execute_probe_work_order(
            probe_work_order(), transport=FakeTransport(FakeResponse(200, stale)),
            user_agent="Dalton Test", max_response_bytes=1_000_000,
            timeout_seconds=10.0, clock=lambda: NOW,
        )
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual([], envelope["outputs"]["matches"])

    def test_http_and_transport_failures_fail_closed(self) -> None:
        for response in (FakeResponse(429, b""), FakeResponse(403, b""), OSError("down")):
            envelope = execute_probe_work_order(
                probe_work_order(), transport=FakeTransport(response),
                user_agent="Dalton Test", max_response_bytes=1_000_000,
                timeout_seconds=10.0, clock=lambda: NOW,
            )
            self.assertEqual("failed", envelope["status"])
            self.assertEqual("SOURCE_UNAVAILABLE", envelope["error"]["code"])

    def test_concept_candidates_fall_back_in_order(self) -> None:
        payload = json.dumps({
            "facts": {"us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"form": "10-Q", "filed": "2026-05-08",
                         "accn": "0001352010-26-000045"},
                    ]},
                },
            }},
        }).encode()
        work = probe_work_order({
            "source_ref": "source:sec-edgar",
            "locator": "company-facts/CIK0001352010",
            "query_terms": [
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "10-Q", "EPAM",
            ],
        })
        envelope = execute_probe_work_order(
            work, transport=FakeTransport(FakeResponse(200, payload)),
            user_agent="Dalton Test", max_response_bytes=1_000_000,
            timeout_seconds=10.0, clock=lambda: NOW,
        )
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(
            [{"source_location": "sec:accession:000135201026000045"}],
            envelope["outputs"]["matches"],
        )

    def test_non_tier1_work_orders_are_rejected(self) -> None:
        for metadata in (
            {"permission_scope": "private", "operation": "get_company_facts"},
            {"permission_scope": "public_sec_read", "operation": "delete_everything"},
        ):
            work = {"id": "work:x", "metadata": metadata}
            with self.assertRaises(BoundedProbeExecutionError):
                execute_probe_work_order(
                    work, transport=FakeTransport(FakeResponse(200, b"{}")),
                    user_agent="u", max_response_bytes=10, timeout_seconds=1.0,
                )


class MissionObservationDispatchTests(unittest.TestCase):
    def _server(self, launcher):
        server = WriterServer.__new__(WriterServer)
        server._bounded_control = type("Control", (), {
            "record_observation_followup": lambda _self, _round, **_kw: {
                "status": "recorded", "outcome_ref": "outcome:1",
                "question_ref": "question:1", "company_ref": ACN,
                "source_location": "sec:accession:000146737326000031",
                "form": "10-Q", "filed_from": "2026-01-01", "filed_to": "2026-08-27",
            }
        })()
        authorization = {
            "mission_version_ref": "coverage-mission-version:us-it-services:1",
            "mission_version_hash": "b" * 64,
            "mission_ref": "coverage-mission:us-it-services", "company_ref": ACN,
            "ticker": "ACN", "actor_ref": "automation:coverage-mission",
            "paid_calls_reserved": 0, "cost_usd_reserved": 0.0,
            "budget": {"max_daily_paid_calls": 40, "max_daily_cost_usd": 5.0,
                       "max_alphaengine_calls_24h": 30},
        }
        class Mission:
            def __init__(self):
                self.pending = []

            def sec_lane_authorization_for_company(self, _company):
                return authorization

            def authorize_sec_lane(self, **_request):
                return authorization

            def queue_sec_dispatch(self, **request):
                row = {
                    "dispatch_id": "mission-sec-dispatch:" + "2" * 32,
                    "mission_version_ref": authorization["mission_version_ref"],
                    "mission_version_hash": authorization["mission_version_hash"],
                    "company_ref": authorization["company_ref"],
                    "ticker": authorization["ticker"], "actor_ref": authorization["actor_ref"],
                    "authorization": authorization, "status": "pending", "ticket_ref": None,
                    **{key: request[key] for key in (
                        "form", "filed_from", "filed_to", "expected_accession"
                    )},
                }
                self.pending.append(row)
                return {**row, "status_marker": "fresh"}

            def pending_sec_dispatches(self, *, limit=1):
                return self.pending[:limit]

            def mark_sec_dispatch_launched(self, dispatch_id, ticket_ref):
                self.pending = [row for row in self.pending if row["dispatch_id"] != dispatch_id]
                return {"status": "launched", "ticket_ref": ticket_ref}

            def mark_sec_dispatch_rejected(self, dispatch_id, reason):
                self.pending = [row for row in self.pending if row["dispatch_id"] != dispatch_id]
                return {"status": "rejected", "failure_reason": reason}

        server._coverage_mission = Mission()
        server._sec_lane_launcher = launcher
        return server

    def test_writer_dispatches_exact_observation_under_mission_grant(self) -> None:
        class Launcher:
            def __init__(self):
                self.request = None

            def start(self, **request):
                self.request = request
                return {"id": "sec-lane-run:" + "1" * 24}

        launcher = Launcher()
        result = self._server(launcher)._op_bounded_planner_record_observation({
            "round_ref": "round:1", "mandate_version_ref": "mandate:1",
        })
        self.assertEqual(result["lane_status"], "launched")
        self.assertEqual(result["expected_accession"], "0001467373-26-000031")
        self.assertEqual(launcher.request["issuers"], ["ACN"])
        self.assertEqual(launcher.request["actor_ref"], "automation:coverage-mission")
        self.assertEqual(launcher.request["form"], "10-Q")
        self.assertEqual(launcher.request["mission_context"]["paid_calls_reserved"], 0)

    def test_busy_lane_is_reported_as_deferred_not_as_launched(self) -> None:
        class BusyLauncher:
            def start(self, **_request):
                raise LaneLaunchConflict("busy")

        result = self._server(BusyLauncher())._op_bounded_planner_record_observation({
            "round_ref": "round:1", "mandate_version_ref": "mandate:1",
        })
        self.assertEqual(result["lane_status"], "deferred")
        self.assertNotIn("lane_ticket_ref", result)


class BoundedPlannerDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.socket = str(self.root / "writer.sock")
        self.scheduler_path = self.root / "scheduler.sqlite"
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN,
                HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER,
            ),
        }
        self.server = WriterServer(
            self.root / "core.sqlite", self.socket, principals,
            scheduler_path=self.scheduler_path,
        )
        self.server.start()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def _close() -> None:
            self.server.stop()
            thread.join(timeout=10)

        self.addCleanup(_close)

        self.core = WriterClient(self.socket, CORE_TOKEN, timeout=60)
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN, timeout=60)
        mandate = self.governance.call("create_mandate", {
            "mandate_ref": "mandate:driver", "actor_ref": OWNER,
            "objective": "Driver test mandate.",
            "scope_refs": [INDUSTRY, ACN, "company:sec-cik:0001058290"],
            "constraints": {}, "success_criteria": {},
            "effective_from": "2026-08-23T00:00:00+00:00", "effective_until": None,
        })
        question = self.governance.call("record_backlog_question", {
            "mandate_version_ref": mandate["id"], "company_ref": INDUSTRY,
            "question": "Has demand bottomed?",
            "answer_criteria": "Source-level coverage of the lane.",
            "source_refs": ["source:sec-edgar"], "actor_ref": OWNER,
            "idempotency_key": "driver:question:1",
        })
        template = self.governance.call("publish_probe_template", {
            "template_ref": "probe-template:driver:v1",
            "capability_ref": "capability:sec-read-only",
            "operation": "get_company_facts",
            "runtime_profile_ref": "runtime:sec-read-only:0.1",
            "parameter_contract": {
                "allowed_fields": ["source_ref", "locator", "query_terms"],
                "required_fields": ["source_ref", "locator", "query_terms"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            "output_contract_ref": "schema:bounded-planner-probe-output:0.1",
            "verifier_ref": "verifier:source-level-coverage:0.1",
            "permission_scope": "public_sec_read",
            "declared_side_effects": ["read:public-http"],
            "cost": {"cost_units": 1, "max_attempts": 2, "max_seconds": 120},
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        self.loop = self.governance.call("create_bounded_planner_loop", {
            "loop_ref": "bounded-loop:driver:v1",
            "question_version_ref": question["question_version_ref"],
            "template_bindings": [
                {
                    "coverage_item_ref": "coverage:revenue-growth:acn",
                    "template_version_ref": template["id"],
                    "parameters": {
                        "source_ref": "source:sec-edgar",
                        "locator": "company-facts/CIK0001467373",
                        "query_terms": ["Revenues", "10-Q", "ACN"],
                    },
                },
                {
                    "coverage_item_ref": "coverage:revenue-growth:other",
                    "template_version_ref": template["id"],
                    "parameters": {
                        "source_ref": "source:sec-edgar",
                        "locator": "company-facts/CIK0001058290",
                        "query_terms": ["Revenues", "10-Q", "CTSH"],
                    },
                },
            ],
            "required_coverage_items": [
                "coverage:revenue-growth:acn", "coverage:revenue-growth:other",
            ],
            "budget": {"max_rounds": 4, "max_cost_units": 4, "max_seconds": 600},
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        self.mandate_id = mandate["id"]
        self.template_id = template["id"]
        self.config = BoundedPlannerDriverConfig(
            writer_socket=Path(self.socket),
            token_config=self.root / "tokens.json",
            scheduler_db=self.scheduler_path,
            user_agent="Dalton Test",
            max_response_bytes=1_000_000,
            timeout_seconds=10.0,
            max_probes_per_tick=1,
            filed_window_days=400,
            observation_mandate_version_ref=self.mandate_id,
            doctrine_pack_version_ref=None,
            doctrine_pack_version_hash=None,
            planner_routing_policy_ref=None,
            planner_credential_slot_refs=None,
            planner_model_router_db=None,
            planner_broker_socket=None,
            planner_broker_auth_key=None,
            planner_broker_client_id="client:dalton-core",
            planner_expected_agent_id="chem",
            planner_max_cost_usd=0.5,
        )

    def _driver(self, transport) -> BoundedPlannerDriver:
        return BoundedPlannerDriver(
            self.config, client=self.core, transport=transport, clock=lambda: NOW,
        )

    def test_driver_runs_loop_to_terminal_one_probe_per_tick(self) -> None:
        from dalton_core.writer_server import write_token_config
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        transport = FakeTransport(FakeResponse(200, company_facts_body()))
        driver = self._driver(transport)

        first = driver.run_once()
        self.assertEqual("completed", first["status"])
        self.assertEqual(1, first["probes_executed"])
        self.assertEqual("observed", first["executed"][0]["outcome_kind"])
        self.assertEqual("recorded", first["executed"][0]["observation_status"])
        self.assertEqual([], first["skipped"])

        second = driver.run_once()
        self.assertEqual(1, second["probes_executed"])
        self.assertEqual("observed", second["executed"][0]["outcome_kind"])

        third = driver.run_once()
        self.assertEqual("terminal", third["executed"][0]["kind"])
        self.assertEqual(
            "evidence_observed_for_review",
            third["executed"][0]["terminal_state"],
        )
        fourth = driver.run_once()
        self.assertEqual("idle", fourth["status"])
        self.assertEqual([], fourth["executed"])

        # New observations open idempotent backlog questions.
        backlog_rows = self.core.call("bounded_planner_active_loops", {})
        self.assertEqual([], backlog_rows["loops"])
        store = DaltonStore(str(self.root / "core.sqlite"))
        authority = BoundedPlannerAuthority(store)
        self.assertEqual(2, len(authority.outcomes(self.loop["id"])))
        terminal = authority.terminal(self.loop["id"])
        self.assertEqual("evidence_observed_for_review", terminal["terminal_state"])
        from dalton_core.research_question_backlog import ResearchQuestionBacklog
        backlog = ResearchQuestionBacklog(store)
        questions = backlog.questions()
        self.assertEqual(3, len(questions))  # standing question + two observations
        observed_accessions = {
            question["head"]["company_ref"]: question["head"]["question"]
            for question in questions
        }
        self.assertIn(ACN, observed_accessions)
        self.assertIn("000146737326000031", observed_accessions[ACN])

        # Loop v2 with the same coverage items replays the identical source:
        # observations are unchanged, no new questions.
        loop_v2 = self.governance.call("create_bounded_planner_loop", {
            "loop_ref": "bounded-loop:driver:v1",
            "question_version_ref": self.loop["question_version_ref"],
            "template_bindings": [
                {
                    "coverage_item_ref": item,
                    "template_version_ref": self.template_id,
                    "parameters": params,
                }
                for item, params in (
                    ("coverage:revenue-growth:acn", {
                        "source_ref": "source:sec-edgar",
                        "locator": "company-facts/CIK0001467373",
                        "query_terms": ["Revenues", "10-Q", "ACN"],
                    }),
                    ("coverage:revenue-growth:other", {
                        "source_ref": "source:sec-edgar",
                        "locator": "company-facts/CIK0001058290",
                        "query_terms": ["Revenues", "10-Q", "CTSH"],
                    }),
                )
            ],
            "required_coverage_items": [
                "coverage:revenue-growth:acn", "coverage:revenue-growth:other",
            ],
            "budget": {"max_rounds": 4, "max_cost_units": 4, "max_seconds": 600},
            "actor_ref": OWNER, "prior_version_ref": self.loop["id"],
        })
        for _ in range(3):
            driver.run_once()
        self.assertIsNotNone(authority.terminal(loop_v2["id"]))
        self.assertEqual(3, len(backlog.questions()))
        store.close()

    def test_doctrine_context_mode_binds_proposals_to_context(self) -> None:
        from dalton_core.writer_server import write_token_config
        pack = self.governance.call("publish_doctrine_pack", {
            "doctrine_pack_ref": "doctrine-pack:driver",
            "title": "Driver Doctrine",
            "default_lens_ref": "lens:demand",
            "lenses": [{
                "lens_ref": "lens:demand",
                "label": "Demand",
                "objective": "Track demand.",
                "priority_topics": ["bookings"],
                "evidence_standard": {
                    "preferred_source_classes": ["source:sec-edgar"],
                    "minimum_independent_sources": 1,
                    "negative_claim_rule": (
                        "candidate_only_until_separate_claim_admission"
                    ),
                },
            }],
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        config = BoundedPlannerDriverConfig(
            writer_socket=Path(self.socket),
            token_config=self.root / "tokens.json",
            scheduler_db=self.scheduler_path,
            user_agent="Dalton Test",
            max_response_bytes=1_000_000,
            timeout_seconds=10.0,
            max_probes_per_tick=1,
            filed_window_days=400,
            observation_mandate_version_ref=self.mandate_id,
            doctrine_pack_version_ref=pack["id"],
            doctrine_pack_version_hash=pack["content_hash"],
            planner_routing_policy_ref=None,
            planner_credential_slot_refs=None,
            planner_model_router_db=None,
            planner_broker_socket=None,
            planner_broker_auth_key=None,
            planner_broker_client_id="client:dalton-core",
            planner_expected_agent_id="chem",
            planner_max_cost_usd=0.5,
        )
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        driver = BoundedPlannerDriver(
            config, client=self.core,
            transport=FakeTransport(FakeResponse(200, company_facts_body())),
            clock=lambda: NOW,
        )
        executed = []
        while len(executed) < 3:
            result = driver.run_once()
            executed.extend(result["executed"])
            if not result["executed"]:
                break
        kinds = [entry["kind"] for entry in executed]
        self.assertIn("terminal", kinds)
        probes = [entry for entry in executed if entry["kind"] == "probe"]
        self.assertGreaterEqual(len(probes), 1)
        # The loop's proposals carry the exact planner context binding.
        from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
        from dalton_core.store import DaltonStore
        store = DaltonStore(str(self.root / "core.sqlite"))
        authority = BoundedPlannerAuthority(store)
        proposals = authority.connection.execute(
            "SELECT record_json FROM bounded_planner_proposal_versions "
            "WHERE loop_version_ref=?", (self.loop["id"],)
        ).fetchall()
        with_context = [
            json.loads(row["record_json"]) for row in proposals
            if json.loads(row["record_json"]).get("planner_context_pack_ref")
        ]
        store.close()
        self.assertGreaterEqual(len(with_context), 1)

    def test_planner_model_failure_falls_back_to_deterministic(self) -> None:
        from dalton_core.writer_server import write_token_config
        from dalton_core.agenda import AgendaStore
        pack = self.governance.call("publish_doctrine_pack", {
            "doctrine_pack_ref": "doctrine-pack:driver-llm",
            "title": "Driver Doctrine LLM",
            "default_lens_ref": "lens:demand",
            "lenses": [{
                "lens_ref": "lens:demand",
                "label": "Demand",
                "objective": "Track demand.",
                "priority_topics": ["bookings"],
                "evidence_standard": {
                    "preferred_source_classes": ["source:sec-edgar"],
                    "minimum_independent_sources": 1,
                    "negative_claim_rule": (
                        "candidate_only_until_separate_claim_admission"
                    ),
                },
            }],
            "actor_ref": OWNER, "prior_version_ref": None,
        })
        config = BoundedPlannerDriverConfig(
            writer_socket=Path(self.socket),
            token_config=self.root / "tokens.json",
            scheduler_db=self.scheduler_path,
            user_agent="Dalton Test",
            max_response_bytes=1_000_000,
            timeout_seconds=10.0,
            max_probes_per_tick=1,
            filed_window_days=400,
            observation_mandate_version_ref=self.mandate_id,
            doctrine_pack_version_ref=pack["id"],
            doctrine_pack_version_hash=pack["content_hash"],
            # The writer in this harness has no planner model config, so the
            # execute RPC fails and the driver must fall back deterministically.
            planner_routing_policy_ref="model-routing-policy-version:planner-test:1",
            planner_credential_slot_refs=("credential-slot:openclaw:deepseek",),
            planner_model_router_db=self.root / "router.sqlite",
            planner_broker_socket=self.root / "broker.sock",
            planner_broker_auth_key=self.root / "broker.key",
            planner_broker_client_id="client:dalton-core",
            planner_expected_agent_id="chem",
            planner_max_cost_usd=0.5,
        )
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        driver = BoundedPlannerDriver(
            config, client=self.core,
            transport=FakeTransport(FakeResponse(200, company_facts_body())),
            clock=lambda: NOW,
        )
        first = driver.run_once()
        self.assertEqual("completed", first["status"])
        self.assertEqual(1, first["probes_executed"])
        self.assertEqual("observed", first["executed"][0]["outcome_kind"])
        # The deterministic doctrine planner produced the admitted proposal.
        from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
        from dalton_core.store import DaltonStore
        store = DaltonStore(str(self.root / "core.sqlite"))
        authority = BoundedPlannerAuthority(store)
        rows = authority.connection.execute(
            "SELECT record_json FROM bounded_planner_proposal_versions "
            "WHERE loop_version_ref=?", (self.loop["id"],)
        ).fetchall()
        store.close()
        self.assertGreaterEqual(len(rows), 1)

    def test_config_validates_closed_shape(self) -> None:
        raw = {
            "writer_socket": self.socket,
            "token_config": str(self.root / "tokens.json"),
            "scheduler_db": str(self.scheduler_path),
            "user_agent": "Dalton Test",
            "max_response_bytes": 1000,
            "timeout_seconds": 5.0,
            "max_probes_per_tick": 1,
            "filed_window_days": 400,
            "observation_mandate_version_ref": "mandate-version:test:1",
            "doctrine_pack_version_ref": None,
            "doctrine_pack_version_hash": None,
            "planner_routing_policy_ref": None,
            "planner_credential_slot_refs": None,
            "planner_model_router_db": None,
            "planner_broker_socket": None,
            "planner_broker_auth_key": None,
            "planner_broker_client_id": "client:dalton-core",
                        "planner_expected_agent_id": "chem",
            "planner_max_cost_usd": 0.5,
        }
        parsed = BoundedPlannerDriverConfig.from_mapping(raw)
        self.assertEqual(
            "mandate-version:test:1", parsed.observation_mandate_version_ref
        )
        self.assertEqual(1, parsed.max_probes_per_tick)
        bad = dict(raw)
        bad["extra"] = True
        with self.assertRaises(BoundedPlannerDriverError):
            BoundedPlannerDriverConfig.from_mapping(bad)
        bad = dict(raw, writer_socket="relative/sock")
        with self.assertRaises(BoundedPlannerDriverError):
            BoundedPlannerDriverConfig.from_mapping(bad)


if __name__ == "__main__":
    unittest.main()
