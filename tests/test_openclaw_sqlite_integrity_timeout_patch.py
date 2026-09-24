from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from integrations.openclaw_host_patches.patch_sqlite_integrity_timeout import (
    ORIGINAL, PATCHED, SUPERSEDED_MARKER, apply, superseded, target,
)

# Hermetic fixtures: excerpts of the reviewed OpenClaw dist bundles. Never read
# the locally installed OpenClaw here.
FIXTURES = Path(__file__).parent / "fixtures" / "openclaw_host_patches"
FIXTURE_2026_9_3 = FIXTURES / "openclaw-2026.9.3-sqlite"
FIXTURE_2026_9_6 = FIXTURES / "openclaw-2026.9.6"
SCRIPT = (Path(__file__).parents[1] / "integrations" / "openclaw_host_patches" /
          "patch_sqlite_integrity_timeout.py")


class TestPatch(unittest.TestCase):
    def copy_fixture(self, fixture: Path, td: str) -> Path:
        root = Path(td) / "openclaw"
        shutil.copytree(fixture, root)
        return root

    def test_exact_patch_keeps_integrity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.copy_fixture(FIXTURE_2026_9_3, td)
            self.assertFalse(superseded(root))
            self.assertTrue(apply(root))
            self.assertFalse(apply(root))
            self.assertFalse(apply(root, check=True))
            out = target(root).read_text()
            self.assertIn(PATCHED, out)
            self.assertIn("SQLITE_INSPECTION_TIMEOUT_MS", out)
            self.assertNotIn(ORIGINAL, out)

    def test_wrong_version_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "dist").mkdir()
            (root / "package.json").write_text(json.dumps({"version": "x"}))
            with self.assertRaises(ValueError):
                target(root)
            self.assertFalse(superseded(root))

    def test_2026_9_6_upstream_budget_supersedes_patch_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.copy_fixture(FIXTURE_2026_9_6, td)
            before = {p.name: p.read_bytes() for p in (root / "dist").glob("sqlite-readonly-worker-*.mjs")}
            self.assertEqual(len(before), 2)
            self.assertTrue(superseded(root))
            # The 120s patch must never be applied over the larger upstream budget.
            with self.assertRaises(ValueError):
                apply(root)
            for mode in ([], ["--check"]):
                run = subprocess.run(
                    [sys.executable, str(SCRIPT), *mode],
                    env={**os.environ, "OPENCLAW_INSTALL_ROOT": str(root)},
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("superseded upstream", run.stdout)
            after = {p.name: p.read_bytes() for p in (root / "dist").glob("sqlite-readonly-worker-*.mjs")}
            self.assertEqual(after, before)

    def test_2026_9_6_changed_budget_refuses_to_retire(self):
        with tempfile.TemporaryDirectory() as td:
            root = self.copy_fixture(FIXTURE_2026_9_6, td)
            chunk = root / "dist" / "sqlite-readonly-worker-CmkAsqCm.mjs"
            chunk.write_text(chunk.read_text().replace(
                SUPERSEDED_MARKER, "const SQLITE_INSPECTION_TIMEOUT_MS = 3e4;"))
            with self.assertRaisesRegex(ValueError, "budget changed"):
                superseded(root)
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "--openclaw-root", str(root)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(run.returncode, 1)
            self.assertIn("ERROR", run.stdout)


if __name__ == '__main__':
    unittest.main()
