from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import execute_successor_stopped_window_candidate as execute
from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, LANE_CONFIG, MODEL_ADDITIONS, MODEL_REPLACEMENT,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class SuccessorStoppedWindowCandidateTests(unittest.TestCase):
    def test_template_is_inert_and_does_not_guess_release_identity(self) -> None:
        candidate = execute.template()
        self.assertEqual("inputs_incomplete", candidate["status"])
        self.assertEqual("pending", candidate["acceptance_state"])
        self.assertIsNone(candidate["source"]["commit"])
        self.assertIsNone(candidate["runtime"]["model_config_count_before"])
        self.assertIsNone(candidate["runtime"]["model_config_count_after"])
        self.assertEqual(execute.ARTIFACT_NAMES, set(candidate["artifacts"]))
        self.assertTrue(all(row == {"file": None, "sha256": None}
                            for row in candidate["artifacts"].values()))
        self.assertFalse(candidate["boundaries"]["manifest_publication"])

    def test_unaccepted_or_incomplete_packet_cannot_reach_live_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary)
            manifest = execute.template()
            manifest["content_hash"] = execute.canonical_hash(manifest)
            write_json(packet / "release-manifest.candidate.json", manifest)
            with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                        "candidate manifest differs"):
                execute.packet_preflight(packet)

    def test_rollback_restores_owned_targets_and_preserves_one_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()

            before = {"route": "before"}
            after = {"route": "after", "structured_output_repair": {"max_attempts": 1}}
            rows = []
            for name in (*MODEL_ADDITIONS, DOCUMENT_CONFIG, LANE_CONFIG):
                artifact = packet / (name + ".after")
                value = ({"schema_version": "0.1", "enabled": True}
                         if name == LANE_CONFIG else {"name": name})
                write_json(artifact, value)
                rows.append({"name": name, "kind": "exclusive_add",
                             "after": {"file": artifact.name,
                                       "sha256": execute.sha(artifact)}})
                write_json(state / name, value)
            before_path = packet / "initial.before.json"
            after_path = packet / "initial.after.json"
            write_json(before_path, before); write_json(after_path, after)
            rows.insert(2, {"name": MODEL_REPLACEMENT,
                            "kind": "compare_and_replace",
                            "before": {"file": before_path.name,
                                       "sha256": execute.sha(before_path)},
                            "after": {"file": after_path.name,
                                      "sha256": execute.sha(after_path)}})
            write_json(state / MODEL_REPLACEMENT, after)
            write_json(rollback_state / MODEL_REPLACEMENT, before)
            write_json(state / "owner.json", {"kept": True})
            write_json(rollback_state / "owner.json", {"kept": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": rows})
            write_json(rollback / "initial-state.json", {
                "protected_state_sha256": "old",
            })

            # A concurrent owner replaces only one successor addition.  The
            # rollback must preserve it while restoring every other owned byte.
            conflict = state / MODEL_ADDITIONS[0]
            write_json(conflict, {"concurrent": True})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback",
                              return_value={"status": "rolled_back_healthy"}) as parent:
                with patch.object(execute.r11, "STATE", state):
                    result = worker.rollback()

            parent.assert_called_once_with()
            self.assertEqual([MODEL_ADDITIONS[0]],
                             result["preserved_concurrent_config_targets"])
            self.assertEqual({"concurrent": True}, json.loads(conflict.read_text()))
            self.assertEqual(before, json.loads((state / MODEL_REPLACEMENT).read_text()))
            self.assertFalse((state / MODEL_ADDITIONS[1]).exists())
            self.assertFalse((state / DOCUMENT_CONFIG).exists())
            self.assertFalse((state / LANE_CONFIG).exists())
            initial = json.loads((rollback / "initial-state.json").read_text())
            self.assertEqual(execute.r11.protected_state_hash(state),
                             initial["protected_state_sha256"])

    def test_rollback_refuses_unrelated_owner_drift_before_parent_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()
            artifact = packet / "draft.after.json"; write_json(artifact, {"draft": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": [{
                "name": MODEL_ADDITIONS[0], "kind": "exclusive_add",
                "after": {"file": artifact.name, "sha256": execute.sha(artifact)},
            }]})
            write_json(state / MODEL_ADDITIONS[0], {"concurrent": True})
            write_json(state / "owner.json", {"owner": "changed"})
            write_json(rollback_state / "owner.json", {"owner": "before"})
            write_json(rollback / "initial-state.json", {"protected_state_sha256": "old"})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback") as parent:
                with patch.object(execute.r11, "STATE", state):
                    with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                                "outside successor"):
                        worker.rollback()
            parent.assert_not_called()
            self.assertEqual({"concurrent": True},
                             json.loads((state / MODEL_ADDITIONS[0]).read_text()))


if __name__ == "__main__":
    unittest.main()
