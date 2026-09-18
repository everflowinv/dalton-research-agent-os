"""A release built by scripts/build_release.py must carry the venv integrity marker."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from dalton_core.store import content_hash
from dalton_core.workspace_release import (
    DEPENDENCY_LOCK_SCHEMA, MARKER, validate_release, write_release_marker,
)
from dalton_core.workspace import WorkspaceError


def _fake_venv(root: Path) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("python", "daltond", "dalton-writer"):
        path = venv / "bin" / name
        path.write_text("#!/bin/sh\n")
        os.chmod(path, 0o755)
    (venv / "lib").mkdir()
    (venv / "lib" / "x.py").write_text("x = 1\n")
    return venv


class ReleaseMarkerTests(unittest.TestCase):
    def test_marker_written_after_the_fact_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = _fake_venv(Path(tmp))
            wheel_sha = hashlib.sha256(b"wheel").hexdigest()
            wheels = [{"filename": "a-1.0-py3-none-any.whl", "sha256": "0" * 64}]
            lock_hash = content_hash({"schema_version": DEPENDENCY_LOCK_SCHEMA, "wheels": wheels})
            # The identity install_release and build_release.py both derive.
            release_hash = content_hash({"wheel_sha256": wheel_sha,
                                         "dependency_lock_hash": lock_hash})
            record = write_release_marker(
                venv, release_hash=release_hash, wheel_sha256=wheel_sha,
                dependency_lock_hash=lock_hash, dependency_wheels=wheels)
            self.assertEqual(record["schema_version"], "0.2")
            self.assertEqual(record["dependency_wheels"], wheels)
            stored = json.loads((venv / MARKER).read_text())
            self.assertEqual(stored["content_hash"], record["content_hash"])
            validated = validate_release(venv, release_hash)
            self.assertEqual(validated["wheel_sha256"], wheel_sha)

    def test_marker_without_a_lock_is_schema_0_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = _fake_venv(Path(tmp))
            record = write_release_marker(
                venv, release_hash="a" * 64, wheel_sha256="a" * 64)
            self.assertEqual(record["schema_version"], "0.1")
            validate_release(venv, "a" * 64)

    def test_an_incomplete_venv_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            (venv / "bin").mkdir(parents=True)
            with self.assertRaises(WorkspaceError):
                write_release_marker(venv, release_hash="a" * 64, wheel_sha256="b" * 64)


if __name__ == "__main__":
    unittest.main()
