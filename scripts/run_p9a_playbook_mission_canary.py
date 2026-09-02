#!/usr/bin/env python3
"""P9a canary: publish the research playbook and the US IT Services mission.

Two modes, both without network, paid model calls or live writes:

* default — an in-memory Core bootstrapped from the P8a manifest (mandate,
  driver packs, doctrine, constitution), then playbook + mission + a stage
  drill on ACN;
* ``--source-core PATH`` — copy an existing Core (for example the live
  ``core.sqlite``) into a temporary directory, bind the mission to the
  constitution and mandate that are *actually* active there, and run the same
  playbook + mission + stage drill on the copy.  The source file is opened
  read-only and never modified.

The stage drill proves the ledger rules: automation may enter and pass
``initial_screen``, may enter ``deep_insight_gate``, but its attempt to pass
that gate is rejected; a human passes it; the progress projection then points
ACN at ``industry_model``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

from dalton_core.agenda import AgendaStore
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.coverage_mission import CoverageMissionAuthority, CoverageMissionConflict
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.research_doctrine import ResearchDoctrineAuthority
from dalton_core.research_playbook import ResearchPlaybookAuthority
from dalton_core.store import DaltonStore


ROOT = Path(__file__).resolve().parents[1]
P8A_MANIFEST = ROOT / "deploy/phase8/p8a-us-it-services-bootstrap-v1.json"
PLAYBOOK_MANIFEST = ROOT / "deploy/phase9/p9a-research-playbook-v1.json"
MISSION_MANIFEST = ROOT / "deploy/phase9/p9a-us-it-services-mission-v1.json"
ACN = "company:sec-cik:0001467373"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _load(path: Path) -> dict[str, Any]:
    manifest = _object(json.loads(path.read_text(encoding="utf-8")), path.name)
    if manifest.pop("schema_version", None) != "0.1":
        raise ValueError(f"{path.name} schema_version must be 0.1")
    return manifest


def _bootstrap_in_memory(store: DaltonStore, actor_ref: str) -> dict[str, Any]:
    manifest = _object(json.loads(P8A_MANIFEST.read_text(encoding="utf-8")), "p8a manifest")
    agenda = AgendaStore(store)
    admission = CoverageAdmissionAuthority(store)
    doctrine = ResearchDoctrineAuthority(store)
    constitution = ResearchConstitutionAuthority(store)

    mandate_params = _object(manifest["mandate"], "mandate")
    mandate_ref = mandate_params.pop("mandate_ref")
    mandate = agenda.create_mandate(mandate_ref, actor_ref=actor_ref, **mandate_params)

    core = _object(manifest["driver_pack_core"], "driver_pack_core")
    pack_ref = core.pop("driver_pack_ref")
    active_pack = None
    for pack_version in manifest["driver_packs"]:
        params = {**core, **_object(pack_version, "driver_packs[]")}
        active_pack = admission.register_driver_pack(pack_ref, actor_ref=actor_ref, **params)
    if active_pack is None:
        raise ValueError("p8a manifest has no driver packs")

    doctrine_params = _object(manifest["doctrine_pack"], "doctrine_pack")
    doctrine_ref = doctrine_params.pop("doctrine_pack_ref")
    doctrine_pack = doctrine.publish_pack(doctrine_ref, actor_ref=actor_ref, **doctrine_params)

    policy = store.active_policy_version()
    constitution_params = _object(manifest["constitution"], "constitution")
    published = constitution.publish_constitution(
        constitution_params.pop("constitution_ref"),
        industry_ref=manifest["industry_ref"],
        bindings={
            "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
            "driver_pack_version": {"ref": active_pack["id"], "hash": active_pack["content_hash"]},
            "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
            "doctrine_pack_version": {"ref": doctrine_pack["id"], "hash": doctrine_pack["content_hash"]},
            "weekly_brief_plan": None,
        },
        actor_ref=actor_ref,
        **constitution_params,
    )
    return {"constitution": published, "mandate": mandate}


def _resolve_existing(store: DaltonStore, constitution_ref: str) -> dict[str, Any]:
    constitution = ResearchConstitutionAuthority(store).active_constitution(constitution_ref)
    binding = constitution["bindings"]["mandate_version"]
    row = store.connection.execute(
        "SELECT record_json FROM mandate_versions WHERE version_id=?", (binding["ref"],)
    ).fetchone()
    if row is None:
        raise ValueError("constitution mandate binding is missing from the source Core")
    mandate = json.loads(row["record_json"])
    if mandate.get("content_hash") != binding["hash"]:
        raise ValueError("constitution mandate binding hash drifted in the source Core")
    return {"constitution": constitution, "mandate": mandate}


def run(*, actor_ref: str, source_core: Path | None) -> dict[str, Any]:
    playbook_manifest = _load(PLAYBOOK_MANIFEST)
    mission_manifest = _load(MISSION_MANIFEST)
    temp = tempfile.TemporaryDirectory(prefix="dalton-p9a-canary-")
    try:
        if source_core is None:
            store = DaltonStore(":memory:")
            mode = "in_memory"
        else:
            copy = Path(temp.name) / "core.sqlite"
            source = sqlite3.connect(f"file:{source_core}?mode=ro", uri=True)
            try:
                target = sqlite3.connect(str(copy))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            store = DaltonStore(copy)
            mode = "source_copy"
        try:
            if source_core is None:
                state = _bootstrap_in_memory(store, actor_ref)
            else:
                state = _resolve_existing(store, mission_manifest["constitution_ref"])
            if state["mandate"]["mandate_ref"] != mission_manifest["mandate_ref"]:
                raise ValueError(
                    "mission manifest mandate_ref does not match the constitution's bound mandate"
                )

            playbooks = ResearchPlaybookAuthority(store)
            playbook_ref = playbook_manifest.pop("playbook_ref")
            playbook = playbooks.publish_playbook(playbook_ref, actor_ref=actor_ref, **playbook_manifest)
            playbook_replay = playbooks.publish_playbook(
                playbook_ref, actor_ref=actor_ref, **playbook_manifest
            )
            if playbook_ref != mission_manifest.pop("playbook_ref"):
                raise ValueError("mission manifest playbook_ref does not match the playbook manifest")
            mission_manifest.pop("constitution_ref")
            mission_manifest.pop("mandate_ref")

            missions = CoverageMissionAuthority(store)
            mission_ref = mission_manifest.pop("mission_ref")
            bindings = {
                "playbook_version": {"ref": playbook["id"], "hash": playbook["content_hash"]},
                "constitution_version": {
                    "ref": state["constitution"]["id"], "hash": state["constitution"]["content_hash"],
                },
                "mandate_version": {"ref": state["mandate"]["id"], "hash": state["mandate"]["content_hash"]},
            }
            mission = missions.create_mission(
                mission_ref, actor_ref=actor_ref, bindings=bindings, **mission_manifest
            )
            mission_replay = missions.create_mission(
                mission_ref, actor_ref=actor_ref, bindings=bindings, **mission_manifest
            )

            automation = mission["autonomy"]["automation_principal"]

            def stage(company: str, stage_ref: str, status: str, actor: str, evidence: list[str], key: str):
                return missions.record_stage(
                    mission_version_ref=mission["id"],
                    mission_version_hash=mission["content_hash"],
                    company_ref=company,
                    stage_ref=stage_ref,
                    status=status,
                    evidence_refs=evidence,
                    rationale=f"p9a canary {stage_ref} {status}",
                    actor_ref=actor,
                    idempotency_key=f"p9a-canary:{company}:{stage_ref}:{status}:{key}",
                )

            drill: dict[str, Any] = {}
            drill["initial_screen_entered"] = stage(ACN, "initial_screen", "entered", automation, [], "a")["status_marker"]
            drill["initial_screen_entered_replay"] = stage(ACN, "initial_screen", "entered", automation, [], "a")["status_marker"]
            drill["initial_screen_gate_passed"] = stage(
                ACN, "initial_screen", "gate_passed", automation,
                ["artifact-version:canary-acn-initial-screen"], "a",
            )["status_marker"]
            drill["deep_insight_gate_entered"] = stage(ACN, "deep_insight_gate", "entered", automation, [], "a")["status_marker"]
            try:
                stage(
                    ACN, "deep_insight_gate", "gate_passed", automation,
                    ["artifact-version:canary-acn-deep-insights"], "auto",
                )
                drill["automation_gate_pass_rejected"] = False
            except CoverageMissionConflict as exc:
                drill["automation_gate_pass_rejected"] = True
                drill["automation_gate_pass_error"] = str(exc)
            drill["deep_insight_gate_human_passed"] = stage(
                ACN, "deep_insight_gate", "gate_passed", actor_ref,
                ["artifact-version:canary-acn-deep-insights"], "human",
            )["status_marker"]

            progress = missions.mission_progress(mission_ref)
            acn = next(item for item in progress["companies"] if item["company_ref"] == ACN)
            integrity = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
            ok = (
                playbook["status"] == "fresh"
                and playbook_replay["status"] == "duplicate"
                and mission["status"] == "fresh"
                and mission_replay["status"] == "duplicate"
                and drill["initial_screen_entered"] == "fresh"
                and drill["initial_screen_entered_replay"] == "duplicate"
                and drill["initial_screen_gate_passed"] == "fresh"
                and drill["deep_insight_gate_entered"] == "fresh"
                and drill["automation_gate_pass_rejected"] is True
                and drill["deep_insight_gate_human_passed"] == "fresh"
                and acn["next_stage"] == "industry_model"
                and acn["completed_stages"] == ["initial_screen", "deep_insight_gate"]
                and integrity == "ok"
            )
            return {
                "ok": ok,
                "mode": mode,
                "source_core": None if source_core is None else str(source_core),
                "actor_ref": actor_ref,
                "playbook": {
                    "id": playbook["id"], "content_hash": playbook["content_hash"],
                    "status": playbook["status"], "replay": playbook_replay["status"],
                    "stage_refs": [item["stage_ref"] for item in playbook["stages"]],
                },
                "mission": {
                    "id": mission["id"], "content_hash": mission["content_hash"],
                    "status": mission["status"], "replay": mission_replay["status"],
                    "bindings": mission["bindings"],
                    "universe": [item["ticker"] for item in mission["universe"]],
                    "source_plan": {item["source_ref"]: item["status"] for item in mission["source_plan"]},
                },
                "stage_drill": drill,
                "acn_progress": acn,
                "integrity_check": integrity,
                "paid_model_calls": 0,
                "network_calls": 0,
                "live_writes": 0,
            }
        finally:
            store.close()
    finally:
        temp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default="human:coverage-owner")
    parser.add_argument("--source-core", type=Path, default=None,
                        help="copy this Core read-only and run the canary on the copy")
    parser.add_argument("--output", type=Path, default=None, help="write the summary JSON here")
    args = parser.parse_args()
    if args.source_core is not None and not args.source_core.is_file():
        parser.error("--source-core must point to an existing Core database")
    summary = run(actor_ref=args.actor, source_core=args.source_core)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        if args.output.exists():
            parser.error("--output must not already exist")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
