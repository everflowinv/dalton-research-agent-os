#!/usr/bin/env python3
"""删除没有任何东西引用、又不在最近 N 个里的 release venv。

38 个 release venv 占了 14 GB，是磁盘在 2026-09-16 只剩 3.6 GB 的主因。手工
删很危险：一个还被某个 plist、manager.json、workspace.json 或发布指针引用的
release 被删掉，对应服务下次启动就直接起不来。

所以这里的规则是"被引用就绝不删"，并且引用面要全：

  * ~/Library/LaunchAgents 下所有 space.lumos.dalton*/com.dalton.* 的 plist
  * ~/.dalton/manager.json 的 release_path / release_ref / shared_readonly_paths
  * ~/.dalton/workspaces/ * /workspace.json 的同三个字段
  * current-release.json 的 release_ref
  * 最近 N 个（按 mtime，默认 3）
  * 任何正在运行的进程的可执行路径

默认 dry-run：只打印会删哪些、能回收多少字节。--apply 才真的删。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
SCHEMA = "dalton-release-gc:0.1"
HEX64 = re.compile(r"[0-9a-f]{64}")


def hashes_in(text: str) -> set[str]:
    return set(HEX64.findall(text))


def tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def referenced(args: argparse.Namespace) -> dict[str, list[str]]:
    """每个被引用的 release 哈希 → 引用它的地方。"""

    found: dict[str, list[str]] = {}

    def note(digest: str, where: str) -> None:
        found.setdefault(digest, [])
        if where not in found[digest]:
            found[digest].append(where)

    agents_dir = args.launch_agents_dir.expanduser().resolve()
    for pattern in ("space.lumos.dalton*.plist", "com.dalton.*.plist"):
        for path in sorted(agents_dir.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # 二进制 plist：用 plutil 转成可读形式再扫。
                text = subprocess.run(["plutil", "-p", str(path)], capture_output=True,
                                      text=True, check=False).stdout
            for digest in hashes_in(text):
                note(digest, f"launchagent:{path.name}")

    for path in [args.manager.expanduser().resolve(),
                 *sorted((args.host_root.expanduser().resolve() / "workspaces")
                         .glob("*/workspace.json"))]:
        if not path.is_file():
            continue
        for digest in hashes_in(path.read_text(encoding="utf-8")):
            note(digest, f"config:{path}")

    for path in args.pointer:
        pointer = Path(path).expanduser()
        if pointer.is_file():
            try:
                value = json.loads(pointer.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for digest in hashes_in(str(value.get("release_ref", ""))):
                note(digest, f"pointer:{pointer.name}")

    listing = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True,
                             text=True, check=False).stdout
    for line in listing.splitlines():
        if "/runtime/releases/" not in line:
            continue
        for digest in hashes_in(line):
            note(digest, "running-process")
    return found


def plan(args: argparse.Namespace) -> dict[str, Any]:
    root = args.releases_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"release 目录不存在：{root}")
    releases = sorted((path for path in root.iterdir()
                       if path.is_dir() and HEX64.fullmatch(path.name)),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    keep_recent = {path.name for path in releases[: args.keep_recent]}
    uses = referenced(args)
    rows = []
    for path in releases:
        reasons = list(uses.get(path.name, []))
        if path.name in keep_recent:
            reasons.append(f"recent:最近 {args.keep_recent} 个之一")
        rows.append({
            "release": path.name,
            "path": str(path),
            "bytes": tree_bytes(path),
            "kept_because": reasons,
            "deletable": not reasons,
        })
    deletable = [row for row in rows if row["deletable"]]
    return {
        "schema_version": SCHEMA,
        "writes_performed": False,
        "releases_root": str(root),
        "keep_recent": args.keep_recent,
        "release_count": len(rows),
        "deletable_count": len(deletable),
        "reclaimable_bytes": sum(row["bytes"] for row in deletable),
        "releases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--releases-root", type=Path,
                        default=HOME / ".dalton" / "runtime" / "releases")
    parser.add_argument("--launch-agents-dir", type=Path,
                        default=HOME / "Library" / "LaunchAgents")
    parser.add_argument("--manager", type=Path, default=HOME / ".dalton" / "manager.json")
    parser.add_argument("--host-root", type=Path, default=HOME / ".dalton")
    parser.add_argument("--pointer", action="append", default=[
        str(HOME / "Projects" / "dalton-owner-activation-20260910"
            / "current-release.json")])
    parser.add_argument("--keep-recent", type=int, default=3)
    parser.add_argument("--apply", action="store_true", help="真正删除；缺省只打印计划")
    args = parser.parse_args(argv)
    if args.keep_recent < 1:
        parser.error("--keep-recent 至少是 1")
    try:
        result = plan(args)
    except (OSError, RuntimeError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    if args.apply:
        deleted = []
        for row in result["releases"]:
            if not row["deletable"]:
                continue
            # 删之前再算一次引用：这一步之间可能刚刚有人切了发布。
            if row["release"] in referenced(args):
                row["deletable"] = False
                row["kept_because"] = ["raced:删除前复查时出现了引用"]
                continue
            shutil.rmtree(row["path"])
            deleted.append(row["release"])
        result["deleted"] = deleted
        result["writes_performed"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
