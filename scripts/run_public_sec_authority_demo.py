#!/usr/bin/env python3
"""Run the isolated, read-only SEC authority -> candidate demo.

The harness owns temporary SQLite stores and a temporary raw spool.  It does
not open any repository/live database, and the output intentionally contains
only authority references and verification status—not the SEC response body.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.research_verification import (
    CandidateStagingStore,
    build_authority_source_material,
    build_candidate_claim,
    build_candidate_evidence,
    verify_authority_source_material,
    verify_numeric_spec,
)
from dalton_core.sec_authority_harness import SecAuthorityHarness, WIRE_WHEN
from dalton_core.store import content_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user-agent",
        default="Dalton Research Agent public-read-only canary",
        help="operator-visible SEC User-Agent (no credentials are accepted)",
    )
    args = parser.parse_args()
    harness = SecAuthorityHarness(live=True, user_agent=args.user_agent)
    try:
        resolver = harness.resolver()
        source_ref = harness.checkpoint["source_envelopes"][0]["ref"]
        resolved = resolver.resolve(source_ref, checkpoint_ref=harness.checkpoint["id"])
        material = build_authority_source_material(resolved)
        receipt = json.loads(
            harness.coordinator_store.connection.execute(
                "SELECT receipt_json FROM research_completion_receipts"
            ).fetchone()[0]
        )
        source_bundle = verify_authority_source_material(
            material,
            resolver=resolver,
            checkpoint=harness.checkpoint,
            plan=harness.plan,
            context_pack=harness.context,
            step=harness.step,
            runner_request=harness.coordinator_request,
            receipt=receipt,
        )
        count = len(material["source_record_refs"])
        spec = {
            "schema_version": "0.1",
            "id": "numeric-spec:sec:filing-count:demo",
            "created_at": WIRE_WHEN,
            "operator": "identity",
            "inputs": [{
                "name": "filing_count",
                "value": str(count),
                "unit": "records",
                "currency": None,
                "scale": "one",
                "period": "FY2025",
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": "/records",
                "extractor": "count",
            }],
            "output_value": str(count),
            "output_unit": "records",
            "output_currency": None,
            "output_scale": "one",
            "output_period": "FY2025",
            "rounding": {"mode": "down", "digits": 0},
        }
        spec["content_hash"] = content_hash(spec)
        numeric_bundle = verify_numeric_spec(
            spec,
            checkpoint_ref=harness.checkpoint["id"],
            checkpoint_hash=harness.checkpoint["content_hash"],
            source_material=material,
            source_bundle=source_bundle,
        )
        evidence = build_candidate_evidence(
            material,
            source_bundle,
            candidate_evidence_ref="candidate-evidence:sec:public-demo",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
            verification_mode="connector_authority",
        )
        claim = build_candidate_claim(
            evidence,
            source_bundle,
            spec,
            numeric_bundle,
            candidate_claim_ref="candidate-claim:sec:public-demo",
            subject_ref="company:issuer-0000789019",
            metric_or_aspect="filing_count",
            basis="official-filing",
            normalized_statement=f"The bounded SEC result contains {count} 2025 10-Q filings.",
            actor_ref="system:offline-verifier",
            created_at=WIRE_WHEN,
        )
        staging = CandidateStagingStore(":memory:")
        try:
            staged = staging.stage(
                checkpoint=harness.checkpoint,
                plan=harness.plan,
                context_pack=harness.context,
                step=harness.step,
                runner_request=harness.coordinator_request,
                receipt=receipt,
                material=material,
                numeric_spec=spec,
                source_verification=source_bundle,
                numeric_verification=numeric_bundle,
                evidence=evidence,
                claim=claim,
                idempotency_key="stage:sec:public-demo",
                verification_mode="connector_authority",
                authority_resolver=resolver,
            )
        finally:
            staging.close()
        print(json.dumps({
            "status": "human-review-ready-candidate",
            "source_ref": resolved.summary["source_ref"],
            "operation": resolved.summary["operation"],
            "source_record_count": count,
            "source_record_refs": resolved.summary["source_record_refs"],
            "source_envelope_ref": resolved.summary["source_envelope_ref"],
            "artifact_ref": resolved.summary["artifact_ref"],
            "authority_resolution_ref": resolved.summary["id"],
            "checkpoint_ref": resolved.summary["checkpoint_ref"],
            "coordinator_runner_request_ref": resolved.summary["runner_request_ref"],
            "actual_runner_request_ref": resolved.summary["actual_runner_request_ref"],
            "candidate_claim_ref": staged["candidate_claim_ref"],
            "semantic_verification_status": claim["semantic_verification_status"],
            "user_agent": args.user_agent,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
