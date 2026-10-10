#!/usr/bin/env python3
"""Would each published capability's lease pass its approval check? Read-only.

``validate_lease_for_use`` refuses a lease with "capability approval changed
after lease issuance" when the approval receipt the governance record resolves
*now* differs from the one sealed into the published descriptor.  ws-7d's SEC
filings index failed every search that way from 2026-09-27 (its descriptor
sealed the whole ``sec`` fixture manifest; 485c9ad6 resolved the operation
projection).  This resolves every catalog's current descriptor against the
governance records in the same state directory, exactly as lease use does,
and says which match.

Nothing is written and nothing is called: catalogs are opened ``mode=ro`` and
governance records are read as files.  Exit status 1 if any descriptor that
has a governance record would be refused.

    PYTHONPATH=src python scripts/check_capability_approval_receipts.py --state-dir <state>/dalton-core
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def check(state: Path) -> dict[str, Any]:
    from dalton_core.connector_governance import load_connector_governance

    governance: dict[str, list[Path]] = {}
    for path in sorted((state / "connector-governance").glob("*.json")):
        try:
            capability = json.loads(path.read_text(encoding="utf-8")).get("capability_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(capability, str):
            governance.setdefault(capability, []).append(path)
    rows = []
    for catalog in sorted(state.glob("catalog*.sqlite")):
        try:
            connection = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True, timeout=5)
            current = connection.execute(
                "SELECT d.descriptor_json FROM capability_current c "
                "JOIN capability_descriptor_versions d ON d.revision_ref=c.revision_ref").fetchall()
            connection.close()
        except sqlite3.Error as exc:
            rows.append({"catalog": catalog.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for (wire,) in current:
            descriptor = json.loads(wire)
            sealed = descriptor["approval"]
            row: dict[str, Any] = {
                "catalog": catalog.name, "capability_id": descriptor["id"],
                "revision_ref": descriptor.get("revision_ref"),
                "sealed_fixture_manifest_hash": sealed["fixture_manifest_hash"],
                "sealed_effective_from": sealed.get("effective_from"),
                "governance": []}
            query = {"capability_id": descriptor["id"],
                     "source_ref": descriptor["source"]["source_ref"],
                     "source_hash": descriptor["source_hash"],
                     "schema_hash": descriptor["schema_hash"],
                     "sealed_fixture_manifest_hash": sealed["fixture_manifest_hash"]}
            for path in governance.get(descriptor["id"], ()):
                try:
                    receipt = load_connector_governance(path).approval(query)
                except Exception as exc:  # noqa: BLE001 - reported per record
                    row["governance"].append({"record": path.name,
                                              "error": f"{type(exc).__name__}: {exc}"[:200]})
                    continue
                row["governance"].append({
                    "record": path.name,
                    "applies": receipt is not None,
                    "receipt_matches": (receipt is not None
                                        and receipt["receipt_hash"] == sealed["receipt_hash"]),
                    "resolved_fixture_manifest_hash": (None if receipt is None
                                                       else receipt["fixture_manifest_hash"]),
                })
            applicable = [item for item in row["governance"] if item.get("applies")]
            row["lease_would_pass"] = (None if not applicable
                                       else any(item["receipt_matches"] for item in applicable))
            rows.append(row)
    return {"state_dir": str(state), "descriptors": rows,
            "refused": [row["capability_id"] for row in rows
                        if row.get("lease_would_pass") is False]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = check(args.state_dir.expanduser())
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
