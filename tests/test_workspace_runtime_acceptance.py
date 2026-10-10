import json
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.run_workspace_runtime_acceptance import run_acceptance, _wait_initial_screen


class WorkspaceRuntimeAcceptanceTests(unittest.TestCase):
    def test_discovery_success_still_waits_for_formal_stage_entry(self):
        mission = {"mission_ref": "mission:fixture", "id": "mission-version:fixture"}
        waiting = {"mission_version_ref": mission["id"], "companies": []}
        entered = {"mission_version_ref": mission["id"], "companies": [{
            "current_stage": "initial_screen", "current_status": "entered", "record_count": 1}]}
        authority = SimpleNamespace(mission_progress=Mock(side_effect=[waiting, entered]))
        with patch("scripts.run_workspace_runtime_acceptance.time.sleep"), patch(
            "scripts.run_workspace_runtime_acceptance.time.monotonic", return_value=0):
            self.assertEqual(_wait_initial_screen(authority, mission), entered)
        self.assertEqual(authority.mission_progress.call_args_list,
                         [unittest.mock.call(mission["mission_ref"])] * 2)

    def test_stage_wait_is_bounded_and_wrong_mission_is_not_a_success(self):
        mission = {"mission_ref": "mission:fixture", "id": "mission-version:fixture"}
        authority = SimpleNamespace(mission_progress=Mock(return_value={
            "mission_version_ref": mission["id"], "companies": []}))
        with patch("scripts.run_workspace_runtime_acceptance.time.sleep"), patch(
            "scripts.run_workspace_runtime_acceptance.time.monotonic", side_effect=[0, 0, 21]):
            with self.assertRaisesRegex(RuntimeError, "initial_screen/entered"):
                _wait_initial_screen(authority, mission)
        authority.mission_progress.return_value = {"mission_version_ref": "mission-version:foreign"}
        with self.assertRaisesRegex(RuntimeError, "workspace mission authority differs"):
            _wait_initial_screen(authority, mission)

    def test_two_real_engines_keep_processing_and_refuse_cross_namespace_inputs(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "receipt"
            result = run_acceptance(output)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["external_provider_calls"], 0)
            self.assertGreater(result["continuous_processing"]["a_ticks_after_b"],
                               result["continuous_processing"]["a_ticks_before_b"])
            self.assertTrue(result["continuous_processing"]["a_pid_unchanged"])
            self.assertEqual({item["company_ref"] for item in result["workspaces"]},
                             {"company:ticker:same"})
            self.assertGreater(result["engine"]["a_research_tick"]["lane_count"], 0)
            self.assertGreater(result["engine"]["b_research_tick"]["lane_count"], 0)
            self.assertEqual(
                result["engine"]["a_research_tick"]["mission_stage"]["status"], "entered")
            self.assertEqual(
                result["engine"]["b_research_tick"]["mission_stage"]["status"], "entered")
            self.assertEqual(
                result["engine"]["a_mission_progress"]["companies"][0]["current_stage"],
                "initial_screen")
            self.assertEqual(
                result["engine"]["b_mission_progress"]["companies"][0]["current_stage"],
                "initial_screen")
            for key in ("a_source_discovery", "b_source_discovery"):
                self.assertGreater(result["engine"][key]["source_discovery_records"], 0)
                self.assertGreater(result["engine"][key]["discovered_document_records"], 0)
                self.assertTrue(result["engine"][key]["succeeded_ticket_refs"])
                self.assertEqual(result["engine"][key]["selected_mission_ref"],
                                 result["workspaces"][0 if key.startswith("a_") else 1]["mission_ref"])
            self.assertTrue(all(item["refused"] for item in result["isolation"].values()))
            stored = json.loads((output / "receipt.json").read_text())
            asserted = stored.pop("content_hash")
            from dalton_core.store import content_hash
            self.assertEqual(asserted, content_hash(stored))


if __name__ == "__main__":
    unittest.main()
