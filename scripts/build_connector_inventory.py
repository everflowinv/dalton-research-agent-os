#!/usr/bin/env python3
"""Regenerate the deterministic P1-0 connector inventory package data."""

from __future__ import annotations

import json
from pathlib import Path

from dalton_core.connector_inventory import build_connector_inventory


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "dalton_core" / "connector_inventory"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    inventory = build_connector_inventory()
    write_json(TARGET / "index.json", inventory["index"])
    for kind in ("templates", "fixtures", "proposals"):
        directory = "profiles" if kind == "templates" else kind
        for slug, value in inventory[kind].items():
            write_json(TARGET / directory / f"{slug}.json", value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
