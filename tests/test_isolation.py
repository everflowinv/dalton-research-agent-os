"""Guardrails that keep live-system access behind explicit bridge modules."""

from __future__ import annotations

import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "dalton_core"


class IsolationTests(unittest.TestCase):
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
