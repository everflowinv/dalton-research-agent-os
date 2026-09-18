#!/usr/bin/env python3
"""Backfill the venv integrity marker (dalton-release.json) of a built release.

``scripts/build_release.py`` wrote no ``venv/dalton-release.json`` between
2026-09-17 05:00 and this fix, and every workspace install or repair validates
that marker before pointing a workspace at the release.  This reads the
release's own ``release-manifest.json`` (wheel hash, release hash, dependency
lock hash), checks the repository's dependency lock still matches, and writes
the marker ``install_release`` would have written.

    .venv/bin/python scripts/write_release_marker.py ~/.dalton/runtime/releases/<hash>          # dry run
    .venv/bin/python scripts/write_release_marker.py ~/.dalton/runtime/releases/<hash> --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.workspace_release import MARKER, validate_release, write_release_marker  # noqa: E402

DEFAULT_LOCK = ROOT / "deploy" / "release" / "dependency-lock.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("release", type=Path, help="发布目录（含 release-manifest.json 与 venv/）")
    parser.add_argument("--dependency-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    release = args.release.expanduser().resolve()
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    lock = json.loads(args.dependency_lock.expanduser().read_text(encoding="utf-8"))
    if lock["dependency_lock_hash"] != manifest["dependency_lock_hash"]:
        print("STOP: 仓库里的依赖锁与该发布记录的锁哈希不一致，不能据此补写清单", file=sys.stderr)
        return 2
    venv = release / "venv"
    marker = venv / MARKER
    print(json.dumps({
        "release_hash": manifest["release_hash"], "venv": str(venv),
        "marker": str(marker), "marker_exists": marker.exists(),
        "wheel_sha256": manifest["wheel_sha256"],
        "dependency_wheels": len(lock["dependency_wheels"]),
    }, ensure_ascii=False, indent=1))
    if marker.exists():
        try:
            validate_release(venv, manifest["release_hash"])
            print("清单已存在且有效，无需补写")
            return 0
        except Exception as exc:  # noqa: BLE001 - an invalid marker is what we are here to replace
            print(f"现有清单无效，将重写：{exc}")
    if not args.apply:
        print("dry run；加 --apply 才写入", file=sys.stderr)
        return 0
    record = write_release_marker(
        venv, release_hash=manifest["release_hash"], wheel_sha256=manifest["wheel_sha256"],
        dependency_lock_hash=lock["dependency_lock_hash"], dependency_wheels=lock["dependency_wheels"])
    print(json.dumps({"written": str(marker), "files": len(record["files"]),
                      "content_hash": record["content_hash"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
