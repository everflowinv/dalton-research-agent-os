from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_two_workspace_acceptance import run_acceptance


class TwoWorkspaceAcceptanceTests(unittest.TestCase):
    def test_complete_local_acceptance_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance"
            result = run_acceptance(output)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(result["broker"]["accepted"], 2)
            self.assertEqual(result["capacity"]["model"], ["capacity_refused", "reserved"])
            self.assertEqual(result["capacity"]["connector"], ["capacity_refused", "reserved"])
            self.assertTrue(result["port_collision"]["refused"])
            self.assertTrue(result["release_rollback"]["b_running"])
            self.assertEqual(result["release_rollback"]["b_tree_before"], result["release_rollback"]["b_tree_after"])
            self.assertEqual(result["release_rollback"]["a_manifest_original_sha256"], result["release_rollback"]["a_manifest_restored_sha256"])
            stored = json.loads((output / "result.json").read_text())
            self.assertEqual(stored["status"], "passed")
            with self.assertRaisesRegex(RuntimeError, "must be new"):
                run_acceptance(output)


if __name__ == "__main__": unittest.main()
