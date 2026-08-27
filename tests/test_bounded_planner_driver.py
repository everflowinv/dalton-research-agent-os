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
            "objective": "Driver test mandate.", "scope_refs": [INDUSTRY, ACN],
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
        self.config = BoundedPlannerDriverConfig(
            writer_socket=Path(self.socket),
            token_config=self.root / "tokens.json",
            scheduler_db=self.scheduler_path,
            user_agent="Dalton Test",
            max_response_bytes=1_000_000,
            timeout_seconds=10.0,
            max_probes_per_tick=1,
            filed_window_days=400,
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

        authority = BoundedPlannerAuthority(
            DaltonStore(str(self.root / "core.sqlite"))
        )
        self.assertEqual(2, len(authority.outcomes(self.loop["id"])))
        terminal = authority.terminal(self.loop["id"])
        self.assertEqual("evidence_observed_for_review", terminal["terminal_state"])

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
        }
        parsed = BoundedPlannerDriverConfig.from_mapping(raw)
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
