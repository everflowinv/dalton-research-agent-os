from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.dashboard import ProjectionWriter
from dalton_core.plugins.static_dashboard import (
    StaticDashboardError,
    TencentCosConfig,
    render_static_dashboard,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.macos_launchagent import CONTROLLER_LABEL, WRITER_LABEL, render
from dalton_core.health import check
from dalton_core.service import DaltonService, ServiceConfig, ServiceConfigError
from dalton_core.store import DaltonStore
from tests.test_dashboard import snapshot


class StaticDashboardTests(unittest.TestCase):
    def test_render_embeds_projection_and_escapes_script_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = root / "projection.sqlite"
            data = snapshot()
            data["workflow_summaries"][0]["title"] = "</script><b>unsafe</b>"
            with ProjectionWriter(projection) as writer:
                writer.replace(data)
            output = root / "public" / "index.html"
            result = render_static_dashboard(projection, output)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn("const EMBEDDED_DATA = null", html)
            self.assertNotIn("</script><b>unsafe", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertEqual(result["projection_watermark"], "watermark:42")

    def test_cos_publisher_is_scoped_to_dalton_key(self) -> None:
        raw = {
            "bucket": "bucket",
            "region": "region",
            "key": "index.html",
            "public_url": "https://example.com/dalton/",
            "keychain_account": "account",
            "secret_id_service": "id",
            "secret_key_service": "key",
            "protected_urls": [],
        }
        with self.assertRaises(StaticDashboardError):
            TencentCosConfig.from_mapping(raw)


class ServiceTests(unittest.TestCase):
    def test_one_cycle_sweeps_projects_and_renders_without_an_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1",
                "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [
                    {
                        "type": "static_dashboard",
                        "enabled": True,
                        "output_path": str(root / "public" / "index.html"),
                        "publisher": None,
                    }
                ],
            }
            service = DaltonService(ServiceConfig.from_mapping(raw))
            try:
                heartbeat = service.run_once(force_projection=True)
            finally:
                service.close()
            self.assertEqual(heartbeat["state"], "running")
            self.assertIsNotNone(heartbeat["projection_watermark"])
            self.assertEqual(heartbeat["plugins"]["static_dashboard"]["state"], "ready")
            self.assertTrue((root / "projection.sqlite").is_file())
            self.assertTrue((root / "public" / "index.html").is_file())
            saved = json.loads((root / "run" / "heartbeat.json").read_text())
            self.assertEqual(saved["service"], "daltond")

    def test_config_rejects_arbitrary_plugin_imports(self) -> None:
        raw = {
            "schema_version": "0.1",
            "core_db": "/tmp/core.sqlite",
            "scheduler_db": "/tmp/scheduler.sqlite",
            "projection_db": "/tmp/projection.sqlite",
            "model_router_db": None,
            "capability_catalog_db": None,
            "heartbeat_path": "/tmp/heartbeat.json",
            "writer_socket": "/tmp/writer.sock",
            "tick_seconds": 1,
            "projection_min_interval_seconds": 1,
            "plugin_retry_seconds": 1,
            "plugins": [{"type": "os.system", "enabled": True}],
        }
        with self.assertRaises(ServiceConfigError):
            ServiceConfig.from_mapping(raw)

    def test_launchagents_keep_only_deterministic_services_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = render(
                root / "LaunchAgents",
                root / "venv" / "bin",
                root / "state",
                root / "config.json",
                root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            controller = plistlib.loads(Path(paths["controller"]).read_bytes())
            self.assertEqual(writer["Label"], WRITER_LABEL)
            self.assertEqual(controller["Label"], CONTROLLER_LABEL)
            self.assertTrue(writer["KeepAlive"])
            self.assertTrue(controller["KeepAlive"])
            self.assertIn("dalton-writer", writer["ProgramArguments"][0])
            self.assertIn("daltond", controller["ProgramArguments"][0])
            self.assertNotIn("model", " ".join(controller["ProgramArguments"]).lower())

    def test_health_rejects_degraded_controller_even_with_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("core.sqlite", "scheduler.sqlite", "projection.sqlite"):
                (root / name).write_bytes(b"placeholder")
            heartbeat = root / "heartbeat.json"
            heartbeat.write_text(json.dumps({
                "state": "degraded",
                "pid": 99999999,
                "last_tick_at": "2026-08-14T00:00:00+00:00",
                "plugins": {"static_dashboard": {"state": "ready"}},
            }))
            config = root / "service.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(heartbeat),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
            }))
            result = check(config, max_age_seconds=10**9)
            self.assertFalse(result["ok"])
            self.assertFalse(result["checks"]["controller_state_running"])


if __name__ == "__main__":
    unittest.main()
