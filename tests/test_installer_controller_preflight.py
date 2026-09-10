"""Installer refuses an unmanaged controller before drain or replacement."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "deploy" / "macos" / "install.sh"
START = "# Quiesce the old runtime before replacing any installed code."
END = 'if [[ ! -x "$venv_dir/bin/python" ]]; then'


class InstallerControllerPreflightTests(unittest.TestCase):
    def _run(self, *, blocked: bool) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            log = root / "calls"
            launchctl = bin_dir / "launchctl"
            launchctl.write_text(
                "#!/bin/sh\necho launchctl:$1:${2:-} >> \"$CALL_LOG\"\n"
                "[ \"$1\" = print ] && exit 1\nexit 0\n",
                encoding="utf-8",
            )
            launchctl.chmod(0o755)
            python = bin_dir / "python"
            python.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  */controller_singleton.py)\n"
                "    echo preflight:$1:$2:$4 >> \"$CALL_LOG\"\n"
                "    [ \"${BLOCKED:-0}\" = 1 ] && exit 1\n"
                "    exit 0;;\n"
                "  */launch_drain.py) echo drain >> \"$CALL_LOG\"; exit 0;;\n"
                "  *) echo later-python >> \"$CALL_LOG\"; exit 0;;\n"
                "esac\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            code = INSTALL.read_text(encoding="utf-8")
            fragment = code[code.index(START):code.index(END)]
            command = "set -e\n" + "\n".join([
                f"domain=gui/501", f"python_source={python}",
                f"repo_root={root / 'new-checkout'}", f"config_path={root / 'service.json'}",
                f"state_dir={root / 'state'}", fragment,
            ])
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "CALL_LOG": str(log), "BLOCKED": "1" if blocked else "0"}
            completed = subprocess.run(
                ["zsh", "-c", command], text=True, capture_output=True, env=env)
            return completed, log.read_text().splitlines()

    def test_preflight_uses_new_checkout_after_managed_controller_stop(self) -> None:
        completed, calls = self._run(blocked=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        preflight = next(i for i, call in enumerate(calls) if call.startswith("preflight:"))
        controller_probe = next(i for i, call in enumerate(calls)
                                if call.endswith("space.lumos.dalton.controller"))
        self.assertLess(controller_probe, preflight)
        self.assertIn("/new-checkout/src/dalton_core/controller_singleton.py:--check:",
                      calls[preflight])
        self.assertTrue(calls[preflight].endswith("/service.json"))
        self.assertGreater(calls.index("drain"), preflight)

    def test_blocked_preflight_stops_before_drain_and_later_mutations(self) -> None:
        completed, calls = self._run(blocked=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(any("controller_singleton.py:--check:" in call for call in calls))
        self.assertNotIn("drain", calls)
        self.assertNotIn("later-python", calls)
        self.assertIn("runtime has not been upgraded", completed.stderr)


if __name__ == "__main__":
    unittest.main()
