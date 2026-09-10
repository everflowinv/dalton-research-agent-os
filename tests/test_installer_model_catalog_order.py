"""The installer must refresh dynamic profiles before tier policy setup."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "deploy" / "macos" / "install.sh"
SYNC_MARKER = "# P14-M: make the router's model catalog agree"
NEXT_MARKER = "# P13k: the planner's model"


class InstallerModelCatalogOrderTests(unittest.TestCase):
    def _run_fragment(self, *, fail_sync: bool = False,
                      with_openclaw: bool = True) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            state = root / "state"
            repo = root / "repo"
            venv = root / "venv" / "bin"
            (home / ".openclaw").mkdir(parents=True)
            if with_openclaw:
                (home / ".openclaw" / "openclaw.json").write_text("{}")
            (repo / "scripts").mkdir(parents=True)
            (repo / "src").mkdir()
            (repo / "scripts" / "sync_openclaw_model_catalog.py").write_text("# probe\n")
            state.mkdir()
            venv.mkdir(parents=True)
            log = root / "calls.log"
            fake_python = venv / "python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  */sync_openclaw_model_catalog.py)\n"
                "    echo sync >> \"$CALL_LOG\"\n"
                "    [ \"${FAIL_SYNC:-0}\" = 1 ] && exit 17\n"
                "    : > \"$CATALOG_READY\";;\n"
                "  -m)\n"
                "    [ \"$2\" = dalton_core.document_extraction_setup ] || exit 18\n"
                "    [ ! -f \"$HOME/.openclaw/openclaw.json\" ] || "
                "[ -f \"$CATALOG_READY\" ] || exit 19\n"
                "    echo extraction >> \"$CALL_LOG\";;\n"
                "  *) exit 20;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            code = INSTALL.read_text(encoding="utf-8")
            fragment = code[code.index(SYNC_MARKER):code.index(NEXT_MARKER)]
            command = "set -e\n" + "\n".join([
                f"repo_root={repo}", f"venv_dir={root / 'venv'}",
                f"state_dir={state}", f"config_path={root / 'service.json'}",
                fragment,
            ])
            env = {**os.environ, "HOME": str(home), "CALL_LOG": str(log),
                   "CATALOG_READY": str(root / "catalog.ready"),
                   "FAIL_SYNC": "1" if fail_sync else "0"}
            completed = subprocess.run(
                ["zsh", "-c", command], text=True, capture_output=True, env=env)
            calls = log.read_text().splitlines() if log.is_file() else []
            return completed, calls

    def test_old_router_is_synchronized_before_cheap_tier_setup(self) -> None:
        completed, calls = self._run_fragment()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, ["sync", "extraction"])

    def test_failed_catalog_sync_never_enters_extraction_setup(self) -> None:
        completed, calls = self._run_fragment(fail_sync=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls, ["sync"])
        self.assertIn("model catalog sync failed", completed.stderr)

    def test_offline_install_keeps_the_existing_extraction_path(self) -> None:
        completed, calls = self._run_fragment(with_openclaw=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, ["extraction"])


if __name__ == "__main__":
    unittest.main()
