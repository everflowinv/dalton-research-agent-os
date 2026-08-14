import json
import subprocess
import sys
import unittest

from dalton_core.demo import run_demo


class DemoTests(unittest.TestCase):
    def test_demo_commits_offline(self):
        result = run_demo(":memory:")
        self.assertEqual(result["commit"]["status"], "fresh")
        self.assertEqual(result["commit"]["thesis_id"], "demo-thesis")

    def test_demo_module_emits_json(self):
        completed = subprocess.run(
            [sys.executable, "-m", "dalton_core.demo"],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["commit"]["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
