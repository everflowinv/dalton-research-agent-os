"""Canary: a workspace created by the formal flow runs every lane, and keeps running.

The legacy Core reached a working governance and lane setup over eighteen
policy versions of hand edits; ws-7d, created through the setup flow, met the
missing pieces one refusal at a time.  This test is the rehearsal that would
have caught them: it creates a workspace exactly as the cockpit does, for a
mission that has nothing to do with US IT services (two fictional robotics
issuers, ``company:ticker:`` refs), and without signing anything by hand runs

    discovery -> extraction -> claim-support check -> ledger commit
    -> SEC company facts (10-Q, the 10-K pair, and FY - 9M for a 10-K of
       fiscal-year totals) -> claim index -> dossier -> event judgement,

then upgrades the mission through the real policy -> constitution -> mission
cascade and runs them again.  After the upgrade nothing may stall (an open
review left under the old version must reach the extraction queue) and nothing
may be read twice -- including when a search under the intermediate version
found the open document again and a second version was published over it
before any carry ran (the Guidepoint re-search shape, 2026-09-28).

Hermetic: every model is a fake, every connector a fake handle or a recorded
body, the SEC ticker resolver a fixture, and opening a socket fails the test.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.store import DaltonStore, canonical_json, content_hash

OWNER = "human:canary-owner"
AUTOMATION = "automation:coverage-mission"
WDGT = "company:ticker:wdgt"
GZMO = "company:ticker:gzmo"
ISSUERS = {
    "WDGT": {"ticker": "WDGT", "cik": "9900001", "name": "Widget Robotics Inc."},
    "GZMO": {"ticker": "GZMO", "cik": "9900002", "name": "Gizmo Motion Corp."},
}
# The foundation's ceilings as the live setup writes them (ws-7d), including
# the probe ceiling the closed research-budget checks refuse.
BUDGET_CEILINGS = {"max_alphaengine_calls_24h": 50, "max_alphaengine_probe_calls_24h": 10,
                   "max_daily_cost_usd": 100.0, "max_daily_paid_calls": 100}


def _no_network(*args, **kwargs):  # pragma: no cover - only reached on a regression
    raise AssertionError(f"the canary opened a network connection: {args!r}")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


SEC_ACCESSION = "0009900001-26-000101"
# WDGT's 10-K for calendar 2025.  Like Accenture's, it reports its fourth
# quarter and the same quarter a year earlier as quarters, so the annual rule
# (COMPANY_FACTS_RULE_REFS["10-K"]) can answer it.
SEC_ANNUAL_ACCESSION = "0009900001-26-000050"


def annual_rows(accession: str) -> list[dict]:
    return [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 440000000000, "accn": accession,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-10", "frame": "CY2025"},
        {"start": "2024-10-01", "end": "2024-12-31", "val": 104000000000, "accn": accession,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-10", "frame": "CY2024Q4"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 117000000000, "accn": accession,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-10", "frame": "CY2025Q4"},
    ]
# GZMO's 10-K for calendar 2025 reports fiscal years only, like AMZN's.  Its
# fourth quarter is derived as fiscal year less nine months from the filed
# statement rows of the 10-K and the year's three 10-Qs (FY - 9M), in millions:
# 2025: 414,000 - (100,000 + 102,000 + 104,000) = 108,000
# 2024: 372,000 - ( 90,000 +  92,000 +  94,000) =  96,000  -> +12.5%
GZMO_ANNUAL_ACCESSION = "0009900002-26-000050"
MILLION = 1_000_000


def annual_only_rows(accession: str) -> list[dict]:
    return [
        {"start": start, "end": end, "val": value * MILLION, "accn": accession,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-12", "frame": frame}
        for start, end, value, frame in (("2025-01-01", "2025-12-31", 414000, "CY2025"),
                                         ("2024-01-01", "2024-12-31", 372000, "CY2024"))]


def gzmo_statement_filings() -> dict[str, tuple[str, str, str, list[dict]]]:
    def line(start, end, value):
        return {"statement": "income",
                "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "label": "Revenue", "level": 0, "parent_concept": None, "is_breakdown": False,
                "dimension_axis": None, "dimension_member": None, "period_start": start,
                "period_end": end, "value": str(value * MILLION), "unit": "usd",
                "balance": "credit"}

    return {
        "0009900002-25-000011": ("10-Q", "2025-05-01", "2025-03-31", [
            line("2024-01-01", "2024-03-31", 90000), line("2025-01-01", "2025-03-31", 100000)]),
        "0009900002-25-000022": ("10-Q", "2025-08-01", "2025-06-30", [
            line("2024-04-01", "2024-06-30", 92000), line("2024-01-01", "2024-06-30", 182000),
            line("2025-04-01", "2025-06-30", 102000), line("2025-01-01", "2025-06-30", 202000)]),
        "0009900002-25-000033": ("10-Q", "2025-10-30", "2025-09-30", [
            line("2024-07-01", "2024-09-30", 94000), line("2024-01-01", "2024-09-30", 276000),
            line("2025-07-01", "2025-09-30", 104000), line("2025-01-01", "2025-09-30", 306000)]),
        GZMO_ANNUAL_ACCESSION: ("10-K", "2026-02-12", "2025-12-31", [
            line("2024-01-01", "2024-12-31", 372000), line("2025-01-01", "2025-12-31", 414000)]),
    }


DOC_A = "alphaengine-doc:990000000000001"
DOC_B = "alphaengine-doc:990000000000002"


def document_text(document_ref: str) -> str:
    # Both documents carry the same words, so one fixture quote serves both.
    del document_ref
    return ("HERMETIC FIXTURE ONLY. Widget Robotics Inc. (WDGT) management says factory "
            "automation orders remain strong; this is not a real research result.\n") * 40


class Selection:
    """The discovery-selection child, answering "read every candidate"."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.consumed: list[str] = []

    def currently_consumed(self, **kwargs):
        return []

    def current_selections(self, **kwargs):
        return {}

    def start(self, *, discovery_ref, view, **kwargs):
        self.started.append(discovery_ref)
        return {"id": f"discovery-selection:{len(self.started):024x}", "status": "succeeded",
                "discovery_ref": discovery_ref, "summary": {"status": "succeeded"},
                "effective_selected": [item["document_ref"] for item in view["candidates"]]}

    def mark_consumed(self, ticket) -> None:
        self.consumed.append(ticket["id"])


class Acquisitions:
    """The acquisition child: a real governed acquisition on a fake page handle.

    It also leaves the ticket directory the review plane reads a manifest from,
    exactly as the launched child does.
    """

    def __init__(self, harness, state: Path) -> None:
        self.h = harness
        self.state = state
        self.calls: list[str] = []
        self.tickets: dict[str, dict] = {}

    def start_bounded_probe(self, *, document_ref, caller_ref, max_pages=20):
        self.calls.append(document_ref)
        ticket = f"alphaengine-acquisition:{len(self.calls):024x}"
        self.tickets[ticket] = {"id": ticket, "status": "running", "document_ref": document_ref}
        return dict(self.tickets[ticket])

    def finish(self) -> None:
        for ticket in self.tickets.values():
            if ticket["status"] != "running":
                continue
            self.h.document_handle.text = document_text(ticket["document_ref"])
            self.h.document_handle.page_chars = 7000
            manifest = self.h.acquisition.acquire(
                self.h.acquisition.build_plan(ticket["document_ref"]))["manifest"]
            directory = self.state / "acquisitions" / ticket["id"].split(":")[1]
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            for name, value in {
                "ticket.json": {"id": ticket["id"], "status": "succeeded",
                                "document_ref": ticket["document_ref"]},
                "summary.json": {"document_ref": ticket["document_ref"],
                                 "manifest_ref": manifest["id"],
                                 "manifest_hash": manifest["content_hash"],
                                 "manifest_status": "complete",
                                 "assembled_content_sha256": manifest["declared_content_sha256"]},
                "manifest.json": manifest,
            }.items():
                (directory / name).write_text(canonical_json(value)); (directory / name).chmod(0o600)
            ticket["status"] = "succeeded"

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


class NewWorkspaceCanaryTests(unittest.TestCase):
    """One workspace, the whole research loop, a mission upgrade, the loop again."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environ = patch.dict(os.environ, {}, clear=False)
        environ.start(); self.addCleanup(environ.stop)
        os.environ.pop("DALTON_WORKSPACE_MANIFEST", None)
        for target in ("socket.create_connection", "socket.socket.connect",
                       "urllib.request.urlopen", "http.client.HTTPConnection.connect"):
            guard = patch(target, _no_network)
            guard.start(); self.addCleanup(guard.stop)

    # -- stage 0: the formal creation flow --------------------------------------

    def create_workspace(self):
        from dalton_core.workspace import load_workspace_manifest
        from dalton_core.workspace_creation import create_blank_workspace
        from dalton_core.workspace_mission_setup import (
            draft_first_mission, publish_first_mission_to_store,
        )
        from dalton_core.workspace_runtime_setup import install

        release = self.root / "release"; release.mkdir()
        receipt = create_blank_workspace(
            self.root / "fleet", "canary", 18931, "release:sha256:" + "c" * 64, release,
            request_id="canary-request", display_name="Canary robotics",
            shared_readonly_paths=(release,))
        install(receipt["manifest_path"], actor_ref=OWNER, stage_host_lanes=False)
        workspace = load_workspace_manifest(receipt["manifest_path"])
        state = workspace.state_dir
        foundation = json.loads((state / "research-foundation.json").read_text())
        body = {key: value for key, value in foundation.items() if key != "content_hash"}
        body["mission_defaults"]["source_plan"] = [
            {"source_ref": ref, "role": "shared connected evidence source", "status": "connected"}
            for ref in ("source:alphaengine", "source:sales-notes", "source:sec-edgar")]
        body["mission_defaults"]["budget_ceilings"] = dict(BUDGET_CEILINGS)
        foundation = {**body, "content_hash": content_hash(body)}
        _write_json(state / "research-foundation.json", foundation)
        proposal = draft_first_mission(
            workspace, goal="Research the industrial robotics industry: $WDGT and $GZMO",
            method_foundation=foundation, industry="Industrial Robotics",
            companies=[{"company_ref": WDGT, "ticker": "WDGT"},
                       {"company_ref": GZMO, "ticker": "GZMO"}])
        self.assertEqual(proposal["setup_state"], "ready_for_confirmation", proposal["review_issues"])
        resolved: list[str] = []

        def resolver(ticker: str) -> dict[str, str]:
            resolved.append(ticker)
            return dict(ISSUERS[ticker])

        with DaltonStore(state / "core.sqlite") as store:
            mission = publish_first_mission_to_store(
                store, workspace, proposal=proposal, proposal_hash=proposal["content_hash"],
                actor_ref=OWNER, method_foundation=foundation, sec_ticker_resolver=resolver)
        self.assertEqual(sorted(resolved), ["GZMO", "WDGT"])
        return workspace, mission

    def assert_governance_baseline(self, workspace, mission) -> None:
        from dalton_core.coverage_mission import research_budget_shape_valid
        from dalton_core.research_auto_commit import policy_lists_document_rule
        from dalton_core.sec_company_facts_lane import check_core_governance_rules
        from dalton_core.workspace_health_parity import Environment, check_environment

        state = workspace.state_dir
        with DaltonStore(state / "core.sqlite") as store:
            check_core_governance_rules(store)  # ws-7d's policy-4 failed exactly here
            active = store.active_policy_version().to_dict()
            self.assertTrue(policy_lists_document_rule(active))
            # FY - 9M is signed with the rest of the baseline, by the flow.
            from dalton_core.research_auto_commit import SEC_FY_MINUS_9M_RULE_REF
            self.assertIn(SEC_FY_MINUS_9M_RULE_REF,
                          active["policy"]["research_candidate_auto_commit"]["rules"])
            self.assertEqual(active["policy"]["research_budget"], {
                "max_alphaengine_calls_24h": 50, "max_daily_cost_usd": 100.0,
                "max_daily_paid_calls": 100})
            self.assertTrue(active["independence_predicates"])
            from dalton_core.agenda import AgendaStore
            mandate = AgendaStore(store).mandate_version(mission["bindings"]["mandate_version"]["ref"])
        cap = mandate["constraints"]["research_budget"]
        self.assertTrue(research_budget_shape_valid(cap), cap)
        self.assertNotIn("max_alphaengine_probe_calls_24h", cap)
        env = Environment("canary", state, service_config=workspace.config_path)
        try:
            report = check_environment(env, include_host=False)
        finally:
            env.close()
        gaps = [row for row in report["rows"] if row["status"] == "gap"
                and row["section"] in {"governance", "authorization", "mission"}]
        self.assertEqual(gaps, [], json.dumps(gaps, indent=1, ensure_ascii=False))

    # -- stage 1: discovery ---------------------------------------------------------

    def discover(self, state: Path):
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.macos_launchagent import _alphaengine_discovery_plan
        from dalton_core.mission_source_discovery import (
            MissionSourceDiscoveryCoordinator, load_discovery_plan,
        )
        from tests.test_mission_source_discovery import FakeSearchLauncher, SearchHarness

        harness = SearchHarness(state, [{"doc_id": DOC_A.split(":")[1]},
                                        {"doc_id": DOC_B.split(":")[1]}])
        self.addCleanup(harness.close)
        self.harness = harness
        self.missions = CoverageMissionAuthority(harness.core)
        plan = load_discovery_plan(_alphaengine_discovery_plan(state))
        self.assertEqual(set(plan["companies"]), {WDGT, GZMO})
        self.search = FakeSearchLauncher(harness, self.missions, plan)
        self.acquisitions = Acquisitions(harness, state)
        self.selection = Selection()
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=harness.core, missions=self.missions, plan=plan,
            search_launcher=self.search, acquisition_launcher=self.acquisitions,
            selection_launcher=self.selection, spool_dir=state / "spool",
            clock=harness.clock)
        ticks = []
        for _ in range(12):
            ticks.append(self.coordinator.dispatch_once())
            self.acquisitions.finish()
        mission = self.missions.active_mission(plan["mission_ref"])
        reviews = self.missions.document_reviews(mission["id"])
        self.assertEqual(sorted(r["document_ref"] for r in reviews), [DOC_A, DOC_B],
                         json.dumps(ticks, indent=1, default=str)[-3000:])
        self.assertEqual(sorted(self.acquisitions.calls), [DOC_A, DOC_B])
        return mission, reviews

    # -- stages 2-4: extraction, claim-support check, ledger commit ------------------

    def prepare_extraction(self, state: Path) -> None:
        from dalton_core.extraction_backlog import DocumentProvenanceStore
        from dalton_core.model_router import ModelRouter
        from dalton_core.writer_server import HUMAN_GOVERNANCE_OPERATIONS, Principal, WriterServer
        from dalton_core.alphaengine_acquisition_launcher import AlphaEngineAcquisitionLauncher
        from tests.test_transcript_polish_model_worker import policy, profile

        h = self.harness
        provenance = DocumentProvenanceStore(h.core.connection, clock=h.clock)
        for document_ref in (DOC_A, DOC_B):
            provenance.record({
                "document_ref": document_ref, "source_ref": "source:alphaengine",
                "spec_ref": "earnings-call-transcripts", "provenance_tier": "management",
                "broker": None, "broker_key": "", "authors": None, "sources": None,
                "title": "Widget Robotics Inc. Q2 2026 Earnings Call Transcript",
                "named_companies": ["Widget Robotics Inc."], "published_at": None,
                "metadata_seen": True})
        router = ModelRouter(str(state / "canary-router.sqlite"), clock=h.clock)
        self.addCleanup(router.close)
        pr = profile()
        pr["provider"] = "hermetic-fixture"
        pr["cost"]["input_per_million_usd"] = pr["cost"]["output_per_million_usd"] = 0
        pr["availability"]["checked_at"] = h.clock().isoformat()
        pr["availability"]["valid_until"] = (h.clock() + timedelta(days=2)).isoformat()
        router.register_profile(pr); router.register_policy(policy())
        self.model_config = state / "canary-extraction-model-config.json"
        _write_json(self.model_config, {
            "routing_policy_ref": policy()["policy_version_ref"],
            "credential_slot_refs": [profile()["credential_slot_ref"]],
            "model_router_db": str(state / "canary-router.sqlite"),
            "broker_socket": str(state / "none.sock"), "broker_auth_key": str(state / "none.key"),
            "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
            "budget_db": str(state / "canary-budget.sqlite"),
            "budget_policy_ref": "thesis-impact-day-budget-policy:production:1"})
        launcher = AlphaEngineAcquisitionLauncher(state_dir=state,
                                                  governance_path=state / "unused-governance.json")
        writer = WriterServer(state / "unused.sqlite", state / "unused.sock", {
            "human": Principal("human", "fixture-token", HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER),
        }, acquisition_launcher=launcher)
        writer._store = h.core; writer._connectors = h.connectors
        writer._observability = h.observability; writer._coverage_mission = self.missions
        writer._transcript_spool = h.spool; writer._scheduler = h.scheduler
        self.writer = writer

    def quote_of(self, review) -> str:
        from dalton_core.document_extraction import DocumentExtractionService

        context = DocumentExtractionService(self.writer).view(
            review_id=review["review_id"], expected_review_hash=content_hash(review),
            offset=0, actor_ref=OWNER)["context"]
        return context["quotes"][0]["quote_id"]

    def extract(self, state: Path, *, quote_id: str, run: str, max_windows: int = 1):
        from dalton_core import document_extraction_cli
        from dalton_core.claim_support_verification import ClaimSupportVerifier
        from dalton_core.document_extraction_cli import run_extraction
        from tests.test_claim_support_verification import FakeModel, _reply

        fixture = state / f"canary-fixture-{run}.json"
        _write_json(fixture, {"schema_version": "0.1", "suggestions": [{
            "quote_id": quote_id,
            "normalized_statement": "Widget Robotics management says factory automation orders remain strong.",
            "metric_or_aspect": "aspect:order-intake", "period": "Q2 2026",
            "basis": "fixture management commentary"}]})
        verifier = FakeModel(*([_reply(("supported", "about_subject", None))] * 4))
        base = document_extraction_cli.ExtractionHost

        class Host(base):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                inner._claim_support_verifier = ClaimSupportVerifier(
                    store=inner.store, model_call=verifier, daily_cap_micros=100_000,
                    producer_family=lambda ref: "deepseek-v4", max_attempts=3)

        with patch.object(document_extraction_cli, "ExtractionHost", Host):
            summary = run_extraction(
                state_dir=state, model_config_path=self.model_config,
                summary_dir=state / "extractions" / f"canary-{run}", spool_dir=state / "spool",
                scheduler_db=state / "scheduler.sqlite", requested_by=None,
                max_windows=max_windows, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=fixture,
                candidate_staging=state / "research-review" / "candidate-staging.sqlite")
        return summary, verifier

    # -- stage 5: SEC company facts ----------------------------------------------------

    def sec_company_facts(self, state: Path, mission, *, run_key: str, ticker: str = "WDGT",
                          accession: str | None = None, day: int = 0, form: str = "10-Q",
                          annual_accession: str | None = None,
                          annual_only_accession: str | None = None):
        from dalton_core.sec_authority_harness import MutableClock
        from dalton_core.sec_company_facts_lane import Issuer, RehearsalGovernance, SecCompanyFactsLane
        from tests.test_research_plan_executor import _sec_company_facts_body
        from tests.test_sec_company_facts_lane import fake_adapter

        accession = accession or SEC_ACCESSION
        company_ref = {"WDGT": WDGT, "GZMO": GZMO}[ticker]
        body = json.loads(_sec_company_facts_body())
        body.update({"cik": int(ISSUERS[ticker]["cik"]),
                     "entityName": ISSUERS[ticker]["name"].upper()})
        rows = body["facts"]["us-gaap"][
            "RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
        for fact in rows:
            fact["accn"] = accession
        if annual_accession is not None:
            rows.extend(annual_rows(annual_accession))
        if annual_only_accession is not None:
            rows.extend(annual_only_rows(annual_only_accession))
        clock = MutableClock()
        clock.advance(day * 86400)  # a new connector quota day
        issuer = Issuer(ticker, ISSUERS[ticker]["cik"], company_ref, ISSUERS[ticker]["name"])
        lane = SecCompanyFactsLane(
            state_dir=state, staging_path=state / "research-review" / "sec-candidate-staging.sqlite",
            governance=RehearsalGovernance(approved_by=OWNER), issuers=(issuer,),
            adapter=fake_adapter(clock, json.dumps(body, sort_keys=True).encode()), clock=clock)
        with lane:
            return lane.run_issuer(
                issuer, actor_ref=AUTOMATION, run_key=run_key,
                filed_from="2025-08-20", filed_to="2026-08-20", form=form,
                expected_accession=annual_accession if form == "10-K" else accession,
                mission_context={"mission_version_ref": mission["id"],
                                 "mission_version_hash": mission["content_hash"],
                                 "company_ref": company_ref})

    def assert_fourth_quarter_is_dispatched(self, state: Path, mission) -> None:
        from dalton_core.mission_sec_quarters import (
            MissionSecQuartersCoordinator,
            accession_in_hand,
        )

        def coordinator():
            return MissionSecQuartersCoordinator(
                store=self.harness.core, missions=self.missions, state_dir=state,
                checklist=lambda: [{"company_ref": WDGT, "ticker": "WDGT", "items": [
                    {"item_ref": "quarterly_financials", "have": 1, "required": 4}]}],
                clock=lambda: datetime(2026, 9, 28, tzinfo=timezone.utc))

        first = coordinator().dispatch_once()
        self.assertEqual(first["status"], "queued", json.dumps(first, indent=1, default=str))
        self.assertIn("2025-12-31", first["recent_quarters_missing"])
        [annual] = [q for q in first["queued"] if q["form"] == "10-K"]
        self.assertEqual((annual["accession"], annual["period"]),
                         (SEC_ANNUAL_ACCESSION, "2025-10-01..2025-12-31"))
        pending = {row["expected_accession"]: row
                   for row in self.missions.pending_sec_dispatches(limit=10)}
        self.assertEqual(pending[SEC_ANNUAL_ACCESSION]["form"], "10-K")
        claims = self.claim_count()
        q4 = self.sec_company_facts(state, mission, run_key="canary-q4", form="10-K",
                                    annual_accession=SEC_ANNUAL_ACCESSION, day=2)
        self.assertEqual(q4["status"], "committed", json.dumps(q4, indent=1, default=str)[-3000:])
        self.assertEqual((q4["form"], q4["candidate"]["period"]), ("10-K", "2025-10-01..2025-12-31"))
        self.assertEqual(q4["facts"]["growth_percent"], "12.50")
        self.assertEqual(self.claim_count(), claims + 1)
        # The drain's bookkeeping, as the writer does it after the lane ran.
        for accession, row in pending.items():
            ticket = f"sec-lane-run:canary-{accession[-6:]}"
            self.missions.mark_sec_dispatch_launched(row["dispatch_id"], ticket)
            self.missions.settle_sec_dispatch(
                row["dispatch_id"], outcome="finished", ticket_ref=ticket,
                detail="succeeded" if accession == SEC_ANNUAL_ACCESSION else "failed")
        # The bounded-planner path sees the accession answered ...
        self.assertEqual(accession_in_hand(self.harness.core.connection,
                                           SEC_ANNUAL_ACCESSION)["state"], "succeeded")
        # ... and the coordinator sees the quarter held.
        again = coordinator().dispatch_once()
        self.assertNotIn(SEC_ANNUAL_ACCESSION,
                         [q["accession"] for q in again.get("queued", [])], again)
        self.assertEqual(self.claim_count(), claims + 1)

    def ingest_statements(self, mission, company_ref: str, ticker: str, filings) -> None:
        """The financial-statements lane's record of these filings, as its child leaves it."""

        authorization = self.missions.authorize_sec_lane(
            company_ref=company_ref, ticker=ticker, actor_ref=AUTOMATION,
            mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"])
        for form in ("10-Q", "10-K"):
            batch = [{"accession": accession, "form": kind, "filed": filed,
                      "report_date": report, "lines": lines}
                     for accession, (kind, filed, report, lines) in sorted(filings.items())
                     if kind == form]
            dispatch = self.missions.queue_statement_dispatch(authorization=authorization, form=form)
            self.missions.mark_statement_dispatch_launched(
                dispatch["dispatch_id"], f"sec-financials-run:canary-{form.lower()}")
            self.missions.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"],
                observation={"schema_version": "0.1", "cik": ISSUERS[ticker]["cik"].zfill(10),
                             "entity_name": ISSUERS[ticker]["name"], "filings": batch,
                             "source_record_refs": ["raw-sink:" + ("9" if form == "10-K" else "8") * 64],
                             "next_cursor": None, "provider_status": 200},
                governance_ref="connector-governance:sec-financial-statements:v2",
                governance_hash="b" * 64)
            self.missions.settle_statement_dispatch(dispatch["dispatch_id"], outcome="succeeded")

    def assert_fourth_quarter_is_derived(self, state: Path, mission) -> None:
        """A 10-K of fiscal-year totals: the quarter lane derives Q4 as FY - 9M.

        No 10-K connector run (it could only fail) and no hand-signed rule: the
        workspace's baseline policy lists the FY - 9M rule, the statement lane's
        rows are in Core, and the coordinator stages the derivation and the
        Ledger admits it after rebuilding it from those rows.
        """

        from dalton_core.mission_sec_quarters import MissionSecQuartersCoordinator
        from dalton_core.research_auto_commit import SEC_FY_MINUS_9M_RULE_REF
        from dalton_core.research_verification import CandidateStagingStore

        self.ingest_statements(mission, GZMO, "GZMO", gzmo_statement_filings())
        staging = CandidateStagingStore(state / "research-review" / "candidate-staging.sqlite")
        self.addCleanup(staging.close)

        def coordinator():
            return MissionSecQuartersCoordinator(
                store=self.harness.core, missions=self.missions, state_dir=state,
                checklist=lambda: [{"company_ref": GZMO, "ticker": "GZMO", "items": [
                    {"item_ref": "quarterly_financials", "have": 1, "required": 4}]}],
                clock=lambda: datetime(2026, 9, 28, tzinfo=timezone.utc), staging=staging)

        claims = self.claim_count()
        first = coordinator().dispatch_once()
        self.assertIn(first["status"], {"queued", "committed"},
                      json.dumps(first, indent=1, default=str))
        [derived] = first["derived"]
        self.assertEqual(derived["status"], "committed", json.dumps(derived, indent=1))
        self.assertEqual((derived["accession"], derived["period"], derived["value"],
                          derived["rule_ref"]),
                         (GZMO_ANNUAL_ACCESSION, "2025-10-01..2025-12-31", "12.5",
                          SEC_FY_MINUS_9M_RULE_REF))
        self.assertEqual((derived["q4"], derived["prior_q4"]),
                         (str(108000 * MILLION), str(96000 * MILLION)))
        # The 10-K itself is never sent to the SEC lane.
        self.assertNotIn(GZMO_ANNUAL_ACCESSION, [q["accession"] for q in first["queued"]])
        self.assertNotIn(GZMO_ANNUAL_ACCESSION,
                         [row["expected_accession"]
                          for row in self.missions.pending_sec_dispatches(limit=20)])
        self.assertEqual(self.claim_count(), claims + 1)
        row = self.harness.core.connection.execute(
            "SELECT claim_json FROM claim_versions WHERE json_extract(claim_json,'$.subject_ref')=? "
            "AND json_extract(claim_json,'$.metric_or_aspect')='quarterly_revenue_yoy_growth' "
            "AND json_extract(claim_json,'$.period')='2025-10-01..2025-12-31'", (GZMO,)).fetchone()
        claim = json.loads(row[0])
        self.assertEqual(claim["basis"], "official-filing-xbrl-derived")
        self.assertIn("not a filed quarter", claim["normalized_statement"])
        # Held now: the next tick neither derives it again nor queues its 10-K.
        again = coordinator()
        self.assertIn("2025-10-01..2025-12-31", again._held_periods(GZMO))
        second = again.dispatch_once()
        self.assertFalse([item for item in second.get("derived", [])
                          if item["accession"] == GZMO_ANNUAL_ACCESSION], second)
        self.assertEqual(self.claim_count(), claims + 1)

    # -- stage 6: claim index and dossier -------------------------------------------

    def pass_screen(self, mission) -> None:
        from dalton_core.coverage_mission import CoverageMissionConflict

        for status in ("entered", "gate_passed"):
            if status == "entered":
                try:
                    self.missions.record_stage(
                        mission_version_ref=mission["id"],
                        mission_version_hash=mission["content_hash"],
                        company_ref=WDGT, stage_ref="initial_screen", status=status,
                        evidence_refs=[mission["id"]], rationale="canary: screened",
                        actor_ref=AUTOMATION, idempotency_key=f"canary:{mission['id']}:{status}")
                except CoverageMissionConflict:
                    pass  # the SEC lane's stage claim already entered it
                continue
            self.missions.record_stage(
                mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
                company_ref=WDGT, stage_ref="initial_screen", status=status,
                evidence_refs=[mission["id"]], rationale="canary: screened",
                actor_ref=AUTOMATION, idempotency_key=f"canary:{mission['id']}:{status}")

    def index_and_dossier(self, state: Path, *, run: str):
        from dalton_core.claim_index_cli import run_claim_index
        from dalton_core.company_dossier_cli import run_dossier
        from tests.test_claim_index_lane import FakeModel as IndexModel
        from tests.test_dossier_lane import FakeModel as DossierModel, resolver

        config = state / "canary-empty-model-config.json"
        config.write_text("{}", encoding="utf-8")
        # One prose claim per batch; the numeric SEC claim is rule-tagged.
        index = []
        with patch("dalton_core.claim_index_cli.CockpitModel", IndexModel("1\tdemand_drivers\n")):
            for attempt in range(4):
                index.append(run_claim_index(
                    state_dir=state, model_config_path=config,
                    summary_dir=state / "claim-index-runs" / f"canary-{run}-{attempt}",
                    scheduler_db=None, company_ref=WDGT))
                if not index[-1].get("pending"):
                    break
        drafter = DossierModel()
        dossier = run_dossier(
            state_dir=state, model_config_path=config,
            summary_dir=state / "company-dossier-runs" / f"canary-{run}",
            policy_path=state / "p12a-dossier-policy-v1.json",
            model_factory=lambda: drafter,
            verifier_model_factory=lambda: DossierModel(route="route:verify"),
            family_resolver=resolver)
        return index, dossier, drafter

    # -- stage 7: event judgement ------------------------------------------------------

    def record_news(self, mission, document_ref: str) -> dict:
        from dalton_core.research_event import ResearchEventAuthority, record_event

        occurred = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
        return record_event(
            ResearchEventAuthority(self.harness.core), company_ref=WDGT, kind="news",
            occurred_at=occurred.isoformat(),
            source_refs=["source:alphaengine", document_ref],
            payload={"document_ref": document_ref, "source_ref": "source:alphaengine",
                     "spec_ref": "earnings-call-transcripts", "discovery_ref": "d",
                     "title": None, "host": None},
            mission=mission, actor_ref=AUTOMATION)

    def judge(self, state: Path, *, run: str):
        from dalton_core.event_judgement_cli import run_judgement
        from tests.test_event_judgement import (
            JUDGE_ROUTE, PASS, VERIFIER_ROUTE, FakeModel, decision, resolver,
        )

        judge_model = FakeModel([decision(), decision()], route=JUDGE_ROUTE)
        summary = run_judgement(
            state_dir=state, summary_dir=state / "event-judgement-runs" / f"canary-{run}",
            policy_path=state / "tracking-policy.json", now=datetime.now(timezone.utc),
            judge_model=judge_model,
            verifier_model=FakeModel([PASS, PASS], route=VERIFIER_ROUTE),
            family_resolver=resolver())
        return summary, judge_model

    def judged(self) -> int:
        from dalton_core.event_judgement import EventJudgementAuthority

        return EventJudgementAuthority(self.harness.core).judged_count(WDGT)

    # -- the upgrade ---------------------------------------------------------------------

    def upgrade_mission(self) -> dict:
        """Sign one more rule through the owner's real cascade, on this Core.

        The same policy -> constitution -> mission chain the owner scripts run
        (ws-7d's policy-5 was one), applied in place rather than on a copy.
        """

        from dalton_core.agenda import AgendaStore
        from dalton_core.research_constitution import ResearchConstitutionAuthority
        from dalton_core.research_plan import PLAN_AUTO_START_RULE_REF
        from scripts.publish_extraction_authority_chain import apply_chain
        from scripts.sign_auto_commit_rules import read_current
        from scripts.sign_research_plan_auto_start import build_chain

        store = self.harness.core
        current = read_current(store.connection)
        chain = build_chain(current, now=datetime.now(timezone.utc).isoformat(),
                            rules=[PLAN_AUTO_START_RULE_REF])
        agenda = AgendaStore(store)
        constitutions = ResearchConstitutionAuthority(store)

        def apply(operation, params):
            values = dict(params)
            if operation == "create_policy":
                return {"policy_version": store.create_policy(
                    values.pop("policy"), actor_ref=OWNER, **values)}
            if operation == "create_mandate":
                return agenda.create_mandate(values.pop("mandate_ref"), actor_ref=OWNER, **values)
            if operation == "publish_research_constitution":
                return constitutions.publish_constitution(
                    values.pop("constitution_ref"), actor_ref=OWNER, **values)
            if operation == "create_coverage_mission":
                return self.missions.create_mission(
                    values.pop("mission_ref"), actor_ref=OWNER, **values)
            raise AssertionError(operation)

        apply_chain(chain, apply)
        return self.missions.active_mission(current["mission_ref"])

    def republish_mission(self) -> dict:
        """A mission-only version over the one in force (P2's weekly-brief switch)."""

        from scripts.sign_research_plan_auto_start import BODY_FIELDS, _next_version_id

        current = self.missions.active_mission(self.missions.active_mission(
            self.search.plan["mission_ref"])["mission_ref"])
        number = int(current["version"]) + 1
        self.missions.create_mission(
            current["mission_ref"], actor_ref=OWNER,
            **{field: json.loads(json.dumps(current[field])) for field in BODY_FIELDS},
            version_id=_next_version_id(current["id"], number),
            prior_version_ref=current["id"],
            idempotency_key=f"{current['mission_ref']}:{number}:canary-republish")
        return self.missions.active_mission(current["mission_ref"])

    def research_without_reconciling(self, version) -> None:
        """One search under ``version`` recorded as a Guidepoint child records it.

        The row lands as the search saw it -- ``already_in_authority`` for a
        document Core holds -- and nothing settles it or opens a review.  The
        first version's search result is recorded again (the page a re-search
        would return), so no second governed call is needed.
        """

        [first] = self.missions.source_discoveries(
            version["id"], company_ref=WDGT, across_versions=True, limit=1000)[-1:]
        grant = self.missions.authorize_source_discovery(
            company_ref=WDGT, source_ref="source:alphaengine", requested_by=AUTOMATION,
            mission_version_ref=version["id"])
        self.missions.record_source_discovery(
            authorization=grant, discovery_plan_ref=first["discovery_plan_ref"],
            discovery_plan_hash=first["discovery_plan_hash"], spec_ref=first["spec_ref"],
            query_hash=first["query_hash"], parameters=first["parameters"],
            connector_invocation_ref=first["connector_invocation_ref"],
            connector_invocation_hash=first["connector_invocation_hash"],
            source_envelope_ref=first["source_envelope_ref"],
            source_envelope_hash=first["source_envelope_hash"],
            document_refs=first["document_refs"],
            in_authority_document_refs=first["document_refs"])

    def awaiting_in_active_version(self) -> int:
        # Exactly the extraction launcher's queue (``_awaiting_state``).
        return self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'").fetchone()[0]

    def claim_count(self) -> int:
        return self.harness.core.connection.execute(
            "SELECT COUNT(*) FROM claim_versions").fetchone()[0]

    def test_a_created_workspace_runs_the_whole_loop_and_survives_an_upgrade(self) -> None:
        workspace, mission = self.create_workspace()
        self.assertEqual({m["company_ref"] for m in mission["universe"]}, {WDGT, GZMO})
        self.assert_governance_baseline(workspace, mission)
        state = workspace.state_dir
        mission, reviews = self.discover(state)

        self.prepare_extraction(state)
        quote = self.quote_of(reviews[0])
        self.assertEqual(quote, self.quote_of(reviews[1]))
        claims_before = self.claim_count()
        summary, verifier = self.extract(state, quote_id=quote, run="v1")
        admitted = [item for item in summary["admitted"] if item["status"] == "admitted"]
        self.assertEqual(len(admitted), 1, json.dumps(summary, indent=1, default=str)[-4000:])
        self.assertEqual(admitted[0]["support_verdict"]["support"], "supported")  # 核验
        self.assertEqual(len(verifier.calls), 1)
        self.assertIn("Widget Robotics", verifier.calls[0]["prompt"])
        self.assertEqual(self.claim_count(), claims_before + 1)  # 入账
        states = {r["document_ref"]: r["state"] for r in self.missions.document_reviews(mission["id"])}
        closed = [ref for ref, value in states.items() if value != "awaiting_human_extraction"]
        still_open = [ref for ref, value in states.items() if value == "awaiting_human_extraction"]
        self.assertEqual((len(closed), len(still_open)), (1, 1), states)

        # Nothing signed by hand: the first mission's policy already carries
        # research_plan_auto_start and the company-facts rule.
        sec = self.sec_company_facts(state, mission, run_key="canary-v1",
                                     annual_accession=SEC_ANNUAL_ACCESSION)
        self.assertEqual(sec["status"], "committed", json.dumps(sec, indent=1, default=str)[-3000:])
        self.assertEqual(sec["mission_stage_claim"]["actor_ref"], AUTOMATION)
        # A replayed run is the same run: no second claim for the accession.
        claims = self.claim_count()
        replay_v1 = self.sec_company_facts(state, mission, run_key="canary-v1",
                                           annual_accession=SEC_ANNUAL_ACCESSION)
        self.assertEqual(replay_v1["status"], "duplicate", replay_v1)
        self.assertEqual(self.claim_count(), claims)
        # The fourth quarter lives in the 10-K: the quarterly coordinator
        # queues it by itself, the lane commits it under the annual rule, and
        # nothing -- neither path -- queues it again.
        self.assert_fourth_quarter_is_dispatched(state, mission)

        self.pass_screen(mission)
        index, dossier, _ = self.index_and_dossier(state, run="v1")
        self.assertNotIn("refused", [item["index_status"] for item in index], index)
        self.assertEqual(index[-1].get("pending", 0), 0, index)
        self.assertIn(dossier["dossier_status"], {"published", "partial_published"},
                      json.dumps(dossier, indent=1, default=str)[-3000:])
        self.assertTrue(dossier["version_ref"])

        self.record_news(mission, closed[0])
        judgement, judge_model = self.judge(state, run="v1")
        self.assertEqual((judgement["status"], judgement["judged"]), ("succeeded", 1),
                         json.dumps(judgement, indent=1, default=str)[-3000:])
        self.assertEqual(len(judge_model.prompts), 1)
        self.assertEqual(self.judged(), 1)

        # ---- mission upgrade: policy -> constitution -> mission ----
        claims_before_upgrade = self.claim_count()
        v2 = self.upgrade_mission()
        self.assertNotEqual(v2["id"], mission["id"])
        # The parity check sees the stall before any tick runs ...
        from dalton_core.workspace_health_parity import Environment, check_mission_reviews
        env = Environment("canary", state, service_config=workspace.config_path)
        try:
            [stranded] = [row for row in check_mission_reviews(env)
                          if row["check"] == "reviews.stranded_on_superseded_version"]
        finally:
            env.close()
        self.assertEqual(stranded["status"], "gap", stranded)
        # The stall ws-7d hit: the open review stayed on the old version and
        # the extraction queue under the new one was empty.
        self.assertEqual(self.awaiting_in_active_version(), 0)
        # 2026-09-28, the Guidepoint shape: before any carry runs, a search
        # under the new version finds the open document again and records it
        # already_in_authority (no reconciliation turns it into a review),
        # and the mission is upgraded a second time over it.
        intermediate = v2
        self.research_without_reconciling(intermediate)
        rows = {row["document_ref"]: row["status"]
                for row in self.missions.discovered_documents(intermediate["id"])}
        self.assertEqual(rows.get(still_open[0]), "already_in_authority", rows)
        v2 = self.republish_mission()
        self.assertNotIn(v2["id"], {mission["id"], intermediate["id"]})
        self.research_without_reconciling(v2)
        self.assertEqual(self.awaiting_in_active_version(), 0)
        # The extraction tick's first step (DocumentExtractionCoordinator
        # .dispatch_once -> _carry_awaiting -> carry_forward_awaiting_reviews).
        from dalton_core.document_extraction_launcher import DocumentExtractionCoordinator
        tick = object.__new__(DocumentExtractionCoordinator)
        tick.missions = self.missions
        carried = tick._carry_awaiting()
        self.assertEqual([item["status"] for item in carried], ["carried"], carried)
        self.assertEqual(carried[0]["from_version_ref"], mission["id"])
        self.assertEqual(self.awaiting_in_active_version(), 1)
        self.assertEqual(tick._carry_filtered().get("carryable"), None)
        env = Environment("canary", state, service_config=workspace.config_path)
        try:
            [stranded] = [row for row in check_mission_reviews(env)
                          if row["check"] == "reviews.stranded_on_superseded_version"]
        finally:
            env.close()
        self.assertEqual(stranded["status"], "ok", stranded)
        v2_reviews = self.missions.document_reviews(v2["id"])
        # The one open review, and the re-searched decided document carried
        # with its decision (the search under this version found it again).
        self.assertEqual([(r["document_ref"], r["state"]) for r in v2_reviews
                          if r["state"] == "awaiting_human_extraction"],
                         [(still_open[0], "awaiting_human_extraction")])
        self.assertEqual({r["document_ref"]: r["state"] for r in v2_reviews
                          if r["document_ref"] == closed[0]},
                         {closed[0]: states[closed[0]]})
        self.assertEqual(tick._carry_awaiting(), [])  # idempotent
        self.assert_governance_baseline(workspace, v2)  # ... and none after it

        # Discovery under v2 does not fetch what v1 already acquired.
        for _ in range(4):
            self.coordinator.dispatch_once()
            self.acquisitions.finish()
        self.assertEqual(sorted(self.acquisitions.calls), [DOC_A, DOC_B])

        # Extraction reads the carried review -- and only it.
        summary, verifier = self.extract(state, quote_id=quote, run="v2", max_windows=4)
        self.assertEqual(summary["partial"]["windows_succeeded"], 1,
                         json.dumps(summary, indent=1, default=str)[-3000:])
        self.assertEqual(summary["partial"]["reviews_complete"], 1)
        self.assertEqual(self.awaiting_in_active_version(), 0)
        v2_states = {r["document_ref"]: r["state"] for r in self.missions.document_reviews(v2["id"])}
        self.assertEqual(v2_states.get(closed[0], states[closed[0]]), states[closed[0]],
                         "a document finished under v1 was reopened")
        self.assertNotEqual(v2_states[still_open[0]], "awaiting_human_extraction")
        extraction_claims = self.claim_count() - claims_before_upgrade
        # The statement and citation were already checked under v1: the verdict
        # is reused, not bought again.
        self.assertEqual(len(verifier.calls), 0)

        # SEC company facts: a new filing commits under v2 (the lane's
        # governance precondition and grant resolve against the new version),
        # and the v1 accession still has exactly one claim.
        sec2 = self.sec_company_facts(state, v2, run_key="canary-v2", ticker="GZMO",
                                      accession="0009900002-26-000101", day=1,
                                      annual_only_accession=GZMO_ANNUAL_ACCESSION)
        self.assertEqual(sec2["status"], "committed", json.dumps(sec2, indent=1, default=str)[-3000:])
        self.assertEqual(self.claim_count() - claims_before_upgrade, extraction_claims + 1)
        # GZMO's 10-K reports only fiscal years: its fourth quarter is FY - 9M,
        # derived by the quarter lane under v2 from the statement rows in Core.
        self.assert_fourth_quarter_is_derived(state, v2)
        self.assertEqual(self.claim_count() - claims_before_upgrade, extraction_claims + 2)

        # Dossier under v2: nothing new from SEC, so it may redraw or stay, but not fail.
        index2, dossier2, _ = self.index_and_dossier(state, run="v2")
        self.assertNotIn("refused", [item["index_status"] for item in index2], index2)
        self.assertNotIn(dossier2["status"], {"failed"}, json.dumps(dossier2, indent=1, default=str)[-3000:])
        self.assertIn(dossier2["dossier_status"], {"nothing_new", "published", "partial_published"},
                      dossier2)

        # Event judgement: the v1 event is not judged again; a v2 event is judged.
        again, judge_model = self.judge(state, run="v2-replay")
        self.assertEqual(again["judgement_status"], "nothing_unjudged", again)
        self.assertEqual(judge_model.prompts, [])
        self.record_news(v2, still_open[0])
        fresh, _ = self.judge(state, run="v2")
        self.assertEqual((fresh["status"], fresh["judged"]), ("succeeded", 1), fresh)
        self.assertEqual(self.judged(), 2)

        # ---- maintenance with the extraction queue empty ----
        # Nothing is open under v2 any more.  The claim-support checks and the
        # P13i subject re-check used to run only inside a drafting child, so an
        # empty queue stopped them; the lane now starts a support-only child
        # and that child runs all of them.
        self.assertEqual(self.awaiting_in_active_version(), 0)
        self.assert_maintenance_runs_with_an_empty_queue(state, v2)

    def assert_maintenance_runs_with_an_empty_queue(self, state: Path, mission) -> None:
        from dalton_core import document_extraction_cli
        from dalton_core.document_extraction_cli import run_extraction
        from dalton_core.document_extraction_launcher import DocumentExtractionCoordinator
        from tests.test_support_lane_independent import SupportFakeLauncher

        launcher_root = self.root / "support-launcher"
        launcher_root.mkdir()
        launcher = SupportFakeLauncher(launcher_root)
        tick = DocumentExtractionCoordinator(missions=self.missions, launcher=launcher).dispatch_once()
        self.assertEqual((tick["status"], tick.get("mode"), tick["awaiting"]),
                         ("launched", "support_only", 0), tick)
        self.assertTrue(launcher.starts[-1].get("support_only"))

        fixture = state / "canary-fixture-support-only.json"
        _write_json(fixture, {"schema_version": "0.1", "suggestions": []})
        ran: list[str] = []
        base = document_extraction_cli.ExtractionHost

        class Host(base):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                inner._claim_support_verifier = object()

        with patch.object(document_extraction_cli, "ExtractionHost", Host), \
                patch.object(document_extraction_cli, "_run_claim_support_recheck",
                             lambda host, db: ran.append("support_recheck") or {"status": "idle"}), \
                patch.object(document_extraction_cli, "_secondary_sweep",
                             side_effect=AssertionError("a support-only run reads no document")):
            summary = run_extraction(
                state_dir=state, model_config_path=self.model_config,
                summary_dir=state / "extractions" / "canary-support-only", spool_dir=state / "spool",
                scheduler_db=state / "scheduler.sqlite", requested_by=None,
                max_windows=1, max_numeric_windows=0, max_discovery_windows=0,
                connector_governance=None, web_fetch_governance=None,
                hermetic_fixture=fixture,
                candidate_staging=state / "research-review" / "candidate-staging.sqlite",
                support_only=True)
        self.assertEqual((summary["stop_reason"], summary["reviews_scanned"], summary["drafted"]),
                         ("support_only", 0, []), summary)
        self.assertEqual(ran, ["support_recheck"])
        [reevaluation] = summary["subject_reevaluation"]
        self.assertEqual((reevaluation["mission_version_ref"], reevaluation["stop_reason"]),
                         (mission["id"], None), reevaluation)


class NextVersionIdTests(unittest.TestCase):
    """The owner scripts could not upgrade a created workspace's first mission."""

    def test_a_hash_named_first_version_gets_the_writers_numbered_name(self) -> None:
        from scripts.sign_auto_commit_rules import _next_version_id

        # ws-7d's first version; the cascade used to refuse it outright.
        self.assertEqual(
            _next_version_id("coverage-mission-version:ws-7d:f85cd96a7f406dd704e8ca5c", 2),
            "coverage-mission-version:ws-7d:2")
        # A hash that happens to end in digits used to become "...a52".
        self.assertEqual(
            _next_version_id("coverage-mission-version:canary:3fde112926c0291adddab215", 2),
            "coverage-mission-version:canary:2")
        self.assertEqual(_next_version_id("policy-17", 18), "policy-18")
        self.assertEqual(_next_version_id("constitution-version:first-mission:ab12:5", 6),
                         "constitution-version:first-mission:ab12:6")


if __name__ == "__main__":
    unittest.main()
