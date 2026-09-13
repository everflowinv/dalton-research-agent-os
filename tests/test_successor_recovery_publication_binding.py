"""Exercise publication with the recovery builder's real serialized contract."""
from __future__ import annotations

import json
import unittest

from scripts import publish_successor_verified_release as publish
from tests import test_publish_successor_verified_release as publisher_fixtures
from tests import test_successor_predecessor_recovery as recovery_fixtures

write = publisher_fixtures.write


class RecoveryPublicationBindingTests(unittest.TestCase):
    def test_builder_identity_reaches_verified_release_without_shape_translation(self):
        predecessor = recovery_fixtures.PredecessorRecoveryTests()
        self.addCleanup(predecessor.doCleanups)
        predecessor.setUp()
        proof = recovery_fixtures.build(predecessor.paths)
        publisher = publisher_fixtures.SuccessorPublisherTests()
        self.addCleanup(publisher.doCleanups)
        fixture = publisher.fixture()
        packet, owner, _, manifest, _, accepted, args = fixture
        (owner / "current-release.json").write_bytes(
            predecessor.paths["published_pointer"].read_bytes())
        (owner / "current-runtime-config.json").write_bytes(
            predecessor.paths["published_runtime_pointer"].read_bytes())
        args["expected_current_release_sha256"] = publish.sha(owner / "current-release.json")
        args["expected_current_runtime_config_sha256"] = publish.sha(owner / "current-runtime-config.json")
        manifest["predecessor_recovery"] = proof
        write(packet / "release-manifest.candidate.json", manifest)
        args["expected_manifest_sha256"] = publish.sha(packet / "release-manifest.candidate.json")
        deployed = json.loads(args["deployment_path"].read_text())
        deployed["candidate_manifest_sha256"] = args["expected_manifest_sha256"]
        write(args["deployment_path"], deployed)
        args["expected_deployment_sha256"] = publish.sha(args["deployment_path"])
        write(args["installed_path"], {"predecessor_recovery": proof})
        args["expected_installed_sha256"] = publish.sha(args["installed_path"])
        exact = {"authority": "exact", "predecessor_recovery": proof}
        publish.finalizer.verify_predecessor_recovery(
            manifest, json.loads(args["installed_path"].read_text()), exact)
        accepted.update(
            candidate_manifest_sha256=args["expected_manifest_sha256"],
            deployment_receipt_sha256=args["expected_deployment_sha256"],
            installed_verification_sha256=args["expected_installed_sha256"],
            runtime_verification=exact,
        )
        write(args["finalization_path"], accepted)
        args["expected_finalization_sha256"] = publish.sha(args["finalization_path"])
        with publisher.verified_context(fixture):
            result = publish.publish(**args)
        self.assertEqual(result["predecessor_recovery"], proof)
        current = json.loads((owner / "current-release.json").read_text())
        self.assertEqual(current["previous_release"]["source_commit"],
                         proof["published_release"]["source_commit"])
        self.assertEqual(current["runtime_verification"]["predecessor_recovery"][
            "failed_install"]["deployment_status"], "deployment_failed")


if __name__ == "__main__":
    unittest.main()
