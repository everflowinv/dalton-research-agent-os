#!/usr/bin/env python3
"""Emit a secret-free Dalton/OpenClaw model-catalog drift report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.openclaw_catalog_reconcile import (
    OpenClawCatalogError,
    load_openclaw_config,
    reconcile_openclaw_model_catalog,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare OpenClaw provider/broker models with Dalton verifier profiles."
    )
    parser.add_argument("--openclaw-config", type=Path, required=True)
    parser.add_argument(
        "--calibrated-profile-id", action="append", default=[], dest="calibrated_profile_ids"
    )
    args = parser.parse_args(argv)
    try:
        report = reconcile_openclaw_model_catalog(
            load_openclaw_config(args.openclaw_config),
            checked_at=datetime.now(timezone.utc),
            calibrated_profile_ids=args.calibrated_profile_ids,
        )
    except OpenClawCatalogError as exc:
        print(f"catalog reconciliation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
