from __future__ import annotations

import json
import hashlib
import os
import plistlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_release_copied_state_rehearsal import RehearsalBindingError
from scripts.run_successor_copied_state_rehearsal import (
    capture_external_market_digest_preservation, derive_confined_transition,
    replay_preserved_production_setup,
    stage_existing_install_authorities, stage_preserved_runtime_configs,
    validate_external_market_digest_preservation,
    validate_existing_install_authorities, validate_successor_snapshots,
    verify_scratch_bootstrap_writer_append,
    run as run_successor_rehearsal,
)
from scripts.prepare_successor_config_transition import (
    apply_transition_to_scratch, canonical_hash,
    expected_openclaw_frame_transition_state,
)
from tests.test_successor_config_transition import PreserveExistingTransitionTests


class IdentityModule:
    @staticmethod
    def model_config_inventory(state_dir: Path):
        return {path.name: json.loads(path.read_text())
                for path in sorted(state_dir.glob("*-model-config.json"))}

    @staticmethod
    def rewrite_paths(value, _replacements):
        return value

    @staticmethod
    def invert(replacements):
        return replacements


class PathModule(IdentityModule):
    STATE_SUBDIR = Path("state/dalton-core")

    @staticmethod
    def invert(replacements):
        return {new: old for old, new in replacements.items()}

    @staticmethod
    def rewrite_paths(value, replacements):
        if isinstance(value, dict):
            return {key: PathModule.rewrite_paths(item, replacements)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [PathModule.rewrite_paths(item, replacements) for item in value]
        if isinstance(value, str):
            for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
                if value.startswith(old): return new + value[len(old):]
        return value

    @staticmethod
    def foreign_paths(value, root):
        found = []

        def visit(item):
            if isinstance(item, dict):
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str) and item.startswith("/") \
                    and not Path(item).is_relative_to(root):
                found.append(item)

        visit(value)
        return found


class SuccessorCopiedStateRehearsalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state = root / "state"; self.state.mkdir()
        self.agents = root / "LaunchAgents"; self.agents.mkdir()
        self.config = root / "service.json"
        self.service = {"backup": {"enabled": True, "keep_latest": 3},
                        "thesis_impact": {"enabled": False},
                        "bounded_planner": {"config": {}}}
        self.config.write_text(json.dumps(self.service))
        self.models = {
            "initial-screen-model-config.json": {
                "routing_policy_ref": "route:preserved",
                "structured_output_repair": {"max_attempts": 1}},
            "mission-document-draft-model-config.json": {
                "routing_policy_ref": "route:draft"},
            "mission-document-verifier-model-config.json": {
                "routing_policy_ref": "route:verify"},
        }
        for name, value in self.models.items():
            (self.state / name).write_text(json.dumps(value))
        self.document = {"schema_version": "document-research-config-0.1"}
        self.lane = {"schema_version": "0.1", "enabled": True}
        (self.state / "document-research-config.json").write_text(json.dumps(self.document))
        (self.state / "mission-document-research-lane.json").write_text(json.dumps(self.lane))
        (self.agents / "space.lumos.dalton.writer.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": ["dalton-writer",
                "--mission-document-research-lane", str(self.state / "mission-document-research-lane.json")]})
        )
        self.rehearsal = SimpleNamespace(
            temp_state=self.state, temp_config=self.config,
            launch_agents_dir=self.agents, replacements={},
        )

    def validate(self):
        return validate_successor_snapshots(
            IdentityModule, self.rehearsal, expected_models=self.models,
            expected_document=self.document, expected_lane=self.lane,
            expected_service=self.service,
        )

    def test_writer_append_runs_real_scratch_bootstrap_and_matches_prediction(self):
        token = self.state / "writer-tokens.json"
        before = (b'{"principals":[{"id":"core","operations":["a"],'
                  b'"token":"secret"}],"schema_version":"0.1"}\n')
        after = (b'{"principals":[{"id":"core","operations":["a","b"],'
                 b'"token":"secret"}],"schema_version":"0.1"}\n')
        token.write_bytes(before)

        def bootstrap():
            token.write_bytes(after)
            return "bootstrapped", []

        detail, findings = verify_scratch_bootstrap_writer_append(
            token_path=token, before=before, predicted_after=after,
            bootstrap=bootstrap)
        self.assertEqual("bootstrapped", detail)
        self.assertEqual([], findings)
        self.assertEqual(after, token.read_bytes())

    def test_writer_append_refuses_credential_or_noncore_drift(self):
        token = self.state / "writer-tokens.json"
        before = b'{"core":{"token":"secret"},"other":{"token":"kept"}}\n'
        predicted = b'{"core":{"token":"secret"},"other":{"token":"kept"}}\n'
        token.write_bytes(before)

        def bootstrap():
            token.write_bytes(
                b'{"core":{"token":"changed"},"other":{"token":"kept"}}\n')
            return "bootstrapped", []

        with self.assertRaisesRegex(
                RehearsalBindingError,
                "did not produce predicted writer tokens"):
            verify_scratch_bootstrap_writer_append(
                token_path=token, before=before, predicted_after=predicted,
                bootstrap=bootstrap)

    def test_writer_append_derivation_confines_exact_before_and_after_artifacts(self):
        fixture = PreserveExistingTransitionTests(methodName="runTest")
        fixture.setUp()
        try:
            manifest = fixture.build_writer_append()
            scratch = fixture.root / "writer-scratch"; scratch.mkdir()
            scratch_state = scratch / PathModule.STATE_SUBDIR
            scratch_state.mkdir(parents=True)
            for name in fixture.models:
                (scratch_state / name).write_bytes(
                    (fixture.packet / name).read_bytes())
            for name, path in fixture.preserved.items():
                (scratch_state / name).write_bytes(path.read_bytes())
            authority = (scratch_state /
                         "connector-governance/yfinance-analyst-estimates-v1.json")
            authority.parent.mkdir()
            authority.write_bytes(
                (fixture.packet / "yfinance-approved.json").read_bytes())
            scratch_config = scratch / "service.json"
            scratch_config.write_bytes(
                (fixture.packet / "service.before.json").read_bytes())
            scratch_openclaw = scratch / "openclaw/openclaw.json"
            scratch_openclaw.parent.mkdir()
            scratch_openclaw.write_bytes(
                (fixture.packet / "openclaw.preserved.json").read_bytes())
            rehearsal = SimpleNamespace(
                temp_root=scratch, temp_state=scratch_state,
                temp_config=scratch_config, replacements={})
            before = (fixture.packet / "writer-tokens.before.json").read_bytes()
            after = b'{"principals":[],"schema_version":"0.1","added":true}\n'
            with patch(
                "scripts.run_successor_copied_state_rehearsal."
                "expected_writer_operation_transition_state",
                return_value=(before, after,
                              manifest["writer_operation_transition"]),
            ):
                derived_path, proof_path, proof = derive_confined_transition(
                    PathModule, rehearsal, packet_root=fixture.packet,
                    manifest=manifest, original_manifest_sha256="f" * 64,
                    source_root=fixture.root / "successor")
            derived = json.loads(derived_path.read_text())
            row = derived["writer_operation_transition"]
            confined_before = derived_path.parent / row["before"]["file"]
            evidence = proof["writer_operation_transition"]
            confined_after = derived_path.parent / evidence["predicted_after"]["file"]
            self.assertEqual(before, confined_before.read_bytes())
            self.assertEqual(after, confined_after.read_bytes())
            self.assertEqual("successor-confined-transition-derivation-0.5",
                             proof["schema_version"])
            self.assertEqual(["b"], evidence["added_operations"])
            self.assertTrue(proof_path.is_file())
        finally:
            fixture.doCleanups()

    def test_schema_v05_main_path_stages_openclaw_then_bootstraps_and_applies(self):
        root = self.state.parent
        packet = root / "main-packet"; packet.mkdir()
        source = root / "source"; source.mkdir()
        live = root / "live"; live.mkdir()
        temp = Path(tempfile.mkdtemp(dir="/private/tmp"))
        temp.rmdir()
        before = b'{"principals":[{"id":"core","operations":["a"]}]}\n'
        after = b'{"principals":[{"id":"core","operations":["a","b"]}]}\n'
        openclaw = packet / "openclaw.json"
        openclaw.write_text('{"owner":{"signature":"kept"}}\n')
        service = packet / "service.json"; service.write_text("{}\n")
        baseline = packet / "models.json"; baseline.write_text("{}\n")
        writer_before = packet / "writer-before.json"; writer_before.write_bytes(before)
        proof = {
            "kind": "bootstrap_source_operations_append", "target": "writer-tokens.json",
            "principal_id": "core", "before_sha256": hashlib.sha256(before).hexdigest(),
            "predicted_after_sha256": hashlib.sha256(after).hexdigest(),
            "predecessor": {"commit": "c" * 40, "operations": ["a"],
                            "operations_hash": "1" * 64},
            "successor": {"commit": "d" * 40, "operations": ["a", "b"],
                          "operations_hash": "2" * 64},
            "added_operations": ["b"],
        }
        manifest = {
            "schema_version": "successor-config-transition-0.5",
            "status": "prepared_inert", "source_commit": "d" * 40,
            "acceptance": {"state": "pending"},
            "supporting_evidence": {"baseline_model_snapshot": {
                "file": baseline.name,
                "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()}},
            "writer_operation_transition": {
                "before": {"file": writer_before.name,
                           "sha256": hashlib.sha256(before).hexdigest()},
                "predecessor_source_root": str(root / "predecessor"),
                "proof": proof},
        }
        manifest_path = packet / "transition.json"
        manifest_path.write_text(json.dumps(manifest))
        transition_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        log, report, binding = (root / name for name in
                                ("main.log", "main.md", "main-binding.json"))

        class FakeBase:
            def __init__(self, _live, temp_root, **_kwargs):
                self.temp_root = temp_root; self.temp_state = temp_root / "state"
                self.temp_config = temp_root / "config/service.json"
                self.launch_agents_dir = temp_root / "LaunchAgents"
                self.replacements = {}; self.real_home = root
                self.steps = []; self.rows = []; self.confined = True
            def copy_state(self):
                self.temp_state.mkdir(parents=True)
                self.temp_config.parent.mkdir(parents=True)
                self.temp_config.write_text("{}\n")
                (self.temp_state / "writer-tokens.json").write_bytes(before)
                (self.temp_state / "document-research-config.json").write_text("{}\n")
                (self.temp_state / "mission-document-research-lane.json").write_text("{}\n")
                return "copied", []
            def confine_to_temp_root(self): return "confined", []
            def run_bootstrap(self):
                (self.temp_state / "writer-tokens.json").write_bytes(after)
                return "bootstrapped", []
            def run(self):
                self.copy_state()
                self.run_bootstrap()
                for _name, operation in self.post_catalog_sync_steps(): operation()
                return 0
            def report(self): return "passed"

        class FakeModule:
            Rehearsal = FakeBase
            STATE_SUBDIR = Path("state")
            validate_cli_paths = staticmethod(lambda **_kwargs: None)
            model_config_inventory = staticmethod(lambda _state: {})
            rewrite_paths = staticmethod(lambda value, _replacements: value)
            invert = staticmethod(lambda replacements: replacements)
            foreign_paths = staticmethod(lambda value, root: [
                item for item in []])

        def fake_derive(_module, rehearsal, **_kwargs):
            derived_root = rehearsal.temp_root / "derived"; derived_root.mkdir()
            derived = derived_root / "transition.json"; derived.write_text("{}\n")
            proof_path = derived_root / "proof.json"; proof_path.write_text("{}\n")
            return derived, proof_path, {"status": "derived"}

        def fake_apply(**kwargs):
            self.assertTrue(kwargs["external_config_path"].is_file())
            self.assertEqual(source.resolve(), kwargs["successor_source_root"])
            kwargs["receipt_path"].write_text("{}\n")
            return {"status": "configured_controller_start_pending"}

        args = SimpleNamespace(
            source_root=source, live_root=live, packet_root=packet, temp_root=temp,
            log=log, report=report, binding=binding,
            code_commit="d" * 40, ops_code_commit="700bde7c" + "0" * 32,
            transition_manifest=manifest_path,
            transition_manifest_sha256=transition_sha,
            service_config_snapshot=service,
            service_config_snapshot_sha256=hashlib.sha256(service.read_bytes()).hexdigest(),
            openclaw_config_snapshot=openclaw,
            openclaw_config_snapshot_sha256=hashlib.sha256(openclaw.read_bytes()).hexdigest(),
        )
        actual_ops = __import__("subprocess").check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip()
        args.ops_code_commit = actual_ops
        with patch("scripts.run_successor_copied_state_rehearsal._verify_frozen_source"), \
             patch("scripts.run_successor_copied_state_rehearsal._load_frozen_rehearsal",
                   return_value=FakeModule), \
             patch("scripts.run_successor_copied_state_rehearsal.expected_transition_state",
                   return_value=({}, {}, {})), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "expected_service_transition_state", return_value=({}, {})), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "expected_writer_operation_transition_state",
                   return_value=(before, after, manifest["writer_operation_transition"])), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "expected_preserved_openclaw_state", return_value=openclaw.read_bytes()), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "stage_existing_install_authorities", return_value={}), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "capture_external_market_digest_preservation", return_value={}), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "stage_preserved_runtime_configs"), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "replay_preserved_production_setup", return_value=("kept", [])), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "derive_confined_transition", side_effect=fake_derive), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "apply_transition_to_scratch", side_effect=fake_apply), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "validate_successor_snapshots", return_value={}), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "validate_existing_install_authorities", return_value={}), \
             patch("scripts.run_successor_copied_state_rehearsal."
                   "validate_external_market_digest_preservation", return_value={}):
            result = run_successor_rehearsal(args)
        self.assertEqual(hashlib.sha256(after).hexdigest(),
                         result["results"]["writer_operation_transition"]["after_sha256"])
        self.assertEqual(openclaw.read_bytes(),
                         (temp / "openclaw/openclaw.json").read_bytes())

    def test_final_snapshot_binds_configs_and_writer_lane_consumption(self):
        result = self.validate()
        self.assertEqual(result["model_config_count"], 3)
        self.assertEqual(result["writer_directed_document_flags"], [
            "--mission-document-research-lane"])

    def test_missing_writer_lane_flag_is_rejected(self):
        (self.agents / "space.lumos.dalton.writer.plist").write_bytes(
            plistlib.dumps({"ProgramArguments": ["dalton-writer"]}))
        with self.assertRaisesRegex(RehearsalBindingError, "does not consume"):
            self.validate()

    def test_model_document_lane_and_preserved_service_drift_are_rejected(self):
        mutations = (
            (self.state / "mission-document-draft-model-config.json", {"changed": 1}, "model"),
            (self.state / "document-research-config.json", {"changed": 1}, "document"),
            (self.state / "mission-document-research-lane.json", {"enabled": False}, "lane"),
            (self.config, {"backup": {"keep_latest": 9}}, "service"),
        )
        for path, value, reason in mutations:
            with self.subTest(reason=reason):
                before = path.read_bytes(); path.write_text(json.dumps(value))
                with self.assertRaises(RehearsalBindingError): self.validate()
                path.write_bytes(before)

    def test_document_config_is_inverse_normalized_without_changing_scratch_bytes(self):
        expected = {"originals_root": "/live/state/originals"}
        confined = {"originals_root": "/scratch/state/originals"}
        path = self.state / "document-research-config.json"
        path.write_text(json.dumps(confined))
        self.rehearsal.replacements = {"/live": "/scratch"}
        result = validate_successor_snapshots(
            PathModule, self.rehearsal, expected_models=self.models,
            expected_document=expected, expected_lane=self.lane,
            expected_service=self.service)
        self.assertEqual(result["document_research_config_sha256"],
                         __import__('hashlib').sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads(path.read_text()), confined)

    def _install_authority_fixture(self):
        root = self.state.parent
        live_root = root / "live"
        live_state = live_root / "state/dalton-core"
        (live_state / "phase8").mkdir(parents=True)
        phase8 = live_state / "phase8/p14e-adhoc-probe-templates-v1.json"
        phase8.write_bytes(b"owner-published-bytes-differ-from-repo\n")
        openclaw_live = root / "host/.openclaw/openclaw.json"
        catalog = live_state / "model-catalog-sync.json"
        catalog.write_text(json.dumps({
            "model_router_db": str(live_state / "model-router.sqlite"),
            "openclaw_config_path": str(openclaw_live),
        }), encoding="utf-8")
        snapshot = root / "packet/openclaw-config.before.json"
        snapshot.parent.mkdir()
        snapshot.write_bytes(b'{"models":{"providers":{}}}\n')
        scratch = root / "scratch"
        rehearsal = SimpleNamespace(
            live_root=live_root, temp_root=scratch,
            temp_state=scratch / "state/dalton-core",
            real_home=root / "home",
            replacements={str(live_root): str(scratch),
                          str(openclaw_live.parent): str(scratch / "broker")},
        )
        rehearsal.temp_state.mkdir(parents=True)
        return rehearsal, snapshot, phase8, catalog

    def _market_digest_fixture(self, rehearsal):
        target = (
            rehearsal.real_home
            / ".openclaw/workspace/skills/market-digest/output"
        )
        target.mkdir(parents=True)
        target.joinpath("large-external-corpus.json").write_text(
            '{"must_not_be_copied":true}', encoding="utf-8")
        link = rehearsal.live_root / PathModule.STATE_SUBDIR \
            / "feeds/market-digest-output"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        installer = self.state.parent / "source/deploy/macos/install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text(
            'digest_source="$openclaw_workspace/skills/market-digest/output"\n'
            'if [[ ! -e "$feeds_dir/market-digest-output" ]]; then\n'
            '  ln -s "$digest_source" "$feeds_dir/market-digest-output"\n'
            'fi\n',
            encoding="utf-8",
        )
        return link, target, installer

    def test_present_install_authorities_are_preserved_and_confined(self):
        rehearsal, snapshot, phase8, catalog = self._install_authority_fixture()
        digest = __import__('hashlib').sha256
        proof = stage_existing_install_authorities(
            PathModule, rehearsal, openclaw_snapshot=snapshot,
            openclaw_snapshot_sha256=digest(snapshot.read_bytes()).hexdigest(),
        )
        result = validate_existing_install_authorities(
            PathModule, rehearsal, proof)
        self.assertEqual(
            (rehearsal.temp_state /
             "phase8/p14e-adhoc-probe-templates-v1.json").read_bytes(),
            phase8.read_bytes(),
        )
        self.assertEqual(
            result["phase8_template_sha256"],
            digest(phase8.read_bytes()).hexdigest(),
        )
        confined = json.loads((
            rehearsal.temp_state / "model-catalog-sync.json"
        ).read_text(encoding="utf-8"))
        self.assertTrue(Path(confined["model_router_db"]).is_relative_to(
            rehearsal.temp_root))
        self.assertTrue(Path(confined["openclaw_config_path"]).is_relative_to(
            rehearsal.temp_root))
        self.assertEqual(
            Path(confined["openclaw_config_path"]).read_bytes(),
            snapshot.read_bytes(),
        )
        self.assertEqual(proof["model-catalog-sync.json"]["source_sha256"],
                         digest(catalog.read_bytes()).hexdigest())
        self.assertEqual(result["model_catalog_source_sha256"],
                         digest(catalog.read_bytes()).hexdigest())
        self.assertEqual(
            result["model_catalog_source_semantic_sha256"],
            proof["model-catalog-sync.json"]["source_semantic_sha256"],
        )

    def test_present_install_authority_symlink_and_tamper_are_refused(self):
        rehearsal, snapshot, phase8, _catalog = self._install_authority_fixture()
        digest = __import__('hashlib').sha256(snapshot.read_bytes()).hexdigest()
        phase8.unlink()
        outside = self.state.parent / "foreign.json"
        outside.write_text("{}", encoding="utf-8")
        phase8.symlink_to(outside)
        with self.assertRaisesRegex(RehearsalBindingError, "regular owned file"):
            stage_existing_install_authorities(
                PathModule, rehearsal, openclaw_snapshot=snapshot,
                openclaw_snapshot_sha256=digest,
            )

        phase8.unlink()
        phase8.write_bytes(b"owner\n")
        proof = stage_existing_install_authorities(
            PathModule, rehearsal, openclaw_snapshot=snapshot,
            openclaw_snapshot_sha256=digest,
        )
        target = rehearsal.temp_state / "phase8/p14e-adhoc-probe-templates-v1.json"
        target.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(RehearsalBindingError, "phase8 owner authority drifted"):
            validate_existing_install_authorities(PathModule, rehearsal, proof)

    def test_install_authority_swap_to_symlink_during_open_is_refused(self):
        rehearsal, snapshot, phase8, _catalog = self._install_authority_fixture()
        outside = self.state.parent / "foreign.json"
        outside.write_text('{"foreign":true}', encoding="utf-8")
        real_open = os.open

        def swap_then_open(path, flags, *args, **kwargs):
            if Path(path) == phase8:
                phase8.unlink()
                phase8.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)

        with patch(
            "scripts.run_successor_copied_state_rehearsal.os.open",
            side_effect=swap_then_open,
        ), self.assertRaisesRegex(RehearsalBindingError, "regular owned file"):
            stage_existing_install_authorities(
                PathModule, rehearsal, openclaw_snapshot=snapshot,
                openclaw_snapshot_sha256=(
                    __import__('hashlib').sha256(snapshot.read_bytes()).hexdigest()
                ),
            )

    def test_external_market_digest_link_is_proven_but_corpus_is_not_copied(self):
        rehearsal, _snapshot, _phase8, _catalog = (
            self._install_authority_fixture())
        link, target, installer = self._market_digest_fixture(rehearsal)
        link_text = os.readlink(link)
        proof = capture_external_market_digest_preservation(
            PathModule, rehearsal, installer=installer)
        result = validate_external_market_digest_preservation(
            PathModule, rehearsal, proof, installer=installer)
        self.assertEqual(os.readlink(link), link_text)
        self.assertEqual(result["resolved_external_target"], str(target.resolve()))
        self.assertTrue(result["install_preserves_existing_link"])
        self.assertFalse(result["external_corpus_copied"])
        self.assertFalse((rehearsal.temp_state / "feeds").exists())

    def test_external_market_digest_dangling_link_is_refused(self):
        rehearsal, _snapshot, _phase8, _catalog = (
            self._install_authority_fixture())
        link, target, installer = self._market_digest_fixture(rehearsal)
        target.joinpath("large-external-corpus.json").unlink()
        target.rmdir()
        with self.assertRaises((FileNotFoundError, RehearsalBindingError)):
            capture_external_market_digest_preservation(
                PathModule, rehearsal, installer=installer)

    def test_preserved_setup_replays_actual_entrypoints_twice_without_byte_drift(self):
        router = self.state / "model-router.sqlite"; router.touch()
        service = json.loads(self.config.read_text())
        service["model_router_db"] = str(router)
        self.config.write_text(json.dumps(service))
        rehearsal = SimpleNamespace(
            confined=True, temp_root=self.state.parent.resolve(), temp_state=self.state,
            temp_config=self.config, confine_to_temp_root=lambda: ("", []),
        )
        module = SimpleNamespace(configuration_setup_guard=lambda _path: nullcontext())
        extraction = {"policy": {"status": "duplicate"}}
        annual = {"created": []}
        cockpit = {"service_config_changed": False}
        with patch("dalton_core.document_extraction_setup.install",
                   return_value=extraction) as extraction_install, \
             patch("dalton_core.annual_report_setup.install",
                   return_value=annual) as annual_install, \
             patch("dalton_core.cockpit_setup.install",
                   return_value=cockpit) as cockpit_install:
            detail, findings = replay_preserved_production_setup(module, rehearsal)
        self.assertIn("3 model configs", detail)
        self.assertEqual([], findings)
        self.assertEqual(2, extraction_install.call_count)
        self.assertEqual(2, annual_install.call_count)
        self.assertEqual(2, cockpit_install.call_count)

    def test_preserved_setup_replay_refuses_installer_owned_config_drift(self):
        router = self.state / "model-router.sqlite"; router.touch()
        service = json.loads(self.config.read_text())
        service["model_router_db"] = str(router)
        self.config.write_text(json.dumps(service))
        rehearsal = SimpleNamespace(
            confined=True, temp_root=self.state.parent.resolve(), temp_state=self.state,
            temp_config=self.config, confine_to_temp_root=lambda: ("", []),
        )
        module = SimpleNamespace(configuration_setup_guard=lambda _path: nullcontext())

        def drift(_config):
            value = json.loads(self.config.read_text())
            value["owner_signature"] = "changed"
            self.config.write_text(json.dumps(value))
            return {"service_config_changed": False}

        with patch("dalton_core.document_extraction_setup.install",
                   return_value={"policy": {"status": "duplicate"}}), \
             patch("dalton_core.annual_report_setup.install",
                   return_value={"created": []}), \
             patch("dalton_core.cockpit_setup.install", side_effect=drift):
            with self.assertRaisesRegex(
                    RehearsalBindingError, "changed preserved configuration"):
                replay_preserved_production_setup(module, rehearsal)

    def test_confined_transition_derives_scratch_paths_without_weakening_original(self):
        root = self.state.parent
        packet = root / "packet"; packet.mkdir()
        scratch = root / "scratch"; scratch.mkdir()
        scratch_state = scratch / "state/dalton-core"; scratch_state.mkdir(parents=True)
        baseline = {"initial-screen-model-config.json": {
            "purpose": "initial", "budget_db": "/live/state/budget.sqlite"}}
        rewritten = PathModule.rewrite_paths(baseline, {"/live": str(scratch)})
        for name, value in rewritten.items():
            (scratch_state / name).write_text(json.dumps(value))
        additions = {
            "mission-document-draft-model-config.json": {
                "purpose": "draft", "budget_db": "/live/state/budget.sqlite"},
            "mission-document-verifier-model-config.json": {
                "purpose": "verify", "budget_db": "/live/state/budget.sqlite"},
        }
        targets = []
        for name, value in additions.items():
            path = packet / name; path.write_text(json.dumps(value))
            targets.append({"name": name, "kind": "exclusive_add",
                            "before_sha256": None,
                            "after": {"file": name,
                                      "sha256": __import__('hashlib').sha256(path.read_bytes()).hexdigest()}})
        initial_after = {**baseline["initial-screen-model-config.json"],
                         "structured_output_repair": {"max_attempts": 1}}
        for suffix, value in (("before", baseline["initial-screen-model-config.json"]),
                              ("after", initial_after)):
            (packet / f"initial.{suffix}.json").write_text(json.dumps(value))
        digest = lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        targets.append({"name": "initial-screen-model-config.json",
                        "kind": "compare_and_replace",
                        "before": {"file": "initial.before.json",
                                   "sha256": digest(packet / "initial.before.json")},
                        "after": {"file": "initial.after.json",
                                  "sha256": digest(packet / "initial.after.json")}})
        for name, value in (("document-research-config.json", {"root": "/live/originals"}),
                            ("mission-document-research-lane.json",
                             {"schema_version": "0.1", "enabled": True})):
            path = packet / name; path.write_text(json.dumps(value))
            targets.append({"name": name, "kind": "exclusive_add",
                            "before_sha256": None,
                            "after": {"file": name, "sha256": digest(path)}})
        (packet / "models.before.json").write_text(json.dumps(baseline))
        (packet / "audit.json").write_text("{}")
        final = {**baseline, **additions,
                 "initial-screen-model-config.json": initial_after}
        manifest = {
            "schema_version": "successor-config-transition-0.1",
            "status": "prepared_inert", "release_ref": "test",
            "source_commit": "a" * 40,
            "acceptance": {"state": "pending", "full_suite_receipt_sha256": None,
                           "wheel_sha256": None,
                           "copied_state_rehearsal_binding_sha256": None,
                           "health_acceptance_required": True},
            "model_inventory": {"before_count": 1, "after_count": 3,
                                "before_semantic_sha256": canonical_hash(baseline),
                                "after_semantic_sha256": canonical_hash(final)},
            "targets": targets,
            "supporting_evidence": {
                "baseline_model_snapshot": {"file": "models.before.json",
                    "sha256": digest(packet / "models.before.json")},
                "document_research_readonly_audit": {"file": "audit.json",
                    "sha256": digest(packet / "audit.json")}},
            "preserved_authorities": [], "boundaries": {},
        }
        manifest["content_hash"] = canonical_hash(manifest)
        rehearsal = SimpleNamespace(temp_root=scratch, temp_state=scratch_state,
                                    replacements={"/live": str(scratch)})
        derived_path, proof_path, proof = derive_confined_transition(
            PathModule, rehearsal, packet_root=packet, manifest=manifest,
            original_manifest_sha256="b" * 64)
        derived = json.loads(derived_path.read_text())
        self.assertEqual(proof["original_transition_manifest_sha256"], "b" * 64)
        self.assertEqual(json.loads((derived_path.parent /
            derived["supporting_evidence"]["baseline_model_snapshot"]["file"]).read_text()),
            rewritten)
        installed = [json.loads((derived_path.parent / row["after"]["file"]).read_text())
                     for row in derived["targets"]]
        self.assertNotIn('/live', json.dumps(installed))
        self.assertTrue(proof_path.is_file())
        receipt = apply_transition_to_scratch(
            packet_root=derived_path.parent, scratch_root=scratch,
            state_dir=scratch_state, manifest_path=derived_path,
            expected_manifest_sha256=digest(derived_path),
            receipt_path=scratch / "transition-receipt.json")
        self.assertEqual(receipt["status"], "scratch_configuration_applied")
        self.assertEqual(len(PathModule.model_config_inventory(scratch_state)), 3)

    def test_v02_stages_exact_live_document_and_lane_into_scratch(self):
        root = self.state.parent
        packet = root / "stage-packet"; packet.mkdir()
        live = root / "live"
        live_state = live / PathModule.STATE_SUBDIR
        live_state.mkdir(parents=True)
        scratch = root / "stage-scratch"; scratch.mkdir()
        scratch_state = scratch / PathModule.STATE_SUBDIR
        scratch_state.mkdir(parents=True)
        document = {"spool_dir": "/live/state/transcript-spool", "enabled": True}
        lane = {"schema_version": "0.1", "enabled": True}
        digest = lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        rows = []
        for name, value in (("document-research-config.json", document),
                            ("mission-document-research-lane.json", lane)):
            reviewed = packet / name
            reviewed.write_text(json.dumps(value))
            (live_state / name).write_bytes(reviewed.read_bytes())
            rows.append({"name": name, "kind": "preserve_existing",
                         "before": {"file": name, "sha256": digest(reviewed)},
                         "after_sha256": digest(reviewed)})
        confined = []
        rehearsal = SimpleNamespace(
            live_root=live, temp_root=scratch, temp_state=scratch_state,
            replacements={"/live": str(scratch)},
            confine_to_temp_root=lambda: confined.append(True),
        )
        staged = stage_preserved_runtime_configs(
            PathModule, rehearsal, packet_root=packet,
            manifest={"schema_version": "successor-config-transition-0.2",
                      "targets": rows})
        self.assertEqual(set(staged), {
            "document-research-config.json",
            "mission-document-research-lane.json"})
        self.assertEqual(str(scratch / "state/transcript-spool"), json.loads(
            (scratch_state / "document-research-config.json").read_text())["spool_dir"])
        self.assertEqual((packet / "mission-document-research-lane.json").read_bytes(),
                         (scratch_state / "mission-document-research-lane.json").read_bytes())
        self.assertEqual([True], confined)

    def test_v02_refuses_live_preserved_config_drift_before_scratch_write(self):
        root = self.state.parent
        packet = root / "drift-packet"; packet.mkdir()
        live = root / "drift-live"
        live_state = live / PathModule.STATE_SUBDIR
        live_state.mkdir(parents=True)
        scratch = root / "drift-scratch"; scratch.mkdir()
        scratch_state = scratch / PathModule.STATE_SUBDIR
        scratch_state.mkdir(parents=True)
        rows = []
        digest = lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        for name in ("document-research-config.json",
                     "mission-document-research-lane.json"):
            reviewed = packet / name
            reviewed.write_text(json.dumps({"name": name}))
            (live_state / name).write_bytes(reviewed.read_bytes())
            rows.append({"name": name, "kind": "preserve_existing",
                         "before": {"file": name, "sha256": digest(reviewed)},
                         "after_sha256": digest(reviewed)})
        (live_state / "document-research-config.json").write_text(
            json.dumps({"owner": "changed"}))
        rehearsal = SimpleNamespace(
            live_root=live, temp_root=scratch, temp_state=scratch_state,
            replacements={}, confine_to_temp_root=lambda: None)
        with self.assertRaisesRegex(RehearsalBindingError,
                                    "differs from review"):
            stage_preserved_runtime_configs(
                PathModule, rehearsal, packet_root=packet,
                manifest={"schema_version": "successor-config-transition-0.2",
                          "targets": rows})
        self.assertEqual([], list(scratch_state.iterdir()))

    def test_preserve_existing_derivation_normalizes_scratch_and_only_patches_service(self):
        root = self.state.parent
        packet = root / "packet-v2"; packet.mkdir()
        scratch = root / "scratch-v2"; scratch.mkdir()
        scratch_state = scratch / "state/dalton-core"; scratch_state.mkdir(parents=True)
        scratch_config = scratch / "config/service.json"; scratch_config.parent.mkdir()
        live_models = {
            "initial-screen-model-config.json": {"root": "/live/state", "kind": "initial"},
            "mission-document-draft-model-config.json": {"root": "/live/state", "kind": "draft"},
            "mission-document-verifier-model-config.json": {"root": "/live/state", "kind": "verify"},
        }
        replacements = {"/live": str(scratch)}
        scratch_models = PathModule.rewrite_paths(live_models, replacements)
        for name, value in scratch_models.items():
            (scratch_state / name).write_text(json.dumps(value))
        live_document = {"root": "/live/originals"}
        scratch_document = PathModule.rewrite_paths(live_document, replacements)
        (scratch_state / "document-research-config.json").write_text(
            json.dumps(scratch_document))
        lane = {"schema_version": "0.1", "enabled": True}
        (scratch_state / "mission-document-research-lane.json").write_text(json.dumps(lane))
        authority = {"schema_version": "0.1", "status": "approved",
                     "id": "connector-governance:test:v1"}
        authority["content_hash"] = __import__('hashlib').sha256(json.dumps(
            authority, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (packet / "authority.json").write_text(json.dumps(authority))
        scratch_authority = scratch_state / "connector-governance/authority.json"
        scratch_authority.parent.mkdir()
        scratch_authority.write_text(json.dumps(authority))
        live_service = {"bounded_planner": {"config": {}},
                        "owner": {"signature": "unchanged"},
                        "root": "/live/state"}
        scratch_service = PathModule.rewrite_paths(live_service, replacements)
        scratch_config.write_text(json.dumps(scratch_service, indent=2) + "\n")
        digest = lambda path: __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        (packet / "models.json").write_text(json.dumps(live_models))
        targets = []
        values = {**live_models, "document-research-config.json": live_document,
                  "mission-document-research-lane.json": lane}
        for name, value in values.items():
            path = packet / name; path.write_text(json.dumps(value))
            targets.append({"name": name, "kind": "preserve_existing",
                            "before": {"file": name, "sha256": digest(path)},
                            "after_sha256": digest(path)})
        (packet / "service.before.json").write_text(
            json.dumps(live_service, indent=2) + "\n")
        budget = {"max_input_tokens": 250000, "max_output_tokens": 4000,
                  "max_cost_usd": 3.0, "timeout_seconds": 300}
        service_after = json.loads(json.dumps(live_service))
        service_after["bounded_planner"]["config"]["planner_call_budget"] = budget
        delta = {
            "schema_version": "planner-call-budget-activation-0.1",
            "activation": "stopped-window CAS; refuse if target bytes or expected absent path drift",
            "target": "config/service.json",
            "expected_before_sha256": digest(packet / "service.before.json"),
            "json_path": ["bounded_planner", "config", "planner_call_budget"],
            "expected_before": {"state": "absent"}, "after": budget,
            "expected_after_sha256": __import__('hashlib').sha256(
                (json.dumps(service_after, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest(),
            "preservation": "all other service configuration, model configuration and routing authority remain unchanged",
        }
        delta["content_hash"] = __import__('hashlib').sha256(json.dumps(
            delta, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        (packet / "service.delta.json").write_text(json.dumps(delta))
        manifest = {
            "schema_version": "successor-config-transition-0.2",
            "transition_kind": "preserve_existing", "status": "prepared_inert",
            "release_ref": "test", "source_commit": "a" * 40,
            "acceptance": {"state": "pending", "full_suite_receipt_sha256": None,
                           "wheel_sha256": None,
                           "copied_state_rehearsal_binding_sha256": None,
                           "health_acceptance_required": True},
            "model_inventory": {"before_count": 3, "after_count": 3,
                "before_semantic_sha256": canonical_hash(live_models),
                "after_semantic_sha256": canonical_hash(live_models),
                "file_sha256": {name: digest(packet / name)
                                for name in live_models}},
            "targets": targets,
            "preserved_state_authorities": [{
                "path": "connector-governance/authority.json",
                "before": {"file": "authority.json",
                           "sha256": digest(packet / "authority.json")},
                "after_sha256": digest(packet / "authority.json"),
                "content_hash": authority["content_hash"], "status": "approved"}],
            "service_transition": {"kind": "compare_and_patch", "mutation_count": 1,
                "before": {"file": "service.before.json",
                           "sha256": digest(packet / "service.before.json")},
                "delta": {"file": "service.delta.json",
                          "sha256": digest(packet / "service.delta.json")},
                "after_sha256": delta["expected_after_sha256"]},
            "supporting_evidence": {
                "baseline_model_snapshot": {
                    "file": "models.json", "sha256": digest(packet / "models.json")},
                "model_config_files": {name: {"file": name,
                    "sha256": digest(packet / name)} for name in live_models}},
            "preserved_authorities": [], "boundaries": {
                "configuration_mutations": 0, "service_config_mutations": 1,
                "live_mutation": False, "manifest_publication": False,
                "service_lifecycle": False, "model_calls": False},
        }
        manifest["content_hash"] = canonical_hash(manifest)
        exact_authority_bytes = scratch_authority.read_bytes()
        scratch_authority.write_text(json.dumps(authority, indent=2) + "\n")
        drift_rehearsal = SimpleNamespace(
            temp_root=root / "drift-run", temp_state=scratch_state,
            temp_config=scratch_config, replacements=replacements)
        drift_rehearsal.temp_root.mkdir()
        with self.assertRaisesRegex(RehearsalBindingError,
                                    "authority bytes differ"):
            derive_confined_transition(
                PathModule, drift_rehearsal, packet_root=packet,
                manifest=manifest, original_manifest_sha256="b" * 64)
        scratch_authority.write_bytes(exact_authority_bytes)
        rehearsal = SimpleNamespace(temp_root=scratch, temp_state=scratch_state,
                                    temp_config=scratch_config, replacements=replacements)
        derived_path, _proof_path, proof = derive_confined_transition(
            PathModule, rehearsal, packet_root=packet, manifest=manifest,
            original_manifest_sha256="b" * 64)
        derived = json.loads(derived_path.read_text())
        self.assertEqual("successor-confined-transition-derivation-0.2",
                         proof["schema_version"])
        before_config_bytes = {
            path.relative_to(scratch_state).as_posix(): path.read_bytes()
            for path in scratch_state.rglob("*") if path.is_file()
        }
        receipt = apply_transition_to_scratch(
            packet_root=derived_path.parent, scratch_root=scratch,
            state_dir=scratch_state, service_config_path=scratch_config,
            manifest_path=derived_path, expected_manifest_sha256=digest(derived_path),
            receipt_path=scratch / "transition-receipt.json")
        self.assertEqual(0, receipt["configuration_mutations"])
        self.assertEqual(1, receipt["service_config_mutations"])
        self.assertEqual(before_config_bytes, {
            path.relative_to(scratch_state).as_posix(): path.read_bytes()
            for path in scratch_state.rglob("*") if path.is_file()
        })
        self.assertEqual(
            "connector-governance/authority.json",
            receipt["preserved_state_authorities"][0]["path"],
        )
        installed = json.loads(scratch_config.read_text())
        self.assertEqual(budget,
                         installed["bounded_planner"]["config"]["planner_call_budget"])
        self.assertEqual("unchanged", installed["owner"]["signature"])
        self.assertNotIn("/live", json.dumps(installed))
        self.assertEqual(derived["model_inventory"]["before_count"],
                         derived["model_inventory"]["after_count"])

    def test_r18_external_derivation_confines_model_broker_and_host_artifacts(self):
        fixture = PreserveExistingTransitionTests(methodName="runTest")
        fixture.setUp()
        try:
            manifest = fixture.build_external(
                managed_model_broker=True, already_target=True)
            before_host = fixture.packet / "runtime.before.mjs"
            after_host = fixture.packet / "runtime.after.mjs"
            before_host.write_text("const old = true;\n")
            after_host.write_text("const guarded = true;\n")
            source_root = fixture.root / "source"
            helper = (source_root / "integrations/openclaw_host_patches" /
                      "patch_provider_output_control_endpoint.py")
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("# inert reviewed helper\n")
            external = manifest["external_config_transitions"][0]
            external["managed_host_patch"] = {
                "source_commit": manifest["source_commit"],
                "helper_relative_path": (
                    "integrations/openclaw_host_patches/"
                    "patch_provider_output_control_endpoint.py"),
                "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                "target_relative_path": "dist/runtime-llm.runtime-test.mjs",
                "before": before_host.name, "before_sha256": hashlib.sha256(before_host.read_bytes()).hexdigest(),
                "after": after_host.name, "after_sha256": hashlib.sha256(after_host.read_bytes()).hexdigest(),
                "capability_check": "repo_helper_check_no_call",
            }
            manifest["content_hash"] = canonical_hash({
                key: value for key, value in manifest.items()
                if key != "content_hash"})
            scratch = fixture.root / "r18-scratch"; scratch.mkdir()
            scratch_state = scratch / PathModule.STATE_SUBDIR
            scratch_state.mkdir(parents=True)
            for name in fixture.models:
                (scratch_state / name).write_bytes((fixture.packet / name).read_bytes())
            for name, path in fixture.preserved.items():
                (scratch_state / name).write_bytes(path.read_bytes())
            authority = scratch_state / "connector-governance/yfinance-analyst-estimates-v1.json"
            authority.parent.mkdir(); authority.write_bytes(
                (fixture.packet / "yfinance-approved.json").read_bytes())
            scratch_config = scratch / "service.json"
            scratch_config.write_bytes(
                (fixture.packet / "service.installed.json").read_bytes())
            scratch_openclaw = scratch / "openclaw/openclaw.json"
            scratch_openclaw.parent.mkdir(); scratch_openclaw.write_bytes(
                (fixture.packet / "openclaw.before.json").read_bytes())
            rehearsal = SimpleNamespace(
                temp_root=scratch, temp_state=scratch_state,
                temp_config=scratch_config, replacements={})
            derived_path, _proof_path, proof = derive_confined_transition(
                PathModule, rehearsal, packet_root=fixture.packet,
                manifest=manifest, original_manifest_sha256="e" * 64,
                source_root=source_root)
            derived = json.loads(derived_path.read_text())
            _before, _after, row = expected_openclaw_frame_transition_state(
                packet_root=derived_path.parent, manifest=derived)
            plugin = row["managed_plugins"][0]
            self.assertTrue(Path(plugin["destination"]).resolve().is_relative_to(
                derived_path.parent.resolve()))
            self.assertTrue(plugin["replaces"].endswith(
                "/old/openclaw-model-broker"))
            host = row["managed_host_patch"]
            self.assertEqual(manifest["source_commit"], host["source_commit"])
            self.assertEqual(
                {"status": "artifacts_confined_not_executed", "model_calls": 0,
                 "before_sha256": host["before_sha256"],
                 "after_sha256": host["after_sha256"],
                 "helper_sha256": host["helper_sha256"]},
                proof["managed_host_patch"])
            self.assertTrue((derived_path.parent / "managed-host-patch" /
                             "patch-helper.py").is_file())
            for name in ("before", "after"):
                self.assertTrue((derived_path.parent / host[name]).is_file())
                self.assertTrue((derived_path.parent / host[name]).resolve().is_relative_to(
                    derived_path.parent.resolve()))
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
