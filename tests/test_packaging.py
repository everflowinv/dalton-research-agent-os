"""Static distribution checks; wheel installation is part of release acceptance.

The declared package data used to be a list of every schema file by name, and
this test asserted that a hand-picked subset of those names appeared in it --
which is the same list checked twice, and neither copy could notice a file
that existed on disk and in neither list.  A lane that adds its own schema and
forgets the declaration ships a wheel with a missing migration, and that
failure surfaces on someone else's machine at install time.

So this asks the question that actually matters: does every runtime asset in
``src/dalton_core`` match a declared pattern?  It evaluates the patterns
rather than reading them, so a glob and a literal are checked the same way.
"""

from __future__ import annotations

import fnmatch
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "dalton_core"


def declared_patterns() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config["tool"]["setuptools"]["package-data"]["dalton_core"])


def covered(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


class PackagingTests(unittest.TestCase):
    def test_every_schema_file_on_disk_is_packaged(self) -> None:
        patterns = declared_patterns()
        schemas = sorted(PACKAGE.glob("*.sql"))
        # A Core with no schema at all would pass an "every schema is
        # declared" check vacuously.
        self.assertGreaterEqual(len(schemas), 37)
        undeclared = [
            path.name for path in schemas
            if not covered(path.name, patterns)
        ]
        self.assertEqual(undeclared, [])

    def test_a_new_lane_schema_needs_no_pyproject_edit(self) -> None:
        # The point of the glob: the name a lane will actually use.
        patterns = declared_patterns()
        for hypothetical in ("market_price_schema.sql", "claim_index_schema.sql",
                             "forecast_driver_schema.sql",
                             "analyst_journal_schema.sql"):
            self.assertTrue(
                covered(hypothetical, patterns),
                f"{hypothetical} would not be packaged",
            )

    def test_the_other_runtime_assets_are_still_declared(self) -> None:
        patterns = declared_patterns()
        for runtime_asset in (
            "thesis-impact-verifier-provider-output-v0.2.schema.json",
            "thesis-impact-verifier-output-v0.2.schema.json",
            "thesis-impact-verifier-decision-provider-output-v0.1.schema.json",
            "dashboard.html",
            "cockpit_control.html",
            "cockpit_control_legacy.html",
            "reference_shadow_fixtures/one.json",
            "calibration_fixtures/one.json",
            "connector_inventory/index.json",
            "connector_inventory/profiles/one.json",
            "connector_inventory/fixtures/one.json",
            "connector_inventory/proposals/one.json",
        ):
            self.assertTrue(covered(runtime_asset, patterns), runtime_asset)

    def test_every_packaged_json_and_html_asset_on_disk_is_declared(self) -> None:
        patterns = declared_patterns()
        undeclared: list[str] = []
        for path in sorted(PACKAGE.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".json"):
                continue
            relative = path.relative_to(PACKAGE).as_posix()
            if "__pycache__" in relative or relative.startswith("plugins/"):
                continue
            if not covered(relative, patterns):
                undeclared.append(relative)
        self.assertEqual(undeclared, [])

    def test_the_contract_schemas_ship_as_data_files(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        data_files = config["tool"]["setuptools"]["data-files"]
        self.assertIn("contracts/*.schema.json", data_files["share/dalton-core/contracts"])
        schemas = list((ROOT / "contracts").glob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 34)


if __name__ == "__main__":
    unittest.main()
