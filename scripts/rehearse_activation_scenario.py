#!/usr/bin/env python3
"""SIMULATION ONLY: rehearse owner activation inside the deploy temp copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from rehearse_deploy import Rehearsal  # noqa: E402


ROLE_CONFIGS = {
    "event_brain": ("model-routing-policy:dalton-openclaw-event-judgement", "event-judgement-model-config.json"),
    "event_verifier": ("model-routing-policy:dalton-openclaw-event-verifier", "event-verifier-model-config.json"),
    "zero_base_brain": ("model-routing-policy:dalton-openclaw-zero-base-review", "zero-base-review-model-config.json"),
    "zero_base_verifier": ("model-routing-policy:dalton-openclaw-zero-base-review-verifier", "zero-base-review-verifier-model-config.json"),
    "dossier_brain": ("model-routing-policy:dalton-openclaw-company-dossier", "dossier-model-config.json"),
    "dossier_verifier": ("model-routing-policy:dalton-openclaw-dossier-verifier", "company-dossier-verifier-model-config.json"),
    "earnings_brain": ("model-routing-policy:dalton-openclaw-earnings-season", "earnings-season-model-config.json"),
    "earnings_verifier": ("model-routing-policy:dalton-openclaw-earnings-season-verifier", "earnings-season-verifier-model-config.json"),
    "claim_index": ("model-routing-policy:dalton-openclaw-claim-index", "claim-index-model-config.json"),
}


def checked_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"hash mismatch for {path}: expected {expected_sha256}, got {actual}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


class ActivationScenarioRehearsal(Rehearsal):
    def __init__(self, *args: Any, mission_params: Path, mission_sha256: str,
                 model_manifest: Path, model_manifest_sha256: str,
                 simulation_actor: str, governance_source: Path | None = None,
                 approve_governance: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mission_params_path = mission_params.resolve()
        self.mission_sha256 = mission_sha256
        self.model_manifest_path = model_manifest.resolve()
        self.model_manifest_sha256 = model_manifest_sha256
        self.simulation_actor = simulation_actor
        self.governance_source = governance_source
        self.approve_governance = approve_governance

    def post_catalog_sync_steps(self):
        return (("SIMULATION activation (temp copy only)", self.apply_activation),)

    def apply_activation(self) -> tuple[str, list[str]]:
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.connector_governance import ConnectorGovernance
        from dalton_core.deliverable_model_setup import install as install_deliverable
        from dalton_core.research_planner_setup import install as install_model
        from dalton_core.store import DaltonStore, content_hash

        if not self.confined:
            raise RuntimeError("SIMULATION activation requires passed confinement")
        params = checked_json(self.mission_params_path, self.mission_sha256)
        manifest = checked_json(self.model_manifest_path, self.model_manifest_sha256)
        if set(manifest) != {"schema_version", "simulation", "roles", "planner", "deliverable"}:
            raise ValueError("model manifest has an invalid closed shape")
        if manifest["schema_version"] != "activation-models-0.1" or manifest["simulation"] is not True:
            raise ValueError("model manifest must explicitly declare simulation=true")
        approved = []
        for name in self.approve_governance:
            if Path(name).name != name or not name.endswith(".json") or self.governance_source is None:
                raise ValueError("simulated governance approvals require plain JSON filenames and a source")
            source = self.governance_source / name
            record = json.loads(source.read_text(encoding="utf-8"))
            ConnectorGovernance(record)
            record["status"] = "approved"
            record["approved_by"] = self.simulation_actor
            record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            ConnectorGovernance(record)
            target = self.temp_state / "connector-governance" / name
            target.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            target.chmod(0o600)
            approved.append(name)
        core = self.temp_state / "core.sqlite"
        with DaltonStore(core) as store:
            ref = params.pop("mission_ref")
            mission = CoverageMissionAuthority(store).create_mission(
                ref, actor_ref=self.simulation_actor, **params)
        roles = manifest["roles"]
        if set(roles) != set(ROLE_CONFIGS):
            raise ValueError("model manifest must name every approved activation role exactly once")
        installed: list[str] = []
        for role, (policy_id, filename) in ROLE_CONFIGS.items():
            tier = roles[role]
            install_model(self.temp_config, tier=tier, policy_id=policy_id, config_file_name=filename)
            installed.append(filename)
        install_model(self.temp_config, tier=manifest["planner"])
        install_deliverable(self.temp_config, tier=manifest["deliverable"])
        installed += ["research-planner-model-config.json", "initial-screen-model-config.json"]
        outside = []
        for filename in installed:
            config = json.loads((self.temp_state / filename).read_text(encoding="utf-8"))
            for key in ("model_router_db", "broker_socket"):
                value = config.get(key)
                if isinstance(value, str) and not Path(value).resolve().is_relative_to(self.temp_root):
                    outside.append(f"{filename}:{key}={value}")
        if outside:
            raise RuntimeError("SIMULATION model configs escaped temp root: " + ", ".join(outside))
        return (f"SIMULATION mission={mission['id']}; model configs={len(installed)}; "
                f"governance approvals={len(approved)}; no paid calls", [])

    def report(self) -> str:
        return "SIMULATION — NOT LIVE, NOT PRODUCT ACCEPTANCE\n" + super().report()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--openclaw-config", type=Path, required=True)
    parser.add_argument("--mission-params", type=Path, required=True)
    parser.add_argument("--mission-sha256", required=True)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--model-manifest-sha256", required=True)
    parser.add_argument("--simulation-actor", required=True)
    parser.add_argument("--governance-source", type=Path)
    parser.add_argument("--approve-governance", action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not str(args.temp_root).startswith(("/tmp/", "/private/tmp/", "/var/folders/")):
        raise SystemExit("--temp-root must live under a temporary directory")
    rehearsal = ActivationScenarioRehearsal(
        args.live_root, args.temp_root, source_root=args.source_root,
        openclaw_config=args.openclaw_config, mission_params=args.mission_params,
        mission_sha256=args.mission_sha256, model_manifest=args.model_manifest,
        model_manifest_sha256=args.model_manifest_sha256,
        simulation_actor=args.simulation_actor,
        governance_source=args.governance_source,
        approve_governance=tuple(args.approve_governance),
    )
    code = rehearsal.run()
    report = rehearsal.report()
    print(report)
    if args.report:
        args.report.write_text(report + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
