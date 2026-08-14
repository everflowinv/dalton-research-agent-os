"""Static distribution checks; wheel installation is part of release acceptance."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_runtime_sql_and_all_contract_schemas_are_declared(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = config["tool"]["setuptools"]["package-data"]
        data_files = config["tool"]["setuptools"]["data-files"]

        for runtime_asset in (
            "schema.sql",
            "capability_schema.sql",
            "scheduler_schema.sql",
            "capability_catalog_schema.sql",
            "model_router_schema.sql",
            "observability_schema.sql",
            "dashboard_schema.sql",
            "agenda_schema.sql",
            "connector_schema.sql",
            "dashboard.html",
            "agenda_control.html",
        ):
            self.assertIn(runtime_asset, package_data["dalton_core"])
        self.assertIn("contracts/*.schema.json", data_files["share/dalton-core/contracts"])
        schemas = list((ROOT / "contracts").glob("*.schema.json"))
        self.assertGreaterEqual(len(schemas), 34)


if __name__ == "__main__":
    unittest.main()
