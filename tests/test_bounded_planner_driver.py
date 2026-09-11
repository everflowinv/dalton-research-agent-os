"""Tier 1 probe executor and bounded planner driver tests."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.agenda import AgendaStore
from dalton_core.bounded_planner_driver import (
    BoundedPlannerDriver,
    BoundedPlannerDriverConfig,
    BoundedPlannerDriverError,
)
from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
from dalton_core.budget_pools import POOL_EXHAUSTED_REASON, POOL_EXHAUSTED_STATUS
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
    WriterServerError,
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

    def test_a_probe_the_executor_refuses_becomes_a_failed_round(self) -> None:
        from unittest.mock import patch

        from dalton_core.writer_server import write_token_config
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        driver = self._driver(FakeTransport(FakeResponse(200, company_facts_body())))
        with patch(
            "dalton_core.bounded_planner_driver.execute_probe_work_order",
            side_effect=BoundedProbeExecutionError("probe scope is not executable"),
        ):
            first = driver.run_once()
        # The round is spent and recorded, not left admitted with no outcome.
        self.assertEqual(first["probes_executed"], 1)
        self.assertEqual(first["executed"][0]["outcome_kind"], "source_unavailable")
        self.assertIn("probe scope is not executable", first["executed"][0]["probe_refused"])
        # And the loop is free to move: the next tick proposes again rather
        # than refusing forever because a round has no outcome.
        second = driver.run_once()
        self.assertEqual(second["probes_executed"], 1)
        self.assertEqual(second["executed"][0]["outcome_kind"], "observed")

    def test_a_pending_discovery_child_holds_the_round_and_resumes(self) -> None:
        # The AlphaEngine branch of the probe is a writer RPC, so "the writer
        # is busy or restarting" arrives here as an ordinary exception.  It
        # used to be recorded as a refused probe, which made a coverage item
        # permanently source_unavailable for a failure that was over a minute
        # later.
        from unittest.mock import patch

        from dalton_core.bounded_alphaengine_search_probe import (
            BoundedAlphaEngineSearchProbePending,
        )
        from dalton_core.writer_server import write_token_config
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        driver = self._driver(FakeTransport(FakeResponse(200, company_facts_body())))
        with patch(
            "dalton_core.bounded_planner_driver.execute_probe_work_order",
            side_effect=BoundedAlphaEngineSearchProbePending("discovery child still runs"),
        ):
            first = driver.run_once()
        self.assertEqual(first["probes_executed"], 0)
        self.assertEqual(first["executed"], [])
        held = first["skipped"][0]
        self.assertEqual(held["reason"], "probe_child_pending")
        store = DaltonStore(str(self.root / "core.sqlite"))
        self.addCleanup(store.close)
        authority = BoundedPlannerAuthority(store)
        # Nothing terminal was written: the round is still waiting.
        self.assertEqual(authority.outcomes(self.loop["id"]), [])

        # The next tick picks the same round up and finishes it.
        second = driver.run_once()
        self.assertEqual(second["probes_executed"], 1)
        entry = second["executed"][0]
        self.assertTrue(entry["resumed"])
        self.assertEqual(entry["round_ref"], held["round_ref"])
        self.assertEqual(entry["outcome_kind"], "observed")
        self.assertEqual(len(authority.outcomes(self.loop["id"])), 1)

    def test_a_rolling_quota_refusal_holds_then_resumes_same_round(self) -> None:
        from unittest.mock import patch

        from dalton_core.writer_server import write_token_config
        write_token_config(self.root / "tokens.json", list(self.server.principals.values()))
        driver = self._driver(FakeTransport(FakeResponse(200, company_facts_body())))
        quota = {
            "status": "failed",
            "error": {"code": "ALPHAENGINE_PROBE_BUDGET_EXCEEDED", "message": "window full"},
        }
        with patch(
            "dalton_core.bounded_planner_driver.execute_probe_work_order",
            return_value=quota,
        ):
            first = driver.run_once()
        self.assertEqual(first["probes_executed"], 0)
        held = first["skipped"][0]
        self.assertEqual(held["reason"], "alphaengine_quota_window_exhausted")

        second = driver.run_once()
        self.assertEqual(second["probes_executed"], 1)
        self.assertEqual(second["executed"][0]["round_ref"], held["round_ref"])
        self.assertTrue(second["executed"][0]["resumed"])

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
        self.assertEqual(parsed.planner_call_budget, {
            "max_input_tokens": 16000, "max_output_tokens": 1200,
            "max_cost_usd": 0.5, "timeout_seconds": 180,
        })
        configured = BoundedPlannerDriverConfig.from_mapping({
            **raw, "planner_call_budget": {
                "max_input_tokens": 9000, "max_output_tokens": 700,
                "max_cost_usd": 0.2, "timeout_seconds": 45,
            },
        })
        self.assertEqual(configured.planner_call_budget["max_cost_usd"], 0.2)
        self.assertEqual(configured.planner_call_budget["timeout_seconds"], 45)
        bad = dict(raw)
        bad["extra"] = True
        with self.assertRaises(BoundedPlannerDriverError):
            BoundedPlannerDriverConfig.from_mapping(bad)
        bad = dict(raw, writer_socket="relative/sock")
        with self.assertRaises(BoundedPlannerDriverError):
            BoundedPlannerDriverConfig.from_mapping(bad)


class StalledLoopTests(unittest.TestCase):
    """P14e/B2: a stalled loop must not be paid for, and must not stay stalled.

    Both failures were invisible in the summary.  A probe the executor refuses
    raised out of ``run_once`` and left the round admitted with no outcome, so
    the loop was pending forever; and every later tick would have asked a paid
    planner what to do about a loop that could not act.
    """

    class _Client:
        """A writer that answers the tick, and remembers what it was asked."""

        def __init__(self, *, materialize_error: str | None = None,
                     rounds_remaining: int = 2) -> None:
            self.materialize_error = materialize_error
            self.rounds_remaining = rounds_remaining
            self.calls: list[str] = []

        def call(self, operation, params=None):
            self.calls.append(operation)
            if operation == "bounded_planner_active_loops":
                return {"loops": [{
                    "loop_version_ref": "bounded-planner-loop-version:1",
                    "loop_ref": "bounded-loop:1",
                }]}
            if operation == "materialize_bounded_planner_context":
                if self.materialize_error is not None:
                    raise RuntimeError(self.materialize_error)
                return {
                    "id": "planner-context-pack-version:1",
                    "remaining_budget": {
                        "rounds_remaining": self.rounds_remaining,
                        "cost_units_remaining": 4, "seconds_remaining": 600,
                    },
                }
            if operation in {
                "bounded_planner_propose_next",
                "bounded_planner_propose_next_with_context",
            }:
                # No ``round`` in the reply, so the driver has nothing to
                # resume and reports the pending round as the skip it is.
                return {"status": "pending_round"}
            if operation == "llm_planner_execute":
                raise AssertionError("the planner model must not be called here")
            return {"status": "idle"}

    def _config(self, root: Path) -> BoundedPlannerDriverConfig:
        return BoundedPlannerDriverConfig(
            writer_socket=root / "writer.sock", token_config=root / "tokens.json",
            scheduler_db=root / "scheduler.sqlite", user_agent="Dalton Test",
            max_response_bytes=1_000_000, timeout_seconds=10.0,
            max_probes_per_tick=1, filed_window_days=400,
            observation_mandate_version_ref=None,
            doctrine_pack_version_ref="doctrine-pack-version:1",
            doctrine_pack_version_hash="d" * 64,
            planner_routing_policy_ref=None, planner_credential_slot_refs=None,
            planner_model_router_db=None, planner_broker_socket=None,
            planner_broker_auth_key=None, planner_broker_client_id="client:dalton-core",
            planner_expected_agent_id="chem", planner_max_cost_usd=0.5,
        )

    def _run(self, client) -> dict:
        with tempfile.TemporaryDirectory() as name:
            driver = BoundedPlannerDriver(
                self._config(Path(name)), client=client, transport=object(),
                clock=lambda: NOW,
            )
            return driver.run_once()

    def test_a_pending_round_is_named_and_costs_nothing(self) -> None:
        client = self._Client(
            materialize_error="cannot materialize context while a round is pending")
        result = self._run(client)
        self.assertEqual(result["skipped"][0]["reason"], "pending_round")
        self.assertNotIn("llm_planner_execute", client.calls)

    def test_a_doctrine_failure_still_reads_as_a_doctrine_failure(self) -> None:
        client = self._Client(materialize_error="doctrine pack hash binding failed")
        result = self._run(client)
        self.assertEqual(
            result["skipped"][0]["reason"], "doctrine_context_unavailable:RuntimeError")

    def test_a_loop_with_no_round_left_is_not_asked_a_paid_question(self) -> None:
        client = self._Client(rounds_remaining=0)
        result = self._run(client)
        self.assertNotIn("llm_planner_execute", client.calls)
        # The free deterministic planner still runs, so the loop can still
        # reach a terminal state rather than sitting there.
        self.assertIn("bounded_planner_propose_next_with_context", client.calls)
        self.assertEqual(result["skipped"][0]["reason"], "pending_round")

    def test_the_default_planner_price_is_named_once(self) -> None:
        from dalton_core.bounded_planner_driver import DEFAULT_PLANNER_MAX_COST_USD
        from dalton_core.research_task import default_planner_cost_usd

        self.assertEqual(
            float(default_planner_cost_usd()), DEFAULT_PLANNER_MAX_COST_USD)




class PlannerPoolDerivationTests(BoundedPlannerDriverTests):
    """C2b: the Core decides which pool a planner call spends from.

    The driver may declare one -- it reads it out of the projection -- but the
    loop record is the authority, exactly as the doctrine pack hash is.  A
    driver able to name its own pool could name the cheapest one and the 25%
    ad-hoc boundary would be advisory.
    """

    UNBUDGETED = {
        "routing_policy_ref": "model-routing-policy-version:test:1",
        "credential_slot_refs": ["credential-slot:openclaw:test"],
        "model_router_db": "/nonexistent/model-router.sqlite",
        "broker_socket": "/nonexistent/broker.sock",
        "broker_auth_key": "/nonexistent/broker.sock.key",
        "broker_client_id": "client:dalton-core",
        "expected_agent_id": "chem",
    }

    def _context_ref(self) -> str:
        pack = self.governance.call("publish_doctrine_pack", {
            "doctrine_pack_ref": "doctrine-pack:driver-pool",
            "title": "Driver Doctrine Pool",
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
        return self.core.call("materialize_bounded_planner_context", {
            "loop_version_ref": self.loop["id"],
            "doctrine_pack_version_ref": pack["id"],
            "doctrine_pack_version_hash": pack["content_hash"],
            "as_of": NOW.isoformat(timespec="microseconds"),
        })["id"]

    def test_writer_keeps_the_model_router_wal_available_to_strict_readers(self):
        from dalton_core.model_router import ModelRouter

        router_path = self.root / "owned-model-router.sqlite"
        second_router_path = self.root / "second-model-router.sqlite"
        with ModelRouter(router_path):
            pass
        with ModelRouter(second_router_path):
            pass
        config = {**self.UNBUDGETED, "model_router_db": str(router_path)}
        (self.root / "initial-screen-model-config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        (self.root / "dossier-model-config.json").write_text(
            json.dumps({**config, "model_router_db": str(second_router_path)}),
            encoding="utf-8",
        )
        server = WriterServer(
            self.root / "owner-core.sqlite", str(self.root / "owner.sock"),
            dict(self.server.principals),
            scheduler_path=self.root / "owner-scheduler.sqlite",
        )
        server.start()
        self.addCleanup(server.stop)

        with ModelRouter(router_path, read_only=True) as reader:
            self.assertEqual(
                reader.connection.execute("PRAGMA journal_mode").fetchone()[0],
                "wal",
            )
        with ModelRouter(second_router_path, read_only=True):
            pass
        server.stop()
        self.assertEqual(server._model_router_owners, [])
        self.assertFalse(Path(str(router_path) + "-wal").exists())
        self.assertFalse(Path(str(router_path) + "-shm").exists())
        self.assertFalse(Path(str(second_router_path) + "-wal").exists())
        self.assertFalse(Path(str(second_router_path) + "-shm").exists())

    def test_failed_writer_start_closes_a_model_router_opened_earlier(self):
        from dalton_core.model_router import ModelRouter

        router_path = self.root / "failed-start-router.sqlite"
        with ModelRouter(router_path):
            pass
        config = {**self.UNBUDGETED, "model_router_db": str(router_path)}
        server = WriterServer(
            self.root / "failed-core.sqlite", str(self.root / "failed.sock"),
            dict(self.server.principals),
            scheduler_path=self.root / "failed-scheduler.sqlite",
            planner_model_config=config,
        )
        with patch(
            "dalton_core.writer_server.BoundedPlannerControlPlane",
            side_effect=RuntimeError("injected authority failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected authority failure"):
                server.start()

        self.assertEqual(server._model_router_owners, [])
        self.assertFalse(Path(str(router_path) + "-wal").exists())
        self.assertFalse(Path(str(router_path) + "-shm").exists())

    def test_the_projection_tells_the_driver_which_pool_the_loop_drinks_from(
            self) -> None:
        loops = self.core.call("bounded_planner_active_loops", {})["loops"]
        # A loop the owner created directly is the mission's own coverage
        # work; only P14e's inquiry admissions are ad-hoc.
        self.assertEqual(loops[0]["pool"], "coverage")

    def test_the_wire_contract_accepts_pool_and_never_requires_it(self) -> None:
        from dalton_core.writer_server import OPERATION_FIELDS

        # OPERATION_FIELDS is the closed set of *allowed* field names, so
        # adding "pool" to it is additive by construction: a driver that has
        # not been upgraded sends what it always sent. The driver tests cover
        # the other half -- a projection with no pool sends no pool.
        self.assertIn("pool", OPERATION_FIELDS["llm_planner_execute"])

    def test_writer_binds_planner_retry_and_sizes_scheduler_for_the_route(self) -> None:
        from dalton_core.model_router import ModelRouter
        from tests.test_llm_research_planner_worker import (
            ReturnedProviderPlannerAdapter,
            alternate_profile,
            profile,
            retry_policy,
        )

        router_path = self.root / "planner-retry-router.sqlite"
        with ModelRouter(router_path, clock=lambda: NOW) as router:
            primary = profile()
            alternate = alternate_profile()
            for item in (primary, alternate):
                item["availability"] = {
                    **item["availability"],
                    "checked_at": NOW.isoformat(),
                    "valid_until": (NOW + timedelta(days=365)).isoformat(),
                }
            router.register_profile(primary)
            router.register_profile(alternate)
            router.register_policy(retry_policy())
        retry = {"max_same_profile_retries": 1, "retry_backoff_seconds": 0}
        config = {
            **self.UNBUDGETED,
            "routing_policy_ref": (
                "model-routing-policy-version:test-planner-retry:1"
            ),
            "credential_slot_refs": [
                "credential-slot:openclaw:test",
                "credential-slot:openclaw:test-z",
            ],
            "model_router_db": str(router_path),
            "provider_retry": retry,
        }
        server = WriterServer(
            self.root / "core.sqlite", str(self.root / "planner-retry.sock"),
            dict(self.server.principals),
            scheduler_path=self.root / "planner-retry-scheduler.sqlite",
            planner_model_config=config,
        )
        server.start()
        self.addCleanup(server.stop)
        adapter = ReturnedProviderPlannerAdapter({}, ["RATE_LIMITED"])
        context_ref = self._context_ref()
        with patch(
            "dalton_core.openclaw_model_adapter.OpenClawModelAdapter",
            return_value=adapter,
        ):
            result = server._store_executor.submit(
                server._op_llm_planner_execute,
                {"context_pack_ref": context_ref, "max_cost_usd": 0.5},
            ).result(timeout=30)
        authority, status, formal = server._store_executor.submit(
            lambda: (
                server._scheduler.work_order_authority(result["work_order_ref"]),
                server._scheduler.status(result["work_order_ref"]),
                server._scheduler.formal_result(result["work_order_ref"]),
            )
        ).result(timeout=30)
        self.assertEqual(
            result["status"], "model_retryable", (result, status, formal)
        )
        self.assertEqual(status["max_attempts"], 4)
        self.assertEqual(
            authority["work_order"]["metadata"]["provider_retry"], retry
        )

    def _budget_binding(self) -> dict:
        from dalton_core.budget_pools import mission_pool_scope

        mission = {
            "mission_ref": "coverage-mission:c2b",
            "id": "coverage-mission-version:c2b:1",
            "content_hash": content_hash({"mission": "c2b"}),
            "budget": {"max_daily_paid_calls": 100, "max_daily_cost_usd": 10.0},
        }
        return {
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "max_daily_paid_calls": 100,
            "max_daily_cost_micros": 10_000_000,
            **mission_pool_scope(
                mission, pool="adhoc", lane="llm_planner_execute"),
        }

    def _local(self, *, budget_db: str | None = None,
               policy: str | None = None) -> WriterServer:
        """A second server on the same Core, in this thread, with a planner.

        The op is read directly rather than over the socket because the wire
        deliberately answers every refusal with one opaque sentence, and the
        thing under test here is *which* refusal it was.  SQLite connections
        belong to the thread that opened them, so the served instance cannot
        be called from the test.
        """

        config = dict(self.UNBUDGETED)
        if budget_db is not None:
            config["budget_db"] = budget_db
            config["budget_policy_ref"] = policy
        server = WriterServer(
            self.root / "core.sqlite", str(self.root / "local.sock"),
            dict(self.server.principals), scheduler_path=self.scheduler_path,
            planner_model_config=config,
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def _op(self, params: dict):
        """Run one op the way the server runs it: on its own store thread.

        Every Core connection belongs to the single ``dalton-store`` worker a
        WriterServer opens, so an op called from anywhere else is a SQLite
        thread error rather than an answer.  ``Future.result`` re-raises what
        the op raised, which is the message under test.
        """

        server = self._local()
        return server._store_executor.submit(
            server._op_llm_planner_execute, params).result(timeout=30)

    def test_a_pool_that_is_not_one_of_the_four_is_refused(self) -> None:
        context_ref = self._context_ref()
        with self.assertRaisesRegex(WriterServerError, "capacity pools"):
            self._op({"context_pack_ref": context_ref,
                      "max_cost_usd": 0.5, "pool": "petty_cash"})

    def test_a_driver_that_names_a_cheaper_pool_than_its_loop_is_refused(
            self) -> None:
        context_ref = self._context_ref()
        with self.assertRaisesRegex(
                WriterServerError, "coverage pool, not maintenance"):
            self._op({"context_pack_ref": context_ref,
                      "max_cost_usd": 0.5, "pool": "maintenance"})

    def test_an_unbudgeted_planner_config_binds_nothing_and_says_so(self) -> None:
        # The pre-C2b shape: no budget_db, so no mission binding, so the call
        # runs exactly as it did before and the op result says "unbudgeted"
        # rather than implying a ledger saw it.
        server = self._local()
        self.assertIsNone(server._store_executor.submit(
            server._planner_budget_binding, "coverage").result(timeout=30))

    def test_the_writer_holds_the_ledger_so_settled_spend_is_readable(
            self) -> None:
        """The failure this branch nearly shipped.

        The ledger is WAL and C2's read-only open refuses a database with no
        sidecars.  Opening it per op meant the sidecars existed only while a
        planner call was in flight, and P14e's admission lane reads *between*
        calls -- so ``settled_micros`` would have been zero in every real
        deployment and the cap enforced would still have been the pre-C2b one.
        """

        from dalton_core.budget_pools import day_pool_spend_at
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore

        budget_db = self.root / "thesis-impact-budget.sqlite"
        policy = "thesis-impact-day-budget-policy:c2b-writer:1"
        server = self._local(budget_db=str(budget_db), policy=policy)
        def book() -> None:
            # On the store thread, because that is the thread the writer's
            # ledger connection belongs to -- and the only one an op runs on.
            ledger = server._planner_budget_ledger()
            ledger.register_policy(
                policy_version_id=policy, day_cap_micros=100_000_000)
            admitted = ledger.admit(
                policy_version_id=policy, day="2026-08-23",
                work_order_ref="work:llm-research-planner-held",
                attempt_number=1, phase="assessment",
                route_decision_ref="route:1", reserved_micros=500_000,
                mission_binding=self._budget_binding(),
            )
            ledger.settle(admitted["admission_id"], actual_micros=300_000)

        server._store_executor.submit(book).result(timeout=30)

        # No op in flight, and the reading still works -- which is only true
        # because somebody is holding the ledger open.
        spend = day_pool_spend_at(
            budget_db, day="2026-08-23", mission_ref="coverage-mission:c2b")
        self.assertEqual(spend.get("adhoc"), 300_000)

        # And when the writer lets go, the sidecars go with it: this asserts
        # the mechanism rather than the wish.
        server.stop()
        self.assertEqual(
            day_pool_spend_at(budget_db, day="2026-08-23"), {})

    def test_pool_for_loop_reads_why_the_loop_exists(self) -> None:
        from dalton_core.budget_pools import pool_for_loop

        self.assertEqual(pool_for_loop({"admission": {"source": "inquiry"}}),
                         "adhoc")
        # A loop with no admission block predates P14e and is coverage, and so
        # is one admitted by anything the map does not name.
        self.assertEqual(pool_for_loop({}), "coverage")
        self.assertEqual(pool_for_loop(None), "coverage")
        self.assertEqual(pool_for_loop({"admission": {"source": "mandate"}}),
                         "coverage")

class PlannerPoolHoldTests(unittest.TestCase):
    """C2b: a spent capacity pool holds the loop; it does not spend a round.

    The writer refuses before it leases anything, so the honest thing for the
    driver to do is nothing at all.  What it must *not* do is fall through to
    the free deterministic planner: that would consume the round the pool just
    said no to, and the loop would arrive at tomorrow with one fewer round and
    nothing to show for it.
    """

    class _Client:
        def __init__(self, *, pool: str | None = "adhoc",
                     rejected: bool = True, budget: dict | None = None) -> None:
            self.pool = pool
            self.rejected = rejected
            self.budget = budget
            self.calls: list[str] = []
            self.planner_params: dict | None = None

        def call(self, operation, params=None):
            self.calls.append(operation)
            if operation == "bounded_planner_active_loops":
                loop = {
                    "loop_version_ref": "bounded-planner-loop-version:1",
                    "loop_ref": "bounded-loop:1",
                }
                if self.pool is not None:
                    loop["pool"] = self.pool
                return {"loops": [loop]}
            if operation == "materialize_bounded_planner_context":
                return {
                    "id": "planner-context-pack-version:1",
                    "remaining_budget": {
                        "rounds_remaining": 2, "cost_units_remaining": 4,
                        "seconds_remaining": 600,
                    },
                }
            if operation == "llm_planner_execute":
                self.planner_params = dict(params or {})
                if self.rejected:
                    return {
                        "status": "rejected", "reason": POOL_EXHAUSTED_REASON,
                        "lane_status": POOL_EXHAUSTED_STATUS,
                        "pool": "adhoc", "day": "2026-09-09",
                        "spent": 2_500_000, "cap": 2_500_000,
                    }
                answer = {"status": "model_failed"}
                if self.budget is not None:
                    answer["budget"] = self.budget
                return answer
            if operation in {
                "bounded_planner_propose_next",
                "bounded_planner_propose_next_with_context",
            }:
                return {"status": "pending_round"}
            return {"status": "idle"}

    def _run(self, client, *, root: Path | None = None) -> dict:
        with tempfile.TemporaryDirectory() as name:
            root = root or Path(name)
            config = BoundedPlannerDriverConfig(
                writer_socket=root / "writer.sock",
                token_config=root / "tokens.json",
                scheduler_db=root / "scheduler.sqlite", user_agent="Dalton Test",
                max_response_bytes=1_000_000, timeout_seconds=10.0,
                max_probes_per_tick=1, filed_window_days=400,
                observation_mandate_version_ref=None,
                doctrine_pack_version_ref="doctrine-pack-version:1",
                doctrine_pack_version_hash="d" * 64,
                planner_routing_policy_ref=None,
                planner_credential_slot_refs=None,
                planner_model_router_db=None, planner_broker_socket=None,
                planner_broker_auth_key=None,
                planner_broker_client_id="client:dalton-core",
                planner_expected_agent_id="chem", planner_max_cost_usd=0.5,
            )
            return BoundedPlannerDriver(
                config, client=client, transport=object(), clock=lambda: NOW,
            ).run_once()

    def test_a_spent_pool_holds_the_loop_without_consuming_its_round(self) -> None:
        client = self._Client()
        result = self._run(client)

        held = result["skipped"][0]
        self.assertEqual(held["reason"], POOL_EXHAUSTED_REASON)
        self.assertEqual(held["pool"], "adhoc")
        self.assertEqual(held["lane_status"], POOL_EXHAUSTED_STATUS)
        self.assertEqual(held["loop_version_ref"],
                         "bounded-planner-loop-version:1")
        # The deterministic fallback is for a planner that failed, not for a
        # pool that refused: reaching it here would spend the round.
        self.assertNotIn("bounded_planner_propose_next_with_context",
                         client.calls)
        self.assertNotIn("bounded_planner_propose_next", client.calls)

    def test_the_pool_the_projection_named_travels_with_the_call(self) -> None:
        client = self._Client(pool="adhoc")
        self._run(client)
        self.assertEqual(client.planner_params["pool"], "adhoc")

    def test_a_projection_without_a_pool_sends_no_pool_at_all(self) -> None:
        # An older writer's projection has no "pool" key. The driver must not
        # invent one: the loop record is what decides, and a guess that
        # disagreed with it would be refused.
        client = self._Client(pool=None, rejected=False)
        self._run(client)
        self.assertNotIn("pool", client.planner_params)

    def test_the_hold_and_the_budget_word_reach_the_tick_ledger(self) -> None:
        # A summary the next tick overwrites cannot answer "is the 25%
        # boundary doing anything" or "is the planner on the ledger yet".
        from dalton_core.tick_ledger import TickLedger

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            summary = self._run(self._Client(), root=root)

            reported = summary["planner_budget"]
            self.assertEqual(reported["pool_holds"], 1)
            self.assertEqual(reported["held_pools"], ["adhoc"])
            self.assertEqual(summary["tick_ledger"]["status"], "recorded")

            with TickLedger(root / "tick-ledger.sqlite", read_only=True) as led:
                row = led.connection.execute(
                    "SELECT planner_budget_json FROM tick_ledger_ticks"
                ).fetchone()
            self.assertEqual(json.loads(row["planner_budget_json"]), reported)

    def test_an_unbudgeted_planner_is_a_standing_condition_the_tick_records(
            self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            summary = self._run(
                self._Client(pool="coverage", rejected=False,
                             budget={"status": "unbudgeted"}),
                root=root)
            self.assertEqual(summary["planner_budget"]["status"], "unbudgeted")
            self.assertEqual(summary["planner_budget"]["pool_holds"], 0)

    def test_planner_budget_is_not_mistaken_for_a_lane(self) -> None:
        # It is a Mapping in the tick summary, and every Mapping in the
        # summary that is not reserved becomes a lane row -- which would put
        # the driver's own work into the idle ratio.
        from dalton_core.lane_registry import RESERVED_DRIVER_KEYS

        self.assertIn("planner_budget", RESERVED_DRIVER_KEYS)

    def test_a_model_failure_still_falls_through_to_the_free_planner(self) -> None:
        # The hold is narrow: only "rejected/pool_exhausted" holds. Every
        # other planner outcome keeps P14e's behaviour.
        client = self._Client(pool="coverage", rejected=False)
        result = self._run(client)
        self.assertIn("bounded_planner_propose_next_with_context", client.calls)
        self.assertNotEqual(result["skipped"][0]["reason"],
                            POOL_EXHAUSTED_REASON)

if __name__ == "__main__":
    unittest.main()
