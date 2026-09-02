"""Adversarial tests for the authority-bound research-plan executor."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.authority_resolver import ConnectorAuthorityResolver
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.connector_authority_port import ConnectorAuthorityPort
from dalton_core.connector_inventory import load_packaged_connector_inventory
from dalton_core.connector_runner import (
    ConnectorRunnerAdmissionGate,
    StaticAdapterResolver,
    validate_runner_environment_manifest,
)
from dalton_core.connector_transport_executor import ConnectorTransportExecutor
from dalton_core.observability import ObservabilityStore
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.raw_spool import RawSpool
from dalton_core.research_coordinator import ResearchCoordinatorStore
from dalton_core.research_auto_commit import (
    COMPANY_FACTS_RULE_REF as COMPANY_FACTS_AUTO_COMMIT_RULE_REF,
    RULE_REF as AUTO_COMMIT_RULE_REF,
)
from dalton_core.research_plan import (
    PLAN_AUTO_START_RULE_REF,
    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF,
    _plan_work_orders,
    sec_current_rate_policy_ref,
    sec_current_runner_binding_ref,
    sec_current_runner_environment_ref,
)
from dalton_core.research_plan_coordinator import (
    ResearchPlanCoordinator,
    ResearchPlanCoordinatorConflict,
    _stage_output_ref,
)
from dalton_core.research_plan_executor import (
    ResearchPlanExecutor,
    ResearchPlanExecutorConflict,
    sec_connector_identity,
    sec_descriptor_spec,
)
from dalton_core.research_verification import CandidateStagingStore
from dalton_core.runner_journal import RunnerJournal
from dalton_core.sec_authority_harness import (
    MutableClock,
    PUBLIC_PERMISSIONS,
    _PublicAuthorities,
    _Response,
)
from dalton_core.sec_public_adapter import SecPublicRouterAdapter
from dalton_core.store import content_hash
from tests import test_research_plan as planner_test_support


def _sec_body() -> bytes:
    rows = [
        ("0000320193-26-000001", "8-K", "2026-01-01", "jan.htm", None),
        ("0000320193-26-000002", "10-Q", "2026-01-29", "q1.htm", None),
        ("0000320193-26-000003", "10-Q/A", "2026-02-15", "q1a.htm",
         "0000320193-26-000002"),
        ("0000320193-26-000004", "10-Q", "2026-04-29", "q2.htm", None),
        ("0000320193-26-000005", "8-K", "2026-08-15", "aug.htm", None),
    ]
    recent = {
        "accessionNumber": [row[0] for row in rows],
        "form": [row[1] for row in rows],
        "filingDate": [row[2] for row in rows],
        "primaryDocument": [row[3] for row in rows],
        "amendmentOf": [row[4] for row in rows],
    }
    return json.dumps(
        {"cik": "320193", "filings": {"recent": recent}},
        sort_keys=True, separators=(",", ":"),
    ).encode()


def _sec_company_facts_body() -> bytes:
    concept = {
        "label": "Revenue from Contract with Customer, Excluding Assessed Tax",
        "description": "Synthetic quarterly revenue series.",
        "units": {"USD": [
            {
                "start": "2025-01-01", "end": "2025-03-31",
                "val": 100000000000, "accn": "0000320193-26-000101",
                "fy": 2026, "fp": "Q2", "form": "10-Q",
                "filed": "2026-05-01", "frame": "CY2025Q1",
            },
            {
                "start": "2026-01-01", "end": "2026-03-31",
                "val": 112500000000, "accn": "0000320193-26-000101",
                "fy": 2026, "fp": "Q2", "form": "10-Q",
                "filed": "2026-05-01", "frame": "CY2026Q1",
            },
        ]},
    }
    payload = {
        "cik": 320193,
        "entityName": "APPLE INC.",
        "facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": concept,
        }},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class InjectedExecutorCrash(RuntimeError):
    pass


class PlanExecutorHarness:
    """One approved + started SEC plan with the full real connector stack."""

    def __init__(
        self,
        *,
        suffix: str = "exec",
        fault_at: str | None = None,
        auto_start: bool = False,
        company_facts: bool = False,
    ):
        self.planner = planner_test_support.ResearchPlanTests(
            methodName="test_create_plan_is_exact_closed_four_step_tree"
        )
        self.planner.setUp()
        self._cleanups = [self.planner.doCleanups]
        if company_facts:
            decision, records = self.planner._selected_questions([(
                f"How did quarterly revenue change year over year for {suffix}?",
                "Return the exact same-filing SEC quarterly revenue comparison",
            )])
            record = records[0]
            created = self.planner.plans.create_company_facts_plan(
                question_ref=record["question_ref"],
                question_version_ref=record["question_version_ref"],
                decision_ref=decision["id"],
                cik="320193",
                filed_from="2025-08-20",
                filed_to="2026-08-20",
                actor_ref="core:planner",
                idempotency_key=f"create-plan:{suffix}",
            )
        else:
            created = self.planner._create_plan(suffix=suffix)
        if auto_start:
            active = self.planner.store.active_policy()
            policy = dict(active["policy"])
            policy["research_plan_auto_start"] = {
                "enabled": True,
                "rules": [
                    PLAN_COMPANY_FACTS_AUTO_START_RULE_REF
                    if company_facts else PLAN_AUTO_START_RULE_REF
                ],
            }
            policy["research_candidate_auto_commit"] = {
                "enabled": True,
                "rules": [
                    COMPANY_FACTS_AUTO_COMMIT_RULE_REF
                    if company_facts else AUTO_COMMIT_RULE_REF
                ],
                "max_records": 20,
            }
            self.planner.store.create_policy(
                policy,
                policy_version_id=f"policy:autonomous-research:{suffix}:v2",
                version_number=2,
                prior_version_ref=active["policy_version_id"],
                actor_ref="human:test-owner",
                change_reason="authorize isolated autonomous SEC research",
                activate=True,
            )
            self.planner.plans.authorize_plan_by_policy(
                plan_version_ref=created["plan_version_ref"],
                idempotency_key=f"authorize-plan:{suffix}",
            )
        else:
            self.planner._approve(created, suffix=suffix)
        self.planner._start(created, suffix=suffix)
        self.plan_wire = self.planner.plans.plan_version(
            created["plan_version_ref"]
        )
        self.work_orders = _plan_work_orders(self.plan_wire)
        self.clock = MutableClock()
        # The plan control plane used the real-time scheduler clock; from
        # here on every component shares one frozen executor clock.
        self.planner.scheduler.clock = self.clock
        self.temp = tempfile.TemporaryDirectory()
        self.core = self.planner.store
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.authorities = _PublicAuthorities()
        self.catalog = CapabilityCatalog(
            ":memory:", clock=self.clock,
            approval_resolver=self.authorities.approval,
            policy_resolver=self.authorities.policy,
        )
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        # The spool high-water mark must admit one reservation at the current
        # SEC response-budget head.  The response fixture itself remains
        # tiny; this only mirrors production's conservative reservation.
        self.spool = RawSpool(self.temp.name, max_total_bytes=10_000_000)

        self.template = load_packaged_connector_inventory()["templates"]["sec"]
        self.identity = sec_connector_identity(
            self.template, self.plan_wire["execution_scope"]["operation"]
        )
        self.actor_ref = "runner:research-plan-executor"
        self.permissions = copy.deepcopy(PUBLIC_PERMISSIONS)
        # The operator publishes the exact capability descriptor once, then
        # installs the runner manifest bound to that published revision.
        self.descriptor = self.catalog.publish(sec_descriptor_spec(
            self.template, self.permissions,
            self.clock.value.isoformat(timespec="microseconds"),
            operation_name=self.identity["operation"],
        ))
        binding = {
            "binding_ref": sec_current_runner_binding_ref(),
            "descriptor_revision_ref": self.descriptor.revision_ref,
            "descriptor_hash": self.descriptor.content_hash,
            "adapter_ref": self.identity["adapter_ref"],
            "adapter_hash": self.identity["adapter_hash"],
            "source_ref": self.identity["source_identity"]["source_ref"],
            "source_hash": self.identity["source_hash"],
            "operation": self.identity["operation"],
            "input_schema_ref": self.identity["input_schema_ref"],
            "input_schema_hash": self.identity["input_schema_hash"],
            "output_schema_ref": self.identity["output_schema_ref"],
            "output_schema_hash": self.identity["output_schema_hash"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "required_permissions": copy.deepcopy(self.permissions),
            "side_effects": ["read:public-http"],
            "rate_policy_ref": sec_current_rate_policy_ref(),
        }
        manifest_payload = {
            "schema_version": "0.1",
            "id": sec_current_runner_environment_ref(),
            "created_at": MutableClock().value.isoformat(timespec="microseconds"),
            "runner_runtime_ref": "runner-runtime:sec-public:v1",
            "runner_actor_ref": self.actor_ref,
            "resolver_ref": "resolver:connector-static:0.1",
            "resolver_version": "0.1",
            "package_manifest_ref": "artifact:runner-packages:sec-public:v1",
            "package_manifest_hash": "9" * 64,
            "bindings": [binding],
        }
        self.manifest = validate_runner_environment_manifest({
            **manifest_payload,
            "content_hash": content_hash(manifest_payload),
        })
        self.runner_environment_hash = self.manifest["content_hash"]
        adapter = SecPublicRouterAdapter(
            transport=PublicHttpTransport(
                resolver=lambda _host, _port: ("93.184.216.34",),
                exchange=lambda _target, _method, _headers, _body, _timeout: _Response(
                    _sec_company_facts_body() if company_facts else _sec_body()
                ),
            ),
            clock=self.clock,
        )
        self.static_resolver = StaticAdapterResolver(
            self.manifest,
            {binding["binding_ref"]: adapter},
            {
                binding["binding_ref"]: (
                    lambda params: set(params) == (
                        {
                            "cik", "taxonomy", "concept_candidates", "unit", "form",
                            "filed_from", "filed_to",
                        }
                        if company_facts
                        else {"issuer", "form", "date_from", "date_to", "limit"}
                    )
                )
            },
        )
        self.gate = ConnectorRunnerAdmissionGate(
            scheduler=self.planner.scheduler,
            catalog=self.catalog,
            connectors=self.connectors,
            resolver=self.static_resolver,
            visibility_scopes=["research"],
            clock=self.clock,
        )
        self.authority_port = ConnectorAuthorityPort(
            connectors=self.connectors,
            observability=self.observability,
            scheduler=self.planner.scheduler,
        )
        self.transport = ConnectorTransportExecutor(
            gate=self.gate,
            journal=self.journal,
            spool=self.spool,
            authority=self.authority_port,
            connector_reader=self.connectors,
            clock=self.clock,
        )
        self.connector_records = ResearchCoordinatorStore(":memory:")
        self.coordinator = ResearchPlanCoordinator(
            plan=self.planner.plans,
            scheduler=self.planner.scheduler,
            connector_records=self.connector_records,
        )
        self.staging = CandidateStagingStore(":memory:")
        self.resolver = ConnectorAuthorityResolver(
            core=self.core,
            connectors=self.connectors,
            observability=self.observability,
            scheduler=self.planner.scheduler,
            coordinator=self.connector_records,
            artifact_reader=lambda artifact: self.spool.read_object(
                artifact["artifact_content_hash"]
            ),
            runner_journal=self.journal,
        )

        faulted = False

        def fault_hook(seam: str) -> None:
            nonlocal faulted
            if seam == fault_at and not faulted:
                faulted = True
                raise InjectedExecutorCrash(seam)

        self.executor = ResearchPlanExecutor(
            plan=self.planner.plans,
            scheduler=self.planner.scheduler,
            connector_records=self.connector_records,
            coordinator=self.coordinator,
            connectors=self.connectors,
            catalog=self.catalog,
            transport=self.transport,
            resolver=self.resolver,
            staging=self.staging,
            clock=self.clock,
            permissions=self.permissions,
            policy_resolver=self.authorities.policy,
            principal_ref="principal:worker-1",
            runner_environment_hash=self.runner_environment_hash,
            actor_ref=self.actor_ref,
            fault_injector=fault_hook if fault_at is not None else None,
        )

    def close(self) -> None:
        self.connector_records.close()
        self.staging.close()
        self.catalog.close()
        self.scheduler().close()
        self.temp.cleanup()
        for cleanup in self._cleanups:
            cleanup()

    def scheduler(self):
        return self.planner.scheduler

    def run_to_complete(self) -> list[dict]:
        outcomes: list[dict] = []
        while True:
            outcome = self.executor.run_once(
                plan_version_ref=self.plan_wire["id"]
            )
            outcomes.append(outcome)
            if outcome["status"] in {"complete", "blocked"}:
                return outcomes

    def staging_counts(self) -> dict[str, int]:
        return self.staging.counts()


class ResearchPlanExecutorTests(unittest.TestCase):
    def harness(self, **kwargs) -> PlanExecutorHarness:
        harness = PlanExecutorHarness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def test_full_tree_runs_connector_to_staged_candidate_one_edge_at_a_time(self) -> None:
        harness = self.harness(suffix="full")
        outcomes = harness.run_to_complete()
        statuses = [item["status"] for item in outcomes]
        self.assertEqual(
            statuses, ["admitted", "admitted", "admitted", "complete"]
        )
        self.assertEqual(
            [item["admitted_step_ordinal"] for item in outcomes[:3]],
            [2, 3, 4],
        )
        self.assertEqual(
            [item["admitted_stage"] for item in outcomes[:3]],
            ["authority_resolver", "verifier", "candidate_staging"],
        )
        status = harness.coordinator.tree_status(harness.plan_wire["id"])
        self.assertEqual(
            [node["attempt_state"] for node in status["nodes"]],
            ["succeeded", "succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual(
            [node["admission_state"] for node in status["nodes"]],
            ["queued", "queued", "queued", "queued"],
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)
        self.assertEqual(harness.staging_counts()["candidate_evidence_versions"], 1)
        # The connector authority chain is exact: one source envelope, one raw
        # artifact, one receipt, one checkpoint, one formal result.
        core = harness.core.connection
        self.assertEqual(
            core.execute(
                "SELECT COUNT(*) FROM connector_source_envelopes"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            core.execute(
                "SELECT COUNT(*) FROM observability_artifact_versions_v2"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_completion_receipts"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_checkpoints"
            ).fetchone()[0],
            1,
        )
        # No formal Ledger write and no auto-accept of human review.
        for table in ("claim_versions", "evidence_versions", "thesis_versions"):
            self.assertEqual(
                core.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )
        # The staged candidate carries the verified numeric claim.
        claim = harness.staging.connection.execute(
            "SELECT record_json FROM candidate_claim_versions"
        ).fetchone()[0]
        self.assertEqual(json.loads(claim)["value"], "3")
        self.assertEqual(
            json.loads(claim)["semantic_verification_status"], "unverified"
        )
        # Replay after completion is a no-op complete.
        replay = harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(replay["status"], "complete")
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)

    def test_company_facts_tree_stages_verified_revenue_growth_candidate(self) -> None:
        harness = self.harness(suffix="company-facts", company_facts=True)
        outcomes = harness.run_to_complete()
        self.assertEqual(
            [item["status"] for item in outcomes],
            ["admitted", "admitted", "admitted", "complete"],
        )

    def test_company_facts_run_registers_budget_v2_authority(self) -> None:
        # P9b: the packaged template is registry tag v2, so the budget-v2 profile
        # and its sibling price / rate-policy refs carry the template suffix.
        harness = self.harness(suffix="budget-v2", company_facts=True)
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[-1]["status"], "complete")
        rows = harness.connectors.connection.execute(
            "SELECT profile_version_id, version_number, record_json "
            "FROM connector_profile_versions "
            "WHERE connector_ref='connector:sec-edgar' ORDER BY version_number"
        ).fetchall()
        self.assertEqual([row["version_number"] for row in rows], [1, 2])
        self.assertEqual(
            rows[0]["profile_version_id"], "connector-profile-template:sec:0.1"
        )
        self.assertEqual(
            rows[1]["profile_version_id"],
            "connector-profile-template:sec:0.1:budget-v2:template-v2",
        )
        seeded = json.loads(rows[0]["record_json"])
        upgraded = json.loads(rows[1]["record_json"])
        self.assertEqual(seeded["max_response_bytes"], 5 * 1024 * 1024)
        self.assertEqual(upgraded["max_response_bytes"], 8 * 1024 * 1024)
        policy = harness.connectors.connection.execute(
            "SELECT policy_ref, record_json FROM connector_rate_policy_versions "
            "WHERE policy_version_id='connector-rate-policy:sec-public:budget-v2:template-v2:v1'"
        ).fetchone()
        self.assertIsNotNone(policy)
        self.assertEqual(policy["policy_ref"], "connector-rate-policy:sec-public:budget-v2:template-v2")
        self.assertEqual(
            json.loads(policy["record_json"])["limits"]["bytes"], 8 * 1024 * 1024
        )
        price = harness.connectors.connection.execute(
            "SELECT price_rate_ref, connector_profile_ref "
            "FROM connector_price_rate_versions "
            "WHERE price_rate_version_id="
            "'connector-price:sec-public:zero:budget-v2:template-v2:v1'"
        ).fetchone()
        self.assertIsNotNone(price)
        self.assertEqual(
            price["price_rate_ref"],
            "connector-price:sec-public:zero:budget-v2:template-v2",
        )
        self.assertEqual(
            price["connector_profile_ref"],
            "connector-profile-template:sec:0.1:budget-v2:template-v2",
        )
        # The invocation that actually ran must bind the budget-v2 profile.
        invocation = json.loads(
            harness.connectors.connection.execute(
                "SELECT record_json FROM connector_invocations"
            ).fetchone()[0]
        )
        self.assertEqual(
            invocation["connector_profile_ref"],
            "connector-profile-template:sec:0.1:budget-v2:template-v2",
        )

        claim = json.loads(
            harness.staging.connection.execute(
                "SELECT record_json FROM candidate_claim_versions"
            ).fetchone()[0]
        )
        self.assertEqual(claim["metric_or_aspect"], "quarterly_revenue_yoy_growth")
        self.assertEqual(claim["value"], "12.5")
        self.assertEqual(claim["unit"], "percent")
        self.assertIn("USD 112500000000", claim["normalized_statement"])
        self.assertIn("12.5% year over year", claim["normalized_statement"])
        material = json.loads(
            harness.staging.connection.execute(
                "SELECT record_json FROM candidate_source_materials"
            ).fetchone()[0]
        )
        self.assertEqual(
            material["normalized_payload"]["current"]["accession"],
            material["normalized_payload"]["prior"]["accession"],
        )
        self.assertEqual(material["normalized_payload"]["growth_percent"], "12.50")

    def test_connector_receipt_binds_transport_source_artifact_and_envelope(self) -> None:
        harness = self.harness(suffix="bindings")
        harness.run_to_complete()
        row = harness.connector_records.connection.execute(
            "SELECT receipt_json FROM research_completion_receipts"
        ).fetchone()[0]
        receipt = json.loads(row)
        envelope_row = harness.scheduler().connection.execute(
            "SELECT result_envelope_json FROM scheduler_result_envelopes "
            "WHERE work_order_id=?",
            (harness.work_orders[0]["id"],),
        ).fetchone()[0]
        envelope = json.loads(envelope_row)
        self.assertEqual(
            envelope["artifact_refs"],
            [receipt["artifacts"][0]["ref"]],
        )
        self.assertEqual(
            envelope["outputs"]["source_envelope_ref"],
            receipt["source_envelopes"][0]["ref"],
        )
        self.assertEqual(
            envelope["metadata"]["runner_request_ref"],
            receipt["actual_runner_request_ref"],
        )
        self.assertEqual(envelope["actual_side_effects"], ["read:public-http"])
        source = harness.connectors.connection.execute(
            "SELECT record_json FROM connector_source_envelopes"
        ).fetchone()[0]
        self.assertEqual(
            json.loads(source)["source_record_refs"],
            [
                "sec:filing:0000320193-26-000002",
                "sec:filing:0000320193-26-000003",
                "sec:filing:0000320193-26-000004",
            ],
        )

    def test_second_plan_reuses_versioned_sec_authority_without_fork(self) -> None:
        harness = self.harness(suffix="shared-profile-a")
        harness.run_to_complete()
        harness.clock.advance(60)
        created = harness.planner._create_plan(suffix="shared-profile-b")
        harness.planner._approve(created, suffix="shared-profile-b")
        harness.planner._start(created, suffix="shared-profile-b")
        second_ref = created["plan_version_ref"]
        statuses: list[str] = []
        while True:
            outcome = harness.executor.run_once(plan_version_ref=second_ref)
            statuses.append(outcome["status"])
            if outcome["status"] in {"complete", "blocked"}:
                break
        self.assertEqual(
            statuses, ["admitted", "admitted", "admitted", "complete"]
        )
        core = harness.connectors.connection
        self.assertEqual(
            core.execute(
                "SELECT COUNT(*) FROM connector_profile_versions"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            core.execute(
                "SELECT COUNT(*) FROM connector_price_rate_versions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            core.execute(
                "SELECT COUNT(*) FROM connector_rate_policy_versions"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            core.execute("SELECT COUNT(*) FROM connector_invocations").fetchone()[0],
            2,
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 2)

    def test_caller_fabricated_stage_payload_cannot_admit_downstream(self) -> None:
        harness = self.harness(suffix="fabricated")
        first = harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(first["status"], "admitted")
        # A caller claims the resolver WorkOrder and completes it with an
        # opaque success payload, exactly like the coordinator's adversarial
        # fixture.  The executor must refuse to admit the verifier.
        resolver_work = harness.work_orders[1]
        claim = harness.scheduler().claim("caller", work_order_id=resolver_work["id"])
        self.assertIsNotNone(claim)
        opaque = {
            "schema_version": "0.1",
            "id": "result-envelope:caller-fabricated",
            "created_at": "2026-08-15T08:00:01.000000+00:00",
            "work_order_ref": resolver_work["id"],
            "invocation_ref": "execution:caller-fabricated",
            "status": "succeeded",
            "outputs": {"success": True, "payload": "looks plausible"},
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "metadata": {},
        }
        harness.scheduler().complete(
            resolver_work["id"], 1, "caller", claim["lease_token"], opaque,
            idempotency_key="caller:complete:resolver",
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertIsNone(
            harness.scheduler().connection.execute(
                "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                (harness.work_orders[2]["id"],),
            ).fetchone()
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 0)

    def test_upstream_swap_between_plans_fails_closed(self) -> None:
        harness = self.harness(suffix="swap-a")
        harness.run_to_complete()
        other = PlanExecutorHarness(suffix="swap-b")
        self.addCleanup(other.close)
        # Run B's connector only; then fabricate B's resolver completion whose
        # stage proof binds A's connector envelope (upstream swap).
        first = other.executor.run_once(plan_version_ref=other.plan_wire["id"])
        self.assertEqual(first["status"], "admitted")
        a_connector_formal = harness.scheduler().formal_result(
            harness.work_orders[0]["id"]
        )
        b_resolver_work = other.work_orders[1]
        claim = other.scheduler().claim("caller", work_order_id=b_resolver_work["id"])
        self.assertIsNotNone(claim)
        b_steps = other.plan_wire["execution_scope"]["steps"]
        proof = {
            "schema_version": "0.1",
            "id": _stage_output_ref(
                plan_version_ref=other.plan_wire["id"],
                step_ref=b_steps[1]["id"],
                upstream_result_ref=a_connector_formal["result_envelope_id"],
                upstream_result_hash=a_connector_formal["result_envelope_hash"],
            ),
            "created_at": "2026-08-15T08:00:01.000000+00:00",
            "plan_version_ref": other.plan_wire["id"],
            "plan_version_hash": other.plan_wire["content_hash"],
            "step_ref": b_steps[1]["id"],
            "step_hash": b_steps[1]["content_hash"],
            "stage": "authority_resolver",
            "operation": "resolve_connector_authority",
            "output_contract_ref": b_steps[1]["output_contract_ref"],
            "upstream_work_order_ref": other.work_orders[0]["id"],
            "upstream_result_ref": a_connector_formal["result_envelope_id"],
            "upstream_result_hash": a_connector_formal["result_envelope_hash"],
            "records": [{
                "kind": "authority_resolution",
                "ref": "authority-resolution:swapped",
                "hash": "0" * 64,
            }],
        }
        proof["content_hash"] = content_hash(proof)
        swapped = {
            "schema_version": "0.1",
            "id": "result-envelope:swap-b-resolver",
            "created_at": "2026-08-15T08:00:01.000000+00:00",
            "work_order_ref": b_resolver_work["id"],
            "invocation_ref": "execution:swap-b-resolver",
            "status": "succeeded",
            "outputs": proof,
            "actual_side_effects": [],
            "usage_refs": [],
            "artifact_refs": [],
            "metadata": {},
        }
        other.scheduler().complete(
            b_resolver_work["id"], 1, "caller", claim["lease_token"], swapped,
            idempotency_key="caller:complete:swap-b",
        )
        with self.assertRaises(ResearchPlanCoordinatorConflict):
            other.executor.run_once(plan_version_ref=other.plan_wire["id"])
        self.assertIsNone(
            other.scheduler().connection.execute(
                "SELECT 1 FROM scheduler_work_orders WHERE work_order_id=?",
                (other.work_orders[2]["id"],),
            ).fetchone()
        )

    def test_typed_record_tamper_fails_closed(self) -> None:
        harness = self.harness(suffix="tamper")
        harness.run_to_complete()
        # Tamper the persisted ResearchCheckpoint row: the verifier re-reads
        # the typed record through its validator and must fail closed.
        harness.connector_records.connection.execute(
            "DROP TRIGGER research_checkpoints_no_update"
        )
        harness.connector_records.connection.execute(
            "UPDATE research_checkpoints SET content_hash=?",
            ("0" * 64,),
        )
        # Re-running is blocked at the checkpoint re-read, before any write.
        with self.assertRaises(ResearchPlanExecutorConflict):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])

    def test_receipt_tamper_fails_closed_on_replay(self) -> None:
        harness = self.harness(suffix="receipt-tamper")
        harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_completion_receipts"
            ).fetchone()[0],
            1,
        )
        harness.connector_records.connection.execute(
            "DROP TRIGGER research_completion_receipts_no_update"
        )
        harness.connector_records.connection.execute(
            "UPDATE research_completion_receipts SET content_hash=?",
            ("0" * 64,),
        )
        # The executor rebuilds the deterministic receipt and the idempotent
        # store detects the drift instead of forking a second receipt.
        with self.assertRaises(Exception) as raised:
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertIn("Conflict", type(raised.exception).__name__)
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_completion_receipts"
            ).fetchone()[0],
            1,
        )

    def test_crash_between_receipt_store_and_admission_replays_to_one_receipt(self) -> None:
        harness = self.harness(suffix="crash-receipt", fault_at="after_receipt_store")
        with self.assertRaises(InjectedExecutorCrash):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_completion_receipts"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )
        # Replay converges: no second transport run, no second receipt.
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[0]["status"], "admitted")
        self.assertEqual(outcomes[-1]["status"], "complete")
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_completion_receipts"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            harness.connectors.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)

    def test_crash_after_checkpoint_store_replays_to_one_checkpoint(self) -> None:
        harness = self.harness(suffix="crash-checkpoint", fault_at="after_checkpoint_store")
        harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        with self.assertRaises(InjectedExecutorCrash):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_checkpoints"
            ).fetchone()[0],
            1,
        )
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[-1]["status"], "complete")
        self.assertEqual(
            harness.connector_records.connection.execute(
                "SELECT COUNT(*) FROM research_checkpoints"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)

    def test_crash_after_internal_complete_replays_to_one_result(self) -> None:
        harness = self.harness(suffix="crash-complete", fault_at="after_internal_complete")
        harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        with self.assertRaises(InjectedExecutorCrash):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_formal_results"
            ).fetchone()[0],
            2,
        )
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[0]["status"], "admitted")
        self.assertEqual(outcomes[-1]["status"], "complete")
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_formal_results"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)

    def test_crash_after_staging_commit_replays_to_one_candidate(self) -> None:
        harness = self.harness(suffix="crash-staging", fault_at="after_staging_commit")
        for _ in range(3):
            outcome = harness.executor.run_once(
                plan_version_ref=harness.plan_wire["id"]
            )
            self.assertEqual(outcome["status"], "admitted")
        with self.assertRaises(InjectedExecutorCrash):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_formal_results"
            ).fetchone()[0],
            3,
        )
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[-1]["status"], "complete")
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_formal_results"
            ).fetchone()[0],
            4,
        )

    def test_crash_before_transport_returns_pending_then_converges_after_lease(self) -> None:
        harness = self.harness(suffix="crash-transport", fault_at="before_transport")
        with self.assertRaises(InjectedExecutorCrash):
            harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        # The lease is still held by the crashed run; the executor must not
        # fork a second attempt while the first is outstanding.
        pending = harness.executor.run_once(plan_version_ref=harness.plan_wire["id"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["reason"], "not_claimable")
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )
        # After lease expiry the scheduler re-arms the node and a fresh
        # attempt converges on one task tree.
        harness.clock.advance(60)
        outcomes = harness.run_to_complete()
        self.assertEqual(outcomes[0]["status"], "admitted")
        self.assertEqual(outcomes[-1]["status"], "complete")
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            harness.connectors.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(harness.staging_counts()["candidate_claim_versions"], 1)

    def test_unapproved_or_unstarted_plan_is_rejected(self) -> None:
        harness = self.harness(suffix="gated")
        created = harness.planner._create_plan(suffix="gated-extra")
        harness.planner._approve(created, suffix="gated-extra")
        with self.assertRaises(Exception) as raised:
            harness.executor.run_once(plan_version_ref=created["plan_version_ref"])
        self.assertIn("Not", type(raised.exception).__name__)
        # An approved-but-not-started plan is also not executable.
        pending = harness.planner._create_plan(suffix="pending-extra")
        harness.planner._approve(pending, suffix="pending-extra")
        with self.assertRaises(Exception) as raised:
            harness.executor.run_once(plan_version_ref=pending["plan_version_ref"])
        self.assertIn("Not", type(raised.exception).__name__)

    def test_wrong_plan_ref_fails_closed_before_any_write(self) -> None:
        harness = self.harness(suffix="wrong-ref")
        with self.assertRaises(Exception) as raised:
            harness.executor.run_once(plan_version_ref="research-plan:does-not-exist")
        self.assertIn("Not", type(raised.exception).__name__)
        self.assertEqual(
            harness.scheduler().connection.execute(
                "SELECT COUNT(*) FROM scheduler_work_orders"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
