import copy
import json
from pathlib import Path
import unittest

from dalton_core.capability_attestation import (
    AttestationError,
    AttestationMismatch,
    CapabilityAttestation,
    PermissionViolation,
    compute_attestation_hash,
    compute_attestation_id,
    sha256_digest,
    validate_sandbox_report,
)
from dalton_core.store import content_hash


def digest(label):
    return sha256_digest({"fixture": label})


def trusted_context():
    empty_input = []
    empty_output = ""
    records_input = [{"z": 2, "a": "x"}, {"a": 1}]
    records_output = '{"a":"x","z":2}\n{"a":1}\n'
    fixtures = [
        {"fixture_id": "formatter:empty", "input_hash": sha256_digest(empty_input), "output_hash": sha256_digest(empty_output)},
        {"fixture_id": "formatter:records", "input_hash": sha256_digest(records_input), "output_hash": sha256_digest(records_output)},
    ]
    return {
        "capability_ref": "capability:formatter",
        "proposal_ref": "proposal:formatter:v1",
        "proposal_hash": digest("proposal"),
        "artifact_hash": digest("artifact"),
        "dependency_lock_hash": digest("stdlib-lock"),
        "environment_hash": digest("python-runtime"),
        "image_hash": digest("sandbox-image"),
        "fixtures": fixtures,
        "fixture_manifest_hash": sha256_digest(fixtures),
        "builder_invocation_ref": "invocation:builder",
        "evaluator_invocation_ref": "invocation:evaluator",
        "runner_identity": {
            "runner_ref": "runner:external-sandbox:v1",
            "invocation_ref": "invocation:runner",
            "actor_ref": "service:sandbox-runner",
        },
        "policy_ref": "policy:capability:v1",
        "policy_hash": digest("policy"),
        "grants": {
            "network": False,
            "filesystem_read": ["artifact:formatter", "fixture:formatter"],
            "filesystem_write": ["workspace:output"],
            "credential_refs": [],
            "core_db": False,
        },
        "limits": {
            "max_seconds": 5,
            "max_memory_bytes": 134217728,
            "max_stdout_bytes": 1048576,
            "max_stderr_bytes": 65536,
        },
        "started_at": "2026-08-14T00:00:00Z",
    }


def sandbox_report():
    expected = trusted_context()
    return {
        "observed_proposal_hash": expected["proposal_hash"],
        "observed_artifact_hash": expected["artifact_hash"],
        "observed_dependency_lock_hash": expected["dependency_lock_hash"],
        "observed_environment_hash": expected["environment_hash"],
        "observed_image_hash": expected["image_hash"],
        "observed_policy_hash": expected["policy_hash"],
        "fixtures": [
            dict(item, status="passed") for item in expected["fixtures"]
        ],
        "observed_effects": {
            "network_used": False,
            "filesystem_writes": ["workspace:output"],
            "credential_refs_used": [],
            "core_db_accessed": False,
        },
        "observed_usage": {
            "duration_seconds": 1,
            "peak_memory_bytes": 33554432,
            "stdout_bytes": 128,
            "stderr_bytes": 0,
        },
        "completed_at": "2026-08-14T00:00:01Z",
        "exit_code": 0,
        "stdout_hash": digest("stdout"),
        "stderr_hash": digest("stderr"),
        "result_status": "passed",
    }


class CapabilityAttestationTests(unittest.TestCase):
    def test_json_schema_matches_persisted_wire_shape(self):
        schema_path = Path(__file__).resolve().parents[1] / "contracts" / "capability-attestation.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        wire = validate_sandbox_report(trusted_context(), sandbox_report()).to_dict()
        self.assertEqual(set(schema["required"]), set(wire))
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(schema["properties"]["fixtures"]["uniqueItems"])
        for field in ("created_at", "started_at", "completed_at"):
            self.assertEqual(schema["properties"][field]["format"], "date-time")

    def test_formatter_positive_path_and_canonical_integrity(self):
        attestation = validate_sandbox_report(trusted_context(), sandbox_report())
        wire = attestation.to_dict()
        self.assertEqual(wire["result_status"], "passed")
        self.assertEqual(wire["capability_ref"], "capability:formatter")
        self.assertEqual(wire["observed_effects"]["filesystem_writes"], ["workspace:output"])
        self.assertTrue(wire["id"].startswith("attestation:"))
        self.assertEqual(len(wire["id"]), len("attestation:") + 64)
        self.assertEqual(compute_attestation_id(wire), wire["id"])
        self.assertEqual(compute_attestation_hash(wire), wire["content_hash"])
        self.assertEqual(CapabilityAttestation.from_dict(wire), attestation)
        self.assertEqual(CapabilityAttestation.from_dict(wire).to_dict(), wire)
        self.assertEqual(sha256_digest({"same": "wire"}), content_hash({"same": "wire"}))

        tampered = copy.deepcopy(wire)
        tampered["exit_code"] = 9
        with self.assertRaises(AttestationMismatch):
            CapabilityAttestation.from_dict(tampered)
        tampered_effects = copy.deepcopy(wire)
        tampered_effects["observed_effects"]["filesystem_writes"] = []
        with self.assertRaises(AttestationMismatch):
            CapabilityAttestation.from_dict(tampered_effects)
        self_consistent_unsafe = copy.deepcopy(wire)
        self_consistent_unsafe["observed_effects"]["network_used"] = True
        self_consistent_unsafe["id"] = compute_attestation_id(self_consistent_unsafe)
        self_consistent_unsafe["content_hash"] = compute_attestation_hash(self_consistent_unsafe)
        with self.assertRaises(PermissionViolation):
            CapabilityAttestation.from_dict(self_consistent_unsafe)

    def test_artifact_dependency_and_hash_format_tampering_fail_closed(self):
        for field in ("observed_artifact_hash", "observed_dependency_lock_hash"):
            report = sandbox_report()
            report[field] = digest("tampered")
            with self.subTest(field=field):
                with self.assertRaises(AttestationMismatch):
                    validate_sandbox_report(trusted_context(), report)
        report = sandbox_report()
        report["stdout_hash"] = "not-a-sha256"
        with self.assertRaises(AttestationError):
            validate_sandbox_report(trusted_context(), report)

    def test_missing_extra_duplicate_and_reordered_fixtures(self):
        missing = sandbox_report()
        missing["fixtures"] = missing["fixtures"][:1]
        with self.assertRaises(AttestationMismatch):
            validate_sandbox_report(trusted_context(), missing)

        extra = sandbox_report()
        extra["fixtures"].append({"fixture_id": "formatter:fake", "input_hash": digest("x"), "output_hash": digest("y"), "status": "passed"})
        with self.assertRaises(AttestationMismatch):
            validate_sandbox_report(trusted_context(), extra)

        duplicate = sandbox_report()
        duplicate["fixtures"].append(copy.deepcopy(duplicate["fixtures"][0]))
        with self.assertRaises(AttestationError):
            validate_sandbox_report(trusted_context(), duplicate)

        ordered = validate_sandbox_report(trusted_context(), sandbox_report())
        reordered_report = sandbox_report()
        reordered_report["fixtures"].reverse()
        reordered_context = trusted_context()
        reordered_context["fixtures"].reverse()
        reordered = validate_sandbox_report(reordered_context, reordered_report)
        self.assertEqual(reordered.to_dict(), ordered.to_dict())

    def test_environment_policy_and_fixture_hash_forgery_rejected(self):
        for field in ("observed_environment_hash", "observed_image_hash", "observed_policy_hash", "observed_proposal_hash"):
            report = sandbox_report()
            report[field] = digest("forged")
            with self.subTest(field=field):
                with self.assertRaises(AttestationMismatch):
                    validate_sandbox_report(trusted_context(), report)
        report = sandbox_report()
        report["fixtures"][0]["output_hash"] = digest("wrong-output")
        with self.assertRaises(AttestationMismatch):
            validate_sandbox_report(trusted_context(), report)
        changed_manifest = trusted_context()
        changed_manifest["fixtures"][0]["output_hash"] = digest("alternate-expected-output")
        with self.assertRaises(AttestationMismatch):
            validate_sandbox_report(changed_manifest, sandbox_report())

    def test_passed_attestation_must_obey_observed_resource_limits(self):
        long_report = sandbox_report()
        long_report["completed_at"] = "2026-08-14T01:00:00Z"
        long_report["observed_usage"]["duration_seconds"] = 3600
        with self.assertRaises(AttestationMismatch):
            validate_sandbox_report(trusted_context(), long_report)

        for field, value in (
            ("peak_memory_bytes", 134217729),
            ("stdout_bytes", 1048577),
            ("stderr_bytes", 65537),
        ):
            report = sandbox_report()
            report["observed_usage"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(AttestationMismatch):
                    validate_sandbox_report(trusted_context(), report)

    def test_permissions_and_undeclared_writes_are_rejected(self):
        for field, value in (("network", True), ("core_db", True), ("credential_refs", ["credential:secret"])):
            context = trusted_context()
            context["grants"][field] = value
            with self.subTest(grant=field):
                with self.assertRaises(PermissionViolation):
                    validate_sandbox_report(context, sandbox_report())

        for field, value in (("network_used", True), ("core_db_accessed", True), ("credential_refs_used", ["credential:secret"])):
            report = sandbox_report()
            report["observed_effects"][field] = value
            with self.subTest(observed=field):
                with self.assertRaises(PermissionViolation):
                    validate_sandbox_report(trusted_context(), report)
        report = sandbox_report()
        report["observed_effects"]["filesystem_writes"].append("workspace:undeclared")
        with self.assertRaises(PermissionViolation):
            validate_sandbox_report(trusted_context(), report)

    def test_builder_is_independent_from_evaluator_and_runner(self):
        for field in ("evaluator_invocation_ref", "runner"):
            context = trusted_context()
            if field == "runner":
                context["runner_identity"]["invocation_ref"] = context["builder_invocation_ref"]
            else:
                context[field] = context["builder_invocation_ref"]
            with self.subTest(field=field):
                with self.assertRaises(AttestationMismatch):
                    validate_sandbox_report(context, sandbox_report())

    def test_report_cannot_supply_authority_fields(self):
        for field, value in (
            ("runner_identity", {"actor_ref": "human:forged"}),
            ("policy_ref", "policy:forged"),
            ("grants", {"network": False}),
            ("limits", {"max_seconds": 999}),
        ):
            report = sandbox_report()
            report[field] = value
            with self.subTest(field=field):
                with self.assertRaises(AttestationError):
                    validate_sandbox_report(trusted_context(), report)


if __name__ == "__main__":
    unittest.main()
