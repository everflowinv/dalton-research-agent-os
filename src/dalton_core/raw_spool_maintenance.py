"""Operator maintenance commands for the lossless raw spool archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .raw_spool import RawSpool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("archive", "restore"))
    parser.add_argument("--spool-dir", type=Path, required=True)
    parser.add_argument("--max-total-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--min-age-seconds", type=int, default=7 * 24 * 60 * 60)
    parser.add_argument("--target-free-bytes", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spool = RawSpool(
        args.spool_dir,
        max_total_bytes=args.max_total_bytes,
        archive_after_seconds=args.min_age_seconds,
    )
    if args.command == "restore":
        result = spool.restore_archived_objects()
    else:
        result = spool.archive_old_objects(
            target_free_bytes=args.target_free_bytes,
            min_age_seconds=args.min_age_seconds,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
