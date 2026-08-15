#!/usr/bin/env python3
"""Regenerate deterministic public and MCP reference-shadow fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from dalton_core.recorded_source_adapter import build_recorded_source_fixtures
from dalton_core.recorded_alphaengine_adapter import (
    build_recorded_alphaengine_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "dalton_core" / "reference_shadow_fixtures"


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    for source, fixture in build_recorded_source_fixtures().items():
        (TARGET / f"{source}.json").write_text(
            json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    (TARGET / "alphaengine.json").write_text(
        json.dumps(
            build_recorded_alphaengine_fixture(), indent=2, sort_keys=True
        ) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
