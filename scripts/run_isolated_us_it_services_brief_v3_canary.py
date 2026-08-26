#!/usr/bin/env python3
"""Isolated US IT Services brief v3 canary.

brief v3 = the v2 peer evidence pack (ACN, CTSH, EPAM, IBM; SEC exhibits) plus
one formal *qualitative* ACN transcript claim produced through the real
ADR-0003 B path inside one caller-owned Core:

    fake-handle Core-hosted AlphaEngine acquisition (no network)
    -> TranscriptCorrectionSetVersion + eligible TranscriptClaimCitationBinding
    -> stage_transcript_qualitative_candidate (CandidateStaging)
    -> HumanReviewAuthority.decide(accept) -> DaltonStore.commit_reviewed_candidate
    -> formal EvidenceVersion 0.2 + ClaimVersion 0.2 (claim_kind=qualitative)
    -> Driver Pack v4 (+ semantic aspect) -> Evidence Pack v3 -> four overlays
    -> industry_brief_snapshot -> render_industry_brief_markdown (replayed)

No network, no paid model, no live Core, no ThesisVersion.  The transcript
text is supplied by the caller (``--document-file``); the committed fixtures
never contain the AlphaEngine document itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dalton_core.alphaengine_acquisition_cli import FakeDocumentHandle, run_acquisition  # noqa: E402
from dalton_core.alphaengine_core_acquisition import (  # noqa: E402
    StaticConnectorGovernance,
    build_governance_record,
)
from dalton_core.coverage_admission import CoverageAdmissionAuthority  # noqa: E402
from dalton_core.industry_research import IndustryResearchAuthority  # noqa: E402
from dalton_core.model_input import ModelInputLedger  # noqa: E402
from dalton_core.raw_spool import RawSpool  # noqa: E402
from dalton_core.research_review import HumanReviewAuthority  # noqa: E402
from dalton_core.research_verification import CandidateStagingStore  # noqa: E402
from dalton_core.store import DaltonStore  # noqa: E402
from dalton_core.transcript_candidate_staging import (  # noqa: E402
    stage_transcript_qualitative_candidate,
)
from dalton_core.transcript_correction import TranscriptCorrectionAuthority  # noqa: E402

import run_isolated_us_it_services_evidence_canary as base  # noqa: E402


DEFAULT_BRIEF_MANIFEST = ROOT / "deploy/coverage/us-it-services-industry-evidence-v3.json"
GOVERNANCE_APPROVED_BY = "human:isolated-canary"


def _asr_flags(document: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate the manifest's ASR word-boundary flags that exist in this document."""

    found: list[tuple[int, int, str]] = []
    for text in spec["asr_flag_texts"]:
        start = document.find(text)
        if start < 0:
            continue
        end = start + len(text)
        if any(not (end <= s or start >= e) for s, e, _ in found):
            continue
        found.append((start, end, text))
    found.sort()
    if not found:
        raise ValueError("none of the manifest ASR flag texts exist in the document")
    return [{
        "source_start": start,
        "source_end": end,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "correction_kind": "terminology",
        "disposition": "unresolved",
        "replacement_text": None,
        "rationale": spec["asr_flag_rationale"],
        "evidence_bindings": [],
    } for start, end, text in found]


def _citation_span(document: str, spec: dict[str, Any]) -> tuple[int, int]:
    start = document.find(spec["citation"]["start_text"])
    if start < 0:
        raise ValueError("citation start text is not in the document")
    end_marker = spec["citation"]["end_text"]
    end = document.find(end_marker, start)
    if end < 0:
        raise ValueError("citation end text is not in the document after the start")
    return start, end + len(end_marker)


def promote_transcript_claim(
    core: DaltonStore, spool: RawSpool, manifest: dict[str, Any], document: str,
    spec: dict[str, Any], staging_path: Path,
) -> dict[str, Any]:
    """ADR-0003 B path on one Core: correction set -> citation -> stage -> accept -> commit."""

    corrections = TranscriptCorrectionAuthority(
        core, spool=spool,
        manifest_resolver=lambda ref: manifest if ref == manifest["id"] else None,
        evidence_resolver=lambda _ref: None,
    )
    correction_set = corrections.publish(
        spec["correction_set_ref"],
        source_manifest_ref=manifest["id"],
        source_manifest_hash=manifest["content_hash"],
        source_content_hash=hashlib.sha256(document.encode("utf-8")).hexdigest(),
        review_scope=spec["review_scope"],
        corrections=_asr_flags(document, spec),
        actor_ref=spec["reviewer_ref"],
    )
    start, end = _citation_span(document, spec)
    citation = corrections.bind_claim_citation(
        correction_set["id"], correction_set["content_hash"],
        source_start=start, source_end=end,
    )
    if not citation["claim_eligible"]:
        raise RuntimeError("transcript citation is not claim eligible")

    staging = CandidateStagingStore(staging_path)
    try:
        staged = stage_transcript_qualitative_candidate(
            core, staging,
            correction_set_ref=correction_set["id"],
            citation_ref=citation["id"],
            subject_ref=spec["subject_ref"],
            metric_or_aspect=spec["metric_or_aspect"],
            period=spec["period"],
            basis=spec["basis"],
            normalized_statement=spec["normalized_statement"],
            actor_ref=spec["stager_actor_ref"],
            idempotency_key=spec["stage_idempotency_key"],
            artifact_reader=lambda artifact: spool.read_object(artifact["artifact_content_hash"]),
        )
    finally:
        staging.close()
    if staged["write_status"] != "fresh" or staged["source_verification"]["verdict"] != "pass":
        raise RuntimeError("transcript candidate did not stage fresh with a passing verifier")
    candidate = staged["claim"]

    review = HumanReviewAuthority(staging_path)
    try:
        decision_ref = review.decide(
            candidate_claim_ref=candidate["id"],
            candidate_claim_hash=candidate["content_hash"],
            verdict="accept",
            reviewed_semantics={
                key: candidate[key] for key in (
                    "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement",
                )
            },
            rationale=spec["review_rationale"],
            findings=list(spec["review_findings"]),
            reviewer_ref=spec["reviewer_ref"],
            source_event_ref=spec["source_event_ref"],
            idempotency_key=spec["review_idempotency_key"],
            created_at=candidate["created_at"],
        )["decision_ref"]
        pending = review.pending_commits()
        if len(pending) != 1:
            raise RuntimeError("expected exactly one pending reviewed commit")
        result = core.commit_reviewed_candidate(
            **pending[0], idempotency_key=spec["commit_idempotency_key"],
        )
        review.record_commit_result(
            decision_ref, created_at=candidate["created_at"], ledger_result=result,
        )
        status = review.candidate_status(candidate["candidate_claim_ref"])
    finally:
        review.close()
    if result["status"] != "fresh" or status["review_state"] != "committed":
        raise RuntimeError("reviewed transcript candidate did not commit fresh")

    claim_row = core.connection.execute(
        "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?",
        (result["claim_version_ref"],),
    ).fetchone()
    relation_row = core.connection.execute(
        "SELECT content_hash FROM evidence_relations WHERE relation_id=?",
        (result["relation_ref"],),
    ).fetchone()
    claim_wire = json.loads(claim_row["claim_json"])
    if claim_wire["claim_kind"] != "qualitative" or claim_wire["value"] is not None:
        raise RuntimeError("formal transcript claim is not qualitative")
    return {
        "correction_set": correction_set,
        "citation": citation,
        "candidate_claim": candidate,
        "decision_ref": decision_ref,
        "ledger_result": result,
        "claim": {"claim_version_id": result["claim_version_ref"], "content_hash": claim_row["content_hash"]},
        "relation": {"relation_id": result["relation_ref"], "content_hash": relation_row["content_hash"]},
        "claim_wire": claim_wire,
    }


def run(
    document_path: Path, *, page_chars: int, base_path: Path, manifest_path: Path,
    peer_manifest_path: Path, brief_manifest_path: Path, include_report_body: bool = False,
) -> dict[str, Any]:
    base_manifest = base._load(base_path)
    manifest = base._load(manifest_path)
    peer_manifest = base._load(peer_manifest_path)
    brief_manifest = base._load(brief_manifest_path)
    if any(
        item.get("schema_version") != "0.1"
        for item in (manifest, peer_manifest, brief_manifest)
    ):
        raise ValueError("unsupported evidence canary manifest")
    if brief_manifest["base_peer_evidence_pack_version_ref"] != peer_manifest["evidence_pack"]["version_id"]:
        raise ValueError("brief manifest is bound to another peer evidence pack")
    spec = brief_manifest["transcript_candidate"]
    document = document_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    expected = spec.get("expected_content_sha256")
    if expected is not None and expected != digest:
        raise ValueError("document digest does not match the manifest expectation")

    with tempfile.TemporaryDirectory() as temp:
        state = Path(temp) / "state"
        governance = StaticConnectorGovernance(
            build_governance_record(approved_by=GOVERNANCE_APPROVED_BY, status="approved")
        )
        summary, source_manifest = run_acquisition(
            document_ref=spec["document_ref"],
            state_dir=state,
            governance=governance,
            handle=FakeDocumentHandle(document, page_chars),
            transport="fake",
            summary_dir=state / "acquisition",
            expected_content_sha256=digest,
        )
        if not summary["transcript_authority_probe"]["ok"] or not summary["expected_digest_match"]:
            raise RuntimeError("isolated acquisition probe failed")

        core = DaltonStore(str(state / "core.sqlite"))
        try:
            spool = RawSpool(str(state / "connector-spool"), max_total_bytes=1_000_000_000)
            transcript = promote_transcript_claim(
                core, spool, source_manifest, document, spec, state / "candidate-staging.sqlite",
            )

            model = ModelInputLedger(core)
            coverage = CoverageAdmissionAuthority(core)
            industry = IndustryResearchAuthority(core)
            pack_v1 = base._driver_pack_v1(coverage, base_manifest)
            if manifest["base_driver_pack_version_ref"] != pack_v1["id"]:
                raise ValueError("evidence manifest is bound to another base driver pack")
            pack_v2 = base._extend_driver_pack(coverage, pack_v1, manifest["driver_pack_v2"])
            pack_v3 = base._extend_driver_pack(coverage, pack_v2, peer_manifest["driver_pack_v3"])
            pack_v4 = base._extend_driver_pack(coverage, pack_v3, brief_manifest["driver_pack_v4"])

            evidence_by_key, claim_by_key, relation_by_key, input_by_claim_key = base._register_dataset(
                core, model, [manifest, peer_manifest]
            )
            key = spec["claim_key"]
            if key in claim_by_key:
                raise ValueError("transcript claim key collides with an SEC claim key")
            claim_by_key[key] = transcript["claim"]
            relation_by_key[key] = transcript["relation"]

            seed_pack = base._publish_evidence_pack(
                industry, manifest["evidence_pack"], pack_v2, claim_by_key, relation_by_key
            )
            base._publish_overlay(
                industry, manifest["company_overlay"], seed_pack, pack_v2,
                claim_by_key, input_by_claim_key,
            )
            peer_pack = base._publish_evidence_pack(
                industry, peer_manifest["evidence_pack"], pack_v3, claim_by_key, relation_by_key
            )
            for item in peer_manifest["company_overlays"]:
                base._publish_overlay(
                    industry, item, peer_pack, pack_v3, claim_by_key, input_by_claim_key
                )

            evidence_pack = base._publish_evidence_pack(
                industry, brief_manifest["evidence_pack"], pack_v4, claim_by_key, relation_by_key
            )
            overlay_specs = brief_manifest["company_overlays"]
            coverage_companies = {item["company_ref"] for item in evidence_pack["coverage_universe"]}
            if {item["company_ref"] for item in overlay_specs} != coverage_companies:
                raise ValueError("brief manifest must publish one overlay for every coverage company")
            overlays = [
                base._publish_overlay(
                    industry, item, evidence_pack, pack_v4, claim_by_key, input_by_claim_key
                )
                for item in overlay_specs
            ]
            overlay_ids = [item["id"] for item in overlays]
            brief = industry.industry_brief_snapshot(evidence_pack["id"], overlay_ids)
            rendered = industry.render_industry_brief_markdown(evidence_pack["id"], overlay_ids)
            replay = industry.render_industry_brief_markdown(evidence_pack["id"], overlay_ids)
            if rendered != replay or rendered["snapshot_hash"] != brief["content_hash"]:
                raise RuntimeError("industry brief v3 did not replay deterministically")

            transcript_claim_ref = transcript["claim"]["claim_version_id"]
            brief_claim = next(
                item for item in brief["claim_versions"]
                if item["claim_version_ref"] == transcript_claim_ref
            )
            transcript_source = next(
                item for item in brief["source_authorities"]
                if item["source_type"] == "authenticated_transcript"
            )
            report = industry.integrity_report()
            if not report["ok"] or not model.integrity_report()["ok"]:
                raise RuntimeError("isolated brief v3 canary integrity failed")
            counts = {
                table: int(core.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "evidence_versions", "claim_versions", "evidence_relations",
                    "reviewed_candidate_commits", "thesis_versions",
                    "connector_source_envelopes", "observability_artifact_versions_v2",
                )
            }
            if counts["thesis_versions"] != 0:
                raise RuntimeError("brief v3 canary must not create a thesis")
        finally:
            core.close()

    result = {
        "ok": True,
        "document_content_sha256": digest,
        "document_chars": len(document),
        "acquisition": {
            "manifest_ref": summary["manifest_ref"],
            "manifest_status": summary["manifest_status"],
            "page_count": summary["page_count"],
            "provider_calls": summary["provider_calls"],
            "document_quota_units": summary["document_quota_units"],
            "transport": summary["transport"],
        },
        "transcript": {
            "correction_set_version_ref": transcript["correction_set"]["id"],
            "unresolved_flag_count": transcript["correction_set"]["unresolved_count"],
            "citation_binding_ref": transcript["citation"]["id"],
            "citation_span": [transcript["citation"]["source_start"], transcript["citation"]["source_end"]],
            "candidate_claim_version_ref": transcript["candidate_claim"]["id"],
            "review_decision_ref": transcript["decision_ref"],
            "claim_version_ref": transcript_claim_ref,
            "evidence_version_ref": transcript["ledger_result"]["evidence_version_ref"],
            "relation_ref": transcript["ledger_result"]["relation_ref"],
            "claim_kind": transcript["claim_wire"]["claim_kind"],
            "value": transcript["claim_wire"]["value"],
            "normalized_statement": transcript["claim_wire"]["normalized_statement"],
        },
        "core_counts": counts,
        "industry_ref": evidence_pack["industry_ref"],
        "driver_pack_version_ref": pack_v4["id"],
        "driver_count": len(pack_v4["drivers"]),
        "metric_count": len(pack_v4["metric_specs"]),
        "coverage_company_count": len(evidence_pack["coverage_universe"]),
        "sec_evidence_count": len(evidence_by_key),
        "claim_count": len(claim_by_key),
        "model_input_count": len(input_by_claim_key),
        "evidence_pack_version_ref": evidence_pack["id"],
        "evidence_pack_hash": evidence_pack["content_hash"],
        "evidence_binding_count": len(evidence_pack["evidence_bindings"]),
        "company_overlay_version_refs": overlay_ids,
        "company_overlay_count": len(overlays),
        "industry_brief_hash": brief["content_hash"],
        "industry_brief_claim_count": len(brief["claim_versions"]),
        "industry_brief_source_count": len(brief["source_authorities"]),
        "industry_brief_transcript_claim": {
            "claim_kind": brief_claim["claim_kind"],
            "value": brief_claim["value"],
            "metric_or_aspect": brief_claim["metric_or_aspect"],
        },
        "industry_brief_transcript_source_ref": transcript_source["source_ref"],
        "driver_scoreboard_count": len(brief["driver_scoreboard"]),
        "metric_matrix_row_count": len(brief["metric_difference_matrix"]),
        "metric_matrix_cell_count": sum(
            len(item["companies"]) for item in brief["metric_difference_matrix"]
        ),
        "thesis_version_count": counts["thesis_versions"],
        "paid_model_calls": 0,
        "network_calls": 0,
        "live_core_writes": 0,
        "report_hash": rendered["content_hash"],
        "report_snapshot_hash": rendered["snapshot_hash"],
        "report_replay_identical": rendered == replay,
    }
    if include_report_body:
        result["report_body"] = rendered["body"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-file", type=Path, required=True,
                        help="UTF-8 transcript text served through the fake AlphaEngine handle")
    parser.add_argument("--page-chars", type=int, default=30_000)
    parser.add_argument("--base", type=Path, default=base.DEFAULT_BASE)
    parser.add_argument("--manifest", type=Path, default=base.DEFAULT_MANIFEST)
    parser.add_argument("--peer-manifest", type=Path, default=base.DEFAULT_PEER_MANIFEST)
    parser.add_argument("--brief-manifest", type=Path, default=DEFAULT_BRIEF_MANIFEST)
    parser.add_argument("--include-report-body", action="store_true")
    parser.add_argument("--output-dir", type=Path,
                        help="also write summary.json and brief-v3.md (owner-only) here")
    args = parser.parse_args()
    result = run(
        args.document_file, page_chars=args.page_chars, base_path=args.base,
        manifest_path=args.manifest, peer_manifest_path=args.peer_manifest,
        brief_manifest_path=args.brief_manifest,
        include_report_body=args.include_report_body or args.output_dir is not None,
    )
    if args.output_dir is not None:
        out = args.output_dir
        out.mkdir(parents=True, exist_ok=True)
        out.chmod(0o700)
        body = result["report_body"]
        (out / "brief-v3.md").write_text(body, encoding="utf-8")
        (out / "brief-v3.md").chmod(0o600)
        printable = {k: v for k, v in result.items() if k != "report_body"}
        (out / "summary.json").write_text(
            json.dumps(printable, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
            encoding="utf-8",
        )
        (out / "summary.json").chmod(0o600)
        if not args.include_report_body:
            result = printable
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
