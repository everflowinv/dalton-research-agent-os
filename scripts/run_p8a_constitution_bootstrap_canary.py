#!/usr/bin/env python3
"""Run the P8a constitution + initial Thesis bootstrap in an in-memory Core.

Publishes the versioned research constitution, admits one industry Thesis and
one ACN company Thesis through the human admission path, then proves the
company-to-thesis mapping resolves through the weekly brief production read
path.  No network, no paid model calls, no live writes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from dalton_core.agenda import AgendaStore
from dalton_core.contracts import ThesisVersion
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.industry_research import IndustryResearchAuthority
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.research_doctrine import ResearchDoctrineAuthority
from dalton_core.store import DaltonStore
from dalton_core.weekly_brief import WeeklyBriefAuthority
from dalton_core.weekly_brief_coordinator import WeeklyBriefSchedulePlan


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "deploy/phase8/p8a-us-it-services-bootstrap-v1.json"
UNMAPPED_COMPANY_REF = "company:sec-cik:0000890018"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def run(manifest_path: Path, *, actor_ref: str) -> dict[str, Any]:
    manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    expected = {
        "schema_version", "industry_ref", "company_ref", "ticker",
        "company_thesis_refs", "weekly_brief_plan_path", "mandate",
        "driver_pack_core", "driver_packs", "doctrine_pack", "constitution",
        "industry_thesis", "company_thesis",
    }
    if set(manifest) != expected or manifest["schema_version"] != "0.1":
        raise ValueError("manifest has an invalid closed shape")
    mapping = _object(manifest["company_thesis_refs"], "company_thesis_refs")
    if mapping != {manifest["company_ref"]: manifest["company_thesis"]["candidate"]["thesis_ref"]}:
        raise ValueError("company_thesis_refs must contain the one exact ACN mapping")

    store = DaltonStore(":memory:")
    try:
        agenda = AgendaStore(store)
        admission = CoverageAdmissionAuthority(store)
        industry = IndustryResearchAuthority(store)
        brief = WeeklyBriefAuthority(store, industry)
        doctrine = ResearchDoctrineAuthority(store)
        constitution = ResearchConstitutionAuthority(store)

        mandate_params = _object(manifest["mandate"], "mandate")
        mandate_ref = mandate_params.pop("mandate_ref")
        mandate = agenda.create_mandate(mandate_ref, actor_ref=actor_ref, **mandate_params)

        core = _object(manifest["driver_pack_core"], "driver_pack_core")
        pack_ref = core.pop("driver_pack_ref")
        packs = []
        for pack_version in manifest["driver_packs"]:
            params = {**core, **_object(pack_version, "driver_packs[]")}
            packs.append(admission.register_driver_pack(pack_ref, actor_ref=actor_ref, **params))
        active_pack = packs[-1]
        if active_pack["version"] != len(packs):
            raise ValueError("driver pack chain did not advance one version per manifest entry")

        doctrine_params = _object(manifest["doctrine_pack"], "doctrine_pack")
        doctrine_ref = doctrine_params.pop("doctrine_pack_ref")
        doctrine_pack = doctrine.publish_pack(doctrine_ref, actor_ref=actor_ref, **doctrine_params)

        policy = store.active_policy_version()
        plan_path = ROOT / manifest["weekly_brief_plan_path"]
        plan = WeeklyBriefSchedulePlan.from_mapping(
            _object(json.loads(plan_path.read_text(encoding="utf-8")), "weekly_brief_plan")
        )
        if dict(plan.company_thesis_refs) != mapping:
            raise ValueError("weekly brief plan mapping does not match the manifest mapping")

        constitution_params = _object(manifest["constitution"], "constitution")
        published = constitution.publish_constitution(
            constitution_params.pop("constitution_ref"),
            industry_ref=manifest["industry_ref"],
            bindings={
                "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
                "driver_pack_version": {"ref": active_pack["id"], "hash": active_pack["content_hash"]},
                "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
                "doctrine_pack_version": {"ref": doctrine_pack["id"], "hash": doctrine_pack["content_hash"]},
                "weekly_brief_plan": {"ref": plan.plan_ref, "hash": plan.content_hash},
            },
            actor_ref=actor_ref,
            **constitution_params,
        )
        constitution_replay = constitution.publish_constitution(
            published["constitution_ref"],
            industry_ref=manifest["industry_ref"],
            title=published["title"],
            bindings=published["bindings"],
            method=published["method"],
            actor_ref=actor_ref,
            version_id=published["id"],
            prior_version_ref=published["prior_version_ref"],
            idempotency_key=constitution_params["idempotency_key"],
        )

        theses = []
        for section in ("industry_thesis", "company_thesis"):
            block = _object(manifest[section], section)
            candidate = admission.propose_thesis_admission(
                mandate_version_ref=mandate["id"],
                mandate_version_hash=mandate["content_hash"],
                driver_pack_version_ref=active_pack["id"],
                driver_pack_version_hash=active_pack["content_hash"],
                actor_ref=actor_ref,
                **_object(block["candidate"], f"{section}.candidate"),
            )
            decision_params = _object(block["decision"], f"{section}.decision")
            admitted = admission.decide_thesis_admission(
                candidate_id=candidate["id"],
                candidate_hash=candidate["content_hash"],
                actor_ref=actor_ref,
                **decision_params,
            )
            replay = admission.decide_thesis_admission(
                candidate_id=candidate["id"],
                candidate_hash=candidate["content_hash"],
                actor_ref=actor_ref,
                **decision_params,
            )
            thesis = ThesisVersion.from_dict(admitted["thesis_version"])
            theses.append((thesis, admitted, replay))

        connection = store.connection
        company_bindings = brief._thesis_bindings(
            connection, [manifest["company_ref"]], mapping
        )
        unmapped_bindings = brief._thesis_bindings(
            connection, [UNMAPPED_COMPANY_REF], {}
        )
        active = constitution.active_constitution(published["constitution_ref"])
        report = constitution.constitution_report()
        if (
            company_bindings[0]["status"] != "current"
            or company_bindings[0]["thesis_version_ref"] != theses[1][0].id
            or unmapped_bindings[0]["status"] != "insufficient"
        ):
            raise ValueError("company-to-thesis mapping did not resolve through the brief read path")

        return {
            "status": "passed",
            "mode": "isolated-in-memory",
            "paid_model_calls": 0,
            "constitution_version_ref": published["id"],
            "constitution_content_hash": published["content_hash"],
            "constitution_replay_status": constitution_replay["status"],
            "constitution_report_count": report["constitution_count"],
            "active_constitution_version": active["version"],
            "driver_pack_versions": [pack["id"] for pack in packs],
            "doctrine_pack_version_ref": doctrine_pack["id"],
            "governance_policy_version_ref": policy.id,
            "weekly_brief_plan_ref": plan.plan_ref,
            "weekly_brief_plan_hash": plan.content_hash,
            "industry_thesis_version_ref": theses[0][0].id,
            "industry_thesis_authority": theses[0][0].authority_kind,
            "industry_thesis_replay_status": theses[0][2]["status"],
            "company_thesis_version_ref": theses[1][0].id,
            "company_thesis_authority": theses[1][0].authority_kind,
            "company_thesis_replay_status": theses[1][2]["status"],
            "company_thesis_refs": mapping,
            "mapped_binding_status": company_bindings[0]["status"],
            "unmapped_binding_status": unmapped_bindings[0]["status"],
            "thesis_version_count": connection.execute(
                "SELECT COUNT(*) FROM thesis_versions"
            ).fetchone()[0],
            "current_pointer_count": connection.execute(
                "SELECT COUNT(*) FROM current_pointers"
            ).fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--actor", default="human:isolated-canary-reviewer")
    args = parser.parse_args()
    result = run(args.manifest.resolve(), actor_ref=args.actor)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
