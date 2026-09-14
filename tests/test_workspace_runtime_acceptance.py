import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_workspace_runtime_acceptance import run_acceptance


class WorkspaceRuntimeAcceptanceTests(unittest.TestCase):
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
            self.assertTrue(all(item["refused"] for item in result["isolation"].values()))
            stored = json.loads((output / "receipt.json").read_text())
            asserted = stored.pop("content_hash")
            from dalton_core.store import content_hash
            self.assertEqual(asserted, content_hash(stored))


if __name__ == "__main__":
    unittest.main()
