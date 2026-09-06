"""P9d-4b: public-web fetch lane -- executor, coordinator settlement, child, writer ops."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.capability_catalog import CapabilityCatalog
from dalton_core.connector_governance import ConnectorGovernance, WEB_FETCH_KIND, build_governance_record
from dalton_core.connector_governance_cli import approve_governance_record
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.mission_source_discovery import (
    ALPHAENGINE_SOURCE_REF,
    MissionSourceDiscoveryCoordinator,
    WEB_SEARCH_SOURCE_REF,
    WebSearchLauncher,
    build_discovery_parameters,
)
from dalton_core.public_http_transport import PublicHttpTransport
from dalton_core.public_web_core_fetch import (
    FETCH_CAPABILITY_ID,
    FETCH_PROFILE_PREFIX,
    PublicWebCoreFetch,
    PublicWebCoreFetchError,
    WebFetchConnectorGovernance,
    build_web_fetch_governance_record,
    count_recent_public_web_fetch_calls,
    host_slug,
    url_authority_from_discovery,
    validate_public_web_fetch_manifest,
    write_web_fetch_governance_proposal,
)
from dalton_core.public_web_core_search import (
    FakeWebSearchHandle,
    build_web_search_governance_record,
    public_web_urls_in_authority,
    web_search_spec_hash,
)
from dalton_core.public_web_fetch_cli import fake_page_transport
from dalton_core.public_web_fetch_launcher import (
    FetchLaunchRejected,
    FetchTicketNotFound,
    PublicWebFetchLauncher,
)
from dalton_core.store import DaltonStore, canonical_json, content_hash
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import CORE_OPERATIONS, HUMAN_GOVERNANCE_OPERATIONS, Principal, WriterServer
from tests.p9a_fixtures import ROOT, bootstrap_method_authorities, mission_params
from tests.test_mission_source_discovery import ACN, AUTOMATION, Clock, OWNER, plan_for_tests
from tests.test_mission_web_search_discovery import (
    CITATIONS,
    FakeWebSearchLauncher,
    URL_A,
    URL_B,
    mission_with_web_status,
    web_plan_for_tests,
)
from tests.test_p9a_writer_ops import AUTOMATION_TOKEN, CORE_TOKEN, GOVERNANCE_TOKEN, P9aWriterHarness
from tests.test_public_web_core_search import WebSearchHarness


BODY = b"<html><body><h1>Leadership update</h1><p>original bytes, never a snippet</p></body></html>"
BODY_HASH = hashlib.sha256(BODY).hexdigest()
SPEC = {"query": "Accenture AI demand", "date_after": "2026-08-01", "date_before": "2026-09-06"}


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.reason = "OK" if status == 200 else "ERR"

    def getheaders(self):
        return [("content-type", "text/html; charset=utf-8"), ("content-length", str(len(self._body)))]

    def read(self, _amount=None):
        body, self._body = self._body, b""
        return body

    def close(self):
        return None


def transport_for(body: bytes = BODY, *, status: int = 200) -> PublicHttpTransport:
    return PublicHttpTransport(
        resolver=lambda _host, _port: ("93.184.216.34",),
        exchange=lambda _target, _method, _headers, _body, _timeout: _Response(body, status),
    )


def approved_fetch_governance() -> WebFetchConnectorGovernance:
    return WebFetchConnectorGovernance(build_web_fetch_governance_record(approved_by="human:lumos", status="approved"))


class FetchHarness(WebSearchHarness):
    """Web search + fetch on one Core (both run in-process)."""

    def __init__(self, root: Path, *, transport=None, fetch_governance=None, clock: Clock | None = None) -> None:
        super().__init__(root, FakeWebSearchHandle(CITATIONS), clock=clock)
        governance = fetch_governance or approved_fetch_governance()
        self.fetch_catalog = CapabilityCatalog(
            str(root / "catalog-fetch.sqlite"), clock=self.clock,
            approval_resolver=governance.approval, policy_resolver=governance.policy,
        )
        self.fetch = PublicWebCoreFetch(
            store=self.core, connectors=self.connectors, observability=self.observability,
            journal=self.journal, scheduler=self.scheduler, catalog=self.fetch_catalog,
            spool=self.spool, governance=governance, transport=transport or transport_for(), clock=self.clock,
        )

    def discover(self):
        receipt = self.search.search(self.search.build_request(SPEC))
        return receipt

    def authority(self, receipt, url_ref):
        return url_authority_from_discovery(
            self.core.connection, self.spool, url_ref=url_ref, source_envelope_ref=receipt["source_envelope_ref"],
        )

    def close(self) -> None:
        self.fetch_catalog.close()
        super().close()


class FetchExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_governance_kind_and_cli_approval(self) -> None:
        proposed = build_web_fetch_governance_record(approved_by="human:lumos")
        self.assertEqual(proposed["capability_id"], FETCH_CAPABILITY_ID)
        self.assertEqual(proposed["allowed_permissions"]["network"], True)
        self.assertEqual(proposed["allowed_permissions"]["credential_slot_refs"], [])
        self.assertEqual(ConnectorGovernance(proposed).kind, WEB_FETCH_KIND)
        self.assertEqual(canonical_json(build_governance_record(WEB_FETCH_KIND, approved_by="human:lumos")), canonical_json(proposed))
        committed = json.loads((ROOT / "deploy/connector-governance/web-fetch-v1.json").read_text())
        self.assertEqual((committed, committed["status"]), (proposed, "proposed"))
        path = self.root / "gov.json"
        write_web_fetch_governance_proposal(path, approved_by="human:lumos")
        self.assertEqual(approve_governance_record(path, approved_by="human:lumos")["status"], "approved")
        self.assertTrue(WebFetchConnectorGovernance.load(path).approved)
        with self.assertRaises(PublicWebCoreFetchError):
            WebFetchConnectorGovernance(build_governance_record("sec-company-facts", approved_by="human:lumos"))

    def test_fetch_binds_original_bytes_per_host_profile_and_replays_free(self) -> None:
        h = FetchHarness(self.root)
        self.addCleanup(h.close)
        receipt = h.discover()
        self.assertEqual(receipt["document_refs"], [URL_A, URL_B])
        self.assertEqual(public_web_urls_in_authority(h.core.connection, [URL_A, URL_B]), [])
        authority = h.authority(receipt, URL_A)
        self.assertEqual((authority["canonical_url"], authority["host"]), ("https://example.com/investors?q=ai", "example.com"))
        request = h.fetch.build_request(authority)
        fetched = h.fetch.fetch(request)
        self.assertEqual(fetched["outcome"], "succeeded")
        self.assertEqual(fetched["document_ref"], f"public-web-document:url-sha256:{hashlib.sha256(authority['canonical_url'].encode()).hexdigest()}:body-sha256:{BODY_HASH}")
        self.assertEqual((fetched["body_bytes"], fetched["raw_media_type"], fetched["source_status"]), (len(BODY), "text/html", "complete"))
        self.assertEqual(fetched["connector_profile_ref"], f"{FETCH_PROFILE_PREFIX}:{host_slug('example.com')}:v1")
        self.assertEqual(h.spool.read_object(fetched["raw_response_hash"]), BODY)
        envelope = h.fetch.receipts.get_source_envelope(fetched["source_envelope_ref"])
        self.assertEqual((envelope["source"], envelope["operation"], envelope["completeness"]), ("source:public-web", "fetch_get", "partial"))
        manifest = h.fetch.manifest(fetched)
        self.assertEqual(validate_public_web_fetch_manifest(manifest), manifest)
        self.assertEqual((manifest["url_ref"], manifest["body_sha256"], manifest["discovery_source_envelope_ref"]),
                         (URL_A, BODY_HASH, receipt["source_envelope_ref"]))
        # Now the URL counts as in authority; the search ref alone never did.
        self.assertEqual(public_web_urls_in_authority(h.core.connection, [URL_A, URL_B]), [URL_A])
        replay = h.fetch.fetch(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual((replay["document_ref"], replay["provider_calls"]), (fetched["document_ref"], 0))
        self.assertEqual(count_recent_public_web_fetch_calls(h.core.connection, as_of=h.clock()), 1)
        # A second host gets its own profile on the same connector chain.
        second = h.fetch.fetch(h.fetch.build_request(h.authority(receipt, URL_B)))
        self.assertEqual(second["outcome"], "succeeded")
        self.assertEqual(second["connector_profile_ref"], f"{FETCH_PROFILE_PREFIX}:{host_slug('news.example.org')}:v1")
        versions = h.core.connection.execute(
            "SELECT profile_version_id,version_number FROM connector_profile_versions WHERE connector_ref='connector:web-fetch' ORDER BY version_number"
        ).fetchall()
        self.assertEqual([row["version_number"] for row in versions], [1, 2])
        for table in ("evidence_versions", "claim_versions"):
            self.assertEqual(h.core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_uncited_refs_forged_authorities_and_http_failures_fail_closed(self) -> None:
        h = FetchHarness(self.root, transport=transport_for(b"server error", status=500))
        self.addCleanup(h.close)
        receipt = h.discover()
        with self.assertRaises(PublicWebCoreFetchError):
            h.authority(receipt, "public-web-url:sha256:" + "0" * 64)
        authority = h.authority(receipt, URL_A)
        forged = {**authority, "host": "attacker.example"}
        with self.assertRaises(Exception):
            h.fetch.build_request(forged)
        with self.assertRaises(PublicWebCoreFetchError):
            h.fetch.build_request({**authority, "extra": 1})
        failed = h.fetch.fetch(h.fetch.build_request(authority))
        self.assertNotEqual(failed["outcome"], "succeeded")
        self.assertIsNone(failed["document_ref"])
        self.assertEqual(public_web_urls_in_authority(h.core.connection, [URL_A]), [])
        with self.assertRaises(PublicWebCoreFetchError):
            h.fetch.manifest(failed)
        # The failed attempt is still an invocation against the shared window.
        self.assertEqual(count_recent_public_web_fetch_calls(h.core.connection, as_of=h.clock()), 1)

    def test_proposed_governance_refuses_before_any_fetch(self) -> None:
        proposed = WebFetchConnectorGovernance(build_web_fetch_governance_record(approved_by="human:lumos"))
        h = FetchHarness(self.root, fetch_governance=proposed)
        self.addCleanup(h.close)
        receipt = h.discover()
        with self.assertRaises(PublicWebCoreFetchError):
            h.fetch.fetch(h.fetch.build_request(h.authority(receipt, URL_A)))
        self.assertEqual(count_recent_public_web_fetch_calls(h.core.connection, as_of=h.clock()), 0)


class FakeFetchLauncher:
    """Stands in for the child: fetches in-process on start_bounded_probe()."""

    def __init__(self, harness: FetchHarness, *, fail: bool = False) -> None:
        self.h = harness
        self.fail = fail
        self.calls: list[dict] = []
        self.tickets: dict[str, dict] = {}

    def _discovery_receipt(self, url_ref: str) -> str:
        row = self.h.core.connection.execute(
            "SELECT s.source_envelope_ref FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE d.document_ref=? ORDER BY d.created_at DESC LIMIT 1", (url_ref,),
        ).fetchone()
        return row["source_envelope_ref"]

    def start_bounded_probe(self, *, document_ref, caller_ref, **_ignored):
        self.calls.append({"document_ref": document_ref, "caller_ref": caller_ref})
        ticket = f"public-web-fetch:{len(self.calls):024x}"
        status = "succeeded"
        if self.fail:
            status = "failed"
        else:
            authority = url_authority_from_discovery(
                self.h.core.connection, self.h.spool, url_ref=document_ref,
                source_envelope_ref=self._discovery_receipt(document_ref),
            )
            self.h.fetch.fetch(self.h.fetch.build_request(authority))
        self.tickets[ticket] = {"id": ticket, "status": status, "exit_code": 0 if status == "succeeded" else 1, "document_ref": document_ref}
        return {"id": ticket, "status": "running"}

    def status(self, ticket_ref):
        try:
            return dict(self.tickets[ticket_ref])
        except KeyError as exc:
            raise FetchTicketNotFound(ticket_ref) from exc


class FetchCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.h = FetchHarness(Path(self.temp.name), clock=self.clock)
        self.addCleanup(self.h.close)
        self.state = bootstrap_method_authorities(self.h.core)
        self.missions = CoverageMissionAuthority(self.h.core)
        self.plan = web_plan_for_tests(max_calls_24h=10)
        self.search_launcher = FakeWebSearchLauncher(self.h, self.missions, self.plan)
        self.fetch_launcher = FakeFetchLauncher(self.h)
        self.coordinator = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.search_launcher, acquisition_launcher=self.fetch_launcher, clock=self.clock,
        )
        params = mission_with_web_status(self.state, status="not_connected", grant=False, version=1, prior=None)
        ref = params.pop("mission_ref")
        v1 = self.missions.create_mission(ref, **params)
        p2 = mission_with_web_status(self.state, status="connected", grant=True, version=2, prior=v1)
        p2.pop("mission_ref")
        self.mission = self.missions.create_mission(ref, **p2)

    def test_discovered_urls_are_fetched_settled_and_queued_for_human_extraction(self) -> None:
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["discovery"]["status"], "launched")
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_dispatches"]], ["succeeded"])
        # Known gaps first: the oldest discovered URL is fetched before a new search.
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["document_ref"]), ("launched", URL_A))
        self.assertEqual(self.fetch_launcher.calls[0]["caller_ref"], AUTOMATION)
        self.assertEqual(tick["acquisition"]["budget"]["spent"], 1)
        tick = self.coordinator.dispatch_once()
        settled = tick["settled_documents"]
        self.assertEqual([(item["document_ref"], item["status"], item["review_status"]) for item in settled],
                         [(URL_A, "acquired", "fresh")])
        self.assertTrue(settled[0]["review_id"].startswith("mission-document-review:"))
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["document_ref"]), ("launched", URL_B))
        # The fetch counts against the plan's shared window alongside the search.
        self.assertEqual(tick["acquisition"]["budget"]["spent"], 3)
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquired"])
        self.assertEqual(tick["acquisition"]["status"], "idle")
        reviews = self.missions.document_reviews(self.mission["id"], state="awaiting_human_extraction")
        self.assertEqual(sorted(item["document_ref"] for item in reviews), sorted([URL_A, URL_B]))
        self.assertTrue(all(item["source_ref"] == WEB_SEARCH_SOURCE_REF for item in reviews))
        documents = self.missions.discovered_documents(self.mission["id"])
        self.assertEqual(sorted(d["status"] for d in documents), ["acquired", "acquired"])
        self.assertEqual(public_web_urls_in_authority(self.h.core.connection, [URL_A, URL_B]), [URL_A, URL_B])
        progress = self.missions.mission_progress(self.mission["mission_ref"])
        acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
        self.assertEqual((acn["acquired_document_count"], acn["awaiting_extraction_review_count"]), (2, 2))
        for table in ("evidence_versions", "claim_versions"):
            self.assertEqual(self.h.core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_failed_fetch_is_recorded_and_retried_after_interval(self) -> None:
        self.fetch_launcher.fail = True
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "launched")
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquisition_failed"])
        self.assertEqual(tick["acquisition"]["document_ref"], URL_B)
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "idle")
        failed = self.missions.discovered_documents(self.mission["id"], status="acquisition_failed")
        self.assertEqual(len(failed), 2)
        self.assertIn("acquisition ended failed", failed[0]["failure_reason"])
        self.assertEqual(self.missions.document_reviews(self.mission["id"]), [])
        # A day later the oldest failure is retried, this time successfully.
        self.fetch_launcher.fail = False
        self.assertIsNone(self.missions.retryable_failed_document(older_than=timedelta(days=1), as_of=self.clock(), source_ref=WEB_SEARCH_SOURCE_REF))
        self.clock.advance(days=1, minutes=1)
        tick = self.coordinator.dispatch_once()
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["retry"]), ("launched", True))
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquired"])
        # AlphaEngine's ledger view is untouched by any of this.
        self.assertIsNone(self.missions.next_discovered_document(source_ref=ALPHAENGINE_SOURCE_REF))
        self.assertEqual(self.missions.launched_discovered_documents(source_ref=ALPHAENGINE_SOURCE_REF), [])


class FetchChildTests(unittest.TestCase):
    """The real launcher spawns the real child in fake-page mode."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        # Discovery on the child's Core: a real web search (fake handle) plus
        # the mission record, exactly what the search child would leave.
        h = WebSearchHarness(self.state, FakeWebSearchHandle(CITATIONS))
        method = bootstrap_method_authorities(h.core)
        missions = CoverageMissionAuthority(h.core)
        params = mission_params(method)
        ref = params.pop("mission_ref")
        v1 = missions.create_mission(ref, **params)
        p2 = mission_with_web_status(method, status="probe_only", grant=False, version=2, prior=v1)
        p2.pop("mission_ref")
        self.mission = missions.create_mission(ref, **p2)
        self.plan = web_plan_for_tests()
        authorization = missions.authorize_source_discovery(company_ref=ACN, source_ref=WEB_SEARCH_SOURCE_REF, requested_by=OWNER)
        spec_params = build_discovery_parameters(self.plan, spec_ref="management-changes", company_ref=ACN, as_of=h.clock().date())
        receipt = h.search.search(h.search.build_request(spec_params))
        missions.record_source_discovery(
            authorization=authorization, discovery_plan_ref=self.plan["id"], discovery_plan_hash=self.plan["content_hash"],
            spec_ref="management-changes", query_hash=web_search_spec_hash(spec_params), parameters=spec_params,
            connector_invocation_ref=receipt["connector_invocation_ref"], connector_invocation_hash=receipt["connector_invocation_hash"],
            source_envelope_ref=receipt["source_envelope_ref"], source_envelope_hash=receipt["source_envelope_hash"],
            document_refs=receipt["document_refs"], in_authority_document_refs=[],
        )
        h.close()
        self.governance_path = self.root / "fetch-governance.json"
        self.governance_path.write_text(canonical_json(build_web_fetch_governance_record(approved_by="human:lumos", status="approved")) + "\n", encoding="utf-8")
        self.page_path = self.root / "page.html"
        self.page_path.write_bytes(BODY)

    def launcher(self, governance_path=None) -> PublicWebFetchLauncher:
        return PublicWebFetchLauncher(
            state_dir=self.state, governance_path=governance_path or self.governance_path,
            mode_args=("--fake-page-file", str(self.page_path)), spool_dir=self.state / "spool",
        )

    def test_launcher_refuses_before_spawning(self) -> None:
        launcher = self.launcher()
        with self.assertRaises(FetchLaunchRejected):
            launcher.start(document_ref="alphaengine-doc:1", actor_ref=OWNER)
        with self.assertRaises(FetchLaunchRejected):
            launcher.start(document_ref=URL_A, actor_ref=AUTOMATION)
        with self.assertRaises(FetchLaunchRejected):
            launcher.start_bounded_probe(document_ref=URL_A, caller_ref=OWNER)
        proposed = self.root / "proposed.json"
        proposed.write_text(canonical_json(build_web_fetch_governance_record(approved_by="human:lumos")) + "\n")
        with self.assertRaises(FetchLaunchRejected):
            self.launcher(proposed).start(document_ref=URL_A, actor_ref=OWNER)
        with self.assertRaises(FetchTicketNotFound):
            launcher.status("public-web-fetch:" + "0" * 24)
        self.assertFalse(any((self.state / "fetches").iterdir()))

    def test_child_fetches_under_human_request_and_refuses_automation_and_unknown_refs(self) -> None:
        launcher = self.launcher()
        ticket = launcher.start(document_ref=URL_A, actor_ref=OWNER)
        self.assertEqual((ticket["status"], ticket["actor_ref"], ticket["transport"]), ("running", OWNER, "rehearsal"))
        code = launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual((code, status["status"]), (0, "succeeded"), status)
        summary = status["summary"]
        self.assertEqual((summary["status"], summary["transport"], summary["provider_calls"], summary["formal_authority_writes"]), ("succeeded", "fake", 1, 0))
        self.assertEqual((summary["canonical_url"], summary["host"], summary["body_bytes"], summary["raw_media_type"]),
                         ("https://example.com/investors?q=ai", "example.com", len(BODY), "text/html"))
        manifest = launcher.read_completed_manifest(ticket["id"], URL_A)
        self.assertEqual((manifest["url_ref"], manifest["body_sha256"], manifest["document_ref"]), (URL_A, BODY_HASH, summary["document_ref"]))
        ticket_dir = self.state / "fetches" / ticket["id"].split(":")[1]
        for name in ("ticket.json", "summary.json", "manifest.json"):
            self.assertEqual((ticket_dir / name).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FetchLaunchRejected):
            launcher.read_completed_manifest(ticket["id"], URL_B)
        core = DaltonStore(str(self.state / "core.sqlite"))
        try:
            self.assertEqual(public_web_urls_in_authority(core.connection, [URL_A, URL_B]), [URL_A])
            for table in ("evidence_versions", "claim_versions"):
                self.assertEqual(core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            core.close()
        # Automation under probe_only is refused by the child before any byte.
        ticket = launcher.start_bounded_probe(document_ref=URL_B, caller_ref=AUTOMATION)
        launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("probe_only", status["summary"]["failure_reason"])
        self.assertEqual(status["summary"]["provider_calls"], 0)
        # A ref nobody discovered is refused, and so is a ref the search never cited.
        unknown = "public-web-url:sha256:" + "f" * 64
        ticket = launcher.start(document_ref=unknown, actor_ref=OWNER)
        launcher.wait(timeout=120)
        status = launcher.status(ticket["id"])
        self.assertEqual(status["status"], "failed")
        self.assertIn("not a discovered document", status["summary"]["failure_reason"])
        self.assertEqual(status["summary"]["provider_calls"], 0)


class P9d4bWriterHarness(P9aWriterHarness):
    """P9a harness plus rehearsal web search + fetch launchers on the web plan."""

    def __init__(self, root: Path):  # noqa: D107 - mirrors the parent harness
        self.socket = str(root / "writer.sock")
        self.web_plan_path = root / "web-plan.json"
        self.web_plan_path.write_text(json.dumps(web_plan_for_tests()), encoding="utf-8")
        web_governance = root / "web-governance.json"
        web_governance.write_text(canonical_json(build_web_search_governance_record(approved_by="human:lumos", status="approved")) + "\n", encoding="utf-8")
        citations_path = root / "citations.json"
        citations_path.write_text(json.dumps(CITATIONS), encoding="utf-8")
        self.web_launcher = WebSearchLauncher(
            state_dir=root, governance_path=web_governance, plan_path=self.web_plan_path,
            mode_args=("--fake-citations-file", str(citations_path)), spool_dir=root / "spool",
        )
        fetch_governance = root / "fetch-governance.json"
        fetch_governance.write_text(canonical_json(build_web_fetch_governance_record(approved_by="human:lumos", status="approved")) + "\n", encoding="utf-8")
        page = root / "page.html"
        page.write_bytes(BODY)
        self.fetch_launcher = PublicWebFetchLauncher(
            state_dir=root, governance_path=fetch_governance,
            mode_args=("--fake-page-file", str(page)), spool_dir=root / "spool",
        )
        principals = {
            "core": Principal("core", CORE_TOKEN, CORE_OPERATIONS, unrestricted=True),
            "coverage-governance": Principal("coverage-governance", GOVERNANCE_TOKEN, HUMAN_GOVERNANCE_OPERATIONS, actor_ref=OWNER),
            "mission-automation": Principal(
                "mission-automation", AUTOMATION_TOKEN,
                frozenset({"acquire_public_web_document", "mission_document_reviews"}), actor_ref=AUTOMATION,
            ),
        }
        self.server = WriterServer(
            root / "core.sqlite", self.socket, principals,
            transcript_spool_dir=root / "spool",
            web_search_launcher=self.web_launcher, web_search_plan_path=self.web_plan_path,
            web_fetch_launcher=self.fetch_launcher,
        )
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.governance = WriterClient(self.socket, GOVERNANCE_TOKEN, timeout=60)
        self.core = WriterClient(self.socket, CORE_TOKEN, timeout=60)
        self.automation = WriterClient(self.socket, AUTOMATION_TOKEN, timeout=60)

    def close(self) -> None:
        self.fetch_launcher.close()
        self.web_launcher.close()
        super().close()


class P9d4bWriterOpsTests(unittest.TestCase):
    def test_tick_fetches_discovered_urls_and_queues_reviews(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        h = P9d4bWriterHarness(Path(root.name))
        self.addCleanup(h.close)
        state = h.bootstrap()
        params = h.mission_params(state)
        v1 = h.governance.call("create_coverage_mission", params)
        p2 = h.mission_params(state)
        p2["autonomy"]["may_write"] = list(p2["autonomy"]["may_write"]) + ["source_discovery"]
        for item in p2["source_plan"]:
            if item["source_ref"] == WEB_SEARCH_SOURCE_REF:
                item["status"] = "connected"
        p2.update({"version_id": "coverage-mission-version:us-it-services:2", "prior_version_ref": v1["id"], "idempotency_key": "coverage-mission:us-it-services:2"})
        mission = h.governance.call("create_coverage_mission", p2)

        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual(tick["status"], "unconfigured")  # no AlphaEngine plan on this writer
        self.assertEqual(tick["web_search"]["discovery"]["status"], "launched")
        h.web_launcher.wait(timeout=120)
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual([item["status"] for item in tick["web_search"]["settled_dispatches"]], ["succeeded"])
        self.assertEqual((tick["web_search"]["acquisition"]["status"], tick["web_search"]["acquisition"]["document_ref"]), ("launched", URL_A))
        ticket_ref = tick["web_search"]["acquisition"]["ticket_ref"]
        self.assertTrue(ticket_ref.startswith("public-web-fetch:"))
        h.fetch_launcher.wait(timeout=120)
        status = h.governance.call("public_web_fetch_status", {"ticket_ref": ticket_ref})
        self.assertEqual(status["status"], "succeeded", status)
        self.assertEqual(status["summary"]["body_bytes"], len(BODY))
        tick = h.core.call("dispatch_mission_source_discovery", {})
        settled = tick["web_search"]["settled_documents"]
        self.assertEqual([(item["document_ref"], item["status"], item["review_status"]) for item in settled], [(URL_A, "acquired", "fresh")])
        reviews = h.governance.call("mission_document_reviews", {"state": "awaiting_human_extraction"})
        self.assertEqual([item["document_ref"] for item in reviews["reviews"]], [URL_A])
        review = reviews["reviews"][0]
        # The review is real and human-only to resolve; its page cannot yet be
        # rendered as an extraction source (P9d-4c): the writer rejects the
        # view (message bodies are fixed text by design) and the review stays queued.
        with self.assertRaises(RemoteError):
            h.governance.call("mission_document_evidence", {
                "review_id": review["review_id"], "expected_review_hash": content_hash(review), "offset": 0,
            })
        self.assertEqual(h.governance.call("mission_document_reviews", {"state": "awaiting_human_extraction"})["reviews"][0]["review_id"], review["review_id"])
        # The same tick already launched the second URL; a human request while
        # the single slot is busy is a conflict, never a second process.
        self.assertEqual(tick["web_search"]["acquisition"]["document_ref"], URL_B)
        with self.assertRaises(RemoteError) as ctx:
            h.governance.call("acquire_public_web_document", {"document_ref": URL_B})
        self.assertEqual(ctx.exception.code, "conflict")
        h.fetch_launcher.wait(timeout=120)
        tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual([item["status"] for item in tick["web_search"]["settled_documents"]], ["acquired"])
        documents = h.governance.call("mission_discovered_documents", {"mission_version_ref": mission["id"]})
        self.assertEqual(sorted((d["document_ref"], d["status"]) for d in documents["documents"]),
                         sorted([(URL_A, "acquired"), (URL_B, "acquired")]))
        # Human fetch op: automation is refused; the owner's re-fetch of an
        # acquired URL is a new bounded fetch that lands on the same bytes.
        with self.assertRaises(RemoteAuthorizationError):
            h.automation.call("acquire_public_web_document", {"document_ref": URL_A})
        ticket = h.governance.call("acquire_public_web_document", {"document_ref": URL_A})
        self.assertEqual((ticket["actor_ref"], ticket["document_ref"]), (OWNER, URL_A))
        h.fetch_launcher.wait(timeout=120)
        status = h.governance.call("public_web_fetch_status", {"ticket_ref": ticket["id"]})
        self.assertEqual((status["status"], status["summary"]["provider_calls"], status["summary"]["requested_by"]),
                         ("succeeded", 1, OWNER))
        self.assertEqual(status["summary"]["document_ref"], settled[0]["document_ref"] if "document_ref" in settled[0] and settled[0]["document_ref"].startswith("public-web-document:") else status["summary"]["document_ref"])
        self.assertTrue(status["summary"]["document_ref"].endswith(f":body-sha256:{BODY_HASH}"))


if __name__ == "__main__":
    unittest.main()
