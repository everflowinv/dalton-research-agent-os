"""Guardrails that keep live-system access behind explicit bridge modules."""

from __future__ import annotations

import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "dalton_core"
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]


class IsolationTests(unittest.TestCase):
    def test_installed_openclaw_tests_never_auto_discover_the_user_install(self) -> None:
        paths = [
            REPOSITORY_ROOT / "tests/test_portable_verifier_contracts.py",
            REPOSITORY_ROOT / "tests/test_openclaw_controlled_transport_patch.py",
        ]
        for path in paths:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("DALTON_TEST_OPENCLAW_INSTALL_ROOT", source)
                self.assertNotIn("Path.home().glob", source)
                self.assertNotIn("/Users/everflow/.openclaw", source)

    def test_source_has_no_live_system_references(self) -> None:
        forbidden = (
            "workspace-chem",
            "coverage.db",
            "/Users/everflow/.openclaw",
            "~/.openclaw",
            "openclaw.json",
            "openclaw-agent.sqlite",
            "crontab",
        )
        offenders: list[str] = []
        bridge_allowances = {
            "legacy_migration.py": {"workspace-chem", "coverage.db"},
            # Explicit network bridge: resolves only beside the supplied broker
            # socket (or an explicit config path), never a global home directory.
            "web_search_provider.py": {"openclaw.json"},
        }

        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            for token in forbidden:
                if token in source and token not in bridge_allowances.get(relative, set()):
                    offenders.append(f"{relative}: {token}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
