"""Conservative, read-only disk sizing for the macOS installer."""
from __future__ import annotations
import argparse, os, shutil
from pathlib import Path
from typing import Callable

BUILD_COPIES = 3  # build tree, wheel/archive, and pip's unpacked candidate
METADATA_FLOOR_BYTES = 64 * 1024 * 1024
RESERVE_PERCENT = 10

def tree_bytes(path: Path) -> int:
    """Logical bytes below *path*, without following symlinks."""
    if not path.exists(): return 0
    if path.is_file(): return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            item = Path(root) / name
            if not item.is_symlink(): total += item.stat().st_size
    return total

def required_bytes(*, source: int, runtime: int, databases: int,
                   reserve: int | None = None) -> int:
    transient = source * BUILD_COPIES + runtime + databases
    margin = (max(METADATA_FLOOR_BYTES, transient * RESERVE_PERCENT // 100)
              if reserve is None else reserve)
    if min(source, runtime, databases, margin) < 0:
        raise ValueError("disk sizing values must be non-negative")
    return transient + margin

def existing_ancestor(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists():
        if candidate.parent == candidate: raise FileNotFoundError(path)
        candidate = candidate.parent
    return candidate

def check_space(*, repo_root: Path, dalton_root: Path, reserve: int | None = None,
                usage: Callable = shutil.disk_usage) -> dict[str, int]:
    source = tree_bytes(repo_root)
    runtime = tree_bytes(dalton_root / "runtime")
    databases = sum(tree_bytes(path) for path in
                    (dalton_root / "state" / "dalton-core").glob("*.sqlite"))
    needed = required_bytes(source=source, runtime=runtime, databases=databases,
                            reserve=reserve)
    free = usage(existing_ancestor(dalton_root)).free
    if free < needed:
        raise RuntimeError(f"insufficient disk space: need {needed} bytes, have {free} bytes "
                           f"(source={source}, runtime={runtime}, databases={databases})")
    return {"required_bytes": needed, "free_bytes": free, "source_bytes": source,
            "runtime_bytes": runtime, "database_bytes": databases}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dalton-root", type=Path, required=True)
    parser.add_argument("--reserve-bytes", type=int)
    args = parser.parse_args(argv)
    try:
        result = check_space(repo_root=args.repo_root, dalton_root=args.dalton_root,
                             reserve=args.reserve_bytes)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"STOP: Dalton install disk preflight failed: {exc}\n")
    print(f"PASS: Dalton install disk preflight requires {result['required_bytes']} bytes; "
          f"{result['free_bytes']} bytes available")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
