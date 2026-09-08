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
from .mission_stage import company_priority_order, review_sort_key
from .document_extraction import (
    DocumentExtractionModelWorker,
    DocumentExtractionService,
    HermeticExtractionAdapter,
    validate_model_config,
)
from .observability import ObservabilityStore
from .public_web_fetch_launcher import PublicWebFetchLauncher
from .raw_spool import RawSpool
from .scheduler import Scheduler
from .store import DaltonStore, canonical_json, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
DEFAULT_MAX_WINDOWS = 2


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
        self._scheduler = Scheduler(str(scheduler_db))
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

    def close(self) -> None:
        for handle in reversed(self._keepalive):
            try:
                handle.close()
            except Exception:
                pass
        self._scheduler.close()
        self.store.close()


# P11o: the sources that state reported figures. A filing and a call
# transcript say "revenue was X"; a news article about the industry almost never
# does, and spending the figures allowance on one costs a paid call to be told
# nothing. This is a spending rule, not a claim that news is worthless -- the
# prose pass still reads all of it.
NUMERIC_SOURCE_REFS: frozenset[str] = frozenset({
    "source:sec-edgar", "source:alphaengine",
})


def numeric_worthy(review: Mapping[str, Any]) -> bool:
    """Whether a window from this source is worth a figures call."""

    return review.get("source_ref") in NUMERIC_SOURCE_REFS


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
        # P11m: the figures pass, counted separately from the prose pass. A
        # window that owes nothing appears here as nothing_owed and costs no
        # model call, so the two counts are not interchangeable.
        "numeric": [],
        "figures": 0,
        "max_numeric_windows": max_numeric_windows,
        # P11r: the pass that learns what to ask for. ``metrics_observed``
        # counts journal entries, not requirements: a requirement needs two
        # distinct documents and is derived when it is read.
        "discovery": [],
        "metrics_observed": 0,
        "max_discovery_windows": max_discovery_windows,
        "skipped": [],
        "admitted": [],
        "resolved_reviews": [],
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
        drafted = 0
        lanes: list[tuple[str, list[dict[str, Any]], dict[str, str]]] = []
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
            reviews = host.coverage_mission.document_reviews(mission["id"], state="awaiting_human_extraction", limit=500)
            # P10a: read in the mission's own order — the P0 company before the
            # P2 one, and management's own words before someone else's summary
            # of them.  Age only breaks ties.
            specs: dict[str, str] = {}
            try:
                rank = {ref: index for index, ref in enumerate(company_priority_order(mission))}
                specs = host.coverage_mission.document_spec_refs(mission["id"])
                reviews = sorted(reviews, key=lambda review: review_sort_key(
                    review, company_rank=rank, spec_by_document=specs))
            except Exception:  # noqa: BLE001 - ordering is not a gate
                pass
            lanes.append((actor, reviews, specs))
            for review in reviews:
                if stop_reason is not None:
                    break
                summary["reviews_scanned"] += 1
                review_hash = content_hash(review)
                offset = 0
                complete = True
                offsets: list[int] = []
                while True:
                    try:
                        view = service.view(review_id=review["review_id"], expected_review_hash=review_hash,
                                            offset=offset, actor_ref=actor)
                    except Exception as exc:
                        # One review that cannot be bound (stale grant, missing
                        # manifest, unreadable original) must not end the run
                        # for the rest; it is reported with its reason.  Live,
                        # one such review aborted a whole run.
                        summary["skipped"].append({"review_id": review["review_id"], "offset": offset,
                                                   "reason": f"{type(exc).__name__}: {exc}"})
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
                        result = service.generate(
                            review_id=review["review_id"], expected_review_hash=review_hash, offset=offset,
                            expected_context_hash=context["content_hash"], actor_ref=actor,
                        )
                        drafted += 1
                        entry = {"review_id": review["review_id"], "source_ref": review["source_ref"],
                                 "document_ref": review["document_ref"], "offset": offset,
                                 "status": result.get("status"),
                                 "suggestions": len(result.get("suggestions", []))}
                        budget = result.get("model_budget") or {}
                        if isinstance(budget, dict) and budget.get("status"):
                            entry["budget"] = budget["status"]
                        summary["drafted"].append(entry)
                        if result.get("status") == "gated":
                            stop_reason = f"gated:{result.get('reason')}"
                            complete = False
                            break
                        if isinstance(budget, dict) and budget.get("status") == "rejected":
                            stop_reason = "budget_rejected"
                            complete = False
                            break
                    elif view["status"] == "pending":
                        complete = False
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
        _secondary_sweep(
            service, lanes, summary,
            limit=max_numeric_windows, entries="numeric",
            wanted=lambda review, spec: numeric_worthy(review),
            call="generate_numeric",
            counts={"verified": "verified", "refused": "refused"},
            total=("figures", "verified"),
        )
        _secondary_sweep(
            service, lanes, summary,
            limit=max_discovery_windows, entries="discovery",
            wanted=lambda review, spec: discovery_worthy(spec),
            call="generate_metric_discovery",
            counts={"proposals": "proposals", "refused": "refused", "recorded": "recorded"},
            total=("metrics_observed", "recorded"),
        )
        # ADR-0005 / P9d-17b: every fully drafted review is staged and
        # policy-admitted, then closed.  Idempotent: a re-run reports
        # duplicates and writes nothing new.
        if candidate_staging is not None:
            _admit_complete_reviews(host, service, complete_reviews, summary)
        summary["stop_reason"] = stop_reason or ("nothing_to_draft" if drafted == 0 else "drained")
        summary["status"] = "succeeded"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface it
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(out / "summary.json", summary)
        host.close()


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
) -> None:
    """Spend one secondary allowance on the documents that pass its own gate.

    Reads windows in the mission's order, skipping the reviews this pass has no
    use for.  A window whose answer already exists is recovered rather than
    paid for, and does not consume the allowance: replay is free, so an
    allowance spent on it would be an allowance not spent on a window nobody
    has read yet.
    """

    if limit <= 0:
        return
    spent = 0
    method = getattr(service, call)
    for actor, reviews, specs in lanes:
        for review in reviews:
            if spent >= limit:
                return
            if not wanted(review, specs.get(review["document_ref"])):
                continue
            review_hash = content_hash(review)
            offset = 0
            while spent < limit:
                try:
                    context = service.view(
                        review_id=review["review_id"], expected_review_hash=review_hash,
                        offset=offset, actor_ref=actor,
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
                    return  # no model is configured; every other window is gated too
                if not result.get("replayed") and status != "nothing_owed":
                    spent += 1
                entry = {"review_id": review["review_id"], "source_ref": review["source_ref"],
                         "offset": offset, "status": status,
                         "replayed": bool(result.get("replayed"))}
                entry.update({name: len(result.get(key, [])) for name, key in counts.items()})
                summary[entries].append(entry)
                summary[total[0]] += len(result.get(total[1], []))
                if context["next_offset"] is None:
                    break
                offset = context["next_offset"]


def _admit_complete_reviews(host: ExtractionHost, service: DocumentExtractionService,
                            reviews: list[tuple[dict[str, Any], str, str, list[int]]],
                            summary: dict[str, Any]) -> None:
    for review, review_hash, actor, offsets in reviews:
        if not actor.startswith("automation:"):
            continue  # a human-requested run drafts only; admission is the mission's
        outcomes: list[dict[str, Any]] = []
        gated: str | None = None
        for offset in offsets:
            try:
                result = service.admit_suggestions(
                    review_id=review["review_id"], expected_review_hash=review_hash, offset=offset, actor_ref=actor,
                )
            except Exception as exc:
                gated = f"{type(exc).__name__}: {exc}"
                break
            if result["status"] == "gated":
                gated = str(result.get("reason"))
                break
            for item in result.get("admitted", []):
                outcomes.append({"review_id": review["review_id"], "offset": offset, **item})
        summary["admitted"].extend(outcomes)
        if gated is not None:
            summary["resolved_reviews"].append({"review_id": review["review_id"], "status": "held", "reason": gated})
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
            else:
                resolution = host.coverage_mission.resolve_document_review(
                    review["review_id"], resolution="dismissed", actor_ref=actor,
                    rationale=(f"ADR-0005: mission automation found no admissible qualitative statement in "
                               f"{len(offsets)} window(s); {len(rejected)} suggestion(s) refused by policy"),
                    expected_review_hash=review_hash,
                )
            summary["resolved_reviews"].append({"review_id": review["review_id"], "status": resolution["state"],
                                                "admitted": len(carried), "rejected": len(rejected)})
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
