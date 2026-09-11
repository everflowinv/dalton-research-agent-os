from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from integrations.openclaw_host_patches.patch_provider_failure_bridge import (
    ORIGINAL, PATCHED, apply, target,
)


INSTALLED = Path("/Users/everflow/.openclaw/tools/node-v26.8.2/lib/node_modules/openclaw")


class ProviderFailureBridgePatchTests(unittest.TestCase):
    def setUp(self):
        if not INSTALLED.exists():
            self.skipTest("reviewed OpenClaw installation is unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "openclaw"
        (self.root / "dist").mkdir(parents=True)
        shutil.copy2(INSTALLED / "package.json", self.root / "package.json")
        source = next((INSTALLED / "dist").glob("runtime-llm.runtime-*.mjs"))
        shutil.copy2(source, self.root / "dist" / source.name)
        # Exercise the unpatched input even after the reviewed patch is live.
        # Never modify the installed bundle to make a fixture pass.
        copied = self.root / "dist" / source.name
        wire = copied.read_text(encoding="utf-8")
        if wire.count(PATCHED) == 1 and not wire.count(ORIGINAL):
            copied.write_text(wire.replace(PATCHED, ORIGINAL, 1), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_bundle_is_patched_once_and_remains_valid_javascript(self):
        self.assertTrue(apply(self.root, check=False))
        self.assertFalse(apply(self.root, check=False))
        source = target(self.root).read_text(encoding="utf-8")
        self.assertEqual(source.count(PATCHED), 1)
        self.assertEqual(source.count(ORIGINAL), 0)
        checked = subprocess.run(["node", "--check", str(target(self.root))], check=False)
        self.assertEqual(checked.returncode, 0)

    def test_partial_or_wrong_version_refuses(self):
        path = target(self.root)
        path.write_text(path.read_text().replace(ORIGINAL, PATCHED + ORIGINAL, 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            apply(self.root, check=False)
        package = json.loads((self.root / "package.json").read_text())
        package["version"] = "2026.9.4"
        (self.root / "package.json").write_text(json.dumps(package))
        with self.assertRaises(ValueError):
            target(self.root)

    def test_concurrent_bundle_edit_during_syntax_check_is_preserved(self):
        bundle = target(self.root)
        changed = bundle.read_bytes() + b"\n// concurrent host maintenance\n"

        def check(*args, **kwargs):
            bundle.write_bytes(changed)
            return subprocess.CompletedProcess(args[0], 0, "", "")

        with patch("integrations.openclaw_host_patches.patch_provider_failure_bridge.subprocess.run", side_effect=check):
            with self.assertRaisesRegex(ValueError, "changed during patch validation"):
                apply(self.root, check=False)
        self.assertEqual(bundle.read_bytes(), changed)
        self.assertEqual(list(bundle.parent.glob(".*.mjs")), [])

    def test_bridge_only_accepts_exact_numeric_error_code_from_error_result(self):
        self.assertIn('result.stopReason === "error"', PATCHED)
        self.assertIn('/^(?:429|5\\d\\d)$/.test(result.errorCode)', PATCHED)
        self.assertNotIn("errorMessage", PATCHED)
        self.assertNotIn("requestId", PATCHED)

        expression = PATCHED.split("const returnedHttpStatus = ", 1)[1].split(";", 1)[0]
        script = f'''const project = (result) => ({expression});
const cases = [
  [{{stopReason:"error",errorCode:"429"}},429],
  [{{stopReason:"error",errorCode:"500"}},500],
  [{{stopReason:"error",errorCode:"599"}},599],
  [{{stopReason:"error",errorCode:"401"}},undefined],
  [{{stopReason:"error",errorCode:429}},undefined],
  [{{stopReason:"stop",errorCode:"429"}},undefined],
  [{{stopReason:"error",errorCode:"x429"}},undefined]
];
for (const [input, expected] of cases) {{
  if (project(input) !== expected) process.exit(2);
}}'''
        executed = subprocess.run(["node", "--input-type=module", "-e", script],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_control_proof_guard_precedes_failure_projection(self):
        self.assertLess(
            PATCHED.index("params.providerControls && !providerControlProof"),
            PATCHED.index("const returnedHttpStatus"),
        )


if __name__ == "__main__":
    unittest.main()
