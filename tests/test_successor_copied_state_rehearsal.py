from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_release_copied_state_rehearsal import RehearsalBindingError
from scripts.run_successor_copied_state_rehearsal import (
    validate_successor_snapshots,
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


if __name__ == "__main__":
    unittest.main()
