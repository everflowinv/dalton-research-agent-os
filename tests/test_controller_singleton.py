from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.controller_singleton import (
    ControllerConflict, ControllerOwnership, check, resident_controllers,
)


class ControllerSingletonTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.config = self.root / "config with spaces" / "service.json"
        self.config.parent.mkdir()
        self.heartbeat = self.root / "state" / "run" / "heartbeat.json"
        self.config.write_text(json.dumps({"heartbeat_path": str(self.heartbeat)}))
        self.source = Path(__file__).resolve().parents[1] / "src"

    def _environment(self, pythonpath: Path | None = None) -> dict[str, str]:
        return dict(os.environ, PYTHONPATH=str(pythonpath or self.source))

    @staticmethod
    def _stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()

    def test_lifetime_lock_refuses_a_second_real_process_then_releases(self) -> None:
        code = (
            "import sys,time; from dalton_core.controller_singleton import ControllerOwnership; "
            "guard=ControllerOwnership(sys.argv[1]); guard.__enter__(); "
            "print('ready',flush=True); time.sleep(30)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.config)],
            stdout=subprocess.PIPE, text=True, env=self._environment(),
        )
        self.addCleanup(self._stop, child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        with self.assertRaisesRegex(ControllerConflict, "controller_lock_held"):
            with ControllerOwnership(self.config):
                self.fail("second owner entered")
        child.terminate()
        child.wait(timeout=5)
        with ControllerOwnership(self.config):
            pass

    def test_legacy_module_process_is_found_but_other_config_is_not(self) -> None:
        package = self.root / "legacy" / "dalton_core"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "service.py").write_text("import time; time.sleep(30)\n")
        child = subprocess.Popen(
            [sys.executable, "-m", "dalton_core.service", "--config", str(self.config),
             "--once"],
            env=self._environment(package.parent),
        )
        self.addCleanup(self._stop, child)
        for _ in range(30):
            if child.pid in resident_controllers(self.config):
                break
        self.assertIn(child.pid, resident_controllers(self.config))
        self.assertNotIn(child.pid, resident_controllers(self.root / "other.json"))
        with self.assertRaisesRegex(ControllerConflict, "legacy_controller"):
            check(self.config)

    def test_scan_excludes_self_and_string_containing_unrelated_programs(self) -> None:
        target = self.config.resolve()
        output = "\n".join([
            f"10 daltond /runtime/daltond --config {target}",
            f"11 python3 /usr/bin/python3 -m dalton_core.service --config {target}",
            f"12 echo /runtime/daltond --config {target}",
            f"13 daltond /runtime/daltond --config {target}-other",
            f"14 echo /usr/bin/python3 -m dalton_core.service --config {target}",
            f"15 Python /usr/bin/python3 -m dalton_core.service --config {target} --once",
        ])
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        self.assertEqual(
            resident_controllers(target, self_pid=10, run=lambda *a, **k: completed),
            [11, 15],
        )

    def test_missing_first_install_config_is_clear_and_creates_nothing(self) -> None:
        missing = self.root / "fresh" / "service.json"
        self.assertEqual(check(missing)["status"], "clear")
        self.assertFalse(missing.parent.exists())

    def test_read_only_check_does_not_create_lock_file(self) -> None:
        lock = self.heartbeat.with_name(self.heartbeat.name + ".controller.lock")
        self.assertEqual(check(self.config)["status"], "clear")
        self.assertFalse(lock.exists())
        script = self.source / "dalton_core" / "controller_singleton.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--check", "--config", str(self.config)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "clear")
        self.assertFalse(lock.exists())

    def test_service_constructs_nothing_when_ownership_conflicts(self) -> None:
        from dalton_core import service

        with mock.patch.object(
            ControllerOwnership, "__enter__",
            side_effect=ControllerConflict([123]),
        ), mock.patch.object(service, "DaltonService") as constructor:
            self.assertEqual(
                service.main(["--config", str(self.config), "--once"]), 1)
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
