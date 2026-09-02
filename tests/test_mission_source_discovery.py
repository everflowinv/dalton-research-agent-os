"""P9d-1: mission source discovery -- plan, authority ledger, coordinator, child."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dalton_core.alphaengine_core_search import (
    AlphaEngineCoreSearch,
    FakeSearchHandle,
    SearchConnectorGovernance,
    alphaengine_documents_in_authority,
    build_search_governance_record,
    search_spec_hash,
    validate_search_spec,
)
from dalton_core.alphaengine_core_acquisition import (
    AlphaEngineCoreAcquisition,
    StaticConnectorGovernance,
    build_governance_record,
)
from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector import ConnectorStore
from dalton_core.coverage_mission import (
    AUTOMATION_WRITE_SCOPES,
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionValidationError,
    validate_mission_source_discovery,
)
from dalton_core.mission_source_discovery import (
    AlphaEngineSearchLauncher,
    DiscoveryLaunchRejected,
    DiscoveryPlanError,
    MissionSourceDiscoveryCoordinator,
    alphaengine_calls_remaining,
    build_discovery_parameters,
    build_discovery_plan,
    load_discovery_plan,
    validate_discovery_plan,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.runner_journal import RunnerJournal
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, canonical_json
from tests.p9a_fixtures import ROOT, bootstrap_method_authorities, mission_params
from tests.test_alphaengine_core_acquisition import FakeDocumentHandle


ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:coverage-owner"
NOW = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)
KNOWN_DOC = "alphaengine-doc:130000095976806"
NEW_DOC = "alphaengine-doc:130000099999999"
PLAN_PATH = ROOT / "deploy/phase9/p9d-us-it-services-discovery-plan-v1.json"


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def approved_governance() -> SearchConnectorGovernance:
    return SearchConnectorGovernance(
        build_search_governance_record(approved_by="human:lumos", status="approved")
    )


def plan_for_tests() -> dict:
    return build_discovery_plan(
        plan_id="discovery-plan:us-it-services:alphaengine:test",
        created_at=NOW.isoformat(timespec="microseconds"),
        mission_ref="coverage-mission:us-it-services",
        companies={ACN: "Accenture ACN", CTSH: "Cognizant CTSH"},
        specs=[{
            "spec_ref": "earnings-call-transcripts", "document_type": "meeting_minutes",
            "query_template": "{terms} earnings call transcript", "lookback_days": 400,
            "rediscovery_interval_days": 7, "retry_interval_days": 1,
        }],
    )


def seed_known_document(harness: "SearchHarness", document_ref: str = KNOWN_DOC) -> None:
    """Acquire the document for real (fake page handle) so Core holds a successful page.

    ``document_in_authority`` requires a complete/partial SourceEnvelope, so a
    bare call spec would not do; the in-process acquisition uses its own
    capability catalog, exactly as the live launcher does.
    """

    harness.document_handle.text = f"Rehearsal transcript for {document_ref}. " * 30
    harness.acquisition.acquire(harness.acquisition.build_plan(document_ref))


class SearchHarness:
    """Core + governed search in one temp dir (search runs in-process)."""

    def __init__(self, root: Path, results: list[dict], *, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.core = DaltonStore(str(root / "core.sqlite"))
        self.connectors = ConnectorStore(self.core, clock=self.clock)
        self.observability = ObservabilityStore(self.core)
        self.journal = RunnerJournal(self.core, clock=self.clock)
        self.scheduler = Scheduler(
            str(root / "scheduler.sqlite"), clock=self.clock,
            default_lease_seconds=30, max_lease_seconds=60,
        )
        governance = approved_governance()
        self.catalog = CapabilityCatalog(
            str(root / "catalog.sqlite"), clock=self.clock,
            approval_resolver=governance.approval, policy_resolver=governance.policy,
        )
        self.spool = RawSpool(str(root / "spool"), max_total_bytes=50_000_000)
        self.handle = FakeSearchHandle(results)
        self.search = AlphaEngineCoreSearch(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.catalog,
            spool=self.spool, governance=governance, mcp_handle=self.handle, clock=self.clock,
        )
        # get_document has its own governance and its own catalog file (one
        # catalog per capability, as the launchers do on live).
        acquisition_governance = StaticConnectorGovernance(
            build_governance_record(approved_by="human:lumos", status="approved")
        )
        self.acquisition_catalog = CapabilityCatalog(
            str(root / "catalog-get-document.sqlite"), clock=self.clock,
            approval_resolver=acquisition_governance.approval,
            policy_resolver=acquisition_governance.policy,
        )
        self.document_handle = FakeDocumentHandle("placeholder", page_chars=30_000)
        self.acquisition = AlphaEngineCoreAcquisition(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.acquisition_catalog,
            spool=self.spool, governance=acquisition_governance,
            mcp_handle=self.document_handle, clock=self.clock,
        )

    def close(self) -> None:
        self.acquisition_catalog.close()
        self.catalog.close()
        self.scheduler.close()
        self.core.close()


class DiscoveryPlanTests(unittest.TestCase):
    def test_committed_plan_loads_and_binds_hash(self) -> None:
        plan = load_discovery_plan(PLAN_PATH)
        self.assertEqual(plan["mission_ref"], "coverage-mission:us-it-services")
        self.assertEqual(len(plan["companies"]), 5)
        self.assertEqual([spec["spec_ref"] for spec in plan["specs"]],
                         ["earnings-call-transcripts", "sell-side-reports"])
        tampered = json.loads(PLAN_PATH.read_text())
        tampered["companies"][ACN]["search_terms"] = "Something Else"
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(tampered)
        params = build_discovery_parameters(
            plan, spec_ref="sell-side-reports", company_ref=ACN, as_of=date(2026, 9, 2),
        )
        self.assertEqual(params["query"], "Accenture ACN research report")
        self.assertEqual(params["filters"], {
            "document_type": "sell_side_report", "date_from": "2026-03-06", "date_to": "2026-09-02",
        })
        self.assertEqual(search_spec_hash(params), search_spec_hash(validate_search_spec(params)))

    def test_plan_rejects_bad_specs(self) -> None:
        for bad in (
            {"spec_ref": "Bad Ref", "document_type": "meeting_minutes", "query_template": "{terms}",
             "lookback_days": 1, "rediscovery_interval_days": 1, "retry_interval_days": 1},
            {"spec_ref": "ok", "document_type": "podcast", "query_template": "{terms}",
             "lookback_days": 1, "rediscovery_interval_days": 1, "retry_interval_days": 1},
            {"spec_ref": "ok", "document_type": "meeting_minutes", "query_template": "no placeholder",
             "lookback_days": 1, "rediscovery_interval_days": 1, "retry_interval_days": 1},
            {"spec_ref": "ok", "document_type": "meeting_minutes", "query_template": "{terms}",
             "lookback_days": 0, "rediscovery_interval_days": 1, "retry_interval_days": 1},
        ):
            with self.assertRaises(DiscoveryPlanError):
                build_discovery_plan(
                    plan_id="discovery-plan:x",
                    created_at=NOW.isoformat(timespec="microseconds"),
                    mission_ref="coverage-mission:x",
                    companies={ACN: "Accenture"}, specs=[bad],
                )
        with self.assertRaises(DiscoveryPlanError):
            build_discovery_parameters(plan_for_tests(), spec_ref="earnings-call-transcripts",
                                       company_ref="company:unknown", as_of=date(2026, 9, 2))


class MissionDiscoveryAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.h = SearchHarness(root, [{"doc_id": KNOWN_DOC.split(":")[1]}, {"doc_id": NEW_DOC.split(":")[1]}])
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        self.plan = plan_for_tests()

    def create_mission(self, **overrides):
        params = mission_params(self.state)
        params.update(overrides)
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def mission_v2(self, v1):
        params = mission_params(self.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({
            "version_id": "coverage-mission-version:us-it-services:2",
            "prior_version_ref": v1["id"],
            "idempotency_key": "coverage-mission:us-it-services:2",
        })
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def run_search(self):
        params = build_discovery_parameters(
            self.plan, spec_ref="earnings-call-transcripts", company_ref=ACN, as_of=NOW.date(),
        )
        receipt = self.h.search.search(self.h.search.build_request(params))
        return params, receipt

    def test_vocabulary_and_authorization_rules(self) -> None:
        self.assertIn("source_discovery", AUTOMATION_WRITE_SCOPES)
        v1 = self.create_mission()
        # v1 marks AlphaEngine probe_only and lacks the scope: automation is refused,
        # a human requester may still rehearse the search under the mission.
        with self.assertRaises(CoverageMissionConflict) as ctx:
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref="source:alphaengine", requested_by=AUTOMATION,
            )
        self.assertIn("probe_only", str(ctx.exception))
        human = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref="source:alphaengine", requested_by=OWNER,
        )
        self.assertEqual(human["actor_ref"], AUTOMATION)
        self.assertEqual(human["requested_by"], OWNER)
        self.assertEqual(human["ticker"], "ACN")
        self.assertEqual(human["max_alphaengine_calls_24h"], 30)
        # Sources the mission marks not_connected are refused even for humans.
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref="source:guidepoint", requested_by=OWNER,
            )
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref="company:unknown", source_ref="source:alphaengine", requested_by=OWNER,
            )
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref="source:alphaengine", requested_by="automation:other",
            )
        v2 = self.mission_v2(v1)
        automation = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref="source:alphaengine", requested_by=AUTOMATION,
        )
        self.assertEqual(automation["mission_version_ref"], v2["id"])
        self.assertEqual(automation["requested_by"], AUTOMATION)
        # Exact binding to a superseded version is refused.
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref="source:alphaengine", requested_by=AUTOMATION,
                mission_version_ref=v1["id"], mission_version_hash=v1["content_hash"],
            )

    def test_discovery_record_binds_search_authority_and_partitions_documents(self) -> None:
        v1 = self.create_mission()
        self.mission_v2(v1)
        seed_known_document(self.h)
        authorization = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref="source:alphaengine", requested_by=AUTOMATION,
        )
        params, receipt = self.run_search()
        self.assertEqual(receipt["document_refs"], [KNOWN_DOC, NEW_DOC])
        present = alphaengine_documents_in_authority(self.h.core.connection, receipt["document_refs"])
        self.assertEqual(present, [KNOWN_DOC])
        common = dict(
            authorization=authorization, discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"], spec_ref="earnings-call-transcripts",
            query_hash=search_spec_hash(params), parameters=params,
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
        )
        record = self.missions.record_source_discovery(
            **common, document_refs=receipt["document_refs"], in_authority_document_refs=present,
        )
        self.assertEqual(record["status"], "fresh")
        self.assertEqual(record["new_document_refs"], [NEW_DOC])
        self.assertEqual(record["in_authority_document_refs"], [KNOWN_DOC])
        self.assertEqual(record["actor_ref"], AUTOMATION)
        validate_mission_source_discovery({k: v for k, v in record.items() if k != "status"})
        schema = json.loads((ROOT / "contracts/coverage-mission-source-discovery.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(record) - {"status"})
        replay = self.missions.record_source_discovery(
            **common, document_refs=receipt["document_refs"], in_authority_document_refs=present,
        )
        self.assertEqual(replay["status"], "duplicate")
        # Document refs that differ from the envelope, or a wrong envelope hash, fail closed.
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_source_discovery(
                **common, document_refs=[NEW_DOC], in_authority_document_refs=[],
            )
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_source_discovery(
                **{**common, "source_envelope_hash": "0" * 64},
                document_refs=receipt["document_refs"], in_authority_document_refs=present,
            )
        with self.assertRaises(CoverageMissionValidationError):
            self.missions.record_source_discovery(
                **common, document_refs=receipt["document_refs"],
                in_authority_document_refs=["alphaengine-doc:1"],
            )
        documents = self.missions.discovered_documents(record["mission_version_ref"], company_ref=ACN)
        self.assertEqual(
            sorted((item["document_ref"], item["status"]) for item in documents),
            [(KNOWN_DOC, "already_in_authority"), (NEW_DOC, "discovered")],
        )
        self.assertEqual(self.missions.next_discovered_document()["document_ref"], NEW_DOC)
        listed = self.missions.source_discoveries(record["mission_version_ref"], company_ref=ACN)
        self.assertEqual([item["id"] for item in listed], [record["id"]])
        progress = self.missions.mission_progress("coverage-mission:us-it-services")
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual((acn["discovery_count"], acn["discovered_document_count"], acn["acquired_document_count"]), (1, 2, 1))

        # Discovered document lifecycle: launched -> acquired; conflicting transitions refused.
        launched = self.missions.mark_discovered_document_launched(
            self.missions.next_discovered_document()["record_id"], "alphaengine-acquisition:abc",
        )
        self.assertEqual(launched["status"], "acquisition_launched")
        self.assertIsNone(self.missions.next_discovered_document())
        with self.assertRaises(CoverageMissionConflict):
            self.missions.mark_discovered_document_launched(launched["record_id"], "alphaengine-acquisition:other")
        acquired = self.missions.settle_discovered_document(launched["record_id"], status="acquired")
        self.assertEqual(acquired["status"], "acquired")
        with self.assertRaises(CoverageMissionConflict):
            self.missions.settle_discovered_document(launched["record_id"], status="acquisition_failed", reason="x")
        self.assertEqual(self.missions.settle_discovered_document(launched["record_id"], status="acquired")["status"], "acquired")

    def test_dispatch_ledger_and_direct_writes(self) -> None:
        v1 = self.create_mission()
        self.mission_v2(v1)
        authorization = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref="source:alphaengine", requested_by=AUTOMATION,
        )
        dispatch = self.missions.record_discovery_dispatch(
            authorization=authorization, discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"], spec_ref="earnings-call-transcripts",
            query_hash="a" * 64, ticket_ref="alphaengine-discovery:0123456789abcdef01234567",
        )
        self.assertEqual(dispatch["status"], "launched")
        self.assertEqual([item["dispatch_id"] for item in self.missions.open_discovery_dispatches()], [dispatch["dispatch_id"]])
        with self.assertRaises(CoverageMissionConflict):
            self.missions.record_discovery_dispatch(
                authorization={**authorization, "ticker": "XXX"}, discovery_plan_ref=self.plan["id"],
                discovery_plan_hash=self.plan["content_hash"], spec_ref="earnings-call-transcripts",
                query_hash="a" * 64, ticket_ref="alphaengine-discovery:0123456789abcdef01234568",
            )
        settled = self.missions.settle_discovery_dispatch(dispatch["dispatch_id"], status="failed", reason="child died")
        self.assertEqual((settled["status"], settled["failure_reason"]), ("failed", "child died"))
        self.assertEqual(self.missions.open_discovery_dispatches(), [])
        with self.assertRaises(CoverageMissionConflict):
            self.missions.settle_discovery_dispatch(dispatch["dispatch_id"], status="succeeded")
        with self.assertRaises(CoverageMissionValidationError):
            self.missions.settle_discovery_dispatch(dispatch["dispatch_id"], status="failed")
        conn = self.h.core.connection
        for statement in (
            "DELETE FROM coverage_mission_discovery_dispatches",
            "UPDATE coverage_mission_discovery_dispatches SET status='succeeded'",
            "INSERT INTO coverage_mission_source_discoveries(record_id,mission_version_ref,"
            "mission_version_hash,company_ref,source_ref,discovery_plan_ref,discovery_plan_hash,"
            "spec_ref,query_hash,connector_invocation_ref,source_envelope_ref,source_envelope_hash,"
            "actor_ref,requested_by,record_json,content_hash,created_at) VALUES('x','" + v1["id"] +
            "','h','c','s','p','ph','sp','q','i','e','eh','automation:x','human:y','{}','ch','t')",
            "INSERT INTO coverage_mission_discovered_documents(record_id,mission_version_ref,"
            "company_ref,source_ref,document_ref,discovery_ref,status,created_at,updated_at) "
            "VALUES('d','" + v1["id"] + "','c','s','doc','x','discovered','t','t')",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(statement)


class FakeAcquisitionLauncher:
    def __init__(self, harness: SearchHarness, *, outcome: str = "succeeded") -> None:
        self.h = harness
        self.outcome = outcome
        self.calls: list[dict] = []
        self.tickets: dict[str, dict] = {}

    def start_bounded_probe(self, *, document_ref, caller_ref, max_pages=20):
        self.calls.append({"document_ref": document_ref, "caller_ref": caller_ref})
        ticket = f"alphaengine-acquisition:{len(self.calls):024x}"
        self.tickets[ticket] = {"id": ticket, "status": "running", "document_ref": document_ref}
        return dict(self.tickets[ticket])

    def finish(self) -> None:
        for ticket in self.tickets.values():
            if ticket["status"] == "running":
                ticket["status"] = self.outcome
                if self.outcome == "succeeded":
                    seed_known_document(self.h, ticket["document_ref"])

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


class FakeSearchLauncher:
    """Stands in for the child: runs the governed search in-process on start()."""

    def __init__(self, harness: SearchHarness, missions: CoverageMissionAuthority, plan: dict) -> None:
        self.h = harness
        self.missions = missions
        self.plan = plan
        self.tickets: dict[str, dict] = {}
        self.starts: list[dict] = []
        self.fail_next = False

    def running(self) -> bool:
        return any(ticket["status"] == "running" for ticket in self.tickets.values())

    def start(self, *, authorization, spec_ref, as_of=None):
        self.starts.append({"authorization": dict(authorization), "spec_ref": spec_ref})
        ticket_id = f"alphaengine-discovery:{len(self.starts):024x}"
        params = build_discovery_parameters(
            self.plan, spec_ref=spec_ref, company_ref=authorization["company_ref"], as_of=as_of,
        )
        summary = {"discovery_ref": None, "new_document_count": 0, "failure_reason": None}
        status = "succeeded"
        if self.fail_next:
            self.fail_next = False
            status = "failed"
            summary["failure_reason"] = "rehearsed failure"
        else:
            receipt = self.h.search.search(self.h.search.build_request(params))
            present = alphaengine_documents_in_authority(self.h.core.connection, receipt["document_refs"])
            record = self.missions.record_source_discovery(
                authorization=authorization, discovery_plan_ref=self.plan["id"],
                discovery_plan_hash=self.plan["content_hash"], spec_ref=spec_ref,
                query_hash=search_spec_hash(params), parameters=params,
                connector_invocation_ref=receipt["connector_invocation_ref"],
                connector_invocation_hash=receipt["connector_invocation_hash"],
                source_envelope_ref=receipt["source_envelope_ref"],
                source_envelope_hash=receipt["source_envelope_hash"],
                document_refs=receipt["document_refs"], in_authority_document_refs=present,
            )
            summary.update({"discovery_ref": record["id"], "new_document_count": len(record["new_document_refs"])})
        self.tickets[ticket_id] = {"id": ticket_id, "status": status, "exit_code": 0 if status == "succeeded" else 1, "summary": summary}
        return {"id": ticket_id, "status": "running"}

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.clock = Clock()
        self.h = SearchHarness(root, [{"doc_id": KNOWN_DOC.split(":")[1]}, {"doc_id": NEW_DOC.split(":")[1]}], clock=self.clock)
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        self.plan = plan_for_tests()
        self.search_launcher = FakeSearchLauncher(self.h, self.missions, self.plan)
        self.acquisition_launcher = FakeAcquisitionLauncher(self.h)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.search_launcher, acquisition_launcher=self.acquisition_launcher,
            clock=self.clock,
        )

    def create_mission(self):
        params = mission_params(self.state)
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def mission_v2(self, v1, *, cap: int = 30):
        params = mission_params(self.state)
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params["budget"]["max_alphaengine_calls_24h"] = cap
        params.update({
            "version_id": "coverage-mission-version:us-it-services:2",
            "prior_version_ref": v1["id"],
            "idempotency_key": "coverage-mission:us-it-services:2",
        })
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def test_no_mission_then_v1_skip_then_v2_full_cycle(self) -> None:
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "no_active_mission")
        self.assertEqual(tick["acquisition"]["status"], "idle")
        v1 = self.create_mission()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "not_authorized")
        self.assertIn("probe_only", tick["discovery"]["reason"])
        self.assertEqual(self.search_launcher.starts, [])
        self.assertEqual(self.h.handle.calls, [])

        self.mission_v2(v1)
        seed_known_document(self.h)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["status"], "launched")
        self.assertEqual(tick["discovery"]["status"], "launched")
        self.assertEqual(tick["discovery"]["company_ref"], ACN)
        # The seeded (real, fake-handle) acquisition of the known document is
        # one AlphaEngine call inside the trailing window.
        self.assertEqual(tick["discovery"]["budget"], {"spent": 1, "cap": 30, "remaining": 29, "reserved": 0})
        self.assertEqual(self.search_launcher.starts[0]["authorization"]["requested_by"], AUTOMATION)
        # Acquisition runs before discovery in a tick, so the document the
        # (fake, synchronous) child just discovered is queued on the next tick.
        self.assertEqual(tick["acquisition"]["status"], "idle")

        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_dispatches"]], ["succeeded"])
        self.assertEqual(tick["settled_dispatches"][0]["new_document_count"], 1)
        self.assertEqual(tick["acquisition"]["status"], "launched")
        self.assertEqual(tick["acquisition"]["document_ref"], NEW_DOC)
        self.assertEqual(self.acquisition_launcher.calls[0]["caller_ref"], AUTOMATION)
        # Second company launches while ACN waits out its cadence.
        self.assertEqual(tick["discovery"]["status"], "launched")
        self.assertEqual(tick["discovery"]["company_ref"], CTSH)
        self.assertTrue(any("rediscovered" in item["reason"] for item in tick["discovery"]["skipped"]))

        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "busy")
        self.acquisition_launcher.finish()
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquired"])
        self.assertEqual(tick["discovery"]["status"], "idle")
        self.assertEqual(tick["acquisition"]["status"], "idle")
        # CTSH's search returned the same two docs; a document is one row per
        # mission version, so nothing was queued twice.
        documents = self.missions.discovered_documents(
            self.missions.active_mission("coverage-mission:us-it-services")["id"]
        )
        self.assertEqual(
            sorted((item["company_ref"], item["document_ref"], item["status"]) for item in documents),
            sorted([
                (ACN, KNOWN_DOC, "already_in_authority"), (ACN, NEW_DOC, "acquired"),
            ]),
        )
        self.assertEqual(len(self.h.handle.calls), 2)
        # Eight days later the cadence window has passed and ACN is searched again.
        self.clock.advance(days=8)
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["discovery"]["status"], tick["discovery"]["company_ref"]), ("launched", ACN))

    def test_failed_child_settles_and_waits_for_retry_interval(self) -> None:
        v1 = self.create_mission()
        self.mission_v2(v1)
        self.search_launcher.fail_next = True
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "launched")
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_dispatches"][0]["status"], "failed")
        self.assertEqual(tick["discovery"]["company_ref"], CTSH)
        self.assertTrue(any("retry interval" in item["reason"] for item in tick["discovery"]["skipped"]))
        self.clock.advance(days=2)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["company_ref"], ACN)

    def test_budget_gate_counts_search_and_document_calls(self) -> None:
        v1 = self.create_mission()
        self.mission_v2(v1, cap=1)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "launched")
        self.assertEqual(
            alphaengine_calls_remaining(self.h.core.connection, mission_cap=1, as_of=self.clock()),
            {"spent": 1, "cap": 1, "remaining": 0},
        )
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "budget_exhausted")
        self.assertEqual(tick["discovery"]["status"], "budget_exhausted")
        self.assertEqual(self.acquisition_launcher.calls, [])
        self.clock.advance(hours=25)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "launched")
        self.assertEqual(tick["discovery"]["status"], "budget_exhausted")

    def test_failed_acquisition_is_recorded_not_retried_silently(self) -> None:
        v1 = self.create_mission()
        self.mission_v2(v1)
        seed_known_document(self.h)
        self.acquisition_launcher.outcome = "failed"
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "launched")
        self.acquisition_launcher.finish()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_documents"][0]["status"], "acquisition_failed")
        self.assertEqual(tick["acquisition"]["status"], "idle")
        failed = self.missions.discovered_documents(
            self.missions.active_mission("coverage-mission:us-it-services")["id"],
            status="acquisition_failed",
        )
        self.assertEqual([item["document_ref"] for item in failed], [NEW_DOC])


class SearchChildTests(unittest.TestCase):
    """The real launcher spawns the real child in fake-search mode."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        core = DaltonStore(str(self.state / "core.sqlite"))
        self.method = bootstrap_method_authorities(core)
        missions = CoverageMissionAuthority(core)
        params = mission_params(self.method)
        ref = params.pop("mission_ref")
        self.mission = missions.create_mission(ref, **params)
        core.close()
        self.plan = plan_for_tests()
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.governance_path = self.root / "search-governance.json"
        self.governance_path.write_text(
            canonical_json(build_search_governance_record(approved_by="human:lumos", status="approved")) + "\n",
            encoding="utf-8",
        )
        self.results_path = self.root / "results.json"
        self.results_path.write_text(json.dumps([{"doc_id": NEW_DOC.split(":")[1], "title": "ACN call"}]))

    def test_launcher_refuses_before_spawning(self) -> None:
        proposed = self.root / "proposed.json"
        proposed.write_text(canonical_json(build_search_governance_record(approved_by="human:lumos")) + "\n")
        launcher = AlphaEngineSearchLauncher(
            state_dir=self.state, governance_path=proposed, plan_path=self.plan_path,
            mode_args=("--fake-search-file", str(self.results_path)),
        )
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            authorization = CoverageMissionAuthority(core).authorize_source_discovery(
                company_ref=ACN, source_ref="source:alphaengine", requested_by=OWNER,
            )
        finally:
            core.close()
        with self.assertRaises(DiscoveryLaunchRejected):
            launcher.start(authorization=authorization, spec_ref="earnings-call-transcripts")
        launcher = AlphaEngineSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-search-file", str(self.results_path)),
        )
        with self.assertRaises(DiscoveryLaunchRejected):
            launcher.start(authorization={**authorization, "scope": "other"}, spec_ref="earnings-call-transcripts")
        with self.assertRaises(DiscoveryLaunchRejected):
            launcher.start(authorization=authorization, spec_ref="missing-spec")
        self.assertFalse((self.state / "discoveries").exists() and any((self.state / "discoveries").iterdir()))

    def test_child_records_discovery_under_human_request(self) -> None:
        launcher = AlphaEngineSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-search-file", str(self.results_path)),
        )
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            authorization = CoverageMissionAuthority(core).authorize_source_discovery(
                company_ref=ACN, source_ref="source:alphaengine", requested_by=OWNER,
            )
        finally:
            core.close()
        ticket = launcher.start(authorization=authorization, spec_ref="earnings-call-transcripts", as_of=date(2026, 9, 2))
        self.assertEqual(ticket["status"], "running")
        self.assertEqual(ticket["requested_by"], OWNER)
        code = launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual((code, status["status"]), (0, "succeeded"), status)
        summary = status["summary"]
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["new_document_count"], 1)
        self.assertEqual(summary["provider_calls"], 1)
        self.assertEqual(summary["transport"], "fake")
        self.assertEqual(summary["formal_authority_writes"], 0)
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            missions = CoverageMissionAuthority(core)
            records = missions.source_discoveries(self.mission["id"], company_ref=ACN)
            self.assertEqual([item["id"] for item in records], [summary["discovery_ref"]])
            self.assertEqual(records[0]["requested_by"], OWNER)
            self.assertEqual(records[0]["new_document_refs"], [NEW_DOC])
            self.assertEqual(core.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?",
                ("connector-profile:alphaengine-search-library:v1",),
            ).fetchone()[0], 1)
            self.assertEqual(core.connection.execute("SELECT COUNT(*) FROM evidence_versions").fetchone()[0], 0)
            self.assertEqual(core.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0], 0)
        finally:
            core.close()
        ticket_path = self.state / "discoveries" / ticket["id"].split(":")[1] / "ticket.json"
        self.assertEqual(ticket_path.stat().st_mode & 0o777, 0o600)

    def test_child_refuses_automation_under_probe_only_mission(self) -> None:
        launcher = AlphaEngineSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-search-file", str(self.results_path)),
        )
        forged = {
            "mission_version_ref": self.mission["id"], "mission_version_hash": self.mission["content_hash"],
            "mission_ref": self.mission["mission_ref"], "company_ref": ACN, "ticker": "ACN",
            "source_ref": "source:alphaengine", "actor_ref": AUTOMATION, "requested_by": AUTOMATION,
            "scope": "source_discovery", "max_alphaengine_calls_24h": 30,
        }
        ticket = launcher.start(authorization=forged, spec_ref="earnings-call-transcripts")
        launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("probe_only", status["summary"]["failure_reason"])
        self.assertEqual(status["summary"]["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()
