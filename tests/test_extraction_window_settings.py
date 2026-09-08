"""P11r: the three window allowances survive the path from config to child.

The prose allowance was once set and silently lost by a plain re-install.  The
two secondary passes travel the same path -- service.json, LaunchAgent, writer,
coordinator, launcher, child argv -- so each hop is asserted rather than
assumed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_extraction_launcher import (
    DocumentExtractionCoordinator,
    DocumentExtractionLauncher,
    ExtractionLaunchRejected,
)


class LauncherCommandTests(unittest.TestCase):
    def launcher(self, root: Path) -> DocumentExtractionLauncher:
        config = root / "model-config.json"
        config.write_text("{}", encoding="utf-8")
        return DocumentExtractionLauncher(state_dir=root, model_config_path=config)

    def test_the_child_is_told_all_three_allowances(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            command = self.launcher(root)._command(
                requested_by=None, max_windows=30, ticket_dir=root,
                max_numeric_windows=10, max_discovery_windows=6,
            )
            for flag, value in (("--max-windows", "30"), ("--max-numeric-windows", "10"),
                                ("--max-discovery-windows", "6")):
                self.assertEqual(command[command.index(flag) + 1], value)

    def test_the_child_defaults_both_secondary_passes_off(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            command = self.launcher(root)._command(
                requested_by=None, max_windows=4, ticket_dir=root,
            )
            self.assertEqual(command[command.index("--max-numeric-windows") + 1], "0")
            self.assertEqual(command[command.index("--max-discovery-windows") + 1], "0")

    def test_an_out_of_range_discovery_allowance_is_refused_before_spawning(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaises(ExtractionLaunchRejected):
                self.launcher(root).start(max_windows=4, max_discovery_windows=51)


class RecordingLauncher:
    def __init__(self, root: Path) -> None:
        self.tickets_dir = root
        self.starts: list[dict] = []

    def start(self, **kwargs):
        self.starts.append(kwargs)
        return {"id": "document-extraction:" + "a" * 24, "status": "running"}


class CoordinatorTests(unittest.TestCase):
    def test_the_coordinator_passes_its_configured_allowances_on(self):
        class Missions:
            class connection:
                @staticmethod
                def execute(*_args):
                    class Row:
                        @staticmethod
                        def fetchone():
                            return [1]
                    return Row()

        with tempfile.TemporaryDirectory() as name:
            launcher = RecordingLauncher(Path(name))
            coordinator = DocumentExtractionCoordinator(
                missions=Missions(), launcher=launcher,
                max_windows_per_tick=30, numeric_windows_per_tick=10,
                discovery_windows_per_tick=6,
            )
            tick = coordinator.dispatch_once()
            self.assertEqual(tick["status"], "launched")
            self.assertEqual(launcher.starts, [{
                "max_windows": 30, "max_numeric_windows": 10, "max_discovery_windows": 6,
            }])
            self.assertEqual(tick["max_discovery_windows"], 6)


class ServiceConfigTests(unittest.TestCase):
    def base(self, root: Path) -> dict:
        return {
            "schema_version": "0.1",
            "core_db": str(root / "core.sqlite"),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "projection_db": str(root / "projection.sqlite"),
            "model_router_db": None,
            "capability_catalog_db": None,
            "heartbeat_path": str(root / "run" / "heartbeat.json"),
            "writer_socket": str(root / "run" / "writer.sock"),
            "tick_seconds": 1,
            "projection_min_interval_seconds": 1,
            "plugin_retry_seconds": 1,
            "plugins": [],
        }

    def config(self, block):
        from dalton_core.service import ServiceConfig, ServiceConfigError

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            raw = json.loads(json.dumps(self.base(root)))
            raw["document_extraction"] = block
            try:
                return ServiceConfig.from_mapping(raw)
            except ServiceConfigError as exc:
                return exc

    def test_the_discovery_allowance_round_trips(self):
        config = self.config({"max_windows_per_tick": 30, "numeric_windows_per_tick": 10,
                              "discovery_windows_per_tick": 6})
        self.assertEqual(config.document_extraction_discovery_windows, 6)
        self.assertEqual(config.document_extraction_numeric_windows, 10)

    def test_it_is_absent_rather_than_zero_when_unset(self):
        # Absent means "the writer keeps its own default"; zero means the owner
        # asked for it off. Collapsing the two would make the setting
        # unrecoverable from the config alone.
        config = self.config({"max_windows_per_tick": 30})
        self.assertIsNone(config.document_extraction_discovery_windows)

    def test_a_nonsense_allowance_refuses_the_config(self):
        from dalton_core.service import ServiceConfigError

        for value in (51, -1, True, "6"):
            result = self.config({"max_windows_per_tick": 30,
                                  "discovery_windows_per_tick": value})
            self.assertIsInstance(result, ServiceConfigError, value)


if __name__ == "__main__":
    unittest.main()
