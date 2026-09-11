from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_release_copied_state_rehearsal import RehearsalBindingError
from scripts.run_successor_copied_state_rehearsal import (
    derive_confined_transition, validate_successor_snapshots,
)
from scripts.prepare_successor_config_transition import (
    apply_transition_to_scratch, canonical_hash,
)


class IdentityModule:
    @staticmethod
    def model_config_inventory(state_dir: Path):
        return {path.name: json.loads(path.read_text())
                for path in sorted(state_dir.glob("*-model-config.json"))}

    @staticmethod
    def rewrite_paths(value, _replacements):
        return value

    @staticmethod
    def invert(replacements):
        return replacements


class PathModule(IdentityModule):
    @staticmethod
    def rewrite_paths(value, replacements):
        if isinstance(value, dict):
            return {key: PathModule.rewrite_paths(item, replacements)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [PathModule.rewrite_paths(item, replacements) for item in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                if value.startswith(old): return new + value[len(old):]
        return value


class SuccessorCopiedStateRehearsalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state = root / "state"; self.state.mkdir()
        self.agents = root / "LaunchAgents"; self.agents.mkdir()
        self.config = root / "service.json"
        self.service = {"backup": {"enabled": True, "keep_latest": 3},
                        "thesis_impact": {"enabled": False}}
        self.config.write_text(json.dumps(self.service))
        self.models = {
            "initial-screen-model-config.json": {
                "routing_policy_ref": "route:preserved",
                "structured_output_repair": {"max_attempts": 1}},
            "mission-document-draft-model-config.json": {
                "routing_policy_ref": "route:draft"},
            "mission-document-verifier-model-config.json": {
                "routing_policy_ref": "route:verify"},
        }
        for name, value in self.models.items():
            (self.state / name).write_text(json.dumps(value))
        self.document = {"schema_version": "document-research-config-0.1"}
        self.lane = {"schema_version": "0.1", "enabled": True}
        (self.state / "document-research-config.json").write_text(json.dumps(self.document))
        (self.state / "mission-document-research-lane.json").write_text(json.dumps(self.lane))
        (self.agents / "space.lumos.dalton.writer.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": ["dalton-writer",
                "--mission-document-research-lane", str(self.state / "mission-document-research-lane.json")]})
        )
        self.rehearsal = SimpleNamespace(
            temp_state=self.state, temp_config=self.config,
            launch_agents_dir=self.agents, replacements={},
        )

    def validate(self):
        return validate_successor_snapshots(
            IdentityModule, self.rehearsal, expected_models=self.models,
            expected_document=self.document, expected_lane=self.lane,
            expected_service=self.service,
        )

    def test_final_snapshot_binds_configs_and_writer_lane_consumption(self):
        result = self.validate()
        self.assertEqual(result["model_config_count"], 3)
        self.assertEqual(result["writer_directed_document_flags"], [
            "--mission-document-research-lane"])

    def test_missing_writer_lane_flag_is_rejected(self):
        (self.agents / "space.lumos.dalton.writer.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": ["dalton-writer"]}))
        with self.assertRaisesRegex(RehearsalBindingError, "does not consume"):
            self.validate()

    def test_model_document_lane_and_preserved_service_drift_are_rejected(self):
        mutations = (
            (self.state / "mission-document-draft-model-config.json", {"changed": 1}, "model"),
            (self.state / "document-research-config.json", {"changed": 1}, "document"),
            (self.state / "mission-document-research-lane.json", {"enabled": False}, "lane"),
            (self.config, {"backup": {"keep_latest": 9}}, "service"),
        )
        for path, value, reason in mutations:
            with self.subTest(reason=reason):
                before = path.read_bytes(); path.write_text(json.dumps(value))
                with self.assertRaises(RehearsalBindingError): self.validate()
                path.write_bytes(before)

    def test_confined_transition_derives_scratch_paths_without_weakening_original(self):
        root = self.state.parent
        packet = root / "packet"; packet.mkdir()
        scratch = root / "scratch"; scratch.mkdir()
        scratch_state = scratch / "state/dalton-core"; scratch_state.mkdir(parents=True)
        baseline = {"initial-screen-model-config.json": {
            "purpose": "initial", "budget_db": "/live/state/budget.sqlite"}}
        rewritten = PathModule.rewrite_paths(baseline, {"/live": str(scratch)})
        for name, value in rewritten.items():
            (scratch_state / name).write_text(json.dumps(value))
        additions = {
            "mission-document-draft-model-config.json": {
                "purpose": "draft", "budget_db": "/live/state/budget.sqlite"},
            "mission-document-verifier-model-config.json": {
                "purpose": "verify", "budget_db": "/live/state/budget.sqlite"},
        }
        targets = []
        for name, value in additions.items():
            path = packet / name; path.write_text(json.dumps(value))
            targets.append({"name": name, "kind": "exclusive_add",
                            "before_sha256": None,
                            "after": {"file": name,
                                      "sha256": __import__('hashlib').sha256(path.read_bytes()).hexdigest()}})
        initial_after = {**baseline["initial-screen-model-config.json"],
                         "structured_output_repair": {"max_attempts": 1}}
        for suffix, value in (("before", baseline["initial-screen-model-config.json"]),
                              ("after", initial_after)):
            (packet / f"initial.{suffix}.json").write_text(json.dumps(value))
        digest = lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        targets.append({"name": "initial-screen-model-config.json",
                        "kind": "compare_and_replace",
                        "before": {"file": "initial.before.json",
                                   "sha256": digest(packet / "initial.before.json")},
                        "after": {"file": "initial.after.json",
                                  "sha256": digest(packet / "initial.after.json")}})
        for name, value in (("document-research-config.json", {"root": "/live/originals"}),
                            ("mission-document-research-lane.json",
                             {"schema_version": "0.1", "enabled": True})):
            path = packet / name; path.write_text(json.dumps(value))
            targets.append({"name": name, "kind": "exclusive_add",
                            "before_sha256": None,
                            "after": {"file": name, "sha256": digest(path)}})
        (packet / "models.before.json").write_text(json.dumps(baseline))
        (packet / "audit.json").write_text("{}")
        final = {**baseline, **additions,
                 "initial-screen-model-config.json": initial_after}
        manifest = {
            "schema_version": "successor-config-transition-0.1",
            "status": "prepared_inert", "release_ref": "test",
            "source_commit": "a" * 40,
            "acceptance": {"state": "pending", "full_suite_receipt_sha256": None,
                           "wheel_sha256": None,
                           "copied_state_rehearsal_binding_sha256": None,
                           "health_acceptance_required": True},
            "model_inventory": {"before_count": 1, "after_count": 3,
                                "before_semantic_sha256": canonical_hash(baseline),
                                "after_semantic_sha256": canonical_hash(final)},
            "targets": targets,
            "supporting_evidence": {
                "baseline_model_snapshot": {"file": "models.before.json",
                    "sha256": digest(packet / "models.before.json")},
                "document_research_readonly_audit": {"file": "audit.json",
                    "sha256": digest(packet / "audit.json")}},
            "preserved_authorities": [], "boundaries": {},
        }
        manifest["content_hash"] = canonical_hash(manifest)
        rehearsal = SimpleNamespace(temp_root=scratch, temp_state=scratch_state,
                                    replacements={"/live": str(scratch)})
        derived_path, proof_path, proof = derive_confined_transition(
            PathModule, rehearsal, packet_root=packet, manifest=manifest,
            original_manifest_sha256="b" * 64)
        derived = json.loads(derived_path.read_text())
        self.assertEqual(proof["original_transition_manifest_sha256"], "b" * 64)
        self.assertEqual(json.loads((derived_path.parent /
            derived["supporting_evidence"]["baseline_model_snapshot"]["file"]).read_text()),
            rewritten)
        installed = [json.loads((derived_path.parent / row["after"]["file"]).read_text())
                     for row in derived["targets"]]
        self.assertNotIn('/live', json.dumps(installed))
        self.assertTrue(proof_path.is_file())
        receipt = apply_transition_to_scratch(
            packet_root=derived_path.parent, scratch_root=scratch,
            state_dir=scratch_state, manifest_path=derived_path,
            expected_manifest_sha256=digest(derived_path),
            receipt_path=scratch / "transition-receipt.json")
        self.assertEqual(receipt["status"], "scratch_configuration_applied")
        self.assertEqual(len(PathModule.model_config_inventory(scratch_state)), 3)


if __name__ == "__main__":
    unittest.main()
