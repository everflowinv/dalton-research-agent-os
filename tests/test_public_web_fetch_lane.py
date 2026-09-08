"""P9d-4b: public-web fetch lane -- executor, coordinator settlement, child, writer ops."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
    HOST_SCOPED_GENERATION,
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
from dalton_core.document_extraction import (
    DocumentExtractionModelWorker,
    GATE_REASON,
    HermeticExtractionAdapter,
    WEB_STAGING_GATE_REASON,
)
from dalton_core.model_router import ModelRouter
from dalton_core.public_web_fetch_cli import _fetch_failure_reason, fake_page_transport
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
from tests.test_transcript_polish_model_worker import policy, profile


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
        self.assertEqual(fetched["connector_profile_ref"], f"{FETCH_PROFILE_PREFIX}:{host_slug('example.com')}:{HOST_SCOPED_GENERATION}:v1")
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
        self.assertEqual(second["connector_profile_ref"], f"{FETCH_PROFILE_PREFIX}:{host_slug('news.example.org')}:{HOST_SCOPED_GENERATION}:v1")
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

    def test_blocked_host_receipt_names_the_status_not_just_the_outcome(self) -> None:
        """P9d-9: a 403 from a host that refuses automated clients must say so.

        Live, every fetch of news.alphastreet.com failed with nothing recorded
        but ``exit 1``, which reads identically to a network blip.  The runner
        wire already knew it was a 403, so the receipt now carries the provider
        status and the closed error, and the reason string repeats them.
        """

        h = FetchHarness(self.root, transport=transport_for(b"forbidden", status=403))
        self.addCleanup(h.close)
        authority = h.authority(h.discover(), URL_A)
        failed = h.fetch.fetch(h.fetch.build_request(authority))
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["error"]["code"], "http_status")
        self.assertIs(failed["error"]["retryable"], False)
        reason = _fetch_failure_reason(failed)
        self.assertIn("403", reason)
        self.assertIn("not retryable", reason)
        # A retryable status is reported as such, so a caller can tell them apart.
        (self.root / "retryable").mkdir()
        h2 = FetchHarness(self.root / "retryable", transport=transport_for(b"busy", status=503))
        self.addCleanup(h2.close)
        again = h2.fetch.fetch(h2.fetch.build_request(h2.authority(h2.discover(), URL_A)))
        self.assertIs(again["error"]["retryable"], True)
        self.assertNotIn("not retryable", _fetch_failure_reason(again))
        # A succeeded fetch carries no error at all.
        (self.root / "ok").mkdir()
        h3 = FetchHarness(self.root / "ok")
        self.addCleanup(h3.close)
        ok = h3.fetch.fetch(h3.fetch.build_request(h3.authority(h3.discover(), URL_A)))
        self.assertEqual(ok["outcome"], "succeeded")
        self.assertIsNone(ok["error"])

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
        # P9d-9: the real child leaves a summary.json beside its ticket; when it
        # says why the fetch failed the coordinator must prefer that over the
        # exit code.  None reproduces a child that left no summary.
        self.fail_summary: dict | None = None
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
        self.tickets[ticket] = {
            "id": ticket, "status": status,
            "exit_code": 0 if status == "succeeded" else 1,
            "document_ref": document_ref,
            "summary": None if status == "succeeded" else self.fail_summary,
        }
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

    def _coordinator(self, plan: dict, *, spool_dir=None) -> MissionSourceDiscoveryCoordinator:
        self.search_launcher.plan = plan
        return MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=plan,
            search_launcher=self.search_launcher, acquisition_launcher=self.fetch_launcher,
            clock=self.clock, spool_dir=spool_dir,
        )

    def test_document_already_in_authority_at_search_time_enters_the_review_queue(self) -> None:
        """P9d-12: bytes a human fetched first still owe the human queue a review.

        Live, the one document search marked already_in_authority (a hand
        fetched PDF) never entered the queue, because registration required
        ``acquired`` and nothing ever moved the row.
        """

        # A human search + fetch puts URL_A's bytes into authority before the
        # mission's own search runs.
        receipt = self.h.discover()
        self.h.fetch.fetch(self.h.fetch.build_request(self.h.authority(receipt, URL_A)))
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_dispatches"][0]["new_document_count"], 1)
        held = tick["already_held"]
        self.assertEqual([(h["document_ref"], h["status"], h["review_status"]) for h in held], [(URL_A, "acquired", "fresh")])
        # No fetch was spent on it; the fetch that did launch is URL_B.
        self.assertEqual([c["document_ref"] for c in self.fetch_launcher.calls], [URL_B])
        reviews = self.missions.document_reviews(self.mission["id"], state="awaiting_human_extraction")
        self.assertEqual([r["document_ref"] for r in reviews], [URL_A])
        self.assertEqual(self.coordinator.dispatch_once()["already_held"], [])

    def test_plan_acquisition_policy_orders_preferred_hosts_first_and_never_fetches_skipped(self) -> None:
        """P9d-13: first-party hosts jump the queue; hosts that refuse the lane cost nothing."""

        preferred = self._coordinator(web_plan_for_tests(
            max_calls_24h=10, acquisition={"preferred_hosts": ["news.example.org"], "skip_hosts": []},
        ))
        preferred.dispatch_once()
        tick = preferred.dispatch_once()
        # URL_A is older, but URL_B's host is preferred.
        self.assertEqual(tick["acquisition"]["document_ref"], URL_B)
        docs = {d["document_ref"]: d for d in self.missions.discovered_documents(self.mission["id"])}
        self.assertEqual((docs[URL_A]["host"], docs[URL_B]["host"]), ("example.com", "news.example.org"))
        tick = preferred.dispatch_once()
        self.assertEqual(tick["acquisition"]["document_ref"], URL_A)

        # Start again with example.com skipped: URL_A is never picked, not even
        # as a retry, and the idle tick says how many rows the skip is holding.
        self.setUp()
        skipping = self._coordinator(web_plan_for_tests(
            max_calls_24h=10, acquisition={"preferred_hosts": [], "skip_hosts": ["example.com"]},
        ))
        skipping.dispatch_once()
        tick = skipping.dispatch_once()
        self.assertEqual(tick["acquisition"]["document_ref"], URL_B)
        tick = skipping.dispatch_once()
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["held_by_skip_hosts"]), ("idle", 1))
        self.assertEqual([c["document_ref"] for c in self.fetch_launcher.calls], [URL_B])
        self.assertIsNone(self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF, skip_hosts=["example.com"]))
        self.assertEqual(self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)["document_ref"], URL_A)
        self.assertIsNone(self.missions.retryable_failed_document(
            older_than=timedelta(days=1), as_of=self.clock() + timedelta(days=3),
            source_ref=WEB_SEARCH_SOURCE_REF, skip_hosts=["example.com"],
        ))
        # The provider's redirect proxies are always held, plan or no plan:
        # the transport refuses to follow one out, so a fetch could only fail.
        self.assertIn("vertexaisearch.cloud.google.com", skipping.skip_hosts)
        self.assertIn("vertexaisearch.cloud.google.com", self._coordinator(self.plan).skip_hosts)

    def test_host_backfill_fills_pre_ledger_rows_from_the_exact_discovery_envelope(self) -> None:
        """P9d-13: rows recorded before the ledger carried a host learn it from the spool."""

        self.search_launcher.with_hosts = False
        blind = self._coordinator(self.plan)
        blind.dispatch_once()
        tick = blind.dispatch_once()
        self.assertEqual(tick["host_backfill"]["status"], "no_spool")
        self.assertEqual([d["host"] for d in self.missions.discovered_documents(self.mission["id"])], [None, None])
        # URL_A's fetch launched on that tick; URL_B is still queued and, with
        # no host recorded, the policy neither prefers nor skips it.
        self.assertEqual(self.missions.next_discovered_document(
            source_ref=WEB_SEARCH_SOURCE_REF, skip_hosts=["news.example.org"])["document_ref"], URL_B)
        sighted = self._coordinator(self.plan, spool_dir=Path(self.temp.name) / "spool")
        tick = sighted.dispatch_once()
        self.assertEqual((tick["host_backfill"]["status"], tick["host_backfill"]["filled"]), ("filled", 2))
        self.assertEqual({d["document_ref"]: d["host"] for d in self.missions.discovered_documents(self.mission["id"])},
                         {URL_A: "example.com", URL_B: "news.example.org"})
        self.assertEqual(sighted.dispatch_once()["host_backfill"], {"status": "complete", "filled": 0})
        with self.assertRaises(Exception):
            self.missions.set_document_host(self.missions.discovered_documents(self.mission["id"])[0]["record_id"], "other.example")

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

    def test_bytes_already_held_settle_without_a_second_paid_fetch(self) -> None:
        """A human fetch leaves the ledger at discovered; the tick must not re-fetch."""

        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["settled_dispatches"][0]["status"], "succeeded")
        # That tick launched URL_A, so URL_B is the next one still queued.
        queued = self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)
        self.assertEqual(queued["document_ref"], URL_B)
        self.assertEqual(queued["status"], "discovered")
        # Simulate the human-only fetch op: bytes enter authority directly and
        # the mission ledger is untouched.
        before = len(self.fetch_launcher.calls)
        envelope_ref = self.fetch_launcher._discovery_receipt(URL_B)
        authority = url_authority_from_discovery(
            self.h.core.connection, self.h.spool, url_ref=URL_B, source_envelope_ref=envelope_ref,
        )
        self.h.fetch.fetch(self.h.fetch.build_request(authority))
        self.assertEqual(public_web_urls_in_authority(self.h.core.connection, [URL_B]), [URL_B])
        self.assertEqual(
            self.missions.next_discovered_document(source_ref=WEB_SEARCH_SOURCE_REF)["status"], "discovered"
        )
        tick = self.coordinator.dispatch_once()
        acquisition = tick["acquisition"]
        self.assertEqual(acquisition["status"], "already_in_authority")
        self.assertEqual((acquisition["document_ref"], acquisition["settled_status"]), (URL_B, "acquired"))
        self.assertEqual(acquisition["review_status"], "fresh")
        # No second fetch was launched for those bytes.
        self.assertEqual(len(self.fetch_launcher.calls), before)
        reviews = self.missions.document_reviews(self.mission["id"], state="awaiting_human_extraction")
        self.assertIn(URL_B, [item["document_ref"] for item in reviews])

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


    def test_a_restart_retries_earlier_failures_without_waiting_the_interval(self) -> None:
        """P11b: a restart means new code, so retry what failed under the old.

        Waiting the full interval to learn whether a fix worked is a bad loop:
        live, five filings failed on bugs that were corrected within the hour
        and would have sat until the interval passed, which also kept the fix
        itself unverified.
        """

        self.fetch_launcher.fail = True
        self.coordinator.dispatch_once()
        self.coordinator.dispatch_once()
        self.coordinator.dispatch_once()
        failed = self.missions.discovered_documents(
            self.mission["id"], status="acquisition_failed"
        )
        self.assertTrue(failed)
        # Same coordinator, interval not elapsed: nothing is retried.
        tick = self.coordinator.dispatch_once()
        self.assertEqual(tick["acquisition"]["status"], "idle")
        self.assertFalse(tick["retried_after_restart"])

        # A restart builds a fresh coordinator, which takes one catch-up pass.
        # Time passes across a restart, which is what makes a failure recorded
        # before it eligible at all.
        self.clock.advance(minutes=1)
        self.fetch_launcher.fail = False
        restarted = MissionSourceDiscoveryCoordinator(
            store=self.h.core, missions=self.missions, plan=self.plan,
            search_launcher=self.search_launcher,
            acquisition_launcher=self.fetch_launcher, clock=self.clock,
        )
        tick = restarted.dispatch_once()
        self.assertTrue(tick["retried_after_restart"])
        self.assertEqual((tick["acquisition"]["status"], tick["acquisition"]["retry"]), ("launched", True))
        # Only one such pass, so a permanently failing document is not hot-looped.
        self.assertFalse(restarted.dispatch_once()["retried_after_restart"])

    def test_ledger_records_why_the_fetch_failed_not_just_the_exit_code(self) -> None:
        """P9d-9: a blocked host and a network blip must not read the same.

        Live, every fetch of one host failed with only ``acquisition ended
        failed (exit 1)`` in the ledger, so nothing distinguished a permanent
        403 from a transient fault.  The search side already preferred the
        child's own failure_reason; the fetch side now does too.
        """

        self.fetch_launcher.fail = True
        self.fetch_launcher.fail_summary = {
            "failure_reason": "fetch outcome failed; public web fetch returned HTTP 403; not retryable",
        }
        self.coordinator.dispatch_once()
        self.coordinator.dispatch_once()
        tick = self.coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in tick["settled_documents"]], ["acquisition_failed"])
        failed = self.missions.discovered_documents(self.mission["id"], status="acquisition_failed")
        self.assertIn("HTTP 403", failed[0]["failure_reason"])
        self.assertIn("not retryable", failed[0]["failure_reason"])
        # The exit-code fallback for a child that left no summary stays covered
        # by test_failed_fetch_is_recorded_and_retried_after_interval.


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

    def test_finished_child_is_adopted_after_a_writer_restart_not_called_orphaned(self) -> None:
        """P9d-14: a restart between child exit and the next tick lost the work.

        Live, thirteen tickets across three lanes were settled orphaned even
        though their child had finished and written its summary.  A fresh
        launcher (no process handle) now adopts the child's own terminal
        summary and says so; without a summary the ticket stays orphaned.
        """

        launcher = self.launcher()
        ticket = launcher.start(document_ref=URL_A, actor_ref=OWNER)
        self.assertEqual(launcher.wait(timeout=120), 0)
        self.assertEqual(launcher.status(ticket["id"])["status"], "succeeded")
        ticket_path = self.state / "fetches" / ticket["id"].split(":", 1)[1] / "ticket.json"
        record = json.loads(ticket_path.read_text(encoding="utf-8"))
        record.update({"status": "running", "exit_code": None, "completed_at": None, "pid": 2**22 - 1})
        ticket_path.write_text(json.dumps(record), encoding="utf-8")
        fresh = self.launcher()
        adopted = fresh.status(ticket["id"])
        self.assertEqual((adopted["status"], adopted["exit_code"], adopted["adopted_from_summary"]), ("succeeded", 0, True))
        self.assertIsNotNone(adopted["completed_at"])
        self.assertEqual(adopted["summary"]["status"], "succeeded")
        # The settle-side verification is untouched: the manifest still has to agree.
        manifest = fresh.read_completed_manifest(ticket["id"], URL_A)
        self.assertEqual(manifest["url_ref"], URL_A)
        # ADR-0005: a row that names no ticket is still readable through the
        # ticket directory, by document ref, via the same verified reader.
        self.assertEqual(fresh.locate_completed_manifest(URL_A)["id"], manifest["id"])
        with self.assertRaises(FetchLaunchRejected):
            fresh.locate_completed_manifest(URL_B)
        # No summary means nothing to adopt: orphaned, as before.
        record["status"] = "running"
        ticket_path.write_text(json.dumps(record), encoding="utf-8")
        (ticket_path.with_name("summary.json")).unlink()
        orphan = self.launcher().status(ticket["id"])
        self.assertEqual(orphan["status"], "orphaned")
        self.assertNotIn("adopted_from_summary", orphan)

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
            # P9d-15: drafting needs the writer's Scheduler as work-order authority.
            scheduler_path=root / "scheduler.sqlite",
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
        # ADR-0005: the extraction lane answers the tick truthfully when no
        # model configuration is installed, instead of raising.
        self.assertEqual(h.core.call("dispatch_document_extraction", {})["status"], "unconfigured")
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
        # P10x: the tick that fetches also settles. Settlement used to wait for
        # the next tick 300s later, which is why one document moved per tick;
        # the page is acquired and queued for review inside the same pass now.
        settled = tick["web_search"]["settled_documents"]
        # Both queued URLs move in this one tick, not one per tick.
        self.assertEqual(
            [(item["document_ref"], item["status"], item["review_status"]) for item in settled],
            [(URL_A, "acquired", "fresh"), (URL_B, "acquired", "fresh")],
        )
        self.assertEqual(tick["web_search"]["acquisitions_launched"], 2)
        idle_tick = h.core.call("dispatch_mission_source_discovery", {})
        self.assertEqual(idle_tick["web_search"]["settled_documents"], [])
        reviews = h.governance.call("mission_document_reviews", {"state": "awaiting_human_extraction"})
        # Both pages the tick fetched are waiting to be read.
        self.assertEqual(
            [item["document_ref"] for item in reviews["reviews"]], [URL_A, URL_B]
        )
        review = reviews["reviews"][0]
        # P9d-4c: the fetched page is a verified read-only original. The human
        # sees the URL it came from and bounded quotes of its exact bytes.
        review_hash = content_hash(review)
        evidence = h.governance.call("mission_document_evidence", {
            "review_id": review["review_id"], "expected_review_hash": review_hash, "offset": 0,
        })
        context = evidence["context"]
        self.assertEqual((context["source_ref"], context["document_ref"]), (WEB_SEARCH_SOURCE_REF, URL_A))
        self.assertEqual((context["canonical_url"], context["host"]), ("https://example.com/investors?q=ai", "example.com"))
        self.assertEqual((context["source_renderer"], context["body_sha256"]), ("html-visible-blocks:0.1", BODY_HASH))
        self.assertEqual(context["source_content_hash"], hashlib.sha256(
            "Leadership update\n\noriginal bytes, never a snippet".encode("utf-8")).hexdigest())
        self.assertEqual(context["quotes"][0]["raw_text"], "Leadership update\n\noriginal bytes, never a snippet")
        self.assertEqual((context["total_chars"], context["next_offset"], context["untrusted_source"]),
                         (len(context["quotes"][0]["raw_text"]), None, True))
        # P9d-15: drafting is gated only by the model configuration, exactly
        # as for AlphaEngine documents (this harness installs none); staging
        # stays refused because web sources have no citation authority yet.
        self.assertEqual((evidence["generation_enabled"], evidence["gate_reason"], evidence["status"]),
                         (False, GATE_REASON, "not_generated"))
        gated = h.governance.call("generate_document_extraction", {
            "review_id": review["review_id"], "expected_review_hash": review_hash, "offset": 0,
            "expected_context_hash": context["content_hash"],
        })
        self.assertEqual((gated["status"], gated["reason"], gated["formal_authority_writes"]),
                         ("gated", GATE_REASON, 0))
        # A hermetic fixture worker drafts suggestions bound to exact quotes of
        # the deterministic rendering; nothing formal is written.
        router_path = str(Path(root.name) / "router.sqlite")
        pr = profile(); pr["provider"] = "hermetic-fixture"
        pr["cost"]["input_per_million_usd"] = pr["cost"]["output_per_million_usd"] = 0
        now = datetime.now(timezone.utc)
        pr["availability"]["checked_at"] = now.isoformat()
        pr["availability"]["valid_until"] = (now + timedelta(days=2)).isoformat()
        with ModelRouter(router_path) as seed:
            seed.register_profile(pr); seed.register_policy(policy())
        routers = []  # opened on the writer thread; never closed from this one
        adapter = HermeticExtractionAdapter({"schema_version": "0.1", "suggestions": [{
            "quote_id": context["quotes"][0]["quote_id"],
            "normalized_statement": "Fixture: the company announced a leadership update.",
            "metric_or_aspect": "aspect:leadership", "period": "not specified in this window",
            "basis": "fixture company announcement",
        }]}, created_at=now.isoformat())
        def factory(service, ctx, actor):
            # Opened on the writer's thread: SQLite handles are thread-bound.
            router = ModelRouter(router_path)
            routers.append(router)
            return DocumentExtractionModelWorker(
                scheduler=h.server._scheduler, router=router, store=h.server.store, observability=h.server.observability,
                adapter=adapter, routing_policy_ref=policy()["policy_version_ref"],
                credential_slot_refs=[profile()["credential_slot_ref"]],
                context_resolver=lambda c: service.reread(c, actor),
            )
        h.server._document_extraction_worker_factory = factory
        def counts():
            import sqlite3
            ro = sqlite3.connect(f"file:{Path(root.name) / 'core.sqlite'}?mode=ro", uri=True)
            try:
                return {t: ro.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                        for t in ("claim_versions", "evidence_versions")}
            finally:
                ro.close()
        before = counts()
        drafted = h.governance.call("generate_document_extraction", {
            "review_id": review["review_id"], "expected_review_hash": review_hash, "offset": 0,
            "expected_context_hash": context["content_hash"],
        })
        self.assertEqual(drafted["status"], "succeeded", drafted)
        suggestion = drafted["suggestions"][0]
        self.assertEqual(suggestion["citation"]["raw_text"], "Leadership update\n\noriginal bytes, never a snippet")
        self.assertEqual(suggestion["source_content_hash"], context["source_content_hash"])
        self.assertEqual(suggestion["citation_status"], "pending_human_citation_admission")
        self.assertTrue(suggestion["hermetic_fixture"])
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(h.governance.call("generate_document_extraction", {
            "review_id": review["review_id"], "expected_review_hash": review_hash, "offset": 0,
            "expected_context_hash": context["content_hash"],
        })["suggestions"], drafted["suggestions"])
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(counts(), before)
        h.server._document_extraction_worker_factory = None
        with self.assertRaises(RemoteError) as staged:
            h.governance.call("stage_document_extraction", {
                "review_id": review["review_id"], "expected_review_hash": review_hash, "offset": 0,
                "expected_context_hash": context["content_hash"],
                "suggestion_ref": "document-extraction-suggestion:x", "suggestion_hash": "0" * 64,
                "request_id": "req-1", "normalized_statement": "s", "metric_or_aspect": "m",
                "period": "2026Q3", "basis": "reported", "source_start": 0, "source_end": 5,
                "raw_text": "Leade", "rationale": "checked", "confirm_citation": True,
                "correction_set_version_ref": None, "correction_set_version_hash": None,
            })
        # The writer maps the refusal to a generic client message; the reason
        # itself is asserted where the service raises it (WEB_STAGING_GATE_REASON).
        self.assertIsInstance(staged.exception, RemoteError)
        # A stale review hash still fails closed on the new lane.
        with self.assertRaises(RemoteError):
            h.governance.call("mission_document_evidence", {
                "review_id": review["review_id"], "expected_review_hash": "0" * 64, "offset": 0,
            })
        self.assertEqual(h.governance.call("mission_document_reviews", {"state": "awaiting_human_extraction"})["reviews"][0]["review_id"], review["review_id"])
        # P10x: the tick took both URLs, so ``acquisition`` names the first one
        # it started and the queue is already empty behind it.
        self.assertEqual(tick["web_search"]["acquisition"]["document_ref"], URL_A)
        # A human request while the single slot is busy is a conflict, never a
        # second process. The slot is free now that the tick drained, so hold
        # it open explicitly rather than relying on a leftover child.
        h.fetch_launcher.start_bounded_probe(
            document_ref=URL_B, caller_ref="automation:coverage-mission"
        )
        with self.assertRaises(RemoteError) as ctx:
            h.governance.call("acquire_public_web_document", {"document_ref": URL_B})
        self.assertEqual(ctx.exception.code, "conflict")
        h.fetch_launcher.wait(timeout=120)
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
