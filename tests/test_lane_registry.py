"""One lane registration has to reach all three places, or it reaches none.

The point of the registry is that adding a lane is one line.  That claim is
only true if the writer's operation tables, the controller tick and the writer
LaunchAgent all read the same record, so the test that matters registers one
made-up lane and then asks all three whether they can see it.

The second half of the file pins what the old literals said.  Those sets were
hand-maintained for nine lanes across four files; deriving them is only an
improvement if a derivation that quietly drops one fails here.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from dalton_core import lane_registry, writer_server
from dalton_core.bounded_planner_driver import (
    BoundedPlannerDriver,
    BoundedPlannerDriverConfig,
)
from dalton_core.lane_registry import (
    LANE_MODULES,
    RESERVED_DRIVER_KEYS,
    LaneRegistryError,
    LaneSpec,
    LaunchAgentContext,
    lane_argv,
    lane_for_operation,
    register_lane,
    registered_lanes,
    tick_lanes,
    unregister_lane,
)
from dalton_core.macos_launchagent import render


# What the literals said before the registry derived them.  These are the
# expected values, spelled out, so a lane that falls out of a set is visible as
# a diff here rather than as a lane that silently stops being dispatched.
#
# This is a *migration* check, and the assertions below are containment rather
# than equality for that reason: they pin that every lane the writer used to
# spell out is still registered, still core-discoverable, still in the same
# relative tick order and still arriving on the same keyword.  A lane added
# after P14-0 -- the first was P11a's market prices -- is not supposed to
# appear here; it is pinned in its own module's tests, and the registry's own
# duplicate, order and reserved-key rules are what stop it colliding.
LANE_OPERATIONS = frozenset({
    "dispatch_mission_source_discovery",
    "dispatch_document_extraction",
    "dispatch_mission_stage",
    "dispatch_claim_review",
    "dispatch_mission_sec_quarters",
    "dispatch_mission_statements",
    "dispatch_company_model_spec",
    "dispatch_research_plan",
    "dispatch_initial_screen",
    # S1: the two human / vendor feeds, registered after the migration. They
    # are listed here rather than exempted, so this stays an exact set and a
    # lane that appears without anybody meaning it to still fails.
    "dispatch_sales_notes_feed",
    "dispatch_company_wiki_feed",
})
CORE_DISCOVERY_OPERATIONS = frozenset({
    "dispatch_mission_source_discovery", "mission_source_discovery_status",
    "mission_source_discoveries", "mission_discovered_documents",
    "dispatch_document_extraction",
    "dispatch_mission_stage", "mission_stage_checklist",
    "dispatch_claim_review", "dispatch_initial_screen", "dispatch_research_plan",
    "mission_deliverables",
    "dispatch_mission_sec_quarters", "dispatch_mission_statements",
    "dispatch_company_model_spec",
    "dispatch_sales_notes_feed", "dispatch_company_wiki_feed",
})
# The controller tick's lane order, as run_once ran it before P14-0, plus
# what has been registered since.
TICK_ORDER = (
    ("dispatch_mission_source_discovery", "mission_source_discovery"),
    ("dispatch_document_extraction", "document_extraction"),
    ("dispatch_mission_stage", "mission_stage"),
    ("dispatch_claim_review", "claim_review"),
    ("dispatch_mission_sec_quarters", "mission_sec_quarters"),
    ("dispatch_mission_statements", "mission_statements"),
    ("dispatch_company_model_spec", "company_model_spec"),
    ("dispatch_research_plan", "research_plan"),
    ("dispatch_initial_screen", "initial_screen"),
    ("dispatch_sales_notes_feed", "sales_notes_feed"),
    ("dispatch_company_wiki_feed", "company_wiki_feed"),
)
LANE_PARAM_FIELDS = {
    "dispatch_claim_review": frozenset({"max_claims"}),
}


class FakeLaneLauncher:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.closed = False

    def close(self) -> None:
        self.closed = True


def fake_lane_spec(**overrides) -> LaneSpec:
    def handler(server, params):
        launcher = server.lane_launcher("fake_probe_launcher")
        if launcher is None:
            return {"status": "unconfigured", "reason": "no fake probe on this writer"}
        return {"status": "idle", "marker": launcher.marker, "params": dict(params)}

    def add_arguments(parser):
        parser.add_argument("--fake-probe-marker", default=None)

    def build_launcher(args):
        if args.fake_probe_marker is None:
            return None
        return FakeLaneLauncher(args.fake_probe_marker)

    def argv_fragment(context):
        marker = context.state / "fake-probe.json"
        return [] if not marker.is_file() else ["--fake-probe-marker", str(marker)]

    values = {
        "operation": "dispatch_fake_probe",
        "order": 10_000,
        "driver_key": "fake_probe",
        "param_fields": frozenset({"depth"}),
        "handler": handler,
        "init_kwarg": "fake_probe_launcher",
        "argparse": add_arguments,
        "launcher_factory": build_launcher,
        "argv_fragment": argv_fragment,
    }
    values.update(overrides)
    return LaneSpec(**values)


@contextlib.contextmanager
def registered(spec: LaneSpec):
    """Register a lane for the body of a test and take it away afterwards."""

    register_lane(spec)
    writer_server.install_lane_operations()
    try:
        yield spec
    finally:
        unregister_lane(spec.operation)
        writer_server.install_lane_operations()


class RecordingClient:
    """A writer client that answers every call and remembers the order."""

    def __init__(self, answers=None):
        self.calls: list[str] = []
        self.answers = dict(answers or {})

    def call(self, operation, params=None, *, request_id=None):
        self.calls.append(operation)
        if operation == "bounded_planner_active_loops":
            return {"loops": []}
        if operation in self.answers:
            value = self.answers[operation]
            if isinstance(value, Exception):
                raise value
            return value
        return {"status": "idle", "operation": operation}


def driver_config(root: Path) -> BoundedPlannerDriverConfig:
    return BoundedPlannerDriverConfig(
        writer_socket=root / "writer.sock",
        token_config=root / "tokens.json",
        scheduler_db=root / "scheduler.sqlite",
        user_agent="Dalton Test",
        max_response_bytes=1_000_000,
        timeout_seconds=10.0,
        max_probes_per_tick=1,
        filed_window_days=400,
        observation_mandate_version_ref=None,
        doctrine_pack_version_ref=None,
        doctrine_pack_version_hash=None,
        planner_routing_policy_ref=None,
        planner_credential_slot_refs=None,
        planner_model_router_db=None,
        planner_broker_socket=None,
        planner_broker_auth_key=None,
        planner_broker_client_id="client:dalton-core",
        planner_expected_agent_id="chem",
        planner_max_cost_usd=0.5,
    )


class LaneRegistrationReachesEveryConsumerTests(unittest.TestCase):
    def test_one_registration_reaches_writer_driver_and_launchagent(self) -> None:
        spec = fake_lane_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with registered(spec):
                # 1. The writer knows the operation, who may call it and what
                #    parameters it takes.
                self.assertIn("dispatch_fake_probe", writer_server.CORE_OPERATIONS)
                self.assertIn(
                    "dispatch_fake_probe", writer_server.CORE_DISCOVERY_OPERATIONS
                )
                self.assertEqual(
                    writer_server.OPERATION_FIELDS["dispatch_fake_probe"],
                    frozenset({"depth"}),
                )

                # 2. The controller tick calls it, last, because its order says so.
                client = RecordingClient()
                driver = BoundedPlannerDriver(
                    driver_config(root), client=client, transport=object(),
                )
                result = driver.run_once()
                self.assertEqual(client.calls[-2], "dispatch_fake_probe")
                self.assertEqual(
                    result["fake_probe"],
                    {"status": "idle", "operation": "dispatch_fake_probe"},
                )

                # 3. The writer LaunchAgent passes its arguments, once the
                #    thing the lane needs is on disk.
                state = root / "state"
                state.mkdir(parents=True, exist_ok=True)
                self.assertEqual(lane_argv(LaunchAgentContext(state=state)), [])
                (state / "fake-probe.json").write_text("{}", encoding="utf-8")
                writer = plistlib.loads(Path(render(
                    root / "LaunchAgents", root / "venv" / "bin", state,
                    root / "config.json", root / "logs",
                )["writer"]).read_bytes())
                self.assertIn("--fake-probe-marker", writer["ProgramArguments"])

                # 4. The writer builds its launcher from its own arguments and
                #    dispatches to its own handler.
                parser = argparse.ArgumentParser()
                lane_registry.add_lane_arguments(parser)
                args = parser.parse_args(
                    ["--fake-probe-marker", str(state / "fake-probe.json")]
                )
                self.assertIsInstance(
                    spec.launcher_factory(args), FakeLaneLauncher
                )

        # Taken away again, none of the three has heard of it.
        self.assertNotIn("dispatch_fake_probe", writer_server.CORE_OPERATIONS)
        self.assertNotIn("dispatch_fake_probe", writer_server.OPERATION_FIELDS)
        self.assertNotIn(
            "dispatch_fake_probe", [spec.operation for spec in registered_lanes()]
        )

    def test_the_writer_closes_every_registered_lane_launcher(self) -> None:
        # close() used to name each launcher; a lane added without a line
        # there leaked its child process handle on shutdown.
        spec = fake_lane_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with registered(spec):
                launcher = FakeLaneLauncher("x")
                server = writer_server.WriterServer(
                    root / "core.sqlite", root / "w.sock",
                    {"core": writer_server.Principal(
                        "core", "t", frozenset({"commit"}), unrestricted=True)},
                    fake_probe_launcher=launcher,
                )
                self.assertIs(server.lane_launcher("fake_probe_launcher"), launcher)
                server.lane_state["fake_probe_launcher"] = object()
                server._close_store()
                self.assertTrue(launcher.closed)
                self.assertEqual(server.lane_state, {})

    def test_an_absent_launcher_is_reported_not_raised(self) -> None:
        # A lane whose launcher is not installed answers the tick truthfully;
        # that string is where an operator reads why a lane did nothing.
        spec = fake_lane_spec()
        with registered(spec):
            class Server:
                def lane_launcher(self, _name):
                    return None

            self.assertEqual(
                spec.handler(Server(), {}),
                {"status": "unconfigured", "reason": "no fake probe on this writer"},
            )


class RegistryRefusalTests(unittest.TestCase):
    def test_a_duplicate_operation_is_refused(self) -> None:
        with registered(fake_lane_spec()):
            with self.assertRaises(LaneRegistryError):
                register_lane(fake_lane_spec(order=10_001, driver_key="other",
                                             init_kwarg="other_launcher"))

    def test_a_duplicate_order_driver_key_or_kwarg_is_refused(self) -> None:
        with registered(fake_lane_spec()):
            for overrides in (
                {"operation": "dispatch_fake_two", "driver_key": "two",
                 "init_kwarg": "two_launcher"},                      # same order
                {"operation": "dispatch_fake_two", "order": 10_001,
                 "init_kwarg": "two_launcher"},                      # same driver key
                {"operation": "dispatch_fake_two", "order": 10_001,
                 "driver_key": "two"},                               # same init kwarg
            ):
                with self.assertRaises(LaneRegistryError):
                    register_lane(fake_lane_spec(**overrides))

    def test_a_lane_operation_is_a_dispatch_tick(self) -> None:
        with self.assertRaises(LaneRegistryError):
            LaneSpec(operation="mission_deliverables", order=10_000)

    def test_a_launcher_factory_needs_somewhere_to_arrive(self) -> None:
        with self.assertRaises(LaneRegistryError):
            LaneSpec(
                operation="dispatch_nowhere", order=10_000,
                launcher_factory=lambda args: None,
            )

    def test_a_driver_key_the_tick_summary_owns_is_refused(self) -> None:
        # The lane results are spread last into the tick summary, so a lane
        # claiming one of the summary's own keys would overwrite it and the
        # tick would report a lane's result as its own status.
        for reserved in sorted(RESERVED_DRIVER_KEYS):
            with self.assertRaises(LaneRegistryError):
                register_lane(fake_lane_spec(driver_key=reserved))

    def test_the_reserved_keys_are_the_ticks_own_keys(self) -> None:
        from dalton_core import bounded_planner_driver

        self.assertEqual(
            RESERVED_DRIVER_KEYS, bounded_planner_driver._RESERVED_SUMMARY_KEYS
        )

    def test_a_lane_without_a_handler_or_a_writer_method_is_refused(self) -> None:
        # Worse than a missing lane: it enters CORE_OPERATIONS, bootstrap
        # grants it to the core principal, and then every call raises. The
        # registration is the mistake, so it fails where the registry is read.
        register_lane(LaneSpec(operation="dispatch_unanswerable", order=10_002))
        try:
            with self.assertRaises(LaneRegistryError):
                writer_server.install_lane_operations()
            self.assertNotIn("dispatch_unanswerable", writer_server.OPERATION_FIELDS)
            self.assertNotIn("dispatch_unanswerable", writer_server.CORE_OPERATIONS)
        finally:
            unregister_lane("dispatch_unanswerable")
            writer_server.install_lane_operations()

    def test_deriving_from_a_half_loaded_registry_is_refused(self) -> None:
        # load_lanes() returns silently on re-entry, which is what stops a
        # circular import recursing. The cost would be a writer folding in
        # whatever happened to be registered so far: the lane in the tick and
        # the plist, absent from OPERATION_FIELDS, refused forever.
        lane_registry._LOADING = True
        try:
            with self.assertRaises(LaneRegistryError):
                writer_server.install_lane_operations()
        finally:
            lane_registry._LOADING = False
        writer_server.install_lane_operations()

    def test_a_lane_may_not_shadow_a_literal_writer_operation(self) -> None:
        # ``dispatch_answer_refresh`` is not a lane; it is a writer operation
        # with its own parameter contract, and a lane claiming that name would
        # otherwise silently replace it.
        register_lane(LaneSpec(operation="dispatch_answer_refresh", order=10_000))
        try:
            with self.assertRaises(LaneRegistryError):
                writer_server.install_lane_operations()
        finally:
            unregister_lane("dispatch_answer_refresh")
            writer_server.install_lane_operations()


class MigratedLanesMatchTheOldLiteralsTests(unittest.TestCase):
    def test_every_lane_the_writer_used_to_spell_out_is_registered(self) -> None:
        self.assertLessEqual(
            LANE_OPERATIONS,
            frozenset(spec.operation for spec in registered_lanes()),
        )

    def test_the_writer_operation_sets_are_what_they_were(self) -> None:
        self.assertLessEqual(
            CORE_DISCOVERY_OPERATIONS, writer_server.CORE_DISCOVERY_OPERATIONS
        )
        self.assertTrue(LANE_OPERATIONS <= writer_server.CORE_OPERATIONS)
        for operation in LANE_OPERATIONS:
            self.assertEqual(
                writer_server.OPERATION_FIELDS[operation],
                LANE_PARAM_FIELDS.get(operation, frozenset()),
            )

    def test_the_tick_order_and_keys_are_what_they_were(self) -> None:
        migrated = frozenset(operation for operation, _key in TICK_ORDER)
        self.assertEqual(
            tuple((spec.operation, spec.driver_key) for spec in tick_lanes()
                  if spec.operation in migrated),
            TICK_ORDER,
        )

    def test_every_lane_is_either_handled_here_or_by_the_writer(self) -> None:
        # The same check install_lane_operations() makes at import; kept as a
        # test so the four handler-less lanes are named somewhere a reader
        # will look.
        writer_server.require_lane_handlers(writer_server.WriterServer)
        handled_by_the_writer = {
            spec.operation for spec in registered_lanes() if spec.handler is None
        }
        self.assertEqual(handled_by_the_writer, {
            "dispatch_mission_source_discovery", "dispatch_document_extraction",
            "dispatch_mission_stage", "dispatch_claim_review",
        })

    def test_the_launcher_lanes_name_the_kwargs_the_writer_took(self) -> None:
        # These four keywords were explicit parameters of WriterServer.__init__
        # before P14-0; existing callers still pass them by name. Lanes
        # registered since arrive on keywords that never were.
        self.assertLessEqual(
            {"statement_lane_launcher", "model_spec_launcher",
             "initial_screen_launcher", "research_planner_launcher"},
            {spec.init_kwarg for spec in registered_lanes()
             if spec.init_kwarg is not None},
        )

    def test_an_unknown_launcher_keyword_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(writer_server.WriterServerError):
                writer_server.WriterServer(
                    root / "core.sqlite", root / "w.sock",
                    {"core": writer_server.Principal(
                        "core", "t", frozenset({"commit"}), unrestricted=True)},
                    statment_lane_launcher=object(),
                )

    def test_the_writer_launchagent_still_wires_the_migrated_lanes(self) -> None:
        # P13ak / P13am / P13ad / P13o: exactly the argv the lanes had before
        # they were fragments, and exactly the conditions under which they
        # appeared.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
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
                        "transcript_review_directory": str(root / "review-inbox"),
                        "reconcile_interval_seconds": 60,
                        "document_extraction_model_config_path": str(
                            root / "extraction.json"
                        ),
                    },
                }},
            }), encoding="utf-8")
            state.mkdir(parents=True, exist_ok=True)

            def writer_args() -> list[str]:
                return plistlib.loads(Path(render(
                    root / "LaunchAgents", root / "venv" / "bin", state, config,
                    root / "logs",
                )["writer"]).read_bytes())["ProgramArguments"]

            bare = writer_args()
            for absent in ("--statement-lane-governance", "--model-spec-model-config",
                           "--initial-screen-model-config",
                           "--research-planner-model-config"):
                self.assertNotIn(absent, bare)

            governance = state / "connector-governance"
            governance.mkdir(parents=True, exist_ok=True)
            from dalton_core.sec_financials_core import build_sec_financials_governance_record
            (governance / "sec-financial-statements-v2.json").write_text(json.dumps(
                build_sec_financials_governance_record(
                    approved_by="human:test", status="approved", version=2)),
                encoding="utf-8")
            (state / "initial-screen-model-config.json").write_text(
                "{}", encoding="utf-8")
            (state / "research-planner-model-config.json").write_text(
                "{}", encoding="utf-8")
            wired = writer_args()
            self.assertEqual(
                wired[wired.index("--statement-lane-governance") + 1],
                str((governance / "sec-financial-statements-v2.json").resolve()),
            )
            self.assertIn("--statement-lane-user-agent", wired)
            self.assertEqual(
                wired[wired.index("--model-spec-model-config") + 1],
                str((state / "initial-screen-model-config.json").resolve()),
            )
            self.assertEqual(
                wired[wired.index("--initial-screen-model-config") + 1],
                str((state / "initial-screen-model-config.json").resolve()),
            )
            self.assertEqual(
                wired[wired.index("--research-planner-model-config") + 1],
                str((state / "research-planner-model-config.json").resolve()),
            )

    def test_the_tick_names_a_failing_lane_without_failing_the_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RecordingClient(
                {"dispatch_mission_statements": RuntimeError("boom")}
            )
            driver = BoundedPlannerDriver(
                driver_config(root), client=client, transport=object(),
            )
            result = driver.run_once()
            self.assertEqual(
                result["mission_statements"], {"status": "unavailable:RuntimeError"}
            )
            self.assertEqual(result["initial_screen"]["status"], "idle")
            for _operation, key in TICK_ORDER:
                self.assertIn(key, result)
            self.assertIn("mission_sec_dispatch", result)
            self.assertIn("forecast_reconciliation", result)

    def test_no_lane_module_imports_a_registry_consumer_at_module_level(self) -> None:
        # A lane module may not import, at module level, anything that derives
        # from the registry: writer_server folds the registry in at its own
        # import, so being imported *by* a lane module would fold in a
        # half-built registry.  Their own expensive imports stay inside the
        # handler and the launcher factory, which is what the top-level import
        # list has to show.
        consumers = ("writer_server", "bounded_planner_driver", "macos_launchagent")
        for name in LANE_MODULES:
            module = importlib.import_module(name)
            source = Path(module.__file__).read_text(encoding="utf-8")
            top_level = [
                line for line in source.splitlines()
                if line.startswith(("import ", "from "))
            ]
            for consumer in consumers:
                self.assertFalse(
                    [line for line in top_level if consumer in line],
                    f"{name} imports {consumer} at module level",
                )
            self.assertIn("register_lane", source, name)

    def test_importing_a_lane_module_alone_pulls_in_no_consumer(self) -> None:
        # The check above reads the file; this one runs it.  A transitive
        # import three modules down is exactly the one nobody would spot by
        # reading, and it is the one that produces a lane the tick calls and
        # the writer refuses.
        source_root = Path(lane_registry.__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(source_root))
        for name in LANE_MODULES:
            script = textwrap.dedent(f"""
                import sys
                import importlib
                importlib.import_module({name!r})
                leaked = sorted(
                    module for module in sys.modules
                    if module in (
                        "dalton_core.writer_server",
                        "dalton_core.bounded_planner_driver",
                        "dalton_core.macos_launchagent",
                    )
                )
                print(",".join(leaked))
            """)
            finished = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, env=environment, timeout=120,
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            self.assertEqual(finished.stdout.strip(), "", f"{name} pulled in a consumer")

    def test_every_lane_is_registered_and_findable(self) -> None:
        for spec in registered_lanes():
            self.assertIsNotNone(lane_for_operation(spec.operation))


if __name__ == "__main__":
    unittest.main()
