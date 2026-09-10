from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import render


class WorkspaceLaunchAgentTests(unittest.TestCase):
    def test_two_workspaces_get_distinct_labels_paths_and_state_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels = []
            for slug, port in (("analyst-a", 7411), ("analyst-b", 7412)):
                workspace = root / slug
                paths = render(
                    root / "LaunchAgents",
                    root / "release" / "bin",
                    workspace / "state",
                    workspace / "config" / "service.json",
                    workspace / "logs",
                    label_namespace=f"space.lumos.dalton.workspace.{slug}",
                )
                writer = plistlib.loads(Path(paths["writer"]).read_bytes())
                controller = plistlib.loads(Path(paths["controller"]).read_bytes())
                labels.extend((writer["Label"], controller["Label"]))
                self.assertEqual(
                    controller["ProgramArguments"][-1],
                    str((workspace / "config" / "service.json").resolve()),
                )
                self.assertEqual(
                    writer["ProgramArguments"][writer["ProgramArguments"].index("--db") + 1],
                    str((workspace / "state" / "core.sqlite").resolve()),
                )
                self.assertTrue(writer["StandardOutPath"].startswith(str(workspace.resolve())))
                self.assertNotIn(str(port), " ".join(writer["ProgramArguments"]))
            self.assertEqual(len(labels), len(set(labels)))

    def test_invalid_namespace_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "namespace"):
                render(
                    root / "agents",
                    root / "release" / "bin",
                    root / "state",
                    root / "config.json",
                    root / "logs",
                    label_namespace="space.lumos.dalton.workspace.bad/slug",
                )
            self.assertFalse((root / "agents").exists())


if __name__ == "__main__":
    unittest.main()
