"""A reinstall must retain the configured evidence capacity and opt-in policy."""
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import render
from dalton_core.service import ServiceConfig, ServiceConfigError


class SpoolServiceConfigTests(unittest.TestCase):
    def config(self, root):
        return {
            "schema_version": "0.1", "core_db": str(root / "core.sqlite"),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "projection_db": str(root / "projection.sqlite"),
            "model_router_db": None, "capability_catalog_db": None,
            "heartbeat_path": str(root / "heartbeat.json"),
            "writer_socket": str(root / "writer.sock"),
            "tick_seconds": 1, "plugin_retry_seconds": 1, "plugins": [],
        }

    def test_reinstall_retains_policy_and_default_does_not_enable_archival(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = self.config(root)
            config = root / "service.json"
            for policy in (None, {"max_total_bytes": 4_000_000_000,
                                  "archive_after_seconds": 604800}):
                if policy:
                    raw["raw_spool"] = policy
                config.write_text(json.dumps(raw))
                for _ in range(2):
                    paths = render(root / "agents", root / "bin", root / "state",
                                   config, root / "logs")
                    for path in paths.values():
                        env = plistlib.loads(Path(path).read_bytes())["EnvironmentVariables"]
                        if policy:
                            self.assertEqual(env["DALTON_RAW_SPOOL_MAX_TOTAL_BYTES"], "4000000000")
                            self.assertEqual(env["DALTON_RAW_SPOOL_ARCHIVE_AFTER_SECONDS"], "604800")
                        else:
                            self.assertNotIn("DALTON_RAW_SPOOL_ARCHIVE_AFTER_SECONDS", env)

    def test_invalid_policy_fails_closed(self):
        for policy in (None, [], {"unknown": 10}, {"max_total_bytes": True},
                       {"max_total_bytes": 0}, {"archive_after_seconds": -1},
                       {"archive_after_seconds": "604800"}):
            with self.subTest(policy=policy):
                raw = self.config(Path("/tmp"))
                raw["raw_spool"] = policy
                with self.assertRaises(ServiceConfigError):
                    ServiceConfig.from_mapping(raw)
