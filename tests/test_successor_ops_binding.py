from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.successor_ops_binding import (
    OpsBindingError, frozen_ops_binding, verify_ops_binding,
)


class FrozenOpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "scripts").mkdir()
        self.helper = self.root / "scripts" / "deploy.py"
        self.helper.write_text("print('reviewed')\n")
        self.git("add", ".")
        self.git("commit", "-qm", "reviewed helpers")
        self.binding = frozen_ops_binding(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args])

    def test_exact_helpers_reverify(self):
        verify_ops_binding(self.binding, self.root)
        self.assertEqual(1, self.binding["helper_count"])

    def test_uncommitted_helper_edit_refused(self):
        self.helper.write_text("print('changed')\n")
        with self.assertRaises(OpsBindingError):
            verify_ops_binding(self.binding, self.root)

    def test_committed_helper_edit_still_requires_new_rehearsal(self):
        self.helper.write_text("print('changed')\n")
        self.git("commit", "-qam", "different helpers")
        with self.assertRaises(OpsBindingError):
            verify_ops_binding(self.binding, self.root)

    def test_untracked_importable_helper_refused(self):
        (self.root / "scripts" / "extra.py").write_text("print('unreviewed')\n")
        with self.assertRaises(OpsBindingError):
            verify_ops_binding(self.binding, self.root)

    def test_missing_binding_refused(self):
        with self.assertRaises(OpsBindingError):
            verify_ops_binding({}, self.root)
