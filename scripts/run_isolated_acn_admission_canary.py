#!/usr/bin/env python3
"""Run the ACN coverage bootstrap entirely in an in-memory Dalton Core."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from dalton_core.agenda import AgendaStore
from dalton_core.contracts import ThesisVersion
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.store import DaltonStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "deploy/coverage/us-it-services-acn-bootstrap-v1.json"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def run(manifest_path: Path, *, actor_ref: str) -> dict[str, Any]:
    manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    expected = {
        "schema_version", "industry_ref", "company_ref", "ticker",
        "company_thesis_refs", "mandate", "driver_pack", "candidate", "decision",
    }
    if set(manifest) != expected or manifest["schema_version"] != "0.1":
        raise ValueError("manifest has an invalid closed shape")
    mapping = _object(manifest["company_thesis_refs"], "company_thesis_refs")
    if mapping != {manifest["company_ref"]: manifest["candidate"]["thesis_ref"]}:
        raise ValueError("company_thesis_refs must contain the one exact ACN mapping")

    store = DaltonStore(":memory:")
    try:
        agenda = AgendaStore(store)
        admission = CoverageAdmissionAuthority(store)

        mandate_params = _object(manifest["mandate"], "mandate")
        mandate_ref = mandate_params.pop("mandate_ref")
        mandate = agenda.create_mandate(
            mandate_ref, actor_ref=actor_ref, **mandate_params
        )

        pack_params = _object(manifest["driver_pack"], "driver_pack")
        pack_ref = pack_params.pop("driver_pack_ref")
        pack = admission.register_driver_pack(
            pack_ref, actor_ref=actor_ref, **pack_params
        )

        candidate_params = _object(manifest["candidate"], "candidate")
        candidate = admission.propose_thesis_admission(
            mandate_version_ref=mandate["id"],
            mandate_version_hash=mandate["content_hash"],
            driver_pack_version_ref=pack["id"],
            driver_pack_version_hash=pack["content_hash"],
            actor_ref=actor_ref,
            **candidate_params,
        )

        decision_params = _object(manifest["decision"], "decision")
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
        integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "status": "passed",
            "mode": "isolated-in-memory",
            "paid_model_calls": 0,
            "mapping_count": len(mapping),
            "company_thesis_refs": mapping,
            "driver_pack_version_ref": pack["id"],
            "mandate_version_ref": mandate["id"],
            "candidate_ref": candidate["id"],
            "decision_ref": admitted["decision"]["id"],
            "thesis_version_ref": thesis.id,
            "thesis_authority_kind": thesis.authority_kind,
            "confidence": thesis.confidence,
            "replay_status": replay["status"],
            "thesis_version_count": store.conn.execute(
                "SELECT COUNT(*) FROM thesis_versions"
            ).fetchone()[0],
            "integrity_check": integrity,
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
