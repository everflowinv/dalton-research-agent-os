from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.rehearse_activation_scenario import (
    ActivationScenarioRehearsal, ROLE_CONFIGS, checked_json, validate_mission_candidate,
    validate_model_manifest,
)
from scripts.rehearse_deploy import Rehearsal, StepResult


class ActivationScenarioTests(unittest.TestCase):
    def manifest(self):
        roles = {name: ("verifier" if name.endswith("verifier") else "brain")
                 for name in ROLE_CONFIGS}
        roles["claim_index"] = "cheap"
        return {"schema_version": "activation-models-0.1", "simulation": True,
                "roles": roles, "planner": "brain", "deliverable": "brain",
                "governance_allowlist": ["approved.json"]}

    def test_hash_bound_input_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text('{"simulation":true}\n', encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertTrue(checked_json(path, digest)["simulation"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            path.write_text('{"simulation":false}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                checked_json(path, digest)

    def test_base_rehearsal_has_no_activation_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rehearsal = Rehearsal(root / "live", root / "temp",
                                   openclaw_config=root / "openclaw.json")
            self.assertEqual(rehearsal.post_catalog_sync_steps(), ())

    def test_manifest_rejects_unlisted_governance_and_same_family_verifier(self):
        manifest = self.manifest()
        with self.assertRaisesRegex(ValueError, "not explicitly allowlisted"):
            validate_model_manifest(manifest, ("surprise.json",))
        manifest["roles"]["event_verifier"] = "brain"
        with self.assertRaisesRegex(ValueError, "event producer and verifier"):
            validate_model_manifest(manifest, ())

    def test_candidate_must_extend_exact_active_scope(self):
        active = {field: field for field in (
            "title", "objective", "industry_ref", "universe", "research_questions",
            "deliverables", "source_plan", "bindings", "budget")}
        active.update({"id": "mission:v13", "autonomy": {
            "automation_principal": "automation:x", "may_write": ["claim"],
            "human_checkpoints": ["deliverable"]}})
        candidate = {**active, "prior_version_ref": active["id"],
                     "autonomy": {"automation_principal": "automation:x",
                                  "may_write": ["claim", "dossier"],
                                  "human_checkpoints": ["deliverable", "gate_reopen"]}}
        validate_mission_candidate(candidate, active)
        candidate["universe"] = "different"
        with self.assertRaisesRegex(ValueError, "preserved scope field universe"):
            validate_mission_candidate(candidate, active)

    def test_catalog_failure_stops_activation_before_candidate_write(self):
        rehearsal = object.__new__(ActivationScenarioRehearsal)
        rehearsal.confined = True
        rehearsal.steps = [StepResult(
            "model catalog sync (copy of model-router.sqlite)", False, 0.0, "failed")]
        with self.assertRaisesRegex(RuntimeError, "successful model catalog sync"):
            rehearsal.apply_activation()


if __name__ == "__main__":
    unittest.main()
