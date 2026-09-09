#!/usr/bin/env python3
"""Make the Dalton router's model catalog agree with the OpenClaw broker's.

Append-only and idempotent.  A broker profile with no Dalton profile is
registered; a Dalton profile the broker no longer offers gets a *retired*
version rather than being deleted, so every route decision that ever named it
still resolves; a retired profile the broker offers again comes back live.
Running it twice changes nothing the second time.

The report it prints is secret-free by construction: it names profile ids,
model references and one hash of the broker catalog, and reads nothing else out
of the OpenClaw configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.model_router import ModelRouter
from dalton_core.openclaw_catalog_reconcile import (
    OpenClawCatalogError,
    catalog_sync_status,
    load_openclaw_config,
    sync_openclaw_model_catalog,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--openclaw-config", type=Path, required=True)
    parser.add_argument("--model-router-db", type=Path, required=True)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report the drift and change nothing; exit 2 when out of sync",
    )
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        config = load_openclaw_config(args.openclaw_config)
    except OpenClawCatalogError as exc:
        print(f"catalog sync failed: {exc}", file=sys.stderr)
        return 1
    if not args.model_router_db.exists():
        print(
            f"catalog sync failed: no model router at {args.model_router_db}",
            file=sys.stderr,
        )
        return 1
    try:
        with ModelRouter(args.model_router_db, read_only=args.check_only) as router:
            report = (
                catalog_sync_status(router, config, checked_at=now)
                if args.check_only
                else sync_openclaw_model_catalog(router, config, checked_at=now)
            )
    except OpenClawCatalogError as exc:
        print(f"catalog sync failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.check_only and not report["catalog_in_sync"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
