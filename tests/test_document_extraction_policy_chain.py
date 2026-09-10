from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.model_router import ModelRouter
from tests.test_transcript_polish_model_worker import policy, profile


class DocumentExtractionPolicyChainTests(unittest.TestCase):
    def test_approved_fallback_chain_is_readable_before_exact_route_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with ModelRouter(Path(directory) / "router.sqlite") as router:
                first = profile()
                second = profile()
                second.update({
                    "profile_version_ref": "model-profile-version:test-extraction-backup:1",
                    "id": "profile:test-extraction-backup",
                    "model": "extraction-backup",
                    "family": "test-extraction-backup",
                    "credential_slot_ref": "credential-slot:openclaw:test-backup",
                })
                router.register_profile(first)
                router.register_profile(second)
                chain = policy()
                chain["filters"]["allowed_profile_ids"] = [first["id"], second["id"]]
                router.register_policy(chain)
                loaded = DocumentExtractionService.model_policy(
                    router, chain["policy_version_ref"])
                self.assertEqual(
                    loaded["filters"]["allowed_profile_ids"],
                    [first["id"], second["id"]],
                )


if __name__ == "__main__":
    unittest.main()
