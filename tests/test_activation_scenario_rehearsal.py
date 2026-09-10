from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.rehearse_activation_scenario import checked_json
from scripts.rehearse_deploy import Rehearsal


class ActivationScenarioTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
