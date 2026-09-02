#!/usr/bin/env python3
"""Build ``create_coverage_mission`` params for the next mission version.

Reads the active CoverageMission from a Core (read-only), keeps every field
and exact binding as-is, appends the requested ``may_write`` scopes and emits
the human-governance params file for ``dalton-gov``.  Nothing is written to
the source Core.

Usage::

    python scripts/build_mission_v2_params.py \
        --source-core "$HOME/Library/Application Support/Dalton/state/dalton-core/core.sqlite" \
        --mission-ref coverage-mission:us-it-services \
        --add-scope forecast_reconciliation \
        --output temp/us-it-services-mission-v2.params.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.coverage_mission import (  # noqa: E402
    AUTOMATION_WRITE_SCOPES,
    validate_coverage_mission_version,
)

BODY_FIELDS = (
    "title", "objective", "industry_ref", "universe", "research_questions",
    "deliverables", "source_plan", "bindings", "autonomy", "budget",
)


def build_next_version_params(
    active: dict[str, Any], *, add_scopes: list[str], title: str | None = None,
) -> dict[str, Any]:
    for scope in add_scopes:
        if scope not in AUTOMATION_WRITE_SCOPES:
            raise ValueError(f"{scope} is outside the frozen automation write vocabulary")
    may_write = list(active["autonomy"]["may_write"])
    for scope in add_scopes:
        if scope not in may_write:
            may_write.append(scope)
    params: dict[str, Any] = {"mission_ref": active["mission_ref"]}
    for field in BODY_FIELDS:
        params[field] = json.loads(json.dumps(active[field]))
    params["autonomy"]["may_write"] = may_write
    if title is not None:
        params["title"] = title
    slug = active["mission_ref"].split(":", 1)[1]
    params["version_id"] = f"coverage-mission-version:{slug}:{active['version'] + 1}"
    params["prior_version_ref"] = active["id"]
    params["idempotency_key"] = f"coverage-mission:{slug}:{active['version'] + 1}"
    return params


def read_active_mission(source_core: Path, mission_ref: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{source_core}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        pointer = connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
            (mission_ref,),
        ).fetchone()
        if pointer is None:
            raise RuntimeError(f"no active mission for {mission_ref}")
        row = connection.execute(
            "SELECT record_json, content_hash FROM coverage_mission_versions WHERE mission_version_id=?",
            (pointer["mission_version_id"],),
        ).fetchone()
    finally:
        connection.close()
    wire = validate_coverage_mission_version(json.loads(row["record_json"]))
    if wire["content_hash"] != row["content_hash"]:
        raise RuntimeError("active mission row hash drifted")
    return wire


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-core", type=Path, required=True)
    parser.add_argument("--mission-ref", default="coverage-mission:us-it-services")
    parser.add_argument("--add-scope", action="append", default=[])
    parser.add_argument("--title")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    active = read_active_mission(args.source_core, args.mission_ref)
    params = build_next_version_params(active, add_scopes=args.add_scope, title=args.title)
    text = json.dumps(params, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
