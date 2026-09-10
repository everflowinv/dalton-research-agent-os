from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from dalton_core.bounded_planner_loop import BoundedPlannerControlPlane
from dalton_core.mission_source_discovery import load_discovery_plan
from dalton_core.observability import ObservabilityStore
from dalton_core.scheduler import Scheduler
from dalton_core.writer_server import WriterServer, WriterServerError
from dalton_core.bounded_alphaengine_search_probe import (
    BoundedAlphaEngineSearchProbePending, execute_alphaengine_search_probe)
from tests.test_research_task import ResearchTaskFixture, inquiry
from dalton_core import research_task as rt


class Launcher:
    def __init__(self):
        self.starts = 0
        self.records = {}

    def start(self, **kwargs):
        self.starts += 1
        ticket = {"id": "alphaengine-search-ticket:" + f"{self.starts:024x}",
                  "status": "running"}
        self.records[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return dict(self.records[ticket_ref])


class InquiryDiscoveryWriterTests(ResearchTaskFixture):
    publishes = ("probe-template:inquiry-alphaengine-discovery-refresh:v1",)
    daily_cost_usd = 20.0

    def setUp(self):
        super().setUp()
        body = {key: self.mission[key] for key in (
            "title", "objective", "industry_ref", "universe", "research_questions",
            "deliverables", "source_plan", "bindings", "autonomy", "budget")}
        body["source_plan"] = [
            {**row, "status": "connected"} if row["source_ref"] == "source:alphaengine" else row
            for row in body["source_plan"]]
        body["autonomy"] = {**body["autonomy"], "may_write": sorted(
            set(body["autonomy"]["may_write"]) | {"source_discovery"})}
        self.mission = self.missions.create_mission(
            self.mission["mission_ref"], **body, actor_ref="human:p14e-test-owner",
            version_id="coverage-mission-version:us-it-services:2",
            prior_version_ref=self.mission["id"], idempotency_key="inquiry-search:mission:v2")
        wire = inquiry(question="How does ACN reconcile adjusted margin guidance?")
        plan = self.record_plan([wire])
        admitted = self.admit(plan, self.admissions(plan)[0], wire)
        self.loop = self.authority.loop(admitted["loop_version_ref"])
        self.scheduler = Scheduler(connection=self.store.connection)
        control = BoundedPlannerControlPlane(
            self.authority, ObservabilityStore(self.store), self.scheduler)
        proposal = self.authority.propose_next_capital_lease(self.loop["id"])
        round_wire = control.admit_proposal(proposal["id"])["round"]
        self.work = self.scheduler.work_order_authority(round_wire["work_order_ref"])["work_order"]
        production_plan = load_discovery_plan(
            Path(__file__).resolve().parents[1] /
            "deploy/phase9/p9d-us-it-services-discovery-plan-v1.json")
        self.launcher = Launcher()
        server = object.__new__(WriterServer)
        server._scheduler = self.scheduler
        server._bounded_planner = self.authority
        server._coverage_mission = self.missions
        coordinator = Mock()
        coordinator.plan = production_plan
        server._source_discovery = coordinator
        server._search_launcher = self.launcher
        self.server = server

    def call(self, work=None):
        return self.server._op_start_bounded_source_discovery(
            {"work_order": self.work if work is None else work})

    def test_exact_admitted_work_launches_once_and_retry_reuses_ticket(self):
        first = self.call()
        second = self.call()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.launcher.starts, 1)
        dispatches = self.missions.discovery_dispatches(
            self.mission["id"], company_ref=rt.ADHOC_PROBE_TEMPLATES[0].get("company_ref", "company:sec-cik:0001467373"))
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(self.launcher.starts, 1)
        self.assertEqual(self.loop["budget"]["max_cost_units"], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM scheduler_work_orders WHERE work_order_id=?",
            (self.work["id"],)).fetchone()[0], 1)

    def test_scheduler_authority_rejects_every_work_mutation_before_launch(self):
        cases = {
            "work": {**self.work, "id": self.work["id"] + ":forged"},
            "template": {**self.work, "metadata": {**self.work["metadata"],
                "probe_template_version_hash": "0" * 64}},
            "mission": {**self.work, "metadata": {**self.work["metadata"],
                "mission_version_hash": "0" * 64}},
            "company": {**self.work, "metadata": {**self.work["metadata"],
                "parameters": {**self.work["metadata"]["parameters"], "inquiry_hash": "0" * 64}}},
            "plan": {**self.work, "metadata": {**self.work["metadata"],
                "parameters": {**self.work["metadata"]["parameters"], "discovery_plan_hash": "0" * 64}}},
        }
        for name, forged in cases.items():
            with self.subTest(name=name), self.assertRaises(WriterServerError):
                self.call(forged)
        self.assertEqual(self.launcher.starts, 0)

    def test_executor_timeout_then_next_tick_reuses_child_and_returns_result(self):
        outer = self
        class Client:
            def call(self, operation, params):
                if operation == "start_bounded_source_discovery":
                    return outer.call(params["work_order"])
                return outer.launcher.status(params["ticket_ref"])
        client = Client()
        with self.assertRaises(BoundedAlphaEngineSearchProbePending):
            execute_alphaengine_search_probe(
                self.work, client=client, timeout_seconds=0, poll_seconds=0)
        ticket = next(iter(self.launcher.records))
        query_hash = self.missions.discovery_dispatches(self.mission["id"])[0]["query_hash"]
        self.launcher.records[ticket] = {"id": ticket, "status": "succeeded",
            "summary": {"status": "succeeded", "query_hash": query_hash, "provider_calls": 1,
                "search": {"outcome": "succeeded", "document_refs": ["document:a"],
                    "connector_invocation_ref": "connector-invocation:a",
                    "connector_invocation_hash": "d" * 64,
                    "source_envelope_ref": "source-envelope:a",
                    "source_envelope_hash": "e" * 64}}}
        result = execute_alphaengine_search_probe(
            self.work, client=client, timeout_seconds=0, poll_seconds=0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.launcher.starts, 1)

    def test_scheduler_registered_shape_without_an_admitted_round_is_refused(self):
        forged = {**self.work, "id": self.work["id"] + ":orphan",
                  "idempotency_key": self.work["idempotency_key"] + ":orphan"}
        self.assertEqual(self.scheduler.enqueue(forged)["status"], "fresh")
        with self.assertRaisesRegex(WriterServerError, "not an admitted loop round"):
            self.call(forged)
        self.assertEqual(self.launcher.starts, 0)

    def test_unadmitted_but_well_shaped_work_cannot_bypass_template_gate(self):
        forged = {**self.work, "id": self.work["id"] + ":not-admitted"}
        with self.assertRaises(WriterServerError):
            self.call(forged)
        self.assertEqual(self.launcher.starts, 0)

    def test_configured_plan_drift_is_refused_before_launch(self):
        original = self.server._source_discovery.plan
        for field, value in (("id", "discovery-plan:wrong"),
                             ("content_hash", "0" * 64),
                             ("mission_ref", "coverage-mission:wrong")):
            with self.subTest(field=field):
                self.server._source_discovery.plan = {**original, field: value}
                with self.assertRaises(WriterServerError):
                    self.call()
                self.server._source_discovery.plan = original
        self.assertEqual(self.launcher.starts, 0)

    def test_republished_disabled_template_cannot_be_bypassed_by_old_work(self):
        old = self.authority.probe_template(self.work["metadata"]["probe_template_version_ref"])
        self.authority.publish_probe_template(
            old["template_ref"], capability_ref=old["capability_ref"],
            operation="disabled_discovery", runtime_profile_ref=old["runtime_profile_ref"],
            parameter_contract=old["parameter_contract"],
            output_contract_ref=old["output_contract_ref"], verifier_ref=old["verifier_ref"],
            permission_scope=old["permission_scope"],
            declared_side_effects=old["declared_side_effects"], cost=old["cost"],
            actor_ref="human:p14e-test-owner", prior_version_ref=old["id"])
        with self.assertRaises(WriterServerError):
            self.call()
        self.assertEqual(self.launcher.starts, 0)


if __name__ == "__main__":
    unittest.main()
