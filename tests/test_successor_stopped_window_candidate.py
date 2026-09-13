from __future__ import annotations

import io
import json
import tempfile
import unittest
import subprocess
import os
import sys
import plistlib
import zipfile
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import execute_successor_stopped_window_candidate as execute
from scripts import finalize_successor_health_candidate as final
from scripts import observe_successor_health_candidate as health
from scripts.prepare_successor_config_transition import (
    DOCUMENT_CONFIG, EXTERNAL_CAS_SCHEMA_VERSION, LANE_CONFIG,
    MODEL_ADDITIONS, MODEL_REPLACEMENT, PRESERVE_SCHEMA_VERSION,
    PURE_PRESERVE_SCHEMA_VERSION,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class SuccessorStoppedWindowCandidateTests(unittest.TestCase):
    def test_runtime_commands_drop_ops_python_environment(self) -> None:
        worker = execute.SuccessorOrchestrator(Path("/packet"), io.StringIO())
        script = (
            "import json,os; print(json.dumps({"
            "'pythonpath':os.environ.get('PYTHONPATH'),"
            "'pythonhome':os.environ.get('PYTHONHOME'),"
            "'marker':os.environ.get('DALTON_STARTUP_TIMEOUT_SECONDS')}))"
        )
        inherited = {
            **os.environ,
            "PYTHONPATH": "/separately-frozen-ops:/separately-frozen-ops/src",
            "PYTHONHOME": "/wrong-python-home",
            "DALTON_STARTUP_TIMEOUT_SECONDS": "900",
        }
        with patch.dict(os.environ, inherited, clear=True):
            result = worker.command([sys.executable, "-c", script])
        observed = json.loads(result.stdout)
        self.assertIsNone(observed["pythonpath"])
        self.assertIsNone(observed["pythonhome"])
        self.assertEqual("900", observed["marker"])

    def test_explicit_installer_environment_is_sanitized_too(self) -> None:
        worker = execute.SuccessorOrchestrator(Path("/packet"), io.StringIO())
        captured = {}

        def parent_command(_self, argv, *, timeout=None, env=None, check=True):
            captured.update(env)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(execute.r11.Orchestrator, "command", parent_command):
            worker.command(
                ["/bin/zsh", "/candidate/deploy/macos/install.sh"],
                env={"PYTHONPATH": "/ops/src", "PYTHONHOME": "/ops/python",
                     "DALTON_STARTUP_TIMEOUT_SECONDS": "900",
                     "DRAIN_TIMEOUT": "600"},
            )
        self.assertNotIn("PYTHONPATH", captured)
        self.assertNotIn("PYTHONHOME", captured)
        self.assertEqual("900", captured["DALTON_STARTUP_TIMEOUT_SECONDS"])
        self.assertEqual("600", captured["DRAIN_TIMEOUT"])

    def test_real_installer_discovery_boundary_does_not_seed_present_host_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = root / "host-workspace"
            (host / "wiki").mkdir(parents=True)
            (host / "wiki/vectors.db").write_bytes(b"index")
            state = root / "state"
            governance = state / "connector-governance"
            feeds = state / "feeds"
            hidden = root / "rollback/preserved-absent-openclaw-workspace"
            probe = root / "wiki-install-probe.zsh"
            # This is the install.sh discovery and three-create condition,
            # executed by a real zsh child under the executor environment.
            probe.write_text(
                "set -e\n"
                "openclaw_workspace=${DALTON_OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}\n"
                "wiki_index_source=$openclaw_workspace/wiki/vectors.db\n"
                "[[ -e $wiki_index_source ]] || wiki_index_source=$openclaw_workspace/wiki-index.sqlite\n"
                "if [[ -d $openclaw_workspace && -e $wiki_index_source ]]; then\n"
                " mkdir -p $GOVERNANCE_DIR $FEEDS_DIR\n"
                " print '{}' >$GOVERNANCE_DIR/company-wiki-list-documents-v1.json\n"
                " print '{}' >$GOVERNANCE_DIR/company-wiki-get-document-v1.json\n"
                " ln -s $openclaw_workspace $FEEDS_DIR/company-wiki\n"
                "fi\n")
            worker = execute.SuccessorOrchestrator(root / "packet", io.StringIO())
            result = worker.command(
                ["/bin/zsh", str(probe)],
                env={**os.environ, "HOME": str(root),
                     "PYTHONPATH": "/separately-frozen-ops/src",
                     "PYTHONHOME": "/wrong-python-home",
                     "DALTON_OPENCLAW_WORKSPACE": str(hidden),
                     "GOVERNANCE_DIR": str(governance),
                     "FEEDS_DIR": str(feeds)},
            )
            self.assertEqual(0, result.returncode)
            self.assertTrue((host / "wiki/vectors.db").is_file())
            self.assertFalse(hidden.exists())
            self.assertFalse(governance.exists())
            self.assertFalse(feeds.exists())

    def test_schema_v03_preserves_noncanonical_service_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary)
            raw = b'{"backup":{"keep_latest":3},"owner": {"signature":"x"}}\n'
            artifact = packet / "service.before.json"
            artifact.write_bytes(raw)
            transition = {
                "schema_version": EXTERNAL_CAS_SCHEMA_VERSION,
                "transition_kind": "preserve_existing",
                "service_transition": {
                    "kind": "preserve_exact", "mutation_count": 0,
                    "before": {"file": artifact.name,
                               "sha256": execute.sha(artifact)},
                    "after_sha256": execute.sha(artifact),
                },
            }
            self.assertNotEqual(
                execute._json_bytes(json.loads(raw)), raw)
            self.assertEqual(
                raw, execute.expected_preserved_service_bytes(packet, transition))

    def test_schema_v04_preserves_noncanonical_service_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary)
            raw = b'{"backup":{"keep_latest":3},"owner": {"signature":"x"}}\n'
            artifact = packet / "service.before.json"
            artifact.write_bytes(raw)
            transition = {
                "schema_version": PURE_PRESERVE_SCHEMA_VERSION,
                "transition_kind": "preserve_existing",
                "service_transition": {
                    "kind": "preserve_exact", "mutation_count": 0,
                    "before": {"file": artifact.name,
                               "sha256": execute.sha(artifact)},
                    "after_sha256": execute.sha(artifact),
                },
            }
            self.assertEqual(
                raw, execute.expected_preserved_service_bytes(packet, transition))

    def test_template_is_inert_and_does_not_guess_release_identity(self) -> None:
        candidate = execute.template()
        self.assertEqual("inputs_incomplete", candidate["status"])
        self.assertEqual("pending", candidate["acceptance_state"])
        self.assertIsNone(candidate["source"]["commit"])
        self.assertIsNone(candidate["runtime"]["model_config_count_before"])
        self.assertIsNone(candidate["runtime"]["model_config_count_after"])
        self.assertEqual(execute.ARTIFACT_NAMES, set(candidate["artifacts"]))
        self.assertTrue(all(row == {"file": None, "sha256": None}
                            for row in candidate["artifacts"].values()))
        self.assertFalse(candidate["boundaries"]["manifest_publication"])

    def test_recovery_schema_alone_extends_closed_artifact_inventory(self) -> None:
        legacy = execute.template()
        recovery = execute.recovery_template()
        added = {execute.RECOVERY_PROOF_ARTIFACT,
                 *execute.RECOVERY_ARTIFACT_MAP.values()}
        self.assertEqual(execute.RECOVERY_SCHEMA_VERSION,
                         recovery["schema_version"])
        self.assertEqual(set(legacy["artifacts"]) | added,
                         set(recovery["artifacts"]))
        self.assertNotIn("predecessor_recovery", legacy)
        self.assertIsNone(recovery["predecessor_recovery"])

    def test_recovery_chain_rebuild_uses_exact_artifact_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {}
            for name in (execute.RECOVERY_PROOF_ARTIFACT,
                         *execute.RECOVERY_ARTIFACT_MAP.values()):
                path = root / name
                write_json(path, {"name": name})
                paths[name] = path
            identity = {"schema_version": "successor-predecessor-recovery-0.1"}
            module = SimpleNamespace(validate_recovery_proof=(
                lambda proof, *, artifacts: identity
                if proof == {"name": execute.RECOVERY_PROOF_ARTIFACT}
                and set(artifacts) == set(execute.RECOVERY_ARTIFACT_MAP)
                else None))
            with patch.dict("sys.modules", {
                "scripts.successor_predecessor_recovery": module}):
                proof, actual = execute.validate_predecessor_recovery(
                    paths, expected=identity)
            self.assertEqual({"name": execute.RECOVERY_PROOF_ARTIFACT}, proof)
            self.assertEqual(identity, actual)

    def test_recovery_packet_rejects_unbound_normalized_identity(self) -> None:
        for invalid in (None, {}, []):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                packet = Path(temporary)
                manifest = execute.recovery_template()
                manifest["predecessor_recovery"] = invalid
                manifest["content_hash"] = execute.canonical_hash({
                    key: value for key, value in manifest.items()
                    if key != "content_hash"
                })
                write_json(packet / "release-manifest.candidate.json", manifest)
                with self.assertRaisesRegex(
                        execute.SuccessorExecuteError,
                        "predecessor recovery identity is unresolved"):
                    execute.packet_preflight(packet)

    def test_recovery_rehearsal_requires_exact_writer_preservation(self) -> None:
        digest = "a" * 64
        recovery = {"recovery": {"installed_identity": {
            "writer_tokens": {"live_sha256": digest}}}}
        transition = {"schema_version": PURE_PRESERVE_SCHEMA_VERSION}
        preservation = {
            "before_sha256": digest, "after_sha256": digest,
            "before_mode": 0o600, "after_mode": 0o600,
        }
        execute.validate_recovery_writer_preservation(
            transition, {"results": {"writer_token_preservation": preservation}},
            recovery)
        for changed in (
                {**preservation, "after_sha256": "b" * 64},
                {**preservation, "after_mode": 0o644},
                {**preservation, "extra": True}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    execute.SuccessorExecuteError, "exact writer preservation"):
                execute.validate_recovery_writer_preservation(
                    transition,
                    {"results": {"writer_token_preservation": changed}}, recovery)
        with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                    "exact writer preservation"):
            execute.validate_recovery_writer_preservation(
                {"schema_version": execute.WRITER_APPEND_SCHEMA_VERSION},
                {"results": {"writer_token_preservation": preservation}}, recovery)

    def test_real_verify_successor_binds_pure_preserve_plugin_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            launch_agents = root / "agents"; launch_agents.mkdir()
            venv = root / "venv"
            package = venv / "lib/python3.14/site-packages/dalton_core"
            package.mkdir(parents=True)
            package.joinpath("fixture.py").write_text("VALUE = 1\n")
            service = root / "service.json"
            service.write_text('{"thesis_impact":{"enabled":false}}\n')
            openclaw = root / "openclaw.json"; openclaw.write_text("{}\n")
            document = state / DOCUMENT_CONFIG; write_json(document, {"version": 1})
            lane = state / LANE_CONFIG; write_json(lane, {"version": 1})
            writer_plist = launch_agents / "space.lumos.dalton.writer.plist"
            writer_plist.write_bytes(plistlib.dumps({
                "ProgramArguments": ["writer", "--mission-document-research-lane", str(lane)]}))
            wheel = packet / "runtime.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("dalton_core/fixture.py", "VALUE = 1\n")
            transition_path = packet / "transition.json"
            write_json(transition_path, {
                "schema_version": PURE_PRESERVE_SCHEMA_VERSION, "targets": []})
            rollback = root / "rollback"; rollback.mkdir()
            write_json(rollback / "successor-config-transition-receipt.json",
                       {"model_config_byte_sha256": {}})
            write_json(rollback / "initial-state.json",
                       {"protected_state_sha256": "protected",
                        "reviewed_rendered_plist_sha256": {}})
            selected = packet / "selected.json"; selected.write_text("{}\n")
            web = packet / "web.json"
            write_json(web, {"selected_plan": {
                "path": str(selected), "file_sha256": execute.sha(selected)}})
            alpha = packet / "alpha.json"; write_json(alpha, {"owned_targets": {}})
            service_snapshot = packet / "service.snapshot.json"
            service_snapshot.write_bytes(service.read_bytes())
            openclaw_snapshot = packet / "openclaw.snapshot.json"
            openclaw_snapshot.write_bytes(openclaw.read_bytes())
            placeholder = packet / "placeholder.json"; placeholder.write_text("{}\n")
            artifacts = {
                "transition_manifest": transition_path, "wheel": wheel,
                "service_config_snapshot": service_snapshot,
                "openclaw_config_snapshot": openclaw_snapshot,
                "provider_plugin_snapshot": placeholder,
                "web_v6_activation_receipt": web,
                "alpha_v3_activation_receipt": alpha,
                "model_config_after_snapshot": placeholder,
            }
            broker_hash = "b" * 64
            recovery = {"external_dependency": {"authority": {
                "broker_tree_sha256": broker_hash}}}
            manifest = {"schema_version": execute.RECOVERY_SCHEMA_VERSION,
                        "source": {"root": str(root), "commit": "a" * 40},
                        "predecessor_recovery": recovery}
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.rollback_root = rollback; worker.initially_loaded = []
            worker.loaded = lambda _label: False
            healthy = subprocess.CompletedProcess(
                [], 0, json.dumps({"ok": True}), "")
            worker.command = lambda *_args, **_kwargs: healthy
            patches = (
                patch.object(execute.r11, "STATE", state),
                patch.object(execute.r11, "SERVICE_CONFIG", service),
                patch.object(execute.r11, "OPENCLAW", openclaw),
                patch.object(execute.r11, "LAUNCH_AGENTS", launch_agents),
                patch.object(execute.r11, "VENV", venv),
                patch.object(execute.r11, "LABELS", []),
                patch.object(execute.r11, "current_models", return_value={}),
                patch.object(execute.r11, "protected_state_hash", return_value="protected"),
                patch.object(execute.r11, "verify_provider_plugin", return_value=broker_hash),
                patch.object(execute.r11, "verify_runtime_authorities", return_value={"ok": True}),
                patch.object(execute, "expected_transition_state",
                             return_value=({}, {"version": 1}, {"version": 1})),
                patch.object(execute, "verify_preserved_state_authorities", return_value={}),
                patch.object(execute, "expected_service_transition_state",
                             return_value=(json.loads(service.read_text()), json.loads(service.read_text()))),
                patch.object(execute, "expected_preserved_service_bytes", return_value=service.read_bytes()),
                patch.object(execute, "expected_preserved_openclaw_state", return_value=openclaw.read_bytes()),
                patch.object(execute, "validate_predecessor_recovery", return_value=({}, recovery)),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                 patches[5], patches[6], patches[7], patches[8], patches[9], \
                 patches[10], patches[11], patches[12], patches[13], patches[14], \
                 patches[15]:
                result = worker.verify_successor(manifest, artifacts)
                self.assertEqual(recovery, result["predecessor_recovery"])
                execute.r11.verify_provider_plugin.return_value = "c" * 64
                with self.assertRaisesRegex(
                        execute.SuccessorExecuteError,
                        "recovered external broker tree changed"):
                    worker.verify_successor(manifest, artifacts)

    def test_recovered_inventory_uses_historical_receipt_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                    "document-research-config.json",
                    "mission-document-research-lane.json",
                    "model-catalog-sync.json", "writer-tokens.json", "owned.json"):
                (root / name).write_text("{}\n", encoding="utf-8")
            governance = root / "governance-decisions"
            governance.mkdir()
            (governance / "decision.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                {root / "model-catalog-sync.json", root / "owned.json",
                 governance, governance / "decision.json"},
                execute.recovered_protected_entries(root))

    def test_unaccepted_or_incomplete_packet_cannot_reach_live_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary)
            manifest = execute.template()
            manifest["content_hash"] = execute.canonical_hash(manifest)
            write_json(packet / "release-manifest.candidate.json", manifest)
            with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                        "candidate manifest differs"):
                execute.packet_preflight(packet)

    def test_rollback_restores_owned_targets_and_preserves_one_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()

            before = {"route": "before"}
            after = {"route": "after", "structured_output_repair": {"max_attempts": 1}}
            rows = []
            for name in (*MODEL_ADDITIONS, DOCUMENT_CONFIG, LANE_CONFIG):
                artifact = packet / (name + ".after")
                value = ({"schema_version": "0.1", "enabled": True}
                         if name == LANE_CONFIG else {"name": name})
                write_json(artifact, value)
                rows.append({"name": name, "kind": "exclusive_add",
                             "after": {"file": artifact.name,
                                       "sha256": execute.sha(artifact)}})
                write_json(state / name, value)
            before_path = packet / "initial.before.json"
            after_path = packet / "initial.after.json"
            write_json(before_path, before); write_json(after_path, after)
            rows.insert(2, {"name": MODEL_REPLACEMENT,
                            "kind": "compare_and_replace",
                            "before": {"file": before_path.name,
                                       "sha256": execute.sha(before_path)},
                            "after": {"file": after_path.name,
                                      "sha256": execute.sha(after_path)}})
            write_json(state / MODEL_REPLACEMENT, after)
            write_json(rollback_state / MODEL_REPLACEMENT, before)
            write_json(state / "owner.json", {"kept": True})
            write_json(rollback_state / "owner.json", {"kept": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": rows})
            write_json(rollback / "initial-state.json", {
                "protected_state_sha256": "old",
            })

            # A concurrent owner replaces only one successor addition.  The
            # rollback must preserve it while restoring every other owned byte.
            conflict = state / MODEL_ADDITIONS[0]
            write_json(conflict, {"concurrent": True})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback",
                              return_value={"status": "rolled_back_healthy"}) as parent:
                with patch.object(execute.r11, "STATE", state):
                    result = worker.rollback()

            parent.assert_called_once_with()
            self.assertEqual([MODEL_ADDITIONS[0]],
                             result["preserved_concurrent_config_targets"])
            self.assertEqual({"concurrent": True}, json.loads(conflict.read_text()))
            self.assertEqual(before, json.loads((state / MODEL_REPLACEMENT).read_text()))
            self.assertFalse((state / MODEL_ADDITIONS[1]).exists())
            self.assertFalse((state / DOCUMENT_CONFIG).exists())
            self.assertFalse((state / LANE_CONFIG).exists())
            initial = json.loads((rollback / "initial-state.json").read_text())
            self.assertEqual(execute.r11.protected_state_hash(state),
                             initial["protected_state_sha256"])

    def test_rollback_refuses_unrelated_owner_drift_before_parent_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            rollback_state = rollback / "state-files"; rollback_state.mkdir()
            artifact = packet / "draft.after.json"; write_json(artifact, {"draft": True})
            transition = packet / "transition.json"
            write_json(transition, {"targets": [{
                "name": MODEL_ADDITIONS[0], "kind": "exclusive_add",
                "after": {"file": artifact.name, "sha256": execute.sha(artifact)},
            }]})
            write_json(state / MODEL_ADDITIONS[0], {"concurrent": True})
            write_json(state / "owner.json", {"owner": "changed"})
            write_json(rollback_state / "owner.json", {"owner": "before"})
            write_json(rollback / "initial-state.json", {"protected_state_sha256": "old"})
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {"transition_manifest": transition}
            with patch.object(execute.r11.Orchestrator, "rollback") as parent:
                with patch.object(execute.r11, "STATE", state):
                    with self.assertRaisesRegex(execute.SuccessorExecuteError,
                                                "outside successor"):
                        worker.rollback()
            parent.assert_not_called()
            self.assertEqual({"concurrent": True},
                             json.loads((state / MODEL_ADDITIONS[0]).read_text()))

    def test_preserve_existing_rollback_leaves_configs_and_restores_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packet = root / "packet"; packet.mkdir()
            state = root / "state"; state.mkdir()
            rollback = root / "rollback"; rollback.mkdir()
            transition = packet / "transition.json"
            write_json(transition, {
                "schema_version": PRESERVE_SCHEMA_VERSION,
                "targets": [{"name": name, "kind": "preserve_existing"}
                            for name in (*MODEL_ADDITIONS, MODEL_REPLACEMENT,
                                         DOCUMENT_CONFIG, LANE_CONFIG)],
            })
            for name in (*MODEL_ADDITIONS, MODEL_REPLACEMENT,
                         DOCUMENT_CONFIG, LANE_CONFIG):
                write_json(state / name, {"owner": name})
            config = root / "service.json"
            before = root / "service.before.json"
            after = root / "service.after.json"
            write_json(before, {"bounded_planner": {"config": {}}})
            write_json(after, {"bounded_planner": {"config": {
                "planner_call_budget": {"max_cost_usd": 3.0}}}})
            config.write_bytes(after.read_bytes())
            exact_configs = {path.name: path.read_bytes() for path in state.iterdir()}
            worker = execute.SuccessorOrchestrator(packet, io.StringIO())
            worker.stopped = worker.mutations_started = True
            worker.rollback_root = rollback
            worker.artifacts = {
                "transition_manifest": transition,
                "service_config_before": before,
                "service_config_after": after,
            }

            def parent_rollback(_worker):
                self.assertEqual(after.read_bytes(), config.read_bytes())
                config.write_bytes(before.read_bytes())
                return {"status": "rolled_back_healthy"}

            with patch.object(execute.r11.Orchestrator, "rollback",
                              autospec=True, side_effect=parent_rollback) as parent:
                result = worker.rollback()
            parent.assert_called_once_with(worker)
            self.assertEqual(before.read_bytes(), config.read_bytes())
            self.assertEqual(exact_configs,
                             {path.name: path.read_bytes() for path in state.iterdir()})
            self.assertEqual([], result["preserved_concurrent_config_targets"])

    def test_health_observation_uses_manifest_bound_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); packet = root / "packet"; packet.mkdir()
            manifest_path = packet / "release-manifest.candidate.json"
            manifest = {"source": {"commit": "a" * 40},
                        "health_acceptance": {"sample_count": 3,
                                              "interval_seconds": 5,
                                              "minimum_duration_seconds": 10}}
            write_json(manifest_path, manifest)
            started = datetime.now(timezone.utc) - timedelta(seconds=2)
            controller_started = (started + timedelta(seconds=1)).isoformat()
            deployment_path = packet / "deployment.json"
            write_json(deployment_path, {
                "schema_version": "successor-stopped-window-execution-0.1",
                "source_commit": "a" * 40,
                "status": "installer_finished_runtime_health_pending", "exit_code": 0,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "started_at": started.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            clock = [0.0]
            def sleep(seconds): clock[0] += seconds
            def invoke(*_args, **_kwargs):
                wire = {"ok": True,
                        "checks": {"writer": True, "heartbeat_age_seconds": .1},
                        "heartbeat": {"pid": 42, "started_at": controller_started,
                                      "last_tick_at": controller_started}}
                return subprocess.CompletedProcess([], 0, json.dumps(wire), "")
            with patch.object(health, "packet_preflight", return_value=(manifest, {})):
                result = health.observe(
                    packet, deployment_path, root / "health", ["health"],
                    invoke=invoke, sleep=sleep, clock=lambda: clock[0])
            self.assertTrue(result["accepted"])
            self.assertEqual(["0000.json", "0001.json", "0002.json"],
                             [row["raw_file"] for row in result["samples"]])

    def test_finalizer_rechecks_runtime_and_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); packet = root / "packet"; packet.mkdir()
            source = root / "source"; source.mkdir()
            manifest_path = packet / "release-manifest.candidate.json"
            policy = {"sample_count": 3, "interval_seconds": 5,
                      "minimum_duration_seconds": 10}
            manifest = {
                "source": {"root": str(source), "commit": "b" * 40},
                "acceptance": {"wheel_sha256": "c" * 64},
                "runtime": {"model_config_count_after": 17},
                "health_acceptance": policy,
            }
            write_json(manifest_path, manifest)
            installed_path = packet / "installed.json"
            exact = {"model_config_count": 17, "runtime_files": 9,
                     "authority": {"mission": "exact"},
                     "writer_lane_enabled": True, "thesis_impact_enabled": False,
                     "backup_keep_latest": 3,
                     "writer_token_mutations": 1,
                     "writer_operation_transition": {
                         "before_sha256": "d" * 64, "after_sha256": "e" * 64,
                         "predecessor_commit": "a" * 40, "successor_commit": "b" * 40,
                         "added_operations": ["settle_company_model_spec"]}}
            write_json(installed_path, {
                "schema_version": "successor-installed-verification-0.1",
                "status": "installed_bytes_verified_runtime_pending",
                "source_commit": "b" * 40,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "wheel_sha256": "c" * 64, **exact,
            })
            deployed_start = datetime(2026, 9, 11, tzinfo=timezone.utc)
            deployed_finish = deployed_start + timedelta(seconds=2)
            rollback = packet / "rollback"; rollback.mkdir()
            write_json(rollback / "initial-state.json", {"loaded": ["writer"]})
            deployment_path = packet / "deployment.json"
            write_json(deployment_path, {
                "schema_version": "successor-stopped-window-execution-0.1",
                "source_commit": "b" * 40,
                "status": "installer_finished_runtime_health_pending", "exit_code": 0,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "installed_verification": installed_path.name,
                "installed_verification_sha256": execute.sha(installed_path),
                "started_at": deployed_start.isoformat(),
                "finished_at": deployed_finish.isoformat(),
                "fresh_rollback_snapshot": {"path": str(rollback)},
            })
            health_dir = packet / "health"; health_dir.mkdir()
            samples = []
            controller_started = (deployed_start + timedelta(seconds=1)).isoformat()
            for index in range(3):
                at = (deployed_finish + timedelta(seconds=index * 5)).isoformat()
                wire = {"ok": True,
                        "checks": {"writer": True, "heartbeat_age_seconds": .1},
                        "heartbeat": {"pid": 42, "started_at": controller_started}}
                raw = health_dir / f"{index:04d}.json"
                write_json(raw, {"at": at, "exit_code": 0,
                                 "stdout": json.dumps(wire), "stderr": ""})
                samples.append({"at": at, "exit_code": 0, "ok": True,
                                "checks": wire["checks"], "pid": 42,
                                "started_at": controller_started,
                                "raw_file": raw.name, "raw_sha256": execute.sha(raw)})
            summary_path = health_dir / "summary.json"
            write_json(summary_path, {
                "schema_version": "successor-health-observation-0.1", "status": "passed",
                "source_commit": "b" * 40,
                "candidate_manifest_sha256": execute.sha(manifest_path),
                "deployment_receipt_sha256": execute.sha(deployment_path),
                "health_acceptance": policy, "sample_count": 3, "elapsed_seconds": 10,
                "all_healthy": True, "same_controller": True,
                "postdeployment_controller": True, "observed_long_enough": True,
                "accepted": True, "samples": samples,
            })
            transition_path = packet / "transition.json"
            transition = {"schema_version": execute.WRITER_APPEND_SCHEMA_VERSION}
            write_json(transition_path, transition)
            artifacts = {"transition_manifest": transition_path}
            class FakeWorker(execute.SuccessorOrchestrator):
                def verify_successor(self, _manifest, _artifacts):
                    # Exercise the actual fresh-worker proof entry point;
                    # finalization must bind both artifacts and frozen source.
                    self._writer_projection()
                    return exact
            with patch.object(final, "packet_preflight", return_value=(manifest, artifacts)), \
                 patch.object(final, "SuccessorOrchestrator", FakeWorker), \
                 patch.object(execute, "expected_writer_operation_transition_state",
                              return_value=(b"before", b"after", {})) as projection, \
                 patch.object(final.subprocess, "check_output",
                              side_effect=["b" * 40 + "\n", ""]):
                result = final.finalize(
                    packet, deployment_path, summary_path, installed_path,
                    packet / "runtime-verification.json", packet / "post.log")
            projection.assert_called_once_with(
                packet_root=packet, manifest=transition, successor_root=source)
            self.assertEqual("passed_pending_publication", result["status"])
            self.assertFalse(result["manifest_publication"])
            self.assertFalse((packet / "current-release.json").exists())
            # A re-bound installed receipt cannot substitute different writer
            # proof for the freshly reverified source and rollback identity.
            mismatched = json.loads(installed_path.read_text())
            mismatched["writer_operation_transition"]["after_sha256"] = "f" * 64
            write_json(installed_path, mismatched)
            deployment = json.loads(deployment_path.read_text())
            deployment["installed_verification_sha256"] = execute.sha(installed_path)
            write_json(deployment_path, deployment)
            summary = json.loads(summary_path.read_text())
            summary["deployment_receipt_sha256"] = execute.sha(deployment_path)
            write_json(summary_path, summary)
            with patch.object(final, "packet_preflight", return_value=(manifest, artifacts)), \
                 patch.object(final, "SuccessorOrchestrator", FakeWorker), \
                 patch.object(execute, "expected_writer_operation_transition_state",
                              return_value=(b"before", b"after", {})), \
                 patch.object(final.subprocess, "check_output",
                              side_effect=["b" * 40 + "\n", ""]):
                with self.assertRaisesRegex(final.SuccessorFinalizeError, "writer transition differs"):
                    final.finalize(packet, deployment_path, summary_path, installed_path,
                                   packet / "refused-verification.json", packet / "refused-post.log")
            self.assertFalse((packet / "refused-verification.json").exists())


if __name__ == "__main__":
    unittest.main()
