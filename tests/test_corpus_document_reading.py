"""P13ap: every acquired original this workspace holds can be read, not just two.

Live on 2026-09-18 the prose pass scanned 143 reviews in the Hyperscaler
workspace and refused 115 of them with one sentence -- "only acquired
AlphaEngine documents and fetched public-web pages can be viewed here" -- while
every one of those 115 documents had its text sitting in the content-addressed
spool under a manifest that declared its hash.  The legacy environment refused
139 of 156 the same way.  Nothing was missing; the door was shut.

These cases open it one source at a time, and each one proves the same thing:
the window the queue reads is derived from the artifact that source's
acquisition recorded, re-read and re-hashed here, never from a prior step's
transcription.  A document whose artifact is gone stays refused, with a reason
that names what is gone.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from dalton_core import coverage_mission as coverage_mission_module
from dalton_core.connector import ConnectorStore
from dalton_core.connector_governance import (
    COMPANY_WIKI_GET_KIND,
    COMPANY_WIKI_LIST_KIND,
    PRIOR_RESEARCH_GET_KIND,
    PRIOR_RESEARCH_LIST_KIND,
    SALES_NOTES_GET_KIND,
    SALES_NOTES_LIST_KIND,
    ConnectorGovernance,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_extraction import (
    FEED_SOURCE_LAUNCHER_KWARGS,
    GUIDEPOINT_LAUNCHER_KWARG,
    SOURCE_STAGING_GATE_REASON,
    DocumentExtractionService,
)
from dalton_core.feed_launcher import (
    CompanyWikiFeedLauncher,
    ReadOnlyFeedManifestReader,
    SalesNotesFeedLauncher,
)
from dalton_core.mission_feed_lane import (
    FEED_DISCOVERY_SOURCES,
    FeedDiscoveryCoordinator,
    build_feed_runner,
    load_feed_discovery_plan,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.research_verification import ResearchVerificationError
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_s1_human_feeds import (
    ACN_NOTE,
    AUTOMATION,
    COMPANY_WIKI,
    FIXTURES,
    PLAN_PATH,
    PRIOR_RESEARCH,
    SALES_NOTES,
    UNIVERSE,
    build_wiki,
    spawn_env,
    write_governance,
)


def sign_document_qualitative_rule(core, *, version: int = 2) -> str:
    """List the mission document qualitative rule in the active policy.

    The same thing ``workspace_mission_setup.prepare_first_mission_bindings``
    does for a new workspace and ``scripts/sign_auto_commit_rules.py`` does for
    an existing one; a fixture Core boots without it.
    """

    from dalton_core.research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
    from tests.test_document_extraction import OWNER

    core.create_policy(
        {**core.active_policy_version().policy,
         "research_candidate_auto_commit": {
             "enabled": True, "max_records": 20,
             "rules": [DOCUMENT_QUALITATIVE_RULE_REF]}},
        policy_version_id=f"policy:synthetic-document-qualitative:{version}",
        actor_ref=OWNER,
        change_reason="ADR-0005 fixture: list the mission document qualitative rule",
    )
    return DOCUMENT_QUALITATIVE_RULE_REF


def run_hermetic_sweep(case, *, state, root, quote_id, normalized_statement,
                       metric_or_aspect, basis, period="not specified in this window",
                       max_windows=4, spool_dir=None):
    """One real extraction run against a hermetic model fixture.

    Everything the child needs -- router, model config, budget -- with the one
    suggestion the fixture returns supplied by the caller.  Shared so the
    reading cases and the P13aq claim cases drive exactly the same sweep.
    """

    from datetime import datetime, timezone

    from dalton_core.document_extraction_cli import run_extraction
    from dalton_core.model_router import ModelRouter
    from tests.test_transcript_polish_model_worker import policy, profile

    pr = profile()
    pr["provider"] = "hermetic-fixture"
    pr["cost"]["input_per_million_usd"] = pr["cost"]["output_per_million_usd"] = 0
    now = datetime.now(timezone.utc)
    pr["availability"]["checked_at"] = now.isoformat()
    pr["availability"]["valid_until"] = (now + timedelta(days=2)).isoformat()
    if not (root / "router.sqlite").exists():
        with ModelRouter(str(root / "router.sqlite")) as router:
            router.register_profile(pr)
            router.register_policy(policy())
    config_path = root / "extraction-model-config.json"
    config_path.write_text(json.dumps({
        "routing_policy_ref": policy()["policy_version_ref"],
        "credential_slot_refs": [profile()["credential_slot_ref"]],
        "model_router_db": str(root / "router.sqlite"),
        "broker_socket": str(root / "none.sock"),
        "broker_auth_key": str(root / "none.key"),
        "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
        "budget_db": str(root / "budget.sqlite"),
        "budget_policy_ref": "thesis-impact-day-budget-policy:production:1",
    }), encoding="utf-8")
    fixture = root / "fixture.json"
    fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [{
        "quote_id": quote_id,
        "normalized_statement": normalized_statement,
        "metric_or_aspect": metric_or_aspect,
        "period": period,
        "basis": basis,
    }]}), encoding="utf-8")
    return run_extraction(
        state_dir=state, model_config_path=config_path,
        summary_dir=root / "extraction-summary",
        spool_dir=(state / "spool") if spool_dir is None else spool_dir,
        scheduler_db=root / "scheduler.sqlite",
        connector_governance=None, web_fetch_governance=None,
        max_windows=max_windows, max_numeric_windows=0, max_discovery_windows=0,
        requested_by=None, hermetic_fixture=fixture,
        candidate_staging=root / "staging.sqlite",
    )


class FeedReadingHarness(unittest.TestCase):
    """One feed lane, one tick, and everything in one state directory.

    The state directory holds Core, the spool and the acquisition tickets
    together, because the extraction child is given exactly one path and finds
    all three under it.  The end-to-end harness in ``test_s1_human_feeds``
    splits them, which is fine for the lane's own cases and is the one thing a
    sweep cannot live with.
    """

    SOURCE_REF = ""
    GOVERNANCE_KINDS: dict[str, str] = {}
    LAUNCHER_KWARG = ""
    SINCE = "2026-08-01"

    def build_launcher(self):
        raise NotImplementedError

    def connect_sources(self, params):
        params["source_plan"] = list(params["source_plan"]) + [{
            "source_ref": self.SOURCE_REF, "role": "fixture corpus",
            "status": "connected",
        }]
        return params

    def setUp(self) -> None:
        if not self.SOURCE_REF:
            self.skipTest("abstract harness")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root
        spawn_env(self)
        self.core = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.core.close)
        self.connectors = ConnectorStore(self.core)
        self.observability = ObservabilityStore(self.core)
        self.spool = RawSpool(str(self.state / "spool"), max_total_bytes=1_000_000_000)
        self.bootstrap = bootstrap_method_authorities(self.core)
        self.missions = CoverageMissionAuthority(self.core)
        patch = mock.patch.object(
            coverage_mission_module, "DISCOVERY_SOURCES",
            MappingProxyType({
                **dict(coverage_mission_module.DISCOVERY_SOURCES),
                **dict(FEED_DISCOVERY_SOURCES),
            }),
        )
        patch.start()
        self.addCleanup(patch.stop)
        params = mission_params(self.bootstrap)
        params["autonomy"]["may_write"] = sorted(
            set(params["autonomy"]["may_write"]) | {"source_discovery", "observation"}
        )
        params = self.connect_sources(params)
        ref = params.pop("mission_ref")
        self.mission = self.missions.create_mission(ref, **params)
        self.governance = {
            operation: write_governance(self.root, kind)
            for operation, kind in self.GOVERNANCE_KINDS.items()
        }
        self.launcher = self.build_launcher()
        self.addCleanup(self.launcher.close)
        self.dispatch()

    def dispatch(self) -> None:
        operations = list(self.GOVERNANCE_KINDS)
        runners = {
            name: build_feed_runner(
                launcher=self.launcher, operation=operation,
                governance=ConnectorGovernance.load(self.governance[operation]),
                store=self.core, connectors=self.connectors,
                observability=self.observability, spool=self.spool,
                source_ref=self.SOURCE_REF,
            )
            for name, operation in zip(("enumerator", "runner"), operations)
        }
        coordinator = FeedDiscoveryCoordinator(
            missions=self.missions, launcher=self.launcher, source_ref=self.SOURCE_REF,
            plan=load_feed_discovery_plan(PLAN_PATH), tick_budget_seconds=120.0,
            **runners,
        )
        result = coordinator.dispatch_once(universe=UNIVERSE, since=self.SINCE)
        self.assertEqual(result["status"], "dispatched", result)

    # -- what extraction sees ---------------------------------------------

    def writer(self, *, launchers=None):
        """The slice of the writer the extraction service reads."""

        installed = {self.LAUNCHER_KWARG: self.launcher} if launchers is None else launchers
        return SimpleNamespace(
            store=self.core, coverage_mission=self.missions,
            _connectors=self.connectors, observability=self.observability,
            _transcript_spool=self.spool, _scheduler=None,
            _document_extraction_model_config=None,
            _document_extraction_worker_factory=None,
            lane_launcher=installed.get,
        )

    def review(self, document_ref=None):
        reviews = [
            row for row in self.missions.document_reviews(self.mission["id"], limit=100)
            if row["state"] == "awaiting_human_extraction"
            and (document_ref is None or row["document_ref"] == document_ref)
        ]
        self.assertTrue(reviews, "the tick opened no review to read")
        return self.missions.document_review(reviews[0]["review_id"])

    def view(self, review, *, offset=0, **kwargs):
        service = DocumentExtractionService(self.writer(**kwargs))
        return service.view(review_id=review["review_id"],
                            expected_review_hash=content_hash(review),
                            offset=offset, actor_ref=AUTOMATION)

    def manifest_for(self, document_ref):
        reader = ReadOnlyFeedManifestReader(
            state_dir=self.state, source_ref=self.SOURCE_REF)
        return reader.locate_completed_manifest_binding(document_ref)


class SalesNotesReadingTests(FeedReadingHarness):
    """S1: a named sell-side note on this machine."""

    SOURCE_REF = SALES_NOTES
    GOVERNANCE_KINDS = {"list_notes": SALES_NOTES_LIST_KIND,
                        "get_note": SALES_NOTES_GET_KIND}
    LAUNCHER_KWARG = FEED_SOURCE_LAUNCHER_KWARGS[SALES_NOTES]

    def build_launcher(self):
        # No ``spool_dir``: the live shape.  The writer builds this launcher
        # without one, so the child writes its bytes into its own default,
        # ``<state>/connector-spool`` -- a different root from the one the
        # writer and the extraction child hold.  Reading has to follow the
        # bytes, and this case is what proves it does.
        return SalesNotesFeedLauncher(
            digest_dir=FIXTURES, state_dir=self.state,
            governance_paths=self.governance,
        )

    def test_the_child_wrote_its_bytes_outside_the_readers_own_spool(self) -> None:
        manifest = self.manifest_for(ACN_NOTE)["manifest"]
        digest = manifest["assembled_object"]["content_hash"]
        self.assertTrue(
            (self.state / "connector-spool" / "connector-spool" / "objects"
             / digest[:2] / digest).is_file())
        self.assertFalse(
            (self.state / "spool" / "connector-spool" / "objects"
             / digest[:2] / digest).is_file())

    def test_an_acquired_note_is_read_from_the_bytes_its_acquisition_recorded(self) -> None:
        review = self.review(ACN_NOTE)
        context = self.view(review)["context"]
        binding = self.manifest_for(ACN_NOTE)
        manifest = binding["manifest"]
        # The window is the manifest's document, identified by the manifest's
        # own declared hash -- not by anything this read computed for itself.
        self.assertEqual(context["source_manifest_ref"], manifest["id"])
        self.assertEqual(context["source_manifest_hash"], manifest["content_hash"])
        self.assertEqual(context["source_content_hash"],
                         manifest["declared_content_sha256"])
        self.assertEqual(context["total_chars"], manifest["content_chars"])
        self.assertEqual(context["source_ref"], SALES_NOTES)
        # And the quotes are that document, byte for byte.
        window = "".join(quote["raw_text"] for quote in context["quotes"])
        service = DocumentExtractionService(self.writer())
        whole = service._document_text(context)
        self.assertTrue(whole.startswith(window))
        self.assertEqual(
            hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            manifest["declared_content_sha256"],
        )
        self.assertEqual(
            RawSpool(str(self.state / "connector-spool"), max_total_bytes=1 << 30)
            .read_object(manifest["assembled_object"]["content_hash"]),
            whole.encode("utf-8"),
        )

    def test_a_note_whose_artifact_is_gone_is_refused_by_what_is_gone(self) -> None:
        review = self.review(ACN_NOTE)
        manifest = self.manifest_for(ACN_NOTE)["manifest"]
        digest = manifest["assembled_object"]["content_hash"]
        (self.state / "connector-spool" / "connector-spool" / "objects"
         / digest[:2] / digest).unlink()
        with self.assertRaises(Exception) as caught:
            self.view(review)
        self.assertIn("not found", str(caught.exception))

        # And with the acquisition ticket itself gone there is nothing left to
        # prove the document against at all.
        shutil.rmtree(self.state / "feed-acquisitions-sales-notes")
        with self.assertRaises(Exception) as caught:
            self.view(review)
        self.assertIn("unavailable", str(caught.exception))

    def test_a_source_with_no_ticket_reader_installed_is_refused_by_name(self) -> None:
        review = self.review(ACN_NOTE)
        with self.assertRaises(ResearchVerificationError) as caught:
            self.view(review, launchers={})
        self.assertIn("no acquisition ticket reader for source:sales-notes",
                      str(caught.exception))

    def test_the_sweep_reads_the_note_and_closes_its_review(self) -> None:
        review = self.review(ACN_NOTE)
        context = self.view(review)["context"]
        # P13aq: the sales note now has a citation authority of its own, so the
        # end of this sweep is a Claim rather than a dismissal.  Sign the rule
        # the workspace's own first mission signs, or the review is held on the
        # policy exactly as an AlphaEngine document would be.
        sign_document_qualitative_rule(self.core)
        summary = run_hermetic_sweep(
            self, state=self.state, root=self.root, quote_id=context["quotes"][0]["quote_id"],
            normalized_statement="The note described cautious client decisions.",
            metric_or_aspect="aspect:client-decisions",
            basis="fixture sell-side commentary",
        )
        # The queue reaches the note: nothing is refused at the door any more.
        self.assertEqual(
            [item for item in summary["skipped"]
             if "can be viewed here" in item["reason"]], [])
        self.assertEqual(summary["status"], "succeeded", summary)
        drafted = [item for item in summary["drafted"]
                   if item.get("source_ref") == SALES_NOTES]
        self.assertTrue(drafted, summary["drafted"])
        self.assertEqual({item["status"] for item in drafted}, {"succeeded"})
        resolved = {item["review_id"]: item for item in summary["resolved_reviews"]}
        self.assertTrue(resolved, summary)
        self.assertNotIn(
            SOURCE_STAGING_GATE_REASON,
            "".join(str(item.get("reason", "")) for item in resolved.values()))
        # And the number the cockpit calls "read" moves, which is the whole
        # point: the document was read to the end and proved window by window.
        proofs = self.core.connection.execute(
            "SELECT document_ref FROM document_read_completion_proofs"
        ).fetchall()
        self.assertIn(ACN_NOTE, [row["document_ref"] for row in proofs])


class CompanyWikiReadingTests(FeedReadingHarness):
    """S1: a page of the fund's own wiki."""

    SOURCE_REF = COMPANY_WIKI
    GOVERNANCE_KINDS = {"list_documents": COMPANY_WIKI_LIST_KIND,
                        "get_document": COMPANY_WIKI_GET_KIND}
    LAUNCHER_KWARG = FEED_SOURCE_LAUNCHER_KWARGS[COMPANY_WIKI]
    SINCE = "2026-07-01"

    def build_launcher(self):
        index, corpus = build_wiki(self.root / "wiki")
        return CompanyWikiFeedLauncher(
            index_db=index, corpus_root=corpus, state_dir=self.state,
            governance_paths=self.governance, spool_dir=self.state / "spool",
        )

    def test_an_acquired_wiki_page_is_read_from_its_recorded_artifact(self) -> None:
        review = self.review()
        context = self.view(review)["context"]
        manifest = self.manifest_for(review["document_ref"])["manifest"]
        self.assertEqual(context["source_ref"], COMPANY_WIKI)
        self.assertEqual(context["source_content_hash"],
                         manifest["declared_content_sha256"])
        whole = DocumentExtractionService(self.writer())._document_text(context)
        self.assertEqual(
            hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            manifest["declared_content_sha256"],
        )
        self.assertTrue(whole.startswith(context["quotes"][0]["raw_text"][:200]))


class PriorResearchReadingTests(FeedReadingHarness):
    """W3: this fund's own earlier work, read back the same way."""

    SOURCE_REF = PRIOR_RESEARCH
    GOVERNANCE_KINDS = {"list_documents": PRIOR_RESEARCH_LIST_KIND,
                        "get_document": PRIOR_RESEARCH_GET_KIND}
    LAUNCHER_KWARG = FEED_SOURCE_LAUNCHER_KWARGS[PRIOR_RESEARCH]

    def build_launcher(self):
        from dalton_core.prior_research_launcher import PriorResearchFeedLauncher
        from tests.test_prior_research import build_corpus

        # Inside one enumeration window: every window is a governed child.
        as_of = (date.today() - timedelta(days=3)).isoformat()
        self.SINCE = (date.today() - timedelta(days=6)).isoformat()
        corpus = build_corpus(self.root / "corpus", as_of=as_of)
        return PriorResearchFeedLauncher(
            corpus_root=corpus, state_dir=self.state,
            governance_paths=self.governance, spool_dir=self.state / "spool",
        )

    def test_an_acquired_prior_document_is_read_from_its_recorded_artifact(self) -> None:
        review = self.review()
        context = self.view(review)["context"]
        manifest = self.manifest_for(review["document_ref"])["manifest"]
        self.assertEqual(context["source_ref"], PRIOR_RESEARCH)
        self.assertEqual(context["source_content_hash"],
                         manifest["declared_content_sha256"])
        # 0.2 manifests also bind the original file and its projection, and
        # ``verified_feed_source`` re-renders it before this text is returned.
        self.assertEqual(manifest["schema_version"], "0.2")
        whole = DocumentExtractionService(self.writer())._document_text(context)
        self.assertEqual(
            hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            manifest["declared_content_sha256"],
        )


class GuidepointReadingTests(unittest.TestCase):
    """S2: an excerpt derived from the search that returned it."""

    def setUp(self) -> None:
        from datetime import date as date_type

        from dalton_core.guidepoint_cli import run_acquisition, run_discovery
        from dalton_core.guidepoint_launcher import GuidepointAcquisitionLauncher
        from dalton_core.guidepoint_search import FakeGuidepointHandle
        from dalton_core.store import canonical_json
        from tests.test_guidepoint_lane import (
            ACN,
            LaneHarness,
            approved_governance,
            patched_discovery_sources,
            small_plan,
        )
        from tests.test_guidepoint_search_lane import ROWS, Clock

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        plan = small_plan()
        lane = LaneHarness(self.state, Clock())
        mission = lane.mission
        lane.close()  # every child below opens its own connection on this file
        with patched_discovery_sources():
            discovery = run_discovery(
                state_dir=self.state, governance=approved_governance(), plan=plan,
                company_ref=ACN, spec_ref=plan["specs"][0]["spec_ref"],
                requested_by=AUTOMATION, mission_version_ref=mission["id"],
                mission_version_hash=mission["content_hash"],
                as_of=date_type(2026, 9, 9), handle=FakeGuidepointHandle(ROWS),
                transport="fixture", summary_dir=self.root / "discovery",
                spool_dir=self.state / "connector-spool",
            )
        self.assertEqual(discovery["status"], "succeeded", discovery["failure_reason"])
        self.document_ref = discovery["search"]["document_refs"][0]

        # The acquisition child, writing into the ticket directory the review
        # path searches -- the launcher's own, exactly as a launched run does.
        self.launcher = GuidepointAcquisitionLauncher(state_dir=self.state)
        self.addCleanup(self.launcher.close)
        digest = "a" * 24
        ticket_dir = self.launcher.tickets_dir / digest
        ticket_dir.mkdir(parents=True, exist_ok=True)
        acquired = run_acquisition(
            state_dir=self.state,
            source_envelope_ref=discovery["search"]["source_envelope_ref"],
            document_ref=self.document_ref, summary_dir=ticket_dir,
            spool_dir=self.state / "connector-spool",
        )
        self.assertEqual(acquired["status"], "succeeded", acquired["failure_reason"])
        self.manifest = json.loads((ticket_dir / "manifest.json").read_text())
        self.ticket_ref = f"guidepoint-acquire-run:{digest}"
        ticket = ticket_dir / "ticket.json"
        ticket.write_text(canonical_json({
            "schema_version": "0.1", "id": self.ticket_ref,
            "source_ref": "source:guidepoint", "document_ref": self.document_ref,
            "status": "succeeded", "started_at": "2026-09-09T00:00:00.000000+00:00",
        }) + "\n", encoding="utf-8")
        os.chmod(ticket, 0o600)
        for name in ("summary.json", "manifest.json"):
            os.chmod(ticket_dir / name, 0o600)

        self.core = DaltonStore(str(self.state / "core.sqlite"))
        self.addCleanup(self.core.close)
        self.connectors = ConnectorStore(self.core)
        self.observability = ObservabilityStore(self.core)
        self.spool = RawSpool(str(self.state / "connector-spool"),
                              max_total_bytes=50_000_000)
        self.missions = CoverageMissionAuthority(self.core)
        document = next(
            row for row in self.missions.discovered_documents(mission["id"], limit=50)
            if row["document_ref"] == self.document_ref)
        self.missions.mark_discovered_document_launched(
            document["record_id"], self.ticket_ref)
        self.missions.settle_discovered_document(document["record_id"], status="acquired")
        opened = self.missions.register_document_review(
            document["record_id"], requested_by=AUTOMATION)
        self.review = self.missions.document_review(opened["review_id"])
        self.patched = patched_discovery_sources()
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def writer(self, *, installed=True):
        lane = SimpleNamespace(acquisition_launcher=self.launcher) if installed else None
        return SimpleNamespace(
            store=self.core, coverage_mission=self.missions,
            _connectors=self.connectors, observability=self.observability,
            _transcript_spool=self.spool, _scheduler=None,
            _document_extraction_model_config=None,
            _document_extraction_worker_factory=None,
            lane_launcher=lambda kwarg: lane if kwarg == GUIDEPOINT_LAUNCHER_KWARG else None,
        )

    def view(self, *, installed=True):
        service = DocumentExtractionService(self.writer(installed=installed))
        return service, service.view(
            review_id=self.review["review_id"],
            expected_review_hash=content_hash(self.review),
            offset=0, actor_ref=AUTOMATION)

    def test_an_acquired_excerpt_is_viewable_and_is_the_excerpt(self) -> None:
        service, view = self.view()
        context = view["context"]
        self.assertEqual(context["source_ref"], "source:guidepoint")
        self.assertEqual(context["source_manifest_ref"], self.manifest["id"])
        self.assertEqual(context["source_content_hash"],
                         self.manifest["declared_content_sha256"])
        self.assertEqual(context["total_chars"], self.manifest["content_chars"])
        whole = service._document_text(context)
        self.assertEqual(
            hashlib.sha256(whole.encode("utf-8")).hexdigest(),
            self.manifest["declared_content_sha256"],
        )
        self.assertTrue(whole.startswith(context["quotes"][0]["raw_text"]))

    def test_an_excerpt_with_no_acquisition_reader_installed_is_refused(self) -> None:
        with self.assertRaises(ResearchVerificationError) as caught:
            self.view(installed=False)
        self.assertIn("no acquisition ticket reader for source:guidepoint",
                      str(caught.exception))


if __name__ == "__main__":
    unittest.main()
