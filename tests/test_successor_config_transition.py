from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_research import build_document_research_policy
from scripts.prepare_successor_config_transition import (
    ConfigTransitionError, MODEL_ADDITIONS, MODEL_REPLACEMENT,
    apply_transition, build_transition, canonical_hash,
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
        }
        for key, value in (
            ("baseline", self.baseline), ("draft", model("draft")),
            ("verifier", model("verifier")), ("initial_before", self.initial),
            ("initial_after", {**self.initial,
                               "structured_output_repair": {"max_attempts": 1}}),
            ("activation", document_config("document-research-policy:production:1")),
            ("audit", document_config("document-research-policy:readonly-audit")),
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
        )

    def accepted_manifest(self):
        manifest = self.build()
        manifest["acceptance"] = {
            **manifest["acceptance"], "state": "accepted",
            "full_suite_receipt_sha256": "1" * 64,
            "wheel_sha256": "2" * 64,
            "copied_state_rehearsal_binding_sha256": "3" * 64,
        }
        manifest["content_hash"] = canonical_hash(
            {key: value for key, value in manifest.items() if key != "content_hash"}
        )
        return manifest

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
        manifest = self.accepted_manifest(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        unrelated = self.state / "owner.json"; unrelated.write_text("owner\n")
        receipt_path = self.packet / "receipt.json"
        receipt = apply_transition(
            packet_root=self.packet, state_dir=self.state,
            manifest_path=manifest_path,
            expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            receipt_path=receipt_path,
        )
        self.assertEqual(receipt["status"], "configured_controller_start_pending")
        self.assertEqual(unrelated.read_text(), "owner\n")
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()),
                         json.loads(self.paths["initial_after"].read_text()))
        self.assertTrue((self.state / MODEL_ADDITIONS[0]).is_file())
        self.assertTrue((self.state / "document-research-config.json").is_file())
        self.assertFalse(receipt["manifest_publication"])

    def test_precondition_drift_stops_before_any_mutation(self):
        manifest = self.accepted_manifest(); manifest_path = self.packet / "transition.json"
        write(manifest_path, manifest)
        for name, value in self.baseline.items(): write(self.state / name, value)
        write(self.state / MODEL_REPLACEMENT, {"owner": "changed"})
        with self.assertRaisesRegex(ConfigTransitionError, "reviewed baseline"):
            apply_transition(
                packet_root=self.packet, state_dir=self.state,
                manifest_path=manifest_path,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                receipt_path=self.packet / "receipt.json",
            )
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())

    def test_empty_or_duplicate_target_inventory_is_refused(self):
        for mutate in (
            lambda manifest: manifest.update(targets=[]),
            lambda manifest: manifest.update(targets=[manifest["targets"][0]] * 4),
        ):
            with self.subTest(mutate=mutate):
                manifest = self.accepted_manifest(); mutate(manifest)
                manifest["content_hash"] = canonical_hash(
                    {key: value for key, value in manifest.items()
                     if key != "content_hash"})
                manifest_path = self.packet / "transition.json"
                write(manifest_path, manifest)
                for name, value in self.baseline.items(): write(self.state / name, value)
                with self.assertRaisesRegex(ConfigTransitionError, "four targets"):
                    apply_transition(
                        packet_root=self.packet, state_dir=self.state,
                        manifest_path=manifest_path,
                        expected_manifest_sha256=hashlib.sha256(
                            manifest_path.read_bytes()).hexdigest(),
                        receipt_path=self.packet / "receipt.json",
                    )
                manifest_path.unlink()
                for path in self.state.iterdir(): path.unlink()

    def test_receipt_failure_rolls_back_completed_configuration(self):
        manifest = self.accepted_manifest(); manifest_path = self.packet / "transition.json"
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
            )
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())

    def test_rollback_continues_after_one_target_is_changed_externally(self):
        manifest = self.accepted_manifest(); manifest_path = self.packet / "transition.json"
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
            )
        self.assertEqual((self.state / MODEL_ADDITIONS[0]).read_text(), "external\n")
        self.assertFalse((self.state / MODEL_ADDITIONS[1]).exists())
        self.assertFalse((self.state / "document-research-config.json").exists())
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)

    def test_mid_transition_failure_rolls_back_only_owned_bytes(self):
        manifest = self.accepted_manifest(); manifest_path = self.packet / "transition.json"
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
            )
        self.assertEqual(json.loads((self.state / MODEL_REPLACEMENT).read_text()), self.initial)
        self.assertFalse((self.state / MODEL_ADDITIONS[0]).exists())
        self.assertFalse((self.state / MODEL_ADDITIONS[1]).exists())
        self.assertEqual(unrelated.read_text(), "concurrent\n")


if __name__ == "__main__":
    unittest.main()
