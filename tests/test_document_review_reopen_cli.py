import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_review_reopen_cli import SupplementalReviewCliError, main, verify_packet


class SupplementalReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = {"candidate_hash": "a" * 64, "items": [{
            "review_id": "review:1", "prior_review_hash": "b" * 64,
            "failed_windows": [{"work_order_ref": "work:1"}],
        }]}
        self._write("candidate.json", self.candidate)
        self.params = {"review_id": "review:1", "expected_review_hash": "b" * 64,
                       "decision_ref": f"owner-decision:document-supplement:{'a' * 64}",
                       "failed_windows": [{"work_order_ref": "work:1"}]}
        self._write("ACN-params.json", self.params)
        manifest = {"schema_version": "0.1", "actor_ref": "human:lumos",
                    "candidate": "candidate.json", "candidate_sha256": self.sha("candidate.json"),
                    "params": [{"file": "ACN-params.json", "sha256": self.sha("ACN-params.json")}]}
        self._write("execution-manifest.json", manifest)

    def _write(self, name, value):
        (self.root / name).write_text(json.dumps(value, sort_keys=True) + "\n")

    def sha(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def verified(self):
        with patch("dalton_core.document_review_reopen_cli.review_reopen_candidate",
                   return_value={"ready_review_ids": ["review:1"]}):
            return verify_packet(self.root / "execution-manifest.json", self.sha("execution-manifest.json"),
                                 core_db=self.root / "core", scheduler_db=self.root / "scheduler")

    def test_exact_packet_is_semantically_bound(self):
        candidate, params = self.verified()
        self.assertEqual(candidate, self.candidate)
        self.assertEqual(params, [self.params])

    def test_parameter_drift_or_wrong_file_hash_is_rejected(self):
        self.params["expected_review_hash"] = "c" * 64
        self._write("ACN-params.json", self.params)
        with self.assertRaisesRegex(SupplementalReviewCliError, "file hash drifted"):
            self.verified()

    def test_execute_cannot_run_without_interactive_human(self):
        argv = ["--packet-manifest", str(self.root / "execution-manifest.json"),
                "--expected-manifest-sha256", self.sha("execution-manifest.json"),
                "--core-db", str(self.root / "core"), "--scheduler-db", str(self.root / "scheduler"),
                "--token-config", str(self.root / "tokens"), "--socket", str(self.root / "writer.sock"),
                "--execute"]
        with patch("dalton_core.document_review_reopen_cli.review_reopen_candidate",
                   return_value={"ready_review_ids": ["review:1"]}), \
             patch("dalton_core.document_review_reopen_cli.sys.stdin.isatty", return_value=False), \
             patch("dalton_core.document_review_reopen_cli.ephemeral_call") as call:
            with self.assertRaisesRegex(SupplementalReviewCliError, "interactive terminal"):
                main(argv)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
