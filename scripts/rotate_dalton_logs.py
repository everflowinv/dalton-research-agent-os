#!/usr/bin/env python3
"""轮转 ~/Library/Logs/Dalton 下的服务日志。

这些文件从 2026-08-14 起就没有轮转过，因为 macOS 上唯一"标准"的办法
（/etc/newsyslog.d）需要 root，而 Dalton 全套是用户级 LaunchAgent。

做法选的是 copy-truncate，不是 rename-create：launchd 以 O_APPEND 打开
StandardOutPath/StandardErrorPath，并且在服务重启之前一直持有那个 fd。rename
之后服务会继续往已经改名的 inode 里写，新文件永远是空的，只有重启服务才能修
好 —— 而为了轮转日志重启整个研究流水线是不可接受的。copy-truncate 把内容复制
走再把原文件就地截断为 0；O_APPEND 保证下一次写从 0 开始，不会留空洞。

代价是截断和复制之间的极少量新字节可能丢失。对 stderr 日志来说这个代价是对的。

默认 dry-run：只打印会做什么。--apply 才动文件。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "dalton-log-rotation:0.1"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "Dalton"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_KEEP = 5


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rotate_one(path: Path, *, keep: int, apply: bool) -> dict[str, Any]:
    archive = path.with_name(f"{path.name}.{stamp()}.gz")
    action = {"log": str(path), "size_bytes": path.stat().st_size,
              "archive": str(archive), "rotated": False, "pruned": []}
    if apply:
        # 复制 → 压缩 → 就地截断。先落地归档，再截断，任何一步失败都不会
        # 同时丢掉两份。
        with path.open("rb") as source, gzip.open(archive, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.chmod(archive, 0o600)
        descriptor = os.open(path, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)
        action["rotated"] = True
    archives = sorted(path.parent.glob(f"{path.name}.*.gz"))
    doomed = archives[:-keep] if len(archives) > keep else []
    for old in doomed:
        action["pruned"].append(str(old))
        if apply:
            old.unlink()
    return action


def rotate(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.log_dir.expanduser().resolve()
    if not directory.is_dir():
        raise RuntimeError(f"日志目录不存在：{directory}")
    actions = []
    for path in sorted(directory.glob("*.log")):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size < args.max_bytes:
            actions.append({"log": str(path), "size_bytes": size,
                            "rotated": False, "reason": "未达到轮转阈值"})
            continue
        actions.append(rotate_one(path, keep=args.keep, apply=args.apply))
    return {"schema_version": SCHEMA, "writes_performed": bool(args.apply),
            "log_dir": str(directory), "max_bytes": args.max_bytes,
            "keep": args.keep, "actions": actions,
            "rotated_count": sum(1 for row in actions if row.get("rotated"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="超过这个大小才轮转（默认 16 MiB）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="每个日志保留多少个历史归档（默认 5）")
    parser.add_argument("--apply", action="store_true", help="真正轮转；缺省只打印计划")
    args = parser.parse_args(argv)
    if args.keep < 1 or args.max_bytes < 1:
        parser.error("--keep 和 --max-bytes 都必须为正")
    try:
        result = rotate(args)
    except (OSError, RuntimeError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
