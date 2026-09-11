"""P9d-4a: web search as a mission discovery source -- plan 0.2, ledger, coordinator, child, writer."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dalton_core.coverage_mission import (
    DISCOVERY_SOURCES,
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionValidationError,
    validate_mission_source_discovery,
)
from dalton_core.mission_source_discovery import (
    ALPHAENGINE_SOURCE_REF,
    DiscoveryLaunchRejected,
    DiscoveryPlanError,
    MissionSourceDiscoveryCoordinator,
    WEB_SEARCH_SOURCE_REF,
    WebSearchLauncher,
    build_discovery_parameters,
    build_discovery_plan,
    discovery_query_hash,
    load_discovery_plan,
    validate_discovery_plan,
)
from dalton_core.public_web_connector import public_web_url_ref
from dalton_core.public_web_core_search import (
    FakeWebSearchHandle,
    SEARCH_PROFILE_REF,
    build_web_search_governance_record,
    public_web_urls_in_authority,
    web_search_spec_hash,
)
from dalton_core.public_web_search_cli import NETWORK_UNAVAILABLE_REASON, main as web_search_cli_main
from dalton_core.store import DaltonStore, canonical_json
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import (
    CORE_OPERATIONS,
    HUMAN_GOVERNANCE_OPERATIONS,
    Principal,
    WriterServer,
)
from tests.p9a_fixtures import ROOT, bootstrap_method_authorities, mission_params
from tests.test_mission_source_discovery import (
    ACN,
    AUTOMATION,
    CTSH,
    Clock,
    FakeSearchLauncher,
    OWNER,
    SearchHarness,
    plan_for_tests,
)
from tests.test_p9a_writer_ops import AUTOMATION_TOKEN, CORE_TOKEN, GOVERNANCE_TOKEN, P9aWriterHarness
from tests.test_public_web_core_search import CITATIONS, WebSearchHarness


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
WEB_PLAN_PATH = ROOT / "deploy/phase9/p9d4-us-it-services-web-search-plan-v3.json"
URL_A = public_web_url_ref("https://example.com/investors?q=ai")
URL_B = public_web_url_ref("https://news.example.org/accenture-ai")


def web_plan_for_tests(*, max_calls_24h: int = 20, acquisition: dict | None = None) -> dict:
    return build_discovery_plan(
        plan_id="discovery-plan:us-it-services:web-search:test",
        created_at=NOW.isoformat(timespec="microseconds"),
        mission_ref="coverage-mission:us-it-services",
        companies={ACN: "Accenture ACN", CTSH: "Cognizant CTSH"},
        source_ref=WEB_SEARCH_SOURCE_REF,
        max_calls_24h=max_calls_24h,
        acquisition=acquisition,
        specs=[{
            "spec_ref": "management-changes",
            "query_template": "{terms} CEO CFO appointment resignation", "lookback_days": 90,
            "rediscovery_interval_days": 7, "retry_interval_days": 1,
        }],
    )


def mission_with_web_status(state: dict, *, status: str, grant: bool, version: int, prior: dict | None) -> dict:
    params = mission_params(state)
    if grant:
        params["autonomy"]["may_write"] = list(params["autonomy"]["may_write"]) + ["source_discovery"]
    for item in params["source_plan"]:
        if item["source_ref"] == WEB_SEARCH_SOURCE_REF:
            item["status"] = status
    if version > 1:
        params.update({
            "version_id": f"coverage-mission-version:us-it-services:{version}",
            "prior_version_ref": prior["id"],
            "idempotency_key": f"coverage-mission:us-it-services:{version}",
        })
    return params


class DiscoveryPlanV2Tests(unittest.TestCase):
    def test_committed_web_plan_loads_and_binds_hash(self) -> None:
        plan = load_discovery_plan(WEB_PLAN_PATH)
        self.assertEqual((plan["schema_version"], plan["source_ref"]), ("0.3", WEB_SEARCH_SOURCE_REF))
        self.assertEqual(plan["id"], "discovery-plan:us-it-services:web-search:3")
        # Searches and page fetches share this window (P9d-4b); this is the
        # owner's selected budget, rather than a validator-imposed ceiling.
        self.assertEqual(plan["budget"], {"max_calls_24h": 1000})
        self.assertEqual(sorted(plan["companies"]), sorted(load_discovery_plan(
            ROOT / "deploy/phase9/p9d-us-it-services-discovery-plan-v1.json")["companies"]))
        self.assertEqual([spec["spec_ref"] for spec in plan["specs"]],
                         ["industry-demand", "competitive-landscape", "management-changes"])
        self.assertTrue(all("document_type" not in spec for spec in plan["specs"]))
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan({**plan, "budget": {"max_calls_24h": 21}})
        schema = json.loads((ROOT / "contracts/mission-discovery-plan.schema.json").read_text())
        self.assertEqual(set(plan), set(schema["required"]) | {"budget", "acquisition"})
        self.assertTrue(set(plan) <= set(schema["properties"]))

    def test_plan_versions_and_shapes_fail_closed(self) -> None:
        # The committed 0.1 AlphaEngine plan is untouched by the 0.2 rules.
        alpha = plan_for_tests()
        self.assertEqual(alpha["schema_version"], "0.1")
        self.assertNotIn("budget", alpha)
        web = web_plan_for_tests()
        bad_cases = {
            "0.1 cannot name web search": {**{k: v for k, v in web.items() if k != "budget"}, "schema_version": "0.1"},
            "0.2 needs a budget": {k: v for k, v in web.items() if k != "budget"},
            "budget outside storage range": {**web, "budget": {"max_calls_24h": 9223372036854775808}},
            "web spec cannot carry document_type": {
                **web, "specs": [{**web["specs"][0], "document_type": "news"}],
            },
            "unknown source": {**web, "source_ref": "source:not-a-discovery-source"},
        }
        for label, value in bad_cases.items():
            body = {k: v for k, v in value.items() if k != "content_hash"}
            from dalton_core.store import content_hash
            with self.subTest(label=label), self.assertRaises(DiscoveryPlanError):
                validate_discovery_plan({**body, "content_hash": content_hash(body)})
        # An AlphaEngine plan may adopt 0.2 to carry its own budget.
        alpha_v2 = build_discovery_plan(
            plan_id="discovery-plan:x:alphaengine:2", created_at=NOW.isoformat(timespec="microseconds"),
            mission_ref="coverage-mission:us-it-services", companies={ACN: "Accenture ACN"},
            specs=alpha["specs"], max_calls_24h=5,
        )
        self.assertEqual((alpha_v2["schema_version"], alpha_v2["source_ref"]), ("0.2", ALPHAENGINE_SOURCE_REF))
        with self.assertRaises(DiscoveryPlanError):
            build_discovery_plan(
                plan_id="discovery-plan:x:web:1", created_at=NOW.isoformat(timespec="microseconds"),
                mission_ref="coverage-mission:us-it-services", companies={ACN: "Accenture ACN"},
                source_ref=WEB_SEARCH_SOURCE_REF, specs=web["specs"],
            )

        governed = build_discovery_plan(
            plan_id="discovery-plan:x:web:large-budget",
            created_at=NOW.isoformat(timespec="microseconds"),
            mission_ref="coverage-mission:us-it-services",
            companies={ACN: "Accenture ACN"}, source_ref=WEB_SEARCH_SOURCE_REF,
            specs=web["specs"], max_calls_24h=5000,
        )
        self.assertEqual(validate_discovery_plan(governed)["budget"]["max_calls_24h"], 5000)

    def test_parameters_compile_to_a_dated_search_web_spec(self) -> None:
        plan = web_plan_for_tests()
        params = build_discovery_parameters(plan, spec_ref="management-changes", company_ref=ACN, as_of=date(2026, 9, 6))
        self.assertEqual(params, {
            "query": "Accenture ACN CEO CFO appointment resignation",
            "date_after": "2026-06-08", "date_before": "2026-09-06",
        })
        self.assertEqual(discovery_query_hash(plan, params), web_search_spec_hash(params))
        with self.assertRaises(DiscoveryPlanError):
            build_discovery_parameters(plan, spec_ref="management-changes", company_ref="company:other", as_of=date(2026, 9, 6))


    def test_plan_0_3_acquisition_policy_is_closed_and_web_only(self) -> None:
        """P9d-13: preferred / skipped hosts are plan policy, hash bound like the rest."""

        plan = web_plan_for_tests(acquisition={"preferred_hosts": ["newsroom.accenture.com"], "skip_hosts": ["news.alphastreet.com"]})
        self.assertEqual(plan["schema_version"], "0.3")
        self.assertEqual(plan["acquisition"], {"preferred_hosts": ["newsroom.accenture.com"], "skip_hosts": ["news.alphastreet.com"]})
        self.assertEqual(validate_discovery_plan(plan), plan)
        for bad in (
            {"preferred_hosts": ["a.example"], "skip_hosts": ["a.example"]},   # both
            {"preferred_hosts": ["Newsroom.Accenture.com"], "skip_hosts": []},   # case
            {"preferred_hosts": ["https://a.example/x"], "skip_hosts": []},     # not a host
            {"preferred_hosts": ["a.example", "a.example"], "skip_hosts": []},  # duplicate
        ):
            with self.assertRaises(DiscoveryPlanError, msg=str(bad)):
                web_plan_for_tests(acquisition=bad)
        with self.assertRaises(DiscoveryPlanError):  # not closed
            validate_discovery_plan({**plan, "acquisition": {**plan["acquisition"], "extra": []}})
        # A 0.2 plan cannot smuggle a policy, and AlphaEngine plans never carry one.
        v2 = web_plan_for_tests()
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan({**v2, "acquisition": plan["acquisition"]})
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan({**plan, "source_ref": ALPHAENGINE_SOURCE_REF})
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan({**plan, "acquisition": {"preferred_hosts": ["b.example"], "skip_hosts": []}})
        committed = load_discovery_plan(WEB_PLAN_PATH)
        self.assertEqual(committed["schema_version"], "0.3")
        self.assertIn("newsroom.accenture.com", committed["acquisition"]["preferred_hosts"])
        self.assertEqual(committed["acquisition"]["skip_hosts"], ["news.alphastreet.com", "www.spglobal.com"])
        schema = json.loads((ROOT / "contracts/mission-discovery-plan.schema.json").read_text())
        self.assertIn("0.3", schema["properties"]["schema_version"]["enum"])
        self.assertEqual(set(schema["properties"]["acquisition"]["required"]), {"preferred_hosts", "skip_hosts"})

    def test_plan_0_5_binds_configurable_failure_cooldown(self) -> None:
        plan = build_discovery_plan(
            plan_id="discovery-plan:x:web:cooldown", created_at=NOW.isoformat(timespec="microseconds"),
            mission_ref="coverage-mission:us-it-services", companies={ACN: "Accenture ACN"},
            source_ref=WEB_SEARCH_SOURCE_REF, specs=web_plan_for_tests()["specs"],
            max_calls_24h=10, acquisition={"preferred_hosts": [], "skip_hosts": []},
            failure_cooldown={"minimum_distinct_urls": 3, "window_seconds": 86400,
                              "cooldown_seconds": 21600},)
        self.assertEqual(plan["schema_version"], "0.5")
        self.assertEqual(validate_discovery_plan(plan), plan)
        broken = json.loads(json.dumps(plan)); broken["acquisition"]["failure_cooldown"]["extra"] = 1
        broken["content_hash"] = "0" * 64
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(broken)


class WebDiscoveryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.clock = Clock()
        self.h = WebSearchHarness(root, FakeWebSearchHandle(CITATIONS), clock=self.clock)
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        self.plan = web_plan_for_tests()

    def test_source_table_and_grant_rules(self) -> None:
        self.assertEqual(
            set(DISCOVERY_SOURCES),
            {ALPHAENGINE_SOURCE_REF, WEB_SEARCH_SOURCE_REF, "source:sec-edgar", "source:guidepoint",
             "source:sales-notes", "source:company-wiki",
             "source:prior-research"},
        )
        params = mission_params(self.state)
        ref = params.pop("mission_ref")
        v1 = self.missions.create_mission(ref, **params)
        # v1 marks web search not_connected: nobody may search, not even a human.
        for requester in (OWNER, AUTOMATION):
            with self.subTest(requester=requester), self.assertRaises(CoverageMissionConflict):
                self.missions.authorize_source_discovery(company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=requester)
        # A source outside the discovery table is never a discovery source.
        # P10r used to make this point with source:sec-edgar, which was in the
        # mission's source plan and connected but not a discovery source. The
        # filings index made it one, and every other connected source is now in
        # the table too, so the point is made with a source the mission never
        # declared -- which is the same rule, and unambiguous about why it is
        # refused.
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(
                company_ref=ACN, source_ref="source:bloomberg", requested_by=OWNER
            )
        # probe_only: human rehearsal allowed, automation refused.
        p2 = mission_with_web_status(self.state, status="probe_only", grant=True, version=2, prior=v1)
        p2.pop("mission_ref")
        v2 = self.missions.create_mission(ref, **p2)
        human = self.missions.authorize_source_discovery(company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=OWNER)
        self.assertEqual((human["source_ref"], human["actor_ref"], human["scope"]), (WEB_SEARCH_SOURCE_REF, AUTOMATION, "source_discovery"))
        with self.assertRaises(CoverageMissionConflict):
            self.missions.authorize_source_discovery(company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=AUTOMATION)
        # connected + source_discovery: automation allowed.
        p3 = mission_with_web_status(self.state, status="connected", grant=True, version=3, prior=v2)
        p3.pop("mission_ref")
        self.missions.create_mission(ref, **p3)
        auto = self.missions.authorize_source_discovery(company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=AUTOMATION)
        self.assertEqual(auto["requested_by"], AUTOMATION)

    def test_discovery_record_binds_search_web_envelope_and_queues_urls(self) -> None:
        params = mission_params(self.state)
        ref = params.pop("mission_ref")
        v1 = self.missions.create_mission(ref, **params)
        p2 = mission_with_web_status(self.state, status="connected", grant=True, version=2, prior=v1)
        p2.pop("mission_ref")
        mission = self.missions.create_mission(ref, **p2)
        authorization = self.missions.authorize_source_discovery(
            company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=AUTOMATION,
        )
        spec_params = build_discovery_parameters(self.plan, spec_ref="management-changes", company_ref=ACN, as_of=self.clock().date())
        receipt = self.h.search.search(self.h.search.build_request(spec_params))
        self.assertEqual(receipt["document_refs"], [URL_A, URL_B])
        present = public_web_urls_in_authority(self.h.core.connection, receipt["document_refs"])
        self.assertEqual(present, [])
        common = dict(
            authorization=authorization, discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"], spec_ref="management-changes",
            query_hash=web_search_spec_hash(spec_params), parameters=spec_params,
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
        )
        record = self.missions.record_source_discovery(
            **common, document_refs=receipt["document_refs"], in_authority_document_refs=present,
        )
        self.assertEqual((record["status"], record["source_ref"]), ("fresh", WEB_SEARCH_SOURCE_REF))
        self.assertEqual(record["new_document_refs"], [URL_A, URL_B])
        self.assertEqual(record["parameters"], spec_params)
        wire = {k: v for k, v in record.items() if k != "status"}
        validate_mission_source_discovery(wire)
        schema = json.loads((ROOT / "contracts/coverage-mission-source-discovery.schema.json").read_text())
        self.assertEqual(set(schema["required"]), set(wire))
        # Ref shape is bound to the source: an AlphaEngine ref under web search fails closed.
        from dalton_core.store import content_hash
        forged = {**wire, "document_refs": ["alphaengine-doc:1"], "new_document_refs": ["alphaengine-doc:1"], "in_authority_document_refs": []}
        forged.pop("content_hash")
        with self.assertRaises(CoverageMissionValidationError):
            validate_mission_source_discovery({**forged, "content_hash": content_hash(forged)})
        forged = {**wire, "parameters": {"query": "x", "filters": {}, "cursor": None}}
        forged.pop("content_hash")
        with self.assertRaises(CoverageMissionValidationError):
            validate_mission_source_discovery({**forged, "content_hash": content_hash(forged)})
        documents = self.missions.discovered_documents(mission["id"])
        self.assertEqual(
            sorted((d["source_ref"], d["document_ref"], d["status"]) for d in documents),
            sorted([(WEB_SEARCH_SOURCE_REF, URL_A, "discovered"), (WEB_SEARCH_SOURCE_REF, URL_B, "discovered")]),
        )
        # Source filters keep the two lanes apart.
        self.assertIsNone(self.missions.next_discovered_document(source_ref=ALPHAENGINE_SOURCE_REF))
        self.assertEqual(self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)["document_ref"], URL_A)
        self.assertEqual(self.missions.next_discovered_document()["document_ref"], URL_A)
        progress = self.missions.mission_progress(mission["mission_ref"])
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual((acn["discovery_count"], acn["discovered_document_count"], acn["acquired_document_count"]), (1, 2, 0))


class FakeWebSearchLauncher:
    """Stands in for the child: runs the governed web search in-process on start()."""

    def __init__(self, harness: WebSearchHarness, missions: CoverageMissionAuthority, plan: dict) -> None:
        self.h = harness
        self.missions = missions
        self.plan = plan
        self.tickets: dict[str, dict] = {}
        self.starts: list[dict] = []
        # P9d-13: the real child records each URL's host on the ledger row.
        # False reproduces rows written before the ledger carried a host.
        self.with_hosts = True

    def running(self) -> bool:
        return any(ticket["status"] == "running" for ticket in self.tickets.values())

    def start(self, *, authorization, spec_ref, as_of=None):
        self.starts.append({"authorization": dict(authorization), "spec_ref": spec_ref})
        ticket_id = f"web-search-discovery:{len(self.starts):024x}"
        params = build_discovery_parameters(self.plan, spec_ref=spec_ref, company_ref=authorization["company_ref"], as_of=as_of)
        receipt = self.h.search.search(self.h.search.build_request(params))
        present = public_web_urls_in_authority(self.h.core.connection, receipt["document_refs"])
        record = self.missions.record_source_discovery(
            authorization=authorization, discovery_plan_ref=self.plan["id"],
            discovery_plan_hash=self.plan["content_hash"], spec_ref=spec_ref,
            query_hash=web_search_spec_hash(params), parameters=params,
            connector_invocation_ref=receipt["connector_invocation_ref"],
            connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"],
            source_envelope_hash=receipt["source_envelope_hash"],
            document_refs=receipt["document_refs"], in_authority_document_refs=present,
            document_hosts=(
                {a["url_ref"]: a["host"] for a in self.h.search.url_authorities(receipt["source_envelope_ref"])}
                if self.with_hosts else None
            ),
        )
        summary = {"discovery_ref": record["id"], "new_document_count": len(record["new_document_refs"]), "failure_reason": None}
        self.tickets[ticket_id] = {"id": ticket_id, "status": "succeeded", "exit_code": 0, "summary": summary}
        return {"id": ticket_id, "status": "running"}

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


class FailedAfterSuccessfulWebSearchLauncher:
    """Persist a real successful search, then reproduce a local ledger failure."""

    def __init__(self, harness: WebSearchHarness, plan: dict) -> None:
        self.h = harness
        self.plan = plan
        self.tickets: dict[str, dict] = {}
        self.starts: list[dict] = []

    def running(self) -> bool:
        return False

    def start(self, *, authorization, spec_ref, as_of=None):
        self.starts.append({"authorization": dict(authorization), "spec_ref": spec_ref})
        ticket_id = f"web-search-discovery:{len(self.starts):024x}"
        params = build_discovery_parameters(
            self.plan, spec_ref=spec_ref, company_ref=authorization["company_ref"], as_of=as_of,
        )
        receipt = self.h.search.search(self.h.search.build_request(params))
        governance = self.h.search.governance
        common = {
            "id": ticket_id, "company_ref": authorization["company_ref"],
            "spec_ref": spec_ref, "requested_by": authorization["requested_by"],
            "actor_ref": authorization["actor_ref"],
            "mission_version_ref": authorization["mission_version_ref"],
            "mission_version_hash": authorization["mission_version_hash"],
            "as_of": as_of.isoformat(), "governance_ref": governance.id,
            "governance_hash": governance.content_hash, "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
        }
        summary = {
            "schema_version": "0.1", "created_at": self.h.clock().isoformat(),
            "source_ref": WEB_SEARCH_SOURCE_REF, "transport": "rehearsal",
            "expected_provider": "gemini", "provider_selection_policy": "test",
            "governance_ref": governance.id, "governance_hash": governance.content_hash,
            "governance_status": governance.status, "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"], "company_ref": authorization["company_ref"],
            "spec_ref": spec_ref, "requested_by": authorization["requested_by"],
            "as_of": as_of.isoformat(), "status": "failed",
            "failure_reason": "unexpected local registration failure",
            "authorization": dict(authorization), "parameters": params,
            "query_hash": web_search_spec_hash(params),
            "search": {key: receipt[key] for key in (
                "request_hash", "connector_profile_ref", "connector_invocation_ref",
                "connector_invocation_hash", "runner_response_ref", "outcome", "replayed",
                "source_envelope_ref", "source_envelope_hash", "raw_artifact_version_ref",
                "document_refs", "next_cursor", "source_status",
            )},
            "discovery_ref": None, "discovery_hash": None, "discovery_status": None,
            "document_count": 0, "new_document_count": 0,
            "in_authority_document_count": 0, "discovered_urls": [],
            "provider_calls": receipt["provider_calls"], "production_activated": False,
            "formal_authority_writes": 0,
        }
        self.tickets[ticket_id] = {
            **common, "schema_version": "0.1", "transport": "rehearsal",
            "started_at": self.h.clock().isoformat(), "pid": 999999,
            "status": "failed", "exit_code": 1,
            "completed_at": self.h.clock().isoformat(), "summary": summary,
        }
        return {"id": ticket_id, "status": "running"}

    def status(self, ticket_ref):
        return json.loads(json.dumps(self.tickets[ticket_ref]))


class WebCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.clock = Clock()
        self.h = WebSearchHarness(root, FakeWebSearchHandle(CITATIONS), clock=self.clock)
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        self.plan = web_plan_for_tests(max_calls_24h=1)
        self.launcher = FakeWebSearchLauncher(self.h, self.missions, self.plan)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.launcher, acquisition_launcher=None, clock=self.clock,
        )

    def publish(self, *, status: str, grant: bool, version: int = 1, prior: dict | None = None) -> dict:
        params = mission_with_web_status(self.state, status=status, grant=grant, version=version, prior=prior)
        ref = params.pop("mission_ref")
        return self.missions.create_mission(ref, **params)

    def test_explicit_plan_budget_above_old_display_ceiling_reserves_normally(self):
        self.plan = web_plan_for_tests(max_calls_24h=5000)
        self.launcher = FakeWebSearchLauncher(self.h, self.missions, self.plan)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.launcher, acquisition_launcher=None, clock=self.clock,
        )
        mission = self.publish(status="connected", grant=True)
        launched = self.coordinator.launch_discovery()
        self.assertEqual(launched["status"], "launched")
        budget = self.coordinator._budget(mission["budget"]["max_alphaengine_calls_24h"])
        self.assertEqual(budget, {"spent": 1, "cap": 5000, "remaining": 4998,
                                  "reserved": 1})

    def test_documents_stranded_by_a_mission_version_change_are_carried_forward(self) -> None:
        """P9d-12: publishing a new version must not orphan the prior version's queue.

        Live, v3 -> v4 stranded ten discovered URLs because every acquisition
        query joins the active pointer.  The tick now re-registers them under
        the current version's own grant, and reports the ones it refuses.
        """

        v1 = self.publish(status="connected", grant=True)
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_dispatches"][0]["new_document_count"], 2)
        self.assertEqual(tick["carried_forward"], [])
        under_v1 = self.missions.discovered_documents(v1["id"])
        self.assertEqual([d["status"] for d in under_v1], ["discovered", "discovered"])
        self.assertEqual(sorted(d["host"] for d in under_v1), ["example.com", "news.example.org"])

        v2 = self.publish(status="connected", grant=True, version=2, prior=v1)
        tick = self.coordinator.dispatch_once()
        carried = tick["carried_forward"]
        self.assertEqual(sorted((c["document_ref"], c["status"]) for c in carried),
                         sorted([(URL_A, "discovered"), (URL_B, "discovered")]))
        self.assertTrue(all(c["from_version_ref"] == v1["id"] for c in carried))
        under_v2 = self.missions.discovered_documents(v2["id"])
        self.assertEqual(len(under_v2), 2)
        # Same ref, same discovery (the only route back to the URL), same
        # host and timestamps, so queue order is preserved; a fresh record id.
        by_ref = {d["document_ref"]: d for d in under_v1}
        for doc in under_v2:
            original = by_ref[doc["document_ref"]]
            self.assertEqual((doc["discovery_ref"], doc["host"], doc["created_at"]),
                             (original["discovery_ref"], original["host"], original["created_at"]))
            self.assertNotEqual(doc["record_id"], original["record_id"])
        self.assertEqual(self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)["mission_version_ref"], v2["id"])
        # Idempotent: nothing is copied twice, and the old rows are untouched.
        self.assertEqual(self.coordinator.dispatch_once()["carried_forward"], [])
        self.assertEqual([d["status"] for d in self.missions.discovered_documents(v1["id"])], ["discovered", "discovered"])

        # The same document under two superseded versions is carried once,
        # from the most recent version.  Live, the second copy hit the UNIQUE
        # constraint and took the whole discovery tick down.
        v3 = self.publish(status="connected", grant=True, version=3, prior=v2)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(sorted(c["document_ref"] for c in tick["carried_forward"]), sorted([URL_A, URL_B]))
        self.assertTrue(all(c["from_version_ref"] == v2["id"] for c in tick["carried_forward"]))
        self.assertEqual(len(self.missions.discovered_documents(v3["id"])), 2)
        self.assertEqual(self.coordinator.dispatch_once()["carried_forward"], [])
        # A version that no longer lets automation discover on this source
        # refuses the carry-forward and says why, rather than moving rows the
        # grant would not cover.
        self.publish(status="probe_only", grant=True, version=4, prior=v3)
        tick = self.coordinator.dispatch_once()
        self.assertEqual({c["status"] for c in tick["carried_forward"]}, {"skipped"})
        self.assertTrue(all("probe_only" in c["reason"] for c in tick["carried_forward"]))
        self.assertEqual(self.missions.discovered_documents(self.missions.active_mission("coverage-mission:us-it-services")["id"]), [])

    def test_not_connected_then_connected_cycle_with_plan_budget(self) -> None:
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["source_ref"], tick["discovery"]["status"]), (WEB_SEARCH_SOURCE_REF, "no_active_mission"))
        v1 = self.publish(status="not_connected", grant=False)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "not_authorized")
        self.assertIn("not_connected", tick["discovery"]["reason"])
        self.assertEqual(tick["acquisition"]["status"], "unconfigured")
        self.assertFalse(tick["acquisition"]["queued"])
        self.assertEqual(self.launcher.starts, [])

        self.publish(status="connected", grant=True, version=2, prior=v1)
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["status"], tick["discovery"]["status"], tick["discovery"]["company_ref"]),
                         ("launched", "launched", ACN))
        self.assertEqual(tick["discovery"]["budget"], {"spent": 0, "cap": 1, "remaining": 1, "reserved": 0})
        self.assertEqual(self.launcher.starts[0]["authorization"]["requested_by"], AUTOMATION)

        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_dispatches"]], ["succeeded"])
        self.assertEqual(tick["settled_dispatches"][0]["new_document_count"], 2)
        # URLs stay queued: no fetch lane yet, and this is reported, not hidden.
        self.assertEqual(tick["acquisition"]["status"], "unconfigured")
        self.assertTrue(tick["acquisition"]["queued"])
        self.assertIn("fetch launcher is not configured", tick["acquisition"]["reason"])
        # The plan's own 24h cap (1 call) now blocks CTSH; nothing else was spent.
        self.assertEqual(tick["discovery"]["status"], "budget_exhausted")
        self.assertEqual(tick["discovery"]["budget"], {"spent": 1, "cap": 1, "remaining": 0, "reserved": 0})
        self.assertEqual(len(self.h.handle.calls), 1)
        documents = self.missions.discovered_documents(self.missions.active_mission("coverage-mission:us-it-services")["id"])
        self.assertEqual(sorted(d["status"] for d in documents), ["discovered", "discovered"])
        # Past the trailing window the cap frees up and CTSH is searched; ACN
        # waits out its 7-day cadence.
        self.clock.advance(hours=25)
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["discovery"]["status"], tick["discovery"]["company_ref"]), ("launched", CTSH))
        self.assertTrue(any("rediscovered" in item["reason"] for item in tick["discovery"]["skipped"]))
        self.assertEqual(len(self.h.handle.calls), 2)

    def test_successful_partial_search_recovers_local_record_without_another_call(self) -> None:
        # Thirteen ranked results normalize to the admitted top ten and a
        # partial SourceEnvelope.  The provider work succeeds; only the local
        # mission discovery write is absent.
        root = Path(self.temp.name) / "partial-recovery"
        root.mkdir()
        citations = [{"url": f"https://example.com/source/{index}"} for index in range(13)]
        harness = WebSearchHarness(root, FakeWebSearchHandle(citations), clock=self.clock)
        self.addCleanup(harness.close)
        state = bootstrap_method_authorities(harness.core)
        missions = CoverageMissionAuthority(harness.core)
        plan = web_plan_for_tests(max_calls_24h=1)
        launcher = FailedAfterSuccessfulWebSearchLauncher(harness, plan)
        coordinator = MissionSourceDiscoveryCoordinator(
            store=harness.core, missions=missions, plan=plan,
            search_launcher=launcher, acquisition_launcher=None, clock=self.clock,
            spool_dir=root / "spool",
        )
        params = mission_with_web_status(
            state, status="connected", grant=True, version=1, prior=None,
        )
        mission_ref = params.pop("mission_ref")
        mission = missions.create_mission(mission_ref, **params)

        first = coordinator.dispatch_once()
        self.assertEqual("launched", first["discovery"]["status"])
        self.assertEqual(1, len(harness.handle.calls))
        self.assertEqual([], missions.source_discoveries(mission["id"]))

        second = coordinator.dispatch_once()
        self.assertEqual("failed", second["settled_dispatches"][0]["status"])
        self.assertEqual("recovered", second["recovered_dispatches"][0]["status"])
        self.assertEqual(0, second["recovered_dispatches"][0]["provider_calls"])
        self.assertEqual(1, len(harness.handle.calls))
        discoveries = missions.source_discoveries(mission["id"])
        self.assertEqual(1, len(discoveries))
        self.assertEqual(10, len(discoveries[0]["document_refs"]))
        documents = missions.discovered_documents(mission["id"])
        self.assertEqual(10, len(documents))
        self.assertTrue(all(row["status"] == "discovered" for row in documents))
        self.assertTrue(all(row["host"] == "example.com" for row in documents))

        # The failed history stays failed and the same envelope is not
        # published or queued a second time on later ticks.
        with mock.patch.object(
            missions, "record_source_discovery",
            side_effect=AssertionError("an existing envelope must be skipped"),
        ):
            third = coordinator.dispatch_once()
        self.assertEqual("already_recovered", third["recovered_dispatches"][0]["status"])
        self.assertEqual(1, len(missions.source_discoveries(mission["id"])))
        self.assertEqual(10, len(missions.discovered_documents(mission["id"])))
        self.assertEqual(1, len(harness.handle.calls))
        dispatch = missions.discovery_dispatches(mission["id"], limit=1)[0]
        self.assertEqual("failed", dispatch["status"])

    def test_local_recovery_rejects_tamper_pending_and_stale_mission(self) -> None:
        root = Path(self.temp.name) / "refused-recovery"
        root.mkdir()
        harness = WebSearchHarness(root, FakeWebSearchHandle(CITATIONS), clock=self.clock)
        self.addCleanup(harness.close)
        state = bootstrap_method_authorities(harness.core)
        missions = CoverageMissionAuthority(harness.core)
        plan = web_plan_for_tests(max_calls_24h=1)
        launcher = FailedAfterSuccessfulWebSearchLauncher(harness, plan)
        coordinator = MissionSourceDiscoveryCoordinator(
            store=harness.core, missions=missions, plan=plan,
            search_launcher=launcher, acquisition_launcher=None, clock=self.clock,
            spool_dir=root / "spool",
        )
        params = mission_with_web_status(
            state, status="connected", grant=True, version=1, prior=None,
        )
        mission_ref = params.pop("mission_ref")
        mission = missions.create_mission(mission_ref, **params)
        coordinator.dispatch_once()
        coordinator.settle_dispatches()
        ticket = next(iter(launcher.tickets.values()))

        ticket["summary"]["parameters"]["query"] = "foreign query"
        refused = coordinator.recover_local_web_discoveries()
        self.assertEqual("refused", refused[0]["status"])
        self.assertIn("query binding drifted", refused[0]["reason"])
        self.assertEqual([], missions.source_discoveries(mission["id"]))

        ticket["summary"]["parameters"] = build_discovery_parameters(
            plan, spec_ref="management-changes", company_ref=ACN, as_of=self.clock().date(),
        )
        ticket["status"] = "running"
        refused = coordinator.recover_local_web_discoveries()
        self.assertEqual("refused", refused[0]["status"])
        self.assertIn("exact failed child ticket", refused[0]["reason"])
        self.assertEqual(1, len(harness.handle.calls))

        ticket["status"] = "failed"
        ticket["summary"]["search"]["outcome"] = "failed"
        refused = coordinator.recover_local_web_discoveries()
        self.assertEqual("refused", refused[0]["status"])
        self.assertIn("does not prove a successful search", refused[0]["reason"])
        self.assertEqual([], missions.source_discoveries(mission["id"]))
        ticket["summary"]["search"]["outcome"] = "succeeded"
        envelope_ref = ticket["summary"]["search"]["source_envelope_ref"]
        envelope = json.loads(harness.core.connection.execute(
            "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?",
            (envelope_ref,),
        ).fetchone()[0])
        raw_hash = envelope["raw_response_hash"]
        raw_path = root / "spool" / "connector-spool" / "objects" / raw_hash[:2] / raw_hash
        raw_path.write_bytes(b"tampered")
        refused = coordinator.recover_local_web_discoveries()
        self.assertEqual("refused", refused[0]["status"])
        self.assertIn("raw response hash drifted", refused[0]["reason"])
        self.assertEqual([], missions.source_discoveries(mission["id"]))
        next_params = mission_with_web_status(
            state, status="connected", grant=True, version=2, prior=mission,
        )
        next_params.pop("mission_ref")
        missions.create_mission(mission_ref, **next_params)
        # Failed proof from the superseded mission is not even selected by the
        # active-version scan, so it cannot be relabelled into the new mission.
        self.assertEqual([], coordinator.recover_local_web_discoveries())
        self.assertEqual([], missions.source_discoveries(mission["id"]))
        self.assertEqual(1, len(harness.handle.calls))

    def test_alphaengine_coordinator_ignores_web_dispatches_and_documents(self) -> None:
        v1 = self.publish(status="not_connected", grant=False)
        self.publish(status="connected", grant=True, version=2, prior=v1)
        self.coordinator.dispatch_once()
        self.coordinator.dispatch_once()
        # An AlphaEngine coordinator sharing the Core sees nothing of the web lane.
        alpha_root = Path(self.temp.name) / "alpha"
        alpha_root.mkdir()
        alpha = SearchHarness(alpha_root, [], clock=self.clock)
        self.addCleanup(alpha.close)
        alpha_missions = CoverageMissionAuthority(self.h.core)
        alpha_plan = plan_for_tests()
        alpha_coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=alpha_missions, plan=alpha_plan,
            search_launcher=FakeSearchLauncher(alpha, alpha_missions, alpha_plan),
            acquisition_launcher=None, clock=self.clock,
        )
        self.assertEqual(alpha_coordinator.settle_dispatches(), [])
        self.assertEqual(alpha_coordinator.launch_acquisition()["status"], "unconfigured")
        self.assertIsNone(alpha_missions.next_discovered_document(source_ref=ALPHAENGINE_SOURCE_REF))
        self.assertEqual(alpha_missions.open_discovery_dispatches(source_ref=ALPHAENGINE_SOURCE_REF), [])
        self.assertEqual(alpha_coordinator._reserved_calls(), 0)


class WebSearchChildTests(unittest.TestCase):
    """The real launcher spawns the real child in fake-citations mode."""

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
        v1 = missions.create_mission(ref, **params)
        p2 = mission_with_web_status(self.method, status="probe_only", grant=False, version=2, prior=v1)
        p2.pop("mission_ref")
        self.mission = missions.create_mission(ref, **p2)
        core.close()
        self.plan = web_plan_for_tests()
        self.plan_path = self.root / "web-plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.governance_path = self.root / "web-governance.json"
        self.governance_path.write_text(
            canonical_json(build_web_search_governance_record(approved_by="human:lumos", status="approved")) + "\n",
            encoding="utf-8",
        )
        self.citations_path = self.root / "citations.json"
        self.citations_path.write_text(json.dumps(CITATIONS), encoding="utf-8")

    def authorization(self) -> dict:
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            return CoverageMissionAuthority(core).authorize_source_discovery(
                company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=OWNER,
            )
        finally:
            core.close()

    def test_launcher_refuses_network_other_source_and_proposed_governance(self) -> None:
        authorization = self.authorization()
        live = WebSearchLauncher(state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path)
        with self.assertRaises(DiscoveryLaunchRejected) as ctx:
            live.start(authorization=authorization, spec_ref="management-changes")
        self.assertIn("broker is not configured", str(ctx.exception))
        alpha_plan_path = self.root / "alpha-plan.json"
        alpha_plan_path.write_text(json.dumps(plan_for_tests()), encoding="utf-8")
        wrong = WebSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=alpha_plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
        )
        with self.assertRaises(DiscoveryLaunchRejected):
            wrong.start(authorization=authorization, spec_ref="earnings-call-transcripts")
        proposed = self.root / "proposed.json"
        proposed.write_text(canonical_json(build_web_search_governance_record(approved_by="human:lumos")) + "\n")
        unapproved = WebSearchLauncher(
            state_dir=self.state, governance_path=proposed, plan_path=self.plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
        )
        with self.assertRaises(DiscoveryLaunchRejected):
            unapproved.start(authorization=authorization, spec_ref="management-changes")
        self.assertFalse((self.state / "discoveries").exists() and any((self.state / "discoveries").iterdir()))
        # The child itself also refuses a networked run with a fixed reason and spends nothing.
        summary_dir = self.root / "net-summary"
        code = web_search_cli_main([
            "--state-dir", str(self.state), "--governance", str(self.governance_path),
            "--discovery-plan", str(self.plan_path), "--company-ref", ACN,
            "--spec-ref", "management-changes", "--requested-by", OWNER,
            "--mission-version-ref", self.mission["id"], "--mission-version-hash", self.mission["content_hash"],
            "--allow-network", "--summary-dir", str(summary_dir), "--quiet",
        ])
        summary = json.loads((summary_dir / "summary.json").read_text())
        self.assertEqual((code, summary["status"], summary["failure_reason"], summary["provider_calls"]),
                         (1, "failed", NETWORK_UNAVAILABLE_REASON, 0))
        self.assertIn("broker", NETWORK_UNAVAILABLE_REASON)

    def test_finished_search_child_is_adopted_after_a_writer_restart(self) -> None:
        """P9d-14: same rule as the fetch lane; six search tickets were lost this way live."""

        launcher = WebSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
        )
        ticket = launcher.start(authorization=self.authorization(), spec_ref="management-changes", as_of=date(2026, 9, 6))
        self.assertEqual(launcher.wait(timeout=120), 0)
        self.assertEqual(launcher.status(ticket["id"])["status"], "succeeded")
        ticket_path = self.state / "discoveries" / ticket["id"].split(":", 1)[1] / "ticket.json"
        record = json.loads(ticket_path.read_text(encoding="utf-8"))
        record.update({"status": "running", "exit_code": None, "completed_at": None, "pid": 2**22 - 1})
        ticket_path.write_text(json.dumps(record), encoding="utf-8")
        fresh = WebSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
        )
        adopted = fresh.status(ticket["id"])
        self.assertEqual((adopted["status"], adopted["exit_code"], adopted["adopted_from_summary"]), ("succeeded", 0, True))
        self.assertEqual(adopted["summary"]["discovery_ref"], launcher.status(ticket["id"])["summary"]["discovery_ref"])
        ticket_path.write_text(json.dumps(record), encoding="utf-8")
        ticket_path.with_name("summary.json").unlink()
        self.assertEqual(fresh.status(ticket["id"])["status"], "orphaned")

    def test_child_records_web_discovery_under_human_request(self) -> None:
        launcher = WebSearchLauncher(
            state_dir=self.state, governance_path=self.governance_path, plan_path=self.plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
        )
        ticket = launcher.start(authorization=self.authorization(), spec_ref="management-changes", as_of=date(2026, 9, 6))
        self.assertEqual((ticket["status"], ticket["requested_by"], ticket["transport"]), ("running", OWNER, "rehearsal"))
        self.assertTrue(ticket["id"].startswith("web-search-discovery:"))
        code = launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual((code, status["status"]), (0, "succeeded"), status)
        summary = status["summary"]
        self.assertEqual((summary["status"], summary["source_ref"], summary["transport"]), ("succeeded", WEB_SEARCH_SOURCE_REF, "fake"))
        self.assertEqual((summary["new_document_count"], summary["provider_calls"], summary["formal_authority_writes"]), (2, 1, 0))
        self.assertEqual([item["canonical_url"] for item in summary["discovered_urls"]],
                         ["https://example.com/investors?q=ai", "https://news.example.org/accenture-ai"])
        self.assertEqual(summary["parameters"]["date_before"], "2026-09-06")
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            missions = CoverageMissionAuthority(core)
            records = missions.source_discoveries(self.mission["id"], company_ref=ACN)
            self.assertEqual([item["id"] for item in records], [summary["discovery_ref"]])
            self.assertEqual(records[0]["new_document_refs"], [URL_A, URL_B])
            self.assertEqual(core.connection.execute(
                "SELECT COUNT(*) FROM connector_invocations WHERE connector_profile_ref=?", (SEARCH_PROFILE_REF,),
            ).fetchone()[0], 1)
            for table in ("evidence_versions", "claim_versions"):
                self.assertEqual(core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            core.close()
        ticket_path = self.state / "discoveries" / ticket["id"].split(":")[1] / "ticket.json"
        self.assertEqual(ticket_path.stat().st_mode & 0o777, 0o600)
        # Automation under probe_only is refused by the child before spending.
        forged = {**self.authorization(), "requested_by": AUTOMATION}
        ticket = launcher.start(authorization=forged, spec_ref="management-changes")
        launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("probe_only", status["summary"]["failure_reason"])
        self.assertEqual(status["summary"]["provider_calls"], 0)

    def test_antigravity_provider_reaches_the_actual_search_child(self) -> None:
        launcher = WebSearchLauncher(
            state_dir=self.state,
            governance_path=self.governance_path,
            plan_path=self.plan_path,
            mode_args=("--fake-citations-file", str(self.citations_path)),
            expected_provider="antigravity",
        )
        ticket = launcher.start(
            authorization=self.authorization(),
            spec_ref="management-changes",
            as_of=date(2026, 9, 6),
        )
        self.assertEqual(launcher.wait(timeout=120), 0)
        status = launcher.status(ticket["id"])
        self.assertEqual(status["status"], "succeeded")
        self.assertIn(
            ":provider-antigravity:",
            status["summary"]["search"]["connector_profile_ref"],
        )


class P9d4WriterHarness(P9aWriterHarness):
    """P9a harness plus rehearsal AlphaEngine + web search launchers and plans."""

    def __init__(self, root: Path):  # noqa: D107 - mirrors the parent harness
        from dalton_core.alphaengine_core_search import build_search_governance_record
        from dalton_core.mission_source_discovery import AlphaEngineSearchLauncher

        self.socket = str(root / "writer.sock")
        alpha_plan_path = root / "plan.json"
        alpha_plan_path.write_text(json.dumps(plan_for_tests()), encoding="utf-8")
        alpha_governance = root / "search-governance.json"
        alpha_governance.write_text(
            canonical_json(build_search_governance_record(approved_by="human:lumos", status="approved")) + "\n",
            encoding="utf-8",
        )
        results_path = root / "results.json"
        results_path.write_text(json.dumps([{"doc_id": "130000099999999"}]), encoding="utf-8")
        self.alpha_launcher = AlphaEngineSearchLauncher(
            state_dir=root, governance_path=alpha_governance, plan_path=alpha_plan_path,
            mode_args=("--fake-search-file", str(results_path)),
        )
        self.web_plan_path = root / "web-plan.json"
        self.web_plan_path.write_text(json.dumps(web_plan_for_tests()), encoding="utf-8")
        web_governance = root / "web-governance.json"
        web_governance.write_text(
            canonical_json(build_web_search_governance_record(approved_by="human:lumos", status="approved")) + "\n",
            encoding="utf-8",
        )
        citations_path = root / "citations.json"
        citations_path.write_text(json.dumps(CITATIONS), encoding="utf-8")
        self.web_launcher = WebSearchLauncher(
            state_dir=root, governance_path=web_governance, plan_path=self.web_plan_path,
            mode_args=("--fake-citations-file", str(citations_path)),
        )
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal(
                "coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER,
            ),
            "mission-automation": Principal(
                "mission-automation", AUTOMATION_TOKEN,
                frozenset({"run_mission_source_discovery", "mission_source_discoveries"}),
                actor_ref=AUTOMATION,
            ),
        }
        self.server = WriterServer(
            root / "core.sqlite", self.socket, principals,
            search_launcher=self.alpha_launcher, discovery_plan_path=alpha_plan_path,
            web_search_launcher=self.web_launcher, web_search_plan_path=self.web_plan_path,
        )
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN, timeout=60)
        self.core = WriterClient(self.socket, CORE_TOKEN, timeout=60)
        self.automation = WriterClient(self.socket, AUTOMATION_TOKEN, timeout=60)

    def close(self) -> None:
        self.web_launcher.close()
        self.alpha_launcher.close()
        super().close()


class P9d4WriterOpsTests(unittest.TestCase):
    def test_web_search_rides_the_same_tick_and_human_probe_selects_source(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        h = P9d4WriterHarness(Path(root.name))
        self.addCleanup(h.close)
        state = h.bootstrap()
        params = h.mission_params(state)
        v1 = h.governance.call("create_coverage_mission", params)
        # v1: AlphaEngine probe_only, web search not_connected -> both refused, reasons shown.
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual(tick["discovery"]["status"], "not_authorized")
        self.assertEqual(tick["web_search"]["source_ref"], WEB_SEARCH_SOURCE_REF)
        self.assertEqual(tick["web_search"]["discovery"]["status"], "not_authorized")
        self.assertIn("not_connected", tick["web_search"]["discovery"]["reason"])
        with self.assertRaises(RemoteError):
            h.governance.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "management-changes", "source_ref": WEB_SEARCH_SOURCE_REF,
            })
        with self.assertRaises(RemoteError):
            h.governance.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "management-changes", "source_ref": "source:guidepoint",
            })
        # v2 marks web search probe_only: the owner rehearses through the writer.
        p2 = h.mission_params(state)
        for item in p2["source_plan"]:
            if item["source_ref"] == WEB_SEARCH_SOURCE_REF:
                item["status"] = "probe_only"
        p2.update({
            "version_id": "coverage-mission-version:us-it-services:2",
            "prior_version_ref": v1["id"], "idempotency_key": "coverage-mission:us-it-services:2",
        })
        h.governance.call("create_coverage_mission", p2)
        with self.assertRaises(RemoteAuthorizationError):
            h.automation.call("run_mission_source_discovery", {
                "company_ref": ACN, "spec_ref": "management-changes", "source_ref": WEB_SEARCH_SOURCE_REF,
            })
        ticket = h.governance.call("run_mission_source_discovery", {
            "company_ref": ACN, "spec_ref": "management-changes", "source_ref": WEB_SEARCH_SOURCE_REF,
            "as_of": "2026-09-06",
        })
        self.assertTrue(ticket["id"].startswith("web-search-discovery:"))
        self.assertEqual(ticket["parameters"], {
            "query": "Accenture ACN CEO CFO appointment resignation",
            "date_after": "2026-06-08", "date_before": "2026-09-06",
        })
        h.web_launcher.wait(timeout=120)
        status = h.governance.call("mission_source_discovery_status", {"ticket_ref": ticket["id"]})
        self.assertEqual(status["status"], "succeeded", status)
        self.assertEqual(status["summary"]["new_document_count"], 2)
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual([item["status"] for item in tick["web_search"]["settled_dispatches"]], ["succeeded"])
        self.assertEqual(tick["settled_dispatches"], [])
        self.assertEqual(tick["web_search"]["acquisition"]["status"], "unconfigured")
        documents = h.governance.call("mission_discovered_documents", {"status": "discovered"})
        self.assertEqual(sorted(item["document_ref"] for item in documents["documents"]), sorted([URL_A, URL_B]))
        self.assertTrue(all(item["source_ref"] == WEB_SEARCH_SOURCE_REF for item in documents["documents"]))
        listing = h.governance.call("mission_source_discoveries", {"company_ref": ACN})
        self.assertEqual([item["source_ref"] for item in listing["discoveries"]], [WEB_SEARCH_SOURCE_REF])


if __name__ == "__main__":
    unittest.main()


class SecFilingsDiscoveryPlanTests(unittest.TestCase):
    """P10r: the SEC index plan is asked for a form, not a phrase."""

    def plan(self, **overrides) -> dict:
        from dalton_core.store import content_hash

        body = {
            "schema_version": "0.4",
            "id": "discovery-plan:us-it-services:sec-filings:1",
            "created_at": "2026-09-08T00:00:00.000000+00:00",
            "mission_ref": "coverage-mission:us-it-services",
            "source_ref": "source:sec-edgar",
            "budget": {"max_calls_24h": 50},
            "companies": {"company:sec-cik:0001467373": {"cik": "0001467373"}},
            "specs": [{
                "spec_ref": "annual-report-10k", "form": "10-K",
                "lookback_days": 800, "rediscovery_interval_days": 30,
                "retry_interval_days": 2,
            }],
            **overrides,
        }
        body["content_hash"] = content_hash(
            {k: v for k, v in body.items() if k != "content_hash"}
        )
        return body

    def test_a_sec_plan_round_trips(self) -> None:
        from dalton_core.mission_source_discovery import validate_discovery_plan

        cleaned = validate_discovery_plan(self.plan())
        self.assertEqual(cleaned["source_ref"], "source:sec-edgar")
        self.assertEqual(cleaned["specs"][0]["form"], "10-K")
        self.assertNotIn("query_template", cleaned["specs"][0])
        self.assertEqual(
            cleaned["companies"]["company:sec-cik:0001467373"], {"cik": "0001467373"}
        )

    def test_the_shape_and_the_source_must_agree(self) -> None:
        from dalton_core.mission_source_discovery import (
            DiscoveryPlanError, validate_discovery_plan,
        )

        # A SEC source on a web-shaped plan, and a web source on the SEC shape,
        # are both refused: the version is what says which spec fields apply,
        # so letting them drift apart would silently validate the wrong shape.
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(self.plan(schema_version="0.2"))
        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(self.plan(source_ref="source:web-search"))

    def test_a_cik_that_is_not_ten_digits_is_refused(self) -> None:
        from dalton_core.mission_source_discovery import (
            DiscoveryPlanError, validate_discovery_plan,
        )

        # The SEC endpoint takes the padded form; a hand-shortened number would
        # quietly index the wrong issuer.
        for cik in ("1467373", "00014673731", "000146737a"):
            with self.assertRaises(DiscoveryPlanError):
                validate_discovery_plan(
                    self.plan(companies={"company:sec-cik:0001467373": {"cik": cik}})
                )

    def test_a_sec_spec_cannot_smuggle_a_query_template(self) -> None:
        from dalton_core.mission_source_discovery import (
            DiscoveryPlanError, validate_discovery_plan,
        )

        with self.assertRaises(DiscoveryPlanError):
            validate_discovery_plan(self.plan(specs=[{
                "spec_ref": "annual-report-10k", "form": "10-K",
                "query_template": "{terms} annual report",
                "lookback_days": 800, "rediscovery_interval_days": 30,
                "retry_interval_days": 2,
            }]))
