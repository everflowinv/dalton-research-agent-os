"""Small process boundary for bounded, workspace-local SEC ticker lookup."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    cache = Path(args.state_dir).expanduser().resolve() / "sec-edgar-cache"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(cache)
    os.environ["EDGAR_DATA_DIR"] = str(cache)
    from edgar import Company, set_identity

    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        raise RuntimeError("EDGAR_IDENTITY is required for SEC requests")
    set_identity(identity)
    company = Company(args.ticker.upper())
    print(json.dumps({"ticker": args.ticker.upper(), "cik": str(company.cik),
                      "name": str(company.name)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
