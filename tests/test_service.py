from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.dashboard import ProjectionWriter
from dalton_core.bootstrap import bootstrap
from dalton_core.plugins.static_dashboard import (
    StaticDashboardError,
    TencentCosConfig,
    render_static_dashboard,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.macos_launchagent import (
    CONTROL_LABEL,
    CONTROLLER_LABEL,
    THESIS_IMPACT_LABEL,
    WRITER_LABEL,
    render,
)
from dalton_core.health import check
from dalton_core.service import DaltonService, ServiceConfig, ServiceConfigError
from dalton_core.store import DaltonStore
from dalton_core.writer_server import (
    DASHBOARD_CONTROL_OPERATIONS,
    RESEARCH_REVIEW_CONTROL_OPERATIONS,
    WriterServerError,
    load_principals,
)
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
            self.assertEqual("disabled", heartbeat["weekly_brief"]["state"])
            self.assertEqual("disabled", heartbeat["bounded_planner"]["state"])
            self.assertIsNotNone(heartbeat["projection_watermark"])
            self.assertEqual(heartbeat["plugins"]["static_dashboard"]["state"], "ready")
            self.assertTrue((root / "projection.sqlite").is_file())
            self.assertTrue((root / "public" / "index.html").is_file())
            saved = json.loads((root / "run" / "heartbeat.json").read_text())
            self.assertEqual(saved["service"], "daltond")

    def test_bounded_planner_block_parses_and_reports_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
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
                "bounded_planner": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "writer_socket": str(root / "run" / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "scheduler_db": str(root / "scheduler.sqlite"),
                        "user_agent": "Dalton Research Bounded Planner",
                        "max_response_bytes": 8_388_608,
                        "timeout_seconds": 60.0,
                        "max_probes_per_tick": 1,
                        "filed_window_days": 400,
                        "observation_mandate_version_ref": (
                            "mandate-version:us-it-services-sec-lane:v3"
                        ),
                        "doctrine_pack_version_ref": None,
                        "doctrine_pack_version_hash": None,
                        "planner_routing_policy_ref": None,
                        "planner_credential_slot_refs": None,
                        "planner_model_router_db": None,
                        "planner_broker_socket": None,
                        "planner_broker_auth_key": None,
                        "planner_broker_client_id": "client:dalton-core",
                        "planner_expected_agent_id": "chem",
                        "planner_max_cost_usd": 0.5,
                    },
                },
            }
            config = ServiceConfig.from_mapping(raw)
            self.assertEqual(300.0, config.bounded_planner_interval_seconds)
            self.assertEqual(1, config.bounded_planner.max_probes_per_tick)
            bad = json.loads(json.dumps(raw))
            bad["bounded_planner"]["config"]["surprise"] = True
            with self.assertRaises(ServiceConfigError):
                ServiceConfig.from_mapping(bad)

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

    def test_weekly_brief_schedule_is_managed_by_the_existing_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "weekly_brief": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "plan": {
                            "schema_version": "0.1",
                            "plan_ref": "weekly-brief-plan:test:v1",
                            "brief_ref": "weekly-brief:test",
                            "timezone": "America/New_York",
                            "weekday": 3, "hour": 7, "minute": 0,
                            "effective_from": "2026-08-27T00:00:00+00:00",
                            "evidence_pack_version_id": "evidence-pack-version:test",
                            "company_overlay_version_ids": ["overlay-version:test"],
                            "company_thesis_refs": {},
                            "destination_ref": "openclaw:discord:test",
                        },
                    },
                },
                "outbox": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "config": {
                        "openclaw_executable": "/usr/bin/true",
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "account": "default", "target": "channel:123",
                        "guild_id": "456", "channel_id": "123",
                        "endpoint_ref": "openclaw:discord:test",
                        "control_url": "https://dalton.example.test/",
                        "company_labels": {}, "feedback_user_ids": [],
                        "timeout_seconds": 30, "claim_ttl_seconds": 120,
                        "retry_seconds": 60, "max_attempts": 5,
                        "batch_size": 1, "feedback_limit": 10,
                        "weekly_brief_attachment_dir": str(root / "attachments"),
                    },
                },
            }
            config = ServiceConfig.from_mapping(raw)
            self.assertEqual(300, config.weekly_brief_interval_seconds)
            self.assertEqual(
                "weekly-brief-plan:test:v1", config.weekly_brief.plan.plan_ref
            )

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
            self.assertIn("--scheduler", writer["ProgramArguments"])
            self.assertIn("daltond", controller["ProgramArguments"][0])
            self.assertNotIn("model", " ".join(controller["ProgramArguments"]).lower())

    def test_writer_launchagent_is_standard_process_type_others_background(self) -> None:
        # S7d: writer-hosted children inherit the writer's launchd process
        # type; Background runs CPU work ~6x slower and cannot be lifted from
        # inside the child (measured 2026-08-26).
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
            self.assertEqual(writer["ProcessType"], "Standard")
            self.assertEqual(controller["ProcessType"], "Background")

    def test_enabled_thesis_impact_gets_short_lived_launchagent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": str(root / "router.sqlite"),
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "thesis_impact": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "scheduler_db": str(root / "scheduler.sqlite"),
                        "model_router_db": str(root / "router.sqlite"),
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "broker_socket": str(root / "broker.sock"),
                        "broker_auth_key": str(root / "broker.key"),
                        "budget_db": str(root / "budget.sqlite"),
                        "routing_policy_ref": "policy:shared",
                        "assessment_routing_policy_ref": "policy:assessment",
                        "verifier_routing_policy_ref": "policy:verifier",
                        "budget_policy_version_id": "budget:1",
                        "day_cap_micros": 25_000_000,
                        "credential_slot_refs": ["credential-slot:openai", "credential-slot:google"],
                        "broker_client_id": "client:dalton-core",
                        "expected_agent_id": "chem",
                        "company_thesis_refs": {},
                        "max_targets": 25,
                        "timeout_seconds": 180,
                    },
                },
            }
            config.write_text(json.dumps(raw))
            paths = render(
                root / "LaunchAgents",
                root / "venv" / "bin",
                root / "state",
                config,
                root / "logs",
            )
            agent = plistlib.loads(Path(paths["thesis_impact"]).read_bytes())
            self.assertEqual(agent["Label"], THESIS_IMPACT_LABEL)
            self.assertNotIn("KeepAlive", agent)
            self.assertTrue(agent["RunAtLoad"])
            self.assertEqual(agent["StartInterval"], 300)
            self.assertIn("dalton-thesis-impact", agent["ProgramArguments"][0])

    def test_enabled_control_plane_gets_a_separate_launchagent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "control": {"enabled": True, "config": {
                    "host": "127.0.0.1", "port": 8793,
                    "tailscale_host": "dalton.example.ts.net",
                    "tailscale_executable": "/usr/bin/true",
                    "allowed_tailscale_logins": ["owner@example.com"],
                    "writer_socket": str(root / "writer.sock"),
                    "token_config": str(root / "tokens.json"),
                    "endpoint_ref": "openclaw:discord:test",
                    "feedback_timeout_seconds": 86400,
                    "sweep_interval_seconds": 60,
                    "research_review": {
                        "candidate_staging_path": str(
                            root / "candidate-staging.sqlite"
                        ),
                        "transcript_review_directory": str(
                            root / "review-inbox"
                        ),
                        "reconcile_interval_seconds": 60,
                    },
                }},
            }), encoding="utf-8")
            paths = render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )
            control = plistlib.loads(Path(paths["control"]).read_bytes())
            self.assertEqual(control["Label"], CONTROL_LABEL)
            self.assertTrue(control["KeepAlive"])
            self.assertIn("dalton-control", control["ProgramArguments"][0])
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            self.assertIn("--transcript-spool-dir", writer["ProgramArguments"])
            service = ServiceConfig.from_file(config)
            self.assertIsNotNone(service.control.research_review)
            self.assertNotIn("research_review", paths)
            # S7c-3: the writer stages transcript candidates into the very
            # file the Cockpit reviews, derived from the control config.
            writer_args = writer["ProgramArguments"]
            self.assertIn("--candidate-staging", writer_args)
            # S7d: the SEC lane rides on the same staging file and is only
            # wired when that file is configured.
            self.assertIn("--sec-lane-governance", writer_args)
            self.assertEqual(
                writer_args[writer_args.index("--sec-lane-governance") + 1],
                str((root / "state").resolve() / "connector-governance" / "sec-company-facts-v2.json"),
            )
            self.assertIn("--sec-lane-user-agent", writer_args)
            self.assertEqual(
                writer_args[writer_args.index("--candidate-staging") + 1],
                str(service.control.research_review.candidate_staging_path),
            )
            self.assertEqual(
                writer_args[writer_args.index("--candidate-staging") + 1],
                str(root / "candidate-staging.sqlite"),
            )

    def test_writer_without_control_plane_has_no_candidate_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
            }), encoding="utf-8")
            paths = render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            self.assertNotIn("--candidate-staging", writer["ProgramArguments"])
            self.assertNotIn("--sec-lane-governance", writer["ProgramArguments"])
            self.assertIn("--connector-governance", writer["ProgramArguments"])
            self.assertNotIn("control", paths)

    def test_bootstrap_installs_embedded_review_principal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(state / "core.sqlite"),
                "scheduler_db": str(state / "scheduler.sqlite"),
                "projection_db": str(state / "dashboard-projection.sqlite"),
                "model_router_db": str(state / "model-router.sqlite"),
                "capability_catalog_db": None,
                "heartbeat_path": str(state / "run" / "heartbeat.json"),
                "writer_socket": str(state / "run" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "control": {"enabled": True, "config": {
                    "host": "127.0.0.1", "port": 8793,
                    "tailscale_host": "dalton.example.ts.net",
                    "tailscale_executable": "/usr/bin/true",
                    "allowed_tailscale_logins": ["owner@example.com"],
                    "writer_socket": str(state / "run" / "writer.sock"),
                    "token_config": str(state / "writer-tokens.json"),
                    "endpoint_ref": "openclaw:discord:test",
                    "feedback_timeout_seconds": 86400,
                    "sweep_interval_seconds": 60,
                    "research_review": {
                        "candidate_staging_path": str(
                            state / "candidate-staging.sqlite"
                        ),
                        "transcript_review_directory": str(
                            state / "review-inbox"
                        ),
                        "reconcile_interval_seconds": 60,
                    },
                }},
            }), encoding="utf-8")
            result = bootstrap(state, config)
            principals = load_principals(result["token_config"])
            review = principals["research-review-control"]
            self.assertEqual(
                review.operations, RESEARCH_REVIEW_CONTROL_OPERATIONS
            )
            self.assertEqual(review.actor_ref, "bridge:tailscale-review")
            dashboard = principals["dashboard-control"]
            self.assertEqual(
                dashboard.operations, DASHBOARD_CONTROL_OPERATIONS
            )
            self.assertEqual(
                dashboard.actor_ref, "bridge:tailscale-dashboard"
            )
            self.assertNotIn("human-governance", principals)

            token_config = Path(result["token_config"])
            legacy = json.loads(token_config.read_text(encoding="utf-8"))
            for entry in legacy["principals"]:
                if entry["principal_id"] == "dashboard-control":
                    dashboard_token = entry["token"]
                    entry["operations"] = [
                        "list_agenda_feedback_targets",
                        "record_agenda_feedback",
                    ]
            token_config.write_text(
                json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WriterServerError):
                load_principals(token_config)

            bootstrap(state, config)
            migrated = load_principals(token_config)["dashboard-control"]
            self.assertEqual(migrated.token, dashboard_token)
            self.assertEqual(migrated.operations, DASHBOARD_CONTROL_OPERATIONS)

            unauthorized = json.loads(token_config.read_text(encoding="utf-8"))
            for entry in unauthorized["principals"]:
                if entry["principal_id"] == "dashboard-control":
                    entry["operations"].append("commit")
            token_config.write_text(
                json.dumps(unauthorized, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WriterServerError):
                bootstrap(state, config)

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
