"""The installer gives a large Core bounded time to become genuinely healthy."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "deploy" / "macos" / "install.sh"
START = "# A large existing Core can spend well over thirty seconds"
END = "# Validate every independently verified model pair"


class InstallerStartupWaitTests(unittest.TestCase):
    def _run(self, *, timeout: str, healthy_on: int | None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            calls = root / "calls"
            health = venv / "bin" / "dalton-health"
            health.write_text(
                "#!/bin/sh\n"
                "[ -n \"${HEALTH_DELAY:-}\" ] && /bin/sleep \"$HEALTH_DELAY\"\n"
                "n=0; [ -f \"$CALLS\" ] && n=$(cat \"$CALLS\")\n"
                "n=$((n + 1)); echo $n > \"$CALLS\"\n"
                "[ -n \"${HEALTHY_ON:-}\" ] && [ $n -ge \"$HEALTHY_ON\" ]\n",
                encoding="utf-8",
            )
            health.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sleep = fake_bin / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            sleep.chmod(0o755)
            source = INSTALL.read_text(encoding="utf-8")
            fragment = source[source.index(START):source.index(END)]
            command = "\n".join([
                "set -euo pipefail", f"venv_dir={venv}",
                f"config_path={root / 'service.json'}", fragment,
                "wait_for_healthy_runtime",
            ])
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                   "CALLS": str(calls), "DALTON_STARTUP_TIMEOUT_SECONDS": timeout}
            env.setdefault("HEALTH_DELAY", "0.1")
            if healthy_on is not None:
                env["HEALTHY_ON"] = str(healthy_on)
            completed = subprocess.run(
                ["zsh", "-c", command], text=True, capture_output=True, env=env)
            count = int(calls.read_text()) if calls.exists() else 0
            return completed, count

    def test_configured_wait_reaches_a_late_healthy_tick(self):
        completed, calls = self._run(timeout="6", healthy_on=4)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, 4)

    def test_starting_never_counts_as_success_and_wait_is_bounded(self):
        completed, calls = self._run(timeout="1", healthy_on=None)
        self.assertNotEqual(completed.returncode, 0)
        self.assertGreaterEqual(calls, 2)

    def test_health_execution_time_counts_against_wall_clock_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            # Reuse the fragment runner but make each health probe consume most
            # of the one-second deadline. The old sleep-counter loop would
            # always wait a separate two seconds before its final diagnostic.
            before = __import__("time").monotonic()
            old = os.environ.get("HEALTH_DELAY")
            os.environ["HEALTH_DELAY"] = "0.6"
            try:
                completed, calls = self._run(timeout="1", healthy_on=None)
            finally:
                if old is None:
                    os.environ.pop("HEALTH_DELAY", None)
                else:
                    os.environ["HEALTH_DELAY"] = old
            elapsed = __import__("time").monotonic() - before
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls, 2)
        self.assertLess(elapsed, 2.4)

    def test_invalid_timeout_is_refused_before_health(self):
        completed, calls = self._run(timeout="unbounded", healthy_on=1)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(calls, 0)
        self.assertIn("must be an integer", completed.stderr)

    def test_default_allows_three_minutes_for_large_core_replay(self):
        source = INSTALL.read_text(encoding="utf-8")
        self.assertIn(
            "startup_timeout_seconds=${DALTON_STARTUP_TIMEOUT_SECONDS:-180}", source)


if __name__ == "__main__":
    unittest.main()
