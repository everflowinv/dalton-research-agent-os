from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_release_copied_state_rehearsal import RehearsalBindingError
from scripts.run_successor_copied_state_rehearsal import (
    derive_confined_transition, replay_preserved_production_setup,
    validate_successor_snapshots,
)
from scripts.prepare_successor_config_transition import (
    apply_transition_to_scratch, canonical_hash,
)


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
            for old, new in replacements.items():
                if value.startswith(old): return new + value[len(old):]
        return value


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


if __name__ == "__main__":
    unittest.main()
