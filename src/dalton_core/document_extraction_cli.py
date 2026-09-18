"""Out-of-process document extraction child (P9d-17a, ADR-0005).

The writer serialises every request through one 30 s executor, so a model
call that can take a minute cannot run inside it without stalling the
cockpit and the controller tick.  Like search, fetch and acquisition, drafting
therefore runs in a child the writer spawns and tracks through a ticket.

One run drafts at most ``--max-windows`` windows across the reviews that are
awaiting extraction, in the mission's own priority order, under each review's grant and
budget.  It reuses ``DocumentExtractionService`` unchanged: the same context,
prompt, output schema, budget admission and persisted, replayable results a
human-triggered draft would produce.  A window whose result already exists is
skipped (replay is free), so re-running only ever advances.

Outputs (``--summary-dir``, owner-only): ``summary.json``.  Exit 0 when the
run completed (even with nothing to draft), 1 on failure.  ``formal_authority_writes``
is always 0: this child drafts suggestions; staging and admission are P9d-17b.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .alphaengine_acquisition_launcher import AlphaEngineAcquisitionLauncher
from .connector import ConnectorStore
from .coverage_mission import CoverageMissionAuthority
from .extraction_priority import review_sort_key as evidence_review_sort_key
from .mission_stage import company_priority_order, review_sort_key
from .document_extraction import (
    FEED_SOURCE_LAUNCHER_KWARGS,
    GUIDEPOINT_LAUNCHER_KWARG,
    SOURCE_STAGING_GATE_REASON,
    STAGEABLE_SOURCE_REFS,
    DocumentExtractionModelWorker,
    DocumentExtractionService,
    HermeticExtractionAdapter,
    validate_model_config,
)
from .document_extraction_windows import (
    PLAN_UNDIRECTED_RANK,
    ExtractionWindowLedger,
    classify_window_failure,
    plan_rank_for,
    plan_reading_ranks,
    replayed_window_failure,
)
from .document_read_completion import DocumentReadCompletionAuthority
from .observability import ObservabilityStore
from .public_web_fetch_launcher import PublicWebFetchLauncher
from .raw_spool import RawSpool
from .scheduler import Scheduler
from .document_extraction import extraction_scheduler_policy
from .store import DaltonStore, canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_MAX_WINDOWS = 2
# Answers that cost no model call, so they cost no allowance either.
FREE_STATUSES = frozenset({"nothing_owed", "not_graded", "not_attributed"})

# C2-1: how many windows may fail deterministically, in a row, before the run
# stops calling the provider anyway.  Isolating a settled failure is safe; a
# provider that refuses thirty windows in a row is an outage, and reading into
# an outage one window at a time is thirty ways of learning the same thing.
CONSECUTIVE_CERTAIN_FAILURE_LIMIT = 5

# C2-2: the share of one tick's windows that belongs to the evidence the
# README puts first -- filings, then management's own words, then the brokers
# who cover the name.  It is a reservation, not a quota: if no such document is
# waiting, the whole tick still goes to whatever is.  Live on 2026-09-16, 60%
# of 415 read proofs were `management-changes` news pages while five 10-Ks and
# fifty transcripts sat acquired and unread, which is what a lane with no
# reservation does once news outnumbers filings a hundred to one.
HIGH_PRIORITY_WINDOW_SHARE = 0.5
# Tiers that reservation protects, in ``document_provenance`` terms.
HIGH_PRIORITY_TIERS = ("filing", "management", "sell_side")


def secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class ExtractionHost:
    """The slice of the writer the extraction service reads; opened in this process."""

    def __init__(self, *, state_dir: Path, spool_dir: Path, scheduler_db: Path,
                 connector_governance: Path | None, web_fetch_governance: Path | None,
                 model_config: dict[str, Any] | None, candidate_staging: Path | None = None) -> None:
        self.store = DaltonStore(str(state_dir / "core.sqlite"))
        self._connectors = ConnectorStore(self.store)
        self.observability = ObservabilityStore(self.store)
        self.coverage_mission = CoverageMissionAuthority(self.store)
        scheduler_kwargs = (extraction_scheduler_policy(model_config)
                            if model_config is not None else {})
        self._scheduler = Scheduler(str(scheduler_db), **scheduler_kwargs)
        self._transcript_spool = RawSpool(str(spool_dir), max_total_bytes=1_000_000_000)
        # Launchers are used read-only here (read_completed_manifest); they
        # never spawn from this process.  Governance is loaded only on start.
        self.acquisition_launcher = AlphaEngineAcquisitionLauncher(
            state_dir=state_dir, governance_path=connector_governance or (state_dir / "unused-governance.json"),
            spool_dir=spool_dir,
        )
        self.web_fetch_launcher = PublicWebFetchLauncher(
            state_dir=state_dir, governance_path=web_fetch_governance or (state_dir / "unused-governance.json"),
            spool_dir=spool_dir,
        )
        # P13ap: the corpora read through their own ticket directories.  Each
        # reader is opened on first use and only then: a workspace with no
        # Guidepoint lane has no ``guidepoint-acquire-runs`` directory, and
        # opening one here would be this read-only child creating lane state.
        self.state_dir = state_dir
        self._lane_readers: dict[str, Any] = {}
        self._document_extraction_model_config = model_config
        self._document_extraction_worker_factory = None
        # ADR-0005 / P9d-17b: staging and admission need the shared candidate
        # staging authority the cockpit review plane also opens.
        self._candidate_staging = None
        self._candidate_review = None
        if candidate_staging is not None:
            from .research_review import HumanReviewAuthority
            from .research_verification import CandidateStagingStore
            self._candidate_staging = CandidateStagingStore(str(candidate_staging))
            self._candidate_review = HumanReviewAuthority(str(candidate_staging))
        # The service opens the router and the budget ledger read-only to
        # bind a context, and a read-only WAL open refuses when nothing
        # holds the file (no sidecars).  The thesis-impact ledger is closed
        # between that lane's runs, so this child keeps both open, without
        # writing, for as long as it lives.  First live run failed exactly
        # here: "read_only WAL requires existing WAL/SHM".
        self._keepalive: list[Any] = []
        if model_config is not None:
            from .model_router import ModelRouter
            from .thesis_impact_budget import ThesisImpactBudgetStore
            self._keepalive.append(ThesisImpactBudgetStore(model_config["budget_db"]))
            self._keepalive.append(ModelRouter(model_config["model_router_db"]))

    def lane_launcher(self, init_kwarg: str) -> Any | None:
        """The read-only manifest reader for one lane, opened on demand.

        Mirrors ``WriterServer.lane_launcher`` so the extraction service needs
        one resolution path, not one per host.  Nothing is spawned and nothing
        is written: a feed reader refuses outright when its ticket directory is
        not there, which is the refusal the queue should see.
        """

        if init_kwarg in self._lane_readers:
            return self._lane_readers[init_kwarg]
        reader: Any = None
        feeds = {kwarg: source for source, kwarg in FEED_SOURCE_LAUNCHER_KWARGS.items()}
        if init_kwarg in feeds:
            from .feed_launcher import ReadOnlyFeedManifestReader

            reader = ReadOnlyFeedManifestReader(
                state_dir=self.state_dir, source_ref=feeds[init_kwarg])
        elif init_kwarg == GUIDEPOINT_LAUNCHER_KWARG:
            from types import SimpleNamespace

            from .guidepoint_launcher import GuidepointAcquisitionLauncher

            # No spool directory: this launcher is opened to read tickets, not
            # to spawn, and ``spool_dir`` means "where the child I spawn
            # writes".  Saying this child's own spool there would be a claim
            # about the acquisition that is not true.
            reader = SimpleNamespace(
                acquisition_launcher=GuidepointAcquisitionLauncher(state_dir=self.state_dir))
        self._lane_readers[init_kwarg] = reader
        return reader

    @property
    def candidate_staging(self) -> Any:
        if self._candidate_staging is None:
            raise RuntimeError("candidate staging is not configured for this child")
        return self._candidate_staging

    @property
    def candidate_review(self) -> Any:
        if self._candidate_review is None:
            raise RuntimeError("candidate staging is not configured for this child")
        return self._candidate_review

    def _read_transcript_artifact(self, artifact: Any) -> bytes:
        return self._transcript_spool.read_object(artifact["artifact_content_hash"])

    def _transcript_support_authority(self, authority_ref: str) -> dict[str, Any]:
        # Mirrors WriterServer._transcript_support_authority.
        from .observability import ObservabilityNotFound
        from .transcript_correction import TranscriptCorrectionNotFound
        evidence = self.store.connection.execute(
            "SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?", (authority_ref,),
        ).fetchone()
        if evidence is not None:
            return json.loads(evidence["evidence_json"])
        try:
            return self.observability.get_artifact_version_v2(authority_ref)
        except ObservabilityNotFound:
            pass
        source = self.store.connection.execute(
            "SELECT record_json FROM connector_source_envelopes WHERE source_envelope_id=?", (authority_ref,),
        ).fetchone()
        if source is not None:
            return json.loads(source["record_json"])
        raise TranscriptCorrectionNotFound(authority_ref)

    def _transcript_corrections(self, source_manifest: Any) -> tuple[Any, dict[str, Any]]:
        # Mirrors WriterServer._transcript_corrections.
        from .alphaengine_document_acquisition import validate_alphaengine_document_acquisition_manifest
        from .transcript_correction import TranscriptCorrectionAuthority
        manifest = validate_alphaengine_document_acquisition_manifest(source_manifest)
        authority = TranscriptCorrectionAuthority(
            self.store, spool=self._transcript_spool,
            manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
            evidence_resolver=self._transcript_support_authority,
        )
        return authority, manifest

    def _public_web_corrections(self, source_manifest: Any) -> tuple[Any, dict[str, Any]]:
        """ADR-0005 / P9d-17c: the correction authority over a fetched page."""

        from .public_web_core_fetch import validate_public_web_fetch_manifest
        from .transcript_correction import TranscriptCorrectionAuthority
        manifest = validate_public_web_fetch_manifest(source_manifest)
        authority = TranscriptCorrectionAuthority(
            self.store, spool=self._transcript_spool,
            manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
            evidence_resolver=self._transcript_support_authority,
        )
        return authority, manifest

    def _acquired_source_corrections(self, source_manifest: Any, spool: Any) -> tuple[Any, dict[str, Any]]:
        """P13aq: mirrors WriterServer._acquired_source_corrections.

        The spool comes from the caller because the corpora children write into
        their own root, which is the same resolution the reading path already
        does for this document.
        """

        from .acquired_source_authority import acquired_source_correction_authority

        return acquired_source_correction_authority(
            self.store, spool=spool, manifest=source_manifest,
            evidence_resolver=self._transcript_support_authority,
        )

    def close(self) -> None:
        for handle in reversed(self._keepalive):
            try:
                handle.close()
            except Exception:
                pass
        self._scheduler.close()
        self.store.close()


def numeric_worthy(spec_ref: Any) -> bool:
    """Whether a figure may be read out of this kind of document at all.

    P11o gated this by source, which was too coarse to be safe: AlphaEngine
    holds both earnings-call transcripts and sell-side research, and a note
    saying "we model revenue of $17.9bn" passes a digit check perfectly while
    being the analyst's estimate rather than the company's result. The gate is
    therefore the document kind, and it is the same list that decides a
    figure's grade -- a kind with no grade is not read for figures.
    """

    from .document_figure_grade import figure_worthy

    # P13c: whether the *document* is about this company is decided per
    # document, inside the pass, because it needs the document's text -- an
    # industry report with no company tag is still worth reading, and a
    # transcript of another company's call is not.
    return figure_worthy(spec_ref)


def discovery_worthy(spec_ref: Any) -> bool:
    """Whether this document kind says what the market judges a company on.

    The mirror image of ``numeric_worthy``: the figures pass wants filings,
    which report; this pass wants the notes and releases that react, which is
    where the names of the measures that matter actually appear.
    """

    from .metric_discovery_extraction import worthy_spec

    return worthy_spec(spec_ref)


def run_extraction(
    *,
    state_dir: Path,
    model_config_path: Path,
    summary_dir: Path,
    spool_dir: Path | None,
    scheduler_db: Path | None,
    connector_governance: Path | None,
    web_fetch_governance: Path | None,
    max_windows: int,
    max_numeric_windows: int = 0,
    max_discovery_windows: int = 0,
    requested_by: str | None,
    hermetic_fixture: Path | None,
    candidate_staging: Path | None = None,
) -> dict[str, Any]:
    state = secure_dir(state_dir)
    out = secure_dir(summary_dir)
    spool_root = spool_dir if spool_dir is not None else state / "transcript-spool"
    raw_config = json.loads(Path(model_config_path).read_text(encoding="utf-8"))
    config = validate_model_config(raw_config)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "mode": "hermetic_fixture" if hermetic_fixture is not None else "broker",
        "routing_policy_ref": config["routing_policy_ref"],
        "max_windows": max_windows,
        "requested_by": requested_by,
        "reviews_scanned": 0,
        "reviews_complete": 0,
        "drafted": [],
        "qualitative_failures": [],
        # P11m: the figures pass, counted separately from the prose pass. A
        # window that owes nothing appears here as nothing_owed and costs no
        # model call, so the two counts are not interchangeable.
        #
        # P11w: ``figures`` counts figures *stored* -- verified against their
        # own citation and graded by their document -- not figures a model
        # returned. A verified figure already held is a duplicate, not a
        # second figure.
        "numeric": [],
        "figures": 0,
        "max_numeric_windows": max_numeric_windows,
        # Windows each secondary pass newly paid for. A replayed window is not
        # counted, so zero means "this pass has nothing left to read", which is
        # a different thing from "the prose queue is empty".
        "numeric_fresh": 0,
        "discovery_fresh": 0,
        # P11r: the pass that learns what to ask for. ``metrics_observed``
        # counts journal entries, not requirements: a requirement needs two
        # distinct documents and is derived when it is read.
        "discovery": [],
        "metrics_observed": 0,
        "max_discovery_windows": max_discovery_windows,
        "skipped": [],
        # P10x: what past searches said about the documents they returned --
        # publisher and company list -- kept rather than dropped. Best effort:
        # a pruned artefact is one skipped envelope, never a failed run.
        "provenance": [],
        # P10x: a review whose original cannot be read at all -- an encrypted
        # PDF, bytes that are not UTF-8, an acquisition whose ticket is gone.
        # Live, six of fourteen open reviews were of this kind and stayed open
        # forever, so the lane relaunched hourly against work that can never
        # complete and the queue depth never told anyone why.
        "unreadable_reviews": [],
        "admitted": [],
        "resolved_reviews": [],
        # C2-1: windows whose failure was settled -- no open reservation, no
        # unknown send -- so the run isolated them and read on.  The entry
        # carries the classification, so the summary says why each one was
        # safe to step over rather than asking the reader to trust it.
        "isolated_windows": [],
        # Windows a previous run already proved dead, stepped over without
        # re-rendering the document.  Live this was 24% of all window reads.
        "windows_skipped_by_exclusion": 0,
        # C2-3: the reviews a secondary pass finished with this tick, and why.
        # One entry per review, written to the ledger as well, so the same
        # document is not re-derived on the next tick to learn the same
        # nothing.  Live this was four reviews and 107 windows, every five
        # minutes, for two hours.
        "exhausted_reviews": [],
        # C2-3: where each secondary pass actually got to -- the first window
        # it newly paid for, how many it paid for, and how many reviews it
        # stepped over as already exhausted.  A lane that is stuck reads
        # ``review_id: null`` with a rising ``reviews_skipped_exhausted``.
        "advanced_to": {},
        # C2-2: how the tick's windows were actually divided.
        "priority_windows": {"high_tier": 0, "other": 0, "high_tier_reserved": 0},
        # G1: windows this tick spent on a document the mission's latest
        # research plan actually named.  Zero on an install with no plan, and
        # zero is also what every run reported before the planner's reading
        # directives had a consumer -- which is the number this exists to move.
        "plan_directed_windows": 0,
        "plan_directed": {
            # One entry per mission: which plan was used, or why none was.
            "plans": [],
            # Open reviews the plan named, and the ones it wants figures from.
            "reviews": 0,
            "figure_reviews": 0,
            # The resolution itself -- directive to document to review -- for
            # the first few, so "the plan directed this" is checkable rather
            # than asserted.
            "documents": [],
        },
        "stop_reason": None,
        "status": "failed",
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    host = ExtractionHost(
        state_dir=state, spool_dir=spool_root,
        scheduler_db=scheduler_db if scheduler_db is not None else state / "scheduler.sqlite",
        connector_governance=connector_governance, web_fetch_governance=web_fetch_governance,
        model_config=None if hermetic_fixture is not None else config,
        candidate_staging=candidate_staging,
    )
    try:
        if hermetic_fixture is not None:
            # Test-only: the same routed worker over a zero-cost fixture
            # adapter; the router must already carry a fixture profile/policy.
            from .model_router import ModelRouter
            output = json.loads(hermetic_fixture.read_text(encoding="utf-8"))
            router = ModelRouter(config["model_router_db"])
            adapter = HermeticExtractionAdapter(output, created_at=datetime.now(timezone.utc).isoformat())

            def factory(service: DocumentExtractionService, context: Any, actor: str) -> DocumentExtractionModelWorker:
                return DocumentExtractionModelWorker(
                    scheduler=host._scheduler, router=router, store=host.store, observability=host.observability,
                    adapter=adapter, routing_policy_ref=config["routing_policy_ref"],
                    credential_slot_refs=list(config["credential_slot_refs"]),
                    context_resolver=lambda c: service.reread(c, actor),
                )
            host._document_extraction_worker_factory = factory
        service = DocumentExtractionService(host)
        windows = ExtractionWindowLedger(host.store.connection)
        # The identity the window verdicts are scoped to: swapping the model
        # re-opens every window a previous model could not answer.
        window_config_hash = content_hash(config)
        drafted = 0
        # C2-2: windows this tick keeps for filings, transcripts and covering
        # brokers.  ``low_tier_budget`` is what everything else may spend.
        reserved_high = int(max_windows * HIGH_PRIORITY_WINDOW_SHARE)
        summary["priority_windows"]["high_tier_reserved"] = reserved_high
        low_tier_budget = max_windows - reserved_high
        # C2-1: how many settled failures the run has isolated back to back.
        # Reset by any success, so an outage still stops the run quickly while
        # a scattering of dead windows never does.
        consecutive_certain_failures = 0
        lanes: list[tuple[str, list[dict[str, Any]], dict[str, str]]] = []
        held_lanes: list[tuple[str, list[dict[str, Any]], dict[str, str]]] = []
        # G1: the same held documents, ordered for the figures pass instead --
        # a plan's ``extract_figures`` directive is an instruction to that pass
        # and to no other, so it may not reorder the prose or discovery queues.
        numeric_lanes: list[tuple[str, list[dict[str, Any]], dict[str, str]]] = []
        # Reviews the plan asked figures from, across every mission, so the
        # figures sweep can count the windows it spends on them.
        numeric_directed: set[str] = set()
        stop_reason: str | None = None
        complete_reviews: list[tuple[dict[str, Any], str, str, list[int]]] = []
        pointers = host.store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall()
        for pointer in pointers:
            if stop_reason is not None:
                break
            mission = host.coverage_mission.mission(pointer["mission_version_id"])
            actor = requested_by or mission["autonomy"]["automation_principal"]
            # G1: what the mission's newest plan named, read before the queue
            # so the plan's order can lead the queue's rather than be applied
            # to whatever survived the slice below.
            directed = _plan_directed_ranks(host, mission)
            if directed["plan"] is not None:
                summary["plan_directed"]["plans"].append(directed["plan"])
            # C2-2: the 500-row slice has to be the *right* 500.
            #
            # ``document_reviews`` orders by ``created_at, review_id`` and then
            # truncates, so the in-process priority sort below could only ever
            # reorder the oldest five hundred.  With 1,482 reviews open on
            # 2026-09-16 that is not a reordering, it is a filter -- and the
            # thing it filtered out was every filing and transcript queued
            # after the first five hundred news pages.  Ask the database for
            # the queue in reading order instead.
            reviews = _awaiting_reviews_in_reading_order(
                host, mission, limit=500, plan_ranks=directed["ranks"])
            # P11u/P11y: both secondary passes read documents the queue has
            # already closed, so they share a list of everything held. The
            # prose pass laps them -- 30 windows a tick against their 10 -- and
            # on the open queue alone they are locked out of a document before
            # they have finished reading it.
            held = host.coverage_mission.document_reviews(mission["id"], limit=500)
            # P10a: read in the mission's own order — the P0 company before the
            # P2 one, and management's own words before someone else's summary
            # of them.  Age only breaks ties.
            #
            # P10x: which company a document is *about* matters less than what
            # kind of document it is, so evidence value leads and company
            # priority breaks ties inside a kind. Between two documents of one
            # kind the thinner company reads first: the live shape this was
            # written for is Cognizant holding 149 sell-side Claims while
            # Accenture holds five, because Cognizant's queries returned more
            # of the same brokers' notes first.
            specs: dict[str, str] = {}
            rank: dict[str, int] = {}
            summary["provenance"].append(
                {"mission_version_ref": mission["id"], **_record_provenance(host)})

            def _plan_rank(review: Mapping[str, Any], spec_by_document: Mapping[str, str],
                           ranks: Mapping[tuple[str, str], int] = directed["ranks"]) -> int:
                # G1: this mission's ranks are bound at definition, so the
                # closure cannot be re-read against a later mission's plan.
                return plan_rank_for(
                    ranks, company_ref=review.get("company_ref"),
                    spec_ref=spec_by_document.get(review.get("document_ref")))

            try:
                rank = {ref: index for index, ref in enumerate(company_priority_order(mission))}
                specs = host.coverage_mission.document_spec_refs(mission["id"])
                thin = _thinness_ranks(host, mission)
                reviews = sorted(reviews, key=lambda review: (
                    _plan_rank(review, specs),
                    evidence_review_sort_key(
                        review, company_rank=rank, spec_by_document=specs,
                        thinness_rank=thin, now=datetime.now(timezone.utc))))
            except Exception:  # noqa: BLE001 - ordering is not a gate
                thin = {}
                try:
                    reviews = sorted(reviews, key=lambda review: (
                        _plan_rank(review, specs),
                        review_sort_key(
                            review, company_rank=rank, spec_by_document=specs)))
                except Exception:  # noqa: BLE001 - nor is its fallback
                    pass
            lanes.append((actor, reviews, specs))
            # G1: the directive-to-document-to-review resolution, written down
            # once here so every later count means the same thing.
            directed_reviews = {
                review["review_id"] for review in reviews
                if _plan_rank(review, specs) < PLAN_UNDIRECTED_RANK
            }
            summary["plan_directed"]["reviews"] += len(directed_reviews)
            for review in reviews:
                position = _plan_rank(review, specs)
                if position >= PLAN_UNDIRECTED_RANK:
                    continue
                if len(summary["plan_directed"]["documents"]) >= 20:
                    break
                summary["plan_directed"]["documents"].append({
                    "review_id": review["review_id"],
                    "document_ref": review["document_ref"],
                    "company_ref": review["company_ref"],
                    "spec_ref": specs.get(review["document_ref"]),
                    "plan_rank": position,
                })
            try:
                held = sorted(held, key=lambda review: evidence_review_sort_key(
                    review, company_rank=rank, spec_by_document=specs, thinness_rank=thin,
                    now=datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001 - ordering is not a gate
                pass
            held_lanes.append((actor, held, specs))
            # G1: the plan's ``extract_figures`` directives, applied to the
            # figures pass only.  ``sorted`` is stable, so everything the plan
            # did not ask figures from keeps the evidence order it already had.
            figures_ranks = directed["numeric"]
            numeric_held = sorted(held, key=lambda review: plan_rank_for(
                figures_ranks, company_ref=review.get("company_ref"),
                spec_ref=specs.get(review.get("document_ref"))))
            numeric_lanes.append((actor, numeric_held, specs))
            figure_reviews = {
                review["review_id"] for review in held
                if plan_rank_for(figures_ranks, company_ref=review.get("company_ref"),
                                 spec_ref=specs.get(review.get("document_ref")))
                < PLAN_UNDIRECTED_RANK
            }
            numeric_directed |= figure_reviews
            summary["plan_directed"]["figure_reviews"] += len(figure_reviews)
            # The reservation only bites when there is something to reserve
            # windows *for*.  A queue of nothing but news pages -- or a run
            # whose spec lookup failed -- must still read at full rate.
            high_priority_waiting = any(
                is_high_priority_spec(specs.get(item["document_ref"]))
                for item in reviews
            )
            for review in reviews:
                if stop_reason is not None:
                    break
                high_priority = is_high_priority_spec(specs.get(review["document_ref"]))
                if not high_priority and high_priority_waiting and low_tier_budget <= 0:
                    # The tick's reservation is doing its job: what is left of
                    # this batch belongs to a filing, a transcript or a
                    # covering broker's note.  Keep scanning for one.
                    continue
                summary["reviews_scanned"] += 1
                review_hash = content_hash(review)
                excluded = windows.exclusions(
                    review["review_id"], review_hash,
                    model_config_hash=window_config_hash)
                offset = 0
                complete = True
                offsets: list[int] = []
                while True:
                    # C2-1: a window a previous run proved dead is stepped over
                    # without re-deriving its context -- which means without
                    # re-fetching, re-rendering and re-hashing the whole
                    # document.  The review cannot complete either way, so the
                    # only thing the old behaviour bought was the I/O.
                    dead = excluded.get(offset)
                    if dead is not None:
                        summary["windows_skipped_by_exclusion"] += 1
                        complete = False
                        if dead["next_offset"] is None:
                            break
                        offset = int(dead["next_offset"])
                        continue
                    try:
                        view = service.view(review_id=review["review_id"], expected_review_hash=review_hash,
                                            offset=offset, actor_ref=actor)
                    except Exception as exc:
                        # One review that cannot be bound (stale grant, missing
                        # manifest, unreadable original) must not end the run
                        # for the rest; it is reported with its reason.  Live,
                        # one such review aborted a whole run.
                        reason = f"{type(exc).__name__}: {exc}"
                        summary["skipped"].append({"review_id": review["review_id"], "offset": offset,
                                                   "reason": reason})
                        # P10x: separate "could not be read this time" from
                        # "cannot be read at all". The second kind never
                        # completes, so it never resolves, so it holds the
                        # queue open forever without anyone being told.
                        if _permanently_unreadable(reason, offset=offset):
                            unreadable = {
                                "review_id": review["review_id"], "company_ref": review["company_ref"],
                                "source_ref": review["source_ref"], "document_ref": review["document_ref"],
                                "spec_ref": specs.get(review["document_ref"]), "reason": reason,
                                "read_complete": False, "claim_produced": False,
                            }
                            try:
                                parked = host.coverage_mission.resolve_document_review(
                                    review["review_id"], resolution="dismissed", actor_ref=actor,
                                    rationale=(
                                        "Document original is durably unreadable; no read-completion "
                                        f"receipt or Claim was produced. {reason}"
                                    ),
                                    expected_review_hash=review_hash,
                                )
                                unreadable["park_status"] = parked["status"]
                                unreadable["review_state"] = parked["state"]
                                summary["resolved_reviews"].append({
                                    "review_id": review["review_id"],
                                    "status": "dismissed_unreadable",
                                    "read_complete": False,
                                    "claim_produced": False,
                                    "reason": reason,
                                })
                            except Exception as park_exc:  # noqa: BLE001 - keep the failed view visible
                                unreadable["park_status"] = "unresolved"
                                unreadable["park_reason"] = (
                                    f"{type(park_exc).__name__}: {park_exc}"
                                )
                            summary["unreadable_reviews"].append(unreadable)
                        complete = False
                        break
                    context = view["context"]
                    offsets.append(offset)
                    if view["status"] == "not_generated":
                        if not view["generation_enabled"]:
                            stop_reason = f"gated:{view['gate_reason']}"
                            complete = False
                            break
                        if drafted >= max_windows:
                            stop_reason = "max_windows"
                            complete = False
                            break
                        if not _daily_read_admit(
                            host.store.connection, mission, review["document_ref"],
                        ):
                            # The owner's document-per-day reading stopper.
                            # Soft like max_windows: the review stays open and
                            # the next day (UTC) picks it up where it left off.
                            stop_reason = "daily_read_limit"
                            complete = False
                            break
                        result = service.generate(
                            review_id=review["review_id"], expected_review_hash=review_hash, offset=offset,
                            expected_context_hash=context["content_hash"], actor_ref=actor,
                        )
                        drafted += 1
                        if review["review_id"] in directed_reviews:
                            # G1: a window the plan asked for, paid for. The
                            # metric to watch after deploy.
                            summary["plan_directed_windows"] += 1
                        entry = {"review_id": review["review_id"], "source_ref": review["source_ref"],
                                 "document_ref": review["document_ref"], "offset": offset,
                                 "status": result.get("status"),
                                 "suggestions": len(result.get("suggestions", []))}
                        for field in ("error_code", "work_order_ref"):
                            value = result.get(field)
                            if isinstance(value, str) and value:
                                entry[field] = value
                        budget = result.get("model_budget") or {}
                        if isinstance(budget, dict) and budget.get("status"):
                            entry["budget"] = budget["status"]
                        summary["drafted"].append(entry)
                        if high_priority:
                            summary["priority_windows"]["high_tier"] += 1
                        else:
                            summary["priority_windows"]["other"] += 1
                            low_tier_budget -= 1
                        if result.get("status") != "succeeded":
                            summary["qualitative_failures"].append(dict(entry))
                        else:
                            consecutive_certain_failures = 0
                        if result.get("status") == "gated":
                            stop_reason = f"gated:{result.get('reason')}"
                            complete = False
                            break
                        if result.get("status") == "failed":
                            # C2-1.  The 2026-09-14 rule stands where it was
                            # written: a call whose cost is *unknown* ends the
                            # batch, keeps its reservation open and is never
                            # retried under a new identity.  It was applied to
                            # every failure, which is why one refused window
                            # cancelled the other twenty-nine -- and live, 565
                            # of 991 failures never reserved a micro and 66
                            # more had already settled their exact cost.  Those
                            # are settled facts about one window; isolate them.
                            verdict = classify_window_failure(
                                error_code=entry.get("error_code"),
                                budget_status=entry.get("budget"),
                            )
                            complete = False
                            if not verdict["cost_certain"]:
                                stop_reason = "systemic_model_failure"
                                summary["blocked_window"] = {**entry, **verdict}
                                break
                            consecutive_certain_failures += 1
                            try:
                                windows.exclude(
                                    review_id=review["review_id"],
                                    source_review_hash=review_hash,
                                    offset=offset, next_offset=context["next_offset"],
                                    classification=verdict,
                                    document_ref=review["document_ref"],
                                    company_ref=review["company_ref"],
                                    work_order_ref=entry.get("work_order_ref"),
                                    model_config_hash=window_config_hash,
                                )
                            except Exception as exc:  # noqa: BLE001 - the ledger is not a gate
                                verdict = {**verdict,
                                           "not_recorded": f"{type(exc).__name__}: {exc}"}
                            summary["isolated_windows"].append({**entry, **verdict})
                            if consecutive_certain_failures >= CONSECUTIVE_CERTAIN_FAILURE_LIMIT:
                                # Isolating one settled failure is safe;
                                # walking into an outage one window at a time
                                # is the same mistake with more steps.
                                stop_reason = "systemic_model_failure"
                                summary["blocked_window"] = {
                                    **entry, **verdict,
                                    "reason": (
                                        f"{consecutive_certain_failures} windows failed in a row "
                                        "with a settled cost; the provider is treated as down"
                                    ),
                                }
                                break
                            if context["next_offset"] is None:
                                break
                            offset = context["next_offset"]
                            continue
                        if result.get("status") != "succeeded":
                            # A terminal model failure is a statement about the
                            # execution, not about the source window.  Keep the
                            # review open so it cannot be mislabeled as a full
                            # successful read with no admissible statements.
                            complete = False
                        if isinstance(budget, dict) and budget.get("status") == "rejected":
                            stop_reason = "budget_rejected"
                            complete = False
                            break
                    elif view["status"] != "succeeded":
                        # Includes cached formal failures.  They are replayed
                        # on later runs without entering generate(), so this
                        # guard must live on the read path as well.
                        complete = False
                        if view["status"] == "failed":
                            failure = {
                                "review_id": review["review_id"],
                                "source_ref": review["source_ref"],
                                "document_ref": review["document_ref"],
                                "offset": offset,
                                "status": "failed",
                                "replayed": True,
                            }
                            for field in ("error_code", "work_order_ref"):
                                value = view.get(field)
                                if isinstance(value, str) and value:
                                    failure[field] = value
                            summary["qualitative_failures"].append(failure)
                            # C2-1: this window holds a terminal result, and
                            # the child only ever calls the model for a window
                            # that holds none.  So it can never succeed, its
                            # review can never complete, and every later run
                            # re-rendered the whole document to rediscover
                            # that -- 24% of live window reads.  Write it down
                            # once.
                            verdict = replayed_window_failure(
                                error_code=view.get("error_code"))
                            try:
                                windows.exclude(
                                    review_id=review["review_id"],
                                    source_review_hash=review_hash,
                                    offset=offset, next_offset=context["next_offset"],
                                    classification=verdict,
                                    document_ref=review["document_ref"],
                                    company_ref=review["company_ref"],
                                    work_order_ref=view.get("work_order_ref"),
                                    model_config_hash=window_config_hash,
                                )
                            except Exception as exc:  # noqa: BLE001 - the ledger is not a gate
                                verdict = {**verdict,
                                           "not_recorded": f"{type(exc).__name__}: {exc}"}
                            summary["isolated_windows"].append({**failure, **verdict})
                    if context["next_offset"] is None:
                        break
                    offset = context["next_offset"]
                if complete:
                    summary["reviews_complete"] += 1
                    complete_reviews.append((review, review_hash, actor, offsets))
        # P11s: the two secondary passes choose their own documents.
        #
        # They used to ride along on whatever the prose pass had just drafted,
        # which meant their allowances could not be spent at all: the prose
        # pass drains its 30 windows on the top-priority company's largest web
        # pages, so three consecutive live runs read 30 news windows and never
        # reached a single filing or transcript.  Both passes therefore sweep
        # the reviews they want, before admission closes any of them.
        secondary_failure = None
        if stop_reason != "systemic_model_failure":
            secondary_failure = _secondary_sweep(
                service, numeric_lanes, summary,
                limit=max_numeric_windows, entries="numeric",
                wanted=lambda review, spec: numeric_worthy(spec),
                call="generate_numeric",
                counts={"verified": "verified", "refused": "refused",
                        "recorded": "recorded"},
                total=("figures", "recorded"),
                spent_key="numeric_fresh",
                require_open=False,
                directed_reviews=numeric_directed,
                ledger=windows, model_config_hash=window_config_hash,
            )
        if stop_reason != "systemic_model_failure" and secondary_failure is None:
            secondary_failure = _secondary_sweep(
                service, held_lanes, summary,
                limit=max_discovery_windows, entries="discovery",
                wanted=lambda review, spec: discovery_worthy(spec),
                call="generate_metric_discovery",
                counts={"proposals": "proposals", "refused": "refused", "recorded": "recorded"},
                total=("metrics_observed", "recorded"),
                spent_key="discovery_fresh",
                require_open=False,
                ledger=windows, model_config_hash=window_config_hash,
            )
        # ADR-0005 / P9d-17b: every fully drafted review is staged and
        # policy-admitted, then closed.  Idempotent: a re-run reports
        # duplicates and writes nothing new.
        if candidate_staging is not None:
            _admit_complete_reviews(host, service, complete_reviews, summary)
        if stop_reason == "systemic_model_failure" or secondary_failure is not None:
            failed = (summary["qualitative_failures"][-1]
                      if stop_reason == "systemic_model_failure"
                      else secondary_failure)
            summary["blocked"] = {
                "code": "systemic_model_failure",
                "pass": "qualitative" if stop_reason == "systemic_model_failure" else failed["pass"],
                "error_code": failed.get("error_code"),
                "work_order_ref": failed.get("work_order_ref"),
            }
            summary["stop_reason"] = "systemic_model_failure"
            summary["failure_reason"] = (
                "a terminal model execution failed; remaining windows were not launched")
            summary["status"] = "failed"
            return summary
        if (summary["reviews_scanned"] > 0 and summary["reviews_complete"] == 0
                and drafted == 0 and len(summary["skipped"]) == summary["reviews_scanned"]):
            reasons: dict[str, int] = {}
            for skipped in summary["skipped"]:
                reason = str(skipped.get("reason") or "unknown view failure")
                reasons[reason] = reasons.get(reason, 0) + 1
            summary["blocked"] = {
                "code": "all_document_views_failed",
                "review_count": summary["reviews_scanned"],
                "reasons": [{"reason": reason, "count": count}
                            for reason, count in sorted(reasons.items())],
            }
            summary["stop_reason"] = "all_document_views_failed"
            summary["failure_reason"] = "all queued document views failed before drafting"
            summary["status"] = "failed"
            return summary
        if (summary["reviews_scanned"] > 0 and summary["reviews_complete"] == 0
                and summary["qualitative_failures"]
                and not any(item.get("status") == "succeeded"
                            for item in summary["drafted"])):
            reasons: dict[str, int] = {}
            for item in summary["qualitative_failures"]:
                reason = str(item.get("error_code") or item.get("status") or "unknown")
                reasons[reason] = reasons.get(reason, 0) + 1
            summary["blocked"] = {
                "code": "all_model_windows_unavailable",
                "review_count": summary["reviews_scanned"],
                "reasons": [{"reason": reason, "count": count}
                            for reason, count in sorted(reasons.items())],
            }
            summary["stop_reason"] = "model_execution_pending_or_failed"
            summary["failure_reason"] = (
                "all attempted document windows are pending or failed; no review was completed")
            summary["status"] = "failed"
            return summary
        summary["stop_reason"] = stop_reason or ("nothing_to_draft" if drafted == 0 else "drained")
        summary["status"] = "succeeded"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface it
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        # C2-1: whatever ended the run, say what it actually got done.  A
        # summary that reports only the window which stopped it reads as a
        # total loss even when twenty-eight windows were drafted -- which is
        # how a 4.9% run success rate hid a lane doing most of its work.
        succeeded = sum(1 for item in summary["drafted"]
                        if item.get("status") == "succeeded")
        summary["partial"] = {
            "windows_succeeded": succeeded,
            "windows_isolated": len(summary["isolated_windows"]),
            "windows_skipped_by_exclusion": summary["windows_skipped_by_exclusion"],
            "reviews_complete": summary["reviews_complete"],
            # True when the run produced real work despite ending on a stop
            # reason that used to mark the whole thing a failure.
            "productive": bool(succeeded or summary["reviews_complete"]
                               or summary["figures"] or summary["metrics_observed"]),
        }
        _write_owner_only(out / "summary.json", summary)
        host.close()


# The refusals that will still refuse tomorrow.  Every one of them is about
# the bytes themselves -- an encrypted PDF stays encrypted and a page that is
# not UTF-8 will not become UTF-8 -- so retrying is not patience, it is a loop.
# Ticket lookup failures are deliberately absent: a producer may publish a
# completed ticket later.  Anything else is assumed transient and keeps its
# retry.
_PERMANENT_UNREADABLE = (
    "is not valid UTF-8",
    "is encrypted and is not rendered",
    "requires a password and is not rendered",
    "gzip content is incomplete or invalid",
    # P13ap: the acquisition this review was opened on failed and left no
    # manifest, and no other launch of the same document completed either.
    # There is nothing on disk to read and no later run will find one, so the
    # review is parked with the reason instead of retried hourly forever.
    "and no completed acquisition of it remains",
)


def _daily_read_admit(
    connection: Any, mission: Mapping[str, Any], document_ref: str,
) -> bool:
    """Whether one more distinct document may start reading today (UTC).

    The owner's 2026-09-15 simplification keeps three stoppers -- the day's
    money, the AlphaEngine call count, and this document-per-day reading cap.
    A mission without ``max_daily_document_reads`` never stops here, and a
    document already started today (the run that hit the cap mid-document
    resumes it) never counts twice.
    """

    budget = mission.get("budget") or {}
    cap = budget.get("max_daily_document_reads")
    if cap is None:
        return True
    day = datetime.now(timezone.utc).date().isoformat()
    connection.execute(
        "CREATE TABLE IF NOT EXISTS document_extraction_daily_reads ("
        "day TEXT NOT NULL, document_ref TEXT NOT NULL, "
        "PRIMARY KEY (day, document_ref)) WITHOUT ROWID"
    )
    present = connection.execute(
        "SELECT 1 FROM document_extraction_daily_reads WHERE day=? AND document_ref=?",
        (day, document_ref),
    ).fetchone()
    if present is not None:
        return True  # already started today; a capped-out run resumes it
    counted = connection.execute(
        "SELECT COUNT(*) FROM document_extraction_daily_reads WHERE day=?", (day,),
    ).fetchone()[0]
    if counted >= int(cap):
        return False
    connection.execute(
        "INSERT INTO document_extraction_daily_reads (day, document_ref) VALUES (?, ?)",
        (day, document_ref),
    )
    return True


def _permanently_unreadable(reason: str, *, offset: int | None = None) -> bool:
    """Whether this refusal is about the bytes rather than about the moment."""

    if any(marker in reason for marker in _PERMANENT_UNREADABLE):
        return True
    # ``view`` rejects this one sentence for three different conditions.  At
    # the initial offset, zero is already an aligned non-negative integer, so
    # the only possible condition is ``offset >= len(rendered_text)``: the
    # exact rendering is empty.  At later offsets the same sentence may mean a
    # caller/window mismatch and is recoverable rather than evidence that the
    # original is unreadable.
    return (
        offset == 0
        and "source offset must be a valid bounded window" in reason
    )


def spec_reading_rank(spec_ref: Any) -> int:
    """Where this kind of document sits on the owner's reading ladder.

    The same ladder ``document_provenance`` publishes, resolved here so the
    database can sort by it: a 10-K or a press release first, then the
    earnings call, then the brokers who cover the name, then the industry and
    competitive work, and the news wire last.
    """

    from .document_provenance import evidence_value, tier_for_spec

    return evidence_value(tier_for_spec(spec_ref))


def is_high_priority_spec(spec_ref: Any) -> bool:
    """Whether this document belongs to the tiers a tick reserves windows for."""

    from .document_provenance import tier_for_spec

    return tier_for_spec(spec_ref) in HIGH_PRIORITY_TIERS


def _plan_directed_ranks(
    host: ExtractionHost, mission: Mapping[str, Any],
) -> dict[str, Any]:
    """What the mission's latest research plan asks this lane to read next.

    G1: ``research_planner.document_reading_priorities`` has had no consumer at
    all -- live on 2026-09-16 the newest plan's ten directives were five
    ``extract_figures``, three ``read`` and two ``stop``, and the extraction
    queue could not see any of them.  This is the join that was missing: the
    plan's (company, checklist item) becomes the (company, document kind) the
    queue already sorts by, so the planner's order can lead the queue's.

    Fail-open by construction.  No plan, an unreadable plan, a plan naming
    items nothing is held for -- each returns empty maps and the queue keeps
    exactly the order C2 gave it.  A plan states a priority; it is never a gate
    on reading, and a lane that stopped because it could not read one would be
    strictly worse than a lane that never looked.
    """

    from .mission_stage import INDUSTRY_BASE_ITEMS, SOURCE_BASE_ITEMS
    from .research_planner import document_reading_priorities

    empty: dict[str, Any] = {"ranks": {}, "numeric": {}, "plan": None}
    try:
        plan = host.coverage_mission.latest_research_plan(mission["id"])
    except Exception as exc:  # noqa: BLE001 - an unreadable plan is not a decision
        return {**empty, "plan": {"mission_version_ref": mission["id"],
                                  "status": "unavailable",
                                  "reason": f"{type(exc).__name__}: {exc}"}}
    if not plan:
        return {**empty, "plan": {"mission_version_ref": mission["id"],
                                  "status": "no_plan"}}
    try:
        priorities = document_reading_priorities(plan)
        item_specs = {item["item_ref"]: item["spec_refs"]
                      for item in (*SOURCE_BASE_ITEMS, *INDUSTRY_BASE_ITEMS)}
        resolved = plan_reading_ranks(
            priorities, item_specs=item_specs,
            industry_ref=mission.get("industry_ref"))
    except Exception as exc:  # noqa: BLE001 - nor is an unusable one
        return {**empty, "plan": {"mission_version_ref": mission["id"],
                                  "plan_ref": plan.get("plan_id"),
                                  "status": "unusable",
                                  "reason": f"{type(exc).__name__}: {exc}"}}
    return {
        **resolved,
        "plan": {
            "mission_version_ref": mission["id"],
            "plan_ref": plan.get("plan_id"),
            "state_hash": plan.get("state_hash"),
            "created_at": plan.get("created_at"),
            "status": "applied" if resolved["ranks"] else "named_nothing_held",
            "reading_directives": len(priorities),
            "directed_kinds": len(resolved["ranks"]),
            "figure_kinds": len(resolved["numeric"]),
        },
    }


def _awaiting_reviews_in_reading_order(
    host: ExtractionHost, mission: Mapping[str, Any], *, limit: int = 500,
    plan_ranks: Mapping[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    """The open extraction queue, plan-named first, then best evidence.

    Ordered inside SQL so ``LIMIT`` takes the head of the reading order rather
    than the head of the insertion order.  Falls back to the authority's own
    reader on any failure: an ordering is an optimisation, never a gate.

    G1: ``plan_ranks`` leads the key.  Everything the plan did not name keeps
    the C2 order underneath it -- evidence tier, then priority company, then
    age -- so an empty or unreadable plan changes nothing.
    """

    try:
        rank = {ref: index for index, ref in enumerate(company_priority_order(mission))}
    except Exception:  # noqa: BLE001 - ordering is not a gate
        rank = {}
    default_company_rank = len(rank)
    try:
        rows = host.store.connection.execute(
            "SELECT r.*, s.spec_ref AS spec_ref FROM coverage_mission_document_reviews r "
            "LEFT JOIN coverage_mission_discovered_documents d "
            "ON d.record_id=r.discovered_document_ref "
            "LEFT JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE r.mission_version_ref=? AND r.state='awaiting_human_extraction'",
            (mission["id"],),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return host.coverage_mission.document_reviews(
            mission["id"], state="awaiting_human_extraction", limit=limit)
    reviews = []
    for row in rows:
        record = host.coverage_mission._review_row(row)
        record["_spec_ref"] = row["spec_ref"]
        reviews.append(record)
    reviews.sort(key=lambda review: (
        plan_rank_for(plan_ranks, company_ref=review.get("company_ref"),
                      spec_ref=review.get("_spec_ref")),
        spec_reading_rank(review.get("_spec_ref")),
        rank.get(review.get("company_ref") or "", default_company_rank),
        str(review.get("created_at") or ""),
        str(review.get("review_id") or ""),
    ))
    return [{k: v for k, v in review.items() if k != "_spec_ref"}
            for review in reviews[:limit]]


def _record_provenance(host: ExtractionHost) -> dict[str, Any]:
    """Keep what the searches said about the documents they returned.

    P10x: publisher and named companies were on the wire and were dropped, so a
    broker note naming four covered vendors could only ever be one company's
    and two notes from one house looked like two independent sources.  Best
    effort: never a reason to stop reading.
    """

    from .extraction_backlog import backfill_provenance

    try:
        return backfill_provenance(host.store.connection, host._transcript_spool)
    except Exception as exc:  # noqa: BLE001 - provenance is not a gate
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}


def _thinness_ranks(host: ExtractionHost, mission: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    """Which company holds fewest Claims of each provenance tier, thinnest first.

    Counted from the documents each company's Claims were read out of, which is
    the same join the backlog reader uses, so "thin in sell-side" means the
    same thing in the queue as it does in the projection.
    """

    from .document_provenance import tier_for_spec
    from .extraction_priority import thinness_ranks

    companies = [member["company_ref"] for member in mission.get("universe") or ()]
    counts: dict[str, dict[str, int]] = {company: {} for company in companies}
    rows = host.store.connection.execute(
        "SELECT r.company_ref AS company_ref, s.spec_ref AS spec_ref, COUNT(*) AS n "
        "FROM coverage_mission_document_reviews r "
        "JOIN coverage_mission_discovered_documents d ON d.record_id=r.discovered_document_ref "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "WHERE r.state='extraction_staged' GROUP BY 1,2"
    ).fetchall()
    for row in rows:
        tier = tier_for_spec(row["spec_ref"])
        bucket = counts.setdefault(row["company_ref"], {})
        bucket[tier] = bucket.get(tier, 0) + int(row["n"])
    return thinness_ranks(counts, companies=companies or sorted(counts))


def _secondary_sweep(
    service: DocumentExtractionService,
    lanes: list[tuple[str, list[dict[str, Any]], dict[str, str]]],
    summary: dict[str, Any],
    *,
    limit: int,
    entries: str,
    wanted: Any,
    call: str,
    counts: Mapping[str, str],
    total: tuple[str, str],
    spent_key: str,
    require_open: bool = True,
    directed_reviews: Any = None,
    ledger: Any = None,
    model_config_hash: str | None = None,
) -> dict[str, Any] | None:
    """Spend one secondary allowance on the documents that pass its own gate.

    Reads windows in the mission's order, skipping the reviews this pass has no
    use for.  A window whose answer already exists is recovered rather than
    paid for, and does not consume the allowance: replay is free, so an
    allowance spent on it would be an allowance not spent on a window nobody
    has read yet.

    C2-3: free is not the same as finished.  Live on 2026-09-18 the figures
    pass walked 107 windows over the same four reviews every five minutes for
    two hours -- 106 of them replayed, nothing recorded, nothing verified --
    while 114 documents nobody had read waited behind them.  Replay costs no
    model call, but every one of those windows still re-derived its context,
    which means re-fetching, re-rendering and re-hashing the whole document.
    So a review whose every window replayed and recorded nothing is terminal
    for this pass (``windows_exhausted``), as is one whose document names no
    company this lane covers (``not_attributed``), and ``ledger`` remembers
    that across ticks so the queue moves on.  The de-duplication itself is
    untouched: it is what stops the lane paying twice.
    """

    if limit <= 0:
        return None
    spent = 0
    pass_ref = str(entries)
    known: dict[tuple[str, str], dict[str, Any]] = {}
    if ledger is not None:
        try:
            known = ledger.exhausted_reviews(
                pass_ref, model_config_hash=model_config_hash)
        except Exception:  # noqa: BLE001 - the ledger is not a gate
            known = {}
    skipped_exhausted = 0
    advanced: dict[str, Any] | None = None

    def mark_exhausted(review: Mapping[str, Any], review_hash: str, *,
                       reason: str, detail: str, windows: int) -> None:
        """Write the verdict down once, and report it in this tick's summary."""

        record = {
            "review_id": review["review_id"], "pass": pass_ref, "reason": reason,
            "detail": detail, "windows": windows,
            "document_ref": review.get("document_ref"),
            "source_ref": review.get("source_ref"),
        }
        if ledger is not None:
            try:
                ledger.exhaust(
                    review_id=review["review_id"], source_review_hash=review_hash,
                    pass_ref=pass_ref, reason=reason, detail=detail, windows=windows,
                    document_ref=review.get("document_ref"),
                    company_ref=review.get("company_ref"),
                    model_config_hash=model_config_hash,
                )
            except Exception as exc:  # noqa: BLE001 - the ledger is not a gate
                record["not_recorded"] = f"{type(exc).__name__}: {exc}"
        known[(review["review_id"], review_hash)] = record
        summary.setdefault("exhausted_reviews", []).append(record)

    def done() -> None:
        # What this pass actually paid for, so the coordinator can tell a lane
        # with nothing left to read from one whose prose queue merely drained.
        summary[spent_key] = summary.get(spent_key, 0) + spent
        # C2-3: and where the queue got to, so "the lane is stuck on the same
        # four documents" is a thing the owner can read rather than infer.
        summary.setdefault("advanced_to", {})[pass_ref] = {
            **(advanced or {"review_id": None, "document_ref": None, "offset": None}),
            "fresh_windows": spent,
            "reviews_skipped_exhausted": skipped_exhausted,
        }

    method = getattr(service, call)
    for actor, reviews, specs in lanes:
        for review in reviews:
            if spent >= limit:
                done()
                return None
            if not wanted(review, specs.get(review["document_ref"])):
                continue
            review_hash = content_hash(review)
            if (review["review_id"], review_hash) in known:
                # Terminal for this pass under these bytes and this model.
                skipped_exhausted += 1
                continue
            offset = 0
            # C2-3: what this review turned out to be worth, this walk.
            windows_walked = 0
            windows_replayed = 0
            produced = 0
            walked_to_end = False
            exhausted_here: tuple[str, str] | None = None
            while spent < limit:
                try:
                    context = service.view(
                        review_id=review["review_id"], expected_review_hash=review_hash,
                        offset=offset, actor_ref=actor, require_open=require_open,
                    )["context"]
                    result = method(
                        review_id=review["review_id"], expected_review_hash=review_hash,
                        offset=offset, expected_context_hash=context["content_hash"],
                        actor_ref=actor,
                    )
                except Exception as exc:  # noqa: BLE001 - one window, not the run
                    summary[entries].append({
                        "review_id": review["review_id"], "offset": offset,
                        "status": "failed", "reason": f"{type(exc).__name__}: {exc}",
                    })
                    break
                status = result.get("status")
                if status == "gated":
                    # No model is configured; every other window is gated too.
                    done()
                    return None
                # A window that was never asked anything costs no allowance:
                # nothing was owed, or its document kind is not one a figure
                # may be taken from at all.
                if not result.get("replayed") and status not in FREE_STATUSES:
                    spent += 1
                    if advanced is None:
                        advanced = {"review_id": review["review_id"],
                                    "document_ref": review.get("document_ref"),
                                    "offset": offset}
                    if directed_reviews and review["review_id"] in directed_reviews:
                        # G1: a figures window the plan asked for, paid for.
                        summary["plan_directed_windows"] = (
                            summary.get("plan_directed_windows", 0) + 1)
                entry = {"review_id": review["review_id"], "source_ref": review["source_ref"],
                         "offset": offset, "status": status,
                         "replayed": bool(result.get("replayed"))}
                entry.update({name: len(result.get(key, [])) for name, key in counts.items()})
                # P13y: a pass that lost its learned requirements still reads,
                # on the universal floor alone, and used to look identical to a
                # company nobody had read yet. The summary is where the evidence
                # of the last silent failure was eventually found; put it there.
                if result.get("requirements_error"):
                    entry["requirements_error"] = result["requirements_error"]
                # C2-3: a window that verified nothing but refused something is
                # not an empty document, it is an answer the contract threw
                # away.  Live, every one of the twenty figures three 10-Ks
                # produced was refused for the same reason, and the summary
                # said only ``recorded: 0``.
                reasons = _refusal_reasons(result)
                if reasons and not result.get(total[1]):
                    entry["refusal_reasons"] = reasons
                summary[entries].append(entry)
                summary[total[0]] += len(result.get(total[1], []))
                windows_walked += 1
                if result.get("replayed"):
                    windows_replayed += 1
                produced += len(result.get(total[1], []) or ())
                if status in {"failed", "no_result"} and not result.get("replayed"):
                    done()
                    return {
                        **entry, "pass": entries,
                        "error_code": result.get("error_code"),
                        "work_order_ref": result.get("work_order_ref"),
                    }
                if status == "not_attributed":
                    # Attribution is decided over the whole document, not the
                    # window, so every later window would answer identically.
                    # Pay for that finding once rather than every five minutes.
                    exhausted_here = ("not_attributed", str(
                        "the document names no company this lane covers; "
                        "no window of it can be attributed"))
                    break
                if context["next_offset"] is None:
                    walked_to_end = True
                    break
                offset = context["next_offset"]
            if exhausted_here is not None:
                mark_exhausted(review, review_hash, reason=exhausted_here[0],
                               detail=exhausted_here[1], windows=windows_walked)
            elif (walked_to_end and windows_walked > 0
                  and windows_replayed == windows_walked and produced == 0):
                mark_exhausted(
                    review, review_hash, reason="windows_exhausted",
                    detail=(
                        f"all {windows_walked} windows replayed an answer already "
                        "stored and recorded nothing; re-reading can only re-derive "
                        "the same document for the same nothing"
                    ),
                    windows=windows_walked,
                )
    done()
    return None


def _refusal_reasons(result: Mapping[str, Any]) -> list[str]:
    """The distinct reasons a window's candidates were thrown away, if any."""

    refused = result.get("refused")
    if not isinstance(refused, list):
        return []
    reasons = {str(item["reason"]) for item in refused
               if isinstance(item, Mapping) and item.get("reason")}
    return sorted(reasons)[:3]


def _admit_complete_reviews(host: ExtractionHost, service: DocumentExtractionService,
                            reviews: list[tuple[dict[str, Any], str, str, list[int]]],
                            summary: dict[str, Any]) -> None:
    for review, review_hash, actor, offsets in reviews:
        if not actor.startswith("automation:"):
            continue  # a human-requested run drafts only; admission is the mission's
        outcomes: list[dict[str, Any]] = []
        gated: str | None = None
        unattributed: str | None = None
        # P13ap: a source that can be read but not staged.  Admission is
        # skipped rather than attempted -- there is no citation authority to
        # attempt it through -- but the read itself still completes and the
        # review still resolves.  Holding it open instead would mean paying to
        # draft the same note again every five minutes forever, and the
        # cockpit's read count would go on saying nobody had read it.
        unstageable = (
            None if review["source_ref"] in STAGEABLE_SOURCE_REFS else
            f"{SOURCE_STAGING_GATE_REASON}: {review['source_ref']} documents are read "
            "and drafted from, but have no candidate citation authority to be staged "
            "through, so no Claim was published from this read"
        )
        for offset in [] if unstageable else offsets:
            try:
                view = service.view(
                    review_id=review["review_id"], expected_review_hash=review_hash,
                    offset=offset, actor_ref=actor,
                )
                if view.get("status") != "succeeded":
                    gated = (
                        "qualitative window is not successfully readable: "
                        f"{view.get('status') or 'unknown'}"
                    )
                    break
                result = service.admit_suggestions(
                    review_id=review["review_id"], expected_review_hash=review_hash, offset=offset, actor_ref=actor,
                )
            except Exception as exc:
                gated = f"{type(exc).__name__}: {exc}"
                break
            if result["status"] == "not_attributed":
                # Not a hold: this document will never be about this company,
                # so leaving the review open would retry it forever.
                unattributed = str(result.get("reason"))
                break
            if result["status"] == "gated":
                gated = str(result.get("reason"))
                break
            for item in result.get("admitted", []):
                outcomes.append({"review_id": review["review_id"], "offset": offset, **item})
        summary["admitted"].extend(outcomes)
        if unattributed is not None:
            try:
                host.coverage_mission.resolve_document_review(
                    review["review_id"], resolution="dismissed", actor_ref=actor,
                    rationale=f"P13i: {unattributed}", expected_review_hash=review_hash,
                )
                status = "dismissed"
            except Exception as exc:  # noqa: BLE001
                status = f"unresolved: {type(exc).__name__}"
            summary["resolved_reviews"].append({
                "review_id": review["review_id"], "status": status,
                "reason": unattributed,
            })
            continue
        if gated is not None:
            summary["resolved_reviews"].append({"review_id": review["review_id"], "status": "held", "reason": gated})
            continue
        try:
            receipts = [service.read_completion_receipt(
                review_id=review["review_id"], source_review_hash=review_hash,
                offset=offset, actor_ref=actor) for offset in offsets]
            DocumentReadCompletionAuthority(host.store.connection).record(
                review_id=review["review_id"], source_review_hash=review_hash,
                actor_ref=actor, windows=receipts, receipt_reader=service)
        except Exception as exc:  # a proof failure must leave the review open
            summary["resolved_reviews"].append({
                "review_id": review["review_id"], "status": "held",
                "reason": f"completion proof failed: {type(exc).__name__}: {exc}",
            })
            continue
        fresh = [o for o in outcomes if o["status"] == "admitted"]
        carried = [o for o in outcomes if o["status"] in ("admitted", "duplicate")]
        summary["formal_authority_writes"] += 2 * len(fresh)  # one Evidence and one Claim version each
        rejected = [o for o in outcomes if o["status"] == "rejected"]
        # A refusal is a judgment only when the policy or a validator said no
        # to the suggestion itself.  A conflict or an unexpected error is the
        # system's problem: hold the review open rather than dismiss it.
        judged = ("ResearchAutoCommitRejected", "VerificationRejected", "TranscriptCorrectionValidationError",
                  "ResearchVerificationError")
        conflicts = [o for o in rejected if not str(o.get("reason", "")).startswith(judged)]
        if conflicts and not carried:
            summary["resolved_reviews"].append({"review_id": review["review_id"], "status": "held",
                                                "reason": conflicts[0]["reason"]})
            continue
        try:
            if carried:
                resolution = host.coverage_mission.resolve_document_review(
                    review["review_id"], resolution="extraction_staged", actor_ref=actor,
                    candidate_claim_version_ref=carried[0]["candidate_claim_ref"],
                    rationale=(f"ADR-0005 policy admission: {len(carried)} qualitative claim(s) admitted, "
                               f"{len(rejected)} suggestion(s) refused"),
                    expected_review_hash=review_hash,
                )
            elif unstageable is not None:
                resolution = host.coverage_mission.resolve_document_review(
                    review["review_id"], resolution="dismissed", actor_ref=actor,
                    rationale=(f"P13ap: {len(offsets)} window(s) were read and their "
                               f"suggestions kept on the review. {unstageable}"),
                    expected_review_hash=review_hash,
                )
            else:
                resolution = host.coverage_mission.resolve_document_review(
                    review["review_id"], resolution="dismissed", actor_ref=actor,
                    rationale=(f"ADR-0005: mission automation found no admissible qualitative statement in "
                               f"{len(offsets)} window(s); {len(rejected)} suggestion(s) refused by policy"),
                    expected_review_hash=review_hash,
                )
            entry = {"review_id": review["review_id"], "status": resolution["state"],
                     "admitted": len(carried), "rejected": len(rejected)}
            if unstageable is not None:
                entry["reason"] = unstageable
            summary["resolved_reviews"].append(entry)
        except Exception as exc:
            summary["resolved_reviews"].append({"review_id": review["review_id"], "status": "unresolved",
                                                "reason": f"{type(exc).__name__}: {exc}"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--spool-dir", type=Path)
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--connector-governance", type=Path)
    parser.add_argument("--web-fetch-governance", type=Path)
    parser.add_argument("--max-windows", type=int, default=DEFAULT_MAX_WINDOWS)
    # P11m: off unless asked for. The figures pass is a second model call per
    # window, so switching it on is a decision about spend, not a default.
    parser.add_argument(
        "--max-numeric-windows", type=int, default=0,
        help="windows per run that may also be read for the figures a company "
             "owes (0 disables the figures pass)",
    )
    # P11r: also off unless asked for, and for the same reason.
    parser.add_argument(
        "--max-discovery-windows", type=int, default=0,
        help="windows per run that may also be read for the measures the "
             "market judges a company on (0 disables the discovery pass)",
    )
    parser.add_argument("--requested-by", help="human: actor; default is each mission's automation principal")
    parser.add_argument("--hermetic-fixture-file", type=Path, help="test-only fixture model output")
    parser.add_argument("--candidate-staging", type=Path, help="shared CandidateStaging database; enables admission")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_numeric_windows < 0 or args.max_numeric_windows > 50:
        raise SystemExit("--max-numeric-windows must be 0..50")
    if args.max_discovery_windows < 0 or args.max_discovery_windows > 50:
        raise SystemExit("--max-discovery-windows must be 0..50")
    if args.max_windows < 1 or args.max_windows > 50:
        raise SystemExit("--max-windows must be 1..50")
    summary = run_extraction(
        state_dir=args.state_dir, model_config_path=args.model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        spool_dir=args.spool_dir, scheduler_db=args.scheduler_db,
        connector_governance=args.connector_governance, web_fetch_governance=args.web_fetch_governance,
        max_windows=args.max_windows,
        max_numeric_windows=args.max_numeric_windows,
        max_discovery_windows=args.max_discovery_windows,
        requested_by=args.requested_by,
        hermetic_fixture=args.hermetic_fixture_file, candidate_staging=args.candidate_staging,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
