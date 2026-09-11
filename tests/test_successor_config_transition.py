from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_research import build_document_research_policy
from scripts.prepare_successor_config_transition import (
    ConfigTransitionError, LANE_CONFIG, MODEL_ADDITIONS, MODEL_REPLACEMENT,
    DOCUMENT_CONFIG, PRESERVED_TARGETS, apply_transition,
    build_preserve_existing_transition, build_transition, canonical_hash,
)


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def model(name: str) -> dict:
    return {
        "routing_policy_ref": f"routing-policy:{name}:1",
        "credential_slot_refs": [f"credential-slot:openclaw:{name}"],
        "model_router_db": "/state/model-router.sqlite",
        "broker_socket": "/state/model-broker.sock",
        "broker_auth_key": "/state/model-broker.key",
        "broker_client_id": "client:dalton-core",
        "expected_agent_id": "chem", "budget_db": "/state/budget.sqlite",
        "budget_policy_ref": "budget-policy:production:1",
        "call_budget": {"max_input_tokens": 64000, "max_output_tokens": 4096,
                        "max_cost_usd": 1.0, "timeout_seconds": 600},
        "run_budget": {"max_units": 4, "max_seconds": 7200},
        "provider_retry": {"max_same_profile_retries": 1,
                           "retry_backoff_seconds": 2},
        "transport_retry": {"max_definitely_not_sent_retries": 1,
                            "queue_wait_seconds": 600,
                            "retry_backoff_seconds": 2},
    }


def document_config(policy_ref: str) -> dict:
    policy = build_document_research_policy(
        policy_ref=policy_ref,
        allowed_purposes=["mission_directed_document_research"],
        allowed_access_policy_refs=["policy:access:public"],
        max_question_chars=12000, max_query_terms=16,
        max_query_term_chars=240, max_results=24,
        max_context_before_chars=2000, max_context_after_chars=4000,
        max_read_chars=100000,
    )
    return {
        "schema_version": "document-research-config-0.1",
        "purpose": "mission_directed_document_research",
        "spool_dir": "/state/transcript-spool",
        "enabled_sources": ["source:alphaengine", "source:sec-edgar"],
        "policy": policy, "inventory_preview_chars": 600,
        "source_reading_limits": {
            "alphaengine_max_document_chars": 10000000,
            "public_web_max_source_chars": 10000000,
            "public_web_max_pdf_pages": 2000,
            "public_web_max_decompressed_bytes": 100000000,
        },
    }


class SuccessorConfigTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packet = self.root / "packet"; self.packet.mkdir()
        self.state = self.root / "state"; self.state.mkdir()
        self.initial = model("initial")
        self.other = model("other")
        self.baseline = {
            MODEL_REPLACEMENT: self.initial,
            "other-model-config.json": self.other,
        }
        self.paths = {
            "baseline": self.packet / "baseline.json",
            "draft": self.packet / MODEL_ADDITIONS[0],
            "verifier": self.packet / MODEL_ADDITIONS[1],
            "initial_before": self.packet / "initial.before.json",
            "initial_after": self.packet / "initial.after.json",
            "activation": self.packet / "document-research.activation.json",
            "audit": self.packet / "document-research.audit.json",
            "lane": self.packet / LANE_CONFIG,
        }
        for key, value in (
            ("baseline", self.baseline), ("draft", model("draft")),
            ("verifier", model("verifier")), ("initial_before", self.initial),
            ("initial_after", {**self.initial,
                               "structured_output_repair": {"max_attempts": 1}}),
            ("activation", document_config("document-research-policy:production:1")),
            ("audit", document_config("document-research-policy:readonly-audit")),
            ("lane", {"schema_version": "0.1", "enabled": True}),
        ):
            write(self.paths[key], value)

    def build(self):
        return build_transition(
            packet_root=self.packet, release_ref="directed-document-successor",
            source_commit="a" * 40, baseline_models_path=self.paths["baseline"],
            draft_path=self.paths["draft"], verifier_path=self.paths["verifier"],
            initial_before_path=self.paths["initial_before"],
            initial_after_path=self.paths["initial_after"],
            document_activation_path=self.paths["activation"],
            document_audit_path=self.paths["audit"],
            lane_activation_path=self.paths["lane"],
        )

    def acceptance_evidence(self):
        return {
            "state": "accepted", "health_acceptance_required": True,
            "full_suite_receipt_sha256": "1" * 64,
            "wheel_sha256": "2" * 64,
            "copied_state_rehearsal_binding_sha256": "3" * 64,
        }

    def test_prepares_dynamic_exact_delta_and_preserves_other_model_bytes(self):
        result = self.build()
        self.assertEqual(result["status"], "prepared_inert")
        self.assertEqual(result["model_inventory"]["before_count"], 2)
        self.assertEqual(result["model_inventory"]["after_count"], 4)
        final = dict(self.baseline)
        final[MODEL_ADDITIONS[0]] = model("draft")
        final[MODEL_ADDITIONS[1]] = model("verifier")
        final[MODEL_REPLACEMENT] = {
            **self.initial, "structured_output_repair": {"max_attempts": 1}}
        self.assertEqual(result["model_inventory"]["after_semantic_sha256"],
                         canonical_hash(final))
        self.assertIn("all_other_model_configs", result["preserved_authorities"])
        self.assertIsNone(result["acceptance"]["wheel_sha256"])
        self.assertFalse(result["boundaries"]["live_mutation"])

    def test_audit_config_cannot_be_used_as_activation(self):
        self.paths["activation"].write_bytes(self.paths["audit"].read_bytes())
        with self.assertRaisesRegex(ConfigTransitionError, "audit policy cannot"):
            self.build()

    def test_initial_screen_delta_is_exactly_one_bounded_repair_attempt(self):
        changed = {**self.initial, "structured_output_repair": {"max_attempts": 2}}
        write(self.paths["initial_after"], changed)
        with self.assertRaisesRegex(ConfigTransitionError, "changes more than"):
            self.build()

    def test_incomplete_acceptance_evidence_cannot_enter_stopped_window(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        with self.assertRaisesRegex(ConfigTransitionError, "accepted full-suite"):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json",
            )
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())

    def test_apply_uses_exact_preconditions_and_writes_no_other_state(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        unrelated = self.state / "owner.json"; unrelated.write_text("owner\n")
        receipt_path = self.packet / "receipt.json"
        receipt = apply_transition(
            packet_root=self.packet, state_dir=self.state,
            manifest_path=manifest_path,
            expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            receipt_path=receipt_path, accepted_evidence=self.acceptance_evidence(),
        )
        self.assertEqual(receipt["status"], "configured_controller_start_pending")
        self.assertEqual(unrelated.read_text(), "owner\n")
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()),
                         json.loads(self.paths["initial_after"].read_text()))
        self.assertTrue((self.state / MODEL_ADDITIONS[0]).is_file())
        self.assertTrue((self.state / "document-research-config.json").is_file())
        self.assertTrue((self.state / LANE_CONFIG).is_file())
        self.assertFalse(receipt["manifest_publication"])

    def test_precondition_drift_stops_before_any_mutation(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        write(self.state / MODEL_REPLACEMENT, {"owner": "changed"})
        with self.assertRaisesRegex(ConfigTransitionError, "reviewed baseline"):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json",
                accepted_evidence=self.acceptance_evidence(),
            )
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())

    def test_empty_or_duplicate_target_inventory_is_refused(self):
        for mutate in (
            lambda manifest: manifest.update(targets=[]),
            lambda manifest: manifest.update(targets=[manifest["targets"][0]] * 4),
        ):
            with self.subTest(mutate=mutate):
                manifest = self.build(); mutate(manifest)
                manifest["content_hash"] = canonical_hash(
                    {key: value for key, value in manifest.items()
                     if key != "content_hash"})
                manifest_path = self.packet / "transition.json"
                write(manifest_path, manifest)
                for name, value in self.baseline.items(): write(self.state / name, value)
                with self.assertRaisesRegex(ConfigTransitionError, "five targets"):
                    apply_transition(
                        packet_root=self.packet, state_dir=self.state,
                        manifest_path=manifest_path,
                        expected_manifest_sha256=hashlib.sha256(
                            manifest_path.read_bytes()).hexdigest(),
                        receipt_path=self.packet / "receipt.json",
                        accepted_evidence=self.acceptance_evidence(),
                    )
                manifest_path.unlink()
                for path in self.state.iterdir(): path.unlink()

    def test_receipt_failure_rolls_back_completed_configuration(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        def fail(name: str) -> None:
            if name == "before_receipt": raise OSError("receipt storage failed")
        with self.assertRaisesRegex(OSError, "receipt storage failed"):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json", fault_hook=fail,
                accepted_evidence=self.acceptance_evidence(),
            )
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())
        self.assertFalse((self.state / LANE_CONFIG).exists())

    def test_rollback_continues_after_one_target_is_changed_externally(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        def fail(name: str) -> None:
            if name == "before_receipt":
                (self.state / MODEL_ADDITIONS[0]).write_text("external\n")
                raise RuntimeError("late failure")
        with self.assertRaisesRegex(ConfigTransitionError, MODEL_ADDITIONS[0]):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json", fault_hook=fail,
                accepted_evidence=self.acceptance_evidence(),
            )
        self.assertEqual((self.state / MODEL_ADDITIONS[0]).read_text(), "external\n")
        self.assertFalse((self.state / MODEL_ADDITIONS[1]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())
        self.assertFalse((self.state / LANE_CONFIG).exists())
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)

    def test_mid_transition_failure_rolls_back_only_owned_bytes(self):
        manifest = self.build(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        unrelated = self.state / "owner.json"; unrelated.write_text("before\n")
        def fail(name: str) -> None:
            unrelated.write_text("concurrent\n")
            if name == MODEL_REPLACEMENT:
                raise RuntimeError("fixture failure")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json", fault_hook=fail,
                accepted_evidence=self.acceptance_evidence(),
            )
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / MODEL_ADDITIONS[1]).exists())
        self.assertEqual(unrelated.read_text(), "concurrent\n")


class PreserveExistingTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packet = self.root / "packet"; self.packet.mkdir()
        self.state = self.root / "state"; self.state.mkdir()
        self.service = self.root / "config/service.json"
        self.service.parent.mkdir()
        self.models = {
            MODEL_ADDITIONS[0]: model("draft"),
            MODEL_ADDITIONS[1]: model("verifier"),
            MODEL_REPLACEMENT: model("initial"),
            "owner-preserved-model-config.json": {
                **model("owner"), "owner_metadata": {"signature": "owner:sig"}},
        }
        self.preserved = {}
        for name, value in self.models.items():
            path = self.packet / name
            write(path, value)
            if name in (*MODEL_ADDITIONS, MODEL_REPLACEMENT):
                self.preserved[name] = path
        self.document = document_config("document-research-policy:production:1")
        self.lane = {"schema_version": "0.1", "enabled": True}
        for name, value in ((DOCUMENT_CONFIG, self.document), (LANE_CONFIG, self.lane)):
            path = self.packet / name
            write(path, value)
            self.preserved[name] = path
        write(self.packet / "models.json", self.models)
        authority = {"schema_version": "0.1", "status": "approved",
                     "id": "connector-governance:test:v1"}
        authority["content_hash"] = hashlib.sha256(json.dumps(
            authority, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        write(self.packet / "yfinance-approved.json", authority)
        self.service_before = {
            "bounded_planner": {"config": {"other_owner_value": "keep"}},
            "credential": {"slot": "secret-ref"},
            "owner_metadata": {"signature": "signed"},
        }
        write(self.packet / "service.before.json", self.service_before)
        self.budget = {
            "max_cost_usd": 3.0, "max_input_tokens": 250000,
            "max_output_tokens": 4000, "timeout_seconds": 300,
        }
        after = json.loads(json.dumps(self.service_before))
        after["bounded_planner"]["config"]["planner_call_budget"] = self.budget
        delta = {
            "activation": "stopped-window CAS; refuse if target bytes or expected absent path drift",
            "after": self.budget,
            "expected_after_sha256": hashlib.sha256(
                (json.dumps(after, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest(),
            "expected_before": {"state": "absent"},
            "expected_before_sha256": hashlib.sha256(
                (self.packet / "service.before.json").read_bytes()).hexdigest(),
            "json_path": ["bounded_planner", "config", "planner_call_budget"],
            "preservation": "all other service configuration, model configuration and routing authority remain unchanged",
            "schema_version": "planner-call-budget-activation-0.1",
            "target": "config/service.json",
        }
        delta["content_hash"] = hashlib.sha256(json.dumps(
            delta, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        write(self.packet / "service.delta.json", delta)

    def build(self):
        return build_preserve_existing_transition(
            packet_root=self.packet, release_ref="code-successor",
            source_commit="b" * 40,
            baseline_models_path=self.packet / "models.json",
            model_config_paths={name: self.packet / name for name in self.models},
            preserved_config_paths=self.preserved,
            preserved_state_authority_paths={
                "connector-governance/yfinance-analyst-estimates-v1.json":
                    self.packet / "yfinance-approved.json"},
            service_config_before_path=self.packet / "service.before.json",
            service_delta_path=self.packet / "service.delta.json",
        )

    @staticmethod
    def accepted():
        return {
            "state": "accepted", "health_acceptance_required": True,
            "full_suite_receipt_sha256": "1" * 64,
            "wheel_sha256": "2" * 64,
            "copied_state_rehearsal_binding_sha256": "3" * 64,
        }

    def install_before(self):
        for name, value in self.models.items():
            write(self.state / name, value)
        for name in (DOCUMENT_CONFIG, LANE_CONFIG):
            (self.state / name).write_bytes(self.preserved[name].read_bytes())
        self.service.write_bytes((self.packet / "service.before.json").read_bytes())
        governance = self.state / "connector-governance"
        governance.mkdir()
        (governance / "yfinance-analyst-estimates-v1.json").write_bytes(
            (self.packet / "yfinance-approved.json").read_bytes())

    def state_files(self):
        return {path.relative_to(self.state).as_posix(): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()}

    def apply(self, manifest, *, fault_hook=None):
        manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        return apply_transition(
            packet_root=self.packet, state_dir=self.state,
            service_config_path=self.service, manifest_path=manifest_path,
            expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            receipt_path=self.packet / "receipt.json",
            accepted_evidence=self.accepted(), fault_hook=fault_hook,
        )

    def test_dynamic_inventory_and_five_configs_are_preserved_exactly(self):
        manifest = self.build()
        self.assertEqual("successor-config-transition-0.2", manifest["schema_version"])
        self.assertEqual(4, manifest["model_inventory"]["before_count"])
        self.assertEqual(4, manifest["model_inventory"]["after_count"])
        self.assertEqual(set(PRESERVED_TARGETS),
                         {row["name"] for row in manifest["targets"]})
        self.assertTrue(all(row["kind"] == "preserve_existing"
                            and row["before"]["sha256"] == row["after_sha256"]
                            for row in manifest["targets"]))
        self.install_before()
        before = self.state_files()
        receipt = self.apply(manifest)
        self.assertEqual(0, receipt["configuration_mutations"])
        self.assertEqual(1, receipt["service_config_mutations"])
        self.assertEqual(before, self.state_files())
        installed = json.loads(self.service.read_text())
        self.assertEqual(self.budget,
                         installed["bounded_planner"]["config"]["planner_call_budget"])
        self.assertEqual(self.service_before["owner_metadata"],
                         installed["owner_metadata"])
        self.assertEqual(self.service_before["credential"], installed["credential"])
        self.assertEqual([{
            "path": "connector-governance/yfinance-analyst-estimates-v1.json",
            "sha256": hashlib.sha256(
                (self.packet / "yfinance-approved.json").read_bytes()).hexdigest(),
            "content_hash": json.loads(
                (self.packet / "yfinance-approved.json").read_text())["content_hash"],
            "status": "approved",
        }], receipt["preserved_state_authorities"])

    def test_config_drift_refuses_before_service_mutation(self):
        manifest = self.build(); self.install_before()
        write(self.state / MODEL_ADDITIONS[0], {"changed": True})
        service_before = self.service.read_bytes()
        with self.assertRaisesRegex(ConfigTransitionError, "full model|exact bytes"):
            self.apply(manifest)
        self.assertEqual(service_before, self.service.read_bytes())

    def test_semantically_equal_model_reformat_is_still_byte_drift(self):
        manifest = self.build(); self.install_before()
        target = self.state / "owner-preserved-model-config.json"
        target.write_text(json.dumps(self.models[target.name], separators=(",", ":")))
        with self.assertRaisesRegex(ConfigTransitionError, "full model"):
            self.apply(manifest)

    def test_approved_state_authority_drift_refuses_before_service_mutation(self):
        manifest = self.build(); self.install_before()
        target = (self.state / "connector-governance" /
                  "yfinance-analyst-estimates-v1.json")
        changed = json.loads(target.read_text())
        changed["status"] = "proposed"
        unsigned = {key: value for key, value in changed.items()
                    if key != "content_hash"}
        changed["content_hash"] = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        write(target, changed)
        service_before = self.service.read_bytes()
        with self.assertRaisesRegex(ConfigTransitionError,
                                    "state authority bytes differ"):
            self.apply(manifest)
        self.assertEqual(service_before, self.service.read_bytes())

    def test_state_authority_symlinked_ancestor_refuses_before_service_mutation(self):
        manifest = self.build(); self.install_before()
        governance = self.state / "connector-governance"
        external = self.root / "external-governance"
        governance.rename(external)
        governance.symlink_to(external, target_is_directory=True)
        service_before = self.service.read_bytes()
        with self.assertRaisesRegex(ConfigTransitionError,
                                    "state authority bytes differ"):
            self.apply(manifest)
        self.assertEqual(service_before, self.service.read_bytes())
        self.assertTrue(governance.is_symlink())

    def test_nonfinite_service_budget_is_rejected_by_runtime_validator(self):
        delta_path = self.packet / "service.delta.json"
        delta = json.loads(delta_path.read_text())
        delta["after"]["max_cost_usd"] = float("inf")
        unsigned = {key: value for key, value in delta.items() if key != "content_hash"}
        delta["content_hash"] = hashlib.sha256(json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        write(delta_path, delta)
        with self.assertRaisesRegex(ConfigTransitionError, "values are invalid"):
            self.build()

    def test_late_failure_rolls_back_exact_service_bytes(self):
        manifest = self.build(); self.install_before()
        service_before = self.service.read_bytes()
        config_before = self.state_files()

        def fail(stage: str) -> None:
            if stage == "before_receipt":
                raise OSError("receipt failed")

        with self.assertRaisesRegex(OSError, "receipt failed"):
            self.apply(manifest, fault_hook=fail)
        self.assertEqual(service_before, self.service.read_bytes())
        self.assertEqual(config_before, self.state_files())

    def test_late_failure_does_not_overwrite_concurrent_service_change(self):
        manifest = self.build(); self.install_before()

        def fail(stage: str) -> None:
            if stage == "before_receipt":
                write(self.service, {"owner": {"concurrent": True}})
                raise OSError("receipt failed after external change")

        with self.assertRaisesRegex(
                ConfigTransitionError, "refused to overwrite concurrent"):
            self.apply(manifest, fault_hook=fail)
        self.assertEqual({"owner": {"concurrent": True}},
                         json.loads(self.service.read_text()))

    def test_receipt_publish_failure_leaves_no_partial_and_restores_service(self):
        manifest = self.build(); self.install_before()
        service_before = self.service.read_bytes()
        receipt = self.packet / "receipt.json"
        real_link = os.link

        def fail_receipt_link(source, target, *args, **kwargs):
            if Path(target) == receipt:
                raise OSError("receipt publish failed")
            return real_link(source, target, *args, **kwargs)

        with patch.object(os, "link", side_effect=fail_receipt_link):
            with self.assertRaisesRegex(OSError, "receipt publish failed"):
                self.apply(manifest)
        self.assertFalse(receipt.exists())
        self.assertEqual(service_before, self.service.read_bytes())

    def test_receipt_temp_failure_leaves_no_partial_and_restores_service(self):
        manifest = self.build(); self.install_before()
        service_before = self.service.read_bytes()

        def fail(stage: str) -> None:
            if stage == "receipt_temp_written":
                raise OSError("receipt fsync boundary failed")

        with self.assertRaisesRegex(OSError, "receipt fsync boundary failed"):
            self.apply(manifest, fault_hook=fail)
        self.assertFalse((self.packet / "receipt.json").exists())
        self.assertEqual(service_before, self.service.read_bytes())

    def test_atomic_service_create_does_not_overwrite_racing_owner(self):
        manifest = self.build(); self.install_before()
        real_link = os.link

        def race_service_link(source, target, *args, **kwargs):
            if Path(target).resolve() == self.service.resolve():
                write(self.service, {"owner": {"concurrent": True}})
            return real_link(source, target, *args, **kwargs)

        with patch.object(os, "link", side_effect=race_service_link):
            with self.assertRaises(FileExistsError):
                self.apply(manifest)
        self.assertEqual({"owner": {"concurrent": True}},
                         json.loads(self.service.read_text()))
        self.assertFalse((self.packet / "receipt.json").exists())

    def test_atomic_service_create_preserves_racing_dangling_symlink(self):
        manifest = self.build(); self.install_before()
        real_link = os.link

        def race_service_link(source, target, *args, **kwargs):
            if Path(target).resolve() == self.service.resolve():
                self.service.symlink_to(self.root / "owner-missing-target.json")
            return real_link(source, target, *args, **kwargs)

        with patch.object(os, "link", side_effect=race_service_link):
            with self.assertRaises(FileExistsError):
                self.apply(manifest)
        self.assertTrue(self.service.is_symlink())
        self.assertEqual(self.root / "owner-missing-target.json",
                         self.service.readlink())
        self.assertFalse((self.packet / "receipt.json").exists())

    def test_pre_move_dangling_symlink_replacement_is_restored_not_lost(self):
        manifest = self.build(); self.install_before()
        real_rename = os.rename
        injected = False

        def race_service_rename(source, target, *args, **kwargs):
            nonlocal injected
            if not injected and Path(source).resolve() == self.service.resolve():
                injected = True
                self.service.unlink()
                self.service.symlink_to(self.root / "owner-pre-move-missing.json")
            return real_rename(source, target, *args, **kwargs)

        with patch.object(os, "rename", side_effect=race_service_rename):
            with self.assertRaisesRegex(ConfigTransitionError, "changed during"):
                self.apply(manifest)
        self.assertTrue(injected)
        self.assertTrue(self.service.is_symlink())
        self.assertEqual(self.root / "owner-pre-move-missing.json",
                         self.service.readlink())

    def test_service_delta_cannot_overwrite_existing_budget_or_other_path(self):
        manifest = self.build(); self.install_before()
        changed = json.loads(self.service.read_text())
        changed["bounded_planner"]["config"]["planner_call_budget"] = {"old": True}
        write(self.service, changed)
        with self.assertRaisesRegex(ConfigTransitionError, "CAS precondition"):
            self.apply(manifest)


if __name__ == "__main__":
    unittest.main()
