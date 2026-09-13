from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.run_successor_copied_state_rehearsal import (
    RehearsalBindingError, verify_scratch_bootstrap_writer_preservation,
)


class WriterPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = self.root / "writer-tokens.json"
        self.before = b'{"core":"same-token","operations":["settle_company_model_spec"]}\n'
        self.token.write_bytes(self.before)
        self.token.chmod(0o600)

    def verify(self, bootstrap):
        return verify_scratch_bootstrap_writer_preservation(
            token_path=self.token, bootstrap=bootstrap)

    def test_unchanged_authority_emits_exact_proof(self):
        detail, findings, proof = self.verify(lambda: ("bootstrapped", []))
        digest = hashlib.sha256(self.before).hexdigest()
        self.assertEqual((detail, findings), ("bootstrapped", []))
        self.assertEqual(proof, {"before_sha256": digest, "after_sha256": digest,
                                 "before_mode": 0o600, "after_mode": 0o600})

    def test_unreviewed_operation_or_token_change_refuses(self):
        for changed in (self.before.replace(b"same-token", b"new-token"),
                        self.before.replace(b"settle_company_model_spec", b"new-operation")):
            with self.subTest(changed=changed):
                self.token.write_bytes(self.before)
                def bootstrap():
                    self.token.write_bytes(changed)
                    return "bootstrapped", []
                with self.assertRaisesRegex(RehearsalBindingError, "predicted writer tokens"):
                    self.verify(bootstrap)

    def test_mode_change_refuses_even_when_bytes_are_equal(self):
        def bootstrap():
            self.token.chmod(0o640)
            return "bootstrapped", []
        with self.assertRaisesRegex(RehearsalBindingError, "permissions"):
            self.verify(bootstrap)

    def test_symlink_replacement_refuses(self):
        other = self.root / "other"
        other.write_bytes(self.before)
        def bootstrap():
            self.token.unlink()
            self.token.symlink_to(other)
            return "bootstrapped", []
        with self.assertRaisesRegex(RehearsalBindingError, "predicted writer tokens"):
            self.verify(bootstrap)

    def test_missing_before_never_calls_bootstrap(self):
        self.token.unlink()
        called = []
        with self.assertRaisesRegex(RehearsalBindingError, "unavailable"):
            self.verify(lambda: called.append(True))
        self.assertEqual(called, [])

    def test_bootstrap_failure_is_not_a_preservation_pass(self):
        def bootstrap():
            raise RuntimeError("bootstrap failed")
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            self.verify(bootstrap)
