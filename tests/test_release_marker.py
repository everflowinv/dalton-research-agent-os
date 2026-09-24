"""A release built by scripts/build_release.py must carry the venv integrity marker."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dalton_core.store import content_hash
from dalton_core.workspace_release import (
    DEPENDENCY_LOCK_SCHEMA, MARKER, drop_venv_novelty_aliases, validate_release,
    write_release_marker,
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


def _base_python(root: Path, name: str = "3.14.6", body: bytes = b"base-python") -> Path:
    """A stand-in Homebrew interpreter reached through an ``opt``-style link."""

    real = root / "Cellar" / name / "bin" / "python3.14"
    real.parent.mkdir(parents=True)
    real.write_bytes(body)
    os.chmod(real, 0o755)
    opt = root / "opt"
    if opt.is_symlink():
        opt.unlink()
    opt.symlink_to(real.parent.parent)
    return real


def _linked_venv(root: Path) -> Path:
    """What ``python -m venv`` (no ``--copies``) lays out on Homebrew 3.14."""

    venv = _fake_venv(root)
    (venv / "bin" / "python").unlink()
    (venv / "bin" / "python3.14").symlink_to(root / "opt" / "bin" / "python3.14")
    for name in ("python", "python3", "\U0001d70bthon"):
        (venv / "bin" / name).symlink_to("python3.14")
    return venv


class InterpreterLinkTests(unittest.TestCase):
    """Schema 0.3: bin/python* link to one pinned base interpreter (macOS TCC)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.base = _base_python(self.root)
        self.venv = _linked_venv(self.root)
        drop_venv_novelty_aliases(self.venv)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self):
        return write_release_marker(self.venv, release_hash="a" * 64, wheel_sha256="a" * 64)

    def test_a_linked_release_is_schema_0_3_and_validates(self):
        self.assertFalse((self.venv / "bin" / "\U0001d70bthon").is_symlink())
        record = self._write()
        self.assertEqual(record["schema_version"], "0.3")
        self.assertEqual(record["base_interpreter"], {
            "path": str(self.base),
            "sha256": hashlib.sha256(b"base-python").hexdigest()})
        links = {row["path"]: row["symlink"] for row in record["files"] if "symlink" in row}
        self.assertEqual(links, {
            "bin/python": "python3.14", "bin/python3": "python3.14",
            "bin/python3.14": str(self.root / "opt" / "bin" / "python3.14")})
        self.assertEqual(validate_release(self.venv, "a" * 64)["schema_version"], "0.3")

    def test_a_linked_release_with_a_dependency_lock_validates(self):
        wheels = [{"filename": "a-1.0-py3-none-any.whl", "sha256": "0" * 64}]
        lock_hash = content_hash({"schema_version": DEPENDENCY_LOCK_SCHEMA, "wheels": wheels})
        wheel_sha = "b" * 64
        release_hash = content_hash({"wheel_sha256": wheel_sha,
                                     "dependency_lock_hash": lock_hash})
        record = write_release_marker(
            self.venv, release_hash=release_hash, wheel_sha256=wheel_sha,
            dependency_lock_hash=lock_hash, dependency_wheels=wheels)
        self.assertEqual(record["schema_version"], "0.3")
        self.assertEqual(validate_release(self.venv, release_hash)["dependency_wheels"], wheels)

    def test_a_symbolic_link_outside_bin_is_refused(self):
        (self.venv / "lib" / "linked.py").symlink_to("x.py")
        with self.assertRaisesRegex(WorkspaceError, "symbolic link"):
            self._write()
        (self.venv / "lib" / "linked.py").unlink()
        self._write()
        (self.venv / "lib" / "linked.py").symlink_to(self.base)
        with self.assertRaisesRegex(WorkspaceError, "symbolic link"):
            validate_release(self.venv, "a" * 64)

    def test_a_non_interpreter_name_in_bin_is_refused(self):
        for name in ("pip", "python2", "python3.14t-config", "\U0001d70bthon"):
            with self.subTest(name=name):
                (self.venv / "bin" / name).unlink(missing_ok=True)
                (self.venv / "bin" / name).symlink_to("python3.14")
                with self.assertRaisesRegex(WorkspaceError, "symbolic link"):
                    self._write()
                (self.venv / "bin" / name).unlink()

    def test_a_link_to_another_interpreter_is_refused(self):
        self._write()
        other = self.root / "other-python"
        other.write_bytes(b"another interpreter")
        os.chmod(other, 0o755)
        (self.venv / "bin" / "python").unlink()
        (self.venv / "bin" / "python").symlink_to(other)
        with self.assertRaisesRegex(WorkspaceError, "another interpreter"):
            validate_release(self.venv, "a" * 64)
        # Nor can such a release be sealed: the links must share one target.
        with self.assertRaisesRegex(WorkspaceError, "one base interpreter"):
            self._write()

    def test_a_regular_file_in_place_of_the_link_is_refused(self):
        self._write()
        (self.venv / "bin" / "python").unlink()
        shutil.copyfile(self.base, self.venv / "bin" / "python")
        os.chmod(self.venv / "bin" / "python", 0o755)
        with self.assertRaisesRegex(WorkspaceError, "pinned interpreter link"):
            validate_release(self.venv, "a" * 64)

    def test_a_replaced_base_interpreter_is_refused(self):
        self._write()
        # A Homebrew patch upgrade: opt/ now points at a different Cellar.
        _base_python(self.root, "3.14.7", b"newer-python")
        with self.assertRaisesRegex(WorkspaceError, "rebuild the release"):
            validate_release(self.venv, "a" * 64)
        # The same interpreter path with other bytes is refused too.
        _base_python(self.root, "3.14.8", b"x")
        (self.root / "opt").unlink()
        (self.root / "opt").symlink_to(self.root / "Cellar" / "3.14.6")
        self.base.write_bytes(b"tampered-python")
        with self.assertRaisesRegex(WorkspaceError, "rebuild the release"):
            validate_release(self.venv, "a" * 64)

    def test_a_missing_base_interpreter_is_refused(self):
        self._write()
        shutil.rmtree(self.root / "Cellar")
        with self.assertRaisesRegex(WorkspaceError, "missing or moved"):
            validate_release(self.venv, "a" * 64)

    def test_a_forged_base_interpreter_declaration_is_refused(self):
        self._write()
        marker = self.venv / MARKER
        record = json.loads(marker.read_text())
        record["base_interpreter"]["sha256"] = "0" * 64
        body = {key: value for key, value in record.items() if key != "content_hash"}
        record["content_hash"] = content_hash(body)
        marker.write_text(json.dumps(record))
        with self.assertRaisesRegex(WorkspaceError, "base interpreter changed"):
            validate_release(self.venv, "a" * 64)

    def test_legacy_schemas_still_refuse_every_symbolic_link(self):
        # A --copies release sealed as 0.1 before this change keeps validating,
        # and a link planted into it later is not excused by the 0.3 rule.
        venv = _fake_venv(self.root / "legacy")
        record = write_release_marker(venv, release_hash="c" * 64, wheel_sha256="c" * 64)
        self.assertEqual(record["schema_version"], "0.1")
        self.assertNotIn("base_interpreter", record)
        validate_release(venv, "c" * 64)
        (venv / "bin" / "python3").symlink_to("python")
        with self.assertRaisesRegex(WorkspaceError, "symbolic link"):
            validate_release(venv, "c" * 64)

    def test_legacy_0_2_marker_written_by_the_old_code_validates(self):
        venv = _fake_venv(self.root / "legacy")
        wheels = [{"filename": "a-1.0-py3-none-any.whl", "sha256": "0" * 64}]
        lock_hash = content_hash({"schema_version": DEPENDENCY_LOCK_SCHEMA, "wheels": wheels})
        wheel_sha = "d" * 64
        release_hash = content_hash({"wheel_sha256": wheel_sha,
                                     "dependency_lock_hash": lock_hash})
        files = []
        for path in sorted(venv.rglob("*")):
            if path.is_file():
                files.append({"path": path.relative_to(venv).as_posix(),
                              "size": path.stat().st_size,
                              "mode": path.stat().st_mode & 0o777,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        body = {"schema_version": "0.2", "release_ref": f"release:sha256:{release_hash}",
                "wheel_sha256": wheel_sha, "files": files,
                "dependency_lock_hash": lock_hash, "dependency_wheels": wheels}
        (venv / MARKER).write_text(json.dumps({**body, "content_hash": content_hash(body)}))
        self.assertEqual(validate_release(venv, release_hash)["schema_version"], "0.2")
        # A 0.2 record cannot borrow the 0.3 field.
        forged = {**body, "base_interpreter": {"path": str(self.base), "sha256": "0" * 64}}
        (venv / MARKER).write_text(json.dumps({**forged, "content_hash": content_hash(forged)}))
        with self.assertRaisesRegex(WorkspaceError, "closed shape"):
            validate_release(venv, release_hash)

    def test_a_0_3_record_without_links_is_refused(self):
        self._write()
        for name in ("python", "python3", "python3.14"):
            (self.venv / "bin" / name).unlink()
        (self.venv / "bin" / "python").write_text("#!/bin/sh\n")
        os.chmod(self.venv / "bin" / "python", 0o755)
        with self.assertRaisesRegex(WorkspaceError, "pinned interpreter link"):
            validate_release(self.venv, "a" * 64)


if __name__ == "__main__":
    unittest.main()
