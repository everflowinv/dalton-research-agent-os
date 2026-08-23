#!/usr/bin/env python3
"""Build the US IT Services evidence pack and ACN overlay in memory only."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.model_input import ModelInputLedger
from dalton_core.store import DaltonStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = ROOT / "deploy/coverage/us-it-services-acn-bootstrap-v1.json"
DEFAULT_MANIFEST = ROOT / "deploy/coverage/us-it-services-industry-evidence-v1.json"
ACTOR = "human:coverage-owner"
PRODUCER = "invocation:us-it-services-evidence-extractor:1"
PERIOD = {
    "start": "2026-03-01", "end": "2026-05-31",
    "calendar": "company:fiscal", "kind": "quarter",
}


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _driver_pack_v1(authority: CoverageAdmissionAuthority, manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest["driver_pack"]
    return authority.register_driver_pack(
        value["driver_pack_ref"], industry_ref=value["industry_ref"], title=value["title"],
        drivers=value["drivers"], metric_specs=value["metric_specs"],
        thesis_templates=value["thesis_templates"], actor_ref=ACTOR,
        version_id=value["version_id"], prior_version_ref=value["prior_version_ref"],
        idempotency_key=value["idempotency_key"],
    )


def _driver_pack_v2(
    authority: CoverageAdmissionAuthority,
    base: dict[str, Any], manifest: dict[str, Any], prior: dict[str, Any],
) -> dict[str, Any]:
    base_value = base["driver_pack"]
    patch = manifest["driver_pack_v2"]
    drivers = copy.deepcopy(base_value["drivers"])
    additions = patch["driver_metric_additions"]
    if set(additions) - {item["driver_ref"] for item in drivers}:
        raise ValueError("driver metric addition references an unknown driver")
    for driver in drivers:
        for metric_ref in additions.get(driver["driver_ref"], []):
            if metric_ref in driver["metric_refs"]:
                raise ValueError("driver metric addition is already present")
            driver["metric_refs"].append(metric_ref)
    metrics = copy.deepcopy(base_value["metric_specs"])
    existing = {item["metric_ref"] for item in metrics}
    if existing & {item["metric_ref"] for item in patch["metric_specs"]}:
        raise ValueError("driver pack v2 metric already exists in v1")
    metrics.extend(copy.deepcopy(patch["metric_specs"]))
    if patch["prior_version_ref"] != prior["id"]:
        raise ValueError("driver pack v2 prior version does not match v1")
    return authority.register_driver_pack(
        base_value["driver_pack_ref"], industry_ref=base_value["industry_ref"],
        title=patch["title"], drivers=drivers, metric_specs=metrics,
        thesis_templates=copy.deepcopy(base_value["thesis_templates"]), actor_ref=ACTOR,
        version_id=patch["version_id"], prior_version_ref=patch["prior_version_ref"],
        idempotency_key=patch["idempotency_key"],
    )


def _producer_invocation() -> dict[str, Any]:
    now = "2026-08-23T12:00:00+00:00"
    return {
        "schema_version": "0.1", "id": PRODUCER, "created_at": now,
        "work_order_ref": "work:us-it-services-evidence-pack:1",
        "profile_ref": "profile:deterministic-sec-extractor", "granularity": "task",
        "capability": "research", "provider": "provider:local",
        "model": "model:deterministic-sec-extractor", "model_family": "deterministic",
        "runtime_ref": "runtime:isolated-canary", "actor_ref": "automation:researcher",
        "usage": {"tokens": 0}, "input_refs": [], "output_refs": [],
        "started_at": now, "completed_at": now, "side_effects": [], "parent_ref": None,
    }


def run(base_path: Path, manifest_path: Path) -> dict[str, Any]:
    base = _load(base_path)
    manifest = _load(manifest_path)
    if manifest.get("schema_version") != "0.1":
        raise ValueError("unsupported evidence canary manifest")
    with DaltonStore(":memory:") as store:
        model = ModelInputLedger(store)
        coverage = CoverageAdmissionAuthority(store)
        industry = IndustryResearchAuthority(store)
        pack_v1 = _driver_pack_v1(coverage, base)
        if manifest["base_driver_pack_version_ref"] != pack_v1["id"]:
            raise ValueError("evidence manifest is bound to another base driver pack")
        pack_v2 = _driver_pack_v2(coverage, base, manifest, pack_v1)

        evidence_by_key: dict[str, dict[str, Any]] = {}
        for source in manifest["sources"]:
            evidence_by_key[source["source_key"]] = store.register_evidence({
                "evidence_ref": source["evidence_ref"], "source_type": source["source_type"],
                "source_ref": source["source_ref"], "retrieved_at": source["retrieved_at"],
                "source_lineage": source["source_lineage"],
                "independence_group": source["independence_group"],
                "actor_ref": "system:sec-adapter",
            })
        store.register_invocation(_producer_invocation())

        claim_by_key: dict[str, dict[str, Any]] = {}
        relation_by_key: dict[str, dict[str, Any]] = {}
        claim_spec_by_key: dict[str, dict[str, Any]] = {}
        for claim_spec in manifest["claims"]:
            key = claim_spec["claim_key"]
            if key in claim_by_key:
                raise ValueError("duplicate claim_key")
            evidence = evidence_by_key[claim_spec["source_key"]]
            claim = store.register_claim({
                "claim_ref": claim_spec["claim_ref"], "subject_ref": claim_spec["subject_ref"],
                "metric_or_aspect": claim_spec["metric_or_aspect"], "period": claim_spec["period"],
                "basis": claim_spec["basis"], "normalized_statement": claim_spec["normalized_statement"],
                "claim_kind": claim_spec["claim_kind"], "value": claim_spec["value"],
                "unit": claim_spec["unit"], "producer_invocation_refs": [PRODUCER],
                "actor_ref": "automation:researcher",
            })
            relation = store.relate_evidence({
                "id": f"relation:us-it-services:{key}",
                "evidence_version_ref": evidence["evidence_version_id"],
                "claim_version_ref": claim["claim_version_id"],
                "relation": claim_spec["relation"],
            })
            claim_by_key[key] = claim
            relation_by_key[key] = relation
            claim_spec_by_key[key] = claim_spec

        input_by_claim_key: dict[str, dict[str, Any]] = {}
        for index, input_spec in enumerate(manifest["model_inputs"], start=1):
            key = input_spec["claim_key"]
            claim_spec = claim_spec_by_key[key]
            evidence = evidence_by_key[claim_spec["source_key"]]
            candidate = model.propose_input(
                candidate_id=f"model-input-candidate:acn:q3fy26:{index}", input_kind="actual",
                model_input_ref=input_spec["model_input_ref"], prior_version_ref=None,
                payload={
                    "schema_version": "0.1", "metric_ref": claim_spec["metric_or_aspect"],
                    "subject_ref": claim_spec["subject_ref"], "business_line_ref": None,
                    "period": PERIOD, "unit": input_spec["unit"], "currency": input_spec["currency"],
                    "value": input_spec["value"], "source_authorities": [{
                        "authority_kind": "evidence_version",
                        "version_ref": evidence["evidence_version_id"],
                        "content_hash": evidence["content_hash"],
                    }],
                }, proposed_by="agent:industry-research",
                idempotency_key=f"model-input-candidate:acn:q3fy26:{index}",
            )["candidate"]
            input_by_claim_key[key] = model.decide_input(
                decision_id=f"model-input-decision:acn:q3fy26:{index}",
                candidate_id=candidate["id"], candidate_hash=candidate["content_hash"],
                verdict="admit", rationale="Exact ACN Q3 FY2026 SEC exhibit authority.",
                findings=[], reviewer_ref="human:coverage-owner",
                version_id=f"model-input-version:acn:q3fy26:{index}",
                idempotency_key=f"model-input-decision:acn:q3fy26:{index}",
            )["version"]

        pack_spec = manifest["evidence_pack"]
        bindings = []
        for item in pack_spec["evidence_bindings"]:
            claim = claim_by_key[item["claim_key"]]
            relation = relation_by_key[item["claim_key"]]
            bindings.append({
                "binding_ref": item["binding_ref"], "driver_ref": item["driver_ref"],
                "metric_ref": item["metric_ref"], "claim_version_ref": claim["claim_version_id"],
                "claim_version_hash": claim["content_hash"],
                "relation_refs": [{"ref": relation["relation_id"], "hash": relation["content_hash"]}],
            })
        debates = []
        for debate in pack_spec["debates"]:
            debates.append({
                "debate_ref": debate["debate_ref"], "question": debate["question"],
                "status": debate["status"], "positions": [{
                    "label": position["label"], "stance": position["stance"],
                    "claim_version_refs": [claim_by_key[key]["claim_version_id"] for key in position["claim_keys"]],
                } for position in debate["positions"]],
            })
        evidence_pack = industry.register_evidence_pack(
            pack_spec["evidence_pack_ref"], industry_ref=pack_spec["industry_ref"],
            title=pack_spec["title"], as_of=pack_spec["as_of"], boundary=pack_spec["boundary"],
            coverage_universe=pack_spec["coverage_universe"], driver_pack_version_ref=pack_v2["id"],
            driver_pack_version_hash=pack_v2["content_hash"], evidence_bindings=bindings,
            debates=debates, source_plan=pack_spec["source_plan"],
            report_contract=pack_spec["report_contract"], actor_ref=ACTOR,
            version_id=pack_spec["version_id"], prior_version_ref=pack_spec["prior_version_ref"],
            idempotency_key=pack_spec["idempotency_key"],
        )

        overlay_spec = manifest["company_overlay"]
        driver_views = []
        for view in overlay_spec["driver_views"]:
            driver_views.append({
                "driver_ref": view["driver_ref"], "stance": view["stance"],
                "claim_version_refs": [{
                    "ref": claim_by_key[key]["claim_version_id"],
                    "hash": claim_by_key[key]["content_hash"],
                } for key in view["claim_keys"]],
                "model_input_version_refs": [{
                    "ref": input_by_claim_key[key]["id"],
                    "hash": input_by_claim_key[key]["content_hash"],
                } for key in view["model_input_claim_keys"]],
                "differentiators": view["differentiators"], "watchpoints": view["watchpoints"],
            })
        overlay = industry.register_company_overlay(
            overlay_spec["overlay_ref"], company_ref=overlay_spec["company_ref"],
            industry_ref=overlay_spec["industry_ref"], title=overlay_spec["title"],
            as_of=overlay_spec["as_of"], role=overlay_spec["role"],
            evidence_pack_version_ref=evidence_pack["id"],
            evidence_pack_version_hash=evidence_pack["content_hash"],
            driver_views=driver_views, key_differences=overlay_spec["key_differences"],
            open_questions=overlay_spec["open_questions"], falsifier_refs=overlay_spec["falsifier_refs"],
            thesis_candidate_refs=overlay_spec["thesis_candidate_refs"], actor_ref=ACTOR,
            version_id=overlay_spec["version_id"], prior_version_ref=overlay_spec["prior_version_ref"],
            idempotency_key=overlay_spec["idempotency_key"],
        )

        report = industry.integrity_report()
        if not report["ok"] or not model.integrity_report()["ok"]:
            raise RuntimeError("isolated industry evidence canary integrity failed")
        if store.connection.execute("SELECT COUNT(*) FROM thesis_versions").fetchone()[0] != 0:
            raise RuntimeError("industry evidence canary must not create a thesis")
        return {
            "ok": True,
            "industry_ref": pack_spec["industry_ref"],
            "driver_pack_version_ref": pack_v2["id"],
            "driver_count": len(pack_v2["drivers"]),
            "coverage_company_count": len(evidence_pack["coverage_universe"]),
            "evidence_count": len(evidence_by_key),
            "claim_count": len(claim_by_key),
            "model_input_count": len(input_by_claim_key),
            "evidence_pack_version_ref": evidence_pack["id"],
            "evidence_pack_hash": evidence_pack["content_hash"],
            "company_overlay_version_ref": overlay["id"],
            "company_overlay_hash": overlay["content_hash"],
            "thesis_version_count": 0,
            "paid_model_calls": 0,
            "source_accession": "0001467373-26-000031",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    print(json.dumps(run(args.base, args.manifest), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
